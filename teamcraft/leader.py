"""M1-07 minimal single-machine Leader lease/fencing over SQLite.

Accepted M0 Review requires that runtime admission and stale-leader write
rejection do NOT rest on ``claim_command`` alone: a command claim orders one
command's execution, but it does not make its holder a unique Leader whose
later writes may be trusted.  This module provides the missing piece as a
purely local, transactional lease on the same ``teamcraft.db``:

* ``leader_lease`` (schema v4) holds at most one live holder per scope with a
  monotonically increasing ``epoch`` (never reused, even across process
  crashes and rollbacks; a released lease leaves its row behind permanently —
  release is a state transition, never history deletion);
* admission is transactional (``BEGIN IMMEDIATE``): exactly one concurrent
  acquirer wins; a live unexpired lease held by another holder is refused;
* stale-leader write rejection: every guarded mutation re-verifies
  ``(scope, holder, epoch, not expired)`` INSIDE its own write transaction.
  An old leader whose lease expired or was re-acquired by someone else can
  therefore never commit a success write;
* an expired lease is NOT authority: reacquisition bumps the epoch, so any
  in-flight work from the previous epoch fails its guard at commit time.
  That is exactly the "external call returns after the lease was lost" case:
  the caller must not commit success and instead records the command as
  UNKNOWN requiring reconciliation (see ``runtime_adapter``).

Deliberate scope limits (CEO M1-07 gate D): single machine, local SQLite only,
NO background service, no clock synchronized across hosts.  Clock source is
injectable for tests.  Leases guard AUTHORITATIVE commits; the flock-style
operation locks in ``teamctl`` remain only as cheap entry serialization and
are not a source of truth.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .clock import Clock, SystemClock
from .errors import TeamCraftError, ValidationError
from .storage import TeamCraftConnection, transaction


@contextlib.contextmanager
def _tx(conn: TeamCraftConnection) -> Iterator[TeamCraftConnection]:
    """Durability gate composed with any outer open transaction.

    Mirrors :func:`teamcraft.domain._tx`: when the caller already holds a
    transaction (e.g. :class:`teamcraft.runtime_adapter.RuntimeCore` bundling
    lease checks with state writes), this joins it instead of nesting a
    ``BEGIN`` SQLite forbids; standalone calls open their own IMMEDIATE
    transaction exactly as before.
    """

    if conn.in_transaction:
        yield conn
    else:
        with transaction(conn) as gated:
            yield gated


class LeaderError(TeamCraftError):
    """Base class for leader lease failures."""


class LeaderBusyError(LeaderError):
    """A live lease exists for the scope held by another holder."""

    def __init__(self, message: str, *, scope: str, holder_id: str,
                 epoch: int, expires_at: str) -> None:
        super().__init__(message)
        self.scope = scope
        self.holder_id = holder_id
        self.epoch = epoch
        self.expires_at = expires_at


class StaleLeaderError(LeaderError):
    """The presented lease is no longer current; success writes are refused."""

    def __init__(self, message: str, *, scope: str, epoch: int) -> None:
        super().__init__(message)
        self.scope = scope
        self.epoch = epoch


@dataclass(frozen=True)
class Lease:
    """A held lease token.  Callers pass it back to guarded operations."""

    scope: str
    holder_id: str
    epoch: int
    acquired_at: str
    expires_at: str


def project_scope(project_id: str) -> str:
    """Return the canonical Leader scope shared by every executor in a project."""
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValidationError("project_id must not be empty")
    return f"project:{project_id}"


def require_project_lease(
    conn: TeamCraftConnection,
    lease: Lease,
    project_id: str,
    *,
    clock: Clock | None = None,
) -> None:
    """Fail closed unless a live lease fences the specified project."""
    scope = project_scope(project_id)
    if lease.scope != scope:
        raise StaleLeaderError(
            f"leader lease scope {lease.scope!r} does not match project scope {scope!r}",
            scope=lease.scope, epoch=lease.epoch,
        )
    require_lease(conn, lease, clock=clock)


def _now_iso(clock: Clock | None) -> str:
    source = clock if clock is not None else SystemClock()
    return source.now().astimezone(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _is_expired(expires_at: str, now: str) -> bool:
    try:
        return _parse_iso(expires_at) <= _parse_iso(now)
    except ValueError as exc:  # unparsable lease row is corrupt: fail closed
        raise ValidationError(f"leader lease carries unparsable expiry: {exc}") from exc


def _read_row(conn: TeamCraftConnection, scope: str) -> Any | None:
    return conn.execute(
        "SELECT holder_id, epoch, acquired_at, expires_at, state"
        " FROM leader_lease WHERE scope = ?",
        (scope,),
    ).fetchone()


def acquire_or_renew(
    conn: TeamCraftConnection,
    scope: str,
    holder_id: str,
    *,
    ttl_seconds: int,
    clock: Clock | None = None,
) -> Lease:
    """Admission for ``scope``: take a new lease or renew our own.

    New acquisition (no row, a permanently retained ``released`` row, an
    expired held row, or our own expired row) bumps ``epoch`` by one —
    epochs are monotonic and never reused, because released rows are
    retained forever, so a zombie from any previous epoch can always be
    detected.  Renewal by the same holder of a live unexpired lease keeps
    the epoch and extends the deadline.  A live unexpired held row owned by
    another holder raises :class:`LeaderBusyError`.
    """

    if not scope.strip():
        raise ValidationError("lease scope must not be empty")
    if not holder_id.strip():
        raise ValidationError("holder_id must not be empty")
    if ttl_seconds <= 0:
        raise ValidationError("ttl_seconds must be positive")
    now = _now_iso(clock)
    with _tx(conn):
        row = _read_row(conn, scope)
        if row is None:
            lease = Lease(scope, holder_id, 1, now,
                        _shift(now, ttl_seconds))
            conn.execute(
                "INSERT INTO leader_lease (scope, holder_id, epoch, state,"
                " acquired_at, expires_at, released_at)"
                " VALUES (?, ?, ?, 'held', ?, ?, NULL)",
                (scope, holder_id, lease.epoch, now, lease.expires_at),
            )
            return lease
        holder, epoch, acquired_at, expires_at, state = (
            str(row[0]), int(row[1]), str(row[2]), str(row[3]), str(row[4]),
        )
        if (
            state == "held"
            and holder == holder_id
            and not _is_expired(expires_at, now)
        ):
            renewed = _shift(now, ttl_seconds)
            conn.execute(
                "UPDATE leader_lease SET expires_at = ? WHERE scope = ?",
                (renewed, scope),
            )
            return Lease(scope, holder_id, epoch, acquired_at, renewed)
        if state == "held" and not _is_expired(expires_at, now):
            raise LeaderBusyError(
                f"leader lease for scope {scope!r} is held by {holder!r}"
                f" (epoch {epoch}, expires {expires_at})",
                scope=scope, holder_id=holder, epoch=epoch, expires_at=expires_at,
            )
        bumped = epoch + 1
        lease = Lease(scope, holder_id, bumped, now, _shift(now, ttl_seconds))
        conn.execute(
            "UPDATE leader_lease SET holder_id = ?, epoch = ?, state = 'held',"
            " acquired_at = ?, expires_at = ?, released_at = NULL"
            " WHERE scope = ?",
            (holder_id, bumped, now, lease.expires_at, scope),
        )
        return lease


def release(conn: TeamCraftConnection, scope: str, holder_id: str,
            *, epoch: int | None = None,
            clock: Clock | None = None) -> bool:
    """Release our own lease (matching holder, and epoch when given).

    The row is RETAINED (``state='released'``): epoch history is durable and
    the next acquisition of this scope therefore continues the monotonic
    sequence instead of restarting at 1.  Releasing a lease that is already
    released is an idempotent ``True``; releasing a lease held by someone
    else (or at a stale epoch) is a ``False`` no-op that never touches the
    live lease.
    """

    with _tx(conn):
        row = _read_row(conn, scope)
        if row is None or str(row[0]) != holder_id:
            return False
        if epoch is not None and int(row[1]) != epoch:
            return False
        if str(row[4]) == "released":
            return True
        conn.execute(
            "UPDATE leader_lease SET state = 'released', released_at = ?"
            " WHERE scope = ?",
            (_now_iso(clock), scope),
        )
        return True


def current_lease(conn: TeamCraftConnection, scope: str,
                  *, clock: Clock | None = None) -> Lease | None:
    """The live lease for ``scope`` (``None`` when absent, released, or expired)."""

    now = _now_iso(clock)
    row = _read_row(conn, scope)
    if row is None or str(row[4]) != "held":
        return None
    lease = Lease(scope, str(row[0]), int(row[1]), str(row[2]), str(row[3]))
    if _is_expired(lease.expires_at, now):
        return None
    return lease


def lease_is_current(conn: TeamCraftConnection, lease: Lease,
                    *, clock: Clock | None = None) -> bool:
    live = current_lease(conn, lease.scope, clock=clock)
    return (
        live is not None
        and live.holder_id == lease.holder_id
        and live.epoch == lease.epoch
    )


def require_lease(conn: TeamCraftConnection, lease: Lease,
                  *, clock: Clock | None = None) -> None:
    """Guard for authoritative writes; MUST run inside the write transaction.

    Raises :class:`StaleLeaderError` when the lease row no longer matches this
    holder/epoch, has been released, or has expired — the caller's
    transaction must then be aborted (or converted into an UNKNOWN
    recording).
    """

    now = _now_iso(clock)
    row = _read_row(conn, lease.scope)
    if (
        row is None
        or str(row[0]) != lease.holder_id
        or int(row[1]) != lease.epoch
        or str(row[4]) != "held"
        or _is_expired(str(row[3]), now)
    ):
        raise StaleLeaderError(
            f"leader lease lost for scope {lease.scope!r} (our epoch"
            f" {lease.epoch}, holder {lease.holder_id!r}); success writes"
            " are refused and the outcome must be reconciled",
            scope=lease.scope, epoch=lease.epoch,
        )


def _shift(now_iso: str, seconds: int) -> str:
    return (_parse_iso(now_iso) + timedelta(seconds=seconds)).isoformat()
