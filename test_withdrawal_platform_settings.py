"""
Focused tests — Platform Withdrawal Settings Integration (MT-ADMIN-24)
=======================================================================

The production create flow now reads ``minimum_withdrawal_units`` and
``withdrawal_fee_units`` (MT-ADMIN-15 platform settings) on the SAME
transaction connection and drives the USDT minimum/fee from them —
exact integer atomic USDT units, never float, never a Decimal display
comparison.  Vodafone Cash keeps the approved EGP contract (the
settings are USDT-denominated, so applying them there would add a
second fee in the wrong unit — the unit conflict is reported, not
silently absorbed).  Reject/complete stay settings-free and keep
operating on the immutable persisted ``wallet_debit_units``.

  1. configured minimum accepted exactly
  2. amount below configured minimum rejected (no writes)
  3. configured minimum larger than the previous hardcoded behavior
  4. configured withdrawal fee is used
  5. USDT debit == amount + configured fee exactly
  6. no float arithmetic (AST + integer columns)
  7. settings are read through the transaction connection
  8. settings-read failure causes complete rollback
  9. wallet reserve failure causes complete rollback
 10. ledger hold failure causes complete rollback
 11. repository insert failure causes complete rollback
 12. concurrent pending invariant remains intact
 13. reject does not reread current fee/minimum
 14. complete does not reread current fee/minimum
 15. changing settings after creation does not alter persisted facts
 16. missing required setting follows the platform-settings contract
 17. zero/negative/invalid setting values follow the validation
     contract
 18. existing MT-ADMIN-23 Vodafone behavior remains unchanged
 19. existing MT-ADMIN-23 USDT no-round-trip behavior remains
     unchanged
 20. legacy rows still behave exactly as MT-ADMIN-23 established

Temp databases only; no production destinations or balances are used.
Money math is Decimal/int only — floats are forbidden.

Run:
    python -m pytest test_withdrawal_platform_settings.py -v
"""

from __future__ import annotations

import ast
import inspect
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock

import config
import db
import payment_method_store
import platform_settings
import wallet
import withdrawal_service
from platform_settings import (
    PlatformSettingError,
    SettingNotFoundError,
    SettingValidationError,
)
from rate_quote import RateQuote
from withdrawal_rules import (
    METHOD_USDT_BEP20,
    METHOD_VODAFONE_CASH,
    CooldownError,
    InvalidAmountError,
    PendingWithdrawalExistsError,
    RequestStatus,
    WithdrawalError,
)
from withdrawal_service import MissingWalletDebitError, WithdrawalService
from withdrawal_store import (
    SqliteLedgerAdapter,
    SqliteWalletAdapter,
    SqliteWithdrawalRepository,
)

# ── Exact reference values ──────────────────────────────────────────
# rate = 48.5 EGP per USDT; the configured test values below are
# deliberately NOT rate-derived so the assertions can only pass when
# the service genuinely uses the stored settings.
RATE = Decimal("48.5")
CAPTURED_AT = datetime(2026, 9, 27, 9, 59, 0, tzinfo=timezone.utc)
QUOTE = RateQuote(RATE, "manual", CAPTURED_AT)
NOW = datetime(2026, 9, 27, 10, 0, 0)          # naive UTC wall-clock
ADMIN_ID = 900_240
FUND = 1_000_000_000                            # 10 USDT in units
USER_DEST = "+201001234567 (TEST)"
PM_DESTINATION = "TEST-PLATFORM-DEST-9"

MIN_UNITS = 150_000_000                         # configured 1.5 USDT
FEE_UNITS = 123_457                             # configured 0.00123457
AMOUNT_1_5 = Decimal("1.5")
AMOUNT_1_5_UNITS = 150_000_000
CONFIGURED_DEBIT_1_5 = 150_123_457              # amount + fee, exact
RATE_DERIVED_FEE_485 = 2_061_856                # 1 EGP at 48.5 (old)
VODAFONE_DEBIT = 22_680_413                     # 11 EGP ceiling @ 48.5


# ── Rollback-injection doubles (same shapes as MT-ADMIN-23) ────────


class _FailingRepository(SqliteWithdrawalRepository):
    """Repository whose insert always fails (rollback injection)."""

    def __init__(self, db_path, exc):
        super().__init__(db_path)
        self._exc = exc

    def insert(self, request, *, connection=None):
        raise self._exc


class _FailingLedger(SqliteLedgerAdapter):
    """Ledger whose hold always fails AFTER the wallet reserve ran."""

    def __init__(self, exc):
        self._exc = exc

    def hold(self, user_id, amount_units, *, request_id, connection,
             rate_usdt_egp=None):
        raise self._exc


class _FailingWallet(SqliteWalletAdapter):
    """Wallet whose reserve always fails (rollback injection)."""

    def __init__(self, exc):
        self._exc = exc

    def reserve(self, user_id, amount_units, *, connection=None):
        raise self._exc


class _Base(unittest.TestCase):
    """Temp DB fixture with the two withdrawal settings configured."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)
        self.svc = WithdrawalService(db_path=self.db_path)
        self.pm = self.create_method()
        self._orig_admins = list(config.ADMINS)
        config.ADMINS[:] = [ADMIN_ID]
        self.addCleanup(self._restore_admins)
        self.set_settings(minimum_units=MIN_UNITS, fee_units=FEE_UNITS)
        self.addCleanup(self._restore)

    def _restore(self):
        db.DB_PATH = self._original_db_path
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    def _restore_admins(self):
        config.ADMINS[:] = self._orig_admins

    # ── seed helpers ───────────────────────────────────────────

    def set_settings(self, *, minimum_units: int, fee_units: int) -> None:
        platform_settings.set_setting(
            platform_settings.MINIMUM_WITHDRAWAL_UNITS,
            minimum_units,
            admin_user_id=ADMIN_ID,
            db_path=self.db_path,
        )
        platform_settings.set_setting(
            platform_settings.WITHDRAWAL_FEE_UNITS,
            fee_units,
            admin_user_id=ADMIN_ID,
            db_path=self.db_path,
        )

    def clear_setting(self, key: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "DELETE FROM platform_settings WHERE key = ?", (key,)
            )
            conn.commit()
        finally:
            conn.close()

    def add_user(self, user_id: int = 501) -> int:
        self.assertTrue(
            db.register_user(user_id, f"u{user_id}", f"U{user_id}")
        )
        return user_id

    def fund(self, user_id: int = 501, units: int = FUND) -> int:
        wallet.ensure_wallet(user_id)          # standalone scope
        return wallet.credit_units(user_id, units)

    def create_method(self, *, active: bool = True, **overrides):
        kwargs = dict(
            category="cash",
            display_name="TEST-METHOD",
            asset="EGP",
            provider="TEST-PROVIDER",
            destination=PM_DESTINATION,
            created_by=1,
            db_path=self.db_path,
        )
        kwargs.update(overrides)
        return payment_method_store.create_payment_method(**kwargs)

    def insert_legacy_null_debit(self, request_id="wd-legacy",
                                 user_id=501):
        """Pre-MT-ADMIN-19 row: NULL debit, NULL user_destination."""
        with db.get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO withdrawal_requests (
                       request_id, user_id, method, amount_egp_minor,
                       fee_egp_minor, amount_native_minor,
                       fee_native_minor, native_unit, rate_usdt_egp,
                       wallet_rate_usdt_egp, rate_captured_at,
                       rate_provider, status, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, user_id, "vodafone_cash", 1000, 100,
                 1000, 100, "EGP", None, QUOTE.rate_text,
                 "2026-09-27T09:59:00+00:00",
                 "manual", "pending", "2026-01-01 00:00:00"),
            )

    # ── create / raw readers ───────────────────────────────────

    def create(self, method=METHOD_USDT_BEP20, amount=None, *,
               user_id=501, now=NOW, quote=QUOTE, svc=None, **extra):
        if amount is None:
            amount = (
                Decimal("10.00")
                if method == METHOD_VODAFONE_CASH
                else AMOUNT_1_5
            )
        kwargs = dict(
            payment_method_id=self.pm.id,
            user_destination=USER_DEST,
            now=now,
            quote=quote,
        )
        kwargs.update(extra)
        service = self.svc if svc is None else svc
        return service.create(user_id, method, amount, **kwargs)

    def raw_row(self, request_id: str) -> sqlite3.Row:
        with db.get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM withdrawal_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        self.assertIsNotNone(row, f"row {request_id!r} missing")
        return row

    def wallet_state(self, user_id: int = 501):
        with db.get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT available_units, held_units FROM wallets "
                "WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return (row["available_units"], row["held_units"])

    def ledger_rows(self, request_id: str | None = None) -> list:
        sql = "SELECT * FROM ledger"
        params: tuple = ()
        if request_id is not None:
            sql += (
                " WHERE reference_type = 'withdrawal' "
                "AND reference_id = ?"
            )
            params = (request_id,)
        sql += " ORDER BY id"
        with db.get_connection(self.db_path) as conn:
            return conn.execute(sql, params).fetchall()

    def request_count(self, user_id: int | None = None) -> int:
        sql = "SELECT COUNT(*) FROM withdrawal_requests"
        params: tuple = ()
        if user_id is not None:
            sql += " WHERE user_id = ?"
            params = (user_id,)
        with db.get_connection(self.db_path) as conn:
            return conn.execute(sql, params).fetchone()[0]

    def pending_count(self, user_id: int | None = None) -> int:
        sql = (
            "SELECT COUNT(*) FROM withdrawal_requests "
            "WHERE status='pending'"
        )
        params: tuple = ()
        if user_id is not None:
            sql += " AND user_id = ?"
            params = (user_id,)
        with db.get_connection(self.db_path) as conn:
            return conn.execute(sql, params).fetchone()[0]

    def assert_no_create_state(self, user_id: int = 501) -> None:
        """The failed create left no request, no hold, no ledger row."""
        self.assertEqual(self.request_count(), 0)
        self.assertEqual(self.wallet_state(user_id), (FUND, 0))
        self.assertEqual(self.ledger_rows(), [])


# ── 1–6: configured minimum / fee ──────────────────────────────────


class TestConfiguredMinimumAndFee(_Base):

    def test_01_configured_minimum_accepted_exactly(self):
        """1. An amount equal to the configured minimum (exact
        integer units) is accepted — the boundary itself passes."""
        self.add_user(501)
        self.fund(501, FUND)
        # MIN_UNITS is exactly 1.5 USDT; the request is exactly 1.5
        req = self.create(METHOD_USDT_BEP20, AMOUNT_1_5)
        self.assertEqual(req.status, RequestStatus.PENDING)
        row = self.raw_row(req.request_id)
        self.assertEqual(row["amount_native_minor"], MIN_UNITS)
        self.assertEqual(
            row["wallet_debit_units"],
            MIN_UNITS + FEE_UNITS,
        )
        self.assertEqual(
            self.wallet_state(501),
            (FUND - (MIN_UNITS + FEE_UNITS), MIN_UNITS + FEE_UNITS),
        )

    def test_02_below_configured_minimum_rejected(self):
        """2. One unit below the configured minimum is a domain
        rejection before any wallet/ledger/repository write."""
        self.add_user(501)
        self.fund(501, FUND)
        for bad in (Decimal("1.49999999"), Decimal("1.49")):
            with self.assertRaises(InvalidAmountError, msg=repr(bad)):
                self.create(METHOD_USDT_BEP20, bad)
        self.assert_no_create_state()

    def test_03_configured_minimum_larger_than_previous_behavior(self):
        """3. A configured minimum ABOVE the previous hardcoded
        behavior (10 EGP ≈ 0.20618557 USDT at 48.5) governs: amounts
        that used to be accepted are now rejected, and the new
        boundary is exact."""
        self.add_user(501)
        self.fund(501, FUND)
        self.set_settings(
            minimum_units=500_000_000,      # 5 USDT, well above 0.206…
            fee_units=FEE_UNITS,
        )
        # both would clear the old 10-EGP-equivalent minimum
        for bad in (Decimal("0.3"), Decimal("4.99999999")):
            with self.assertRaises(InvalidAmountError, msg=repr(bad)):
                self.create(METHOD_USDT_BEP20, bad)
        self.assert_no_create_state()
        # the new boundary itself is accepted exactly
        req = self.create(METHOD_USDT_BEP20, Decimal("5.0"))
        self.assertEqual(
            self.raw_row(req.request_id)["wallet_debit_units"],
            500_000_000 + FEE_UNITS,
        )

    def test_04_configured_withdrawal_fee_is_used(self):
        """4. The persisted fee is the configured value — not a
        rate-derived one (123_457 ≠ ceil(1/48.5) = 2_061_856)."""
        self.add_user(501)
        self.fund(501, FUND)
        req = self.create(METHOD_USDT_BEP20, AMOUNT_1_5)
        row = self.raw_row(req.request_id)
        self.assertEqual(row["fee_native_minor"], FEE_UNITS)
        self.assertEqual(row["fee_native_minor"],
                         platform_settings.get_setting(
                             platform_settings.WITHDRAWAL_FEE_UNITS,
                             db_path=self.db_path,
                         ))
        self.assertNotEqual(row["fee_native_minor"],
                            RATE_DERIVED_FEE_485)
        self.assertEqual(req.fee_native, Decimal("0.00123457"))
        # the EGP display facts keep the approved rule-4 value
        self.assertEqual(row["fee_egp_minor"], 100)

    def test_05_usdt_debit_equals_amount_plus_configured_fee(self):
        """5. wallet_debit_units = requested units + configured fee
        as an exact integer sum, mirrored by wallet and ledger."""
        self.add_user(501)
        self.fund(501, FUND)
        req = self.create(METHOD_USDT_BEP20, AMOUNT_1_5)
        self.assertIs(type(req.wallet_debit_units), int)
        self.assertEqual(
            req.wallet_debit_units,
            AMOUNT_1_5_UNITS + FEE_UNITS,
        )
        self.assertEqual(req.wallet_debit_units, CONFIGURED_DEBIT_1_5)
        self.assertEqual(
            self.wallet_state(501),
            (FUND - CONFIGURED_DEBIT_1_5, CONFIGURED_DEBIT_1_5),
        )
        ledger = self.ledger_rows(req.request_id)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["amount_units"],
                         CONFIGURED_DEBIT_1_5)

    def test_06_no_float_arithmetic(self):
        """6. The service source has no float literal/call/reference
        and no REAL token; every persisted money value is INTEGER."""
        self.add_user(501)
        self.fund(501, FUND)
        req = self.create(METHOD_USDT_BEP20, AMOUNT_1_5)

        src = inspect.getsource(withdrawal_service)
        tree = ast.parse(src)
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
            if isinstance(node, ast.Constant) \
                    and isinstance(node.value, float):
                self.fail(f"float literal at line {node.lineno}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "float"
            ):
                self.fail(f"float() call at line {node.lineno}")
            if (
                isinstance(node, ast.Name)
                and node.id == "float"
                and node.id not in guarded
            ):
                self.fail(f"float reference at line {node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr == "float":
                self.fail(f"float attribute at line {node.lineno}")
        self.assertNotIn("REAL", src.upper())

        with db.get_connection(self.db_path) as conn:
            kinds = conn.execute(
                "SELECT typeof(amount_native_minor),"
                "       typeof(fee_native_minor),"
                "       typeof(wallet_debit_units)"
                "  FROM withdrawal_requests WHERE request_id = ?",
                (req.request_id,),
            ).fetchone()
            declared = [
                c[2].upper()
                for c in conn.execute(
                    "PRAGMA table_info(withdrawal_requests)"
                ).fetchall()
            ]
        self.assertEqual(
            list(kinds),
            ["integer", "integer", "integer"],
        )
        for banned in ("REAL", "FLOAT", "DOUBLE", "NUMERIC"):
            self.assertNotIn(banned, declared)


# ── 7–11: transaction / rollback ───────────────────────────────────


class TestTransactionAndRollback(_Base):

    def test_07_settings_read_through_the_transaction_connection(self):
        """7. Both settings are read with the exact connection the
        create transaction yields — no separate read connection."""
        self.add_user(501)
        self.fund(501, FUND)
        real_transaction = db.transaction
        real_get = platform_settings.get_required_setting
        seen_tx: list = []
        seen_reads: list = []

        @contextmanager
        def spy_transaction(*args, **kwargs):
            with real_transaction(*args, **kwargs) as conn:
                seen_tx.append(conn)
                yield conn

        def spy_get(key, *, conn=None, db_path=None):
            seen_reads.append((key, conn, db_path))
            return real_get(key, conn=conn, db_path=db_path)

        with mock.patch.object(db, "transaction", spy_transaction), \
                mock.patch.object(
                    platform_settings, "get_required_setting", spy_get
                ):
            req = self.create(METHOD_USDT_BEP20, AMOUNT_1_5)

        self.assertEqual(len(seen_tx), 1)
        create_tx = seen_tx[0]
        self.assertEqual(
            [key for key, _, _ in seen_reads],
            [
                platform_settings.MINIMUM_WITHDRAWAL_UNITS,
                platform_settings.WITHDRAWAL_FEE_UNITS,
            ],
        )
        for _, conn, db_path in seen_reads:
            self.assertIs(conn, create_tx)
            self.assertIsNone(db_path)
        # and the facts really came from those reads
        self.assertEqual(
            self.raw_row(req.request_id)["wallet_debit_units"],
            CONFIGURED_DEBIT_1_5,
        )

    def test_08_settings_read_failure_rolls_back_completely(self):
        """8. A settings-read failure inside the transaction leaves
        the database byte-identical to its pre-create state, and the
        next healthy create succeeds (the transaction is clean)."""
        self.add_user(501)
        self.fund(501, FUND)
        self.clear_setting(platform_settings.WITHDRAWAL_FEE_UNITS)
        with self.assertRaises(SettingNotFoundError):
            self.create(METHOD_USDT_BEP20, AMOUNT_1_5)
        self.assert_no_create_state()
        # configuration restored → the very next create works
        self.set_settings(minimum_units=MIN_UNITS, fee_units=FEE_UNITS)
        req = self.create(METHOD_USDT_BEP20, AMOUNT_1_5)
        self.assertEqual(
            self.raw_row(req.request_id)["wallet_debit_units"],
            CONFIGURED_DEBIT_1_5,
        )

    def test_09_wallet_reserve_failure_rolls_back(self):
        """9. A wallet reserve failure rolls the whole create back —
        no row, no ledger entry, wallet untouched."""
        self.add_user(501)
        self.fund(501, FUND)
        sentinel = RuntimeError("reserve exploded")
        svc = WithdrawalService(
            db_path=self.db_path,
            wallet_port=_FailingWallet(sentinel),
        )
        with self.assertRaises(RuntimeError) as ctx:
            self.create(METHOD_USDT_BEP20, svc=svc)
        self.assertIs(ctx.exception, sentinel)
        self.assert_no_create_state()

    def test_10_ledger_hold_failure_rolls_back(self):
        """10. A ledger hold failure AFTER the wallet reserve rolls
        the reserve back with everything else."""
        self.add_user(501)
        self.fund(501, FUND)
        sentinel = RuntimeError("hold exploded")
        svc = WithdrawalService(
            db_path=self.db_path,
            ledger_port=_FailingLedger(sentinel),
        )
        with self.assertRaises(RuntimeError) as ctx:
            self.create(METHOD_USDT_BEP20, svc=svc)
        self.assertIs(ctx.exception, sentinel)
        self.assert_no_create_state()

    def test_11_repository_insert_failure_rolls_back(self):
        """11. A repository insert failure rolls back wallet AND
        ledger — nothing partial survives."""
        self.add_user(501)
        self.fund(501, FUND)
        sentinel = RuntimeError("insert exploded")
        svc = WithdrawalService(
            db_path=self.db_path,
            repository=_FailingRepository(self.db_path, sentinel),
        )
        with self.assertRaises(RuntimeError) as ctx:
            self.create(METHOD_USDT_BEP20, svc=svc)
        self.assertIs(ctx.exception, sentinel)
        self.assert_no_create_state()


# ── 12: concurrency ────────────────────────────────────────────────


class TestConcurrency(_Base):

    def test_12_concurrent_pending_invariant_intact(self):
        """12. Two concurrent creates yield exactly ONE pending row
        whose debit carries the configured fee — the race can neither
        duplicate the request nor bypass minimum/fee."""
        self.add_user(501)
        self.fund(501, FUND)
        barrier = threading.Barrier(2)
        outcomes = []

        def worker():
            barrier.wait()
            try:
                req = self.create(METHOD_USDT_BEP20, AMOUNT_1_5)
                outcomes.append(("ok", req))
            except WithdrawalError as exc:
                outcomes.append(("err", exc))
            except Exception as exc:  # pragma: no cover
                outcomes.append(("boom", exc))

        threads = []
        for _ in range(2):
            thread = threading.Thread(target=worker)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(outcomes), 2)
        oks = [o for o in outcomes if o[0] == "ok"]
        errs = [o for o in outcomes if o[0] != "ok"]
        self.assertEqual(len(oks), 1, outcomes)
        self.assertEqual(len(errs), 1, outcomes)
        self.assertIsInstance(
            errs[0][1], (CooldownError, PendingWithdrawalExistsError)
        )
        self.assertEqual(self.pending_count(501), 1)
        self.assertEqual(self.request_count(501), 1)
        self.assertEqual(len(self.ledger_rows()), 1)
        # the surviving row used the configured fee, not a bypass
        winner = oks[0][1]
        self.assertEqual(
            self.raw_row(winner.request_id)["wallet_debit_units"],
            CONFIGURED_DEBIT_1_5,
        )
        self.assertEqual(
            self.wallet_state(501),
            (FUND - CONFIGURED_DEBIT_1_5, CONFIGURED_DEBIT_1_5),
        )


# ── 14–15: reject / complete stay settings-free ────────────────────


class TestRejectCompleteSettingsFree(_Base):

    def _create(self) -> object:
        self.add_user(501)
        self.fund(501, FUND)
        return self.create(METHOD_USDT_BEP20, AMOUNT_1_5)

    def _forbid_settings_reads(self):
        return mock.patch.object(
            platform_settings,
            "get_required_setting",
            side_effect=AssertionError("settings were reread"),
        )

    def test_13_reject_does_not_reread_settings(self):
        """13. Reject moves exactly the persisted debit without a
        single settings read."""
        req = self._create()
        with self._forbid_settings_reads():
            rejected = self.svc.reject(req.request_id, now=NOW)
        self.assertEqual(rejected.status, RequestStatus.REJECTED)
        self.assertEqual(
            self.wallet_state(501), (FUND, 0)
        )                                   # full exact release
        ledger = self.ledger_rows(req.request_id)
        self.assertEqual(
            [r["entry_type"] for r in ledger], ["hold", "release"]
        )
        self.assertEqual(
            ledger[1]["amount_units"], CONFIGURED_DEBIT_1_5
        )

    def test_14_complete_does_not_reread_settings(self):
        """14. Complete settles exactly the persisted debit without a
        single settings read."""
        req = self._create()
        with self._forbid_settings_reads():
            completed = self.svc.complete(req.request_id, now=NOW)
        self.assertEqual(completed.status, RequestStatus.COMPLETED)
        self.assertEqual(
            self.wallet_state(501),
            (FUND - CONFIGURED_DEBIT_1_5, 0),
        )
        ledger = self.ledger_rows(req.request_id)
        self.assertEqual(
            [r["entry_type"] for r in ledger], ["hold", "settlement"]
        )
        self.assertEqual(
            ledger[1]["amount_units"], CONFIGURED_DEBIT_1_5
        )

    def test_15_settings_change_after_create_preserves_facts(self):
        """15. Rewriting both settings after creation changes
        NOTHING about the persisted request or the money it later
        releases."""
        req = self._create()
        before = dict(self.raw_row(req.request_id))
        self.set_settings(minimum_units=1, fee_units=999_999_999)
        after = dict(self.raw_row(req.request_id))
        self.assertEqual(before, after)
        # reject still releases the ORIGINAL debit
        self.svc.reject(req.request_id, now=NOW)
        ledger = self.ledger_rows(req.request_id)
        self.assertEqual(
            ledger[1]["amount_units"], CONFIGURED_DEBIT_1_5
        )
        self.assertEqual(self.wallet_state(501), (FUND, 0))


# ── 16–17: settings contract ───────────────────────────────────────


class TestSettingsContract(_Base):

    def test_16_missing_setting_follows_platform_settings_contract(self):
        """16. A missing required key raises the EXISTING
        SettingNotFoundError (never a fallback, never a domain
        substitute) and writes nothing — for each of the two keys."""
        self.add_user(501)
        self.fund(501, FUND)

        self.clear_setting(platform_settings.WITHDRAWAL_FEE_UNITS)
        with self.assertRaises(SettingNotFoundError) as ctx:
            self.create(METHOD_USDT_BEP20, AMOUNT_1_5)
        self.assertIsInstance(ctx.exception, PlatformSettingError)
        self.assertNotIsInstance(ctx.exception, WithdrawalError)
        self.assert_no_create_state()

        self.set_settings(minimum_units=MIN_UNITS, fee_units=FEE_UNITS)
        self.clear_setting(platform_settings.MINIMUM_WITHDRAWAL_UNITS)
        with self.assertRaises(SettingNotFoundError) as ctx:
            self.create(METHOD_USDT_BEP20, AMOUNT_1_5)
        self.assertNotIsInstance(ctx.exception, WithdrawalError)
        self.assert_no_create_state()

    def test_17_invalid_setting_values_follow_validation_contract(self):
        """17. Zero is a valid configured value; negative, float,
        bool and malformed inputs are rejected by the EXISTING
        setting validation contract before anything is stored."""
        self.add_user(501)
        self.fund(501, FUND)

        for bad in (-1, 1.5, True, "-5", "abc"):
            with self.assertRaises(
                SettingValidationError, msg=repr(bad)
            ):
                platform_settings.set_setting(
                    platform_settings.WITHDRAWAL_FEE_UNITS,
                    bad,
                    admin_user_id=ADMIN_ID,
                    db_path=self.db_path,
                )
            with self.assertRaises(
                SettingValidationError, msg=repr(bad)
            ):
                platform_settings.set_setting(
                    platform_settings.MINIMUM_WITHDRAWAL_UNITS,
                    bad,
                    admin_user_id=ADMIN_ID,
                    db_path=self.db_path,
                )
        # nothing invalid was persisted
        self.assertEqual(
            platform_settings.get_setting(
                platform_settings.WITHDRAWAL_FEE_UNITS,
                db_path=self.db_path,
            ),
            FEE_UNITS,
        )

        # zero fee and zero minimum are legal and used exactly: an
        # amount below the default 1.5-USDT minimum is accepted, the
        # fee contributes nothing, and the debit is the bare amount
        self.set_settings(minimum_units=0, fee_units=0)
        small = Decimal("0.01")               # below the 1.5 default
        req = self.create(METHOD_USDT_BEP20, small)
        row = self.raw_row(req.request_id)
        self.assertEqual(row["amount_native_minor"], 1_000_000)
        self.assertEqual(row["fee_native_minor"], 0)
        self.assertEqual(row["wallet_debit_units"], 1_000_000)
        self.assertEqual(
            self.wallet_state(501), (FUND - 1_000_000, 1_000_000)
        )


# ── 18–20: MT-ADMIN-23 regressions ─────────────────────────────────


class TestMtAdmin23BehaviorPreserved(_Base):

    def test_18_vodafone_contract_unchanged(self):
        """18. Vodafone Cash keeps the approved EGP contract even
        under adversarial USDT-denominated settings: 10 EGP + 1 EGP
        convert ONCE to the same wallet debit.  (The setting's units
        conflict with the EGP fee model, so they are not applied
        there — reported with MT-ADMIN-24 instead.)"""
        self.add_user(501)
        self.fund(501, FUND)
        # 50 USDT minimum + a 9.99999999 USDT fee would dominate every
        # USDT create — the EGP path must ignore both entirely
        self.set_settings(
            minimum_units=5_000_000_000,
            fee_units=999_999_999,
        )
        req = self.create(METHOD_VODAFONE_CASH, Decimal("10.00"))
        row = self.raw_row(req.request_id)
        self.assertEqual(row["native_unit"], "EGP")
        self.assertEqual(row["amount_native_minor"], 1000)
        self.assertEqual(row["fee_native_minor"], 100)
        self.assertEqual(row["fee_egp_minor"], 100)
        self.assertIsNone(row["rate_usdt_egp"])
        self.assertEqual(row["wallet_debit_units"], VODAFONE_DEBIT)
        self.assertEqual(
            self.wallet_state(501),
            (FUND - VODAFONE_DEBIT, VODAFONE_DEBIT),
        )

    def test_19_usdt_no_round_trip_behavior_preserved(self):
        """19. The USDT debit stays a direct integer sum — the
        configured fee enters WITHOUT any EGP conversion, so the
        result can never equal a round trip through display EGP."""
        self.add_user(501)
        self.fund(501, FUND)
        req = self.create(METHOD_USDT_BEP20, AMOUNT_1_5)
        self.assertEqual(
            req.wallet_debit_units,
            AMOUNT_1_5_UNITS + FEE_UNITS,
        )
        # NOT the rate-derived fee, and NOT a round-tripped value
        self.assertNotEqual(
            req.wallet_debit_units,
            AMOUNT_1_5_UNITS + RATE_DERIVED_FEE_485,
        )
        row = self.raw_row(req.request_id)
        self.assertEqual(row["amount_egp_minor"], 7275)  # display only
        # structural: the debit path uses the direct-sum helper only
        src = inspect.getsource(withdrawal_service)
        self.assertIn("usdt_wallet_debit", src)
        self.assertNotIn("egp_to_wallet_units", src)
        self.assertNotIn("usdt_units_to_egp_display", src)

    def test_20_legacy_rows_behave_exactly_as_mt_admin_23(self):
        """20. A legacy row with NULL wallet_debit_units is still
        refused by reject/complete BEFORE any mutation — settings
        configured or not, the amount is never guessed."""
        self.add_user(501)
        self.fund(501, FUND)
        self.insert_legacy_null_debit("wd-legacy", user_id=501)
        for op in (
            lambda: self.svc.reject("wd-legacy", now=NOW),
            lambda: self.svc.complete("wd-legacy", now=NOW),
        ):
            with self.assertRaises(MissingWalletDebitError):
                op()
            row = self.raw_row("wd-legacy")
            self.assertEqual(row["status"], "pending")
            self.assertIsNone(row["rejected_at"])
            self.assertIsNone(row["completed_at"])
        self.assertEqual(self.wallet_state(501), (FUND, 0))
        self.assertEqual(self.ledger_rows("wd-legacy"), [])


if __name__ == "__main__":
    unittest.main()
