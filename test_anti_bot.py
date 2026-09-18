"""
Tests for the anti-bot 3-attempt limit feature.

Run:
    python -m pytest test_anti_bot.py -v
    # or
    python -m unittest test_anti_bot.py -v
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.ext import ConversationHandler

from bot import (
    ANTI_BOT,
    ANTI_BOT_BLOCKED,
    MAX_ANTI_BOT_ATTEMPTS,
    _blocked,
    check_answer,
    start,
)
from config import CHANNELS
from subscription import unlock_user

_TEST_USER_ID = 77777


# ── Test helpers ──────────────────────────────────────────────────────


def _make_update(user_id: int, text: str | None = None) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    if text is not None:
        update.message.text = text
    else:
        update.message.text = None
    return update


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.user_data = {}
    return ctx


def _set_correct_answer(ctx: MagicMock, answer: int = 10) -> None:
    """Pre-set a known answer in user_data."""
    ctx.user_data["anti_bot_answer"] = answer


# ── Tests: /start initialises anti-bot ────────────────────────────────


class TestStartInit(unittest.IsolatedAsyncioTestCase):
    """Tests that /start sets up the anti-bot question."""

    async def test_start_returns_anti_bot_state(self) -> None:
        """start() should return ANTI_BOT state."""
        update = _make_update(_TEST_USER_ID, "/start")
        ctx = _make_context()

        result = await start(update, ctx)

        self.assertEqual(result, ANTI_BOT)
        self.assertIn("anti_bot_answer", ctx.user_data)
        self.assertIsInstance(ctx.user_data["anti_bot_answer"], int)

    async def test_start_initialises_attempts_to_zero(self) -> None:
        """start() should set anti_bot_attempts to 0."""
        update = _make_update(_TEST_USER_ID, "/start")
        ctx = _make_context()

        await start(update, ctx)

        self.assertEqual(ctx.user_data["anti_bot_attempts"], 0)

    async def test_start_sends_question(self) -> None:
        """start() should send a math question message."""
        update = _make_update(_TEST_USER_ID, "/start")
        ctx = _make_context()

        await start(update, ctx)

        update.message.reply_text.assert_called_once()
        question = update.message.reply_text.call_args[0][0]
        self.assertIn("ما ناتج", question)

    async def test_start_resets_on_repeat(self) -> None:
        """Starting again resets attempts to 0."""
        update = _make_update(_TEST_USER_ID, "/start")
        ctx = _make_context()
        ctx.user_data["anti_bot_attempts"] = 2

        await start(update, ctx)

        self.assertEqual(ctx.user_data["anti_bot_attempts"], 0)


# ── Tests: correct answer on first try ───────────────────────────────


class TestCorrectFirstTry(unittest.IsolatedAsyncioTestCase):
    """Correct answer on the first attempt passes immediately."""

    def setUp(self) -> None:
        CHANNELS.clear()
        unlock_user(_TEST_USER_ID)

    async def test_correct_answer_proceeds(self) -> None:
        """Correct answer returns ConversationHandler.END (passed anti-bot)."""
        update = _make_update(_TEST_USER_ID, "25")
        ctx = _make_context()
        _set_correct_answer(ctx, 25)

        result = await check_answer(update, ctx)

        self.assertEqual(result, ConversationHandler.END)

    async def test_correct_answer_no_attempts_increment(self) -> None:
        """Correct answer should not store attempts in user_data."""
        update = _make_update(_TEST_USER_ID, "25")
        ctx = _make_context()
        _set_correct_answer(ctx, 25)

        await check_answer(update, ctx)

        self.assertNotIn("anti_bot_attempts", ctx.user_data)
        self.assertNotIn("anti_bot_answer", ctx.user_data)

    async def test_correct_answer_success_message(self) -> None:
        """Correct answer produces a success message."""
        update = _make_update(_TEST_USER_ID, "25")
        ctx = _make_context()
        _set_correct_answer(ctx, 25)

        await check_answer(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("تحقق ناجح", reply)


# ── Tests: wrong then correct ────────────────────────────────────────


class TestWrongThenCorrect(unittest.IsolatedAsyncioTestCase):
    """One wrong answer followed by a correct answer."""

    def setUp(self) -> None:
        CHANNELS.clear()
        unlock_user(_TEST_USER_ID)

    async def test_wrong_then_correct_passes(self) -> None:
        """Wrong answer → new question, correct answer → passes."""
        # First attempt: wrong
        update1 = _make_update(_TEST_USER_ID, "999")
        ctx = _make_context()
        _set_correct_answer(ctx, 10)

        result1 = await check_answer(update1, ctx)
        self.assertEqual(result1, ANTI_BOT)
        self.assertEqual(ctx.user_data["anti_bot_attempts"], 1)
        # New question was generated
        self.assertIn("anti_bot_answer", ctx.user_data)
        new_answer = ctx.user_data["anti_bot_answer"]

        # Second attempt: correct
        update2 = _make_update(_TEST_USER_ID, str(new_answer))
        result2 = await check_answer(update2, ctx)
        self.assertEqual(result2, ConversationHandler.END)
        self.assertNotIn("anti_bot_attempts", ctx.user_data)

    async def test_wrong_answer_remaining_message(self) -> None:
        """Wrong answer shows remaining attempts."""
        update = _make_update(_TEST_USER_ID, "999")
        ctx = _make_context()
        _set_correct_answer(ctx, 10)

        await check_answer(update, ctx)

        # Should have two reply calls: the error + the new question
        self.assertEqual(update.message.reply_text.call_count, 2)
        error_msg = update.message.reply_text.call_args_list[0][0][0]
        self.assertIn("متبقي", error_msg)
        self.assertIn("2 محاولة", error_msg)

    async def test_wrong_answer_stays_in_anti_bot(self) -> None:
        """Wrong answer keeps user in ANTI_BOT state."""
        update = _make_update(_TEST_USER_ID, "999")
        ctx = _make_context()
        _set_correct_answer(ctx, 10)

        result = await check_answer(update, ctx)

        self.assertEqual(result, ANTI_BOT)


# ── Tests: wrong twice then correct ──────────────────────────────────


class TestWrongTwiceThenCorrect(unittest.IsolatedAsyncioTestCase):
    """Two wrong answers followed by a correct answer."""

    def setUp(self) -> None:
        CHANNELS.clear()
        unlock_user(_TEST_USER_ID)

    async def test_wrong_twice_then_correct_passes(self) -> None:
        """2 wrong → still in ANTI_BOT, correct → passes."""
        # First attempt: wrong
        update1 = _make_update(_TEST_USER_ID, "0")
        ctx = _make_context()
        _set_correct_answer(ctx, 10)
        await check_answer(update1, ctx)
        self.assertEqual(ctx.user_data["anti_bot_attempts"], 1)

        # Second attempt: wrong
        answer2 = ctx.user_data["anti_bot_answer"]
        update2 = _make_update(_TEST_USER_ID, "0")
        await check_answer(update2, ctx)
        self.assertEqual(ctx.user_data["anti_bot_attempts"], 2)

        # Third attempt: correct
        answer3 = ctx.user_data["anti_bot_answer"]
        update3 = _make_update(_TEST_USER_ID, str(answer3))
        result = await check_answer(update3, ctx)
        self.assertEqual(result, ConversationHandler.END)

    async def test_second_wrong_shows_one_remaining(self) -> None:
        """Second wrong answer shows 1 remaining attempt."""
        # First wrong
        update1 = _make_update(_TEST_USER_ID, "0")
        ctx = _make_context()
        _set_correct_answer(ctx, 10)
        await check_answer(update1, ctx)

        # Second wrong
        update2 = _make_update(_TEST_USER_ID, "0")
        await check_answer(update2, ctx)

        error_msg = update2.message.reply_text.call_args_list[0][0][0]
        self.assertIn("متبقي 1 محاولة", error_msg)


# ── Tests: 3 errors block ────────────────────────────────────────────


class TestThreeErrorsBlock(unittest.IsolatedAsyncioTestCase):
    """Three wrong answers block the user."""

    def setUp(self) -> None:
        CHANNELS.clear()

    async def test_three_wrong_blocks(self) -> None:
        """3 wrong answers → ANTI_BOT_BLOCKED state."""
        update = _make_update(_TEST_USER_ID, "9999")
        ctx = _make_context()
        _set_correct_answer(ctx, 10)

        # Attempt 1
        result = await check_answer(update, ctx)
        self.assertEqual(result, ANTI_BOT)
        self.assertEqual(ctx.user_data["anti_bot_attempts"], 1)

        # Attempt 2
        update2 = _make_update(_TEST_USER_ID, "9999")
        result = await check_answer(update2, ctx)
        self.assertEqual(result, ANTI_BOT)
        self.assertEqual(ctx.user_data["anti_bot_attempts"], 2)

        # Attempt 3
        update3 = _make_update(_TEST_USER_ID, "9999")
        result = await check_answer(update3, ctx)
        self.assertEqual(result, ANTI_BOT_BLOCKED)

    async def test_three_wrong_clears_user_data(self) -> None:
        """After 3 wrong answers, anti_bot_answer is cleaned up."""
        update = _make_update(_TEST_USER_ID, "9999")
        ctx = _make_context()
        _set_correct_answer(ctx, 10)

        await check_answer(update, ctx)

        update2 = _make_update(_TEST_USER_ID, "9999")
        await check_answer(update2, ctx)

        update3 = _make_update(_TEST_USER_ID, "9999")
        await check_answer(update3, ctx)

        self.assertNotIn("anti_bot_answer", ctx.user_data)
        self.assertNotIn("anti_bot_attempts", ctx.user_data)

    async def test_three_wrong_sends_block_message(self) -> None:
        """3rd wrong answer sends the blocked message."""
        update = _make_update(_TEST_USER_ID, "9999")
        ctx = _make_context()
        _set_correct_answer(ctx, 10)

        await check_answer(update, ctx)

        update2 = _make_update(_TEST_USER_ID, "9999")
        await check_answer(update2, ctx)

        update3 = _make_update(_TEST_USER_ID, "9999")
        await check_answer(update3, ctx)

        # The last call should be the block message
        block_msg = update3.message.reply_text.call_args_list[-1][0][0]
        self.assertIn("تجاوزت الحد الأقصى", block_msg)

    async def test_three_invalid_text_answers_block(self) -> None:
        """Three non-integer answers also block."""
        ctx = _make_context()
        _set_correct_answer(ctx, 10)

        update1 = _make_update(_TEST_USER_ID, "abc")
        result = await check_answer(update1, ctx)
        self.assertEqual(result, ANTI_BOT)

        update2 = _make_update(_TEST_USER_ID, "xyz")
        result = await check_answer(update2, ctx)
        self.assertEqual(result, ANTI_BOT)

        update3 = _make_update(_TEST_USER_ID, "hello")
        result = await check_answer(update3, ctx)
        self.assertEqual(result, ANTI_BOT_BLOCKED)

    async def test_mixed_invalid_and_wrong_answers_block(self) -> None:
        """Mix of non-integer and wrong integer answers counts toward limit."""
        ctx = _make_context()
        _set_correct_answer(ctx, 10)

        # 1: non-integer
        update1 = _make_update(_TEST_USER_ID, "abc")
        result = await check_answer(update1, ctx)
        self.assertEqual(result, ANTI_BOT)

        # 2: wrong integer
        update2 = _make_update(_TEST_USER_ID, "999")
        result = await check_answer(update2, ctx)
        self.assertEqual(result, ANTI_BOT)

        # 3: wrong integer → blocked
        update3 = _make_update(_TEST_USER_ID, "9999")
        result = await check_answer(update3, ctx)
        self.assertEqual(result, ANTI_BOT_BLOCKED)


# ── Tests: answer after being blocked ────────────────────────────────


class TestAnswerAfterBlock(unittest.IsolatedAsyncioTestCase):
    """Sending an answer after being blocked is handled."""

    async def test_blocked_handler_replies_with_start_prompt(self) -> None:
        """_blocked handler tells user to /start again."""
        update = _make_update(_TEST_USER_ID, "25")
        ctx = _make_context()

        result = await _blocked(update, ctx)

        self.assertEqual(result, ANTI_BOT_BLOCKED)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("تجاوزت الحد الأقصى", reply)
        self.assertIn("/start", reply)

    async def test_blocked_handler_does_not_modify_user_data(self) -> None:
        """_blocked handler does not alter user_data."""
        update = _make_update(_TEST_USER_ID, "anything")
        ctx = _make_context()
        ctx.user_data["some_key"] = "some_value"

        await _blocked(update, ctx)

        self.assertEqual(ctx.user_data["some_key"], "some_value")

    async def test_blocked_with_empty_text(self) -> None:
        """_blocked handles empty text."""
        update = _make_update(_TEST_USER_ID, "")
        ctx = _make_context()

        result = await _blocked(update, ctx)

        self.assertEqual(result, ANTI_BOT_BLOCKED)


# ── Tests: constants and configuration ───────────────────────────────


class TestAntiBotConfig(unittest.TestCase):
    """Verify anti-bot configuration constants."""

    def test_max_attempts_is_three(self) -> None:
        self.assertEqual(MAX_ANTI_BOT_ATTEMPTS, 3)

    def test_anti_bot_blocked_state_differs(self) -> None:
        self.assertNotEqual(ANTI_BOT, ANTI_BOT_BLOCKED)

    def test_blocked_state_is_1(self) -> None:
        self.assertEqual(ANTI_BOT_BLOCKED, 1)

    def test_anti_bot_state_is_0(self) -> None:
        self.assertEqual(ANTI_BOT, 0)


# ── Tests: start → check_answer flow ────────────────────────────────


class TestStartThenAnswerFlow(unittest.IsolatedAsyncioTestCase):
    """Integration test: start then answer correctly in one flow."""

    def setUp(self) -> None:
        CHANNELS.clear()
        unlock_user(_TEST_USER_ID)

    async def test_full_correct_flow(self) -> None:
        """start → correct answer → passes."""
        start_update = _make_update(_TEST_USER_ID, "/start")
        ctx = _make_context()

        result = await start(start_update, ctx)
        self.assertEqual(result, ANTI_BOT)

        correct = ctx.user_data["anti_bot_answer"]
        answer_update = _make_update(_TEST_USER_ID, str(correct))

        result = await check_answer(answer_update, ctx)
        self.assertEqual(result, ConversationHandler.END)

    async def test_full_wrong_then_correct_flow(self) -> None:
        """start → wrong → new question → correct → passes."""
        start_update = _make_update(_TEST_USER_ID, "/start")
        ctx = _make_context()

        await start(start_update, ctx)

        # Wrong answer (-999 can never be the correct math result)
        wrong_update = _make_update(_TEST_USER_ID, "-999")
        result = await check_answer(wrong_update, ctx)
        self.assertEqual(result, ANTI_BOT)

        # Correct answer
        correct = ctx.user_data["anti_bot_answer"]
        correct_update = _make_update(_TEST_USER_ID, str(correct))
        result = await check_answer(correct_update, ctx)
        self.assertEqual(result, ConversationHandler.END)

    async def test_full_three_wrong_flow(self) -> None:
        """start → 3 wrong → blocked."""
        start_update = _make_update(_TEST_USER_ID, "/start")
        ctx = _make_context()

        await start(start_update, ctx)

        for _ in range(2):
            wrong_update = _make_update(_TEST_USER_ID, "9999")
            result = await check_answer(wrong_update, ctx)
            self.assertEqual(result, ANTI_BOT)

        wrong_update = _make_update(_TEST_USER_ID, "9999")
        result = await check_answer(wrong_update, ctx)
        self.assertEqual(result, ANTI_BOT_BLOCKED)


# ── Tests: no ReplyKeyboard introduced ───────────────────────────────


class TestNoReplyKeyboard(unittest.TestCase):
    """Verify no ReplyKeyboard was introduced."""

    def test_no_reply_keyboard_in_bot(self) -> None:
        """bot.py should not import or use ReplyKeyboard."""
        import bot as bot_mod
        import inspect
        source = inspect.getsource(bot_mod)
        self.assertNotIn("ReplyKeyboard", source)
        self.assertNotIn("ReplyKeyboardMarkup", source)
        self.assertNotIn("ReplyKeyboardRemove", source)


if __name__ == "__main__":
    unittest.main()
