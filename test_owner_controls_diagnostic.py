"""
Owner Controls (minimal) — ``is_owner`` on GET /api/tasks (isolated)
====================================================================

Covers the implemented minimal owner feature on its own tmp_path
SQLite database only; no real task, funding, wallet or ledger row is
ever touched, and no session material is ever logged.

Ownership source of truth (``tasks.advertiser_id`` does NOT exist —
verified in db.py): ``task_funding.advertiser_id`` compared against
the verified initData identity.  The page renders a display-only
owner indicator from that boolean; no management controls exist
because the backend exposes no owner task-management endpoint.

Run:
    python3 -m pytest test_owner_controls_diagnostic.py -v
"""

import json
from pathlib import Path

import pytest

import db
import serve_miniapp
import task_funding
import wallet
from config import CHANNELS, Channel
from channel_task_verifier import CHANNEL_TASK_TYPE, ChannelTaskVerifier
from task_creation import TaskSpec, create_task_from_spec
from task_taxonomy import VERIFICATION_MANUAL

from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

ADVERTISER = 7101   # the funding owner (task_funding.advertiser_id)
WORKER = 7102       # a different, unrelated user

USDT = wallet.USDT_SCALE
REWARD = 1 * USDT

_PROJECT_ROOT = Path(__file__).resolve().parent
_TASKS_JS_PATH = _PROJECT_ROOT / "miniapp" / "js" / "tasks.js"
_INDEX_HTML_PATH = _PROJECT_ROOT / "miniapp" / "index.html"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated database + registered identities + fake channel."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)

    db_path = str(tmp_path / "owner_controls_diag.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(ADVERTISER, "adv", "Advertiser")
    db.register_user(WORKER, "worker", "Worker")

    original_channels = CHANNELS.copy()
    CHANNELS.clear()
    CHANNELS["main"] = Channel(
        slug="main", channel_id=-100777, username="taskcoin_diag",
        title="TaskCoin", required=True,
    )
    try:
        yield db_path
    finally:
        CHANNELS.clear()
        CHANNELS.update(original_channels)


@pytest.fixture
def client(env):
    serve_miniapp.app.config["TESTING"] = True
    return serve_miniapp.app.test_client()


def _auth(user_id: int) -> dict:
    return {INIT_DATA_HEADER: _make_init_data(user_id=user_id)}


def _spec(title: str) -> TaskSpec:
    whole, _fraction = divmod(REWARD, USDT)
    return TaskSpec(
        title=title,
        description="وصف المهمة للاختبار",
        provider="instagram",
        action="follow",
        target={},
        verification=VERIFICATION_MANUAL,
        reward=whole,
        approver_id=ADVERTISER,
        reward_units=REWARD,
    )


def _create_funded_task() -> int:
    """Funded through the ONE canonical service (real funding row)."""
    wallet.credit_units(ADVERTISER, 100 * USDT)
    return create_task_from_spec(
        _spec("مهمة مموّلة"), funding_advertiser_id=ADVERTISER
    )


def _create_unfunded_task() -> int:
    """Direct creation with no advertiser / no task_funding row."""
    return db.create_task(
        title="مهمة بلا تمويل",
        description="وصف المهمة",
        task_type="deterministic",
        reward=10,
        task_data=json.dumps({"expected": "secret"}),
    )


def _task_entry(client, user_id: int, task_id: int) -> dict:
    response = client.get("/api/tasks", headers=_auth(user_id))
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    by_id = {t["id"]: t for t in data["tasks"]}
    assert task_id in by_id
    return by_id[task_id]


# ════════════════════════════════════════════════════════════════════
# The API returns an explicit is_owner boolean for every task
# ════════════════════════════════════════════════════════════════════


class TestApiReturnsIsOwner:
    def test_funding_owner_gets_is_owner_true(self, client):
        task_id = _create_funded_task()
        entry = _task_entry(client, ADVERTISER, task_id)
        assert entry["is_owner"] is True

    def test_non_owner_gets_is_owner_false(self, client):
        task_id = _create_funded_task()
        entry = _task_entry(client, WORKER, task_id)
        assert entry["is_owner"] is False

    def test_unfunded_task_grants_no_default_ownership(self, client):
        task_id = _create_unfunded_task()
        assert task_funding.get_funding(task_id) is None
        for user_id in (ADVERTISER, WORKER):
            entry = _task_entry(client, user_id, task_id)
            assert entry["is_owner"] is False

    def test_is_owner_is_an_explicit_boolean(self, client):
        task_id = _create_funded_task()
        entry = _task_entry(client, ADVERTISER, task_id)
        assert isinstance(entry["is_owner"], bool)

    def test_response_matches_the_enforced_safe_field_set(self, client):
        task_id = _create_funded_task()
        entry = _task_entry(client, ADVERTISER, task_id)
        assert set(entry.keys()) <= {
            "id", "title", "description", "type", "reward",
            "reward_units", "status", "awaiting_decision", "is_owner",
        }


# ════════════════════════════════════════════════════════════════════
# Ownership stays a narrow boolean — no funding data leaks
# ════════════════════════════════════════════════════════════════════


class TestNoFundingDataLeak:
    def test_funding_row_exists_but_no_internals_leak(self, client):
        task_id = _create_funded_task()
        record = task_funding.get_funding(task_id)
        assert record is not None
        assert record["advertiser_id"] == ADVERTISER

        response = client.get("/api/tasks", headers=_auth(WORKER))
        raw = response.get_data(as_text=True)
        # The advertiser's identity and the funding snapshot must
        # never appear in the list payload — only the boolean.
        for leak in ("advertiser_id", "commission_units",
                     "total_units", str(ADVERTISER)):
            assert leak not in raw, f"funding internal leaked: {leak}"

    def test_unfunded_task_has_no_ownership_row_at_all(self, client):
        task_id = _create_unfunded_task()
        assert task_funding.get_funding(task_id) is None


# ════════════════════════════════════════════════════════════════════
# The page renders an owner indicator and NOTHING unimplemented
# ════════════════════════════════════════════════════════════════════


class TestFrontendOwnerIndicator:
    @staticmethod
    def _tasks_js() -> str:
        with open(_TASKS_JS_PATH, encoding="utf-8") as fh:
            return fh.read()

    def test_owner_branch_reads_only_the_server_boolean(self):
        content = self._tasks_js()
        assert "if (task.is_owner === true)" in content, \
            "the owner branch must key on the server boolean only"

    def test_owner_indicator_renders(self):
        content = self._tasks_js()
        for marker in ("'task-owner-badge'", "task-owner-badge",
                       "أنت مالك هذه المهمة"):
            assert marker in content, f"missing owner marker: {marker}"

    def test_no_unimplemented_management_buttons(self):
        """No edit/pause/resume/archive controls: the backend has no
        owner task-management endpoint, so the page must not imply
        one exists.  The only mutating calls remain start/submit."""
        content = self._tasks_js()
        for banned in ("task-edit", "task-delete", "task-pause",
                       "task-resume", "task-archive", "task-manage",
                       "PUT", "PATCH", "DELETE"):
            assert banned not in content, \
                f"unimplemented management control found: {banned}"
        assert content.count("fetch(LIST_URL") == 1
        assert content.count("`/api/tasks/${task.id}/submit`") == 2
        assert content.count("`/api/tasks/${task.id}/start`") == 1

    def test_completed_branch_stays_terminal(self):
        content = self._tasks_js()
        completed = content[content.find("// completed — terminal"):]
        assert "task-owner-badge" not in completed, \
            "the owner indicator must not disturb the terminal state"

    def test_index_html_loads_tasks_js_without_cache_buster(self):
        with _INDEX_HTML_PATH.open(encoding="utf-8") as fh:
            html = fh.read()
        assert 'src="js/tasks.js"' in html
        assert "tasks.js?v=" not in html
