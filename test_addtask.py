"""
Tests for the admin /addtask task-creation workflow (MT-TASK-07).

Covers:
  - Authorization: non-admin users are rejected.
  - Valid creation: telegram_channel task persisted with the MT-TASK-05
    nested task_data contract (provider / action / target / instructions).
  - Contract enforcement: a payload the verifier would reject (overlong
    instructions, unsafe registry slug) is rejected, nothing persisted.
  - Invalid channel_slug: rejected, nothing persisted.
  - Persistence: row lands in the tasks table with the right fields.
  - Malformed input (wrong field count / bad reward) is rejected safely.
  - Mandatory subscription channels are never mutated by /addtask.

Run:
    python -m pytest test_addtask.py -v
    # or
    python -m unittest test_addtask.py -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from config import CHANNELS, Channel
import db
from bot import ADD_TASK_USAGE, add_task
from telegram_channel_task_verifier import (
    parse_telegram_channel_task_data,
)

# Explicit test-only admin ID — never depends on ADMINS being non-empty.
_TEST_ADMIN_ID = 77777777


# ── Test helpers ──────────────────────────────────────────────────────


def _make_update(user_id: int = 999, text: str | None = None) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.text = text
    update.callback_query = None
    return update


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = {}
    return ctx


# ── Tests: add_task ───────────────────────────────────────────────────


class TestAddTask(unittest.IsolatedAsyncioTestCase):
    """Tests for the admin-only /addtask handler."""

    def setUp(self) -> None:
        # Temporary SQLite database — never touches the real one.
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(self.test_db_path)

        # Required-channel registry with one known slug.
        CHANNELS.clear()
        CHANNELS["main"] = Channel(
            slug="main",
            channel_id=-100111,
            username="mainchannel",
            title="Main Channel",
            required=True,
        )

    def tearDown(self) -> None:
        db.DB_PATH = self._original_db_path
        CHANNELS.clear()
        if os.path.exists(self.test_db_path):
            os.unlink(self.test_db_path)
        for suffix in ("-wal", "-shm"):
            p = self.test_db_path + suffix
            if os.path.exists(p):
                os.unlink(p)

    # ── Authorization ────────────────────────────────────────

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_non_admin_rejected(self, _mock: MagicMock) -> None:
        """A normal user gets the admin-only reply and creates nothing."""
        update = _make_update(
            user_id=999,
            text="/addtask title | desc | 500 | main",
        )
        ctx = _make_context()

        await add_task(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", reply)
        self.assertEqual(db.list_tasks(db_path=self.test_db_path), [])

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_admin_accepted(self, _mock: MagicMock) -> None:
        """The authorization check itself is exercised for admins too."""
        update = _make_update(
            user_id=_TEST_ADMIN_ID,
            text="/addtask title | desc | 500 | main",
        )
        ctx = _make_context()

        await add_task(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("تم إنشاء المهمة", reply)

    # ── Valid creation + persistence ─────────────────────────

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_valid_creation_persists_task(self, _mock: MagicMock) -> None:
        """A valid telegram_channel task is stored in the tasks table."""
        update = _make_update(
            user_id=_TEST_ADMIN_ID,
            text="/addtask انضم لقناتنا | اشترك في القناة | 500 | main",
        )
        ctx = _make_context()

        await add_task(update, ctx)

        tasks = db.list_tasks(db_path=self.test_db_path)
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["title"], "انضم لقناتنا")
        self.assertEqual(task["description"], "اشترك في القناة")
        self.assertEqual(task["type"], "telegram_channel")
        self.assertEqual(task["reward"], 500)
        task_data = json.loads(task["task_data"])
        # Exactly the MT-TASK-05 verifier contract — nested, never flat.
        self.assertEqual(task_data["provider"], "telegram")
        self.assertEqual(task_data["action"], "join_channel")
        self.assertEqual(
            task_data["target"], {"channel_slug": "main"}
        )
        self.assertEqual(
            task_data["instructions"], "اشترك في القناة"
        )
        self.assertNotIn("channel_slug", task_data)  # no flat key
        # Round-trip through the verifier's own contract validator.
        parse_telegram_channel_task_data(task["task_data"])

        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("تم إنشاء المهمة", reply)
        self.assertIn("main", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_persisted_task_fetchable_by_id(self, _mock: MagicMock) -> None:
        """The created task can be fetched back via get_task."""
        update = _make_update(
            user_id=_TEST_ADMIN_ID,
            text="/addtask Follow us | Join the channel | 250 | main",
        )
        ctx = _make_context()

        await add_task(update, ctx)

        tasks = db.list_tasks(db_path=self.test_db_path)
        self.assertEqual(len(tasks), 1)
        task = db.get_task(tasks[0]["id"], db_path=self.test_db_path)
        self.assertIsNotNone(task)
        self.assertEqual(task["type"], "telegram_channel")
        self.assertEqual(task["reward"], 250)
        self.assertEqual(
            json.loads(task["task_data"])["target"]["channel_slug"],
            "main",
        )

    # ── Invalid channel_slug ────────────────────────────────

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_invalid_channel_rejected(self, _mock: MagicMock) -> None:
        """Unknown channel_slug is rejected and nothing is persisted."""
        update = _make_update(
            user_id=_TEST_ADMIN_ID,
            text="/addtask title | desc | 500 | no_such_slug",
        )
        ctx = _make_context()

        await add_task(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("غير موجود", reply)
        self.assertEqual(db.list_tasks(db_path=self.test_db_path), [])

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_slug_with_at_prefix_normalized(
        self, _mock: MagicMock
    ) -> None:
        """A leading @ on the slug is stripped before validation."""
        update = _make_update(
            user_id=_TEST_ADMIN_ID,
            text="/addtask title | desc | 500 | @main",
        )
        ctx = _make_context()

        await add_task(update, ctx)

        tasks = db.list_tasks(db_path=self.test_db_path)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(
            json.loads(tasks[0]["task_data"])["target"]["channel_slug"],
            "main",
        )

    # ── Contract enforcement (writer never outlives the reader) ──

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_overlong_description_rejected(
        self, _mock: MagicMock
    ) -> None:
        """instructions beyond the contract bound are rejected safely."""
        update = _make_update(
            user_id=_TEST_ADMIN_ID,
            text="/addtask title | " + ("ب" * 1001) + " | 500 | main",
        )
        ctx = _make_context()

        await add_task(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("بيانات المهمة غير صالحة", reply)
        self.assertEqual(db.list_tasks(db_path=self.test_db_path), [])

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_unsafe_registry_slug_rejected(
        self, _mock: MagicMock
    ) -> None:
        """A registry slug the verifier contract cannot express is rejected."""
        CHANNELS["bad-slug!"] = Channel(
            slug="bad-slug!",
            channel_id=-200222,
            username="badslug",
            title="Bad Slug",
            required=True,
        )
        update = _make_update(
            user_id=_TEST_ADMIN_ID,
            text="/addtask title | desc | 500 | bad-slug!",
        )
        ctx = _make_context()

        await add_task(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("بيانات المهمة غير صالحة", reply)
        self.assertEqual(db.list_tasks(db_path=self.test_db_path), [])

    # ── Malformed input ─────────────────────────────────────

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_wrong_field_count_shows_usage(self, _mock: MagicMock) -> None:
        """Fewer than 4 fields → usage message, nothing persisted."""
        update = _make_update(
            user_id=_TEST_ADMIN_ID,
            text="/addtask title | desc | 500",
        )
        ctx = _make_context()

        await add_task(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        self.assertEqual(reply, ADD_TASK_USAGE)
        self.assertEqual(db.list_tasks(db_path=self.test_db_path), [])

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_non_numeric_reward_rejected(self, _mock: MagicMock) -> None:
        """Non-numeric reward → error, nothing persisted."""
        update = _make_update(
            user_id=_TEST_ADMIN_ID,
            text="/addtask title | desc | abc | main",
        )
        ctx = _make_context()

        await add_task(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("النقاط", reply)
        self.assertEqual(db.list_tasks(db_path=self.test_db_path), [])

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_negative_reward_rejected(self, _mock: MagicMock) -> None:
        """Negative reward → error, nothing persisted."""
        update = _make_update(
            user_id=_TEST_ADMIN_ID,
            text="/addtask title | desc | -5 | main",
        )
        ctx = _make_context()

        await add_task(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("النقاط", reply)
        self.assertEqual(db.list_tasks(db_path=self.test_db_path), [])

    # ── Registry safety ─────────────────────────────────────

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_required_channels_registry_unchanged(
        self, _mock: MagicMock
    ) -> None:
        """/addtask never adds, removes, or mutates required channels."""
        update = _make_update(
            user_id=_TEST_ADMIN_ID,
            text="/addtask title | desc | 500 | main",
        )
        ctx = _make_context()

        await add_task(update, ctx)

        self.assertEqual(list(CHANNELS.keys()), ["main"])
        self.assertEqual(CHANNELS["main"].channel_id, -100111)


if __name__ == "__main__":
    unittest.main()
