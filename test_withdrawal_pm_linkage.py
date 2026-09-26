"""
Focused tests — Withdrawal ↔ Payment Method Linkage Foundation (MT-ADMIN-10)
============================================================================

Foundation-only task: ``withdrawal_requests`` gains seven NULLABLE
linkage columns and a real FK to ``payment_methods(id)``, plus the
strict store helper ``get_active_payment_method``.  NO withdrawal
behavior, rules, wallet, ledger or method/native_unit policy changes.

Coverage (task list 1–9):

 1. seven linkage columns exist, are nullable and typed INTEGER/TEXT
 2. FK ``payment_method_id → payment_methods(id)`` is declared
 3. FK enforced: unknown id rejected / valid id accepted / NULL ok
 4. the method CHECK stays exactly (vodafone_cash | usdt_bep20)
 5. ``get_active_payment_method``: active lookup returns the method
 6. ``get_active_payment_method``: inactive lookup rejected
 7. ``get_active_payment_method``: missing lookup rejected
 8. referenced delete rejected → clear store-level error, row kept
 9. unreferenced delete still works
10. schema re-init safety: repeated init_db + pre-linkage migration
11. FK also enforced on the migrated (ALTER-ed) schema

Regression (run alongside, asserted green by the task):
    test_payment_methods.py   — old security guards
    test_wallet_schema.py     — existing withdrawal schema tests

Run:
    python -m pytest test_withdrawal_pm_linkage.py -v
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

import db
import payment_method_store as store

# The seven new columns (MT-ADMIN-10) — all nullable.
LINKAGE_COLUMNS = (
    "payment_method_id",
    "pm_display_name",
    "pm_category",
    "pm_asset",
    "pm_network",
    "pm_provider",
    "pm_destination",
)

# Exact pre-MT-ADMIN-10 shape of withdrawal_requests: used to prove the
# additive migration upgrades an existing database in place.
PRE_LINKAGE_DDL = """
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
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )
"""


class LinkageTestBase(unittest.TestCase):
    """Temp-DB fixture + schema/raw helpers (MT-ADMIN-10)."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)
        self.addCleanup(self._restore)

    def _restore(self):
        db.DB_PATH = self._original_db_path
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    # ── introspection helpers ──────────────────────────────────

    def table_info(self, table):
        with db.get_connection(self.db_path) as conn:
            return conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()

    def column_names(self, table):
        return [row["name"] for row in self.table_info(table)]

    def foreign_keys(self, table):
        with db.get_connection(self.db_path) as conn:
            return conn.execute(
                f"PRAGMA foreign_key_list({table})"
            ).fetchall()

    # ── seed helpers ───────────────────────────────────────────

    def add_user(self, user_id):
        self.assertTrue(
            db.register_user(user_id, f"user{user_id}", f"User {user_id}")
        )

    def insert_withdrawal(self, user_id, *, payment_method_id=None,
                          method="vodafone_cash", status="pending"):
        """Insert one withdrawal row with optional PM linkage."""
        request_id = f"req-{user_id}-{payment_method_id}-" \
                     f"{method}-{status}-{os.urandom(4).hex()}"
        with db.get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO withdrawal_requests (
                       request_id, user_id, method, amount_egp_minor,
                       fee_egp_minor, amount_native_minor, fee_native_minor,
                       native_unit, rate_usdt_egp, wallet_rate_usdt_egp,
                       rate_captured_at, rate_provider, status,
                       payment_method_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, user_id, method, 1000, 100, 1000, 100,
                 "EGP", None, "50.000000", "2026-09-26 12:00:00",
                 "manual", status, payment_method_id),
            )
        return request_id

    def create_method(self, *, active=True):
        method = store.create_payment_method(
            category="crypto",
            display_name="وسيلة الاختبار",
            asset="TESTASSET",
            network="TESTNET",
            provider="مزود الاختبار",
            destination="TEST-DESTINATION-1",
            created_by=1,
            db_path=self.db_path,
        )
        if not active:
            store.set_payment_method_active(
                method.id, False, updated_by=1, db_path=self.db_path
            )
        return method


# ── 1–2: schema shape (columns + declared FK) ────────────────────────


class TestLinkageSchema(LinkageTestBase):

    def test_seven_linkage_columns_exist_and_are_nullable(self):
        """1. All seven columns exist, nullable, INTEGER/TEXT typed."""
        info = {row["name"]: row for row in self.table_info(
            "withdrawal_requests"
        )}
        for name in LINKAGE_COLUMNS:
            self.assertIn(name, info, f"missing column {name}")
            self.assertEqual(
                info[name]["notnull"], 0, f"{name} must be nullable"
            )
            self.assertIsNone(
                info[name]["dflt_value"], f"{name} has no default"
            )
        self.assertEqual(info["payment_method_id"]["type"], "INTEGER")
        for name in LINKAGE_COLUMNS[1:]:
            self.assertEqual(info[name]["type"], "TEXT", name)

    def test_fk_declared_to_payment_methods(self):
        """2. payment_method_id → payment_methods(id) is declared."""
        fks = [
            row for row in self.foreign_keys("withdrawal_requests")
            if row["from"] == "payment_method_id"
        ]
        self.assertEqual(len(fks), 1)
        self.assertEqual(fks[0]["table"], "payment_methods")
        self.assertEqual(fks[0]["to"], "id")
        # No cascade tricks: default NO ACTION keeps rows protected.
        self.assertEqual((fks[0]["on_update"], fks[0]["on_delete"]),
                         ("NO ACTION", "NO ACTION"))


# ── 3–4: FK enforcement + unchanged method policy ────────────────────


class TestForeignKeyEnforcement(LinkageTestBase):

    def test_unknown_payment_method_id_rejected(self):
        """3a. Linking to a non-existent method is an IntegrityError."""
        self.add_user(1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_withdrawal(1, payment_method_id=999999)

    def test_valid_payment_method_id_accepted(self):
        """3b. Linking to an existing method succeeds and persists."""
        self.add_user(1)
        method = self.create_method()
        self.insert_withdrawal(1, payment_method_id=method.id)
        with db.get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT payment_method_id FROM withdrawal_requests "
                "WHERE user_id = ?",
                (1,),
            ).fetchone()
        self.assertEqual(row["payment_method_id"], method.id)

    def test_null_payment_method_id_accepted(self):
        """3c. Unlinked withdrawals (all seven columns NULL) work."""
        self.add_user(1)
        self.insert_withdrawal(1, payment_method_id=None)

    def test_method_policy_unchanged(self):
        """4. The method CHECK still allows ONLY the two old methods."""
        self.add_user(1)
        self.add_user(2)
        self.add_user(3)
        self.insert_withdrawal(1, method="vodafone_cash")
        self.insert_withdrawal(2, method="usdt_bep20")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_withdrawal(3, method="paypal")


# ── 5–7: get_active_payment_method helper ────────────────────────────


class TestGetActivePaymentMethod(LinkageTestBase):

    def test_active_lookup_returns_method(self):
        """5. An active method is returned with its stored fields."""
        created = self.create_method(active=True)
        found = store.get_active_payment_method(
            created.id, db_path=self.db_path
        )
        self.assertIsInstance(found, store.PaymentMethod)
        self.assertEqual(found.id, created.id)
        self.assertTrue(found.is_active)
        self.assertEqual(found.display_name, created.display_name)
        self.assertEqual(found.category, "crypto")
        self.assertEqual(found.asset, "TESTASSET")

    def test_inactive_lookup_rejected(self):
        """6. A deactivated method is rejected, not returned."""
        created = self.create_method(active=False)
        with self.assertRaises(store.PaymentMethodInactiveError):
            store.get_active_payment_method(
                created.id, db_path=self.db_path
            )
        # The rejection is a store-level payment-method error.
        with self.assertRaises(store.PaymentMethodError):
            store.get_active_payment_method(
                created.id, db_path=self.db_path
            )

    def test_missing_lookup_rejected(self):
        """7. A non-existent id (and a malformed id) are rejected."""
        with self.assertRaises(store.PaymentMethodNotFoundError):
            store.get_active_payment_method(999999, db_path=self.db_path)
        with self.assertRaises(store.PaymentMethodValidationError):
            store.get_active_payment_method(0, db_path=self.db_path)


# ── 8–9: delete linkage rules ────────────────────────────────────────


class TestDeleteLinkageRules(LinkageTestBase):

    def test_referenced_delete_rejected_and_row_kept(self):
        """8. Deleting a method a withdrawal points at is refused."""
        self.add_user(1)
        method = self.create_method()
        self.insert_withdrawal(1, payment_method_id=method.id)

        with self.assertRaises(store.PaymentMethodInUseError):
            store.delete_payment_method(
                method.id, deleted_by=1, db_path=self.db_path
            )
        # Clear store-level error, not a raw sqlite error.
        with self.assertRaises(store.PaymentMethodError):
            store.delete_payment_method(
                method.id, deleted_by=1, db_path=self.db_path
            )
        # Nothing was destroyed: method + withdrawal both survive.
        self.assertIsNotNone(
            store.get_payment_method(method.id, db_path=self.db_path)
        )
        with db.get_connection(self.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM withdrawal_requests"
            ).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_unreferenced_delete_still_works(self):
        """9. A method no withdrawal references deletes normally."""
        referenced = self.create_method()
        orphan = self.create_method()
        self.add_user(1)
        self.insert_withdrawal(1, payment_method_id=referenced.id)

        self.assertTrue(
            store.delete_payment_method(
                orphan.id, deleted_by=1, db_path=self.db_path
            )
        )
        self.assertIsNone(
            store.get_payment_method(orphan.id, db_path=self.db_path)
        )
        # The referenced one is still there, still protected.
        self.assertIsNotNone(
            store.get_payment_method(referenced.id, db_path=self.db_path)
        )


# ── 10–11: schema re-init safety ─────────────────────────────────────


class TestSchemaReinitSafety(LinkageTestBase):

    def test_repeated_init_db_keeps_linkage_schema(self):
        """10a. init_db() twice more: columns, FK and tables stable."""
        before_cols = self.column_names("withdrawal_requests")
        before_fks = self.foreign_keys("withdrawal_requests")
        db.init_db(self.db_path)
        db.init_db(self.db_path)
        self.assertEqual(
            self.column_names("withdrawal_requests"), before_cols
        )
        self.assertEqual(
            self.foreign_keys("withdrawal_requests"), before_fks
        )
        for name in LINKAGE_COLUMNS:
            self.assertIn(name, before_cols)

    def test_migration_upgrades_pre_linkage_database(self):
        """10b. An old-schema DB gains the columns; rows stay intact."""
        self.add_user(1)
        # Rebuild the pre-linkage shape on the live database.
        with db.get_connection(self.db_path) as conn:
            conn.execute("DROP TABLE withdrawal_requests")
            conn.execute(PRE_LINKAGE_DDL)
            conn.execute(
                """INSERT INTO withdrawal_requests (
                       request_id, user_id, method, amount_egp_minor,
                       fee_egp_minor, amount_native_minor, fee_native_minor,
                       native_unit, rate_usdt_egp, wallet_rate_usdt_egp,
                       rate_captured_at, rate_provider, status
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("legacy-1", 1, "vodafone_cash", 1000, 100, 1000, 100,
                 "EGP", None, "50.000000", "2026-09-01 12:00:00",
                 "manual", "completed"),
            )
        # No linkage columns on the old shape yet.
        for name in LINKAGE_COLUMNS:
            self.assertNotIn(name, self.column_names("withdrawal_requests"))

        db.init_db(self.db_path)   # additive migration runs

        cols = self.column_names("withdrawal_requests")
        for name in LINKAGE_COLUMNS:
            self.assertIn(name, cols, f"migration missed {name}")
        # FK is declared on the migrated table too.
        fks = [
            row for row in self.foreign_keys("withdrawal_requests")
            if row["from"] == "payment_method_id"
        ]
        self.assertEqual(len(fks), 1)
        self.assertEqual(fks[0]["table"], "payment_methods")
        # The legacy row survives with NULL linkage fields.
        with db.get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT request_id, payment_method_id, pm_display_name, "
                "       pm_destination, method "
                "FROM withdrawal_requests WHERE request_id = 'legacy-1'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["method"], "vodafone_cash")
        self.assertIsNone(row["payment_method_id"])
        self.assertIsNone(row["pm_display_name"])
        self.assertIsNone(row["pm_destination"])

    def test_fk_enforced_on_migrated_schema(self):
        """11. The migrated (ALTER-ed) FK actually rejects bad ids."""
        # Rebuild old shape WITHOUT re-running init_db first: emulate
        # a database that only ever saw the pre-linkage schema.
        with db.get_connection(self.db_path) as conn:
            conn.execute("DROP TABLE withdrawal_requests")
            conn.execute(PRE_LINKAGE_DDL)
        # Fresh connection process would run init_db; simulate it here.
        db.init_db(self.db_path)
        self.add_user(1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_withdrawal(1, payment_method_id=424242)
        method = self.create_method()
        self.insert_withdrawal(1, payment_method_id=method.id)  # accepted


if __name__ == "__main__":
    unittest.main()
