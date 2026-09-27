"""
Focused tests — SQLite Withdrawal Repository + Financial Adapters
(MT-ADMIN-22)
=================================================================

Persistence/adapters ONLY: this suite pins the production SQLite
repository and the wallet/ledger/payment-method adapter boundaries the
FUTURE atomic withdrawal service will compose inside ONE
``db.transaction()``.  No create/reject/complete orchestration exists
here, and none is tested.

  REPOSITORY
   1. insert + read round trip with every field populated
   2. exact INTEGER wallet_debit_units (typeof == 'integer')
   3. exact user_destination (stored as supplied, user column)
   4. rate fields preserved exactly (rate_quote serialization)
   5. payment-method snapshots preserved exactly
   6. timestamps preserved exactly (never regenerated)
   7. legacy NULL fields read back as None (never zeroed/repaired)
   8. not-found behavior -> domain RequestNotFoundError
   9. pending list/query (list_pending / latest_for)
  10. duplicate request_id behavior (deterministic domain errors)

  CONNECTION
  11. repository insert uses the caller transaction
  12. rollback removes the insert
  13. commit persists the insert
  14. no hidden connection is opened when connection= is supplied
  15. the caller connection is never committed/rolled back/closed

  CAS
  16. pending -> rejected succeeds (rejected_at stamped)
  17. pending -> completed succeeds (completed_at stamped)
  18. a second transition fails deterministically
  19. an invalid target status fails before touching the row
  20. not-found is deterministic for transitions too

  WALLET ADAPTER
  21. the caller connection is forwarded (identity + behavior)
  22. wallet exception types are preserved (not translated)
  23. no independent commit ever happens

  LEDGER ADAPTER
  24. hold/release/settlement carry reference_type='withdrawal'
  25. idempotency keys remain intact (replay returns the same entry)
  26. the caller connection is preserved
  27. no duplicated ledger accounting logic (source + behavior)

  PAYMENT METHOD
  28. an active method resolves
  29. an inactive method is rejected
  30. a missing method is rejected

  ERRORS
  31. repository/domain errors surface via the MT-ADMIN-21 boundary
  32. unexpected exceptions are never swallowed (identity preserved)

  FINANCIAL SAFETY
  33. no float anywhere in the module (AST scan)
  34. no REAL column / no REAL in source (schema + AST)
  35. no currency conversion inside repository/adapters
  36. wallet debit remains exact USDT integer units

Semantics pinned here:
- ``wallet_debit_units`` is USDT atomic units (1 USDT = 100,000,000),
  stored VERBATIM — never computed from the EGP/rate columns.
- ``user_destination`` is the USER's payout destination, never
  ``payment_methods.destination`` / ``pm_destination``.
- Legacy NULLs stay ``None``; the repository never synthesizes values.
- The repository moves ONLY rows of ``withdrawal_requests``; money
  movement belongs to the adapters in the caller's transaction.

Temp databases only; no production destinations or balances are used.

Run:
    python -m pytest test_withdrawal_store.py -v
"""

from __future__ import annotations

import ast
import inspect
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock

import db
import payment_method_store as pms
import wallet
import withdrawal_rules
import withdrawal_store
from ledger import (
    LedgerIdempotencyConflictError,
    LedgerReferenceConflictError,
    LedgerService,
)
from withdrawal_contract import translated_errors
from withdrawal_rules import DuplicateRequestError, InvalidStateError, \
    PendingWithdrawalExistsError, PaymentMethodUnavailableError, \
    RequestNotFoundError, RequestStatus, ValidationError, WithdrawalRequest
from withdrawal_store import (
    SqliteLedgerAdapter,
    SqliteWalletAdapter,
    SqliteWithdrawalRepository,
    resolve_active_payment_method,
)

# Exact reference values (rate = 48.5 EGP per USDT).
RATE_TEXT = "48.500000"
RATE_DECI = Decimal("48.5")
CAPTURED_TEXT = "2026-09-27T09:59:00+00:00"
CREATED_AT = datetime(2026, 9, 27, 10, 0, 0)
USDT_SCALE = 100_000_000


def _make_request(request_id: str, user_id: int = 501, **overrides) \
        -> WithdrawalRequest:
    """A fully populated, schema-valid withdrawal request."""
    fields = dict(
        request_id=request_id,
        user_id=user_id,
        method="vodafone_cash",
        amount_egp=Decimal("10.00"),
        fee_egp=Decimal("1.00"),
        rate_usdt_egp=RATE_DECI,
        amount_native=Decimal("10.00"),
        fee_native=Decimal("1.00"),
        status=RequestStatus.PENDING,
        created_at=CREATED_AT,
        wallet_debit_units=22_680_413,
        user_destination="TEST-USER-DEST-1",
        rejected_at=None,
        completed_at=None,
        native_unit="EGP",
        payment_method_id=None,
        pm_display_name="TEST-PM-DISPLAY",
        pm_category="cash",
        pm_asset="EGP",
        pm_network=None,
        pm_provider="TEST-PROV",
        pm_destination="TEST-PLATFORM-DEST-1",
        wallet_rate_usdt_egp=RATE_TEXT,
        rate_captured_at=CAPTURED_TEXT,
        rate_provider="manual",
    )
    fields.update(overrides)
    return WithdrawalRequest(**fields)


class _Base(unittest.TestCase):
    """Temp DB fixture: db.DB_PATH redirected, init_db run, cleanup on exit."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)
        self.repo = SqliteWithdrawalRepository(self.db_path)
        self.addCleanup(self._restore)

    def _restore(self):
        db.DB_PATH = self._original_db_path
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    # ── seed helpers ───────────────────────────────────────────

    def add_user(self, user_id: int) -> int:
        self.assertTrue(
            db.register_user(user_id, f"u{user_id}", f"U{user_id}")
        )
        return user_id

    def raw_row(self, request_id: str) -> sqlite3.Row:
        with db.get_connection(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM withdrawal_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        self.assertIsNotNone(row, f"row {request_id!r} missing")
        return row

    def insert(self, request: WithdrawalRequest, connection=None) -> None:
        self.repo.insert(request, connection=connection)

    def create_method(self, *, active: bool = True) -> pms.PaymentMethod:
        method = pms.create_payment_method(
            category="cash",
            display_name="TEST-METHOD",
            asset="EGP",
            provider="TEST-PROVIDER",
            destination="TEST-PLATFORM-DEST-9",
            created_by=1,
            db_path=self.db_path,
        )
        if not active:
            pms.set_payment_method_active(
                method.id, False, updated_by=1, db_path=self.db_path
            )
        return method


# ── 1–10: repository round trip and reads ────────────────────────────


class TestRepositoryRoundTrip(_Base):
    def test_01_insert_read_round_trip_every_field(self):
        """1. Every populated field survives insert -> get exactly."""
        self.add_user(501)
        method = self.create_method()
        req = _make_request("wd-1", payment_method_id=method.id)
        self.insert(req)
        got = self.repo.get("wd-1")

        self.assertEqual(got.request_id, "wd-1")
        self.assertEqual(got.user_id, 501)
        self.assertEqual(got.method, "vodafone_cash")
        self.assertEqual(got.amount_egp, Decimal("10.00"))
        self.assertEqual(got.fee_egp, Decimal("1.00"))
        self.assertEqual(got.rate_usdt_egp, RATE_DECI)
        self.assertEqual(got.amount_native, Decimal("10.00"))
        self.assertEqual(got.fee_native, Decimal("1.00"))
        self.assertEqual(got.status, RequestStatus.PENDING)
        self.assertEqual(got.created_at, CREATED_AT)
        self.assertEqual(got.wallet_debit_units, 22_680_413)
        self.assertEqual(got.user_destination, "TEST-USER-DEST-1")
        self.assertIsNone(got.rejected_at)
        self.assertIsNone(got.completed_at)
        self.assertEqual(got.native_unit, "EGP")
        self.assertEqual(got.payment_method_id, method.id)
        self.assertEqual(got.pm_display_name, "TEST-PM-DISPLAY")
        self.assertEqual(got.pm_category, "cash")
        self.assertEqual(got.pm_asset, "EGP")
        self.assertIsNone(got.pm_network)
        self.assertEqual(got.pm_provider, "TEST-PROV")
        self.assertEqual(got.pm_destination, "TEST-PLATFORM-DEST-1")
        self.assertEqual(got.wallet_rate_usdt_egp, RATE_TEXT)
        self.assertEqual(got.rate_captured_at, CAPTURED_TEXT)
        self.assertEqual(got.rate_provider, "manual")

    def test_01b_standalone_insert_commits(self):
        """1b. Without connection=, the standalone scope commits."""
        self.add_user(501)
        self.insert(_make_request("wd-standalone"))
        with db.get_connection(self.db_path) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM withdrawal_requests "
                "WHERE request_id = 'wd-standalone'"
            ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_02_wallet_debit_is_exact_integer(self):
        """2. wallet_debit_units stores the exact INTEGER value."""
        self.add_user(501)
        big = 9_007_199_254_740_991  # large exact int within INT64
        self.insert(_make_request("wd-int", wallet_debit_units=big))
        row = self.raw_row("wd-int")
        self.assertEqual(row["wallet_debit_units"], big)
        self.assertIs(type(row["wallet_debit_units"]), int)
        # typeof() proves SQLite stored it in the INTEGER domain.
        with db.get_connection(self.db_path) as conn:
            kind = conn.execute(
                "SELECT typeof(wallet_debit_units) FROM withdrawal_requests "
                "WHERE request_id = 'wd-int'"
            ).fetchone()[0]
        self.assertEqual(kind, "integer")
        got = self.repo.get("wd-int")
        self.assertIs(type(got.wallet_debit_units), int)
        self.assertEqual(got.wallet_debit_units, big)

    def test_03_user_destination_exact_and_distinct(self):
        """3. user_destination is stored as supplied in ITS column —
        never copied into the platform's pm_destination."""
        self.add_user(501)
        dest = "+20 100 000 0000  (TEST)"
        self.insert(_make_request(
            "wd-dest", user_destination=dest,
            pm_destination="TEST-PLATFORM-DEST-1",
        ))
        row = self.raw_row("wd-dest")
        self.assertEqual(row["user_destination"], dest)
        self.assertEqual(row["pm_destination"], "TEST-PLATFORM-DEST-1")
        got = self.repo.get("wd-dest")
        self.assertEqual(got.user_destination, dest)
        self.assertEqual(got.pm_destination, "TEST-PLATFORM-DEST-1")

    def test_04_rate_fields_preserved_exactly(self):
        """4. Rate facts persist exactly per RateQuote serialization."""
        self.add_user(501)
        self.insert(_make_request("wd-rate"))
        row = self.raw_row("wd-rate")
        self.assertEqual(row["wallet_rate_usdt_egp"], RATE_TEXT)
        self.assertEqual(row["rate_captured_at"], CAPTURED_TEXT)
        self.assertEqual(row["rate_provider"], "manual")
        # canonical_rate_text(RATE_DECI) -> '48.5' (trailing zeros stripped)
        self.assertEqual(row["rate_usdt_egp"], "48.5")
        got = self.repo.get("wd-rate")
        self.assertEqual(got.wallet_rate_usdt_egp, RATE_TEXT)
        self.assertEqual(got.rate_captured_at, CAPTURED_TEXT)
        self.assertEqual(got.rate_provider, "manual")
        self.assertEqual(got.rate_usdt_egp, RATE_DECI)

    def test_04b_vodafone_rate_null_stays_null(self):
        """4b. A NULL rate_usdt_egp is preserved, never invented."""
        self.add_user(501)
        self.insert(_make_request(
            "wd-norate", rate_usdt_egp=None,
            amount_native=Decimal("10.00"), fee_native=Decimal("1.00"),
        ))
        row = self.raw_row("wd-norate")
        self.assertIsNone(row["rate_usdt_egp"])
        self.assertIsNone(self.repo.get("wd-norate").rate_usdt_egp)

    def test_05_payment_method_snapshots_preserved(self):
        """5. payment_method_id + all pm_* snapshots round trip."""
        self.add_user(501)
        method = self.create_method()
        # a future service snapshots the method's stored fields at
        # request time; the repository must persist them verbatim
        self.insert(_make_request(
            "wd-pm",
            payment_method_id=method.id,
            pm_display_name=method.display_name,
            pm_category=method.category,
            pm_asset=method.asset,
            pm_network=method.network,
            pm_provider=method.provider,
            pm_destination=method.destination,
        ))
        row = self.raw_row("wd-pm")
        self.assertEqual(row["payment_method_id"], method.id)
        self.assertEqual(row["pm_display_name"], "TEST-METHOD")
        self.assertEqual(row["pm_category"], "cash")
        self.assertEqual(row["pm_asset"], "EGP")
        self.assertEqual(row["pm_provider"], "TEST-PROVIDER")
        self.assertEqual(row["pm_destination"], "TEST-PLATFORM-DEST-9")
        got = self.repo.get("wd-pm")
        self.assertEqual(got.payment_method_id, method.id)
        self.assertEqual(got.pm_display_name, "TEST-METHOD")
        self.assertEqual(got.pm_destination, "TEST-PLATFORM-DEST-9")

    def test_06_timestamps_preserved(self):
        """6. created_at / transition stamps are stored exactly as given."""
        self.add_user(501)
        self.add_user(502)
        aware = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.insert(_make_request(
            "wd-ts-a", created_at=CREATED_AT,
            rate_captured_at="2026-09-27T09:59:00+00:00",
        ))
        self.insert(_make_request(
            "wd-ts-b", user_id=502, created_at=aware,
            rate_captured_at="2026-09-27T09:59:00+00:00",
        ))
        self.assertEqual(
            self.raw_row("wd-ts-a")["created_at"], "2026-09-27 10:00:00"
        )
        # aware input renders as UTC wall-clock text
        self.assertEqual(
            self.raw_row("wd-ts-b")["created_at"], "2026-09-27 12:00:00"
        )
        self.assertEqual(self.repo.get("wd-ts-a").created_at, CREATED_AT)
        # rate_captured_at kept byte-for-byte (str passes through)
        self.assertEqual(
            self.raw_row("wd-ts-a")["rate_captured_at"],
            "2026-09-27T09:59:00+00:00",
        )
        # transition stamps are preserved exactly
        at = "2026-09-27 15:30:00"
        out = self.repo.transition(
            "wd-ts-a", to_status="rejected", at=at
        )
        self.assertEqual(out.rejected_at, datetime(2026, 9, 27, 15, 30, 0))
        self.assertEqual(
            self.raw_row("wd-ts-a")["rejected_at"], "2026-09-27 15:30:00"
        )

    def test_07_legacy_null_fields_read_as_none(self):
        """7. Legacy rows (NULL debit / destination) read back as None —
        never zero, never synthesized, never repaired."""
        self.add_user(501)
        with db.get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO withdrawal_requests (
                       request_id, user_id, method, amount_egp_minor,
                       fee_egp_minor, amount_native_minor,
                       fee_native_minor, native_unit, rate_usdt_egp,
                       wallet_rate_usdt_egp, rate_captured_at,
                       rate_provider, status, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("wd-legacy", 501, "vodafone_cash", 1000, 100, 1000, 100,
                 "EGP", None, RATE_TEXT, CAPTURED_TEXT, "manual",
                 "pending", "2026-01-01 00:00:00"),
            )
        row = self.raw_row("wd-legacy")
        self.assertIsNone(row["wallet_debit_units"])
        self.assertIsNone(row["user_destination"])
        got = self.repo.get("wd-legacy")
        self.assertIsNone(got.wallet_debit_units)
        self.assertIsNone(got.user_destination)
        self.assertIsNot(got.wallet_debit_units, 0)
        self.assertNotEqual(got.wallet_debit_units, 0)
        self.assertIsNone(got.payment_method_id)
        self.assertIsNone(got.rejected_at)
        self.assertIsNone(got.completed_at)
        # exposed through the query APIs too
        pending = [r for r in self.repo.list_pending()
                   if r.request_id == "wd-legacy"]
        self.assertIsNone(pending[0].wallet_debit_units)
        # a transition does NOT repair or rewrite legacy NULL facts
        self.repo.transition(
            "wd-legacy", to_status="rejected",
            at=datetime(2026, 9, 27, 16, 0, 0),
        )
        row2 = self.raw_row("wd-legacy")
        self.assertIsNone(row2["wallet_debit_units"])
        self.assertIsNone(row2["user_destination"])
        self.assertEqual(row2["status"], "rejected")
        self.assertEqual(
            row2["rejected_at"], "2026-09-27 16:00:00"
        )

    def test_08_get_not_found_is_domain_error(self):
        """8. Unknown id -> domain RequestNotFoundError."""
        self.add_user(501)
        with self.assertRaises(RequestNotFoundError) as ctx:
            self.repo.get("wd-missing")
        self.assertIsInstance(ctx.exception, withdrawal_rules.WithdrawalError)
        # deterministic: same type both times
        with self.assertRaises(RequestNotFoundError):
            self.repo.get("wd-missing")

    def test_09_pending_list_and_latest_for(self):
        """9. list_pending filters/orders; latest_for counts every status."""
        self.add_user(501)
        self.add_user(502)
        self.insert(_make_request(
            "wd-p1", 501, created_at=datetime(2026, 9, 27, 9, 0, 0)
        ))
        self.insert(_make_request(
            "wd-p2", 502, created_at=datetime(2026, 9, 27, 8, 0, 0)
        ))
        # a rejected row for 501, newest created_at of that user's set
        self.insert(_make_request(
            "wd-r1", 501, created_at=datetime(2026, 9, 26, 7, 0, 0),
            status=RequestStatus.REJECTED,
        ))
        all_pending = [r.request_id for r in self.repo.list_pending()]
        self.assertEqual(all_pending, ["wd-p1", "wd-p2"])  # newest first
        only_501 = [r.request_id
                    for r in self.repo.list_pending(user_id=501)]
        self.assertEqual(only_501, ["wd-p1"])
        # latest_for: newest created_at regardless of status
        latest = self.repo.latest_for(501)
        self.assertEqual(latest.request_id, "wd-p1")
        self.assertEqual(latest.status, RequestStatus.PENDING)
        # after transition, the rejected row is still discoverable
        self.repo.transition(
            "wd-p1", to_status="rejected",
            at=datetime(2026, 9, 27, 18, 0, 0),
        )
        latest = self.repo.latest_for(501)
        self.assertEqual(latest.request_id, "wd-p1")
        self.assertEqual(latest.status, RequestStatus.REJECTED)
        self.assertEqual(
            [r.request_id for r in self.repo.list_pending()],
            ["wd-p2"],
        )
        self.assertIsNone(self.repo.latest_for(424242))

    def test_10_duplicate_request_id_is_deterministic(self):
        """10. Duplicate PK -> domain DuplicateRequestError; a second
        pending row for one user -> PendingWithdrawalExistsError.
        Both deterministic domain errors, never raw IntegrityError."""
        self.add_user(501)
        self.add_user(502)
        req = _make_request("wd-dup")
        self.insert(req)
        # pure primary-key duplicate (different user: only PK violated)
        with self.assertRaises(DuplicateRequestError):
            self.insert(_make_request("wd-dup", 502))
        # identical re-insert (same user, still pending): the partial
        # one-pending index fires first -> mapped by the boundary
        with self.assertRaises(PendingWithdrawalExistsError):
            self.insert(_make_request("wd-dup", 501))
        # non-pending duplicate for the same user hits the PK alone
        with self.assertRaises(DuplicateRequestError):
            self.insert(_make_request(
                "wd-dup", 501, status=RequestStatus.REJECTED
            ))
        # exactly one row exists
        with db.get_connection(self.db_path) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM withdrawal_requests "
                "WHERE request_id = 'wd-dup'"
            ).fetchone()[0]
        self.assertEqual(n, 1)


# ── 11–15: transaction/connection behavior ───────────────────────────


class TestConnectionOwnership(_Base):
    def test_11_insert_participates_in_caller_transaction(self):
        """11. With connection=, the insert runs on THAT transaction —
        invisible to other connections until the caller commits."""
        self.add_user(501)
        with db.transaction(self.db_path) as conn:
            self.insert(_make_request("wd-tx"), connection=conn)
            # same connection sees it immediately
            got = self.repo.get("wd-tx", connection=conn)
            self.assertEqual(got.request_id, "wd-tx")
            # a second connection does NOT see uncommitted work
            other = sqlite3.connect(self.db_path)
            try:
                n = other.execute(
                    "SELECT COUNT(*) FROM withdrawal_requests "
                    "WHERE request_id = 'wd-tx'"
                ).fetchone()[0]
            finally:
                other.close()
            self.assertEqual(n, 0)
        # committed with the transaction
        self.assertEqual(self.repo.get("wd-tx").request_id, "wd-tx")

    def test_12_rollback_removes_insert(self):
        """12. Caller rollback discards the repository insert."""
        self.add_user(501)
        with self.assertRaises(RuntimeError):
            with db.transaction(self.db_path) as conn:
                self.insert(_make_request("wd-rb"), connection=conn)
                raise RuntimeError("rollback sentinel")
        with self.assertRaises(RequestNotFoundError):
            self.repo.get("wd-rb")

    def test_13_commit_persists_insert(self):
        """13. Caller commit persists the repository insert."""
        self.add_user(501)
        with db.transaction(self.db_path) as conn:
            self.insert(_make_request("wd-cm"), connection=conn)
        self.assertEqual(self.repo.get("wd-cm").request_id, "wd-cm")

    def test_14_no_hidden_connection_when_supplied(self):
        """14. With connection= supplied, db.get_connection is NEVER
        called — by the repository or by either adapter."""
        self.add_user(501)
        wallet.credit_units(501, 1_000_000_000)
        method = self.create_method()
        calls: list = []

        def _boom(*_args, **_kwargs):
            calls.append(1)
            raise AssertionError("hidden connection opened")

        wallet_adapter = SqliteWalletAdapter()
        ledger_adapter = SqliteLedgerAdapter()
        with db.get_connection(self.db_path) as conn:
            with mock.patch.object(db, "get_connection", side_effect=_boom):
                # repository methods
                self.insert(_make_request(
                    "wd-hc", payment_method_id=method.id
                ), connection=conn)
                self.repo.get("wd-hc", connection=conn)
                self.repo.transition(
                    "wd-hc", to_status="rejected",
                    at=datetime(2026, 9, 27, 14, 0, 0), connection=conn,
                )
                self.repo.list_pending(connection=conn)
                self.repo.latest_for(501, connection=conn)
                # wallet adapter
                wallet_adapter.reserve(501, 100_000_000, connection=conn)
                wallet_adapter.release_units(
                    501, 100_000_000, connection=conn
                )
                # ledger adapter
                ledger_adapter.hold(
                    501, 100_000_000, request_id="wd-hc",
                    connection=conn,
                )
        self.assertEqual(calls, [])

    def test_15_caller_connection_never_committed_rolled_back_closed(self):
        """15. The repository borrows the caller connection: no commit,
        no rollback, no close — even when a call raises."""
        self.add_user(501)
        with db.get_connection(self.db_path) as conn:
            self.insert(_make_request("wd-borrow"), connection=conn)
            # not committed: still inside the implicit transaction
            self.assertTrue(conn.in_transaction)
            # not rolled back: the row is still there
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM withdrawal_requests "
                "WHERE request_id = 'wd-borrow'"
            ).fetchone())
            # a raising call must not roll back or close our connection
            with self.assertRaises(RequestNotFoundError):
                self.repo.get("wd-missing", connection=conn)
            self.assertTrue(conn.in_transaction)
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM withdrawal_requests "
                "WHERE request_id = 'wd-borrow'"
            ).fetchone())
            # transition also stays inside our transaction
            self.repo.transition(
                "wd-borrow", to_status="rejected",
                at=datetime(2026, 9, 27, 14, 0, 0), connection=conn,
            )
            self.assertTrue(conn.in_transaction)
            # the connection is still usable
            self.assertEqual(
                conn.execute("SELECT 1").fetchone()[0], 1
            )
            # our own rollback discards BOTH writes — proving the
            # repository never committed anything behind our back
            conn.rollback()
        with self.assertRaises(RequestNotFoundError):
            self.repo.get("wd-borrow")


# ── 16–20: guarded CAS status transitions ────────────────────────────


class TestStatusCas(_Base):
    def setUp(self):
        super().setUp()
        self.add_user(501)
        self.insert(_make_request("wd-cas"))

    def test_16_pending_to_rejected_succeeds(self):
        """16. pending -> rejected stamps rejected_at only."""
        at = datetime(2026, 9, 27, 12, 0, 0)
        out = self.repo.transition(
            "wd-cas", to_status="rejected", at=at
        )
        self.assertEqual(out.status, RequestStatus.REJECTED)
        self.assertEqual(out.rejected_at, at)
        self.assertIsNone(out.completed_at)
        # financial facts untouched by the status move
        self.assertEqual(out.wallet_debit_units, 22_680_413)
        row = self.raw_row("wd-cas")
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["rejected_at"], "2026-09-27 12:00:00")
        self.assertIsNone(row["completed_at"])

    def test_17_pending_to_completed_succeeds(self):
        """17. pending -> completed stamps completed_at only."""
        at = datetime(2026, 9, 27, 12, 30, 0)
        out = self.repo.transition(
            "wd-cas", to_status="completed", at=at
        )
        self.assertEqual(out.status, RequestStatus.COMPLETED)
        self.assertEqual(out.completed_at, at)
        self.assertIsNone(out.rejected_at)
        row = self.raw_row("wd-cas")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["completed_at"], "2026-09-27 12:30:00")
        self.assertIsNone(row["rejected_at"])

    def test_18_second_transition_fails(self):
        """18. Once terminal, every further transition fails: the row
        is not modified (rejected->completed, completed->rejected,
        rejected->rejected, completed->completed)."""
        self.repo.transition(
            "wd-cas", to_status="rejected",
            at=datetime(2026, 9, 27, 12, 0, 0),
        )
        for bad in ("completed", "rejected"):
            with self.assertRaises(InvalidStateError):
                self.repo.transition(
                    "wd-cas", to_status=bad,
                    at=datetime(2026, 9, 27, 13, 0, 0),
                )
        # still exactly what the first transition produced
        row = self.raw_row("wd-cas")
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["rejected_at"], "2026-09-27 12:00:00")
        self.assertIsNone(row["completed_at"])

        # completed -> rejected / completed -> completed also fail
        self.add_user(502)
        self.insert(_make_request("wd-cas2", 502))
        self.repo.transition(
            "wd-cas2", to_status="completed",
            at=datetime(2026, 9, 27, 12, 0, 0),
        )
        for bad in ("rejected", "completed"):
            with self.assertRaises(InvalidStateError):
                self.repo.transition(
                    "wd-cas2", to_status=bad,
                    at=datetime(2026, 9, 27, 13, 0, 0),
                )
        row = self.raw_row("wd-cas2")
        self.assertEqual(row["status"], "completed")

    def test_19_invalid_target_status_fails_untouched(self):
        """19. Unknown target status is rejected before any SQL runs."""
        for bad in ("pending", "PENDING", "", "bogus"):
            with self.assertRaises(ValidationError):
                self.repo.transition(
                    "wd-cas", to_status=bad,
                    at=datetime(2026, 9, 27, 13, 0, 0),
                )
        row = self.raw_row("wd-cas")
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["rejected_at"])
        self.assertIsNone(row["completed_at"])

    def test_20_transition_not_found_deterministic(self):
        """20. Transitioning a missing id is deterministically a
        RequestNotFoundError (distinct from the invalid-state error)."""
        for _ in range(2):
            with self.assertRaises(RequestNotFoundError):
                self.repo.transition(
                    "wd-nope", to_status="rejected",
                    at=datetime(2026, 9, 27, 13, 0, 0),
                )
        # the existing row is untouched
        self.assertEqual(self.raw_row("wd-cas")["status"], "pending")


# ── 21–23: wallet adapter ────────────────────────────────────────────


class TestWalletAdapter(_Base):
    def setUp(self):
        super().setUp()
        self.add_user(501)
        wallet.credit_units(501, 1_000_000_000)  # 10 USDT
        self.adapter = SqliteWalletAdapter()

    def test_21_forwards_caller_connection(self):
        """21. The exact connection object reaches the wallet layer."""
        with db.transaction(self.db_path) as conn:
            with mock.patch.object(
                wallet, "reserve", wraps=wallet.reserve
            ) as spy_reserve, \
                    mock.patch.object(
                        wallet, "release_units",
                        wraps=wallet.release_units,
                    ) as spy_release, \
                    mock.patch.object(
                        wallet, "settle_units",
                        wraps=wallet.settle_units,
                    ) as spy_settle:
                self.adapter.reserve(501, 100_000_000, connection=conn)
                self.adapter.release_units(
                    501, 100_000_000, connection=conn
                )
                self.adapter.reserve(501, 100_000_000, connection=conn)
                self.adapter.settle_units(
                    501, 100_000_000, connection=conn
                )
            self.assertIs(
                spy_reserve.call_args.kwargs["connection"], conn
            )
            self.assertIs(
                spy_release.call_args.kwargs["connection"], conn
            )
            self.assertIs(
                spy_settle.call_args.kwargs["connection"], conn
            )
        # reserve+release is value-neutral; the final reserve+settle
        # permanently consumed 0.1 USDT from held
        self.assertEqual(
            tuple(wallet.wallet_units(501)),
            (900_000_000, 0),
        )

    def test_21b_wallet_join_is_observable_and_rolled_back(self):
        """21b. Behavioral proof: the mutation joins the caller's open
        transaction and vanishes with its rollback."""
        with self.assertRaises(RuntimeError):
            with db.transaction(self.db_path) as conn:
                self.adapter.reserve(501, 150_000_000, connection=conn)
                row = conn.execute(
                    "SELECT available_units, held_units "
                    "FROM wallets WHERE user_id = 501"
                ).fetchone()
                self.assertEqual(
                    (row[0], row[1]), (850_000_000, 150_000_000)
                )
                raise RuntimeError("rollback sentinel")
        self.assertEqual(
            tuple(wallet.wallet_units(501)), (1_000_000_000, 0)
        )

    def test_22_wallet_error_types_preserved(self):
        """22. Wallet exceptions pass through with their original
        types — the adapter never re-labels them."""
        with db.get_connection(self.db_path) as conn:
            with self.assertRaises(
                wallet.InsufficientBalanceError
            ) as ctx:
                self.adapter.reserve(
                    501, 99_000_000_000, connection=conn
                )
            self.assertIs(type(ctx.exception),
                          wallet.InsufficientBalanceError)
            self.assertNotIsInstance(
                ctx.exception, withdrawal_rules.WithdrawalError
            )
        # held-balance error on release
        with db.get_connection(self.db_path) as conn:
            with self.assertRaises(
                wallet.InsufficientHeldBalanceError
            ) as ctx:
                self.adapter.release_units(
                    501, 100_000_000, connection=conn
                )
            self.assertIs(type(ctx.exception),
                          wallet.InsufficientHeldBalanceError)
        # invalid amount errors keep the wallet type too
        for bad in (0, -1, True, 1.5):  # noqa: E501
            with db.get_connection(self.db_path) as conn:
                with self.assertRaises(
                    wallet.InvalidWalletAmountError
                ):
                    self.adapter.release_units(
                        501, bad, connection=conn
                    )
        # unknown user stays a wallet error
        with db.get_connection(self.db_path) as conn:
            with self.assertRaises(wallet.UserNotFoundError):
                self.adapter.reserve(
                    42424242, 100_000_000, connection=conn
                )

    def test_23_no_independent_commit(self):
        """23. The adapter never commits its own work: the caller's
        transaction is still open afterwards and its rollback wins."""
        with db.transaction(self.db_path) as conn:
            self.adapter.reserve(501, 250_000_000, connection=conn)
            self.assertTrue(conn.in_transaction)
            # nothing visible outside yet
            other = sqlite3.connect(self.db_path)
            try:
                row = other.execute(
                    "SELECT available_units, held_units "
                    "FROM wallets WHERE user_id = 501"
                ).fetchone()
            finally:
                other.close()
            self.assertEqual(tuple(row), (1_000_000_000, 0))
        # committing the caller transaction persists it
        self.assertEqual(
            tuple(wallet.wallet_units(501)),
            (750_000_000, 250_000_000),
        )
        # and a rolled-back caller transaction discards it
        with self.assertRaises(RuntimeError):
            with db.transaction(self.db_path) as conn:
                self.adapter.release_units(
                    501, 250_000_000, connection=conn
                )
                raise RuntimeError("rollback sentinel")
        self.assertEqual(
            tuple(wallet.wallet_units(501)),
            (750_000_000, 250_000_000),
        )


# ── 24–27: ledger adapter ────────────────────────────────────────────


class TestLedgerAdapter(_Base):
    def setUp(self):
        super().setUp()
        self.add_user(501)
        self.adapter = SqliteLedgerAdapter()

    def _ledger_rows(self, request_id: str) -> list:
        with db.get_connection(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM ledger WHERE reference_type = 'withdrawal' "
                "AND reference_id = ? ORDER BY id",
                (request_id,),
            ).fetchall()

    def test_24_hold_release_settlement_use_withdrawal_reference(self):
        """24. All three operations carry reference_type='withdrawal',
        reference_id=request_id and the classic deltas."""
        amount = 150_000_000
        with db.transaction(self.db_path) as conn:
            hold = self.adapter.hold(
                501, amount, request_id="wd-led", connection=conn
            )
            release = self.adapter.release(
                501, amount, request_id="wd-led", connection=conn
            )
            settle = self.adapter.settle(
                501, amount, request_id="wd-led", connection=conn
            )
        # reference contract
        for entry in (hold, release, settle):
            self.assertEqual(entry.reference_type, "withdrawal")
            self.assertEqual(entry.reference_id, "wd-led")
            self.assertEqual(entry.user_id, 501)
            self.assertEqual(entry.amount_units, amount)
        # hold: available -> held
        self.assertEqual(hold.entry_type, "hold")
        self.assertEqual(
            (hold.available_delta, hold.held_delta),
            (-amount, amount),
        )
        # release: held -> available
        self.assertEqual(release.entry_type, "release")
        self.assertEqual(
            (release.available_delta, release.held_delta),
            (amount, -amount),
        )
        # settlement: held -> gone
        self.assertEqual(settle.entry_type, "settlement")
        self.assertEqual(
            (settle.available_delta, settle.held_delta),
            (0, -amount),
        )
        rows = self._ledger_rows("wd-led")
        self.assertEqual(
            [r["entry_type"] for r in rows],
            ["hold", "release", "settlement"],
        )
        self.assertEqual(
            {r["reference_type"] for r in rows}, {"withdrawal"}
        )

    def test_25_idempotency_keys_remain_intact(self):
        """25. Keys stay withdrawal:<id>:<entry_type>; a replay returns
        the ORIGINAL entry instead of double-recording."""
        amount = 100_000_000
        with db.transaction(self.db_path) as conn:
            first = self.adapter.hold(
                501, amount, request_id="wd-idem", connection=conn
            )
            replay = self.adapter.hold(
                501, amount, request_id="wd-idem", connection=conn
            )
            self.assertEqual(first.id, replay.id)
            self.assertEqual(
                first.idempotency_key, "withdrawal:wd-idem:hold"
            )
            release = self.adapter.release(
                501, amount, request_id="wd-idem", connection=conn
            )
            settle = self.adapter.settle(
                501, amount, request_id="wd-idem", connection=conn
            )
        self.assertEqual(
            release.idempotency_key, "withdrawal:wd-idem:release"
        )
        self.assertEqual(
            settle.idempotency_key, "withdrawal:wd-idem:settlement"
        )
        rows = self._ledger_rows("wd-idem")
        self.assertEqual(len(rows), 3)  # replay inserted nothing new
        # key reuse with DIFFERENT data is still a domain conflict
        with db.transaction(self.db_path) as conn:
            self.adapter.hold(
                501, 50_000_000, request_id="wd-idem2", connection=conn
            )
        with self.assertRaises(LedgerIdempotencyConflictError):
            with db.transaction(self.db_path) as conn:
                self.adapter.hold(
                    501, 60_000_000, request_id="wd-idem2",
                    connection=conn,
                )
        # same reference + different key -> reference conflict
        with self.assertRaises(LedgerReferenceConflictError):
            with db.transaction(self.db_path) as conn:
                LedgerService(connection=conn).record_hold(
                    501, amount_units=1, reference_type="withdrawal",
                    reference_id="wd-idem", idempotency_key="other-key",
                )

    def test_26_caller_connection_preserved(self):
        """26. Ledger writes join the caller transaction: identity is
        forwarded, nothing commits early, rollback removes everything."""
        with self.assertRaises(RuntimeError):
            with db.transaction(self.db_path) as conn:
                with mock.patch.object(
                    withdrawal_store, "LedgerService",
                    wraps=withdrawal_store.LedgerService,
                ) as spy:
                    self.adapter.hold(
                        501, 100_000_000, request_id="wd-lc",
                        connection=conn,
                    )
                self.assertIs(
                    spy.call_args.kwargs["connection"], conn
                )
                self.assertTrue(conn.in_transaction)
                raise RuntimeError("rollback sentinel")

    def test_26b_ledger_rollback_discards_entries(self):
        """26b. Behavioral proof for the caller-owned connection."""
        with self.assertRaises(RuntimeError):
            with db.transaction(self.db_path) as conn:
                self.adapter.hold(
                    501, 100_000_000, request_id="wd-lc2",
                    connection=conn,
                )
                raise RuntimeError("rollback sentinel")
        self.assertEqual(self._ledger_rows("wd-lc2"), [])
        # committed caller transaction persists the entry
        with db.transaction(self.db_path) as conn:
            self.adapter.hold(
                501, 100_000_000, request_id="wd-lc3",
                connection=conn,
            )
        self.assertEqual(len(self._ledger_rows("wd-lc3")), 1)

    def test_27_no_duplicated_ledger_accounting_logic(self):
        """27. The adapter delegates to LedgerService: no ledger INSERT,
        no wallet UPDATE, no delta table exists in the module — and the
        repository itself never moves money."""
        src = inspect.getsource(withdrawal_store)
        low = src.lower()
        self.assertNotIn("insert into ledger", low)
        self.assertNotIn("update wallets", low)
        self.assertNotIn("insert into wallets", low)
        self.assertNotIn("available_delta", src)
        self.assertNotIn("held_delta", src)
        self.assertNotIn("_deltas", low)
        # delegation IS present
        self.assertIn("LedgerService", src)

        # behavioral: repository insert + transition move NO money
        wallet.credit_units(501, 500_000_000)
        before = tuple(wallet.wallet_units(501))
        with db.transaction(self.db_path) as conn:
            self.insert(_make_request("wd-pure"), connection=conn)
            self.repo.transition(
                "wd-pure", to_status="rejected",
                at=datetime(2026, 9, 27, 12, 0, 0), connection=conn,
            )
        self.assertEqual(tuple(wallet.wallet_units(501)), before)
        with db.get_connection(self.db_path) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM ledger"
            ).fetchone()[0]
        self.assertEqual(n, 0)


# ── 28–30: payment-method read boundary ──────────────────────────────


class TestPaymentMethodBoundary(_Base):
    def test_28_active_method_resolves(self):
        """28. An active method resolves to its stored row."""
        method = self.create_method(active=True)
        got = resolve_active_payment_method(
            method.id, db_path=self.db_path
        )
        self.assertEqual(got.id, method.id)
        self.assertTrue(got.is_active)
        self.assertEqual(got.display_name, "TEST-METHOD")

    def test_29_inactive_method_rejected(self):
        """29. A deactivated method can never back a new withdrawal."""
        method = self.create_method(active=False)
        with self.assertRaises(PaymentMethodUnavailableError):
            resolve_active_payment_method(
                method.id, db_path=self.db_path
            )

    def test_30_missing_method_rejected(self):
        """30. Missing ids fail with the same deterministic domain
        error — no fallback, no selection among methods."""
        with self.assertRaises(PaymentMethodUnavailableError):
            resolve_active_payment_method(
                42424242, db_path=self.db_path
            )
        for bad in (0, -1, "abc", True, None):
            with self.assertRaises(withdrawal_rules.WithdrawalError):
                resolve_active_payment_method(
                    bad, db_path=self.db_path
                )


# ── 31–32: error translation boundary ────────────────────────────────


class TestErrorBoundary(_Base):
    def test_31_repository_errors_flow_through_mt_admin_21_boundary(self):
        """31. Every repository/adapters failure is a withdrawal-domain
        error produced by the MT-ADMIN-21 translation boundary — never
        a raw lower-level type."""
        self.add_user(501)
        self.add_user(502)
        method = self.create_method()

        # rate facts validated by rate_quote, surfaced as domain error
        with self.assertRaises(withdrawal_rules.InvalidRateError):
            self.insert(_make_request(
                "wd-badrate", wallet_rate_usdt_egp="not-a-rate"
            ))
        with self.assertRaises(withdrawal_rules.InvalidRateError):
            self.insert(_make_request(
                "wd-badprov", rate_provider="fake-provider"
            ))
        # missing required facts are never invented
        with self.assertRaises(ValidationError):
            self.insert(_make_request("wd-norate-fact",
                                      wallet_rate_usdt_egp=None))
        with self.assertRaises(ValidationError):
            self.insert(_make_request("wd-nocap",
                                      rate_captured_at=None))
        # invalid financial inputs
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            self.insert(_make_request(
                "wd-badamt", amount_egp=Decimal("0")
            ))
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            self.insert(_make_request(
                "wd-badamt", wallet_debit_units=-1
            ))
        with self.assertRaises(withdrawal_rules.InvalidMethodError):
            self.insert(_make_request("wd-badmethod", method="paypal"))
        # read/state errors are domain errors
        with self.assertRaises(RequestNotFoundError):
            self.repo.get("wd-none")
        # an invalid target status fails validation first
        self.insert(_make_request("wd-e1"))
        with self.assertRaises(ValidationError):
            self.repo.transition(
                "wd-e1", to_status="pending",
                at=datetime(2026, 9, 27, 12, 0, 0),
            )
        # a non-pending row surfaces the domain InvalidStateError
        self.repo.transition(
            "wd-e1", to_status="rejected",
            at=datetime(2026, 9, 27, 12, 0, 0),
        )
        with self.assertRaises(InvalidStateError):
            self.repo.transition(
                "wd-e1", to_status="completed",
                at=datetime(2026, 9, 27, 13, 0, 0),
            )
        # every error above is a WithdrawalError
        for factory in (
            lambda: self.insert(_make_request(
                "wd-badrate", wallet_rate_usdt_egp="nope"
            )),
            lambda: self.repo.get("wd-none"),
        ):
            with self.assertRaises(withdrawal_rules.WithdrawalError):
                factory()
        # payment-method boundary: store errors -> domain class
        with self.assertRaises(PaymentMethodUnavailableError):
            resolve_active_payment_method(
                method.id + 999, db_path=self.db_path
            )
        # ledger boundary: ledger validation error -> domain error
        led = SqliteLedgerAdapter()
        with self.assertRaises(withdrawal_rules.ValidationError) as ctx:
            with db.transaction(self.db_path) as conn:
                led.hold(42424242, 100, request_id="wd-led-x",
                         connection=conn)
        self.assertIsNotNone(ctx.exception.__cause__)
        # wallet error translated at the service edge, per the table
        with self.assertRaises(
            withdrawal_rules.InsufficientBalanceError
        ):
            with translated_errors():
                raise wallet.InsufficientBalanceError("nope")

    def test_32_unexpected_exception_identity_preserved(self):
        """32. Unexpected exceptions are re-raised as the SAME object —
        never swallowed, never re-labeled."""
        sentinel = RuntimeError("unexpected boom")
        self.add_user(501)

        class BoomConnection:
            def execute(self, *_args, **_kwargs):
                raise sentinel

        with self.assertRaises(RuntimeError) as ctx:
            self.insert(
                _make_request("wd-boom"),
                connection=BoomConnection(),
            )
        self.assertIs(ctx.exception, sentinel)

        # boundary level: identity + original traceback chain
        sentinel2 = ValueError("other")
        with self.assertRaises(ValueError) as ctx2:
            with translated_errors():
                raise sentinel2
        self.assertIs(ctx2.exception, sentinel2)

        # payment-method boundary: same rule
        other = RuntimeError("pm boom")
        with mock.patch.object(
            pms, "get_active_payment_method", side_effect=other
        ):
            with self.assertRaises(RuntimeError) as ctx3:
                resolve_active_payment_method(
                    1, db_path=self.db_path
                )
        self.assertIs(ctx3.exception, other)


# ── 33–36: financial safety ──────────────────────────────────────────


class TestFinancialSafety(_Base):
    def test_33_no_float_in_module_ast(self):
        """33. No float literals, no float() conversions, no .float
        attribute in withdrawal_store (isinstance guards allowed —
        they are how float inputs are rejected)."""
        src = inspect.getsource(withdrawal_store)
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
                self.fail(f"float() conversion at line {node.lineno}")
            if (
                isinstance(node, ast.Name)
                and node.id == "float"
                and node.id not in guarded
            ):
                self.fail(f"float reference at line {node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr == "float":
                self.fail(f"float attribute at line {node.lineno}")

    def test_34_no_real_anywhere(self):
        """34. The schema declares no REAL column, the module source
        contains no REAL, and stored money values are integer/text."""
        src = inspect.getsource(withdrawal_store)
        self.assertNotIn("REAL", src.upper())
        with db.get_connection(self.db_path) as conn:
            cols = conn.execute(
                "PRAGMA table_info(withdrawal_requests)"
            ).fetchall()
        declared = [c[2].upper() for c in cols]
        for banned in ("REAL", "FLOAT", "DOUBLE", "NUMERIC"):
            self.assertNotIn(banned, declared)
        # stored money facts live in the INTEGER/TEXT domains only
        self.add_user(501)
        self.insert(_make_request("wd-domains"))
        with db.get_connection(self.db_path) as conn:
            kinds = conn.execute(
                "SELECT typeof(amount_egp_minor), typeof(fee_egp_minor),"
                "       typeof(amount_native_minor), "
                "       typeof(fee_native_minor),"
                "       typeof(wallet_debit_units), "
                "       typeof(wallet_rate_usdt_egp)"
                "  FROM withdrawal_requests "
                " WHERE request_id = 'wd-domains'"
            ).fetchone()
        self.assertEqual(
            list(kinds),
            ["integer", "integer", "integer", "integer",
             "integer", "text"],
        )

    def test_35_no_currency_conversion_in_repository_or_adapters(self):
        """35. The module validates rates but NEVER converts currency:
        no rate-based conversion helper, no arithmetic on a rate."""
        src = inspect.getsource(withdrawal_store)
        # rate_quote's conversion helpers are deliberately unused here
        self.assertNotIn("egp_to_wallet_units", src)
        self.assertNotIn("usdt_units_to_egp_display", src)
        # no arithmetic expression ever references a rate
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                seg = ast.get_source_segment(src, node) or ""
                self.assertNotIn(
                    "rate", seg.lower(),
                    f"rate arithmetic at line {node.lineno}: {seg!r}",
                )
        # behavioral: wallet_debit_units is stored VERBATIM even when it
        # could not possibly be derived from amount/rate (no recompute)
        self.add_user(501)
        self.insert(_make_request("wd-verbatim", wallet_debit_units=7))
        self.assertEqual(
            self.raw_row("wd-verbatim")["wallet_debit_units"], 7
        )

    def test_36_wallet_debit_is_usdt_integer_units(self):
        """36. wallet_debit_units stays exact USDT atomic units:
        integer storage, exact read-back, non-int input rejected."""
        self.add_user(501)
        debit = 1 * USDT_SCALE + 1  # 1.00000001 USDT in units
        self.insert(_make_request("wd-usdt", wallet_debit_units=debit))
        with db.get_connection(self.db_path) as conn:
            kind, value = conn.execute(
                "SELECT typeof(wallet_debit_units), wallet_debit_units "
                "FROM withdrawal_requests WHERE request_id = 'wd-usdt'"
            ).fetchone()
        self.assertEqual(kind, "integer")
        self.assertIs(type(value), int)
        self.assertEqual(value, debit)
        got = self.repo.get("wd-usdt")
        self.assertIs(type(got.wallet_debit_units), int)
        self.assertEqual(got.wallet_debit_units, debit)
        # non-integer inputs never reach storage
        for bad, error in (
            (1.5, ValidationError),
            (True, ValidationError),
            ("100", ValidationError),
            (-1, withdrawal_rules.InvalidAmountError),
            (10 ** 19, withdrawal_rules.InvalidAmountError),
        ):
            with self.assertRaises(error):
                self.insert(_make_request(
                    "wd-usdt-bad", wallet_debit_units=bad
                ))
        # nothing was written for any rejected attempt
        with db.get_connection(self.db_path) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM withdrawal_requests "
                "WHERE request_id = 'wd-usdt-bad'"
            ).fetchone()[0]
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
