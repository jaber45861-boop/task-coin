"""
Tests for the USDT Wallet Service (``wallet.py``, Micro-task MT-2).

Coverage map (mandatory cases):

- Decimal <-> integer-unit conversion        -> TestUnitConversion
- amount validation (zero/negative/>8 dp)    -> TestAmountValidation
- lazy creation / balance reads              -> TestWalletLifecycle
- reserve (atomic, conditional UPDATE)       -> TestReserve
- release (exact inverse of reserve)         -> TestRelease
- settlement (held leaves exactly once)      -> TestSettlement
- schema guards (FK, updated_at, isolation)  -> TestDatabaseGuards
- concurrent reserve safety                  -> TestConcurrency
- scope & no-float policy                    -> TestScopePolicy

Also enforced:
- No ledger rows are created by wallet operations (MT-3 owns the ledger).
- No withdrawal rows are created (MT-4 owns withdrawal persistence).
- Mini App files are untouched.
- No float literal / float() / .float usage in wallet.py (AST scan,
  mirroring the withdrawal rules' source protection).

Run:
    python -m pytest test_wallet.py -v
"""

from __future__ import annotations

import ast
import inspect
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from decimal import Decimal

import db
import wallet
from wallet import (
    USDT_SCALE,
    InvalidWalletAmountError,
    InsufficientBalanceError,
    InsufficientHeldBalanceError,
    UserNotFoundError,
    WalletError,
    balance_of,
    decimal_to_units,
    ensure_wallet,
    release_units,
    reserve,
    settle_units,
    units_to_decimal,
    wallet_units,
)

# Reference amounts in integer wallet units
TEN_USDT = 1_000_000_000
THREE_USDT = 300_000_000
ONE_USDT = 100_000_000


class WalletTestBase(unittest.TestCase):
    """Temp-DB fixture following the project's existing test pattern."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)
        self.add_user(1)

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    # ── helpers ────────────────────────────────────────────────

    def add_user(self, user_id):
        self.assertTrue(db.register_user(user_id, f"u{user_id}", f"U{user_id}"))

    def fund(self, user_id, available_units, held_units=0):
        """Seed raw balances (test setup only — the wallet has no credit
        primitive by design; that is the Ledger service's job in MT-3)."""
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

    def units_row(self, user_id):
        with db.get_connection(self.db_path) as conn:
            return conn.execute(
                "SELECT available_units, held_units, updated_at "
                "FROM wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()

    def snapshot(self, user_id):
        row = self.units_row(user_id)
        return (row["available_units"], row["held_units"])

    def table_count(self, table):
        with db.get_connection(self.db_path) as conn:
            return conn.execute(
                f"SELECT COUNT(*) AS c FROM {table}"
            ).fetchone()["c"]


# ── 1–10 (+ extras): Decimal <-> integer-unit conversion ─────────────


class TestUnitConversion(WalletTestBase):

    def test_one_usdt_is_100m_units(self):
        """1. Decimal 1 USDT -> 100,000,000 units."""
        self.assertEqual(USDT_SCALE, 100_000_000)
        self.assertEqual(decimal_to_units(Decimal("1")), 100_000_000)

    def test_one_point_five_usdt_units(self):
        """2. Decimal 1.5 -> 150,000,000 units."""
        self.assertEqual(decimal_to_units(Decimal("1.5")), 150_000_000)

    def test_smallest_unit_is_one(self):
        """3. Decimal 0.00000001 -> 1 unit."""
        self.assertEqual(decimal_to_units(Decimal("0.00000001")), 1)

    def test_100m_units_is_one_usdt(self):
        """4. units 100,000,000 -> Decimal 1."""
        value = units_to_decimal(100_000_000)
        self.assertIsInstance(value, Decimal)
        self.assertEqual(value, Decimal("1"))

    def test_one_unit_is_smallest_decimal(self):
        """5. units 1 -> Decimal 0.00000001."""
        self.assertEqual(units_to_decimal(1), Decimal("0.00000001"))

    def test_conversion_rejects_beyond_eight_decimals(self):
        """6. Precision > 8 dp is rejected — never silently rounded."""
        for bad in ("0.000000001", "1.123456789", Decimal("0.000000001")):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidWalletAmountError):
                    decimal_to_units(bad)
        # exactly 8 dp is fine
        self.assertEqual(
            decimal_to_units(Decimal("1.12345678")), 112_345_678
        )

    def test_conversion_rejects_float(self):
        """7. float input is rejected explicitly."""
        with self.assertRaises(InvalidWalletAmountError):
            decimal_to_units(1.5)
        with self.assertRaises(InvalidWalletAmountError):
            decimal_to_units(0.00000001)

    def test_conversion_rejects_bool(self):
        """8. bool input is rejected (bool is not money)."""
        for bad in (True, False):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidWalletAmountError):
                    decimal_to_units(bad)

    def test_conversion_rejects_nan(self):
        """9. NaN is rejected."""
        for bad in (Decimal("NaN"), Decimal("sNaN"), "NaN"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidWalletAmountError):
                    decimal_to_units(bad)

    def test_conversion_rejects_infinity(self):
        """10. Infinity is rejected (both signs)."""
        for bad in (Decimal("Infinity"), Decimal("-Infinity"), "inf"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidWalletAmountError):
                    decimal_to_units(bad)

    def test_conversion_rejects_negative(self):
        """Extra: negative amounts are rejected (wallet magnitudes)."""
        for bad in (Decimal("-1"), Decimal("-0.00000001"), "-5"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidWalletAmountError):
                    decimal_to_units(bad)

    def test_conversion_accepts_exact_string(self):
        """Extra: str input is normalized through Decimal (exact text)."""
        self.assertEqual(decimal_to_units("1.5"), 150_000_000)
        self.assertEqual(decimal_to_units("0.00000001"), 1)
        self.assertEqual(decimal_to_units("12.5"), 1_250_000_000)

    def test_conversion_rejects_invalid_string(self):
        """Extra: non-decimal strings are rejected, not coerced."""
        for bad in ("abc", "", "1.2.3"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidWalletAmountError):
                    decimal_to_units(bad)

    def test_conversion_rejects_unsupported_type(self):
        """Extra: unsupported types are rejected with a wallet error."""
        for bad in (None, [], object()):
            with self.subTest(bad=type(bad)):
                with self.assertRaises(InvalidWalletAmountError):
                    decimal_to_units(bad)

    def test_units_to_decimal_rejects_float(self):
        """Extra: units_to_decimal refuses float input."""
        with self.assertRaises(InvalidWalletAmountError):
            units_to_decimal(100000000.0)

    def test_units_to_decimal_rejects_bool(self):
        """Extra: units_to_decimal refuses bool input."""
        for bad in (True, False):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidWalletAmountError):
                    units_to_decimal(bad)

    def test_units_to_decimal_rejects_non_integer(self):
        """Extra: unit counts must be int (str/Decimal rejected here)."""
        for bad in ("100", Decimal("100")):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidWalletAmountError):
                    units_to_decimal(bad)

    def test_round_trip_is_exact(self):
        """Extra: units -> Decimal -> units never loses a unit."""
        for units in (0, 1, 2, 999_999_999, ONE_USDT, TEN_USDT,
                      12_345_678_901):
            with self.subTest(units=units):
                self.assertEqual(
                    decimal_to_units(units_to_decimal(units)), units
                )
        self.assertEqual(
            decimal_to_units(Decimal("12.5")), 1_250_000_000
        )
        self.assertEqual(
            units_to_decimal(1_250_000_000), Decimal("12.5")
        )


# ── 11–16 (+ extras): amount validation at the operation boundary ────


class TestAmountValidation(WalletTestBase):

    def test_reserve_rejects_zero_amount(self):
        """11. reserve(0) is rejected."""
        ensure_wallet(1)
        with self.assertRaises(InvalidWalletAmountError):
            reserve(1, Decimal("0"))

    def test_reserve_rejects_negative_amount(self):
        """12. reserve(negative) is rejected."""
        ensure_wallet(1)
        with self.assertRaises(InvalidWalletAmountError):
            reserve(1, Decimal("-1"))
        with self.assertRaises(InvalidWalletAmountError):
            reserve(1, "-10")

    def test_release_rejects_zero_units(self):
        """13. release_units(0) is rejected."""
        ensure_wallet(1)
        with self.assertRaises(InvalidWalletAmountError):
            release_units(1, 0)

    def test_release_rejects_negative_units(self):
        """14. release_units(negative) is rejected."""
        ensure_wallet(1)
        with self.assertRaises(InvalidWalletAmountError):
            release_units(1, -1)

    def test_settlement_rejects_zero_units(self):
        """15. settle_units(0) is rejected."""
        ensure_wallet(1)
        with self.assertRaises(InvalidWalletAmountError):
            settle_units(1, 0)

    def test_settlement_rejects_negative_units(self):
        """16. settle_units(negative) is rejected."""
        ensure_wallet(1)
        with self.assertRaises(InvalidWalletAmountError):
            settle_units(1, -ONE_USDT)

    def test_reserve_rejects_more_than_eight_decimals(self):
        """Extra: reserve enforces the 8-dp representation limit."""
        self.fund(1, TEN_USDT)
        before = self.snapshot(1)
        with self.assertRaises(InvalidWalletAmountError):
            reserve(1, Decimal("0.000000001"))
        with self.assertRaises(InvalidWalletAmountError):
            reserve(1, "0.000000001")
        self.assertEqual(self.snapshot(1), before)

    def test_unit_operations_reject_bool_and_float(self):
        """Extra: unit-denominated ops refuse bool/float/non-int."""
        ensure_wallet(1)
        for bad in (True, False, 1.5, "100"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidWalletAmountError):
                    release_units(1, bad)
                with self.assertRaises(InvalidWalletAmountError):
                    settle_units(1, bad)


# ── 17–22, 38: creation / balance reads ──────────────────────────────


class TestWalletLifecycle(WalletTestBase):

    def test_ensure_wallet_creates_wallet(self):
        """17. ensure_wallet creates the row on demand."""
        self.assertIsNone(self.units_row(1))
        created = ensure_wallet(1)
        self.assertTrue(created)
        row = self.units_row(1)
        self.assertIsNotNone(row)
        self.assertEqual(row["available_units"], 0)
        self.assertEqual(row["held_units"], 0)

    def test_ensure_wallet_is_idempotent(self):
        """18. Repeated ensure calls are safe and report no new row."""
        self.assertTrue(ensure_wallet(1))
        self.assertFalse(ensure_wallet(1))
        self.assertFalse(ensure_wallet(1))

    def test_unknown_user_is_rejected(self):
        """19. Missing users are rejected by ensure/balance/reserve."""
        with self.assertRaises(UserNotFoundError):
            ensure_wallet(999_999)
        with self.assertRaises(UserNotFoundError):
            balance_of(999_999)
        with self.assertRaises(UserNotFoundError):
            reserve(999_999, Decimal("1"))
        with self.assertRaises(UserNotFoundError):
            release_units(999_999, 1)
        with self.assertRaises(UserNotFoundError):
            settle_units(999_999, 1)
        # invalid ids behave as "no such user"
        for bad_id in (0, -1, True, "1"):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(UserNotFoundError):
                    ensure_wallet(bad_id)
        # nothing was created for the unknown user
        with db.get_connection(self.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM wallets WHERE user_id = 999999"
            ).fetchone()
        self.assertEqual(count["c"], 0)

    def test_new_wallet_starts_at_zero(self):
        """20. A fresh wallet holds exactly 0 available / 0 held."""
        ensure_wallet(1)
        self.assertEqual(self.snapshot(1), (0, 0))
        self.assertEqual(balance_of(1), Decimal("0"))
        self.assertEqual(wallet_units(1).available_units, 0)
        self.assertEqual(wallet_units(1).held_units, 0)

    def test_balance_of_returns_available_balance(self):
        """21. balance_of returns an exact Decimal of available USDT."""
        ensure_wallet(1)
        self.fund(1, 1_250_000_000)          # 12.50000000 USDT
        balance = balance_of(1)
        self.assertIsInstance(balance, Decimal)
        self.assertNotIsInstance(balance, float)
        self.assertEqual(balance, Decimal("12.5"))
        self.assertEqual(str(balance), "12.50000000")

    def test_held_balance_not_included_in_balance_of(self):
        """22. Held funds are never spendable balance."""
        ensure_wallet(1)
        self.fund(1, 700_000_000, held_units=300_000_000)
        self.assertEqual(balance_of(1), Decimal("7"))
        self.assertEqual(wallet_units(1).held_units, 300_000_000)

    def test_balance_of_reads_user_without_wallet_row(self):
        """Extra: existing user, no wallet row yet -> 0, no side effects."""
        self.assertEqual(balance_of(1), Decimal("0"))
        self.assertEqual(wallet_units(1).available_units, 0)
        self.assertIsNone(self.units_row(1))   # read did not create a row

    def test_repeated_ensure_never_duplicates_rows(self):
        """38. One row per user no matter how often ensure runs."""
        for _ in range(5):
            ensure_wallet(1)
        with db.get_connection(self.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM wallets WHERE user_id = 1"
            ).fetchone()
        self.assertEqual(count["c"], 1)


# ── 23–27 (+ extra): reserve ─────────────────────────────────────────


class TestReserve(WalletTestBase):

    def test_reserve_decreases_available(self):
        """23. reserve moves funds out of available."""
        ensure_wallet(1)
        self.fund(1, TEN_USDT)
        reserve(1, Decimal("3"))
        self.assertEqual(self.snapshot(1)[0], 700_000_000)

    def test_reserve_increases_held(self):
        """24. reserve moves the same funds into held."""
        ensure_wallet(1)
        self.fund(1, TEN_USDT)
        reserve(1, Decimal("3"))
        self.assertEqual(self.snapshot(1)[1], THREE_USDT)
        self.assertEqual(wallet_units(1).held_units, THREE_USDT)

    def test_reserve_accepts_exact_eight_decimal_amount(self):
        """25. The smallest representable amount reserves exactly 1 unit."""
        ensure_wallet(1)
        self.fund(1, 100)
        units = reserve(1, Decimal("0.00000001"))
        self.assertEqual(units, 1)
        self.assertEqual(self.snapshot(1), (99, 1))
        # 12.5 USDT also works exactly
        self.assertEqual(
            decimal_to_units(Decimal("12.5")), 1_250_000_000
        )

    def test_reserve_insufficient_balance_fails(self):
        """26. Over-reserving raises the dedicated exception."""
        ensure_wallet(1)
        self.fund(1, ONE_USDT)
        with self.assertRaises(InsufficientBalanceError):
            reserve(1, Decimal("5"))
        # also an exception hierarchy check
        with self.assertRaises(WalletError):
            reserve(1, Decimal("5"))

    def test_failed_reserve_leaves_balance_unchanged(self):
        """27. A failed reserve makes NO balance change."""
        ensure_wallet(1)
        self.fund(1, ONE_USDT, held_units=50_000_000)
        before = self.snapshot(1)
        with self.assertRaises(InsufficientBalanceError):
            reserve(1, Decimal("2"))
        self.assertEqual(self.snapshot(1), before)

    def test_reserve_returns_reserved_units(self):
        """Extra: reserve reports the integer units it reserved."""
        ensure_wallet(1)
        self.fund(1, TEN_USDT)
        self.assertEqual(reserve(1, Decimal("1.5")), 150_000_000)
        self.assertEqual(self.snapshot(1), (850_000_000, 150_000_000))


# ── 28–31, 36: release ───────────────────────────────────────────────


class TestRelease(WalletTestBase):

    def test_release_decreases_held(self):
        """28. release takes funds out of held."""
        ensure_wallet(1)
        self.fund(1, available_units=700_000_000, held_units=THREE_USDT)
        release_units(1, THREE_USDT)
        self.assertEqual(self.snapshot(1)[1], 0)

    def test_release_increases_available(self):
        """29. release returns the same funds to available."""
        ensure_wallet(1)
        self.fund(1, available_units=700_000_000, held_units=THREE_USDT)
        release_units(1, THREE_USDT)
        self.assertEqual(self.snapshot(1)[0], 1_000_000_000)

    def test_release_insufficient_held_fails(self):
        """30. Releasing more than held raises the held exception."""
        ensure_wallet(1)
        self.fund(1, available_units=500_000_000, held_units=200_000_000)
        with self.assertRaises(InsufficientHeldBalanceError):
            release_units(1, THREE_USDT)

    def test_failed_release_leaves_state_unchanged(self):
        """31. A failed release makes NO balance change."""
        ensure_wallet(1)
        self.fund(1, available_units=500_000_000, held_units=200_000_000)
        before = self.snapshot(1)
        with self.assertRaises(InsufficientHeldBalanceError):
            release_units(1, THREE_USDT)
        self.assertEqual(self.snapshot(1), before)

    def test_reserve_then_release_restores_exact_state(self):
        """36. release is the exact inverse of reserve."""
        ensure_wallet(1)
        self.fund(1, TEN_USDT)
        original = self.snapshot(1)
        reserve(1, Decimal("3"))
        self.assertNotEqual(self.snapshot(1), original)
        release_units(1, THREE_USDT)
        self.assertEqual(self.snapshot(1), original)
        self.assertEqual(balance_of(1), Decimal("10"))


# ── 32–35, 37: settlement ────────────────────────────────────────────


class TestSettlement(WalletTestBase):

    def test_settlement_decreases_held(self):
        """32. settle reduces held."""
        ensure_wallet(1)
        self.fund(1, available_units=700_000_000, held_units=THREE_USDT)
        settle_units(1, THREE_USDT)
        self.assertEqual(self.snapshot(1)[1], 0)

    def test_settlement_does_not_decrease_available(self):
        """33. Available is NOT debited again at settlement."""
        ensure_wallet(1)
        self.fund(1, available_units=700_000_000, held_units=THREE_USDT)
        settle_units(1, THREE_USDT)
        self.assertEqual(self.snapshot(1)[0], 700_000_000)
        self.assertEqual(balance_of(1), Decimal("7"))

    def test_settlement_insufficient_held_fails(self):
        """34. Settling more than held raises the held exception."""
        ensure_wallet(1)
        self.fund(1, available_units=500_000_000, held_units=100_000_000)
        with self.assertRaises(InsufficientHeldBalanceError):
            settle_units(1, THREE_USDT)

    def test_failed_settlement_leaves_state_unchanged(self):
        """35. A failed settlement makes NO state change."""
        ensure_wallet(1)
        self.fund(1, available_units=500_000_000, held_units=100_000_000)
        before = self.snapshot(1)
        with self.assertRaises(InsufficientHeldBalanceError):
            settle_units(1, THREE_USDT)
        self.assertEqual(self.snapshot(1), before)

    def test_reserve_then_settle_debits_available_exactly_once(self):
        """37. available is reduced once at reserve; settlement only
        clears the held remainder — never a second deduction."""
        ensure_wallet(1)
        self.fund(1, TEN_USDT)
        reserve(1, Decimal("3"))
        self.assertEqual(self.snapshot(1), (700_000_000, THREE_USDT))
        settle_units(1, THREE_USDT)
        self.assertEqual(self.snapshot(1), (700_000_000, 0))
        self.assertEqual(balance_of(1), Decimal("7"))
        # nothing left to settle
        with self.assertRaises(InsufficientHeldBalanceError):
            settle_units(1, THREE_USDT)
        self.assertEqual(self.snapshot(1), (700_000_000, 0))


# ── 39, 40, 43, 44: schema & scope guards ────────────────────────────


class TestDatabaseGuards(WalletTestBase):

    def test_wallet_foreign_key_remains_enforced(self):
        """39. PRAGMA foreign_keys stays ON and wallets.user_id FK holds."""
        with db.get_connection(self.db_path) as conn:
            self.assertEqual(
                conn.execute("PRAGMA foreign_keys").fetchone()[0], 1
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with db.get_connection(self.db_path) as conn:
                conn.execute("INSERT INTO wallets (user_id) VALUES (999999)")

    def test_updated_at_changes_on_mutation(self):
        """40. Successful wallet mutations refresh updated_at."""
        ensure_wallet(1)
        self.fund(1, TEN_USDT)
        with db.get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE wallets SET updated_at = '2000-01-01 00:00:00' "
                "WHERE user_id = 1"
            )
        old = self.units_row(1)["updated_at"]
        self.assertEqual(old, "2000-01-01 00:00:00")
        reserve(1, Decimal("1"))
        new = self.units_row(1)["updated_at"]
        self.assertNotEqual(new, old)
        release_units(1, ONE_USDT)
        released = self.units_row(1)["updated_at"]
        self.assertNotEqual(released, old)
        # settle path: reserve again then settle, updated_at stays current
        reserve(1, Decimal("1"))
        settle_units(1, ONE_USDT)
        self.assertNotEqual(self.units_row(1)["updated_at"], old)

    def test_wallet_ops_create_no_ledger_rows(self):
        """43. MT-2 never writes to the ledger (MT-3 owns it)."""
        ensure_wallet(1)
        self.fund(1, TEN_USDT)
        reserve(1, Decimal("3"))
        release_units(1, THREE_USDT)
        reserve(1, Decimal("3"))
        settle_units(1, THREE_USDT)
        self.assertEqual(self.table_count("ledger"), 0)

    def test_wallet_ops_create_no_withdrawal_rows(self):
        """44. MT-2 never writes withdrawal_requests (MT-4 owns them)."""
        ensure_wallet(1)
        self.fund(1, TEN_USDT)
        reserve(1, Decimal("3"))
        settle_units(1, THREE_USDT)
        self.assertEqual(self.table_count("withdrawal_requests"), 0)


# ── 41, 42: concurrency ──────────────────────────────────────────────


class TestConcurrency(WalletTestBase):

    def _run_concurrent_reserves(self, start_units, amount_units,
                                 threads=8, attempts_per_thread=3):
        """Hammer reserve() from *threads* threads; return outcomes."""
        ensure_wallet(1)
        self.fund(1, start_units)
        results: list = []
        lock = threading.Lock()
        barrier = threading.Barrier(threads)

        def worker():
            barrier.wait()
            amount = units_to_decimal(amount_units)
            for _ in range(attempts_per_thread):
                try:
                    reserve(1, amount)
                    outcome = "ok"
                except InsufficientBalanceError:
                    outcome = "insufficient"
                except Exception as exc:          # noqa: BLE001
                    outcome = exc
                with lock:
                    results.append(outcome)

        workers = [threading.Thread(target=worker) for _ in range(threads)]
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join(timeout=30)
        for thread in workers:
            self.assertFalse(thread.is_alive(), "worker thread hung")
        return results

    def test_concurrent_reserves_are_atomic(self):
        """41. Concurrent reserves never overspend: available tracks
        exactly start - successes * amount and stays non-negative."""
        results = self._run_concurrent_reserves(TEN_USDT, ONE_USDT)
        unexpected = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(unexpected, [], f"unexpected errors: {unexpected}")
        successes = results.count("ok")
        available, held = self.snapshot(1)
        # no read-calculate-write race: state matches successes exactly
        self.assertEqual(available, TEN_USDT - successes * ONE_USDT)
        self.assertEqual(held, successes * ONE_USDT)
        self.assertGreaterEqual(available, 0)

    def test_total_reserves_never_exceed_starting_balance(self):
        """42. Successful reservations can never exceed the balance:
        24 attempts against 10 USDT yield exactly 10 successes."""
        results = self._run_concurrent_reserves(
            TEN_USDT, ONE_USDT, threads=8, attempts_per_thread=3
        )
        unexpected = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(unexpected, [], f"unexpected errors: {unexpected}")
        successes = results.count("ok")
        self.assertEqual(len(results), 24)
        self.assertEqual(successes, 10)
        self.assertEqual(results.count("insufficient"), 14)
        self.assertLessEqual(successes * ONE_USDT, TEN_USDT)
        available, held = self.snapshot(1)
        self.assertEqual(available, 0)
        self.assertEqual(held, TEN_USDT)


# ── 45, 46: scope & no-float policy ──────────────────────────────────


class TestScopePolicy(WalletTestBase):

    def test_no_miniapp_files_changed(self):
        """45. This micro-task does not touch the Mini App."""
        repo_root = os.path.dirname(os.path.abspath(__file__))
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain", "--", "miniapp/"],
                cwd=repo_root, capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git is not available in this environment")
        if result.returncode != 0:
            self.skipTest(f"git status failed: {result.stderr.strip()}")
        self.assertEqual(
            result.stdout.strip(), "",
            f"Mini App files were modified:\n{result.stdout}",
        )

    def test_source_contains_no_forbidden_float_implementation(self):
        """46. wallet.py has no float literals, float() calls or .float
        usage (isinstance(..., float) guards are allowed — they are how
        the module rejects float inputs)."""
        tree = ast.parse(inspect.getsource(wallet))
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

    def test_wallet_does_not_import_withdrawal_rules(self):
        """Extra: wallet.py stays decoupled from the withdrawal rules."""
        source = inspect.getsource(wallet)
        self.assertNotIn("import withdrawal_rules", source)
        self.assertNotIn("from withdrawal_rules", source)


if __name__ == "__main__":
    unittest.main()
