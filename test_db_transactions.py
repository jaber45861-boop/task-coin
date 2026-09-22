"""
Tests for the SQLite atomic transaction infrastructure (MT-4).

The ``db.transaction()`` primitive is generic infrastructure: it knows
nothing about wallets, ledgers, withdrawals, tasks or Telegram.  These
tests prove the boundary itself — BEGIN IMMEDIATE, commit/rollback
semantics, connection ownership, busy timeout, isolation, nested-entry
rejection and concurrent-writer behaviour — without exercising any
business logic.

Coverage map (spec §13):

  1.  transaction helper exists
  2.  successful transaction commits
  3.  exception rolls back
  4.  original exception is re-raised
  5.  multiple writes rollback atomically
  6.  connection closes after successful transaction
  7.  connection closes after rollback
  8.  BEGIN IMMEDIATE is used
  9.  busy_timeout is configured to 5000ms
  10. foreign_keys remain enabled
  11. WAL behavior remains compatible with existing DB setup
  12. row_factory remains compatible
  13. nested transaction behavior is explicit
  14. transaction can expose/read data from its own uncommitted writes
  15. another connection cannot see uncommitted writes
  16. committed writes become visible after successful commit
  17. failed transaction leaves no partial data
  18. concurrent writers behave correctly under controlled contention
  19. no wallet rows are modified
  20. no ledger rows are modified
  21. existing db tests continue to pass (regression, run separately)

Run:
    python -m pytest test_db_transactions.py -v
    # or
    python -m unittest test_db_transactions.py -v
"""

import ast
import inspect
import os
import sqlite3
import tempfile
import threading
import unittest

import db


class _Boom(RuntimeError):
    """Test-only exception proving rollback + faithful re-raise."""


# Seeded money rows (spec §13.19 / §13.20)
_SEED_USER_ID = 4242
_SEED_WALLET_ROW = (_SEED_USER_ID, 5000, 0)  # available, held (int units)
_SEED_LEDGER_ROW = (
    _SEED_USER_ID, "credit", 1000, 1000, 0, "USDT",
    "admin_credit", "seed-credit",
)


class TransactionTestCase(unittest.TestCase):
    """Shared fixture: temp DB, full schema, scratch table, cleanup."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(self.test_db_path)
        # Scratch table for domain-free writes — the transaction helper
        # must work for any table without knowing anything about it.
        with db.get_connection(self.test_db_path) as conn:
            conn.execute(
                "CREATE TABLE tx_scratch ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "label TEXT NOT NULL UNIQUE)"
            )

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        if os.path.exists(self.test_db_path):
            os.unlink(self.test_db_path)
        for suffix in ("-wal", "-shm"):
            path = self.test_db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    # ── helpers ────────────────────────────────────────────────────

    def _count(self) -> int:
        with db.get_connection(self.test_db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM tx_scratch").fetchone()[0]

    def _rows(self) -> list[sqlite3.Row]:
        with db.get_connection(self.test_db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT id, label FROM tx_scratch ORDER BY id"
            ).fetchall()

    def _seed_money_rows(self) -> None:
        """Insert one wallet row + one ledger row (setup, not logic)."""
        with db.get_connection(self.test_db_path) as conn:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name) "
                "VALUES (?, 'seeduser', 'Seed')",
                (_SEED_USER_ID,),
            )
            conn.execute(
                "INSERT INTO wallets (user_id, available_units, held_units) "
                "VALUES (?, ?, ?)",
                _SEED_WALLET_ROW,
            )
            conn.execute(
                "INSERT INTO ledger (user_id, entry_type, amount_units, "
                "available_delta, held_delta, currency, reference_type, "
                "reference_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                _SEED_LEDGER_ROW,
            )

    def _wallet_rows(self) -> list[tuple]:
        with db.get_connection(self.test_db_path) as conn:
            conn.row_factory = None
            return conn.execute(
                "SELECT user_id, available_units, held_units, created_at, "
                "updated_at FROM wallets ORDER BY user_id"
            ).fetchall()

    def _ledger_rows(self) -> list[tuple]:
        with db.get_connection(self.test_db_path) as conn:
            conn.row_factory = None
            return conn.execute(
                "SELECT id, user_id, entry_type, amount_units, "
                "available_delta, held_delta, currency, reference_type, "
                "reference_id, idempotency_key, actor_user_id, "
                "rate_usdt_egp, metadata, created_at "
                "FROM ledger ORDER BY id"
            ).fetchall()


# ── 1–5, 14, 16, 17: basic commit/rollback semantics ────────────────


class TestTransactionBasics(TransactionTestCase):
    """Helper existence, commit, rollback, re-raise, atomicity."""

    def test_transaction_helper_exists(self):
        """(1) The generic transaction helper exists."""
        self.assertTrue(callable(db.transaction))
        self.assertTrue(issubclass(db.NestedTransactionError, RuntimeError))

    def test_successful_transaction_commits(self):
        """(2) A successful transaction commits its writes."""
        with db.transaction(self.test_db_path) as conn:
            conn.execute("INSERT INTO tx_scratch (label) VALUES ('committed')")
        self.assertEqual(self._count(), 1)
        self.assertEqual(self._rows()[0]["label"], "committed")

    def test_transaction_exposes_result_and_own_uncommitted_writes(self):
        """(8 §result / 14) The block reads its own uncommitted write and
        the assigned result survives the block."""
        with db.transaction(self.test_db_path) as conn:
            conn.execute("INSERT INTO tx_scratch (label) VALUES ('mine')")
            inner_count = conn.execute(
                "SELECT COUNT(*) FROM tx_scratch"
            ).fetchone()[0]
            result = "work done"
        self.assertEqual(inner_count, 1)  # visible to itself pre-commit
        self.assertEqual(result, "work done")
        self.assertEqual(self._count(), 1)  # committed afterwards

    def test_exception_rolls_back_and_reraises_original(self):
        """(3, 4) Rollback happens and the *original* exception instance
        is re-raised unchanged."""
        boom = _Boom("original boom")
        with self.assertRaises(_Boom) as ctx:
            with db.transaction(self.test_db_path) as conn:
                conn.execute("INSERT INTO tx_scratch (label) VALUES ('a')")
                raise boom
        self.assertIs(ctx.exception, boom)
        self.assertEqual(str(ctx.exception), "original boom")
        self.assertEqual(self._count(), 0)

    def test_multiple_writes_rollback_atomically(self):
        """(5) Several writes in one transaction all roll back together."""
        with self.assertRaises(_Boom):
            with db.transaction(self.test_db_path) as conn:
                conn.execute("INSERT INTO tx_scratch (label) VALUES ('a')")
                conn.execute("INSERT INTO tx_scratch (label) VALUES ('b')")
                # both are visible inside the transaction before the failure
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM tx_scratch").fetchone()[0],
                    2,
                )
                raise _Boom("fail after two writes")
        self.assertEqual(self._count(), 0)

    def test_failed_transaction_leaves_no_partial_data(self):
        """(17) A failing transaction preserves every pre-existing row."""
        with db.transaction(self.test_db_path) as conn:
            conn.execute("INSERT INTO tx_scratch (label) VALUES ('baseline')")
        with self.assertRaises(_Boom):
            with db.transaction(self.test_db_path) as conn:
                conn.execute("INSERT INTO tx_scratch (label) VALUES ('x1')")
                conn.execute("INSERT INTO tx_scratch (label) VALUES ('x2')")
                conn.execute("DELETE FROM tx_scratch WHERE label='baseline'")
                raise _Boom("partial state must vanish")
        labels = [row["label"] for row in self._rows()]
        self.assertEqual(labels, ["baseline"])

    def test_committed_writes_visible_after_commit(self):
        """(16) Committed writes are visible to a brand-new connection."""
        with db.transaction(self.test_db_path) as conn:
            conn.execute("INSERT INTO tx_scratch (label) VALUES ('durable')")
        raw = sqlite3.connect(self.test_db_path)
        try:
            seen = raw.execute("SELECT label FROM tx_scratch").fetchall()
        finally:
            raw.close()
        self.assertEqual(seen, [("durable",)])

    def test_default_db_path_used_when_omitted(self):
        """The helper defaults to db.DB_PATH like every other db helper."""
        with db.transaction() as conn:
            conn.execute("INSERT INTO tx_scratch (label) VALUES ('default')")
        self.assertEqual(self._count(), 1)


# ── 8–12: BEGIN IMMEDIATE + connection settings ─────────────────────


class TestTransactionConnectionSettings(TransactionTestCase):
    """BEGIN IMMEDIATE, busy timeout, foreign keys, WAL, row factory."""

    def test_helper_source_declares_begin_immediate(self):
        """(8) The helper explicitly begins an IMMEDIATE transaction."""
        self.assertIn("BEGIN IMMEDIATE", inspect.getsource(db.transaction))

    def test_begin_immediate_holds_write_lock_before_any_write(self):
        """(8) Behavioural proof: entering the transaction acquires the
        write lock before a single DML statement runs, so a second
        connection's BEGIN IMMEDIATE fails immediately (a deferred BEGIN
        would hold no lock yet and the probe would succeed)."""
        with db.transaction(self.test_db_path) as conn:
            self.assertTrue(conn.in_transaction)
            probe = sqlite3.connect(
                self.test_db_path, isolation_level=None, timeout=0
            )
            try:
                probe.execute("PRAGMA busy_timeout=0")
                with self.assertRaises(sqlite3.OperationalError) as pctx:
                    probe.execute("BEGIN IMMEDIATE")
                self.assertIn("locked", str(pctx.exception).lower())
            finally:
                probe.close()

    def test_busy_timeout_configured_to_5000ms(self):
        """(9) Every connection — transaction and get_connection — waits
        up to 5000 ms instead of failing instantly with SQLITE_BUSY."""
        self.assertEqual(db.BUSY_TIMEOUT_MS, 5000)
        with db.transaction(self.test_db_path) as conn:
            value = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(value, 5000)
        with db.get_connection(self.test_db_path) as conn:
            value = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(value, 5000)

    def test_foreign_keys_remain_enabled(self):
        """(10) foreign_keys=ON inside the transaction connection."""
        with db.transaction(self.test_db_path) as conn:
            self.assertEqual(
                conn.execute("PRAGMA foreign_keys").fetchone()[0], 1
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO wallets (user_id, available_units, held_units) "
                    "VALUES (999999999, 1, 0)"  # user does not exist
                )

    def test_wal_behavior_remains_compatible(self):
        """(11) WAL stays active, identical to get_connection()."""
        with db.transaction(self.test_db_path) as conn:
            self.assertEqual(
                conn.execute("PRAGMA journal_mode").fetchone()[0],
                "wal",
            )
        with db.get_connection(self.test_db_path) as conn:
            self.assertEqual(
                conn.execute("PRAGMA journal_mode").fetchone()[0],
                "wal",
            )

    def test_row_factory_remains_compatible(self):
        """(12) sqlite3.Row row factory, identical to get_connection()."""
        with db.transaction(self.test_db_path) as conn:
            self.assertIs(conn.row_factory, sqlite3.Row)
            conn.execute("INSERT INTO tx_scratch (label) VALUES ('rowed')")
            row = conn.execute("SELECT label FROM tx_scratch").fetchone()
            self.assertIsInstance(row, sqlite3.Row)
            self.assertEqual(row["label"], "rowed")
        with db.get_connection(self.test_db_path) as conn:
            self.assertIs(conn.row_factory, sqlite3.Row)


# ── 6, 7: connection ownership ──────────────────────────────────────


class TestConnectionOwnership(TransactionTestCase):
    """The helper owns its connection and closes it exactly once."""

    def test_connection_closed_after_successful_transaction(self):
        """(6) The yielded connection is closed once the block commits."""
        with db.transaction(self.test_db_path) as conn:
            conn.execute("INSERT INTO tx_scratch (label) VALUES ('closed')")
            live = conn
        with self.assertRaises(sqlite3.ProgrammingError):
            live.execute("SELECT 1")

    def test_connection_closed_after_rollback(self):
        """(7) The yielded connection is closed after a rollback too."""
        with self.assertRaises(_Boom):
            with db.transaction(self.test_db_path) as conn:
                conn.execute("INSERT INTO tx_scratch (label) VALUES ('gone')")
                live = conn
                raise _Boom("rollback")
        with self.assertRaises(sqlite3.ProgrammingError):
            live.execute("SELECT 1")
        self.assertEqual(self._count(), 0)

    def test_caller_owned_connection_is_never_closed(self):
        """The helper closes only the connection it opened itself."""
        with db.get_connection(self.test_db_path) as caller_conn:
            with db.transaction(self.test_db_path) as tx_conn:
                self.assertIsNot(caller_conn, tx_conn)
                tx_conn.execute(
                    "INSERT INTO tx_scratch (label) VALUES ('separate')"
                )
            # helper finished: its own connection is closed …
            with self.assertRaises(sqlite3.ProgrammingError):
                tx_conn.execute("SELECT 1")
            # … while the caller's connection stays fully usable
            seen = caller_conn.execute(
                "SELECT COUNT(*) FROM tx_scratch"
            ).fetchone()[0]
            self.assertEqual(seen, 1)
        # caller scope owns its own commit/close; row must be durable
        self.assertEqual(self._count(), 1)


# ── 13: nested transaction behavior ─────────────────────────────────


class TestNestedTransactions(TransactionTestCase):
    """Nesting is rejected explicitly — never faked as a nested BEGIN."""

    def test_nested_transaction_rejected_with_dedicated_error(self):
        """(13) Same-thread nesting raises NestedTransactionError, and the
        outer transaction stays usable and commits normally."""
        with db.transaction(self.test_db_path) as outer:
            outer.execute("INSERT INTO tx_scratch (label) VALUES ('outer1')")
            with self.assertRaises(db.NestedTransactionError) as ctx:
                with db.transaction(self.test_db_path):
                    pass  # must never begin a second transaction
            self.assertIn("nested", str(ctx.exception).lower())
            # the rejected attempt must not have disturbed the outer one
            self.assertTrue(outer.in_transaction)
            outer.execute("INSERT INTO tx_scratch (label) VALUES ('outer2')")
        labels = [row["label"] for row in self._rows()]
        self.assertEqual(labels, ["outer1", "outer2"])


# ── 15: isolation of uncommitted writes ─────────────────────────────


class TestTransactionIsolation(TransactionTestCase):
    """Uncommitted writes are private until COMMIT."""

    def test_other_connection_cannot_see_uncommitted_writes(self):
        """(15) A separate connection never observes in-flight writes,
        and observes them once committed (16)."""
        with db.transaction(self.test_db_path) as conn:
            conn.execute("INSERT INTO tx_scratch (label) VALUES ('hidden')")
            with db.get_connection(self.test_db_path) as reader:
                seen = reader.execute(
                    "SELECT COUNT(*) FROM tx_scratch"
                ).fetchone()[0]
            self.assertEqual(seen, 0)  # uncommitted → invisible
        self.assertEqual(self._count(), 1)  # committed → visible


# ── 18: concurrency ─────────────────────────────────────────────────


class TestConcurrency(TransactionTestCase):
    """Controlled contention with threading.Event — no sleep races."""

    def _run_thread(self, target, *args):
        errors: list[BaseException] = []

        def wrapper():
            try:
                target(*args)
            except BaseException as exc:  # noqa: BLE001 — collected for assert
                errors.append(exc)

        thread = threading.Thread(target=wrapper)
        thread.start()
        return thread, errors

    def test_two_independent_connections_use_helper_safely(self):
        """(18a) Two helper-managed connections on two threads both
        succeed without interference or SQLITE_BUSY."""

        def writer(label: str) -> None:
            with db.transaction(self.test_db_path) as conn:
                conn.execute(
                    "INSERT INTO tx_scratch (label) VALUES (?)", (label,)
                )

        t1, errors1 = self._run_thread(writer, "thread-one")
        t2, errors2 = self._run_thread(writer, "thread-two")
        t1.join(timeout=10)
        t2.join(timeout=10)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        self.assertEqual(errors1, [])
        self.assertEqual(errors2, [])
        self.assertEqual(self._count(), 2)

    def test_writer_waits_for_lock_with_no_partial_state_visible(self):
        """(18b) While one transaction holds the write lock, a second
        writer waits (busy_timeout, BEGIN IMMEDIATE), sees no partial
        state, then commits cleanly once the lock is released — with no
        SQLITE_BUSY and no timing-sensitive assertions."""
        lock_held = threading.Event()   # holder has written, not committed
        release = threading.Event()     # main lets the holder commit
        waiter_started = threading.Event()
        waiter_finished = threading.Event()

        def holder() -> None:
            with db.transaction(self.test_db_path) as conn:
                conn.execute(
                    "INSERT INTO tx_scratch (label) VALUES ('holder')"
                )
                lock_held.set()
                if not release.wait(timeout=10):
                    raise AssertionError("release never signalled")

        def waiter() -> None:
            waiter_started.set()
            with db.transaction(self.test_db_path) as conn:
                conn.execute(
                    "INSERT INTO tx_scratch (label) VALUES ('waiter')"
                )
            waiter_finished.set()

        holder_thread, holder_errors = self._run_thread(holder)
        self.assertTrue(lock_held.wait(timeout=10))

        # Uncommitted write is invisible to an outside reader.
        with db.get_connection(self.test_db_path) as reader:
            seen = reader.execute(
                "SELECT COUNT(*) FROM tx_scratch"
            ).fetchone()[0]
        self.assertEqual(seen, 0)

        waiter_thread, waiter_errors = self._run_thread(waiter)
        self.assertTrue(waiter_started.wait(timeout=10))
        # The holder still owns the write lock, so the waiter cannot have
        # completed — this can only fail if locking is broken.
        self.assertFalse(waiter_finished.is_set())

        release.set()
        holder_thread.join(timeout=10)
        waiter_thread.join(timeout=10)
        self.assertFalse(holder_thread.is_alive())
        self.assertFalse(waiter_thread.is_alive())

        self.assertEqual(holder_errors, [])
        self.assertEqual(waiter_errors, [])
        self.assertTrue(waiter_finished.is_set())

        rows = self._rows()
        self.assertEqual(
            [row["label"] for row in rows], ["holder", "waiter"]
        )  # waiter strictly after holder's commit — no partial interleaving
        self.assertEqual(self._count(), 2)


# ── 19, 20, §11: no business logic ──────────────────────────────────


class TestScopeBoundary(TransactionTestCase):
    """The infrastructure never touches wallet/ledger rows or domains."""

    def setUp(self):
        super().setUp()
        self._seed_money_rows()

    def test_no_wallet_rows_are_modified(self):
        """(19) Wallet rows are byte-identical across a transaction."""
        before = self._wallet_rows()
        self.assertEqual(len(before), 1)
        with db.transaction(self.test_db_path) as conn:
            conn.execute("INSERT INTO tx_scratch (label) VALUES ('w1')")
            conn.execute("INSERT INTO tx_scratch (label) VALUES ('w2')")
        self.assertEqual(self._wallet_rows(), before)
        self.assertEqual(before[0][1], _SEED_WALLET_ROW[1])

    def test_no_ledger_rows_are_modified(self):
        """(20) Ledger rows are byte-identical across a transaction."""
        before = self._ledger_rows()
        self.assertEqual(len(before), 1)
        with db.transaction(self.test_db_path) as conn:
            conn.execute("INSERT INTO tx_scratch (label) VALUES ('l1')")
            conn.execute("DELETE FROM tx_scratch WHERE label='missing'")
        self.assertEqual(self._ledger_rows(), before)
        self.assertEqual(before[0][3], 1000)  # amount_units untouched

    def test_transaction_helper_is_domain_agnostic(self):
        """(§11) The helper's code (docstring excluded) references no
        wallet / ledger / withdrawal / task / Telegram domain."""
        tree = ast.parse(inspect.getsource(db.transaction))
        function = tree.body[0]
        self.assertIsInstance(function, ast.FunctionDef)
        # strip the docstring so only executable code is scanned
        if (
            function.body
            and isinstance(function.body[0], ast.Expr)
            and isinstance(function.body[0].value, ast.Constant)
            and isinstance(function.body[0].value.value, str)
        ):
            function.body = function.body[1:]
        code = ast.unparse(function).lower()
        for word in (
            "wallet", "ledger", "withdraw", "deposit", "reward",
            "referral", "telegram", "reserve", "settle",
        ):
            self.assertNotIn(word, code)


if __name__ == "__main__":
    unittest.main()
