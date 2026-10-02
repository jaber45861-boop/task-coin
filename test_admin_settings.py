"""MT-ADMIN-39 — Admin Settings Management (focused tests).

Covers the brief's required matrix against the REAL repository
contracts (the four registered ``platform_settings`` keys — the
registry test pins the key set — written ONLY through the existing
admin-only ``set_setting`` contract):

 A. REGISTRY (1-2)
   1 → test_01   settings registered exactly once, ctl: parses
   2 → test_02   settings is no longer a placeholder
 B. AUTHORIZATION (3-7)
   3 → test_03   non-admin blocked before any read
   4 → test_04   group/channel presses are silent
   5 → test_05   callback presses re-check authorization
   6 → test_06   text input re-checks authorization (silent refusal)
   7 → test_07   authorization happens before settings reads
 C. RENDERING (8-12)
   8 → test_08   all four registered settings shown
   9 → test_09   missing value renders "غير مضبوط"
  10 → test_10   no invented defaults for unset keys
  11 → test_11   rate is never duplicated as a setting
  12 → test_12   no secrets / paths / db internals in the UI
 D. CALLBACK GRAMMAR (13-20)
  13 → test_13   ctl:settings parses
  14 → test_14   ctl:settings:edit:<registered-key> parses
  15 → test_15   ctl:settings:confirm:<key> parses
  16 → test_16   ctl:settings:cancel:<key> parses
  17 → test_17   ctl:settings:back parses
  18 → test_18   unknown keys rejected (incl. raw DB rows)
  19 → test_19   malformed callbacks rejected
  20 → test_20   foreign namespaces (wd/dp/pm/mr/atw/mproof/sup)
                 remain untouched
 E. VALIDATION (21-24)
  21 → test_21   the EXISTING validator is invoked
  22 → test_22   invalid value writes nothing
  23 → test_23   invalid value keeps the pending operation
  24 → test_24   valid value reaches the preview (no write yet)
 F. MUTATION (25-33)
  25 → test_25   confirm calls set_setting() exactly once
  26 → test_26   fresh re-read happens before the write
  27 → test_27   authorization happens before the write
  28 → test_28   no transaction / no SQL in the handler
  29 → test_29   wallet not called
  30 → test_30   ledger not called
  31 → test_31   withdrawal not mutated
  32 → test_32   deposit not mutated
  33 → test_33   payment methods not mutated
     → test_33b  ONLY platform_settings changed in the whole flow
 G. IDEMPOTENCY (34-36)
  34 → test_34   duplicate confirm cannot write twice
  35 → test_35   stale confirm is safe (no write, no recreate)
  36 → test_36   cancel causes no write
 H. NAVIGATION (37-38)
  37 → test_37   ctl:settings:back returns the Control Center
  38 → test_38   successful update renders the fresh panel
 I. SECURITY (39-42)
  39 → test_39   no SQL in admin_control.py
  40 → test_40   no secrets / staged values in UI or logs
  41 → test_41   callback payloads stay closed (no values)
  42 → test_42   no process-global dict state for settings
 J. EXTRAS (43+)
  43 → test_43   no handler added during press; group 8 static
  44 → test_44   ^ctl: still exactly once; input static in bot.py
  45-48          users/tasks/admins/dashboard regression
  49             exact display math (USDT units / basis points)
  50             settings read failure degrades per-row safely

Temp databases only; every write goes through the production
``platform_settings.set_setting`` contract.

Run:
    .venv/bin/python -m pytest test_admin_settings.py -v
"""

from __future__ import annotations

import re
import sqlite3
import unittest
from types import SimpleNamespace
from unittest import mock

from telegram import InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

import admin_control
import config
import db
import platform_settings
import rate_store

from admin_control import (
    ADMINS_PANEL_HEADER,
    HEADER,
    MSG_ADMIN_ONLY,
    MSG_INVALID,
    MSG_MODULE_UNAVAILABLE,
    MSG_NO_PENDING,
    SETTINGS_PANEL_HEADER,
    TASKS_PANEL_HEADER,
    TOAST_CANCELLED,
    TOAST_SETTING_UPDATED,
    USERS_PANEL_HEADER,
    build_dashboard_keyboard,
    parse_callback,
)

from test_payment_methods import (
    _answered,
    _callback,
    _edited,
    _reply,
    _run,
    _update,
)
from test_admin_admins import _FakeApplication
from test_admin_control import (
    FINANCIAL_TABLES,
    MUTATION_SPY_TARGETS,
    ControlTestBase,
)
from test_admin_users import _capture_handlers
from test_withdrawal_service import ADMIN_ID

STRANGER = 999_999

# The four REAL registered keys — pinned by
# test_platform_settings.test_registered_keys_are_exactly_the_required_four.
KEYS = (
    "minimum_withdrawal_units",
    "minimum_deposit_units",
    "withdrawal_fee_units",
    "advertiser_commission",
)
MIN_WITHDRAWAL = "minimum_withdrawal_units"
MIN_DEPOSIT = "minimum_deposit_units"
FEE = "withdrawal_fee_units"
COMMISSION = "advertiser_commission"

# Keys the brief's (non-existent) names would have had — they must
# NEVER parse, render or write.
ABSENT_KEYS = (
    "maintenance_mode",
    "min_withdrawal",
    "max_withdrawal",
    "daily_withdrawal_limit",
    "deposits_enabled",
    "withdrawals_enabled",
    "evil_setting",
)

# Raw input whose exact text must never reach logs (the canonical
# integer the EXISTING audit line prints is its own contract).
SECRET_RAW = "0.00000042"

_TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}")
_FORBIDDEN_OUTPUT = (
    "secret", "password", "token", "initdata", "init_data",
    "task_coin", "rpc", "/home/", ".db", "sqlite",
)
_SQL_STATEMENT_RE = re.compile(
    r"(?im)^\s*(SELECT|INSERT|UPDATE|DELETE|BEGIN|COMMIT|PRAGMA)\b"
)


class SettingsTestBase(ControlTestBase):
    """MT-ADMIN-39 fixture: control-center drivers + settings drivers.

    Pending settings state lives in the HANDLER CONTEXT's real
    ``user_data`` dict — tests therefore share ONE context across
    edit → text → confirm, which also proves the state is not
    process-global (a fresh context sees nothing pending).
    """

    # ── drivers ────────────────────────────────────────────────

    def _ctx(self, app=None):
        """A real per-user context (genuine dict user_data)."""
        return SimpleNamespace(user_data={}, application=app)

    def _press(
        self,
        data,
        actor_id: int = ADMIN_ID,
        chat_type: str = "private",
        *,
        message_gone: bool = False,
        answer_side_effect=None,
        context=None,
    ):
        update = _callback(actor_id, data, chat_type=chat_type)
        if message_gone:
            update.callback_query.message = None
        else:
            update.callback_query.message.reply_text = mock.AsyncMock()
        if answer_side_effect is not None:
            update.callback_query.answer = mock.AsyncMock(
                side_effect=answer_side_effect
            )
        _run(
            admin_control.control_callback(
                update,
                self._ctx() if context is None else context,
            )
        )
        return update

    def _view(self, data, **kwargs):
        """Press *data* and return (update, edited text, markup)."""
        update = self._press(data, **kwargs)
        edit = update.callback_query.edit_message_text
        text = edit.call_args[0][0]
        markup = edit.call_args[1].get("reply_markup")
        return update, text, markup

    def _send_text(
        self,
        text,
        actor_id: int = ADMIN_ID,
        chat_type: str = "private",
        context=None,
    ):
        update = _update(actor_id, text, chat_type=chat_type)
        _run(
            admin_control.settings_text_input(
                update, self._ctx() if context is None else context
            )
        )
        return update

    @staticmethod
    def _payloads(markup: InlineKeyboardMarkup) -> list[str]:
        return [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]

    @staticmethod
    def _labels(markup: InlineKeyboardMarkup) -> list[str]:
        return [button.text for row in markup.inline_keyboard
                for button in row]

    # ── flows ──────────────────────────────────────────────────

    def _stage(self, key: str, raw: str, ctx=None, actor_id: int = ADMIN_ID):
        """edit → text input on ONE shared context.

        Returns (ctx, prompt_update, preview_update).
        """
        ctx = ctx if ctx is not None else self._ctx()
        prompt = self._press(
            f"ctl:settings:edit:{key}", actor_id=actor_id, context=ctx
        )
        preview = self._send_text(raw, actor_id=actor_id, context=ctx)
        return ctx, prompt, preview

    def _confirm_flow(self, key: str, raw: str, ctx=None):
        """stage → confirm; returns the confirm update."""
        ctx, _prompt, _preview = self._stage(key, raw, ctx=ctx)
        return ctx, self._press(
            f"ctl:settings:confirm:{key}", context=ctx
        )

    # ── spies / dumps / guards ─────────────────────────────────

    def _spies_for(self, *module_names: str) -> dict:
        """Patch the MUTATION_SPY_TARGETS belonging to *module_names*."""
        spies = {}
        for module, name in MUTATION_SPY_TARGETS:
            if module.__name__ in module_names:
                patcher = mock.patch.object(module, name)
                spies[f"{module.__name__}.{name}"] = patcher.start()
                self.addCleanup(patcher.stop)
        return spies

    @staticmethod
    def _settings_section() -> str:
        source = open(admin_control.__file__, encoding="utf-8").read()
        marker = "# ── Settings module (MT-ADMIN-39)"
        return source[source.index(marker):]

    @staticmethod
    def _source() -> str:
        return open(admin_control.__file__, encoding="utf-8").read()

    @staticmethod
    def _forget(key: str, db_path: str) -> None:
        """Remove a setting row — the ONLY way to observe the
        never-configured state when a fixture seeded the key."""
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "DELETE FROM platform_settings WHERE key = ?", (key,)
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _raw_value(key: str, db_path: str):
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT value FROM platform_settings WHERE key = ?", (key,)
            ).fetchone()
            return None if row is None else row[0]
        finally:
            conn.close()

    @staticmethod
    def _assert_no_secrets(testcase, text: str) -> None:
        lowered = text.lower()
        for needle in _FORBIDDEN_OUTPUT:
            testcase.assertNotIn(
                needle, lowered, f"secret leak: {needle}"
            )
        testcase.assertIsNone(_TOKEN_RE.search(text), "bot-token shape")

    # ── seeds (EXISTING production contracts only) ─────────────

    def _seed_setting(self, key: str, value) -> None:
        platform_settings.set_setting(
            key, value, admin_user_id=ADMIN_ID, db_path=self.db_path
        )


# ══════════════════════════════════════════════════════════════════
# A. REGISTRY (1-2)
# ══════════════════════════════════════════════════════════════════


class TestSettingsRegistry(SettingsTestBase):

    def test_01_settings_registered_exactly_once(self) -> None:
        """1. The settings module is registered exactly once with no
        delegation command, and ``ctl:settings`` parses to it."""
        keys = [m.key for m in admin_control.MODULES]
        self.assertEqual(keys.count("settings"), 1)
        module = admin_control.MODULES_BY_KEY["settings"]
        self.assertIsNone(module.command)
        self.assertEqual(parse_callback("ctl:settings"), "settings")

    def test_02_no_longer_placeholder(self) -> None:
        """2. The slot renders the panel instead of the safe
        not-yet-available notice."""
        update, text, markup = self._view("ctl:settings")
        self.assertIn(SETTINGS_PANEL_HEADER, text)
        self.assertNotIn(MSG_MODULE_UNAVAILABLE, text)
        self.assertEqual(_answered(update.callback_query), None)
        update.callback_query.edit_message_text.assert_awaited()
        module = admin_control.MODULES_BY_KEY["settings"]
        self.assertNotIn("قريباً", module.description)
        self.assertEqual(module.description, "إعدادات المنصة")


# ══════════════════════════════════════════════════════════════════
# B. AUTHORIZATION (3-7)
# ══════════════════════════════════════════════════════════════════


class TestSettingsAuth(SettingsTestBase):

    def test_03_non_admin_blocked_before_any_read(self) -> None:
        """3. Non-admins get the standard refusal BEFORE any setting
        is read or any view rendered."""
        with mock.patch.object(
            platform_settings, "get_setting"
        ) as get_spy, mock.patch.object(
            platform_settings, "set_setting"
        ) as set_spy:
            update = self._press(
                "ctl:settings", actor_id=STRANGER, context=self._ctx()
            )
        get_spy.assert_not_called()
        set_spy.assert_not_called()
        self.assertEqual(_answered(update.callback_query), MSG_ADMIN_ONLY)
        update.callback_query.edit_message_text.assert_not_called()

    def test_04_group_and_channel_are_silent(self) -> None:
        """4. Groups/channels receive ZERO administrative response —
        no answer, no render, no read."""
        for chat_type in ("group", "supergroup", "channel"):
            with mock.patch.object(
                platform_settings, "get_setting"
            ) as get_spy:
                update = self._press(
                    "ctl:settings",
                    chat_type=chat_type,
                    context=self._ctx(),
                )
            get_spy.assert_not_called()
            self.assertIsNone(_answered(update.callback_query), chat_type)
            update.callback_query.edit_message_text.assert_not_called()

    def test_05_callback_rechecks_authorization(self) -> None:
        """5. Every settings sub-op re-checks authorization — a
        staged context cannot let a stranger confirm."""
        ctx, _p, _v = self._stage(MIN_DEPOSIT, "0.01")
        with mock.patch.object(
            platform_settings, "get_setting"
        ) as get_spy, mock.patch.object(
            platform_settings, "set_setting"
        ) as set_spy:
            update = self._press(
                f"ctl:settings:confirm:{MIN_DEPOSIT}",
                actor_id=STRANGER,
                context=ctx,
            )
        get_spy.assert_not_called()
        set_spy.assert_not_called()
        self.assertEqual(_answered(update.callback_query), MSG_ADMIN_ONLY)
        update.callback_query.edit_message_text.assert_not_called()

    def test_06_text_input_rechecks_authorization(self) -> None:
        """6. The text handler re-checks authorization BEFORE
        validation or any read; strangers and groups are silent and
        the staged edit stays untouched for the real admin."""
        ctx, _p, _v = self._stage(MIN_DEPOSIT, "")  # arms pending
        # NOTE: empty text keeps pending (rejected, not consumed).
        with mock.patch.object(
            platform_settings, "parse_setting_value"
        ) as parse_spy, mock.patch.object(
            platform_settings, "set_setting"
        ) as set_spy:
            stranger = self._send_text(
                "0.05", actor_id=STRANGER, context=ctx
            )
            group = self._send_text(
                "0.05", chat_type="supergroup", context=ctx
            )
        parse_spy.assert_not_called()
        set_spy.assert_not_called()
        self.assertFalse(stranger.message.reply_text.called)
        self.assertFalse(group.message.reply_text.called)
        # The REAL admin's staged edit still works afterwards.
        preview = self._send_text("0.05", context=ctx)
        self.assertIn("⚠️ تأكيد تحديث الإعداد", _reply(preview))

    def test_07_auth_happens_before_reads(self) -> None:
        """7. A denied press performs ZERO platform-settings reads —
        authorization strictly precedes every read."""
        with mock.patch.object(
            platform_settings, "get_setting"
        ) as get_spy:
            self._press(
                f"ctl:settings:edit:{MIN_WITHDRAWAL}",
                actor_id=STRANGER,
                context=self._ctx(),
            )
        get_spy.assert_not_called()


# ══════════════════════════════════════════════════════════════════
# C. RENDERING (8-12)
# ══════════════════════════════════════════════════════════════════


class TestSettingsRendering(SettingsTestBase):

    def test_08_all_four_settings_shown(self) -> None:
        """8. The panel shows exactly the four registered settings."""
        _u, text, markup = self._view("ctl:settings")
        for label in (
            "💸 الحد الأدنى للسحب",
            "💵 الحد الأدنى للإيداع",
            "💳 رسوم السحب",
            "🎁 عمولة المعلن",
        ):
            self.assertIn(label, text)
        edit_buttons = [
            payload for payload in self._payloads(markup)
            if ":edit:" in payload
        ]
        self.assertEqual(
            sorted(edit_buttons),
            sorted(f"ctl:settings:edit:{key}" for key in KEYS),
        )

    def test_09_missing_value_renders_unset(self) -> None:
        """9. A never-configured key renders "غير مضبوط" — the value
        really is None in the authoritative store."""
        self._forget(MIN_DEPOSIT, self.db_path)
        self.assertIsNone(
            platform_settings.get_setting(
                MIN_DEPOSIT, db_path=self.db_path
            )
        )
        _u, text, _m = self._view("ctl:settings")
        line = next(
            ln for ln in text.splitlines() if "الحد الأدنى للإيداع" in ln
        )
        self.assertTrue(line.endswith("غير مضبوط"), line)

    def test_10_no_invented_defaults(self) -> None:
        """10. Unset keys never display 0 / 1 / 100 / 1000 or any
        other invented number."""
        for key in (MIN_DEPOSIT, MIN_WITHDRAWAL, FEE):
            self._forget(key, self.db_path)
        _u, text, _m = self._view("ctl:settings")
        for label in ("الحد الأدنى للسحب", "الحد الأدنى للإيداع", "رسوم السحب"):
            line = next(ln for ln in text.splitlines() if label in ln)
            self.assertTrue(line.endswith("غير مضبوط"), line)
            for invented in ("0", "1", "100", "1000"):
                self.assertFalse(
                    line.endswith(f": {invented}"),
                    f"invented default on {label}: {line}",
                )

    def test_11_rate_never_duplicated_as_setting(self) -> None:
        """11. The rate is not a settings key, is never read from the
        settings surface, and the rate row stays untouched."""
        self.assertNotIn("rate", KEYS)
        self._seed_rate("48.5")
        before = self._dump_state()["current_rate"]
        with mock.patch.object(
            rate_store, "get_current_quote"
        ) as quote_spy:
            _u, text, _m = self._view("ctl:settings")
        quote_spy.assert_not_called()
        self.assertNotIn("USDT/EGP", text)
        self.assertNotIn("48.5", text)
        self.assertEqual(before, self._dump_state()["current_rate"])

    def test_12_no_secrets_in_ui(self) -> None:
        """12. No secret, token, path or database detail can appear
        on any settings surface."""
        texts = []
        _u, panel, _m = self._view("ctl:settings")
        texts.append(panel)
        ctx = self._ctx()
        _u, prompt, _m2 = self._view(
            f"ctl:settings:edit:{COMMISSION}", context=ctx
        )
        texts.append(prompt)
        preview = self._send_text("42", context=ctx)
        texts.append(_reply(preview))
        for text in texts:
            self._assert_no_secrets(self, text)


# ══════════════════════════════════════════════════════════════════
# D. CALLBACK GRAMMAR (13-20)
# ══════════════════════════════════════════════════════════════════


class TestSettingsGrammar(SettingsTestBase):

    def test_13_panel_callback(self) -> None:
        """13. ``ctl:settings`` parses to the bare registry op."""
        self.assertEqual(parse_callback("ctl:settings"), "settings")

    def test_14_edit_valid_keys(self) -> None:
        """14. edit parses for each of the four registered keys."""
        for key in KEYS:
            self.assertEqual(
                parse_callback(f"ctl:settings:edit:{key}"),
                f"settings:edit:{key}",
                key,
            )

    def test_15_confirm_valid_keys(self) -> None:
        """15. confirm parses for each of the four registered keys."""
        for key in KEYS:
            self.assertEqual(
                parse_callback(f"ctl:settings:confirm:{key}"),
                f"settings:confirm:{key}",
                key,
            )

    def test_16_cancel_valid_keys(self) -> None:
        """16. cancel parses for each of the four registered keys."""
        for key in KEYS:
            self.assertEqual(
                parse_callback(f"ctl:settings:cancel:{key}"),
                f"settings:cancel:{key}",
                key,
            )

    def test_17_back_parses(self) -> None:
        """17. ``ctl:settings:back`` parses."""
        self.assertEqual(
            parse_callback("ctl:settings:back"), "settings:back"
        )

    def test_18_unknown_keys_rejected(self) -> None:
        """18. Keys outside the registered four are rejected — even
        when a row with that key exists in the database."""
        for key in ABSENT_KEYS:
            for op in ("edit", "confirm", "cancel"):
                self.assertIsNone(
                    parse_callback(f"ctl:settings:{op}:{key}"),
                    f"{op}:{key}",
                )
        # A raw database row with an unknown key stays unreachable.
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO platform_settings (key, value) "
                "VALUES ('maintenance_mode', 1)"
            )
            conn.commit()
        finally:
            conn.close()
        self.assertIsNone(
            parse_callback("ctl:settings:edit:maintenance_mode")
        )
        _u, text, _m = self._view("ctl:settings")
        self.assertNotIn("maintenance_mode", text)
        for key in KEYS:
            self.assertIn(admin_control._SETTINGS_LABELS[key], text)

    def test_19_malformed_callbacks_rejected(self) -> None:
        """19. Malformed/oversized settings payloads fail safely."""
        for data in (
            "ctl:settings:edit:",
            "ctl:settings:confirm:",
            "ctl:settings:cancel:",
            "ctl:settings:edit: minimum_withdrawal_units",
            "ctl:settings:edit:Minimum_Withdrawal_Units",
            "ctl:settings:edit:minimum_withdrawal_units:extra",
            "ctl:settings:confirm:advertiser_commission:3000",
            "ctl:settings:back:extra",
            "ctl:settings:p:1",
            "ctl:settings:v:1",
            f"ctl:settings:edit:{'a' * 100}",
            "ctl:settings:edit:0.01",
            "settings:edit:minimum_withdrawal_units",
        ):
            self.assertIsNone(parse_callback(data), data)

    def test_20_foreign_namespaces_untouched(self) -> None:
        """20. wd/dp/pm/mr/atw/mproof/sup payloads never parse into
        the ctl namespace and no foreign callback data is built."""
        for foreign in (
            "wd:list", "dp:open", "pm:list", "mr:view",
            "atw:start", "mproof:1", "sup:open",
        ):
            self.assertIsNone(parse_callback(foreign), foreign)
        source = self._source()
        for foreign in (
            'callback_data="wd', 'callback_data="dp',
            'callback_data="pm', 'callback_data="mr',
            'callback_data="atw', 'callback_data="mproof',
            'callback_data="sup',
        ):
            self.assertNotIn(foreign, source)


# ══════════════════════════════════════════════════════════════════
# E. VALIDATION (21-24)
# ══════════════════════════════════════════════════════════════════


class TestSettingsValidation(SettingsTestBase):

    def test_21_existing_validator_invoked(self) -> None:
        """21. Input flows through the EXISTING
        ``platform_settings.parse_setting_value`` — never a local
        re-implementation."""
        original = platform_settings.parse_setting_value
        calls: list[tuple] = []

        def spy(key, value):
            calls.append((key, value))
            return original(key, value)

        with mock.patch.object(
            platform_settings, "parse_setting_value", side_effect=spy
        ):
            ctx, _p, preview = self._stage(MIN_DEPOSIT, "0.01")
            self.assertIn("⚠️ تأكيد تحديث الإعداد", _reply(preview))
            self._press(
                f"ctl:settings:confirm:{MIN_DEPOSIT}", context=ctx
            )
        # once at staging, again at confirmation (re-validation)
        self.assertEqual(calls[0], (MIN_DEPOSIT, "0.01"))
        self.assertEqual(calls[1], (MIN_DEPOSIT, "0.01"))
        self.assertGreaterEqual(len(calls), 2)

    def test_22_invalid_value_writes_nothing(self) -> None:
        """22. Invalid input fails BEFORE set_setting — the stored
        value is byte-identical and no setter call happens."""
        before = self._raw_value(COMMISSION, self.db_path)
        with mock.patch.object(
            platform_settings, "set_setting"
        ) as set_spy:
            ctx = self._ctx()
            self._press(f"ctl:settings:edit:{COMMISSION}", context=ctx)
            bad = self._send_text("abc", context=ctx)
        set_spy.assert_not_called()
        self.assertEqual(
            before, self._raw_value(COMMISSION, self.db_path)
        )
        reply = _reply(bad)
        self.assertNotIn("⚠️ تأكيد", reply)
        self.assertIn("❌", reply)  # the validator's own rejection
        # Negative value — same contract, still no write.
        with mock.patch.object(
            platform_settings, "set_setting"
        ) as set_spy:
            negative = self._send_text("-5", context=ctx)
        set_spy.assert_not_called()
        self.assertIn("سالبة", _reply(negative))
        self.assertEqual(
            before, self._raw_value(COMMISSION, self.db_path)
        )

    def test_23_invalid_value_keeps_pending(self) -> None:
        """23. After a rejection the pending operation survives, so
        a retry with a valid value still reaches the preview."""
        ctx = self._ctx()
        self._press(f"ctl:settings:edit:{MIN_DEPOSIT}", context=ctx)
        bad = self._send_text("not-a-number", context=ctx)
        self.assertNotIn("⚠️ تأكيد", _reply(bad))
        retry = self._send_text("0.02", context=ctx)
        self.assertIn("⚠️ تأكيد تحديث الإعداد", _reply(retry))

    def test_24_valid_value_reaches_preview_without_write(self) -> None:
        """24. Valid input renders the preview card with old/new
        values — and writes NOTHING before confirmation."""
        self._seed_setting(MIN_DEPOSIT, 1000)  # 0.00001 USDT
        with mock.patch.object(
            platform_settings, "set_setting"
        ) as set_spy:
            _ctx, _p, preview = self._stage(MIN_DEPOSIT, "0.02")
        set_spy.assert_not_called()
        reply = _reply(preview)
        self.assertIn("⚠️ تأكيد تحديث الإعداد", reply)
        self.assertIn("القيمة الحالية:", reply)
        self.assertIn("القيمة الجديدة: 0.02000000 USDT", reply)
        self.assertEqual(
            1000, self._raw_value(MIN_DEPOSIT, self.db_path)
        )
        markup = preview.message.reply_text.call_args[1]["reply_markup"]
        payloads = self._payloads(markup)
        self.assertIn(f"ctl:settings:confirm:{MIN_DEPOSIT}", payloads)
        self.assertIn(f"ctl:settings:cancel:{MIN_DEPOSIT}", payloads)


# ══════════════════════════════════════════════════════════════════
# F. MUTATION (25-33)
# ══════════════════════════════════════════════════════════════════


class TestSettingsMutation(SettingsTestBase):

    def test_25_confirm_calls_set_setting_exactly_once(self) -> None:
        """25. One confirmation → exactly one ``set_setting`` call
        with the staged key, exact input and acting admin."""
        original = platform_settings.set_setting
        with mock.patch.object(
            platform_settings, "set_setting", wraps=original
        ) as set_spy:
            _ctx, update = self._confirm_flow(MIN_DEPOSIT, "0.03")
        set_spy.assert_called_once_with(
            MIN_DEPOSIT, "0.03", admin_user_id=ADMIN_ID
        )
        self.assertEqual(
            TOAST_SETTING_UPDATED, _answered(update.callback_query)
        )
        self.assertEqual(
            3_000_000, self._raw_value(MIN_DEPOSIT, self.db_path)
        )

    def test_26_fresh_reread_before_write(self) -> None:
        """26. The confirm press re-reads the current stored value
        BEFORE the write (a pressed card is never trusted)."""
        calls: list[str] = []
        original_get = platform_settings.get_setting
        original_set = platform_settings.set_setting

        def get(*args, **kwargs):
            calls.append("get")
            return original_get(*args, **kwargs)

        def setter(*args, **kwargs):
            calls.append("set")
            return original_set(*args, **kwargs)

        ctx = self._ctx()
        self._press(f"ctl:settings:edit:{MIN_DEPOSIT}", context=ctx)
        self._send_text("0.04", context=ctx)
        calls.clear()
        with mock.patch.object(
            platform_settings, "get_setting", side_effect=get
        ), mock.patch.object(
            platform_settings, "set_setting", side_effect=setter
        ):
            self._press(
                f"ctl:settings:confirm:{MIN_DEPOSIT}", context=ctx
            )
        self.assertIn("get", calls)
        self.assertIn("set", calls)
        self.assertLess(
            calls.index("get"), calls.index("set"),
            "the fresh re-read must precede the write",
        )

    def test_27_authorization_before_write(self) -> None:
        """27. A stranger's confirm performs zero reads and zero
        writes even with a perfectly staged context."""
        ctx, _p, _v = self._stage(MIN_DEPOSIT, "0.05")
        with mock.patch.object(
            platform_settings, "get_setting"
        ) as get_spy, mock.patch.object(
            platform_settings, "set_setting"
        ) as set_spy:
            update = self._press(
                f"ctl:settings:confirm:{MIN_DEPOSIT}",
                actor_id=STRANGER,
                context=ctx,
            )
        get_spy.assert_not_called()
        set_spy.assert_not_called()
        self.assertEqual(_answered(update.callback_query), MSG_ADMIN_ONLY)

    def test_28_no_transaction_and_no_sql_in_handler(self) -> None:
        """28. The handler issues no SQL and opens no transaction —
        only the existing set_setting contract writes."""
        source = self._source()
        self.assertIsNone(_SQL_STATEMENT_RE.search(source))
        self.assertIsNone(re.search(r"\bdb\.transaction\s*\(", source))
        self.assertIsNone(re.search(r"\bdb\.execute\s*\(", source))
        section = self._settings_section()
        self.assertIsNone(re.search(r"\bdb\.transaction\s*\(", section))
        self.assertIsNone(re.search(r"\bdb\.execute\s*\(", section))

    def test_29_wallet_not_called(self) -> None:
        """29. No wallet primitive runs; wallet rows stay
        byte-identical."""
        spies = self._spies_for("wallet")
        before = self._dump_state()
        _ctx, update = self._confirm_flow(MIN_DEPOSIT, "0.06")
        for label, spy in spies.items():
            self.assertFalse(spy.called, f"{label} must never run")
        after = self._dump_state()
        self.assertEqual(before["wallets"], after["wallets"])
        self.assertEqual(before["ledger"], after["ledger"])

    def test_30_ledger_not_called(self) -> None:
        """30. No ledger service exists on this path; ledger rows
        are byte-identical."""
        section = self._settings_section()
        self.assertNotIn("LedgerService", section)
        self.assertIsNone(re.search(r"\bledger\.\w+\s*\(", section))
        before = self._dump_state()["ledger"]
        self._confirm_flow(MIN_DEPOSIT, "0.07")
        self.assertEqual(before, self._dump_state()["ledger"])

    def test_31_withdrawal_not_mutated(self) -> None:
        """31. No withdrawal row changes — settings are not
        withdrawal state."""
        section = self._settings_section()
        self.assertNotIn("WithdrawalService", section)
        self.assertIsNone(
            re.search(r"\bwithdrawal_store\.\w+\s*\(", section)
        )
        before = self._dump_state()["withdrawal_requests"]
        self._confirm_flow(MIN_WITHDRAWAL, "0.08")
        self.assertEqual(before, self._dump_state()["withdrawal_requests"])

    def test_32_deposit_not_mutated(self) -> None:
        """32. Deposit rows never change on a settings update."""
        section = self._settings_section()
        self.assertIsNone(re.search(r"\bdeposit_store\.\w+\s*\(", section))
        self.assertIsNone(
            re.search(r"\bdeposit_proof_store\.\w+\s*\(", section)
        )
        before = self._dump_state()
        self._confirm_flow(MIN_DEPOSIT, "0.09")
        after = self._dump_state()
        self.assertEqual(before["deposit_requests"], after["deposit_requests"])
        self.assertEqual(before["deposit_proofs"], after["deposit_proofs"])

    def test_33_payment_methods_not_mutated(self) -> None:
        """33. Payment-method rows never change on a settings
        update."""
        section = self._settings_section()
        self.assertIsNone(
            re.search(r"\bpayment_method_store\.\w+\s*\(", section)
        )
        before = self._dump_state()["payment_methods"]
        self._confirm_flow(MIN_DEPOSIT, "0.1")
        self.assertEqual(before, self._dump_state()["payment_methods"])

    def test_33b_only_platform_settings_changed(self) -> None:
        """Full-flow isolation: with every financial mutation entry
        point spied, a confirmed update changes ONLY the
        platform_settings row."""
        spies = self._spies_for(*(m.__name__ for m, _n in MUTATION_SPY_TARGETS))
        before = self._dump_state()
        _ctx, update = self._confirm_flow(COMMISSION, "42.5")
        for label, spy in spies.items():
            self.assertFalse(spy.called, f"{label} must never run")
        after = self._dump_state()
        for table in FINANCIAL_TABLES:
            if table == "platform_settings":
                self.assertNotEqual(
                    before[table], after[table],
                    "the setting row itself must change",
                )
            else:
                self.assertEqual(
                    before[table], after[table],
                    f"{table} must stay byte-identical",
                )


# ══════════════════════════════════════════════════════════════════
# G. IDEMPOTENCY (34-36)
# ══════════════════════════════════════════════════════════════════


class TestSettingsIdempotency(SettingsTestBase):

    def test_34_duplicate_confirm_cannot_write_twice(self) -> None:
        """34. The confirmation is single-use: a second press finds
        nothing staged, answers the safe notice and writes zero
        times."""
        original = platform_settings.set_setting
        ctx = self._ctx()
        self._press(f"ctl:settings:edit:{MIN_DEPOSIT}", context=ctx)
        self._send_text("0.11", context=ctx)
        with mock.patch.object(
            platform_settings, "set_setting", wraps=original
        ) as set_spy:
            first = self._press(
                f"ctl:settings:confirm:{MIN_DEPOSIT}", context=ctx
            )
            second = self._press(
                f"ctl:settings:confirm:{MIN_DEPOSIT}", context=ctx
            )
        set_spy.assert_called_once()
        self.assertEqual(
            TOAST_SETTING_UPDATED, _answered(first.callback_query)
        )
        self.assertIn(MSG_NO_PENDING, _edited(second.callback_query))

    def test_35_stale_confirm_is_safe(self) -> None:
        """35. Confirmations from a lost/never-armed context answer
        the safe notice and write NOTHING."""
        original = platform_settings.set_setting
        with mock.patch.object(
            platform_settings, "set_setting", wraps=original
        ) as set_spy:
            # (a) never staged in THIS context
            update = self._press(
                f"ctl:settings:confirm:{MIN_DEPOSIT}", context=self._ctx()
            )
            self.assertIn(MSG_NO_PENDING, _edited(update.callback_query))
            # (b) staged, then cancelled, then a stale confirm lands
            ctx = self._ctx()
            self._press(f"ctl:settings:edit:{MIN_DEPOSIT}", context=ctx)
            self._send_text("0.12", context=ctx)
            self._press(
                f"ctl:settings:cancel:{MIN_DEPOSIT}", context=ctx
            )
            stale = self._press(
                f"ctl:settings:confirm:{MIN_DEPOSIT}", context=ctx
            )
            self.assertIn(MSG_NO_PENDING, _edited(stale.callback_query))
        set_spy.assert_not_called()

    def test_36_cancel_causes_no_write(self) -> None:
        """36. Cancel drops the staged value, re-renders the panel
        and writes NOTHING — and the confirm button becomes inert."""
        ctx, _p, _v = self._stage(MIN_DEPOSIT, "0.13")
        before = self._raw_value(MIN_DEPOSIT, self.db_path)
        with mock.patch.object(
            platform_settings, "set_setting"
        ) as set_spy:
            update, text, markup = self._view(
                f"ctl:settings:cancel:{MIN_DEPOSIT}", context=ctx
            )
            confirm = self._press(
                f"ctl:settings:confirm:{MIN_DEPOSIT}", context=ctx
            )
        set_spy.assert_not_called()
        self.assertEqual(
            TOAST_CANCELLED, _answered(update.callback_query)
        )
        self.assertIn(SETTINGS_PANEL_HEADER, text)
        self.assertIn(MSG_NO_PENDING, _edited(confirm.callback_query))
        self.assertEqual(
            before, self._raw_value(MIN_DEPOSIT, self.db_path)
        )


# ══════════════════════════════════════════════════════════════════
# H. NAVIGATION (37-38)
# ══════════════════════════════════════════════════════════════════


class TestSettingsNavigation(SettingsTestBase):

    def test_37_back_returns_control_center(self) -> None:
        """37. ``ctl:settings:back`` returns the dashboard — and
        drops any staged edit on the way."""
        ctx, _p, _v = self._stage(MIN_DEPOSIT, "0.14")
        update, text, _m = self._view(
            "ctl:settings:back", context=ctx
        )
        self.assertIn(HEADER, text)
        self.assertNotIn(SETTINGS_PANEL_HEADER, text)
        confirm = self._press(
            f"ctl:settings:confirm:{MIN_DEPOSIT}", context=ctx
        )
        self.assertIn(MSG_NO_PENDING, _edited(confirm.callback_query))

    def test_38_update_renders_fresh_panel(self) -> None:
        """38. A successful update toasts the fixed success copy and
        re-renders the panel from a FRESH read showing the new
        value."""
        self._seed_setting(MIN_DEPOSIT, 1000)
        _ctx, update = self._confirm_flow(MIN_DEPOSIT, "0.15")
        self.assertEqual(
            TOAST_SETTING_UPDATED, _answered(update.callback_query)
        )
        text = _edited(update.callback_query)
        self.assertIn(SETTINGS_PANEL_HEADER, text)
        self.assertIn("0.15000000 USDT", text)
        self.assertEqual(
            15_000_000, self._raw_value(MIN_DEPOSIT, self.db_path)
        )


# ══════════════════════════════════════════════════════════════════
# I. SECURITY (39-42)
# ══════════════════════════════════════════════════════════════════


class TestSettingsSecurity(SettingsTestBase):

    def test_39_no_sql_in_admin_control(self) -> None:
        """39. The module issues no SQL statements of any kind."""
        source = self._source()
        self.assertIsNone(_SQL_STATEMENT_RE.search(source))

    def test_40_no_secrets_or_staged_values_in_ui_or_logs(self) -> None:
        """40. Logs carry only admin id + key + action + result —
        never the staged raw input, never a secret."""
        with self.assertLogs(level="INFO") as captured:
            ctx, _p, _v = self._stage(MIN_DEPOSIT, SECRET_RAW)
            self._press(
                f"ctl:settings:confirm:{MIN_DEPOSIT}", context=ctx
            )
        joined = "\n".join(captured.output)
        # The staged raw text never reaches ANY log line.
        self.assertNotIn(SECRET_RAW, joined)
        for needle in _FORBIDDEN_OUTPUT:
            self.assertNotIn(needle, joined.lower(), needle)
        self.assertIsNone(_TOKEN_RE.search(joined))
        # admin_control's own lines are metadata only: no value.
        control_lines = [
            line for line in captured.records
            if line.name == "admin_control"
        ]
        self.assertTrue(control_lines)
        for record in control_lines:
            self.assertNotIn(SECRET_RAW, record.getMessage())
            self.assertNotIn("value=", record.getMessage())

    def test_41_callback_payloads_stay_closed(self) -> None:
        """41. Every payload the module builds is a fixed op + a
        registered key — never a value, never free-form."""
        allowed = re.compile(
            r"^ctl:settings"
            r"(:back|:(edit|confirm|cancel):("
            + "|".join(re.escape(k) for k in KEYS)
            + r"))?$"
        )
        payloads: list[str] = []
        _u, text, markup = self._view("ctl:settings")
        payloads += self._payloads(markup)
        ctx = self._ctx()
        _u, prompt, markup = self._view(
            f"ctl:settings:edit:{MIN_DEPOSIT}", context=ctx
        )
        payloads += self._payloads(markup)
        preview = self._send_text("0.0123", context=ctx)
        preview_markup = preview.message.reply_text.call_args[1][
            "reply_markup"
        ]
        payloads += self._payloads(preview_markup)
        self.assertTrue(payloads)
        for payload in payloads:
            self.assertRegex(payload, allowed)
            self.assertNotIn("0.0123", payload)
            self.assertNotIn(".", payload)
        # And none of them came from a foreign namespace.
        self.assertFalse(
            [p for p in payloads if not p.startswith("ctl:settings")]
        )

    def test_42_no_process_global_dict_state(self) -> None:
        """42. Settings stage state lives in the handler context —
        no module-level pending dict, and a FRESH context sees
        nothing (so stale state can never leak between chats)."""
        source = self._source()
        self.assertNotIn("_PENDING_SETTINGS", source)
        self.assertEqual(
            [
                name for name in dir(admin_control)
                if name.startswith("_PENDING") and "SETTING" in name.upper()
            ],
            [],
        )
        # Behavioral proof: edit stages in ctx A…
        ctx_a = self._ctx()
        self._press(f"ctl:settings:edit:{MIN_DEPOSIT}", context=ctx_a)
        # …a DIFFERENT context B receives the text (silent — no
        # staged value there)…
        silent = self._send_text("0.16", context=self._ctx())
        self.assertFalse(silent.message.reply_text.called)
        # …and a fresh context C cannot confirm anything.
        confirm = self._press(
            f"ctl:settings:confirm:{MIN_DEPOSIT}", context=self._ctx()
        )
        self.assertIn(MSG_NO_PENDING, _edited(confirm.callback_query))
        # ctx A still owns its staged edit: the admin's own text
        # input there reaches the preview.
        preview = self._send_text("0.16", context=ctx_a)
        self.assertIn("⚠️ تأكيد", _reply(preview))


# ══════════════════════════════════════════════════════════════════
# J. EXTRAS (43+): static registration, regression, edge cases
# ══════════════════════════════════════════════════════════════════


class TestSettingsRegistration(SettingsTestBase):

    def test_43_no_handler_added_group_8_static(self) -> None:
        """43. Pressing ctl:settings adds ZERO handlers through
        context.application; the value catch-all is statically
        registered in bot.py in its own group 8 (0-7 occupied)."""
        app = _FakeApplication()
        ctx = self._ctx(app=app)
        self._press("ctl:settings", context=ctx)
        self.assertEqual(app.added, [])
        # a second press never registers anything either
        self._press(f"ctl:settings:edit:{MIN_DEPOSIT}", context=ctx)
        self.assertEqual(app.added, [])

        captured, _bot = _capture_handlers()
        settings_text = [
            g
            for h, g in captured
            if isinstance(h, MessageHandler)
            and getattr(h, "callback", None)
            is admin_control.settings_text_input
        ]
        self.assertEqual(settings_text, [8])

    def test_44_registration_unchanged(self) -> None:
        """44. bot.py still registers ``^ctl:`` and ``/control``
        exactly once; the settings text input IS statically
        registered there (group 8); foreign patterns are untouched."""
        import bot as bot_mod

        source = open(bot_mod.__file__, encoding="utf-8").read()
        self.assertIn("settings_text_input", source)
        captured, _bot = _capture_handlers()
        ctl = [
            h for h, _g in captured
            if isinstance(h, CallbackQueryHandler)
            and h.pattern.pattern == r"^ctl:"
        ]
        self.assertEqual(len(ctl), 1)
        control = [
            h for h, _g in captured
            if isinstance(h, CommandHandler) and "control" in h.commands
        ]
        self.assertEqual(len(control), 1)
        for pattern in (r"^wd:", r"^dp:", r"^pm:", r"^sup:"):
            matched = [
                h for h, _g in captured
                if isinstance(h, CallbackQueryHandler)
                and h.pattern.pattern == pattern
            ]
            self.assertEqual(len(matched), 1, pattern)


class TestSettingsRegression(SettingsTestBase):

    def test_45_users_module_remains_functional(self) -> None:
        """45. The users module still renders in place."""
        _u, text, _m = self._view("ctl:users")
        self.assertIn(USERS_PANEL_HEADER, text)

    def test_46_tasks_module_remains_functional(self) -> None:
        """46. The tasks module still renders in place."""
        _u, text, _m = self._view("ctl:tasks")
        self.assertIn(TASKS_PANEL_HEADER, text)

    def test_47_admins_module_remains_functional(self) -> None:
        """47. The admins module still renders in place."""
        _u, text, _m = self._view("ctl:admins")
        self.assertIn(ADMINS_PANEL_HEADER, text)

    def test_48_dashboard_and_remaining_placeholders(self) -> None:
        """48. The dashboard keeps its exact payload set; the other
        reserved slots keep their safe notice."""
        data = self._payloads(build_dashboard_keyboard())
        self.assertEqual(
            sorted(data),
            [
                "ctl:admins",
                "ctl:broadcast",
                "ctl:channels",
                "ctl:deposits",
                "ctl:health",
                "ctl:logs",
                "ctl:paymethods",
                "ctl:rate",
                "ctl:refresh",
                "ctl:requests",
                "ctl:reviews",
                "ctl:rewards",
                "ctl:settings",
                "ctl:support",
                "ctl:tasks",
                "ctl:users",
                "ctl:withdrawals",
            ],
        )
        update = self._press("ctl:refresh")
        self.assertIn(HEADER, _edited(update.callback_query))
        for key in ("rewards", "logs", "health"):
            update = self._press(f"ctl:{key}", context=self._ctx())
            self.assertEqual(
                _answered(update.callback_query),
                MSG_MODULE_UNAVAILABLE,
                key,
            )

    def test_49_exact_display_math(self) -> None:
        """49. Displays are EXACT: integer units → 8-dp USDT text,
        basis points → the documented 10000 bp = 100 % scale."""
        self._seed_setting(MIN_WITHDRAWAL, 500_000)   # 0.005 USDT
        self._seed_setting(COMMISSION, 3_050)         # 30.50 %
        _u, text, _m = self._view("ctl:settings")
        withdrawal_line = next(
            ln for ln in text.splitlines() if "الحد الأدنى للسحب" in ln
        )
        commission_line = next(
            ln for ln in text.splitlines() if "عمولة المعلن" in ln
        )
        self.assertTrue(
            withdrawal_line.endswith("0.00500000 USDT"), withdrawal_line
        )
        self.assertTrue(
            commission_line.endswith("30.50%"), commission_line
        )
        # Round-trip: what the panel shows parses back to the same
        # stored integer through the existing contract.
        self.assertEqual(
            platform_settings.parse_setting_value(
                MIN_WITHDRAWAL, "0.005"
            ),
            500_000,
        )

    def test_50_read_failure_degrades_per_row(self) -> None:
        """50. A settings read failure degrades that row to a safe
        state — the panel still renders, never a traceback."""
        with mock.patch.object(
            platform_settings,
            "get_setting",
            side_effect=RuntimeError("db exploded"),
        ):
            update, text, _m = self._view("ctl:settings")
        self.assertIn(SETTINGS_PANEL_HEADER, text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("db exploded", text)
        # all four rows degraded safely
        self.assertEqual(text.count("غير متاح"), 4)


if __name__ == "__main__":
    unittest.main()
