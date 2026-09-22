"""
Tests for the Ledger Persistence Service (``ledger.py``, Micro-task MT-3).

Coverage map (mandatory cases):

- record semantics (credit/debit/hold/release/settlement) -> TestRecording
- validation (amount/bool/float/types/users/ids)          -> TestValidation
- idempotent replay and conflicts                        -> TestIdempotency
- reference uniqueness                                   -> TestReferenceConflict
- append-only public API                                 -> TestAppendOnly
- read APIs and ordering                                 -> TestReads
- field persistence (actor/rate/metadata)                -> TestPersistence
- wallet isolation (no balance mutation)                 -> TestWalletIsolation
- connection ownership (standalone vs injected)          -> TestConnectionOwnership
- no float/REAL accounting, no wallet import             -> TestPolicy

Run:
    python -m pytest test_ledger.py -v
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import os
import re
import sqlite3
import tempfile
import unittest

import db
import ledger
from ledger import (
    ENTRY_TYPES,
    REFERENCE_TYPES,
    InvalidLedgerEntryError,
    LedgerEntry,
    LedgerEntryNotFoundError,
    LedgerIdempotencyConflictError,
    LedgerReferenceConflictError,
    LedgerService,
)

# 10 USDT in integer units (1 USDT = 100,000,000)
TEN_USDT = 1_000_000_000


class LedgerTestBase(unittest.TestCase):
    """Temp-DB fixture following the project's existing test pattern."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)
        self.add_user(1)
        self.add_user(2)
        self.svc = LedgerService()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    # ── helpers ────────────────────────────────────────────────

    def add_user(self, user_id):
        self.assertTrue(db.register_user(user_id, f"u{user_id}", f"U{user_id}"))

    def ledger_count(self):
        with db.get_connection(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM ledger").fetchone()["c"]

    def raw_entry(self, entry_id):
        with db.get_connection(self.db_path) as conn:
            return conn.execute(
                "SELECT * FROM ledger WHERE id = ?", (entry_id,)
            ).fetchone()

    def fund_wallet(self, user_id, available_units, held_units=0):
        """Test-only setup: the wallet has no credit primitive (MT-3 must
        not add one)."""
        with db.get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO wallets
                       (user_id, available_units, held_units)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       available_units = excluded.available_units,
                       held_units = excluded.held_units""",
                (user_id, available_units, held_units),
            )

    def wallet_row(self, user_id):
        with db.get_connection(self.db_path) as conn:
            return conn.execute(
                "SELECT available_units, held_units FROM wallets "
                "WHERE user_id = ?",
                (user_id,),
            ).fetchone()


# ── 1–5 (+ extra): recording semantics ───────────────────────────────


class TestRecording(LedgerTestBase):

    def test_credit_creates_exactly_one_row(self):
        """1. record_credit inserts exactly one immutable row."""
        entry = self.svc.record_credit(
            1, amount_units=1_250_000_000,
            reference_type="task", reference_id="1:7",
            idempotency_key="task_reward:1:7",
        )
        self.assertEqual(self.ledger_count(), 1)
        self.assertIsInstance(entry, LedgerEntry)
        self.assertEqual(entry.entry_type, "credit")
        self.assertEqual(entry.amount_units, 1_250_000_000)
        self.assertEqual(entry.available_delta, 1_250_000_000)
        self.assertEqual(entry.held_delta, 0)
        self.assertEqual(entry.currency, "USDT")
        self.assertEqual(entry.user_id, 1)
        self.assertIsInstance(entry.id, int)
        self.assertEqual(self.raw_entry(entry.id)["amount_units"],
                         1_250_000_000)

    def test_debit_creates_exactly_one_row(self):
        """2. record_debit inserts exactly one row with debit deltas."""
        entry = self.svc.record_debit(
            1, amount_units=500_000_000,
            reference_type="adjustment", reference_id="a1",
        )
        self.assertEqual(self.ledger_count(), 1)
        self.assertEqual(entry.entry_type, "debit")
        self.assertEqual(entry.available_delta, -500_000_000)
        self.assertEqual(entry.held_delta, 0)

    def test_hold_has_correct_deltas(self):
        """3. hold: available_delta negative, held_delta positive,
        magnitudes equal to amount_units."""
        entry = self.svc.record_hold(
            1, amount_units=300_000_000,
            reference_type="withdrawal", reference_id="w1",
        )
        self.assertEqual(self.ledger_count(), 1)
        self.assertEqual(entry.entry_type, "hold")
        self.assertEqual(entry.available_delta, -300_000_000)
        self.assertEqual(entry.held_delta, 300_000_000)
        self.assertEqual(abs(entry.available_delta), entry.amount_units)
        self.assertEqual(abs(entry.held_delta), entry.amount_units)

    def test_release_has_correct_deltas(self):
        """4. release: available positive, held negative, magnitudes
        equal to amount_units."""
        entry = self.svc.record_release(
            1, amount_units=300_000_000,
            reference_type="withdrawal", reference_id="w1",
        )
        self.assertEqual(self.ledger_count(), 1)
        self.assertEqual(entry.entry_type, "release")
        self.assertEqual(entry.available_delta, 300_000_000)
        self.assertEqual(entry.held_delta, -300_000_000)
        self.assertEqual(abs(entry.available_delta), entry.amount_units)
        self.assertEqual(abs(entry.held_delta), entry.amount_units)

    def test_settlement_has_correct_deltas(self):
        """5. settlement: available unchanged (0), held negative."""
        entry = self.svc.record_settlement(
            1, amount_units=300_000_000,
            reference_type="withdrawal", reference_id="w1",
        )
        self.assertEqual(self.ledger_count(), 1)
        self.assertEqual(entry.entry_type, "settlement")
        self.assertEqual(entry.available_delta, 0)
        self.assertEqual(entry.held_delta, -300_000_000)
        self.assertEqual(abs(entry.held_delta), entry.amount_units)

    def test_every_entry_type_persists_usdt_currency(self):
        """Extra: currency is 'USDT' for all five entry types."""
        for index, entry_type in enumerate(sorted(ENTRY_TYPES)):
            method = getattr(self.svc, f"record_{entry_type}")
            entry = method(
                1, amount_units=100,
                reference_type="adjustment", reference_id=f"cur-{index}",
            )
            self.assertEqual(entry.currency, "USDT")
        self.assertEqual(self.ledger_count(), 5)


# ── 6–11 (+ extras): validation ──────────────────────────────────────


class TestValidation(LedgerTestBase):

    def test_amount_units_must_be_positive(self):
        """6. Zero/negative amounts are rejected by every recorder."""
        recorders = (
            self.svc.record_credit, self.svc.record_debit,
            self.svc.record_hold, self.svc.record_release,
            self.svc.record_settlement,
        )
        for index, (method, bad) in enumerate(
            [(m, amount) for m in recorders for amount in (0, -1, -100_000_000)]
        ):
            with self.subTest(method=method.__name__, amount=bad):
                with self.assertRaises(InvalidLedgerEntryError):
                    method(
                        1, amount_units=bad,
                        reference_type="adjustment",
                        reference_id=f"neg-{index}",
                    )
        self.assertEqual(self.ledger_count(), 0)

    def test_bool_rejected(self):
        """7. bool is rejected for money and user ids."""
        with self.assertRaises(InvalidLedgerEntryError):
            self.svc.record_credit(
                1, amount_units=True,
                reference_type="task", reference_id="b1",
            )
        with self.assertRaises(InvalidLedgerEntryError):
            self.svc.record_credit(
                True, amount_units=100,
                reference_type="task", reference_id="b2",
            )
        self.assertEqual(self.ledger_count(), 0)

    def test_float_rejected(self):
        """8. float is rejected for money."""
        for bad in (1.5, 100.0, -2.0):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidLedgerEntryError):
                    self.svc.record_credit(
                        1, amount_units=bad,
                        reference_type="task", reference_id="f1",
                    )
        self.assertEqual(self.ledger_count(), 0)

    def test_invalid_entry_type_rejected(self):
        """9. entry_type outside the five approved values is rejected."""
        for bad in ("transfer", "", None, 1, True, "Credit"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidLedgerEntryError):
                    self.svc._record(
                        bad,
                        user_id=1, amount_units=100,
                        reference_type="task", reference_id="e1",
                        idempotency_key=None, actor_user_id=None,
                        rate_usdt_egp=None, metadata=None,
                    )
        self.assertEqual(self.ledger_count(), 0)

    def test_invalid_reference_type_rejected(self):
        """10. reference_type CHECK values only; all approved ones work."""
        for bad in ("prize", "", None, 7, "Task"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidLedgerEntryError):
                    self.svc.record_credit(
                        1, amount_units=100,
                        reference_type=bad, reference_id="r-bad",
                    )
        # every approved reference type is accepted
        for index, reference_type in enumerate(sorted(REFERENCE_TYPES)):
            entry = self.svc.record_credit(
                1, amount_units=100,
                reference_type=reference_type, reference_id=f"ok-{index}",
            )
            self.assertEqual(entry.reference_type, reference_type)
        self.assertEqual(self.ledger_count(), len(REFERENCE_TYPES))

    def test_missing_or_invalid_user_rejected(self):
        """11. Unknown users and malformed user ids are rejected."""
        # valid id format, user does not exist
        with self.assertRaises(InvalidLedgerEntryError):
            self.svc.record_credit(
                999_999, amount_units=100,
                reference_type="task", reference_id="u-missing",
            )
        # malformed ids
        for bad in (0, -1, True, "1", None, 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidLedgerEntryError):
                    self.svc.record_credit(
                        bad, amount_units=100,
                        reference_type="task", reference_id="u-bad",
                    )
        self.assertEqual(self.ledger_count(), 0)

    def test_malformed_reference_id_rejected(self):
        """Extra: empty/whitespace/non-str reference ids are rejected."""
        for bad in ("", " ", " w1", "w1 ", None, 123, True):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidLedgerEntryError):
                    self.svc.record_credit(
                        1, amount_units=100,
                        reference_type="task", reference_id=bad,
                    )
        self.assertEqual(self.ledger_count(), 0)

    def test_invalid_idempotency_key_rejected(self):
        """Extra: keys must be non-empty, clean strings or None."""
        for bad in ("", " k", "k ", 5, True):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidLedgerEntryError):
                    self.svc.record_credit(
                        1, amount_units=100,
                        reference_type="task", reference_id="k-bad",
                        idempotency_key=bad,
                    )
        # None is fine
        self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="k-ok",
            idempotency_key=None,
        )
        self.assertEqual(self.ledger_count(), 1)

    def test_invalid_rate_and_actor_rejected(self):
        """Extra: rate must be clean canonical text; actor must be an
        existing user."""
        for bad_rate in ("", " 48.5", 48.5, True, 50):
            with self.subTest(rate=bad_rate):
                with self.assertRaises(InvalidLedgerEntryError):
                    self.svc.record_credit(
                        1, amount_units=100,
                        reference_type="task", reference_id="rate-bad",
                        rate_usdt_egp=bad_rate,
                    )
        # actor format invalid
        with self.assertRaises(InvalidLedgerEntryError):
            self.svc.record_credit(
                1, amount_units=100,
                reference_type="task", reference_id="actor-bad",
                actor_user_id=True,
            )
        # actor format valid but user missing (FK / pre-check)
        with self.assertRaises(InvalidLedgerEntryError):
            self.svc.record_credit(
                1, amount_units=100,
                reference_type="task", reference_id="actor-missing",
                actor_user_id=999_999,
            )
        self.assertEqual(self.ledger_count(), 0)

    def test_invalid_metadata_rejected(self):
        """Extra: non-serializable metadata and broken JSON strings are
        rejected without writing a row."""
        with self.assertRaises(InvalidLedgerEntryError):
            self.svc.record_credit(
                1, amount_units=100,
                reference_type="task", reference_id="m-broken",
                metadata={"bad": {1, 2}},          # set is not JSON
            )
        with self.assertRaises(InvalidLedgerEntryError):
            self.svc.record_credit(
                1, amount_units=100,
                reference_type="task", reference_id="m-badjson",
                metadata='{"broken":',
            )
        self.assertEqual(self.ledger_count(), 0)


# ── 12–14 (+ extra): idempotency ─────────────────────────────────────


class TestIdempotency(LedgerTestBase):

    def test_idempotent_retry_returns_original_entry(self):
        """12. Replaying the same idempotency key returns the original."""
        first = self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="1:7",
            idempotency_key="task_reward:1:7",
        )
        second = self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="1:7",
            idempotency_key="task_reward:1:7",
        )
        self.assertEqual(first, second)
        self.assertEqual(first.id, second.id)
        self.assertIsInstance(second, LedgerEntry)

    def test_idempotent_retry_creates_no_duplicate_row(self):
        """13. Repeated retries never duplicate accounting rows."""
        for _ in range(4):
            self.svc.record_credit(
                1, amount_units=100,
                reference_type="deposit", reference_id="tx-1",
                idempotency_key="deposit:provider:tx-1",
            )
        self.assertEqual(self.ledger_count(), 1)

    def test_same_key_different_data_raises_conflict(self):
        """14. Reusing a key with materially different data conflicts."""
        self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="1:7",
            idempotency_key="reward:1:7",
        )
        # different accounting amount
        with self.assertRaises(LedgerIdempotencyConflictError):
            self.svc.record_credit(
                1, amount_units=999,
                reference_type="task", reference_id="1:7",
                idempotency_key="reward:1:7",
            )
        # different reference
        with self.assertRaises(LedgerIdempotencyConflictError):
            self.svc.record_credit(
                1, amount_units=100,
                reference_type="task", reference_id="1:8",
                idempotency_key="reward:1:7",
            )
        # different entry type / direction
        with self.assertRaises(LedgerIdempotencyConflictError):
            self.svc.record_debit(
                1, amount_units=100,
                reference_type="task", reference_id="1:7",
                idempotency_key="reward:1:7",
            )
        # different user
        with self.assertRaises(LedgerIdempotencyConflictError):
            self.svc.record_credit(
                2, amount_units=100,
                reference_type="task", reference_id="1:7",
                idempotency_key="reward:1:7",
            )
        self.assertEqual(self.ledger_count(), 1)

    def test_dict_metadata_is_canonical_across_replays(self):
        """Extra: deterministic serialization makes dict metadata
        replay-safe regardless of insertion order."""
        first = self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="1:9",
            idempotency_key="reward:1:9",
            metadata={"reason": "task", "task_id": 9},
        )
        second = self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="1:9",
            idempotency_key="reward:1:9",
            metadata={"task_id": 9, "reason": "task"},   # other order
        )
        self.assertEqual(first, second)
        self.assertEqual(self.ledger_count(), 1)


# ── 15: reference uniqueness ─────────────────────────────────────────


class TestReferenceConflict(LedgerTestBase):

    def test_duplicate_reference_and_entry_type_is_rejected(self):
        """15. UNIQUE(reference_type, reference_id, entry_type) holds and
        surfaces as a domain exception."""
        self.svc.record_hold(
            1, amount_units=300_000_000,
            reference_type="withdrawal", reference_id="w1",
        )
        # same reference + same entry type, *different* idempotency keys
        with self.assertRaises(LedgerReferenceConflictError):
            self.svc.record_hold(
                1, amount_units=300_000_000,
                reference_type="withdrawal", reference_id="w1",
                idempotency_key="some-other-key",
            )
        # same reference without keys at all
        with self.assertRaises(LedgerReferenceConflictError):
            self.svc.record_hold(
                1, amount_units=300_000_000,
                reference_type="withdrawal", reference_id="w1",
            )
        # a *different* entry type on the same reference is the normal
        # lifecycle and stays allowed
        self.svc.record_release(
            1, amount_units=300_000_000,
            reference_type="withdrawal", reference_id="w1",
        )
        self.assertEqual(self.ledger_count(), 2)


# ── 16 (+ extras): append-only ───────────────────────────────────────


class TestAppendOnly(LedgerTestBase):

    def test_public_api_exposes_no_mutation_methods(self):
        """16. The service offers only the five record APIs + three read
        APIs — there is no way to update or remove an entry."""
        public = {
            name for name in dir(LedgerService) if not name.startswith("_")
        }
        self.assertEqual(
            public,
            {
                "record_credit", "record_debit", "record_hold",
                "record_release", "record_settlement",
                "get_entry", "get_by_idempotency_key", "list_user_entries",
            },
        )

    def test_source_has_no_ledger_mutation_sql(self):
        """Extra: ledger.py contains insert/select statements only."""
        source = inspect.getsource(ledger)
        self.assertIsNone(
            re.search(r"\bUPDATE\s+ledger\b", source, re.IGNORECASE)
        )
        self.assertIsNone(
            re.search(r"\bDELETE\s+FROM\s+ledger\b", source, re.IGNORECASE)
        )
        self.assertIn("INSERT INTO ledger", source)

    def test_entry_is_frozen(self):
        """Extra: LedgerEntry rows are immutable value objects."""
        entry = self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="frz",
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            entry.amount_units = 5                    # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            entry.entry_type = "debit"                # type: ignore[misc]


# ── 17–20: read APIs ─────────────────────────────────────────────────


class TestReads(LedgerTestBase):

    def test_get_entry_works(self):
        """17. get_entry returns the recorded row; misses raise."""
        created = self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="g1",
        )
        fetched = self.svc.get_entry(created.id)
        self.assertEqual(fetched, created)
        with self.assertRaises(LedgerEntryNotFoundError):
            self.svc.get_entry(999_999)
        for bad in (0, -1, True, "1"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidLedgerEntryError):
                    self.svc.get_entry(bad)

    def test_get_by_idempotency_key_works(self):
        """18. get_by_idempotency_key returns the recorded row; misses
        raise."""
        created = self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="k1",
            idempotency_key="lookup:key",
        )
        fetched = self.svc.get_by_idempotency_key("lookup:key")
        self.assertEqual(fetched, created)
        with self.assertRaises(LedgerEntryNotFoundError):
            self.svc.get_by_idempotency_key("never:used")
        with self.assertRaises(InvalidLedgerEntryError):
            self.svc.get_by_idempotency_key("")
        with self.assertRaises(InvalidLedgerEntryError):
            self.svc.get_by_idempotency_key(None)     # type: ignore[arg-type]

    def test_list_user_entries_is_deterministic_newest_first(self):
        """19. Newest-first by monotonic id; repeated calls agree."""
        for index in range(3):
            self.svc.record_credit(
                1, amount_units=100 + index,
                reference_type="task", reference_id=f"seq-{index}",
            )
        first = self.svc.list_user_entries(1)
        second = self.svc.list_user_entries(1)
        ids = [e.id for e in first]
        self.assertEqual(ids, [3, 2, 1])             # newest first
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual([e.amount_units for e in first], [102, 101, 100])
        self.assertEqual(first, second)               # deterministic
        # ordering is decided by the monotonic id, so even identical
        # created_at timestamps cannot shuffle the result
        self.assertEqual([e.id for e in second], [3, 2, 1])

    def test_other_users_entries_are_not_returned(self):
        """20. Listing is strictly scoped to the requested user."""
        self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="u1-a",
        )
        self.svc.record_credit(
            1, amount_units=200,
            reference_type="task", reference_id="u1-b",
        )
        self.svc.record_credit(
            2, amount_units=300,
            reference_type="task", reference_id="u2-a",
        )
        user1 = self.svc.list_user_entries(1)
        user2 = self.svc.list_user_entries(2)
        self.assertEqual(len(user1), 2)
        self.assertTrue(all(e.user_id == 1 for e in user1))
        self.assertEqual(len(user2), 1)
        self.assertTrue(all(e.user_id == 2 for e in user2))
        self.assertEqual(user2[0].amount_units, 300)

    def test_list_user_entries_validates_user_id(self):
        """Extra: malformed user ids are rejected by the read API."""
        for bad in (0, -1, True, "1", None):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidLedgerEntryError):
                    self.svc.list_user_entries(bad)


# ── 21–24: field persistence ─────────────────────────────────────────


class TestPersistence(LedgerTestBase):

    def test_actor_user_id_is_persisted(self):
        """21. actor_user_id round-trips (and stays None when absent)."""
        with_actor = self.svc.record_credit(
            1, amount_units=100,
            reference_type="admin_credit", reference_id="ac-1",
            actor_user_id=2,
        )
        without = self.svc.record_credit(
            1, amount_units=100,
            reference_type="admin_credit", reference_id="ac-2",
        )
        self.assertEqual(with_actor.actor_user_id, 2)
        self.assertIsNone(without.actor_user_id)
        self.assertEqual(
            self.raw_entry(with_actor.id)["actor_user_id"], 2
        )
        self.assertEqual(
            self.svc.get_entry(with_actor.id).actor_user_id, 2
        )

    def test_rate_is_persisted_exactly_as_supplied(self):
        """22. The rate string survives byte-for-byte (no recalculation,
        no float parsing)."""
        rate = "48.500000000000000001"
        entry = self.svc.record_hold(
            1, amount_units=100,
            reference_type="withdrawal", reference_id="w-rate",
            rate_usdt_egp=rate,
        )
        self.assertEqual(entry.rate_usdt_egp, rate)
        self.assertEqual(self.raw_entry(entry.id)["rate_usdt_egp"], rate)
        self.assertEqual(self.svc.get_entry(entry.id).rate_usdt_egp, rate)
        # None round-trips as None
        no_rate = self.svc.record_release(
            1, amount_units=100,
            reference_type="withdrawal", reference_id="w-norate",
        )
        self.assertIsNone(no_rate.rate_usdt_egp)

    def test_metadata_round_trips_correctly(self):
        """23. dict metadata is serialized deterministically and parses
        back to the original structure."""
        original = {"reason": "task_reward", "task_id": 7, "ok": True}
        entry = self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="1:7",
            metadata=original,
        )
        self.assertIsInstance(entry.metadata, str)
        self.assertEqual(json.loads(entry.metadata), original)
        canonical = json.dumps(
            original, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        )
        self.assertEqual(entry.metadata, canonical)
        self.assertEqual(self.svc.get_entry(entry.id).metadata, canonical)
        # complex structures survive too
        complex_meta = {"items": [{"a": 1}, {"b": 2}], "note": "مهم"}
        deep = self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="1:8",
            metadata=complex_meta,
        )
        self.assertEqual(json.loads(deep.metadata), complex_meta)

    def test_metadata_cannot_be_silently_corrupted(self):
        """24. str metadata is stored byte-for-byte verbatim; invalid
        input is rejected without touching the table."""
        raw = ' { "b" : 2,  "a" : [1,   2] } '
        entry = self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="verbatim",
            metadata=raw,
        )
        self.assertEqual(entry.metadata, raw)
        self.assertEqual(self.raw_entry(entry.id)["metadata"], raw)
        # rejected input writes nothing
        with self.assertRaises(InvalidLedgerEntryError):
            self.svc.record_credit(
                1, amount_units=100,
                reference_type="task", reference_id="corrupt",
                metadata=b"\x00\xff not json or dict",
            )
        self.assertEqual(self.ledger_count(), 1)
        # None stays None (never "null" or "{}")
        none_meta = self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="none-meta",
        )
        self.assertIsNone(none_meta.metadata)

    def test_created_at_uses_database_convention(self):
        """Extra: timestamps come from the schema's CURRENT_TIMESTAMP."""
        entry = self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="ts",
        )
        self.assertIsInstance(entry.created_at, str)
        self.assertRegex(
            entry.created_at, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
        )


# ── 25: wallet isolation ─────────────────────────────────────────────


class TestWalletIsolation(LedgerTestBase):

    def test_no_wallet_balance_is_changed_by_ledger_operations(self):
        """25. All five record operations leave wallet balances (and the
        wallets table itself) completely untouched."""
        self.fund_wallet(1, TEN_USDT, held_units=0)
        before = (
            self.wallet_row(1)["available_units"],
            self.wallet_row(1)["held_units"],
        )
        self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="iso-credit",
        )
        self.svc.record_debit(
            1, amount_units=50,
            reference_type="adjustment", reference_id="iso-debit",
        )
        self.svc.record_hold(
            1, amount_units=300_000_000,
            reference_type="withdrawal", reference_id="iso-w",
        )
        self.svc.record_release(
            1, amount_units=300_000_000,
            reference_type="withdrawal", reference_id="iso-w",
        )
        self.svc.record_settlement(
            1, amount_units=300_000_000,
            reference_type="withdrawal", reference_id="iso-w",
        )
        after = (
            self.wallet_row(1)["available_units"],
            self.wallet_row(1)["held_units"],
        )
        self.assertEqual(after, before)
        self.assertEqual(self.ledger_count(), 5)
        # user 2 has no wallet row at all — recording must not create one
        self.svc.record_credit(
            2, amount_units=100,
            reference_type="task", reference_id="iso-u2",
        )
        with db.get_connection(self.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM wallets WHERE user_id = 2"
            ).fetchone()
        self.assertEqual(count["c"], 0)
        self.assertEqual(self.ledger_count(), 6)


# ── 26–27: connection / transaction ownership ────────────────────────


class TestConnectionOwnership(LedgerTestBase):

    def test_standalone_operations_commit(self):
        """26. Without an injected connection each operation commits
        through db.get_connection() and is visible elsewhere."""
        entry = self.svc.record_credit(
            1, amount_units=100,
            reference_type="task", reference_id="standalone",
        )
        with db.get_connection(self.db_path) as other:
            row = other.execute(
                "SELECT COUNT(*) AS c FROM ledger WHERE id = ?",
                (entry.id,),
            ).fetchone()
        self.assertEqual(row["c"], 1)
        # reads work on an independent service instance too
        other_svc = LedgerService()
        self.assertEqual(other_svc.get_entry(entry.id), entry)

    def test_injected_connection_is_never_committed_or_closed(self):
        """27. With an injected connection the service executes statements
        but leaves transaction ownership (commit/close) to the caller."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            svc = LedgerService(connection=conn)
            entry = svc.record_credit(
                1, amount_units=100,
                reference_type="task", reference_id="injected",
                idempotency_key="injected:1",
            )
            # the service did NOT commit...
            self.assertTrue(conn.in_transaction)
            # ...nor close the connection
            conn.execute("SELECT 1").fetchone()
            # reads on the same connection see the uncommitted row
            self.assertEqual(
                svc.get_by_idempotency_key("injected:1").id, entry.id
            )
            # other connections see nothing yet
            with db.get_connection(self.db_path) as other:
                count = other.execute(
                    "SELECT COUNT(*) AS c FROM ledger"
                ).fetchone()["c"]
            self.assertEqual(count, 0)
            # the caller commits on its own schedule
            conn.commit()
            with db.get_connection(self.db_path) as other:
                count = other.execute(
                    "SELECT COUNT(*) AS c FROM ledger"
                ).fetchone()["c"]
            self.assertEqual(count, 1)
        finally:
            conn.close()


# ── 28 (+ extras): policy — no float, no wallet coupling ─────────────


class TestPolicy(LedgerTestBase):

    def test_no_float_or_real_accounting_is_introduced(self):
        """28. AST scan: no float literals, float() calls or .float
        usage in ledger.py; stored amounts come back as exact ints;
        no Decimal arithmetic for integer units."""
        tree = ast.parse(inspect.getsource(ledger))
        guarded: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"
            ):
                for arg in node.args[1:]:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Name):
                            guarded.add(sub.id)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                self.fail(f"float literal at line {node.lineno}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "float"
            ):
                self.fail(f"float() conversion at line {node.lineno}")
            if (
                isinstance(node, ast.Name)
                and node.id == "float"
                and node.id not in guarded
            ):
                self.fail(f"float reference at line {node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr == "float":
                self.fail(f"float attribute at line {node.lineno}")
        # integer-unit discipline: no Decimal maths, no wallet coupling
        source = inspect.getsource(ledger)
        self.assertNotIn("from decimal", source)
        self.assertNotIn("import wallet", source)
        self.assertNotIn("from wallet", source)
        # values read back are exact ints (SQLite INTEGER, not REAL)
        entry = self.svc.record_credit(
            1, amount_units=123_456_789,
            reference_type="task", reference_id="int-check",
        )
        for value in (
            entry.amount_units, entry.available_delta, entry.held_delta
        ):
            self.assertIsInstance(value, int)
            self.assertNotIsInstance(value, float)

    def test_ledger_module_exposes_domain_constants(self):
        """Extra: entry/reference vocabularies match the schema CHECKs."""
        self.assertEqual(
            ENTRY_TYPES,
            {"credit", "debit", "hold", "release", "settlement"},
        )
        self.assertEqual(
            REFERENCE_TYPES,
            {"withdrawal", "task", "referral", "deposit",
             "admin_credit", "adjustment"},
        )
        self.assertEqual(ledger.CURRENCY, "USDT")


if __name__ == "__main__":
    unittest.main()
