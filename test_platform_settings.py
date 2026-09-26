"""
Focused tests — Persistent Admin Platform Settings Foundation (MT-ADMIN-15)
===========================================================================

The platform's mutable business knobs (minimum withdrawal, minimum
deposit, withdrawal fee, advertiser commission) are DATA in the
``platform_settings`` SQLite table instead of Python constants.

Coverage (task list):

  Fresh schema      table + columns, INTEGER-only value, ``typeof()``
                    CHECK rejects REAL/float, primary key on key,
                    the four registered keys
  Migration         pre-MT-ADMIN-15 database gains the table +
                    defaults; existing financial rows untouched;
                    re-running init_db is idempotent and NEVER
                    overwrites an admin-saved value
  Persistence       values survive new connections and a reopened
                    database (restart simulation)
  Get / set         round trip for every key, int and exact-text input
                    agree, overwrite updates updated_by/updated_at
  Missing setting   get -> None, get_required -> SettingNotFoundError,
                    unknown key -> UnknownSettingError everywhere
  Invalid values    float, bool, None, negatives, empty/malformed text,
                    scientific notation, >8 dp USDT, >2 dp percent,
                    out-of-range commission — nothing is written
  Precision         sub-0.01 USDT values are exact (500000 = 0.005,
                    1 = 0.00000001) and survive a reopen
  Commission        30 % stored exactly as 3000 bp, scale documented
                    (COMMISSION_SCALE = 10000 = 100 %)
  Admin auth        non-admin mutation rejected via config.is_admin
                    (the ONE existing authorization model), reads stay
                    ungated, no public mutation path
  Transactions      conn= inside db.transaction() commits/rolls back
                    as one unit; implicit nested transaction rejected
  No float          AST scan of the module source (no float literals,
                    no float() calls), schema has no REAL column, no
                    round() in the settings implementation

Run:
    python -m pytest test_platform_settings.py -v
"""

from __future__ import annotations

import ast
import inspect
import os
import sqlite3
import tempfile
import unittest

import db
import config
import platform_settings as ps


# ── Identities ─────────────────────────────────────────────────────────

ADMIN_A = 111111
STRANGER = 999999

UNIT_KEYS = (
    ps.MINIMUM_WITHDRAWAL_UNITS,
    ps.MINIMUM_DEPOSIT_UNITS,
    ps.WITHDRAWAL_FEE_UNITS,
)
ALL_KEYS = UNIT_KEYS + (ps.ADVERTISER_COMMISSION,)

USDT_SCALE = 100_000_000


class SettingsTestBase(unittest.TestCase):
    """Temp-DB fixture + raw-connection (restart) readers."""

    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)
        self.addCleanup(self._restore_db)

        self._orig_admins = list(config.ADMINS)
        config.ADMINS[:] = [ADMIN_A]
        self.addCleanup(self._restore_admins)

    def _restore_db(self) -> None:
        db.DB_PATH = self._orig_db_path
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    def _restore_admins(self) -> None:
        config.ADMINS[:] = self._orig_admins

    # ── raw readers: fresh connection == restart simulation ────────────

    def _raw(self, sql: str, params: tuple = ()) -> list:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def raw_value(self, key: str):
        rows = self._raw(
            "SELECT value FROM platform_settings WHERE key = ?", (key,)
        )
        return rows[0]["value"] if rows else None

    def raw_all(self) -> dict:
        return {r["key"]: r["value"] for r in self._raw(
            "SELECT key, value FROM platform_settings ORDER BY key"
        )}

    def table_names(self) -> set:
        return {r["name"] for r in self._raw(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}

    def columns(self, table: str) -> dict:
        conn = sqlite3.connect(self.db_path)
        try:
            return {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})")}
        finally:
            conn.close()

    def set_as_admin(self, key, value, conn=None):
        return ps.set_setting(
            key, value, admin_user_id=ADMIN_A, conn=conn,
            db_path=None if conn else self.db_path,
        )


# ── 1. Fresh schema ────────────────────────────────────────────────────


class TestFreshSchema(SettingsTestBase):

    def test_fresh_database_creates_platform_settings(self):
        """1. Fresh database creates the platform_settings table."""
        self.assertIn("platform_settings", self.table_names())

    def test_columns_and_types(self):
        """2. key TEXT PK, value INTEGER, audit columns — no REAL."""
        cols = self.columns("platform_settings")
        self.assertEqual(
            list(cols),
            ["key", "value", "updated_by", "created_at", "updated_at"],
        )
        self.assertEqual(cols["key"][2], "TEXT")
        self.assertEqual(cols["value"][2], "INTEGER")
        self.assertEqual(cols["key"][5], 1)  # primary key ordinal
        for name, col in cols.items():
            self.assertNotEqual(col[2].upper(), "REAL", name)

    def test_value_check_rejects_float(self):
        """3. typeof(value)='integer' keeps a float out of the table."""
        conn = sqlite3.connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO platform_settings (key, value) "
                    "VALUES ('minimum_deposit_units', 1.5)"
                )
            conn.rollback()
        finally:
            conn.close()

    def test_registered_keys_are_exactly_the_required_four(self):
        """4. Exactly the four required settings are registered."""
        self.assertEqual(set(ps.REGISTERED_SETTINGS), set(ALL_KEYS))
        self.assertEqual(len(ps.REGISTERED_SETTINGS), 4)

    def test_fresh_database_seeds_only_defined_defaults(self):
        """5. Commission 30% seeded; no invented monetary defaults."""
        self.assertEqual(
            ps.get_setting(ps.ADVERTISER_COMMISSION, db_path=self.db_path),
            ps.COMMISSION_DEFAULT,
        )
        for key in UNIT_KEYS:
            self.assertIsNone(ps.get_setting(key, db_path=self.db_path))

    def test_key_is_primary_key(self):
        """6. Duplicate keys are rejected by the schema."""
        conn = sqlite3.connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO platform_settings (key, value) "
                    "VALUES (?, 1)",
                    (ps.ADVERTISER_COMMISSION,),
                )
            conn.rollback()
        finally:
            conn.close()

    def test_init_db_twice_is_idempotent(self):
        """7. Re-running init_db changes nothing (tables + values)."""
        tables_before = self.table_names()
        values_before = self.raw_all()
        db.init_db(self.db_path)
        db.init_db(self.db_path)
        self.assertEqual(self.table_names(), tables_before)
        self.assertEqual(self.raw_all(), values_before)


# ── 2. Migration ───────────────────────────────────────────────────────


class TestMigration(SettingsTestBase):

    def _downgrade(self) -> None:
        """Make the database look pre-MT-ADMIN-15."""
        with db.get_connection(self.db_path) as conn:
            conn.execute("DROP TABLE IF EXISTS platform_settings")

    def _seed_financial_rows(self) -> None:
        with db.get_connection(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, username) "
                "VALUES (7, 'worker')"
            )
            conn.execute(
                "INSERT INTO wallets (user_id, available_units, held_units) "
                "VALUES (7, 123456, 7)"
            )
            conn.execute(
                "INSERT INTO ledger (user_id, entry_type, amount_units, "
                "available_delta, held_delta, reference_type, reference_id) "
                "VALUES (7, 'credit', 123456, 123456, 0, 'deposit', 'd1')"
            )
            conn.execute(
                "INSERT INTO payment_methods (category, display_name, "
                "asset, provider, destination, created_by, updated_by) "
                "VALUES ('crypto', 'USDT', 'USDT', 'x', 'TABC', 1, 1)"
            )

    def test_existing_database_gains_table_and_defaults(self):
        """8. A pre-existing DB migrates: table + seeded default."""
        self._downgrade()
        self.assertNotIn("platform_settings", self.table_names())
        db.init_db(self.db_path)
        self.assertIn("platform_settings", self.table_names())
        self.assertEqual(
            self.raw_value(ps.ADVERTISER_COMMISSION), ps.COMMISSION_DEFAULT
        )

    def test_migration_preserves_financial_data(self):
        """9. Migration never destroys or rewrites financial rows."""
        self._seed_financial_rows()
        self._downgrade()
        db.init_db(self.db_path)
        wallets = self._raw("SELECT * FROM wallets")
        ledger = self._raw("SELECT * FROM ledger")
        methods = self._raw("SELECT * FROM payment_methods")
        self.assertEqual(len(wallets), 1)
        self.assertEqual(wallets[0]["available_units"], 123456)
        self.assertEqual(wallets[0]["held_units"], 7)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["amount_units"], 123456)
        self.assertEqual(len(methods), 1)

    def test_migration_is_idempotent_across_runs(self):
        """10. Repeated init_db on a migrated DB is a no-op."""
        self._downgrade()
        db.init_db(self.db_path)
        first = (self.table_names(), self.raw_all())
        db.init_db(self.db_path)
        db.init_db(self.db_path)
        self.assertEqual(first, (self.table_names(), self.raw_all()))

    def test_admin_saved_value_survives_migration(self):
        """11. INSERT OR IGNORE never overwrites an admin's value."""
        self.set_as_admin(ps.ADVERTISER_COMMISSION, 4500)
        db.init_db(self.db_path)
        db.init_db(self.db_path)
        self.assertEqual(
            self.raw_value(ps.ADVERTISER_COMMISSION), 4500
        )

    def test_recreated_table_reseeds_only_missing_rows(self):
        """12. Dropping the table and re-migrating reseeds the default."""
        self.set_as_admin(ps.ADVERTISER_COMMISSION, 1200)
        self._downgrade()
        db.init_db(self.db_path)
        self.assertEqual(
            self.raw_value(ps.ADVERTISER_COMMISSION), ps.COMMISSION_DEFAULT
        )
        for key in UNIT_KEYS:
            self.assertIsNone(self.raw_value(key))


# ── 3. Persistence across connections / restarts ───────────────────────


class TestPersistence(SettingsTestBase):

    def test_value_survives_a_fresh_connection(self):
        """13. Written value is visible to a brand-new connection."""
        self.set_as_admin(ps.MINIMUM_DEPOSIT_UNITS, 250_000)
        self.assertEqual(self.raw_value(ps.MINIMUM_DEPOSIT_UNITS), 250_000)

    def test_value_survives_a_reopened_database(self):
        """14. Restart simulation: DB_PATH reopened from scratch."""
        self.set_as_admin(ps.WITHDRAWAL_FEE_UNITS, 90_000)
        db.DB_PATH = self._orig_db_path  # "process restart"
        db.DB_PATH = self.db_path
        self.assertEqual(
            ps.get_setting(ps.WITHDRAWAL_FEE_UNITS, db_path=self.db_path),
            90_000,
        )

    def test_settings_are_not_cached_in_memory(self):
        """15. Every read goes to SQLite (no module-level cache)."""
        self.set_as_admin(ps.MINIMUM_WITHDRAWAL_UNITS, 1)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE platform_settings SET value = ? WHERE key = ?",
                (2, ps.MINIMUM_WITHDRAWAL_UNITS),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(
            ps.get_setting(
                ps.MINIMUM_WITHDRAWAL_UNITS, db_path=self.db_path
            ),
            2,
        )


# ── 4. Get / set ───────────────────────────────────────────────────────


class TestGetSet(SettingsTestBase):

    def test_round_trip_for_every_key(self):
        """16. Each registered key stores and returns its exact value."""
        values = {
            ps.MINIMUM_WITHDRAWAL_UNITS: 1_000_000_000,
            ps.MINIMUM_DEPOSIT_UNITS: 100_000_000,
            ps.WITHDRAWAL_FEE_UNITS: 10_000_000,
            ps.ADVERTISER_COMMISSION: 2500,
        }
        for key, value in values.items():
            self.assertEqual(self.set_as_admin(key, value), value)
            self.assertEqual(
                ps.get_setting(key, db_path=self.db_path), value
            )

    def test_int_and_exact_text_agree(self):
        """17. Canonical int input == exact decimal text input."""
        self.assertEqual(
            self.set_as_admin(ps.MINIMUM_DEPOSIT_UNITS, 500_000),
            self.set_as_admin(ps.MINIMUM_DEPOSIT_UNITS, "0.005"),
        )
        self.assertEqual(
            ps.get_setting(ps.MINIMUM_DEPOSIT_UNITS, db_path=self.db_path),
            500_000,
        )

    def test_overwrite_updates_actor_and_value(self):
        """18. A second set replaces the value and stamps the admin."""
        self.set_as_admin(ps.WITHDRAWAL_FEE_UNITS, 100)
        self.set_as_admin(ps.WITHDRAWAL_FEE_UNITS, 200)
        rows = self._raw(
            "SELECT value, updated_by FROM platform_settings WHERE key = ?",
            (ps.WITHDRAWAL_FEE_UNITS,),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 200)
        self.assertEqual(rows[0]["updated_by"], ADMIN_A)

    def test_list_settings_returns_only_configured_values(self):
        """19. list_settings exposes configured rows, nothing else."""
        self.set_as_admin(ps.MINIMUM_DEPOSIT_UNITS, 7)
        listed = ps.list_settings(db_path=self.db_path)
        self.assertEqual(
            listed,
            {
                ps.ADVERTISER_COMMISSION: ps.COMMISSION_DEFAULT,
                ps.MINIMUM_DEPOSIT_UNITS: 7,
            },
        )

    def test_seeded_default_is_readable_through_get_required(self):
        """20. Seeded commission satisfies get_required_setting."""
        self.assertEqual(
            ps.get_required_setting(
                ps.ADVERTISER_COMMISSION, db_path=self.db_path
            ),
            3000,
        )


# ── 5. Missing / unknown setting behaviour ─────────────────────────────


class TestMissingAndUnknown(SettingsTestBase):

    def test_get_returns_none_when_never_set(self):
        """21. An unconfigured key reads as None, not as zero."""
        for key in UNIT_KEYS:
            self.assertIsNone(ps.get_setting(key, db_path=self.db_path))

    def test_get_required_raises_when_never_set(self):
        """22. get_required_setting never falls back silently."""
        for key in UNIT_KEYS:
            with self.assertRaises(ps.SettingNotFoundError):
                ps.get_required_setting(key, db_path=self.db_path)

    def test_get_required_returns_configured_value(self):
        """23. Once set, get_required_setting returns the value."""
        self.set_as_admin(ps.MINIMUM_WITHDRAWAL_UNITS, 42)
        self.assertEqual(
            ps.get_required_setting(
                ps.MINIMUM_WITHDRAWAL_UNITS, db_path=self.db_path
            ),
            42,
        )

    def test_unknown_key_rejected_everywhere(self):
        """24. Unknown keys never read, write or validate."""
        for call in (
            lambda: ps.get_setting("nope", db_path=self.db_path),
            lambda: ps.get_required_setting("nope", db_path=self.db_path),
            lambda: ps.set_setting(
                "nope", 1, admin_user_id=ADMIN_A, db_path=self.db_path
            ),
            lambda: ps.parse_setting_value("nope", 1),
        ):
            with self.assertRaises(ps.UnknownSettingError):
                call()
        self.assertNotIn("nope", self.raw_all())

    def test_non_string_key_rejected(self):
        """25. Non-string keys are unknown, not lookups."""
        for key in (None, 1, ("a",), ps.SETTINGS):
            with self.assertRaises(ps.UnknownSettingError):
                ps.get_spec(key)

    def test_error_types_share_the_base_class(self):
        """26. All failures are PlatformSettingError subclasses."""
        for exc in (
            ps.UnknownSettingError,
            ps.SettingValidationError,
            ps.SettingNotFoundError,
            ps.SettingPermissionError,
        ):
            self.assertTrue(issubclass(exc, ps.PlatformSettingError))
            self.assertTrue(issubclass(exc, Exception))


# ── 6. Invalid values ──────────────────────────────────────────────────


class TestInvalidValues(SettingsTestBase):

    def assertRejected(self, key, value) -> None:
        before = self.raw_all()
        with self.assertRaises(ps.SettingValidationError):
            self.set_as_admin(key, value)
        self.assertEqual(self.raw_all(), before)

    def test_float_rejected(self):
        """27. float input is never accepted (no silent conversion)."""
        self.assertRejected(ps.MINIMUM_DEPOSIT_UNITS, 0.005)
        self.assertRejected(ps.ADVERTISER_COMMISSION, 30.0)

    def test_bool_and_none_rejected(self):
        """28. bool/None are not values."""
        for bad in (True, False, None):
            self.assertRejected(ps.MINIMUM_DEPOSIT_UNITS, bad)
            self.assertRejected(ps.ADVERTISER_COMMISSION, bad)

    def test_unsupported_types_rejected(self):
        """29. Lists/dicts/objects are rejected deterministically."""
        for bad in ([1], {"a": 1}, object()):
            self.assertRejected(ps.MINIMUM_DEPOSIT_UNITS, bad)

    def test_negative_values_rejected(self):
        """30. Negative ints and negative text are rejected."""
        self.assertRejected(ps.MINIMUM_WITHDRAWAL_UNITS, -1)
        self.assertRejected(ps.MINIMUM_WITHDRAWAL_UNITS, "-1")
        self.assertRejected(ps.ADVERTISER_COMMISSION, -100)
        self.assertRejected(ps.ADVERTISER_COMMISSION, "-1")

    def test_empty_and_malformed_text_rejected(self):
        """31. Empty / non-numeric / scientific text is rejected."""
        for bad in ("", "   ", "abc", "1e-8", "1,5", "1.2.3", ".5", "30.", "٣٠x"):
            self.assertRejected(ps.MINIMUM_DEPOSIT_UNITS, bad)
            self.assertRejected(ps.ADVERTISER_COMMISSION, bad)

    def test_more_than_eight_decimals_rejected(self):
        """32. USDT amounts beyond 8 dp are rejected, never rounded."""
        self.assertRejected(ps.MINIMUM_DEPOSIT_UNITS, "0.000000001")
        self.assertRejected(ps.WITHDRAWAL_FEE_UNITS, "1.123456789")

    def test_percent_beyond_two_decimals_rejected(self):
        """33. Commission text beyond 0.01% precision is rejected."""
        self.assertRejected(ps.ADVERTISER_COMMISSION, "30.005")
        self.assertRejected(ps.ADVERTISER_COMMISSION, "30.0001")

    def test_out_of_range_commission_rejected(self):
        """34. Commission is bounded to 0..100% (0..10000 bp)."""
        self.assertRejected(ps.ADVERTISER_COMMISSION, 10_001)
        self.assertRejected(ps.ADVERTISER_COMMISSION, "101")
        self.assertRejected(ps.ADVERTISER_COMMISSION, ps.COMMISSION_SCALE + 1)
        # 0 and 100% are valid boundaries.
        self.assertEqual(
            self.set_as_admin(ps.ADVERTISER_COMMISSION, 0), 0
        )
        self.assertEqual(
            self.set_as_admin(ps.ADVERTISER_COMMISSION, "100"), 10_000
        )

    def test_int_overflow_rejected(self):
        """35. Values beyond signed SQLite INTEGER are rejected."""
        self.assertRejected(
            ps.MINIMUM_DEPOSIT_UNITS, 9_223_372_036_854_775_808
        )

    def test_authorization_runs_before_validation(self):
        """36. A non-admin never learns whether the value was valid."""
        with self.assertRaises(ps.SettingPermissionError):
            ps.set_setting(
                ps.ADVERTISER_COMMISSION,
                "not-a-number",
                admin_user_id=STRANGER,
                db_path=self.db_path,
            )


# ── 7. Atomic-unit precision below 0.01 USDT ───────────────────────────


class TestPrecision(SettingsTestBase):

    def test_sub_cent_value_is_exact(self):
        """37. 0.005 USDT == 500,000 atomic units (below 1 cent)."""
        self.assertEqual(
            self.set_as_admin(ps.MINIMUM_DEPOSIT_UNITS, "0.005"), 500_000
        )
        self.assertEqual(
            ps.get_setting(ps.MINIMUM_DEPOSIT_UNITS, db_path=self.db_path),
            500_000,
        )

    def test_smallest_representable_value(self):
        """38. 0.00000001 USDT == exactly 1 atomic unit."""
        self.assertEqual(
            self.set_as_admin(ps.MINIMUM_WITHDRAWAL_UNITS, "0.00000001"), 1
        )
        self.assertEqual(
            self.raw_value(ps.MINIMUM_WITHDRAWAL_UNITS), 1
        )

    def test_zero_is_a_valid_configuration(self):
        """39. 0 (no minimum) is exact and allowed."""
        self.assertEqual(
            self.set_as_admin(ps.WITHDRAWAL_FEE_UNITS, "0"), 0
        )
        self.assertEqual(
            ps.get_required_setting(
                ps.WITHDRAWAL_FEE_UNITS, db_path=self.db_path
            ),
            0,
        )

    def test_arbitrary_precision_round_trip(self):
        """40. 1234.56789012 USDT is stored bit-exact."""
        expected = 123_456_789_012
        self.assertEqual(
            self.set_as_admin(
                ps.MINIMUM_DEPOSIT_UNITS, "1234.56789012"
            ),
            expected,
        )
        self.assertEqual(self.raw_value(ps.MINIMUM_DEPOSIT_UNITS), expected)
        self.assertEqual(
            ps.get_setting(ps.MINIMUM_DEPOSIT_UNITS, db_path=self.db_path),
            expected,
        )

    def test_arabic_indic_digits_normalized(self):
        """41. Arabic-Indic input parses to the same exact units."""
        self.assertEqual(
            self.set_as_admin(ps.MINIMUM_DEPOSIT_UNITS, "٠.٠٠٥"), 500_000
        )


# ── 8. Advertiser commission representation ────────────────────────────


class TestCommission(SettingsTestBase):

    def test_default_is_exactly_30_percent(self):
        """42. 30% == 3000 bp under the documented scale."""
        self.assertEqual(ps.COMMISSION_SCALE, 10_000)
        self.assertEqual(
            ps.COMMISSION_DEFAULT,
            ps.COMMISSION_SCALE * 30 // 100,
        )
        self.assertEqual(
            self.raw_value(ps.ADVERTISER_COMMISSION), 3000
        )

    def test_scale_is_explicit_integer(self):
        """43. The scale is an exact int: 10000 bp == 100%."""
        self.assertIsInstance(ps.COMMISSION_SCALE, int)
        self.assertNotIsInstance(ps.COMMISSION_SCALE, float)
        self.assertEqual(ps.COMMISSION_SCALE, 100 * 100)

    def test_percent_text_converts_exactly(self):
        """44. '30' -> 3000 bp, '30.5' -> 3050 bp, '0.5' -> 50 bp."""
        self.assertEqual(
            ps.parse_setting_value(ps.ADVERTISER_COMMISSION, "30"), 3000
        )
        self.assertEqual(
            ps.parse_setting_value(ps.ADVERTISER_COMMISSION, "30.5"), 3050
        )
        self.assertEqual(
            ps.parse_setting_value(ps.ADVERTISER_COMMISSION, "0.5"), 50
        )
        self.assertEqual(
            ps.parse_setting_value(ps.ADVERTISER_COMMISSION, 3000), 3000
        )

    def test_stored_commission_is_an_int(self):
        """45. The persisted commission value is a Python int."""
        self.set_as_admin(ps.ADVERTISER_COMMISSION, "12.34")
        value = self.raw_value(ps.ADVERTISER_COMMISSION)
        self.assertEqual(value, 1234)
        self.assertIsInstance(value, int)


# ── 9. Admin authorization ─────────────────────────────────────────────


class TestAdminAuthorization(SettingsTestBase):

    def test_non_admin_cannot_mutate(self):
        """46. config.is_admin gates every mutation."""
        self.set_as_admin(ps.ADVERTISER_COMMISSION, 4000)
        with self.assertRaises(ps.SettingPermissionError):
            ps.set_setting(
                ps.ADVERTISER_COMMISSION,
                1000,
                admin_user_id=STRANGER,
                db_path=self.db_path,
            )
        self.assertEqual(
            self.raw_value(ps.ADVERTISER_COMMISSION), 4000
        )

    def test_admin_can_mutate(self):
        """47. A configured admin can set every registered key."""
        for key in ALL_KEYS:
            value = 3000 if key == ps.ADVERTISER_COMMISSION else 1234
            self.assertEqual(self.set_as_admin(key, value), value)

    def test_actor_type_guards(self):
        """48. Missing/invalid actor ids are refused."""
        for actor in (None, True, False, "111111", 0, -1, 1.0):
            with self.assertRaises(ps.SettingPermissionError):
                ps.set_setting(
                    ps.ADVERTISER_COMMISSION,
                    3000,
                    admin_user_id=actor,
                    db_path=self.db_path,
                )

    def test_reads_are_not_admin_gated(self):
        """49. Reading stays open; only mutation is protected."""
        self.assertEqual(
            ps.get_setting(ps.ADVERTISER_COMMISSION, db_path=self.db_path),
            ps.COMMISSION_DEFAULT,
        )
        self.assertEqual(
            ps.list_settings(db_path=self.db_path)[
                ps.ADVERTISER_COMMISSION
            ],
            ps.COMMISSION_DEFAULT,
        )

    def test_authorization_comes_from_config_is_admin(self):
        """50. The module reuses config.is_admin (no second model)."""
        source = inspect.getsource(ps)
        self.assertIn("from config import is_admin", source)
        self.assertNotIn("ADMINS", source)

    def test_no_public_mutation_path(self):
        """51. No route/bot/UI wiring: mutation is service-level only."""
        source = inspect.getsource(ps)
        self.assertNotIn("telegram", source)
        self.assertNotIn("flask", source)
        self.assertNotIn("sqlite3.connect", source)


# ── 10. Transaction compatibility ──────────────────────────────────────


class TestTransactions(SettingsTestBase):

    def test_set_inside_caller_transaction_commits(self):
        """52. conn= joins the caller's transaction and commits."""
        with db.transaction(self.db_path) as conn:
            ps.set_setting(
                ps.MINIMUM_DEPOSIT_UNITS,
                10,
                admin_user_id=ADMIN_A,
                conn=conn,
            )
            # Same connection sees the uncommitted write.
            self.assertEqual(
                ps.get_setting(ps.MINIMUM_DEPOSIT_UNITS, conn=conn), 10
            )
        self.assertEqual(self.raw_value(ps.MINIMUM_DEPOSIT_UNITS), 10)

    def test_rollback_reverts_the_write(self):
        """53. An exception rolls the setting back with the transaction."""
        self.set_as_admin(ps.MINIMUM_DEPOSIT_UNITS, 5)
        with self.assertRaises(RuntimeError):
            with db.transaction(self.db_path) as conn:
                ps.set_setting(
                    ps.MINIMUM_DEPOSIT_UNITS,
                    99,
                    admin_user_id=ADMIN_A,
                    conn=conn,
                )
                raise RuntimeError("boom")
        self.assertEqual(self.raw_value(ps.MINIMUM_DEPOSIT_UNITS), 5)

    def test_multiple_settings_in_one_transaction(self):
        """54. Several settings can be updated atomically."""
        with db.transaction(self.db_path) as conn:
            ps.set_setting(
                ps.MINIMUM_WITHDRAWAL_UNITS,
                1,
                admin_user_id=ADMIN_A,
                conn=conn,
            )
            ps.set_setting(
                ps.WITHDRAWAL_FEE_UNITS,
                2,
                admin_user_id=ADMIN_A,
                conn=conn,
            )
        self.assertEqual(self.raw_value(ps.MINIMUM_WITHDRAWAL_UNITS), 1)
        self.assertEqual(self.raw_value(ps.WITHDRAWAL_FEE_UNITS), 2)

    def test_implicit_nested_transaction_is_rejected(self):
        """55. Writing without conn= inside a transaction cannot nest."""
        with db.transaction(self.db_path):
            with self.assertRaises(db.NestedTransactionError):
                ps.set_setting(
                    ps.ADVERTISER_COMMISSION,
                    1111,
                    admin_user_id=ADMIN_A,
                    db_path=self.db_path,
                )

    def test_failed_validation_leaves_transaction_usable(self):
        """56. A rejected value does not poison the open transaction."""
        with db.transaction(self.db_path) as conn:
            with self.assertRaises(ps.SettingValidationError):
                ps.set_setting(
                    ps.ADVERTISER_COMMISSION,
                    -1,
                    admin_user_id=ADMIN_A,
                    conn=conn,
                )
            ps.set_setting(
                ps.ADVERTISER_COMMISSION,
                2222,
                admin_user_id=ADMIN_A,
                conn=conn,
            )
        self.assertEqual(
            self.raw_value(ps.ADVERTISER_COMMISSION), 2222
        )


# ── 11. No float in the implementation ─────────────────────────────────


class TestNoFloatUsage(unittest.TestCase):

    def test_module_source_contains_no_float(self):
        """57. No float literals, float() calls or float references.

        ``isinstance(value, float)`` guards are allowed — they are how
        the module rejects float input in the first place.
        """
        tree = ast.parse(inspect.getsource(ps))
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

    def test_module_never_rounds(self):
        """58. No round()/quantize() call: invalid input is rejected."""
        tree = ast.parse(inspect.getsource(ps))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name in {"round", "quantize"}:
                self.fail(f"{name}() call at line {node.lineno}")

    def test_schema_has_no_real_column(self):
        """59. The settings table stores no REAL/float column."""
        import tempfile as _tempfile

        handle = _tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        path = handle.name
        handle.close()
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE t (k TEXT PRIMARY KEY, "
                "v INTEGER NOT NULL CHECK (typeof(v) = 'integer'))"
            )
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 't'"
            ).fetchone()[0]
            self.assertNotIn("REAL", sql.upper())
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO t VALUES ('a', 1.5)")
        finally:
            conn.close()
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()
