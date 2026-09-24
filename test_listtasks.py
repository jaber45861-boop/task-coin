"""
Focused tests for the admin-only /listtasks command (MT-TASK-08).

Covers:
  - Authorization: non-admins are rejected safely and the DB is never read.
  - Empty task list: admin gets a friendly empty message.
  - Populated task list: id, title, type, reward, and active status shown.
  - Command registration: "listtasks" is wired as a CommandHandler.
  - Read-only guarantee: listing never modifies the tasks table.

Run:
    python -m pytest test_listtasks.py -v
    # or
    python -m unittest test_listtasks.py -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import db
from config import CHANNELS
from bot import list_tasks

_TEST_ADMIN_ID = 88888888
_NON_ADMIN_ID = 999


# ── Test helpers ──────────────────────────────────────────────────────


def _make_update(user_id: int = 999, text: str | None = None) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.callback_query = None
    update.message.text = text
    return update


def _make_context(bot: MagicMock | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.bot = bot or MagicMock()
    ctx.user_data = {}
    return ctx


class _TempDbTestCase(unittest.IsolatedAsyncioTestCase):
    """Base: each test runs against a fresh, isolated SQLite database."""

    def setUp(self) -> None:
        CHANNELS.clear()
        test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = test_db.name
        test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)

    def tearDown(self) -> None:
        db.DB_PATH = self._original_db_path
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)
        for suffix in ("-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                os.unlink(p)


# ── Authorization ─────────────────────────────────────────────────────


class TestListTasksAuthorization(_TempDbTestCase):
    """Non-admins must be rejected safely with no data exposure."""

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_non_admin_rejected(self, _mock: MagicMock) -> None:
        """A non-admin receives the admin-only rejection message."""
        db.create_task(
            title="Secret Task", description="d", task_type="manual",
            reward=10, db_path=self.db_path,
        )
        update = _make_update(user_id=_NON_ADMIN_ID, text="/listtasks")
        ctx = _make_context()

        await list_tasks(update, ctx)

        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_non_admin_never_reads_tasks(self, _mock: MagicMock) -> None:
        """The tasks table is never queried for a non-admin."""
        db.create_task(
            title="Secret Task", description="d", task_type="manual",
            reward=10, db_path=self.db_path,
        )
        update = _make_update(user_id=_NON_ADMIN_ID, text="/listtasks")
        ctx = _make_context()

        with patch("db.list_tasks") as mock_list:
            await list_tasks(update, ctx)

        mock_list.assert_not_called()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_non_admin_reply_leaks_no_task_data(
        self, _mock: MagicMock
    ) -> None:
        """The rejection message contains no task titles or rewards."""
        db.create_task(
            title="TopSecretTitle", description="d", task_type="manual",
            reward=777, db_path=self.db_path,
        )
        update = _make_update(user_id=_NON_ADMIN_ID, text="/listtasks")
        ctx = _make_context()

        await list_tasks(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        self.assertNotIn("TopSecretTitle", reply)
        self.assertNotIn("777", reply)


# ── Empty task list ───────────────────────────────────────────────────


class TestListTasksEmpty(_TempDbTestCase):
    """Admin with no tasks gets the empty-list message."""

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_empty_list_message(self, _mock: MagicMock) -> None:
        update = _make_update(user_id=_TEST_ADMIN_ID, text="/listtasks")
        ctx = _make_context()

        await list_tasks(update, ctx)

        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("لا توجد مهام", reply)


# ── Populated task list ───────────────────────────────────────────────


class TestListTasksPopulated(_TempDbTestCase):
    """Admin sees id, title, type, reward, and active status per task."""

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_populated_list_shows_all_fields(
        self, _mock: MagicMock
    ) -> None:
        id_a = db.create_task(
            title="Subscribe", description="d", task_type="channel",
            reward=100, db_path=self.db_path,
        )
        id_b = db.create_task(
            title="Watch Video", description="d", task_type="watch",
            reward=250, active=False, db_path=self.db_path,
        )
        update = _make_update(user_id=_TEST_ADMIN_ID, text="/listtasks")
        ctx = _make_context()

        await list_tasks(update, ctx)

        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        # Header
        self.assertIn("قائمة المهام", reply)
        # Task A — active
        self.assertIn(str(id_a), reply)
        self.assertIn("Subscribe", reply)
        self.assertIn("channel", reply)
        self.assertIn("100", reply)
        # Task B — inactive
        self.assertIn(str(id_b), reply)
        self.assertIn("Watch Video", reply)
        self.assertIn("watch", reply)
        self.assertIn("250", reply)
        self.assertIn("لا", reply)  # active status for task B

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_listing_is_read_only(self, _mock: MagicMock) -> None:
        """Listing tasks never modifies the tasks table."""
        task_id = db.create_task(
            title="Stable", description="d", task_type="manual",
            reward=42, db_path=self.db_path,
        )
        before = db.get_task(task_id, db_path=self.db_path)
        update = _make_update(user_id=_TEST_ADMIN_ID, text="/listtasks")
        ctx = _make_context()

        await list_tasks(update, ctx)

        after = db.get_task(task_id, db_path=self.db_path)
        self.assertEqual(before, after)
        self.assertEqual(len(db.list_tasks(db_path=self.db_path)), 1)
        self.assertTrue(after["active"])


# ── Command registration ──────────────────────────────────────────────


class TestListTasksRegistration(unittest.TestCase):
    """/listtasks is registered as a Telegram CommandHandler."""

    def test_handler_registered(self) -> None:
        import inspect
        import bot as bot_mod

        source = inspect.getsource(bot_mod)
        self.assertIn('CommandHandler("listtasks", list_tasks)', source)

    def test_handler_callable(self) -> None:
        self.assertTrue(callable(list_tasks))


if __name__ == "__main__":
    unittest.main()
