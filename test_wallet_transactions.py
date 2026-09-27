"""
Focused tests — Wallet Transaction Connection Injection (MT-ADMIN-18)
======================================================================

Proves that ``wallet.reserve`` / ``wallet.release_units`` /
``wallet.settle_units`` can join a caller-owned SQLite transaction via
the keyword-only ``connection=`` parameter, with the exact ownership
conventions of ``db.get_connection()`` / ``db.transaction()`` /
``wallet.credit_units(connection=...)``:

  A. reserve(connection=conn)        inside a caller transaction
  B. release_units(connection=conn)  inside a caller transaction
  C. settle_units(connection=conn)   inside a caller transaction
  D. COMMIT  — caller commits → mutation persists
  E. ROLLBACK — caller rolls back → wallet state exactly restored
  F. NO INDEPENDENT COMMIT — mutation invisible to other connections
     before commit and not persisted after rollback
  G. SAME CONNECTION — the injected connection is used; no hidden
     connection is opened
  H. Existing callers — calls without ``connection=`` behave as before
  I. ERROR PATHS — insufficient available/held balance raises inside
     the caller transaction, leaves it usable, mutates nothing
  J. NO NESTED-TRANSACTION SIDE EFFECTS — no COMMIT/BEGIN issued, the
     caller-owned connection is never closed

Money stays integer USDT atomic units throughout (1 USDT =
100,000,000 units); floats are forbidden everywhere in this file.

Run:
    python -m pytest test_wallet_transactions.py -v
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

import db
import wallet
from wallet import (
    InsufficientBalanceError,
    InsufficientHeldBalanceError,
    ensure_wallet,
    release_units,
    reserve,
    settle_units,
    wallet_units,
)

# Reference amounts in integer wallet units (USDT_SCALE = 100_000_000)
TEN_USDT = 1_000_000_000
THREE_USDT = 300_000_000
ONE_USDT = 100_000_000
HALF_USDT = 50_000_000
QUARTER_USDT = 25_000_000


class WalletTxnTestBase(unittest.TestCase):
    """Temp-DB fixture following the project's existing test pattern."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)
        self.add_user(1)
        self.addCleanup(self._restore)

    def _restore(self):
        db.DB_PATH = self._original_db_path
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    # ── helpers ────────────────────────────────────────────────

    def add_user(self, user_id):
        self.assertTrue(db.register_user(user_id, f"u{user_id}", f"U{user_id}"))

    def fund(self, user_id, available_units, held_units=0):
        """Seed raw balances (test setup only)."""
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

    def snapshot(self, user_id):
        with db.get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT available_units, held_units FROM wallets "
                "WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return (row["available_units"], row["held_units"])

    @staticmethod
    def on_conn(conn, user_id=1):
        """Read balances on the caller's (possibly uncommitted) connection."""
        row = conn.execute(
            "SELECT available_units, held_units FROM wallets "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return (row["available_units"], row["held_units"])


# ── A–C: each mutation runs inside a caller transaction ───────────────


class TestMutationsInsideCallerTransaction(WalletTxnTestBase):

    def test_A_reserve_inside_caller_transaction(self):
        """A. reserve(connection=conn) joins the caller's transaction."""
        ensure_wallet(1)
        self.fund(1, TEN_USDT)
        with db.transaction() as conn:
            units = reserve(1, Decimal("3"), connection=conn)
            self.assertEqual(units, THREE_USDT)
            # visible IMMEDIATELY on the same (uncommitted) connection
            self.assertEqual(
                self.on_conn(conn), (700_000_000, THREE_USDT)
            )
        self.assertEqual(self.snapshot(1), (700_000_000, THREE_USDT))

    def test_B_release_inside_caller_transaction(self):
        """B. release_units(connection=conn) joins the caller's txn."""
        ensure_wallet(1)
        self.fund(1, 700_000_000, held_units=THREE_USDT)
        with db.transaction() as conn:
            units = release_units(1, THREE_USDT, connection=conn)
            self.assertEqual(units, THREE_USDT)
            self.assertEqual(
                self.on_conn(conn), (1_000_000_000, 0)
            )
        self.assertEqual(self.snapshot(1), (1_000_000_000, 0))

    def test_C_settle_inside_caller_transaction(self):
        """C. settle_units(connection=conn) joins the caller's txn."""
        ensure_wallet(1)
        self.fund(1, 700_000_000, held_units=THREE_USDT)
        with db.transaction() as conn:
            units = settle_units(1, THREE_USDT, connection=conn)
            self.assertEqual(units, THREE_USDT)
            # held leaves; available untouched (no second deduction)
            self.assertEqual(self.on_conn(conn), (700_000_000, 0))
        self.assertEqual(self.snapshot(1), (700_000_000, 0))


# ── D: COMMIT persists ────────────────────────────────────────────────


class TestCommitPersists(WalletTxnTestBase):

    def test_D_commit_persists_all_three_mutations(self):
        """D. caller commits → every injected mutation persists."""
        ensure_wallet(1)
        self.fund(1, TEN_USDT)

        with db.transaction() as conn:                       # reserve
            reserve(1, Decimal("3"), connection=conn)
        self.assertEqual(self.snapshot(1), (700_000_000, THREE_USDT))

        with db.transaction() as conn:                       # release
            release_units(1, THREE_USDT, connection=conn)
        self.assertEqual(self.snapshot(1), (TEN_USDT, 0))

        with db.transaction() as conn:                       # reserve
            reserve(1, Decimal("3"), connection=conn)
        self.assertEqual(self.snapshot(1), (700_000_000, THREE_USDT))

        with db.transaction() as conn:                       # settle
            settle_units(1, THREE_USDT, connection=conn)
        self.assertEqual(self.snapshot(1), (700_000_000, 0))


# ── E: ROLLBACK restores exactly ──────────────────────────────────────


class TestRollbackRestoresState(WalletTxnTestBase):

    def test_E_rollback_returns_exact_pre_transaction_state(self):
        """E. caller rolls back → wallet state returns EXACTLY to
        its pre-transaction values, for each of the three mutations."""
        cases = (
            ("reserve", lambda c: reserve(1, Decimal("3"), connection=c)),
            ("release", lambda c: release_units(1, THREE_USDT, connection=c)),
            ("settle", lambda c: settle_units(1, THREE_USDT, connection=c)),
        )
        for name, op in cases:
            with self.subTest(op=name):
                # available 10, held 3: every op below is valid
                self.fund(1, TEN_USDT, held_units=THREE_USDT)
                before = self.snapshot(1)
                with self.assertRaises(RuntimeError):
                    with db.transaction() as conn:
                        op(conn)
                        self.assertNotEqual(
                            self.on_conn(conn), before,
                            "mutation must be visible inside the txn",
                        )
                        raise RuntimeError("force caller rollback")
                self.assertEqual(self.snapshot(1), before)


# ── F: no independent commit ──────────────────────────────────────────


class TestNoIndependentCommit(WalletTxnTestBase):

    def test_F_mutation_invisible_before_commit_and_after_rollback(self):
        """F. the injected connection never commits on its own: other
        connections cannot see the mutation mid-transaction, and a
        rollback discards it entirely."""
        ensure_wallet(1)
        self.fund(1, TEN_USDT)
        before = self.snapshot(1)

        with self.assertRaises(RuntimeError):
            with db.transaction() as conn:
                reserve(1, Decimal("3"), connection=conn)
                # a separate reader sees the OLD state (WAL snapshot):
                with db.get_connection(self.db_path) as reader:
                    row = reader.execute(
                        "SELECT available_units, held_units "
                        "FROM wallets WHERE user_id = 1"
                    ).fetchone()
                    self.assertEqual(
                        (row["available_units"], row["held_units"]), before
                    )
                raise RuntimeError("force caller rollback")

        # nothing persisted:
        self.assertEqual(self.snapshot(1), before)


# ── G: same connection, no hidden connection ──────────────────────────


class TestSameConnectionUsed(WalletTxnTestBase):

    def test_G_injected_connection_used_no_hidden_connection(self):
        """G. while ``connection=`` is supplied, ``db.get_connection``
        is NEVER called — proving the exact injected connection is
        used and no hidden connection is opened."""
        ensure_wallet(1)
        self.fund(1, TEN_USDT)

        with db.transaction() as conn:
            with mock.patch(
                "db.get_connection",
                side_effect=AssertionError(
                    "hidden connection opened during injected mutation"
                ),
            ):
                reserve(1, Decimal("3"), connection=conn)
                release_units(1, THREE_USDT, connection=conn)
                reserve(1, Decimal("3"), connection=conn)
                settle_units(1, THREE_USDT, connection=conn)
            # every step landed on THIS connection (uncommitted view):
            # (10,0) → reserve 3 → (7,3) → release 3 → (10,0)
            #        → reserve 3 → (7,3) → settle 3 → (7,0)
            self.assertEqual(self.on_conn(conn), (700_000_000, 0))

        self.assertEqual(self.snapshot(1), (700_000_000, 0))
        # wallet row created on the caller's connection, same txn path:
        self.assertEqual(wallet_units(1).held_units, 0)


# ── H: existing callers (no connection=) ──────────────────────────────


class TestLegacyCallsUnchanged(WalletTxnTestBase):

    def test_H_calls_without_connection_behave_exactly_as_before(self):
        """H. the compatibility path (no ``connection=``) keeps the
        original semantics: each call self-commits, invariants hold."""
        ensure_wallet(1)
        self.fund(1, TEN_USDT)

        # reserve — positional args only, exactly like every old caller
        self.assertEqual(reserve(1, Decimal("3")), THREE_USDT)
        self.assertEqual(self.snapshot(1), (700_000_000, THREE_USDT))

        # release — exact inverse, restores pre-reserve state
        self.assertEqual(release_units(1, THREE_USDT), THREE_USDT)
        self.assertEqual(self.snapshot(1), (TEN_USDT, 0))

        # settle — held leaves once; available is NOT debited twice
        reserve(1, Decimal("3"))
        self.assertEqual(settle_units(1, THREE_USDT), THREE_USDT)
        self.assertEqual(self.snapshot(1), (700_000_000, 0))
        self.assertEqual(wallet_units(1).held_units, 0)


# ── I: error paths inside the caller transaction ──────────────────────


class TestErrorPathsInCallerTransaction(WalletTxnTestBase):

    def test_I_errors_leave_caller_transaction_usable_no_partial_mutation(self):
        """I. insufficient-balance failures raise, mutate NOTHING, and
        the caller's transaction remains usable afterwards."""
        cases = (
            # (name, seed, bad op, expected error, good op, final state)
            (
                "reserve_exceeds_available",
                (ONE_USDT, 0),
                lambda c: reserve(1, Decimal("50"), connection=c),
                InsufficientBalanceError,
                lambda c: reserve(1, Decimal("0.5"), connection=c),
                (HALF_USDT, HALF_USDT),
            ),
            (
                "release_exceeds_held",
                (ONE_USDT, HALF_USDT),
                lambda c: release_units(1, THREE_USDT, connection=c),
                InsufficientHeldBalanceError,
                lambda c: release_units(1, QUARTER_USDT, connection=c),
                (ONE_USDT + QUARTER_USDT, QUARTER_USDT),
            ),
            (
                "settle_exceeds_held",
                (ONE_USDT, HALF_USDT),
                lambda c: settle_units(1, THREE_USDT, connection=c),
                InsufficientHeldBalanceError,
                lambda c: settle_units(1, QUARTER_USDT, connection=c),
                (ONE_USDT, QUARTER_USDT),
            ),
        )
        for name, seed, bad_op, error, good_op, final in cases:
            with self.subTest(case=name):
                available, held = seed
                self.fund(1, available, held_units=held)
                before = self.snapshot(1)

                with db.transaction() as conn:
                    with self.assertRaises(error):
                        bad_op(conn)
                    # no partial mutation on the caller's connection:
                    self.assertEqual(self.on_conn(conn), before)
                    # the SAME transaction is still usable:
                    good_op(conn)
                    self.assertNotEqual(self.on_conn(conn), before)

                # only the good operation was committed:
                self.assertEqual(self.snapshot(1), final)


# ── J: no COMMIT / BEGIN / close on the caller-owned connection ───────


class TestNoNestedTransactionSideEffects(WalletTxnTestBase):

    def test_J_no_commit_begin_or_close_on_caller_connection(self):
        """J. injected mutations issue no COMMIT, no BEGIN and never
        close the caller-owned connection — the caller keeps full
        transaction ownership."""
        ensure_wallet(1)
        self.fund(1, TEN_USDT)

        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            self.assertTrue(conn.in_transaction)

            reserve(1, Decimal("3"), connection=conn)
            release_units(1, THREE_USDT, connection=conn)
            reserve(1, Decimal("3"), connection=conn)
            settle_units(1, THREE_USDT, connection=conn)

            # a method-issued COMMIT would have ended the transaction:
            self.assertTrue(conn.in_transaction)
            # a method-issued BEGIN would have raised sqlite3's
            # "cannot start a transaction within a transaction" —
            # reaching this line proves none was issued.
            # connection still open and usable (never closed):
            # (10,0) → reserve3 (7,3) → release3 (10,0)
            #        → reserve3 (7,3) → settle3 (7,0)
            self.assertEqual(
                self.on_conn(conn), (700_000_000, 0)
            )

            # ownership still with the caller: OUR rollback wins and
            # discards every injected mutation.
            conn.execute("ROLLBACK")
            self.assertFalse(conn.in_transaction)
            self.assertEqual(self.snapshot(1), (TEN_USDT, 0))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
