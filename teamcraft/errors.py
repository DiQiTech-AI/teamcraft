"""Shared exception hierarchy for TeamCraft core boundaries."""


class TeamCraftError(Exception):
    """Base class for errors callers may handle without parsing messages."""


class ConfigurationError(TeamCraftError):
    """Configuration is missing or invalid."""


class ValidationError(TeamCraftError):
    """A value violates a core boundary contract."""


class StorageError(TeamCraftError):
    """Base class for SQLite storage boundary failures."""


class UnsupportedLocationError(StorageError):
    """The requested database path is outside the supported local-disk boundary.

    The checks are best-effort capability guards (absolute path, regular file,
    writable parent, WAL capability); they deliberately do NOT claim to detect
    every synced or network filesystem reliably.
    """


class SchemaTooNewError(StorageError):
    """The database schema is newer than this build supports.

    Raised before any write statement so the database is never downgraded,
    migrated, or otherwise modified by an older binary.
    """


class MigrationError(StorageError):
    """Migration cannot proceed safely (partial junk, version mismatch, ...)."""


class BackupError(StorageError):
    """Backup or restore cannot be performed safely."""


class CorruptDatabaseError(StorageError):
    """The database failed an integrity gate and must not be written to."""


class WriteOutsideTransactionError(StorageError):
    """A write was attempted through a public entry point outside the gate.

    Writes against a :class:`teamcraft.storage.TeamCraftConnection` must run
    inside ``teamcraft.transaction()``: only there is the schema version
    re-checked after the write lock is held.  This is raised instead of
    silently writing through an ungated ``execute``/``executemany``/``cursor``
    or an implicit-committing ``executescript``/``commit``.
    """


class DomainError(TeamCraftError):
    """Base class for M1-04 domain state-machine violations."""


class SchemaOutOfDateError(DomainError):
    """The database schema is valid but behind this build's head version.

    M1-04 domain services refuse to run against a lagging (e.g. v1) schema
    instead of failing deep inside an operation with an opaque SQL error.
    The database itself stays fully usable for storage-level contracts
    (connect/query/backup/restore); run ``teamcraft.migrate_to_head()`` to
    apply the forward-only v1 -> v2 migration first.
    """


class NotFoundError(DomainError):
    """A referenced entity (project, task, attempt, session) does not exist."""


class ConflictError(DomainError):
    """A uniqueness rule at the storage boundary was violated.

    Distinct from optimistic-locking conflicts (:class:`VersionConflictError`):
    this signals that a row with the same unique key already exists.
    """


class IllegalTransitionError(DomainError):
    """A state-machine transition is not part of the transition table.

    Carries the structured refusal (entity kind, current state, event,
    reason code) so callers never have to parse exception messages.
    """

    def __init__(
        self,
        message: str,
        *,
        entity: str,
        current: str,
        event: str,
        reason_code: str = "ILLEGAL_TRANSITION",
    ) -> None:
        super().__init__(message)
        self.entity = entity
        self.current = current
        self.event = event
        self.reason_code = reason_code


class DependencyCycleError(DomainError):
    """A task dependency edge would create (or already forms) a cycle.

    ``cycle`` is the concrete closed path, e.g. ``["A", "B", "C", "A"]``:
    consecutive entries are edges of the graph and the last entry repeats
    the first.  Self-dependencies are reported as ``["A", "A"]``.
    """

    def __init__(self, message: str, *, cycle: list[str]) -> None:
        super().__init__(message)
        self.cycle = list(cycle)


class VersionConflictError(DomainError):
    """An optimistic-locking (compare-and-swap) update found a newer version.

    Raised when a caller-supplied ``expected_version`` no longer matches the
    row's current ``version`` under the write lock; nothing was written.
    """

    def __init__(
        self, message: str, *, entity_id: str, expected: int, found: int
    ) -> None:
        super().__init__(message)
        self.entity_id = entity_id
        self.expected = expected
        self.found = found


class AttemptSlotOccupiedError(DomainError):
    """The task's single occupying-attempt slot is taken (DB-enforced).

    ``created``/``starting``/``active``/``unknown`` attempts all hold the
    slot — an unresolved (UNKNOWN) attempt must never allow parallel
    re-dispatch (M0 review 8.2-7, 8.2-11).  The backing constraint is the
    partial unique index ``ux_attempts_single_occupying``; this error is the
    domain-level mapping of violating it.
    """

    def __init__(self, message: str, *, task_id: str, occupying_id: str) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.occupying_id = occupying_id


class IdempotencyConflictError(ConflictError):
    """An idempotency key was reused with a DIFFERENT request (M1-05).

    Same key + identical payload/kind/evidence binding must reuse the stored
    result; presenting different content under an existing key is a client
    error (409 territory, PLAN §8.5) and nothing is written.
    """

    def __init__(
        self, message: str, *, idempotency_key: str, existing_command_id: str
    ) -> None:
        super().__init__(message)
        self.idempotency_key = idempotency_key
        self.existing_command_id = existing_command_id


class CommandStateConflictError(DomainError):
    """Compare-and-swap against a command's state lost the race (M1-05).

    The command exists but its persisted state no longer equals the caller's
    ``expected_state`` under the write lock; nothing was written (M0 review
    8.2-7: every execution admission re-checks the row atomically).
    """

    def __init__(
        self,
        message: str,
        *,
        command_id: str,
        expected_state: str,
        found_state: str,
    ) -> None:
        super().__init__(message)
        self.command_id = command_id
        self.expected_state = expected_state
        self.found_state = found_state
