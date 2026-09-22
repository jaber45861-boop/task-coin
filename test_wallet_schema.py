"""
Schema tests — Wallet / Ledger / withdrawal_requests (Micro-task MT-1).

Covers:
  - wallets, ledger, withdrawal_requests created by db.init_db()
  - migration safety: fresh DB, pre-MT-1 (existing) DB, repeated runs
  - existing users / language / referrals / channels / tasks survive
  - foreign keys to users (PRAGMA foreign_keys = ON is preserved)
  - CHECK constraints: money positivity, enums, ledger delta semantics
  - UNIQUE(reference_type, reference_id, entry_type)
  - partial UNIQUE(idempotency_key) WHERE idempotency_key IS NOT NULL
  - indexes: cooldown lookup, one-pending-withdrawal-per-user, ledger user audit
  - column types: INTEGER money only, TEXT rates, no REAL anywhere

Append-only policy (design decision, MT-1):
    The ledger schema intentionally ships WITHOUT triggers — append-only
    write discipline is the responsibility of the future Ledger service.
    ``test_append_only_has_no_triggers`` documents that decision, while
    every constraint that CAN be enforced in SQL (deltas, uniqueness,
    positivity, enums, FKs) is enforced by the schema and tested here.

Money units:
    1 USDT = 100,000,000 wallet units;  1 EGP = 100 minor units.

Run:
    python -m pytest test_wallet_schema.py -v
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

import db
from config import Channel

# entry_type -> (available_delta, held_delta) for a given amount_units
VALID_DELTAS = {
    "credit": lambda a: (a, 0),
    "debit": lambda a: (-a, 0),
    "hold": lambda a: (-a, a),
    "release": lambda a: (a, -a),
    "settlement": lambda a: (0, -a),
}

ENTRY_TYPES = tuple(VALID_DELTAS)
REFERENCE_TYPES = (
    "withdrawal", "task", "referral", "deposit", "admin_credit", "adjustment",
)
METHODS = ("vodafone_cash", "usdt_bep20")
STATUSES = ("pending", "rejected", "completed")
NEW_TABLES = ("wallets", "ledger", "withdrawal_requests")


class WalletSchemaTestBase(unittest.TestCase):
    """Temp-DB fixture + raw-SQL helpers (schema-level tests only)."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)
        self._ref_seq = 0

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    # ── introspection helpers ──────────────────────────────────

    def table_names(self):
        with db.get_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        return {row["name"] for row in rows}

    def index_names(self):
        with db.get_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        return {row["name"] for row in rows}

    def index_sql(self, name):
        with db.get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND name = ?",
                (name,),
            ).fetchone()
        return row["sql"] if row else None

    def columns(self, table):
        with db.get_connection(self.db_path) as conn:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {row["name"]: row["type"].upper() for row in rows}

    # ── seed helpers ───────────────────────────────────────────

    def add_user(self, user_id):
        self.assertTrue(
            db.register_user(user_id, f"user{user_id}", f"User {user_id}")
        )

    def next_ref(self):
        self._ref_seq += 1
        return f"ref-{self._ref_seq}"

    def insert_wallet(self, user_id, available=0, held=0):
        with db.get_connection(self.db_path) as conn:
            conn.execute(
                "INSERT INTO wallets (user_id, available_units, held_units)"
                " VALUES (?, ?, ?)",
                (user_id, available, held),
            )

    def insert_ledger(self, user_id, *, entry_type="credit", amount=100,
                      available_delta=None, held_delta=None,
                      reference_type="task", reference_id=None,
                      idempotency_key=None, actor_user_id=None,
                      rate_usdt_egp=None, metadata=None, currency="USDT"):
        if reference_id is None:
            reference_id = self.next_ref()
        if entry_type in VALID_DELTAS:
            default_ad, default_hd = VALID_DELTAS[entry_type](amount)
        else:  # invalid entry_type: sensible fallback, CHECK still rejects
            default_ad, default_hd = amount, 0
        ad = default_ad if available_delta is None else available_delta
        hd = default_hd if held_delta is None else held_delta
        with db.get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO ledger (
                       user_id, entry_type, amount_units, available_delta,
                       held_delta, currency, reference_type, reference_id,
                       idempotency_key, actor_user_id, rate_usdt_egp, metadata
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, entry_type, amount, ad, hd, currency,
                 reference_type, reference_id, idempotency_key,
                 actor_user_id, rate_usdt_egp, metadata),
            )

    def insert_withdrawal(self, user_id, *, request_id=None,
                          method="vodafone_cash", status="pending",
                          amount_egp_minor=1000, fee_egp_minor=100,
                          amount_native_minor=1000, fee_native_minor=100,
                          native_unit="EGP", rate_usdt_egp=None,
                          wallet_rate_usdt_egp="50.000000",
                          rate_captured_at="2026-09-22 12:00:00",
                          rate_provider="manual"):
        if request_id is None:
            request_id = self.next_ref()
        with db.get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO withdrawal_requests (
                       request_id, user_id, method, amount_egp_minor,
                       fee_egp_minor, amount_native_minor, fee_native_minor,
                       native_unit, rate_usdt_egp, wallet_rate_usdt_egp,
                       rate_captured_at, rate_provider, status
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, user_id, method, amount_egp_minor,
                 fee_egp_minor, amount_native_minor, fee_native_minor,
                 native_unit, rate_usdt_egp, wallet_rate_usdt_egp,
                 rate_captured_at, rate_provider, status),
            )

    def downgrade_to_pre_mt1(self):
        """Drop the MT-1 tables so the DB matches the pre-MT-1 shape."""
        with db.get_connection(self.db_path) as conn:
            for table in ("withdrawal_requests", "ledger", "wallets"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")


# ── 1–4: table creation / idempotent init ────────────────────────────


class TestTableCreation(WalletSchemaTestBase):

    def test_fresh_database_creates_wallets_table(self):
        """1. Fresh database creates wallets table."""
        self.assertIn("wallets", self.table_names())

    def test_fresh_database_creates_ledger_table(self):
        """2. Fresh database creates ledger table."""
        self.assertIn("ledger", self.table_names())

    def test_fresh_database_creates_withdrawal_requests_table(self):
        """3. Fresh database creates withdrawal_requests table."""
        self.assertIn("withdrawal_requests", self.table_names())

    def test_running_init_db_twice_is_safe(self):
        """4. init_db() is idempotent: same tables, same indexes, no errors."""
        tables_before = self.table_names()
        indexes_before = self.index_names()
        db.init_db(self.db_path)
        db.init_db(self.db_path)
        self.assertEqual(self.table_names(), tables_before)
        self.assertEqual(self.index_names(), indexes_before)
        for table in NEW_TABLES:
            self.assertIn(table, self.table_names())


# ── 5–7, 37: migration safety on an existing database ────────────────


class TestMigrationSafety(WalletSchemaTestBase):

    def test_existing_users_survive_migration(self):
        """5. Users that pre-date the wallet tables remain unchanged."""
        self.add_user(1)
        self.add_user(2)
        self.downgrade_to_pre_mt1()          # DB now looks pre-MT-1
        db.init_db(self.db_path)             # migration runs on it
        with db.get_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT user_id, username FROM users ORDER BY user_id"
            ).fetchall()
        self.assertEqual(
            [(r["user_id"], r["username"]) for r in rows],
            [(1, "user1"), (2, "user2")],
        )
        # migration is additive only: new tables now exist
        for table in NEW_TABLES:
            self.assertIn(table, self.table_names())
        # and no wallet rows were backfilled
        with db.get_connection(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM wallets").fetchone()
        self.assertEqual(count["c"], 0)

    def test_existing_language_values_survive_migration(self):
        """6. users.language values are preserved by the migration."""
        expected = {1: "ar", 2: "en", 3: "ru", 4: "fa"}
        for uid, lang in expected.items():
            self.add_user(uid)
            self.assertTrue(db.set_user_language(uid, lang))
        self.downgrade_to_pre_mt1()
        db.init_db(self.db_path)
        for uid, lang in expected.items():
            self.assertEqual(db.get_user_language(uid), lang)

    def test_existing_referral_relationships_survive_migration(self):
        """7. referred_by attribution survives the migration."""
        self.add_user(1)
        self.assertTrue(
            db.register_user(2, "user2", "User 2", referred_by=1)
        )
        self.downgrade_to_pre_mt1()
        db.init_db(self.db_path)
        self.assertEqual(db.get_user(2)["referred_by"], 1)
        self.assertEqual(db.get_referral_count(1), 1)

    def test_existing_data_remains_intact_after_migration(self):
        """37. Channels, tasks and user_tasks are untouched as well."""
        self.add_user(1)
        db.save_channel(
            Channel(slug="main", channel_id=-100123,
                    username="main_ch", title="Main"),
            self.db_path,
        )
        task_id = db.create_task(
            "Title", "Desc", "deterministic", 50, db_path=self.db_path
        )
        db.create_user_task(1, task_id, self.db_path)

        self.downgrade_to_pre_mt1()
        db.init_db(self.db_path)

        channel = db.get_channel_from_db("main", self.db_path)
        self.assertIsNotNone(channel)
        self.assertEqual(channel.channel_id, -100123)
        task = db.get_task(task_id)
        self.assertEqual(task["reward"], 50)
        user_task = db.get_user_task(1, task_id, self.db_path)
        self.assertEqual(user_task["status"], db.USER_TASK_STATUS_AVAILABLE)
        self.assertEqual(db.get_user_language(1), None)
        for table in NEW_TABLES:
            self.assertIn(table, self.table_names())


# ── 8–11: foreign keys (PRAGMA foreign_keys = ON preserved) ──────────


class TestForeignKeys(WalletSchemaTestBase):

    def test_wallets_user_id_references_users(self):
        """8. wallets.user_id → users(user_id)."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_wallet(999999)
        self.add_user(1)
        self.insert_wallet(1)   # valid FK accepted

    def test_ledger_user_id_references_users(self):
        """9. ledger.user_id → users(user_id)."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_ledger(999999)
        self.add_user(1)
        self.insert_ledger(1)   # valid FK accepted

    def test_ledger_actor_user_id_references_users(self):
        """10. ledger.actor_user_id → users(user_id)."""
        self.add_user(1)
        self.add_user(2)
        self.insert_ledger(1, actor_user_id=2)      # valid actor
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_ledger(1, actor_user_id=888888)

    def test_withdrawal_user_id_references_users(self):
        """11. withdrawal_requests.user_id → users(user_id)."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_withdrawal(999999)
        self.add_user(1)
        self.insert_withdrawal(1)   # valid FK accepted


# ── 12–13: wallet money checks ───────────────────────────────────────


class TestWalletConstraints(WalletSchemaTestBase):

    def test_wallet_available_units_cannot_be_negative(self):
        """12. CHECK (available_units >= 0)."""
        self.add_user(1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_wallet(1, available=-1)
        self.insert_wallet(1, available=0)   # zero is allowed

    def test_wallet_held_units_cannot_be_negative(self):
        """13. CHECK (held_units >= 0)."""
        self.add_user(1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_wallet(1, held=-1)
        self.insert_wallet(1, held=0)        # zero is allowed


# ── 14–25, 41–43: ledger constraints ─────────────────────────────────


class TestLedgerConstraints(WalletSchemaTestBase):

    def test_ledger_amount_units_must_be_positive(self):
        """14. CHECK (amount_units > 0) — 0 and negative rejected."""
        self.add_user(1)
        for bad in (0, -1, -100):
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_ledger(1, amount=bad)
        self.insert_ledger(1, amount=1)      # positive accepted

    def test_ledger_rejects_invalid_entry_type(self):
        """15. entry_type CHECK: exactly credit/debit/hold/release/settlement."""
        self.add_user(1)
        for entry_type in ENTRY_TYPES:
            self.insert_ledger(1, entry_type=entry_type)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_ledger(1, entry_type="transfer")

    def test_ledger_rejects_invalid_currency(self):
        """16. currency CHECK = 'USDT' (no EGP balance entries)."""
        self.add_user(1)
        self.insert_ledger(1, currency="USDT")
        for bad in ("EGP", "usdt", ""):
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_ledger(1, currency=bad)

    def test_ledger_rejects_invalid_reference_type(self):
        """17. reference_type CHECK covers exactly the approved values."""
        self.add_user(1)
        for reference_type in REFERENCE_TYPES:
            self.insert_ledger(1, reference_type=reference_type)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_ledger(1, reference_type="prize")

    def test_ledger_enforces_credit_delta_semantics(self):
        """18. credit: available_delta = +amount, held_delta = 0."""
        self.add_user(1)
        self.insert_ledger(1, entry_type="credit", amount=500)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_ledger(1, entry_type="credit", amount=500,
                               available_delta=499)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_ledger(1, entry_type="credit", amount=500,
                               held_delta=500)

    def test_ledger_enforces_debit_delta_semantics(self):
        """19. debit: available_delta = -amount, held_delta = 0."""
        self.add_user(1)
        self.insert_ledger(1, entry_type="debit", amount=200)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_ledger(1, entry_type="debit", amount=200,
                               available_delta=-199)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_ledger(1, entry_type="debit", amount=200,
                               held_delta=-200)

    def test_ledger_enforces_hold_delta_semantics(self):
        """20. hold: available_delta = -amount, held_delta = +amount."""
        self.add_user(1)
        self.insert_ledger(1, entry_type="hold", amount=1100)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_ledger(1, entry_type="hold", amount=1100,
                               available_delta=1100)      # sign flipped
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_ledger(1, entry_type="hold", amount=1100,
                               held_delta=0)              # nothing held

    def test_ledger_enforces_release_delta_semantics(self):
        """21. release: available_delta = +amount, held_delta = -amount."""
        self.add_user(1)
        self.insert_ledger(1, entry_type="release", amount=1100)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_ledger(1, entry_type="release", amount=1100,
                               available_delta=-1100)     # hold semantics
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_ledger(1, entry_type="release", amount=1100,
                               held_delta=0)

    def test_ledger_enforces_settlement_delta_semantics(self):
        """22. settlement: available_delta = 0, held_delta = -amount."""
        self.add_user(1)
        self.insert_ledger(1, entry_type="settlement", amount=1100)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_ledger(1, entry_type="settlement", amount=1100,
                               available_delta=-1100)     # no 2nd deduction
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_ledger(1, entry_type="settlement", amount=1100,
                               held_delta=0)

    def test_duplicate_ledger_reference_and_type_is_rejected(self):
        """23. UNIQUE(reference_type, reference_id, entry_type)."""
        self.add_user(1)
        self.insert_ledger(1, entry_type="hold",
                           reference_type="withdrawal", reference_id="w1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_ledger(1, entry_type="hold",
                               reference_type="withdrawal", reference_id="w1")
        # a *different* entry_type on the same reference is allowed
        self.insert_ledger(1, entry_type="release",
                           reference_type="withdrawal", reference_id="w1")

    def test_duplicate_non_null_idempotency_key_is_rejected(self):
        """24. partial UNIQUE(idempotency_key)."""
        self.add_user(1)
        self.insert_ledger(1, idempotency_key="task_reward:1:1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_ledger(1, idempotency_key="task_reward:1:1")

    def test_null_idempotency_keys_may_coexist(self):
        """25. NULL idempotency keys do not collide."""
        self.add_user(1)
        self.insert_ledger(1, idempotency_key=None)
        self.insert_ledger(1, idempotency_key=None)
        with db.get_connection(self.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM ledger WHERE idempotency_key IS NULL"
            ).fetchone()
        self.assertEqual(count["c"], 2)

    def test_append_only_has_no_triggers(self):
        """41. Append-only is service-layer policy — no triggers invented."""
        with db.get_connection(self.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM sqlite_master "
                "WHERE type = 'trigger' AND tbl_name = 'ledger'"
            ).fetchone()
        self.assertEqual(count["c"], 0)

    def test_ledger_user_audit_index_exists(self):
        """idx_ledger_user (user_id, id DESC) supports history/audit reads."""
        self.assertIn("idx_ledger_user", self.index_names())
        sql = self.index_sql("idx_ledger_user")
        self.assertIn("user_id", sql)
        self.assertIn("id", sql)

    def test_ledger_idempotency_partial_unique_index_exists(self):
        """The idempotency guarantee is a DB index, not app logic."""
        self.assertIn("ux_ledger_idempotency_key", self.index_names())
        sql = self.index_sql("ux_ledger_idempotency_key")
        self.assertIn("UNIQUE", sql.upper())
        self.assertIn("idempotency_key IS NOT NULL", sql)


# ── 26–36: withdrawal_requests constraints ───────────────────────────


class TestWithdrawalConstraints(WalletSchemaTestBase):

    def test_withdrawal_rejects_invalid_method(self):
        """26. method CHECK: vodafone_cash | usdt_bep20 only."""
        self.add_user(1)
        for method in METHODS:
            self.insert_withdrawal(
                1, method=method, status="rejected",
                rate_usdt_egp="50" if method == "usdt_bep20" else None,
                native_unit="USDT" if method == "usdt_bep20" else "EGP",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_withdrawal(1, method="bank_transfer")

    def test_withdrawal_rejects_invalid_native_unit(self):
        """27. native_unit CHECK: EGP | USDT only."""
        self.add_user(1)
        for unit in ("EGP", "USDT"):
            self.insert_withdrawal(1, native_unit=unit, status="rejected")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_withdrawal(1, native_unit="USD")

    def test_withdrawal_rejects_invalid_status(self):
        """28. status CHECK: pending | rejected | completed only."""
        self.add_user(1)
        for status in STATUSES:
            self.insert_withdrawal(1, status=status)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_withdrawal(1, status="cancelled")

    def test_withdrawal_amount_egp_minor_must_be_positive(self):
        """29. CHECK (amount_egp_minor > 0)."""
        self.add_user(1)
        for bad in (0, -1):
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_withdrawal(1, amount_egp_minor=bad)
        self.insert_withdrawal(1, amount_egp_minor=1)

    def test_withdrawal_fee_egp_minor_cannot_be_negative(self):
        """30. CHECK (fee_egp_minor >= 0)."""
        self.add_user(1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_withdrawal(1, fee_egp_minor=-1)
        self.insert_withdrawal(1, fee_egp_minor=0)   # zero fee representable

    def test_withdrawal_amount_native_minor_must_be_positive(self):
        """31. CHECK (amount_native_minor > 0)."""
        self.add_user(1)
        for bad in (0, -5):
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_withdrawal(1, amount_native_minor=bad)
        self.insert_withdrawal(1, amount_native_minor=1)

    def test_withdrawal_fee_native_minor_cannot_be_negative(self):
        """32. CHECK (fee_native_minor >= 0)."""
        self.add_user(1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_withdrawal(1, fee_native_minor=-1)
        self.insert_withdrawal(1, fee_native_minor=0)

    def test_withdrawal_wallet_rate_cannot_be_null(self):
        """33. wallet_rate_usdt_egp NOT NULL; rule-pinned rate may be NULL."""
        self.add_user(1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_withdrawal(1, wallet_rate_usdt_egp=None)
        # Vodafone Cash: existing rules pin no payout rate → NULL allowed,
        # while the wallet-side conversion rate is still required.
        self.insert_withdrawal(1, method="vodafone_cash", rate_usdt_egp=None)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_withdrawal(1, method="vodafone_cash",
                                   rate_usdt_egp=None,
                                   wallet_rate_usdt_egp=None)

    def test_withdrawal_user_cooldown_index_exists(self):
        """34. idx_withdrawals_user_created supports the 24h cooldown lookup."""
        self.assertIn("idx_withdrawals_user_created", self.index_names())
        sql = self.index_sql("idx_withdrawals_user_created")
        self.assertIn("user_id", sql)
        self.assertIn("created_at", sql)
        self.assertIn("DESC", sql.upper())

    def test_at_most_one_pending_withdrawal_per_user(self):
        """35. partial UNIQUE(user_id) WHERE status = 'pending'."""
        self.add_user(1)
        self.add_user(2)
        self.insert_withdrawal(1, status="pending")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_withdrawal(1, status="pending")   # same user blocked
        self.insert_withdrawal(2, status="pending")       # other user fine

    def test_multiple_non_pending_withdrawals_allowed(self):
        """36. rejected/completed rows accumulate freely per user."""
        self.add_user(1)
        self.insert_withdrawal(1, status="rejected")
        self.insert_withdrawal(1, status="rejected")
        self.insert_withdrawal(1, status="completed")
        with db.get_connection(self.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM withdrawal_requests"
            ).fetchone()
        self.assertEqual(count["c"], 3)


# ── 38–40: column types / money precision ────────────────────────────


class TestColumnTypes(WalletSchemaTestBase):

    def test_no_real_monetary_columns_exist(self):
        """38. No REAL/FLOAT money storage anywhere in the new tables."""
        forbidden = {"REAL", "DOUBLE", "FLOAT", "DOUBLE PRECISION"}
        for table in NEW_TABLES:
            for name, col_type in self.columns(table).items():
                self.assertNotIn(
                    col_type, forbidden,
                    f"{table}.{name} must not be {col_type}",
                )

    def test_rate_columns_are_text(self):
        """39. Rates are canonical Decimal strings (TEXT), never REAL."""
        self.assertEqual(self.columns("ledger")["rate_usdt_egp"], "TEXT")
        withdrawals = self.columns("withdrawal_requests")
        self.assertEqual(withdrawals["rate_usdt_egp"], "TEXT")
        self.assertEqual(withdrawals["wallet_rate_usdt_egp"], "TEXT")

    def test_usdt_wallet_fields_are_integer(self):
        """40. USDT/EGP money fields are INTEGER units."""
        wallets = self.columns("wallets")
        self.assertEqual(wallets["available_units"], "INTEGER")
        self.assertEqual(wallets["held_units"], "INTEGER")

        self.assertEqual(self.columns("ledger")["amount_units"], "INTEGER")
        self.assertEqual(self.columns("ledger")["available_delta"], "INTEGER")
        self.assertEqual(self.columns("ledger")["held_delta"], "INTEGER")

        withdrawals = self.columns("withdrawal_requests")
        for field in ("amount_egp_minor", "fee_egp_minor",
                      "amount_native_minor", "fee_native_minor"):
            self.assertEqual(withdrawals[field], "INTEGER", field)


if __name__ == "__main__":
    unittest.main()
