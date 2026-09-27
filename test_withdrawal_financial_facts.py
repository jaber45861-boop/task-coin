"""
Focused tests — Withdrawal Financial Fact Columns + User Destination
(MT-ADMIN-19)
=====================================================================

Schema + data-model foundation only: ``withdrawal_requests`` gains four
ADDITIVE nullable columns and the read mapping exposes them.

  A. fresh schema contains all four columns (exact types)
  B. an existing (pre-facts) schema migrates successfully
  C. the migration is idempotent across repeated ``init_db()`` calls
  D. existing withdrawal rows remain unchanged after migration
  E. legacy rows can have NULL ``wallet_debit_units``
  F. legacy rows can have NULL ``user_destination``
  G. new rows can persist and read back exact ``wallet_debit_units``,
     exact ``user_destination``, ``rejected_at`` and ``completed_at``
  H. ``wallet_debit_units`` is SQLite INTEGER when populated
  I. no REAL/float column is introduced
  J. payment-method linkage columns remain intact
  K. withdrawal status constraints remain intact
  L. existing withdrawal schema tests continue passing
     (test_wallet_schema / test_withdrawal_pm_linkage /
      test_withdrawal_rules / test_db_transactions / test_wallet /
      test_ledger — run separately)

Semantics pinned here:
- ``wallet_debit_units`` is USDT atomic units (1 USDT = 100,000,000),
  stored, never computed; NULL means "not established", never 0.
- ``user_destination`` is the USER's payout destination — never
  ``payment_methods.destination`` / ``pm_destination``.

Temp databases only; no production destinations are invented.

Run:
    python -m pytest test_withdrawal_financial_facts.py -v
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal

import db
import withdrawal_rules
from withdrawal_rules import RequestStatus, WithdrawalRequest

# The four MT-ADMIN-19 columns -> declared SQLite type.
FACT_COLUMNS = {
    "wallet_debit_units": "INTEGER",
    "user_destination": "TEXT",
    "rejected_at": "TIMESTAMP",
    "completed_at": "TIMESTAMP",
}

# The seven MT-ADMIN-10 linkage columns — must survive untouched.
LINKAGE_COLUMNS = (
    "payment_method_id",
    "pm_display_name",
    "pm_category",
    "pm_asset",
    "pm_network",
    "pm_provider",
    "pm_destination",
)

# Exact pre-MT-ADMIN-19 shape of withdrawal_requests: every column that
# existed before this task, and NOT ONE of the new fact columns.
PRE_FACTS_DDL = """
    CREATE TABLE withdrawal_requests (
        request_id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        method TEXT NOT NULL CHECK (method IN (
            'vodafone_cash', 'usdt_bep20')),
        amount_egp_minor INTEGER NOT NULL
            CHECK (amount_egp_minor > 0),
        fee_egp_minor INTEGER NOT NULL CHECK (fee_egp_minor >= 0),
        amount_native_minor INTEGER NOT NULL
            CHECK (amount_native_minor > 0),
        fee_native_minor INTEGER NOT NULL
            CHECK (fee_native_minor >= 0),
        native_unit TEXT NOT NULL CHECK (native_unit IN ('EGP', 'USDT')),
        rate_usdt_egp TEXT,
        wallet_rate_usdt_egp TEXT NOT NULL,
        rate_captured_at TIMESTAMP NOT NULL,
        rate_provider TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN (
            'pending', 'rejected', 'completed')),
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        payment_method_id INTEGER REFERENCES payment_methods(id),
        pm_display_name TEXT,
        pm_category TEXT,
        pm_asset TEXT,
        pm_network TEXT,
        pm_provider TEXT,
        pm_destination TEXT,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )
"""


class FactsTestBase(unittest.TestCase):
    """Temp-DB fixture + raw helpers (project test pattern)."""

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

    def table_info(self, table):
        with db.get_connection(self.db_path) as conn:
            return conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()

    def column_types(self, table):
        return {row["name"]: row["type"] for row in self.table_info(table)}

    def foreign_keys(self, table):
        with db.get_connection(self.db_path) as conn:
            return conn.execute(
                f"PRAGMA foreign_key_list({table})"
            ).fetchall()

    def insert_withdrawal(self, request_id, user_id=1, **overrides):
        """Insert one row with the FULL current column set; every new
        fact column can be populated explicitly or left NULL."""
        values = {
            "request_id": request_id,
            "user_id": user_id,
            "method": "vodafone_cash",
            "amount_egp_minor": 1000,
            "fee_egp_minor": 100,
            "amount_native_minor": 1000,
            "fee_native_minor": 100,
            "native_unit": "EGP",
            "rate_usdt_egp": None,
            "wallet_rate_usdt_egp": "50.000000",
            "rate_captured_at": "2026-09-26 12:00:00",
            "rate_provider": "manual",
            "status": "completed",
            "payment_method_id": None,
            "pm_display_name": None,
            "pm_category": None,
            "pm_asset": None,
            "pm_network": None,
            "pm_provider": None,
            "pm_destination": None,
            "wallet_debit_units": None,
            "user_destination": None,
            "rejected_at": None,
            "completed_at": None,
        }
        values.update(overrides)
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with db.get_connection(self.db_path) as conn:
            conn.execute(
                f"INSERT INTO withdrawal_requests ({columns}) "
                f"VALUES ({placeholders})",
                tuple(values.values()),
            )

    def insert_legacy(self, request_id, user_id=1):
        """Insert using ONLY the pre-MT-ADMIN-19 columns (for a table
        that does not have the fact columns yet)."""
        with db.get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO withdrawal_requests (
                       request_id, user_id, method, amount_egp_minor,
                       fee_egp_minor, amount_native_minor, fee_native_minor,
                       native_unit, rate_usdt_egp, wallet_rate_usdt_egp,
                       rate_captured_at, rate_provider, status,
                       payment_method_id, pm_display_name, pm_category,
                       pm_asset, pm_network, pm_provider, pm_destination
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                             ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, user_id, "vodafone_cash", 1000, 100,
                 1000, 100, "EGP", None, "50.000000",
                 "2026-09-01 12:00:00", "manual", "completed",
                 None, None, None, None, None, None, None),
            )

    def rebuild_pre_facts_schema(self):
        """Replace the table with its exact pre-MT-ADMIN-19 shape."""
        with db.get_connection(self.db_path) as conn:
            conn.execute("DROP TABLE withdrawal_requests")
            conn.execute(PRE_FACTS_DDL)

    def fetch_row(self, request_id):
        with db.get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM withdrawal_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return dict(row) if row else None


# ── A: fresh schema ───────────────────────────────────────────────────


class TestFreshSchema(FactsTestBase):

    def test_A_fresh_schema_contains_all_four_columns(self):
        """A. A fresh database creates all four columns with the exact
        declared types (INTEGER / TEXT / TIMESTAMP ×2)."""
        cols = self.column_types("withdrawal_requests")
        for name, declared_type in FACT_COLUMNS.items():
            self.assertIn(name, cols, f"missing column {name}")
            self.assertEqual(cols[name], declared_type, name)


# ── B, D, E, F: migration of an existing schema ───────────────────────


class TestMigration(FactsTestBase):

    def test_B_existing_schema_migrates_successfully(self):
        """B. A pre-facts database gains the four columns in place."""
        self.rebuild_pre_facts_schema()
        for name in FACT_COLUMNS:
            self.assertNotIn(name, self.column_types("withdrawal_requests"))

        db.init_db(self.db_path)  # additive migration runs

        cols = self.column_types("withdrawal_requests")
        for name, declared_type in FACT_COLUMNS.items():
            self.assertIn(name, cols, f"migration missed {name}")
            self.assertEqual(cols[name], declared_type, name)

    def test_C_migration_is_idempotent(self):
        """C. Repeated init_db() runs: columns appear exactly once and
        the column list is stable."""
        self.rebuild_pre_facts_schema()
        db.init_db(self.db_path)
        for _ in range(3):
            db.init_db(self.db_path)
            names = [row["name"] for row in self.table_info(
                "withdrawal_requests"
            )]
            for name in FACT_COLUMNS:
                self.assertEqual(names.count(name), 1, name)

    def test_D_existing_rows_unchanged_after_migration(self):
        """D. A row written before the migration keeps every original
        value byte-for-byte; only the new columns are NULL."""
        self.rebuild_pre_facts_schema()
        self.insert_legacy("legacy-keep")
        before = self.fetch_row("legacy-keep")
        self.assertIsNotNone(before)

        db.init_db(self.db_path)

        after = self.fetch_row("legacy-keep")
        self.assertIsNotNone(after)
        for key, value in before.items():
            self.assertEqual(after[key], value, f"column {key} changed")
        for name in FACT_COLUMNS:
            self.assertIn(name, after)
            self.assertIsNone(after[name], name)

    def test_E_legacy_rows_can_have_null_wallet_debit_units(self):
        """E. NULL wallet_debit_units is a legal legacy value — never
        coerced to 0, never derived from amount columns."""
        self.rebuild_pre_facts_schema()
        self.insert_legacy("legacy-no-debit")
        db.init_db(self.db_path)

        row = self.fetch_row("legacy-no-debit")
        self.assertIsNone(row["wallet_debit_units"])
        # explicitly NOT reinterpreted as zero or as any amount:
        self.assertNotEqual(row["wallet_debit_units"], 0)
        self.assertNotEqual(
            row["wallet_debit_units"], row["amount_native_minor"]
        )

    def test_F_legacy_rows_can_have_null_user_destination(self):
        """F. NULL user_destination is a legal legacy value."""
        self.rebuild_pre_facts_schema()
        self.insert_legacy("legacy-no-dest")
        db.init_db(self.db_path)

        row = self.fetch_row("legacy-no-dest")
        self.assertIsNone(row["user_destination"])
        self.assertIsNone(row["rejected_at"])
        self.assertIsNone(row["completed_at"])


# ── G, H: writing and reading the new facts ───────────────────────────


class TestReadWriteFacts(FactsTestBase):

    def test_G_new_rows_persist_and_read_exact_values(self):
        """G. Exact round-trip of all four facts through the read
        mapping (``db.get_withdrawal_request``)."""
        # cash withdrawal: phone number as the USER's destination
        self.insert_withdrawal(
            "req-cash",
            wallet_debit_units=987_654_321,
            user_destination="+201000000001",
            rejected_at="2026-09-27 09:30:00",
        )
        # crypto withdrawal: wallet address as the USER's destination
        self.insert_withdrawal(
            "req-crypto",
            method="usdt_bep20",
            native_unit="USDT",
            amount_native_minor=25_000_000,
            fee_native_minor=2_000_000,
            rate_usdt_egp="50.000000",
            wallet_debit_units=27_000_000,
            user_destination="TEST-USER-ADDRESS-1",
            completed_at="2026-09-27 10:00:00",
        )

        cash = db.get_withdrawal_request("req-cash", db_path=self.db_path)
        self.assertIsNotNone(cash)
        self.assertEqual(cash["wallet_debit_units"], 987_654_321)
        self.assertEqual(cash["user_destination"], "+201000000001")
        self.assertEqual(cash["rejected_at"], "2026-09-27 09:30:00")
        self.assertIsNone(cash["completed_at"])

        crypto = db.get_withdrawal_request(
            "req-crypto", db_path=self.db_path
        )
        self.assertEqual(crypto["wallet_debit_units"], 27_000_000)
        self.assertEqual(crypto["user_destination"], "TEST-USER-ADDRESS-1")
        self.assertEqual(crypto["completed_at"], "2026-09-27 10:00:00")
        self.assertIsNone(crypto["rejected_at"])

        # existing callers: unknown id -> None, omitted facts -> None
        self.assertIsNone(
            db.get_withdrawal_request("nope", db_path=self.db_path)
        )
        self.insert_withdrawal("req-minimal")
        minimal = db.get_withdrawal_request("req-minimal",
                                            db_path=self.db_path)
        for name in FACT_COLUMNS:
            self.assertIsNone(minimal[name], name)
        # pre-existing columns still exposed by the same mapping:
        self.assertEqual(minimal["amount_egp_minor"], 1000)
        self.assertEqual(minimal["status"], "completed")

    def test_H_wallet_debit_units_is_sqlite_integer_when_populated(self):
        """H. The stored value is a true SQLite integer (typeof =
        'integer'), declared INTEGER, and reads back as Python int."""
        self.insert_withdrawal("req-int", wallet_debit_units=1)
        with db.get_connection(self.db_path) as conn:
            typeof = conn.execute(
                "SELECT typeof(wallet_debit_units) "
                "FROM withdrawal_requests WHERE request_id = 'req-int'"
            ).fetchone()[0]
        self.assertEqual(typeof, "integer")
        self.assertEqual(
            self.column_types("withdrawal_requests")["wallet_debit_units"],
            "INTEGER",
        )
        row = db.get_withdrawal_request("req-int", db_path=self.db_path)
        self.assertIsInstance(row["wallet_debit_units"], int)


# ── I, J, K: schema invariants preserved ──────────────────────────────


class TestSchemaInvariants(FactsTestBase):

    def test_I_no_real_or_float_column_introduced(self):
        """I. No REAL/FLOAT/DOUBLE column exists anywhere on
        withdrawal_requests (no float money, ever)."""
        forbidden = {"REAL", "DOUBLE", "FLOAT", "DOUBLE PRECISION"}
        for name, col_type in self.column_types(
            "withdrawal_requests"
        ).items():
            self.assertNotIn(
                col_type, forbidden,
                f"withdrawal_requests.{name} must not be {col_type}",
            )

    def test_J_payment_method_linkage_columns_remain_intact(self):
        """J. All seven linkage columns and the FK to payment_methods
        are unchanged after the MT-ADMIN-19 migration."""
        cols = self.column_types("withdrawal_requests")
        for name in LINKAGE_COLUMNS:
            self.assertIn(name, cols)
            self.assertEqual(cols[name], "TEXT" if name != "payment_method_id"
                             else "INTEGER", name)
        fks = [
            row for row in self.foreign_keys("withdrawal_requests")
            if row["from"] == "payment_method_id"
        ]
        self.assertEqual(len(fks), 1)
        self.assertEqual(fks[0]["table"], "payment_methods")
        self.assertEqual(
            (fks[0]["on_update"], fks[0]["on_delete"]),
            ("NO ACTION", "NO ACTION"),
        )

    def test_K_status_constraints_remain_intact(self):
        """K. The status CHECK still accepts exactly pending /
        rejected / completed and rejects anything else."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_withdrawal("req-bad-status", status="settled")
        # the three valid statuses still insert:
        self.insert_withdrawal("req-pending", status="pending")
        self.insert_withdrawal("req-rejected", status="rejected")
        self.insert_withdrawal("req-completed", status="completed")
        # method CHECK untouched too:
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_withdrawal("req-bad-method", method="paypal")


# ── data model: the WithdrawalRequest extension ───────────────────────


class TestDataModelExtension(unittest.TestCase):

    def test_dataclass_gains_optional_facts_with_none_defaults(self):
        """The existing WithdrawalRequest keeps its original positional
        constructor; the four new facts default to None (never a
        fabricated amount)."""
        req = WithdrawalRequest(
            request_id="r1",
            user_id=1,
            method=withdrawal_rules.METHOD_VODAFONE_CASH,
            amount_egp=Decimal("10"),
            fee_egp=Decimal("1"),
            rate_usdt_egp=None,
            amount_native=Decimal("10"),
            fee_native=Decimal("1"),
            status=RequestStatus.PENDING,
            created_at=datetime(2026, 9, 27, tzinfo=timezone.utc),
        )
        self.assertIsNone(req.wallet_debit_units)
        self.assertIsNone(req.user_destination)
        self.assertIsNone(req.rejected_at)
        self.assertIsNone(req.completed_at)
        # still frozen:
        with self.assertRaises(Exception):
            req.user_destination = "x"  # type: ignore[misc]

    def test_dataclass_facts_are_storable_and_immutable(self):
        """Explicit values persist on the frozen model via replace()."""
        import dataclasses

        req = WithdrawalRequest(
            request_id="r2",
            user_id=1,
            method=withdrawal_rules.METHOD_USDT_BEP20,
            amount_egp=Decimal("48.50"),
            fee_egp=Decimal("1"),
            rate_usdt_egp=Decimal("48.5"),
            amount_native=Decimal("1"),
            fee_native=Decimal("0.02061856"),
            status=RequestStatus.PENDING,
            created_at=datetime(2026, 9, 27, tzinfo=timezone.utc),
            wallet_debit_units=1_206_185_56,
            user_destination="TEST-USER-ADDRESS-2",
        )
        self.assertEqual(req.wallet_debit_units, 1_206_185_56)
        rejected = dataclasses.replace(
            req,
            status=RequestStatus.REJECTED,
            rejected_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
        )
        self.assertEqual(rejected.status, RequestStatus.REJECTED)
        self.assertIsNotNone(rejected.rejected_at)
        self.assertEqual(
            rejected.wallet_debit_units, 1_206_185_56,
            "replace() must carry the stored facts",
        )


if __name__ == "__main__":
    unittest.main()
