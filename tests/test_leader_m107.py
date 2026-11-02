"""M1-07 Leader lease/fencing regression (schema v4 ``leader_lease``).

Covers the accepted M0 Review fencing requirements and the CEO review
correction: monotonic epochs survive release — rows are retained forever,
so release-then-reacquire CONTINUES the epoch sequence instead of restarting
at 1; release is idempotent and never touches foreign holders; and the
lease primitives compose inside an outer ``storage.transaction`` (they join
it instead of nesting a forbidden BEGIN).
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teamcraft import leader, storage
from teamcraft.errors import ValidationError


class _FixedClock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self.moment


class LeaderLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="m107-leader-")
        self.db = Path(self.temp.name) / "teamcraft.db"
        storage.migrate_to_head(self.db)
        self.conn = storage.connect(self.db)
        self.base = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp.cleanup()

    def _clock(self, offset_seconds: int = 0) -> _FixedClock:
        return _FixedClock(self.base + timedelta(seconds=offset_seconds))

    # ------------------------------------------------------------ admission

    def test_first_acquisition_starts_at_epoch_one(self) -> None:
        lease = leader.acquire_or_renew(self.conn, "member:dev", "A",
                                       ttl_seconds=30, clock=self._clock())
        self.assertEqual(lease.epoch, 1)
        self.assertEqual(lease.holder_id, "A")
        self.assertTrue(leader.lease_is_current(self.conn, lease,
                                               clock=self._clock()))

    def test_renewal_keeps_epoch_and_extends(self) -> None:
        first = leader.acquire_or_renew(self.conn, "member:dev", "A",
                                       ttl_seconds=30, clock=self._clock())
        second = leader.acquire_or_renew(self.conn, "member:dev", "A",
                                        ttl_seconds=30,
                                        clock=self._clock(20))
        self.assertEqual(first.epoch, second.epoch)
        self.assertGreater(second.expires_at, first.expires_at)

    def test_live_foreign_holder_is_refused(self) -> None:
        leader.acquire_or_renew(self.conn, "member:dev", "A", ttl_seconds=30,
                               clock=self._clock())
        with self.assertRaises(leader.LeaderBusyError) as ctx:
            leader.acquire_or_renew(self.conn, "member:dev", "B",
                                   ttl_seconds=30, clock=self._clock(5))
        self.assertEqual(ctx.exception.holder_id, "A")
        self.assertEqual(ctx.exception.epoch, 1)

    def test_expired_foreign_lease_taken_over_with_epoch_bump(self) -> None:
        leader.acquire_or_renew(self.conn, "member:dev", "A", ttl_seconds=30,
                               clock=self._clock())
        stolen = leader.acquire_or_renew(self.conn, "member:dev", "B",
                                        ttl_seconds=30, clock=self._clock(31))
        self.assertEqual(stolen.epoch, 2)
        self.assertEqual(leader.current_lease(self.conn, "member:dev",
                                             clock=self._clock(31)).holder_id,
                         "B")

    def test_own_expired_lease_reacquires_with_bumped_epoch(self) -> None:
        # A crashed-and-resurrected holder never reuses its old epoch.
        first = leader.acquire_or_renew(self.conn, "member:dev", "A",
                                       ttl_seconds=10, clock=self._clock())
        again = leader.acquire_or_renew(self.conn, "member:dev", "A",
                                       ttl_seconds=10, clock=self._clock(11))
        self.assertEqual(again.epoch, first.epoch + 1)

    # ---------------------------------- CEO regression: durable epoch history

    def test_release_retains_row_and_epoch_never_restarts(self) -> None:
        first = leader.acquire_or_renew(self.conn, "member:dev", "A",
                                       ttl_seconds=30, clock=self._clock())
        self.assertTrue(leader.release(self.conn, "member:dev", "A",
                                      epoch=first.epoch, clock=self._clock()))
        # Row retained (never deleted): epoch history is durable.
        rows = self.conn.execute(
            "SELECT state, epoch FROM leader_lease WHERE scope = 'member:dev'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0][0]), "released")
        # Reacquire continues the monotonic sequence — it must NOT reset to 1.
        second = leader.acquire_or_renew(self.conn, "member:dev", "B",
                                        ttl_seconds=30, clock=self._clock(1))
        self.assertEqual(second.epoch, first.epoch + 1)

    def test_release_is_idempotent_and_never_touches_foreign_leases(self) -> None:
        lease = leader.acquire_or_renew(self.conn, "member:dev", "A",
                                       ttl_seconds=30, clock=self._clock())
        self.assertTrue(leader.release(self.conn, "member:dev", "A",
                                      epoch=lease.epoch, clock=self._clock()))
        self.assertTrue(leader.release(self.conn, "member:dev", "A",
                                      epoch=lease.epoch, clock=self._clock()))
        self.assertFalse(leader.release(self.conn, "member:dev", "B"))
        self.assertFalse(leader.release(self.conn, "member:dev", "A",
                                       epoch=lease.epoch + 5))
        self.assertFalse(leader.release(self.conn, "scope:absent", "A"))

    def test_many_release_acquire_cycles_keep_monotonic_epochs(self) -> None:
        epoch = 0
        for cycle in range(5):
            lease = leader.acquire_or_renew(
                self.conn, "member:dev", f"holder-{cycle}", ttl_seconds=30,
                clock=self._clock(cycle * 100),
            )
            self.assertEqual(lease.epoch, epoch + 1)
            epoch = lease.epoch
            self.assertTrue(leader.release(self.conn, "member:dev",
                                          lease.holder_id, epoch=lease.epoch,
                                          clock=self._clock(cycle * 100 + 1)))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM leader_lease WHERE scope = 'member:dev'"
            ).fetchone()[0],
            1,
        )

    # ------------------------------------------------------------ guards

    def test_current_lease_none_when_released_or_expired(self) -> None:
        lease = leader.acquire_or_renew(self.conn, "member:dev", "A",
                                       ttl_seconds=20, clock=self._clock())
        self.assertIsNotNone(leader.current_lease(self.conn, "member:dev",
                                                 clock=self._clock(5)))
        leader.release(self.conn, "member:dev", "A", epoch=lease.epoch,
                      clock=self._clock(6))
        self.assertIsNone(leader.current_lease(self.conn, "member:dev",
                                              clock=self._clock(7)))

    def test_require_lease_rejects_released_taken_over_and_expired(self) -> None:
        lease = leader.acquire_or_renew(self.conn, "member:dev", "A",
                                       ttl_seconds=30, clock=self._clock())
        leader.require_lease(self.conn, lease, clock=self._clock(1))  # live
        leader.release(self.conn, "member:dev", "A", epoch=lease.epoch,
                      clock=self._clock(2))
        with self.assertRaises(leader.StaleLeaderError):
            leader.require_lease(self.conn, lease, clock=self._clock(3))
        other = leader.acquire_or_renew(self.conn, "member:dev", "B",
                                       ttl_seconds=30, clock=self._clock(4))
        with self.assertRaises(leader.StaleLeaderError):
            leader.require_lease(self.conn, other, clock=self._clock(35))

    def test_stale_epoch_from_previous_era_fenced_forever(self) -> None:
        first = leader.acquire_or_renew(self.conn, "member:x", "A",
                                       ttl_seconds=5, clock=self._clock())
        leader.release(self.conn, "member:x", "A", epoch=first.epoch,
                      clock=self._clock())
        leader.acquire_or_renew(self.conn, "member:x", "B", ttl_seconds=5,
                               clock=self._clock())
        leader.release(self.conn, "member:x", "B", epoch=2,
                      clock=self._clock())
        with self.assertRaises(leader.StaleLeaderError):
            leader.require_lease(self.conn, first, clock=self._clock())

    # ------------------------------------------------- composability (CEO)

    def test_primitives_compose_inside_outer_transaction(self) -> None:
        with storage.transaction(self.conn):
            lease = leader.acquire_or_renew(self.conn, "member:z", "D",
                                           ttl_seconds=30, clock=self._clock())
            leader.require_lease(self.conn, lease, clock=self._clock())
            self.assertTrue(leader.release(self.conn, "member:z", "D",
                                          epoch=lease.epoch,
                                          clock=self._clock()))
        # The outer transaction committed as one unit; epoch history persists.
        row = self.conn.execute(
            "SELECT state, epoch FROM leader_lease WHERE scope = 'member:z'"
        ).fetchone()
        self.assertEqual(str(row[0]), "released")
        self.assertEqual(int(row[1]), 1)
        again = leader.acquire_or_renew(self.conn, "member:z", "D",
                                       ttl_seconds=30, clock=self._clock(2))
        self.assertEqual(again.epoch, 2)

    # ------------------------------------------------------------ hygiene

    def test_input_validation(self) -> None:
        for bad in (
            dict(scope="", holder_id="A", ttl_seconds=10),
            dict(scope="s", holder_id="", ttl_seconds=10),
            dict(scope="s", holder_id="A", ttl_seconds=0),
        ):
            with self.subTest(**{"k": str(bad)}):
                with self.assertRaises(ValidationError):
                    leader.acquire_or_renew(self.conn, bad["scope"],
                                           bad["holder_id"],
                                           ttl_seconds=bad["ttl_seconds"])


if __name__ == "__main__":
    unittest.main()
