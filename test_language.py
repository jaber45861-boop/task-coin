"""Focused tests for persistent user language selection (micro-task).

Covers the required cases:

 1. New user /start shows language selection.
 2-5. ar / en / ru / fa selections persist.
 6-9. Existing users with ar / en / ru / fa skip language selection.
10. Invalid language callback is rejected safely.
11. Repeated valid callback is safe/idempotent.
12. Referral payload still works with language selection.
13. Self-referral behavior remains unchanged.
14. Anti-Bot still runs after language selection.
15. Required-channel verification still runs after Anti-Bot.
16. Existing admin commands remain unaffected.
(+ db-level constraints, legacy-schema migration, stray-text re-prompt,
    callback cannot touch another user's language).

Run:
    python -m pytest test_language.py -v
    # or
    python -m unittest test_language.py -v
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import InlineKeyboardMarkup
from telegram.ext import ConversationHandler

import db
from config import CHANNELS, Channel
from subscription import is_locked, unlock_user
from bot import (
    ANTI_BOT,
    LANGUAGE_BUTTONS,
    LANGUAGE_PROMPT,
    LANGUAGE_SELECT,
    _language_code,
    addchannel_start,
    check_answer,
    language_callback_fallback,
    language_prompt_again,
    language_selected,
    start,
)

ALL_CODES = ("ar", "en", "ru", "fa")


# ── Test helpers ───────────────────────────────────────────────────────


def _msg_update(user_id: int, text: str | None = None) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = None  # username is never required
    update.effective_user.first_name = "Tester"
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.text = text
    update.callback_query = None
    return update


def _ctx(args: list[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = list(args or [])
    ctx.user_data = {}
    ctx.bot = MagicMock()
    return ctx


def _cb_update(user_id: int, data: str) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.from_user = MagicMock()
    update.callback_query.from_user.id = user_id
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    return update


def _flat_buttons(markup: InlineKeyboardMarkup) -> list:
    return [btn for row in markup.inline_keyboard for btn in row]


class _LanguageTestBase(unittest.IsolatedAsyncioTestCase):
    """Fresh SQLite database per test; original DB path restored."""

    def setUp(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self._db_path = tmp.name
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self._db_path
        db.init_db()
        CHANNELS.clear()

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path
        CHANNELS.clear()
        for suffix in ("", "-wal", "-shm"):
            path = self._db_path + suffix
            if os.path.exists(path):
                os.unlink(path)


# ── 1. First /start shows the language selection ──────────────────────


class TestFirstStartShowsSelection(_LanguageTestBase):
    async def test_new_user_start_shows_language_selection(self):
        """Rule 1: a brand-new user gets the prompt and the flow stops."""
        uid = 51001
        update = _msg_update(uid, "/start")
        ctx = _ctx()

        result = await start(update, ctx)

        self.assertEqual(result, LANGUAGE_SELECT)
        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        self.assertEqual(text, LANGUAGE_PROMPT)
        markup = update.message.reply_text.call_args[1]["reply_markup"]
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        # Anti-Bot must NOT start before a language is chosen
        self.assertNotIn("anti_bot_answer", ctx.user_data)
        # Nothing persisted yet
        self.assertIsNone(db.get_user_language(uid))

    async def test_prompt_exactly_matches_required_text(self):
        self.assertEqual(
            LANGUAGE_PROMPT,
            "🌐 اختر اللغة\n"
            "Choose your language\n"
            "Выберите язык\n"
            "زبان خود را انتخاب کنید",
        )
        self.assertEqual(
            [line for line in LANGUAGE_PROMPT.split("\n") if line],
            [
                "🌐 اختر اللغة",
                "Choose your language",
                "Выберите язык",
                "زبان خود را انتخاب کنید",
            ],
        )

    async def test_keyboard_has_exactly_the_four_buttons(self):
        update = _msg_update(51002, "/start")
        await start(update, _ctx())

        markup = update.message.reply_text.call_args[1]["reply_markup"]
        buttons = _flat_buttons(markup)
        self.assertEqual(len(buttons), 4)
        self.assertEqual(
            [b.text for b in buttons],
            ["🇪🇬 العربية", "🇬🇧 English", "🇷🇺 Русский", "🇮🇷 فارسی"],
        )
        self.assertEqual(
            [b.callback_data for b in buttons],
            ["lang:ar", "lang:en", "lang:ru", "lang:fa"],
        )
        # Buttons are inline — no reply keyboard is introduced
        self.assertEqual(LANGUAGE_BUTTONS[0], ("ar", "🇪🇬 العربية"))

    async def test_stray_text_while_pending_reprompts(self):
        """Text before choosing re-shows the prompt and stays pending."""
        uid = 51003
        ctx = _ctx()
        result = await language_prompt_again(_msg_update(uid, "hello"), ctx)

        self.assertEqual(result, LANGUAGE_SELECT)
        self.assertNotIn("anti_bot_answer", ctx.user_data)
        update = _msg_update(uid, "hello")
        await language_prompt_again(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        self.assertEqual(text, LANGUAGE_PROMPT)


# ── 2-5. Each selection persists ──────────────────────────────────────


class TestLanguagePersistence(_LanguageTestBase):
    async def test_each_selection_persists_and_resumes(self):
        for i, code in enumerate(ALL_CODES):
            with self.subTest(code=code):
                uid = 52000 + i
                ctx = _ctx()
                self.assertEqual(
                    await start(_msg_update(uid, "/start"), ctx),
                    LANGUAGE_SELECT,
                )

                cb = _cb_update(uid, f"lang:{code}")
                state = await language_selected(cb, ctx)

                # persisted exactly the chosen code
                self.assertEqual(db.get_user_language(uid), code)
                # flow resumed at the Anti-Bot step (rule 13)
                self.assertEqual(state, ANTI_BOT)
                self.assertIn("anti_bot_answer", ctx.user_data)
                cb.callback_query.answer.assert_called_once()
                cb.callback_query.edit_message_reply_markup.assert_called_once()

    async def test_each_code_written_by_helper_too(self):
        """db.set_user_language stores each of the four codes verbatim."""
        for i, code in enumerate(ALL_CODES):
            with self.subTest(code=code):
                uid = 52100 + i
                db.register_user(uid, None, None)
                self.assertTrue(db.set_user_language(uid, code))
                self.assertEqual(db.get_user_language(uid), code)


# ── 6-9. Existing users skip selection ────────────────────────────────


class TestExistingUserSkipsSelection(_LanguageTestBase):
    async def test_persisted_language_skips_selection(self):
        for i, code in enumerate(ALL_CODES):
            with self.subTest(code=code):
                uid = 53000 + i
                db.register_user(uid, None, None)
                db.set_user_language(uid, code)
                ctx = _ctx()
                update = _msg_update(uid, "/start")

                result = await start(update, ctx)

                # straight into the existing flow — no prompt
                self.assertEqual(result, ANTI_BOT)
                update.message.reply_text.assert_called_once()
                text = update.message.reply_text.call_args[0][0]
                self.assertIn("ما ناتج", text)          # anti-bot question
                self.assertNotIn("اختر اللغة", text)   # no language prompt
                self.assertNotIn(
                    "reply_markup", update.message.reply_text.call_args.kwargs
                )
                self.assertIn("anti_bot_answer", ctx.user_data)
                # language is untouched by /start
                self.assertEqual(db.get_user_language(uid), code)


# ── 10/11/14/16. Callback safety ──────────────────────────────────────


class TestCallbackSafety(_LanguageTestBase):
    async def test_invalid_language_callback_rejected_safely(self):
        uid = 54001
        db.register_user(uid, None, None)
        ctx = _ctx()
        for data in (
            "lang:de",
            "lang:AR",
            "lang:",
            "lang:english",
            "lang: ar",
            "other:ar",
            "lang:العربية",
        ):
            with self.subTest(data=data):
                cb = _cb_update(uid, data)
                state = await language_selected(cb, ctx)
                # stays in selection, answers the press, changes nothing
                self.assertEqual(state, LANGUAGE_SELECT)
                cb.callback_query.answer.assert_called_once()
                self.assertIsNone(db.get_user_language(uid))
                self.assertNotIn("anti_bot_answer", ctx.user_data)

    async def test_invalid_callback_rejected_by_stale_fallback_too(self):
        uid = 54002
        db.register_user(uid, None, None)
        cb = _cb_update(uid, "lang:xx")
        result = await language_callback_fallback(cb, _ctx())
        self.assertIsNone(result)
        cb.callback_query.answer.assert_called_once()
        self.assertIsNone(db.get_user_language(uid))

    async def test_repeated_valid_callback_is_idempotent(self):
        uid = 54003
        ctx = _ctx()
        self.assertEqual(
            await start(_msg_update(uid, "/start"), ctx), LANGUAGE_SELECT
        )
        self.assertEqual(
            await language_selected(_cb_update(uid, "lang:ar"), ctx),
            ANTI_BOT,
        )
        first_answer = ctx.user_data["anti_bot_answer"]

        # Repeat press is routed to the stale fallback: no state change.
        cb2 = _cb_update(uid, "lang:ar")
        result = await language_callback_fallback(cb2, ctx)
        self.assertIsNone(result)
        cb2.callback_query.answer.assert_called_once()
        self.assertEqual(db.get_user_language(uid), "ar")
        self.assertEqual(ctx.user_data["anti_bot_answer"], first_answer)

        # Even a direct re-run of the selection handler stays safe:
        # same persisted value, valid state, no exception.
        cb3 = _cb_update(uid, "lang:ar")
        state = await language_selected(cb3, ctx)
        self.assertEqual(state, ANTI_BOT)
        self.assertEqual(db.get_user_language(uid), "ar")

    async def test_stale_valid_press_without_selection_fails_safely(self):
        """Valid code but nothing pending (restart) → guidance, no write."""
        uid = 54004
        db.register_user(uid, None, None)
        cb = _cb_update(uid, "lang:fa")
        result = await language_callback_fallback(cb, _ctx())
        self.assertIsNone(result)
        cb.callback_query.answer.assert_called_once()
        self.assertIsNone(db.get_user_language(uid))

    async def test_unknown_user_selection_fails_safely(self):
        """Callback for a user with no row (stale DB) cannot corrupt."""
        uid = 54005  # never registered
        cb = _cb_update(uid, "lang:en")
        state = await language_selected(cb, _ctx())
        self.assertEqual(state, LANGUAGE_SELECT)
        self.assertIsNone(db.get_user_language(uid))

    async def test_pressing_user_language_only_is_affected(self):
        """Rule 14: nobody can change another user's language."""
        user_a, user_b = 54006, 54007
        ctx_a, ctx_b = _ctx(), _ctx()
        self.assertEqual(
            await start(_msg_update(user_a, "/start"), ctx_a),
            LANGUAGE_SELECT,
        )
        self.assertEqual(
            await start(_msg_update(user_b, "/start"), ctx_b),
            LANGUAGE_SELECT,
        )

        # B presses the language button (e.g. on their own prompt in a
        # shared chat): only B is affected, A stays unselected.
        cb = _cb_update(user_b, "lang:en")
        state = await language_selected(cb, ctx_b)

        self.assertEqual(state, ANTI_BOT)
        self.assertEqual(db.get_user_language(user_b), "en")
        self.assertIsNone(db.get_user_language(user_a))  # A untouched

        # A still chooses independently afterwards.
        state_a = await language_selected(
            _cb_update(user_a, "lang:ar"), ctx_a
        )
        self.assertEqual(state_a, ANTI_BOT)
        self.assertEqual(db.get_user_language(user_a), "ar")
        self.assertEqual(db.get_user_language(user_b), "en")

    def test_language_code_accepts_exactly_supported_codes(self):
        for code in ALL_CODES:
            self.assertEqual(_language_code(f"lang:{code}"), code)
        for bad in (
            "lang:de", "lang:AR", "lang:EN", "lang:arabic", "lang:",
            "lang: ar", "other:ar", "Ar", "рус", None, 7, ["lang:ar"], "",
        ):
            self.assertIsNone(_language_code(bad), bad)


# ── 14/15. Anti-Bot and subscription verification still run ───────────


class TestAntiBotAndSubscriptionAfterSelection(_LanguageTestBase):
    async def test_anti_bot_runs_after_language_selection(self):
        uid = 55001
        ctx = _ctx()
        self.assertEqual(
            await start(_msg_update(uid, "/start"), ctx), LANGUAGE_SELECT
        )
        self.assertEqual(
            await language_selected(_cb_update(uid, "lang:ar"), ctx),
            ANTI_BOT,
        )

        # answer the resumed anti-bot question correctly
        correct = str(ctx.user_data["anti_bot_answer"])
        result = await check_answer(_msg_update(uid, correct), ctx)

        self.assertEqual(result, ConversationHandler.END)
        self.assertNotIn("anti_bot_answer", ctx.user_data)

    async def test_wrong_anti_bot_answer_still_rejected(self):
        """Anti-Bot rules are unchanged after the language step."""
        uid = 55002
        ctx = _ctx()
        await start(_msg_update(uid, "/start"), ctx)
        await language_selected(_cb_update(uid, "lang:ru"), ctx)

        update = _msg_update(uid, "-999999")
        result = await check_answer(update, ctx)

        self.assertEqual(result, ANTI_BOT)
        # first reply is the rejection, then a fresh question follows
        first_reply = update.message.reply_text.call_args_list[0][0][0]
        self.assertIn("إجابة غير صحيحة", first_reply)
        self.assertIn("anti_bot_answer", ctx.user_data)

    async def test_subscription_verification_runs_after_anti_bot(self):
        """Rule 15: required-channel check still happens after Anti-Bot."""
        ch = Channel(
            slug="langch",
            channel_id=-100777,
            username="langch",
            title="LangCh",
            required=True,
        )
        CHANNELS["langch"] = ch
        unlock_user(55003)
        uid = 55003
        ctx = _ctx()

        self.assertEqual(
            await start(_msg_update(uid, "/start"), ctx), LANGUAGE_SELECT
        )
        self.assertEqual(
            await language_selected(_cb_update(uid, "lang:fa"), ctx),
            ANTI_BOT,
        )

        member = MagicMock()
        member.status = "member"
        ctx.bot.get_chat_member = AsyncMock(return_value=member)

        answer_update = _msg_update(uid, str(ctx.user_data["anti_bot_answer"]))
        result = await check_answer(answer_update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        ctx.bot.get_chat_member.assert_called_once_with(ch.channel_id, uid)
        texts = [
            c[0][0]
            for c in answer_update.message.reply_text.call_args_list
        ]
        self.assertTrue(
            any("مشترك في جميع القنوات" in t for t in texts), texts
        )
        self.assertFalse(is_locked(uid))


# ── 12/13. Referral attribution untouched ─────────────────────────────


class TestReferralUnaffected(_LanguageTestBase):
    async def test_referral_payload_survives_language_selection(self):
        """Rule 12: deep-link payload captured before the gate, kept after."""
        referrer, uid = 56001, 56002
        db.register_user(referrer, "referrer", "Referrer")
        ctx = _ctx(args=[str(referrer)])

        state = await start(_msg_update(uid, "/start"), ctx)
        self.assertEqual(state, LANGUAGE_SELECT)
        # payload already attributed before the language step
        self.assertEqual(db.get_user(uid)["referred_by"], referrer)

        result = await language_selected(_cb_update(uid, "lang:ar"), ctx)
        self.assertEqual(result, ANTI_BOT)
        # persisting the language did not touch attribution
        self.assertEqual(db.get_user(uid)["referred_by"], referrer)
        self.assertEqual(db.get_user_language(uid), "ar")

    async def test_self_referral_still_blocked_with_language_flow(self):
        """Rule 13: self-referral behavior is unchanged."""
        uid = 56003
        ctx = _ctx(args=[str(uid)])

        state = await start(_msg_update(uid, "/start"), ctx)
        self.assertEqual(state, LANGUAGE_SELECT)
        self.assertIsNone(db.get_user(uid)["referred_by"])

        await language_selected(_cb_update(uid, "lang:fa"), ctx)
        self.assertIsNone(db.get_user(uid)["referred_by"])
        self.assertEqual(db.get_user_language(uid), "fa")

    async def test_referral_payload_with_existing_language(self):
        """Rule 11: deep links behave exactly as before for known users.

        The user already exists (registered earlier with their original
        referrer), so a later payload must NOT change attribution —
        first-referrer-wins is unchanged by the language step.
        """
        first_referrer, other_referrer, uid = 56004, 56006, 56005
        db.register_user(first_referrer, "referrer", "Referrer")
        db.register_user(other_referrer, "other", "Other")
        db.register_user(uid, None, None, referred_by=first_referrer)
        db.set_user_language(uid, "en")
        ctx = _ctx(args=[str(other_referrer)])

        state = await start(_msg_update(uid, "/start"), ctx)

        # language is known → straight into the existing flow
        self.assertEqual(state, ANTI_BOT)
        # attribution unchanged: first referrer still wins
        self.assertEqual(db.get_user(uid)["referred_by"], first_referrer)
        self.assertEqual(db.get_user_language(uid), "en")


# ── 16. Admin commands unaffected ─────────────────────────────────────


class TestAdminUnaffected(_LanguageTestBase):
    @patch("bot.is_admin", side_effect=lambda uid: False)
    async def test_non_admin_addchannel_still_rejected(self, _mock):
        """Admin-only guard still rejects non-admins after the new step."""
        update = _msg_update(57001)
        ctx = _ctx()

        result = await addchannel_start(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", reply)


# ── Database layer constraints ────────────────────────────────────────


class TestDbLanguageHelpers(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self._db_path = tmp.name
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self._db_path
        db.init_db()

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path
        for suffix in ("", "-wal", "-shm"):
            path = self._db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    def test_supported_codes_are_exactly_ar_en_ru_fa(self):
        self.assertEqual(db.SUPPORTED_LANGUAGES, ("ar", "en", "ru", "fa"))

    def test_roundtrip_for_each_supported_code(self):
        for i, code in enumerate(ALL_CODES):
            with self.subTest(code=code):
                uid = 61000 + i
                db.register_user(uid, None, None)
                self.assertTrue(db.set_user_language(uid, code))
                self.assertEqual(db.get_user_language(uid), code)

    def test_invalid_values_rejected_without_writing(self):
        uid = 61100
        db.register_user(uid, None, None)
        self.assertTrue(db.set_user_language(uid, "ar"))
        for bad in ("de", "AR", "EN", "arabic", "", " ar", None, 5, b"ar"):
            with self.subTest(bad=bad):
                self.assertFalse(db.set_user_language(uid, bad))  # type: ignore[arg-type]
                self.assertEqual(db.get_user_language(uid), "ar")

    def test_unset_and_unknown_users_return_none(self):
        uid = 61200
        db.register_user(uid, None, None)
        self.assertIsNone(db.get_user_language(uid))       # unset user
        self.assertIsNone(db.get_user_language(999999999))  # unknown user

    def test_register_user_never_sets_a_language(self):
        uid = 61300
        db.register_user(uid, "someone", "Some", referred_by=None)
        self.assertIsNone(db.get_user_language(uid))

    def test_unknown_user_cannot_be_assigned_language(self):
        self.assertFalse(db.set_user_language(999999998, "ar"))
        self.assertIsNone(db.get_user_language(999999998))

    def test_repeated_set_is_idempotent(self):
        uid = 61400
        db.register_user(uid, None, None)
        self.assertTrue(db.set_user_language(uid, "ru"))
        self.assertTrue(db.set_user_language(uid, "ru"))  # repeat press
        self.assertEqual(db.get_user_language(uid), "ru")

    def test_invalid_stored_value_is_ignored(self):
        uid = 61500
        db.register_user(uid, None, None)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE users SET language = 'xx' WHERE user_id = ?", (uid,)
            )
        self.assertIsNone(db.get_user_language(uid))  # safe re-prompt

    def test_existing_users_remain_compatible_after_migration(self):
        """Rule 7: pre-migration rows keep working, NULL = not selected."""
        legacy = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        legacy.close()
        orig = db.DB_PATH
        try:
            conn = sqlite3.connect(legacy.name)
            conn.execute(
                """
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    referred_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "INSERT INTO users "
                "(user_id, username, first_name, referred_by) "
                "VALUES (?, ?, ?, ?)",
                (70001, "olduser", "Old", 70002),
            )
            conn.commit()
            conn.close()

            db.DB_PATH = legacy.name
            db.init_db()  # migration adds the language column

            # pre-existing row: no language yet, referral untouched
            self.assertIsNone(db.get_user_language(70001))
            self.assertEqual(db.get_user(70001)["referred_by"], 70002)

            # selection now works on the migrated row
            self.assertTrue(db.set_user_language(70001, "ru"))
            self.assertEqual(db.get_user_language(70001), "ru")
            self.assertEqual(db.get_user(70001)["referred_by"], 70002)
        finally:
            db.DB_PATH = orig
            for suffix in ("", "-wal", "-shm"):
                path = legacy.name + suffix
                if os.path.exists(path):
                    os.unlink(path)


if __name__ == "__main__":
    unittest.main()
