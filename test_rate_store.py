"""
Focused tests for the authoritative manual rate source (MT-ADMIN-26)
====================================================================

Covers the 44 required behaviours:

  Parsing / storage (1-12)
      canonical integer/decimal text, trailing-zero canonicalization,
      rejection of exponent / plus sign / zero / negative / NaN /
      Infinity / malformed input, no float storage ever, provider
      persisted as exactly ``"manual"``.

  Quote (13-23)
      persisted row -> immutable RateQuote, exact Decimal preserved,
      captured_at preserved exactly, approved provider accepted,
      missing / stale / future / bad-provider rows rejected, fresh
      rate accepted, exact TTL boundary pinned (age >= TTL is stale).

  Admin (24-31)
      configured admin can set, non-admin rejected, command rejects
      malformed input, captured_at/updated_at server-generated,
      provider unchangeable from ``"manual"``, replacement is
      atomic, failed update preserves the previous rate.

  Transactions (32-36)
      caller-provided ``connection=`` is used (reads and writes) with
      no hidden second connection and no nested transaction, standalone
      calls keep working, a failed transaction rolls back, sequential
      admin updates are deterministic (last write wins, one row).

  Registration
      ``/setrate`` is registered exactly once in ``bot.main()``.

Regression suites for the existing contracts (rate_quote, withdrawal,
platform_settings, wallet/ledger, bot/admin routing, Mini App/auth)
are run as part of the full suite — no existing test is modified here.

Run:
    python3 -m pytest test_rate_store.py -v
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import config
import db
import rate_admin
import rate_store
from rate_quote import (
    PROVIDER_MANUAL,
    RateQuote,
    RateQuoteError,
    RateValidationError,
    UnknownRateProviderError,
)
from rate_store import (
    InvalidPersistedRateError,
    RATE_TTL_SECONDS,
    RatePermissionError,
    RateStaleError,
    RateStoreError,
    RateUnavailableError,
)

_ADMIN_A = 777001
_ADMIN_B = 777002
_NON_ADMIN = 999

# Fixed reference instants — deterministic staleness maths.
NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)


# ── Test helpers ──────────────────────────────────────────────────────


def _make_update(user_id: int, text: str | None, chat_type: str = "private") -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.type = chat_type
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.text = text
    update.callback_query = None
    return update


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.user_data = {}
    return ctx


class _RateStoreTestCase(unittest.TestCase):
    """Base: fresh isolated SQLite DB + configurable admin list."""

    def setUp(self) -> None:
        test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = test_db.name
        test_db.close()
        self._orig_admins = list(config.ADMINS)
        config.ADMINS[:] = [_ADMIN_A]
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)

    def tearDown(self) -> None:
        config.ADMINS[:] = self._orig_admins
        db.DB_PATH = self._orig_db_path
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)
        for suffix in ("-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                os.unlink(p)

    # ── helpers ──────────────────────────────────────────────────────

    def set_rate(self, value, admin: int = _ADMIN_A, **kwargs):
        kwargs.setdefault("db_path", self.db_path)
        return rate_store.set_rate(value, admin_user_id=admin, **kwargs)

    def get_quote(self, **kwargs):
        kwargs.setdefault("db_path", self.db_path)
        return rate_store.get_current_quote(**kwargs)

    def raw(self, sql: str, params: tuple = ()) -> list:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, params).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def raw_row(self):
        rows = self.raw("SELECT * FROM current_rate WHERE id = 1")
        return rows[0] if rows else None

    def raw_rate_text(self):
        row = self.raw_row()
        return None if row is None else row["rate_usdt_egp"]

    def raw_count(self) -> int:
        return self.raw("SELECT COUNT(*) AS n FROM current_rate")[0]["n"]


# ── 1..12  Rate parsing / storage ─────────────────────────────────────


class TestParsingStorage(_RateStoreTestCase):

    def test_1_integer_rate_persists_canonically(self):
        """1. ``48`` persists as the canonical text ``48``."""
        self.set_rate("48", now=NOW)
        self.assertEqual(self.raw_rate_text(), "48")
        row = self.raw_row()
        self.assertEqual(row["provider"], PROVIDER_MANUAL)
        self.assertIsNotNone(row["captured_at"])
        self.assertEqual(row["updated_by"], _ADMIN_A)
        self.assertIsNotNone(row["updated_at"])

    def test_2_decimal_rate_persists_canonically(self):
        """2. ``48.5`` and ``48.5001`` persist exactly as given."""
        self.set_rate("48.5", now=NOW)
        self.assertEqual(self.raw_rate_text(), "48.5")
        self.set_rate("48.5001", now=NOW)
        self.assertEqual(self.raw_rate_text(), "48.5001")

    def test_3_trailing_zeros_canonicalize(self):
        """3. ``48.50``/``48.500`` are stored as ``48.5`` — never with
        trailing zeros (the canonical text, not the raw input)."""
        self.set_rate("48.50", now=NOW)
        self.assertEqual(self.raw_rate_text(), "48.5")
        self.assertNotEqual(self.raw_rate_text(), "48.50")
        self.set_rate("48.500", now=NOW)
        self.assertEqual(self.raw_rate_text(), "48.5")

    def test_4_exponent_notation_rejected(self):
        """4. Scientific notation is rejected; nothing is written."""
        for bad in ("4.85e1", "1e5", "4E+2"):
            with self.assertRaises(RateValidationError, msg=bad):
                self.set_rate(bad, now=NOW)
        self.assertIsNone(self.raw_rate_text())

    def test_5_plus_sign_rejected(self):
        """5. A leading plus sign is rejected."""
        with self.assertRaises(RateValidationError):
            self.set_rate("+48.5", now=NOW)
        self.assertIsNone(self.raw_rate_text())

    def test_6_zero_rejected(self):
        """6. Zero is rejected (str, Decimal and int alike)."""
        for bad in ("0", "0.0", Decimal("0"), 0):
            with self.assertRaises(RateValidationError, msg=repr(bad)):
                self.set_rate(bad, now=NOW)
        self.assertIsNone(self.raw_rate_text())

    def test_7_negative_rejected(self):
        """7. Negative values are rejected."""
        for bad in ("-48.5", Decimal("-1"), -1):
            with self.assertRaises(RateValidationError, msg=repr(bad)):
                self.set_rate(bad, now=NOW)
        self.assertIsNone(self.raw_rate_text())

    def test_8_nan_rejected(self):
        """8. NaN is rejected (text and Decimal alike)."""
        for bad in ("NaN", Decimal("NaN")):
            with self.assertRaises(RateValidationError, msg=repr(bad)):
                self.set_rate(bad, now=NOW)
        self.assertIsNone(self.raw_rate_text())

    def test_9_infinity_rejected(self):
        """9. Infinity is rejected (text and Decimal alike)."""
        for bad in ("Infinity", "inf", Decimal("Infinity")):
            with self.assertRaises(RateValidationError, msg=repr(bad)):
                self.set_rate(bad, now=NOW)
        self.assertIsNone(self.raw_rate_text())

    def test_10_malformed_input_rejected(self):
        """10. Empty/whitespace/comma/dotted-junk text is rejected."""
        for bad in ("", " 48.5", "48.5 ", "48,5", "abc", "48.5.5", "48.", ".5"):
            with self.assertRaises(RateValidationError, msg=repr(bad)):
                self.set_rate(bad, now=NOW)
        self.assertIsNone(self.raw_rate_text())

    def test_11_no_float_storage(self):
        """11. A Python float/bool can never be the authoritative value,
        and the stored column type is TEXT — never REAL."""
        with self.assertRaises(RateValidationError):
            self.set_rate(48.5, now=NOW)
        with self.assertRaises(RateValidationError):
            self.set_rate(True, now=NOW)
        self.assertIsNone(self.raw_rate_text())
        # After a valid write the column is TEXT (typeof = 'text').
        self.set_rate("48.5", now=NOW)
        kind = self.raw(
            "SELECT typeof(rate_usdt_egp) AS t FROM current_rate WHERE id = 1"
        )[0]["t"]
        self.assertEqual(kind, "text")
        ddl = self.raw(
            "SELECT sql FROM sqlite_master WHERE name = 'current_rate'"
        )[0]["sql"]
        self.assertNotIn("REAL", ddl.upper())

    def test_12_provider_persisted_as_manual(self):
        """12. The persisted provider is exactly ``\"manual\"``."""
        self.set_rate("48.5", now=NOW)
        self.assertEqual(self.raw_row()["provider"], "manual")


# ── 13..23  Quote construction / staleness ────────────────────────────


class TestQuote(_RateStoreTestCase):

    def test_13_persisted_rate_produces_rate_quote(self):
        """13. The persisted row yields a real ``RateQuote``."""
        self.set_rate("48.5", now=NOW)
        quote = self.get_quote(now=NOW + timedelta(seconds=1))
        self.assertIsInstance(quote, RateQuote)
        self.assertEqual(quote.rate_text, "48.5")
        self.assertEqual(quote.provider, "manual")
        self.assertEqual(quote.captured_at, NOW)

    def test_14_quote_is_immutable(self):
        """14. The returned quote is frozen — never a mutable row."""
        self.set_rate("48.5", now=NOW)
        quote = self.get_quote(now=NOW + timedelta(seconds=1))
        self.assertIsInstance(quote, RateQuote)
        self.assertNotIsInstance(quote, sqlite3.Row)
        with self.assertRaises(Exception) as ctx:
            quote.rate_usdt_egp = Decimal("999")
        self.assertIsInstance(
            ctx.exception,
            (AttributeError, TypeError) + (ValueError,),
        )

    def test_15_exact_decimal_preserved(self):
        """15. The exact Decimal value round-trips without rounding."""
        self.set_rate("48.5001", now=NOW)
        quote = self.get_quote(now=NOW + timedelta(seconds=1))
        self.assertIsInstance(quote.rate_usdt_egp, Decimal)
        self.assertEqual(quote.rate_usdt_egp, Decimal("48.5001"))
        self.assertEqual(quote.rate_text, "48.5001")

    def test_16_captured_at_preserved_exactly(self):
        """16. captured_at is the PERSISTED instant — never replaced
        with the read-time 'now'."""
        self.set_rate("48.5", now=NOW)
        read_at = NOW + timedelta(seconds=300)
        quote = self.get_quote(now=read_at)
        self.assertEqual(quote.captured_at, NOW)
        self.assertEqual(quote.captured_at.isoformat(), NOW.isoformat())
        stored = self.raw_row()["captured_at"]
        self.assertEqual(quote.captured_at.isoformat(), stored)

    def test_17_approved_provider_accepted(self):
        """17. The approved provider yields a quote without error."""
        self.set_rate("48.5", now=NOW)
        quote = self.get_quote(now=NOW + timedelta(seconds=1))
        self.assertEqual(quote.provider, PROVIDER_MANUAL)
        self.assertIn(quote.provider, {"manual"})

    def test_18_missing_rate_is_explicitly_unavailable(self):
        """18. No row -> RateUnavailableError (no quote is invented)."""
        with self.assertRaises(RateUnavailableError):
            self.get_quote(now=NOW)

    def test_19_stale_rate_rejected(self):
        """19. Older than TTL -> RateStaleError, never a stale quote."""
        self.set_rate("48.5", now=NOW)
        with self.assertRaises(RateStaleError):
            self.get_quote(now=NOW + timedelta(seconds=RATE_TTL_SECONDS + 1))
        # Stale is an *unavailable* quote for every caller.
        with self.assertRaises(RateUnavailableError):
            self.get_quote(now=NOW + timedelta(seconds=RATE_TTL_SECONDS + 1))

    def test_20_fresh_rate_accepted(self):
        """20. Strictly within the TTL the quote is returned."""
        self.set_rate("48.5", now=NOW)
        quote = self.get_quote(now=NOW + timedelta(seconds=RATE_TTL_SECONDS - 1))
        self.assertIsInstance(quote, RateQuote)
        self.assertEqual(quote.rate_text, "48.5")

    def test_21_exact_ttl_boundary_is_stale(self):
        """21. PINNED boundary: freshness is ``age < TTL`` strictly —
        a quote exactly TTL seconds old is STALE, one second younger
        is fresh."""
        self.set_rate("48.5", now=NOW)
        ttl = timedelta(seconds=RATE_TTL_SECONDS)
        # exactly at the boundary -> stale
        with self.assertRaises(RateStaleError):
            self.get_quote(now=NOW + ttl)
        # one second inside the boundary -> fresh
        quote = self.get_quote(now=NOW + ttl - timedelta(seconds=1))
        self.assertIsInstance(quote, RateQuote)

    def test_22_future_captured_at_rejected(self):
        """22. A future captured_at is invalid persisted data, not a
        quote that stays 'fresh' forever."""
        future = (NOW + timedelta(hours=1)).isoformat()
        self.raw(
            "INSERT INTO current_rate "
            "(id, rate_usdt_egp, provider, captured_at, updated_by, updated_at) "
            "VALUES (1, '48.5', 'manual', ?, ?, ?)",
            (future, _ADMIN_A, NOW.isoformat()),
        )
        with self.assertRaises(InvalidPersistedRateError):
            self.get_quote(now=NOW)

    def test_22b_naive_captured_at_rejected(self):
        """22b. A naive (non-aware) persisted timestamp is rejected."""
        self.raw(
            "INSERT INTO current_rate "
            "(id, rate_usdt_egp, provider, captured_at, updated_by, updated_at) "
            "VALUES (1, '48.5', 'manual', ?, ?, ?)",
            ("2026-09-27 12:00:00", _ADMIN_A, NOW.isoformat()),
        )
        with self.assertRaises(InvalidPersistedRateError):
            self.get_quote(now=NOW)

    def test_23_provider_validation_on_persisted_row(self):
        """23. An unapproved persisted provider is rejected — with the
        exact rate_quote failure as the cause."""
        self.set_rate("48.5", now=NOW)
        self.raw(
            "UPDATE current_rate SET provider = 'exchange' WHERE id = 1"
        )
        with self.assertRaises(InvalidPersistedRateError) as ctx:
            self.get_quote(now=NOW + timedelta(seconds=1))
        self.assertIsInstance(ctx.exception.__cause__, UnknownRateProviderError)
        self.assertIsInstance(ctx.exception, RateQuoteError)


# ── 24..31  Admin setter ──────────────────────────────────────────────


class TestAdminSetter(_RateStoreTestCase):

    def test_24_configured_admin_can_set_rate(self):
        """24. A configured admin (config.is_admin) sets the rate."""
        config.ADMINS[:] = [_ADMIN_A]
        quote = self.set_rate("48.5", admin=_ADMIN_A, now=NOW)
        self.assertEqual(quote.rate_text, "48.5")
        self.assertEqual(self.raw_rate_text(), "48.5")
        self.assertEqual(self.raw_row()["updated_by"], _ADMIN_A)

    def test_25_non_admin_cannot_set_rate(self):
        """25. A non-admin is rejected BEFORE anything is written."""
        with self.assertRaises(RatePermissionError):
            self.set_rate("48.5", admin=_NON_ADMIN, now=NOW)
        self.assertIsNone(self.raw_rate_text())
        # Invalid input from a non-admin still fails on authorization.
        with self.assertRaises(RatePermissionError):
            self.set_rate("garbage", admin=_NON_ADMIN, now=NOW)

    def test_27_captured_at_is_server_generated(self):
        """27. captured_at is generated server-side (aware UTC now)."""
        t0 = datetime.now(timezone.utc)
        self.set_rate("48.5")  # no `now` — server clock
        t1 = datetime.now(timezone.utc)
        stored = datetime.fromisoformat(self.raw_row()["captured_at"])
        self.assertIsNotNone(stored.tzinfo)
        self.assertIsNotNone(stored.utcoffset())
        self.assertLessEqual(t0, stored)
        self.assertLessEqual(stored, t1)

    def test_28_updated_at_is_server_generated(self):
        """28. updated_at is server-generated and consistent with the
        capture instant — never taken from any caller text."""
        self.set_rate("48.5", now=NOW)
        row = self.raw_row()
        updated = datetime.fromisoformat(row["updated_at"])
        captured = datetime.fromisoformat(row["captured_at"])
        self.assertIsNotNone(updated.tzinfo)
        self.assertEqual(updated, captured)  # both = the same server instant
        self.assertEqual(row["updated_at"], NOW.isoformat())

    def test_29_provider_cannot_be_changed_from_manual(self):
        """29. There is no provider input anywhere: the setter has no
        such parameter and every write stays ``\"manual\"``."""
        with self.assertRaises(TypeError):
            self.set_rate(
                "48.5", admin=_ADMIN_A, provider="exchange",
                db_path=self.db_path, now=NOW,
            )
        self.set_rate("48.5", now=NOW)
        config.ADMINS[:] = [_ADMIN_A, _ADMIN_B]
        self.set_rate("49", admin=_ADMIN_B, now=NOW + timedelta(seconds=10))
        self.assertEqual(self.raw_row()["provider"], "manual")

    def test_30_new_rate_replaces_current_rate(self):
        """30. A new rate atomically REPLACES the singleton row."""
        self.set_rate("48.5", now=NOW)
        config.ADMINS[:] = [_ADMIN_A, _ADMIN_B]
        self.set_rate("49", admin=_ADMIN_B, now=NOW + timedelta(seconds=5))
        self.assertEqual(self.raw_count(), 1)
        row = self.raw_row()
        self.assertEqual(row["rate_usdt_egp"], "49")
        self.assertEqual(row["updated_by"], _ADMIN_B)

    def test_31_failed_update_preserves_previous_rate(self):
        """31. A rejected update leaves the previous rate intact."""
        self.set_rate("48.5", now=NOW)
        with self.assertRaises(RateValidationError):
            self.set_rate("1e2", now=NOW + timedelta(seconds=5))
        with self.assertRaises(RatePermissionError):
            self.set_rate("49", admin=_NON_ADMIN, now=NOW + timedelta(seconds=5))
        self.assertEqual(self.raw_rate_text(), "48.5")
        self.assertEqual(self.raw_count(), 1)


# ── 26  Admin command (/setrate) ──────────────────────────────────────


class TestSetRateCommand(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = test_db.name
        test_db.close()
        self._orig_admins = list(config.ADMINS)
        config.ADMINS[:] = [_ADMIN_A]
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)

    def tearDown(self) -> None:
        config.ADMINS[:] = self._orig_admins
        db.DB_PATH = self._orig_db_path
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)
        for suffix in ("-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                os.unlink(p)

    def _rate_text(self):
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT rate_usdt_egp FROM current_rate WHERE id = 1"
            ).fetchone()
            return None if row is None else row[0]
        finally:
            conn.close()

    async def test_26_admin_command_malformed_input_rejected(self):
        """26. ``/setrate`` with missing/garbage/exponent/two-token
        input is rejected and nothing is persisted."""
        for text in ("/setrate", "/setrate abc", "/setrate 1e2",
                     "/setrate +48.5", "/setrate 0", "/setrate 48.5 49"):
            update = _make_update(_ADMIN_A, text)
            await rate_admin.setrate_command(update, _make_context())
            update.message.reply_text.assert_awaited_once()
            reply = update.message.reply_text.call_args[0][0]
            self.assertIn("❌", reply, msg=text)
            self.assertIsNone(self._rate_text(), msg=text)

    async def test_command_non_admin_rejected(self):
        """A non-admin gets the admin-only refusal; nothing written."""
        update = _make_update(_NON_ADMIN, "/setrate 48.5")
        await rate_admin.setrate_command(update, _make_context())
        update.message.reply_text.assert_awaited_once()
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", reply)
        self.assertIsNone(self._rate_text())

    async def test_command_group_chat_stays_silent(self):
        """MT-ADMIN-02: group/channel invocations produce zero replies."""
        update = _make_update(_ADMIN_A, "/setrate 48.5", chat_type="supergroup")
        await rate_admin.setrate_command(update, _make_context())
        update.message.reply_text.assert_not_awaited()
        self.assertIsNone(self._rate_text())

    async def test_admin_command_success_response(self):
        """The success reply shows canonical rate, provider, capture
        instant, TTL and current status — and persists the row."""
        update = _make_update(_ADMIN_A, "/setrate 48.500")
        await rate_admin.setrate_command(update, _make_context())
        update.message.reply_text.assert_awaited_once()
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("48.5", reply)            # canonical rate text
        self.assertIn("manual", reply)          # provider
        self.assertIn("+00:00", reply)          # capture instant (UTC ISO)
        self.assertIn(str(RATE_TTL_SECONDS), reply)  # TTL seconds
        self.assertIn("ساري", reply)            # freshness status
        self.assertEqual(self._rate_text(), "48.5")
        row = sqlite3.connect(self.db_path).execute(
            "SELECT updated_by FROM current_rate WHERE id = 1"
        ).fetchone()
        self.assertEqual(row[0], _ADMIN_A)


# ── 32..36  Transactions / connection ownership ───────────────────────


class TestTransactions(_RateStoreTestCase):

    def test_32_caller_connection_is_used_for_reads(self):
        """32. With ``connection=`` the read runs on THAT connection
        (no other connection is ever opened)."""
        self.set_rate("48.5", now=NOW)
        with db.transaction(self.db_path) as conn:
            with patch(
                "rate_store.db.get_connection",
                side_effect=AssertionError("hidden second connection"),
            ):
                quote = rate_store.get_current_quote(
                    connection=conn, now=NOW + timedelta(seconds=1),
                )
            self.assertEqual(quote.rate_text, "48.5")

    def test_33_caller_connection_used_for_writes_no_nested_transaction(self):
        """33. With ``connection=`` the write runs on THAT connection —
        no hidden connection, no nested db.transaction()."""
        with db.transaction(self.db_path) as conn:
            with patch(
                "rate_store.db.transaction",
                side_effect=AssertionError("nested transaction"),
            ), patch(
                "rate_store.db.get_connection",
                side_effect=AssertionError("hidden second connection"),
            ):
                rate_store.set_rate(
                    "49", admin_user_id=_ADMIN_A,
                    connection=conn, now=NOW,
                )
            row = conn.execute(
                "SELECT rate_usdt_egp FROM current_rate WHERE id = 1"
            ).fetchone()
            self.assertEqual(row[0], "49")  # visible on the SAME conn
        # Committed by the caller's transaction on exit.
        self.assertEqual(self.raw_rate_text(), "49")

    def test_34_standalone_calls_keep_working(self):
        """34. Without a connection the standard db scopes are used —
        the standalone compatibility path is unchanged."""
        self.set_rate("48.5", now=NOW)          # standalone write
        quote = self.get_quote(now=NOW + timedelta(seconds=1))  # standalone read
        self.assertEqual(quote.rate_text, "48.5")

    def test_35_failed_transaction_rolls_back(self):
        """35. A failure inside the caller's transaction rolls back the
        write — the previous rate remains the authoritative row."""
        self.set_rate("48.5", now=NOW)
        with self.assertRaises(RuntimeError):
            with db.transaction(self.db_path) as conn:
                rate_store.set_rate(
                    "49", admin_user_id=_ADMIN_A,
                    connection=conn, now=NOW + timedelta(seconds=5),
                )
                raise RuntimeError("boom")
        self.assertEqual(self.raw_rate_text(), "48.5")
        self.assertEqual(self.raw_count(), 1)

    def test_36_sequential_admin_updates_deterministic(self):
        """36. Repeated admin updates are deterministic: exactly one
        singleton row, last write wins with its own audit columns."""
        config.ADMINS[:] = [_ADMIN_A, _ADMIN_B]
        self.set_rate("48.5", admin=_ADMIN_A, now=NOW)
        self.set_rate("49", admin=_ADMIN_B, now=NOW + timedelta(seconds=10))
        self.set_rate("48.75", admin=_ADMIN_A, now=NOW + timedelta(seconds=20))
        rows = self.raw("SELECT * FROM current_rate")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rate_usdt_egp"], "48.75")
        self.assertEqual(rows[0]["updated_by"], _ADMIN_A)
        self.assertEqual(rows[0]["captured_at"], (NOW + timedelta(seconds=20)).isoformat())
        # Singleton identity is enforced by the schema itself.
        conn = sqlite3.connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO current_rate "
                    "(id, rate_usdt_egp, provider, captured_at, "
                    " updated_by, updated_at) "
                    "VALUES (2, '1', 'manual', 'x', 1, 'x')"
                )
            conn.rollback()
        finally:
            conn.close()


# ── Schema / TTL configuration (startup migration + policy) ───────────


class TestSchemaAndTtl(_RateStoreTestCase):

    def test_fresh_database_creates_dedicated_rate_table(self):
        """init_db creates current_rate with the exact required shape."""
        cols = self.raw("PRAGMA table_info(current_rate)")
        self.assertEqual(
            [c["name"] for c in cols],
            ["id", "rate_usdt_egp", "provider", "captured_at",
             "updated_by", "updated_at"],
        )
        self.assertEqual(
            [c["type"] for c in cols],
            ["INTEGER", "TEXT", "TEXT", "TIMESTAMP", "INTEGER", "TIMESTAMP"],
        )
        self.assertEqual(cols[0]["pk"], 1)          # singleton identity
        self.assertEqual(cols[0]["notnull"], 0)
        for c in cols[1:]:
            self.assertEqual(c["notnull"], 1, c["name"])

    def test_migration_is_additive_and_idempotent(self):
        """Re-running init_db preserves the stored rate (startup-safe)."""
        self.set_rate("48.5", now=NOW)
        db.init_db(self.db_path)
        db.init_db(self.db_path)
        self.assertEqual(self.raw_count(), 1)
        self.assertEqual(self.raw_rate_text(), "48.5")

    def test_default_ttl_is_15_minutes(self):
        """The production default staleness TTL is 900 s = 15 minutes."""
        self.assertEqual(RATE_TTL_SECONDS, 900)

    def test_ttl_is_configurable_per_call(self):
        """``ttl_seconds=`` overrides the default deterministically."""
        self.set_rate("48.5", now=NOW)
        quote = self.get_quote(ttl_seconds=10, now=NOW + timedelta(seconds=9))
        self.assertIsInstance(quote, RateQuote)
        with self.assertRaises(RateStaleError):
            self.get_quote(ttl_seconds=10, now=NOW + timedelta(seconds=11))

    def test_bad_ttl_arguments_rejected(self):
        """A negative/bool/non-int TTL is a store error, not a silently
        different policy."""
        self.set_rate("48.5", now=NOW)
        for bad in (-1, True, 1.5, "900"):
            with self.assertRaises(RateStoreError, msg=repr(bad)):
                self.get_quote(ttl_seconds=bad, now=NOW)

    def test_errors_sit_in_the_rate_hierarchy(self):
        """Unavailable/stale errors are rate-contract failures."""
        self.assertTrue(issubclass(RateStaleError, RateUnavailableError))
        self.assertTrue(issubclass(RateUnavailableError, RateQuoteError))
        self.assertTrue(issubclass(InvalidPersistedRateError, RateQuoteError))


# ── Bot registration ──────────────────────────────────────────────────


class TestBotRegistration(unittest.TestCase):
    """``/setrate`` is wired into main() exactly once."""

    def test_setrate_handler_registered_exactly_once(self):
        import bot as bot_mod
        from telegram.ext import CommandHandler

        captured: list = []
        app = MagicMock()
        app.add_handler = lambda handler, group=None: captured.append(
            (handler, group)
        )
        builder = MagicMock()
        builder.token.return_value.build.return_value = app
        with patch.dict(
            os.environ, {"TELEGRAM_BOT_TOKEN": "12345:TESTTOKEN"}
        ), patch.object(
            bot_mod, "ApplicationBuilder", return_value=builder
        ), patch.object(bot_mod, "run_single_entry"), patch.object(bot_mod, "db"):
            bot_mod.main()

        setrate = [
            h for h, _g in captured
            if isinstance(h, CommandHandler)
            and getattr(h, "commands", None)
            and "setrate" in h.commands
        ]
        self.assertEqual(len(setrate), 1, "/setrate must be registered once")
        self.assertIs(setrate[0].callback, rate_admin.setrate_command)
        # Exactly one setrate registration line exists in main().
        source = open(bot_mod.__file__, encoding="utf-8").read()
        self.assertEqual(source.count('"setrate"'), 1)


if __name__ == "__main__":
    unittest.main()
