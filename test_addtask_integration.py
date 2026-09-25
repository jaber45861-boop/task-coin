"""
Focused integration tests — /addtask ⇄ Mini App Task API (MT-TASK-12)
====================================================================

Proves the admin /addtask writer and the registered MT-TASK-05
verifier share ONE task_data contract end-to-end through the
production HTTP pipeline (no mocks of the verifier pipeline itself —
only the Telegram membership lookup is faked):

  - an /addtask-created telegram_channel task is listed by
    GET /api/tasks with a join_url resolved from the channel registry
  - a valid member submission reaches completion using the trusted
    server-side task data (available → started → completed)
  - a non-member submission is rejected by the existing verifier
    (task stays started, never completed)

Run:
    python3 -m pytest test_addtask_integration.py -v
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import db
import serve_miniapp
from bot import add_task
from config import CHANNELS, Channel
from telegram_channel_task_verifier import (
    TELEGRAM_CHANNEL_TASK_TYPE,
    TelegramChannelTaskVerifier,
    parse_telegram_channel_task_data,
    register_telegram_channel_task_verifier,
)

# Reuse the existing, independently implemented Telegram initData helper.
from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

# Explicit test-only admin ID — never depends on ADMINS being non-empty.
_TEST_ADMIN_ID = 77777777

USER_A = 1001

CHANNEL_SLUG = "main"
CHANNEL_ID = -100111
CHANNEL_USERNAME = "mainchannel"

_ADDTASK_TEXT = "/addtask انضم لقناتنا | اشترك في القناة | 500 | " + CHANNEL_SLUG


# ── Membership checker (replaces the live Telegram lookup in tests) ───


class _Members:
    """Fake Telegram membership lookup recording every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self.statuses: dict[int, str] = {}
        self.error: Exception | None = None

    def __call__(self, channel_id: int, user_id: int) -> str:
        self.calls.append((channel_id, user_id))
        if self.error is not None:
            raise self.error
        return self.statuses.get(user_id, "left")


@pytest.fixture
def members():
    return _Members()


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path, members):
    """Environment + isolated database + configured channel + faked
    membership lookup (no real Telegram API call is ever made)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)

    db_path = str(tmp_path / "addtask_integration.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER_A, "alice", "Alice")

    CHANNELS.clear()
    CHANNELS[CHANNEL_SLUG] = Channel(
        slug=CHANNEL_SLUG,
        channel_id=CHANNEL_ID,
        username=CHANNEL_USERNAME,
        title="TaskCoin",
        required=True,
    )
    register_telegram_channel_task_verifier(
        TelegramChannelTaskVerifier(membership_checker=members)
    )

    yield db_path

    CHANNELS.clear()
    register_telegram_channel_task_verifier()  # restore default


@pytest.fixture
def client(env):
    serve_miniapp.app.config["TESTING"] = True
    return serve_miniapp.app.test_client()


def _auth(user_id: int = USER_A) -> dict:
    return {INIT_DATA_HEADER: _make_init_data(user_id=user_id)}


def _addtask(text: str = _ADDTASK_TEXT) -> MagicMock:
    """Run the real /addtask handler as an admin; returns the update."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = _TEST_ADMIN_ID
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.text = text
    update.callback_query = None
    ctx = MagicMock()
    ctx.user_data = {}

    with patch(
        "bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID
    ):
        asyncio.run(add_task(update, ctx))
    return update


def _only_task_id() -> int:
    tasks = db.list_tasks()
    assert len(tasks) == 1
    return tasks[0]["id"]


def _status(user_id: int, task_id: int) -> str | None:
    row = db.get_user_task(user_id, task_id)
    return row["status"] if row else None


# ════════════════════════════════════════════════════════════════════
# /addtask → GET /api/tasks
# ════════════════════════════════════════════════════════════════════


class TestAddTaskListing:
    def test_addtask_created_task_exposes_join_url(self, client):
        """An /addtask-created task is listed with a registry join_url."""
        update = _addtask()
        reply = update.message.reply_text.call_args[0][0]
        assert "تم إنشاء المهمة" in reply
        task_id = _only_task_id()

        # The stored payload is exactly the verifier contract.
        stored = json.loads(db.get_task(task_id)["task_data"])
        parse_telegram_channel_task_data(db.get_task(task_id)["task_data"])
        assert stored["target"] == {"channel_slug": CHANNEL_SLUG}

        response = client.get("/api/tasks", headers=_auth())
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        task = next(t for t in data["tasks"] if t["id"] == task_id)
        assert task["type"] == TELEGRAM_CHANNEL_TASK_TYPE
        assert task["status"] == "available"
        assert task["reward"] == 500
        # join_url resolved server-side from the trusted task data and
        # the existing channel registry — never from the client.
        assert task["join_url"] == f"https://t.me/{CHANNEL_USERNAME}"


# ════════════════════════════════════════════════════════════════════
# /addtask → start → submit (member / non-member)
# ════════════════════════════════════════════════════════════════════


class TestAddTaskSubmission:
    def test_addtask_member_submission_completes(self, client, members):
        """A valid member reaches completion via the trusted task data."""
        _addtask()
        task_id = _only_task_id()

        response = client.post(
            f"/api/tasks/{task_id}/start", headers=_auth()
        )
        assert response.status_code == 200
        assert _status(USER_A, task_id) == "started"

        members.statuses[USER_A] = "member"
        response = client.post(
            f"/api/tasks/{task_id}/submit", headers=_auth()
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["status"] == db.USER_TASK_STATUS_COMPLETED
        assert _status(USER_A, task_id) == db.USER_TASK_STATUS_COMPLETED

        # The membership check used the trusted channel id from the
        # server-side task definition, for the initData-authenticated
        # user — client input never chose the target.
        assert (CHANNEL_ID, USER_A) in members.calls

    def test_addtask_non_member_submission_rejected(
        self, client, members
    ):
        """A non-member is rejected by the existing verifier."""
        _addtask()
        task_id = _only_task_id()

        response = client.post(
            f"/api/tasks/{task_id}/start", headers=_auth()
        )
        assert response.status_code == 200

        # Fake lookup default is "left" — not a valid member.
        response = client.post(
            f"/api/tasks/{task_id}/submit", headers=_auth()
        )
        assert response.status_code == 409
        data = response.get_json()
        assert data["ok"] is False
        assert data["error"] == "verification_failed"
        assert data["status"] == db.USER_TASK_STATUS_STARTED
        assert _status(USER_A, task_id) == db.USER_TASK_STATUS_STARTED
