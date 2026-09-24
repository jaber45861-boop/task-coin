"""
Focused tests for the admin-only /offtask command (MT-TASK-10).

Covers:
  - Authorization: non-admins are rejected safely.
  - Validation: missing / non-digit / unknown task ids get safe errors.
  - Deactivation: valid task becomes active=False via db.update_task(),
    and the row is never deleted.
  - Downstream: TaskCatalog excludes it, StartGate rejects it,
    AttemptPolicy rejects it, /listtasks shows it as inactive.
  - Invariants: user_tasks, task_submissions, and ledger row counts
    are unchanged by /offtask.
  - Source guarantee: bot.py never calls db.delete_task().

Run:
    python -m pytest test_offtask.py -v
    # or
    python -m unittest test_offtask.py -v
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import db
from config import CHANNELS
from bot import list_tasks, off_task
from task_attempt import TaskAttemptPolicy
from task_catalog import TaskCatalog
from task_start import StartGateError, TaskStartGate

_TEST_ADMIN_ID = 88888888
_NON_ADMIN_ID = 999
_TEST_USER_ID = 555001


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

    def _count(self, table: str) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        finally:
            conn.close()

    def _create_task(self, title: str = "Sample Task", **kwargs) -> int:
        kwargs.setdefault("description", "d")
        kwargs.setdefault("task_type", "manual")
        kwargs.setdefault("reward", 10)
        return db.create_task(title=title, db_path=self.db_path, **kwargs)


# ── Authorization ─────────────────────────────────────────────────────


class TestOffTaskAuthorization(_TempDbTestCase):
    """Non-admins must be rejected safely with no side effects."""

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_non_admin_rejected(self, _mock: MagicMock) -> None:
        """A non-admin receives the admin-only rejection message."""
        task_id = self._create_task(title="Secret Task")
        update = _make_update(user_id=_NON_ADMIN_ID, text=f"/offtask {task_id}")
        ctx = _make_context()

        await off_task(update, ctx)

        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", reply)
        # No side effects: the task is untouched.
        self.assertTrue(db.get_task(task_id)["active"])

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_non_admin_never_updates_task(
        self, _mock: MagicMock
    ) -> None:
        """db.update_task is never reached for a non-admin."""
        task_id = self._create_task()
        update = _make_update(user_id=_NON_ADMIN_ID, text=f"/offtask {task_id}")
        ctx = _make_context()

        with patch("db.update_task") as mock_update:
            await off_task(update, ctx)

        mock_update.assert_not_called()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_non_admin_reply_leaks_no_task_data(
        self, _mock: MagicMock
    ) -> None:
        """The rejection message contains no task titles."""
        self._create_task(title="TopSecretTitle")
        update = _make_update(user_id=_NON_ADMIN_ID, text="/offtask 1")
        ctx = _make_context()

        await off_task(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        self.assertNotIn("TopSecretTitle", reply)


# ── Input validation ──────────────────────────────────────────────────


class TestOffTaskValidation(_TempDbTestCase):
    """Bad ids must produce safe usage / not-found errors."""

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_missing_id_shows_usage(self, _mock: MagicMock) -> None:
        update = _make_update(user_id=_TEST_ADMIN_ID, text="/offtask")
        ctx = _make_context()

        await off_task(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("صيغة خاطئة", reply)
        self.assertIn("/offtask <id>", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_non_digit_id_rejected(self, _mock: MagicMock) -> None:
        update = _make_update(user_id=_TEST_ADMIN_ID, text="/offtask abc")
        ctx = _make_context()

        await off_task(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("صيغة خاطئة", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_unknown_task_rejected(self, _mock: MagicMock) -> None:
        """An unknown id returns a safe not-found error, not a crash."""
        self._create_task()  # a real task exists, but with a different id
        update = _make_update(user_id=_TEST_ADMIN_ID, text="/offtask 424242")
        ctx = _make_context()

        await off_task(update, ctx)  # must not raise

        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("لا توجد مهمة", reply)
        self.assertIn("424242", reply)
        # Existing task untouched.
        self.assertEqual(len(db.list_tasks(db_path=self.db_path)), 1)
        self.assertTrue(
            all(t["active"] for t in db.list_tasks(db_path=self.db_path))
        )

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_unknown_task_does_not_call_update(
        self, _mock: MagicMock
    ) -> None:
        update = _make_update(user_id=_TEST_ADMIN_ID, text="/offtask 999999")
        ctx = _make_context()

        with patch("db.update_task") as mock_update:
            await off_task(update, ctx)

        mock_update.assert_not_called()


# ── Deactivation ──────────────────────────────────────────────────────


class TestOffTaskDeactivate(_TempDbTestCase):
    """A valid id flips only the active flag — never deletes the row."""

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_valid_task_becomes_inactive(
        self, _mock: MagicMock
    ) -> None:
        task_id = self._create_task(title="Deactivate Me", reward=77)
        update = _make_update(user_id=_TEST_ADMIN_ID, text=f"/offtask {task_id}")
        ctx = _make_context()

        await off_task(update, ctx)

        # Reply: task id, title, active status in Arabic.
        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn(str(task_id), reply)
        self.assertIn("Deactivate Me", reply)
        self.assertIn("نشطة: لا", reply)

        # Row still present, only active flipped.
        task = db.get_task(task_id)
        self.assertIsNotNone(task)
        self.assertFalse(task["active"])
        self.assertEqual(task["title"], "Deactivate Me")
        self.assertEqual(task["reward"], 77)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_only_active_field_changes(
        self, _mock: MagicMock
    ) -> None:
        """Every other task field is byte-for-byte unchanged."""
        task_id = self._create_task(title="Stable Fields", reward=5)
        before = db.get_task(task_id, db_path=self.db_path)
        update = _make_update(user_id=_TEST_ADMIN_ID, text=f"/offtask {task_id}")
        ctx = _make_context()

        await off_task(update, ctx)

        after = db.get_task(task_id, db_path=self.db_path)
        self.assertNotEqual(before["active"], after["active"])
        for key in before:
            if key == "active":
                continue
            self.assertEqual(before[key], after[key], f"field {key} changed")

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_task_row_not_deleted(self, _mock: MagicMock) -> None:
        """The tasks table keeps exactly its rows after /offtask."""
        keep_id = self._create_task(title="Keep")
        off_id = self._create_task(title="Off")
        update = _make_update(user_id=_TEST_ADMIN_ID, text=f"/offtask {off_id}")
        ctx = _make_context()

        await off_task(update, ctx)

        self.assertEqual(len(db.list_tasks(db_path=self.db_path)), 2)
        self.assertIsNotNone(db.get_task(keep_id))
        self.assertIsNotNone(db.get_task(off_id))


# ── Row-count invariants ──────────────────────────────────────────────


class TestOffTaskRowCounts(_TempDbTestCase):
    """user_tasks, task_submissions, and ledger rows are never touched."""

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_row_counts_unchanged(self, _mock: MagicMock) -> None:
        # Seed one row in each protected table.
        db.register_user(_TEST_USER_ID, "seeduser", "Seed")
        task_id = self._create_task(title="Seeded")
        db.create_user_task(_TEST_USER_ID, task_id, self.db_path)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO task_submissions "
                "(user_id, task_id, attempt_number, idempotency_key) "
                "VALUES (?, ?, 1, ?)",
                (_TEST_USER_ID, task_id, f"key-{task_id}"),
            )
            conn.execute(
                "INSERT INTO ledger "
                "(user_id, entry_type, amount_units, available_delta, "
                "held_delta, reference_type, reference_id) "
                "VALUES (?, 'credit', 10, 10, 0, 'task', ?)",
                (_TEST_USER_ID, str(task_id)),
            )
            conn.commit()
        finally:
            conn.close()

        before = {t: self._count(t) for t in
                  ("user_tasks", "task_submissions", "ledger")}
        self.assertEqual(before["user_tasks"], 1)
        self.assertEqual(before["task_submissions"], 1)
        self.assertEqual(before["ledger"], 1)

        update = _make_update(user_id=_TEST_ADMIN_ID, text=f"/offtask {task_id}")
        ctx = _make_context()
        await off_task(update, ctx)

        after = {t: self._count(t) for t in
                 ("user_tasks", "task_submissions", "ledger")}
        self.assertEqual(before, after)
        # And the task itself still exists (soft delete only).
        self.assertIsNotNone(db.get_task(task_id))
        self.assertFalse(db.get_task(task_id)["active"])


# ── Downstream gates ──────────────────────────────────────────────────


class TestOffTaskDownstream(_TempDbTestCase):
    """Inactive tasks are excluded / rejected everywhere downstream."""

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_catalog_excludes_inactive_task(
        self, _mock: MagicMock
    ) -> None:
        active_id = self._create_task(title="Still Active")
        off_id = self._create_task(title="Gone From Catalog")
        update = _make_update(user_id=_TEST_ADMIN_ID, text=f"/offtask {off_id}")
        ctx = _make_context()

        await off_task(update, ctx)

        ids = [t.id for t in TaskCatalog().list_available_tasks()]
        self.assertIn(active_id, ids)
        self.assertNotIn(off_id, ids)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_start_gate_rejects_inactive_task(
        self, _mock: MagicMock
    ) -> None:
        db.register_user(_TEST_USER_ID, "gateuser", "Gate")
        task_id = self._create_task(title="Gated")
        update = _make_update(user_id=_TEST_ADMIN_ID, text=f"/offtask {task_id}")
        ctx = _make_context()

        await off_task(update, ctx)

        with self.assertRaises(StartGateError) as cm:
            TaskStartGate().start(_TEST_USER_ID, task_id)
        self.assertIn("not active", str(cm.exception))

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_attempt_policy_rejects_inactive_task(
        self, _mock: MagicMock
    ) -> None:
        db.register_user(_TEST_USER_ID, "attemptuser", "Attempt")
        task_id = self._create_task(title="Policy Gated")
        update = _make_update(user_id=_TEST_ADMIN_ID, text=f"/offtask {task_id}")
        ctx = _make_context()

        await off_task(update, ctx)

        result = TaskAttemptPolicy.can_submit(_TEST_USER_ID, task_id)
        self.assertFalse(result.allowed)
        self.assertIn("not active", result.reason)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_listtasks_shows_task_as_inactive(
        self, _mock: MagicMock
    ) -> None:
        task_id = self._create_task(title="Shown Inactive")
        update = _make_update(user_id=_TEST_ADMIN_ID, text=f"/offtask {task_id}")
        ctx = _make_context()
        await off_task(update, ctx)

        list_update = _make_update(user_id=_TEST_ADMIN_ID, text="/listtasks")
        await list_tasks(list_update, ctx)

        list_update.message.reply_text.assert_called_once()
        reply = list_update.message.reply_text.call_args[0][0]
        self.assertIn(str(task_id), reply)
        self.assertIn("Shown Inactive", reply)
        self.assertIn("نشطة: لا", reply)


# ── Source guarantees & registration ──────────────────────────────────


class TestOffTaskSourceGuarantees(unittest.TestCase):
    """bot.py must soft-delete only: no db.delete_task(), no schema tricks."""

    def test_bot_does_not_call_delete_task(self) -> None:
        import bot as bot_mod

        source = inspect.getsource(bot_mod)
        self.assertNotIn("db.delete_task(", source)

    def test_bot_uses_update_task_active_false(self) -> None:
        import bot as bot_mod

        source = inspect.getsource(bot_mod)
        self.assertIn("db.update_task(task_id, active=False)", source)

    def test_handler_registered(self) -> None:
        import bot as bot_mod

        source = inspect.getsource(bot_mod)
        self.assertIn('CommandHandler("offtask", off_task)', source)

    def test_off_task_callable(self) -> None:
        self.assertTrue(callable(off_task))


if __name__ == "__main__":
    unittest.main()
