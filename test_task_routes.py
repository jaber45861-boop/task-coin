"""
Focused tests — production Mini App Task API (MT-TASK-03)
=========================================================

Covers the required checklist:

  Authentication
  - unauthenticated request rejected (list / start / submit)
  - invalid initData rejected
  - the authenticated Telegram initData user is the identity
  - a client-supplied user_id cannot impersonate another user

  Catalog
  - active tasks returned from the existing task catalog
  - inactive tasks excluded
  - safe fields only (no task_data / active / created_at / internals)
  - per-user status: available / started / completed

  Start
  - available task starts (available → started)
  - already-started task keeps existing domain behaviour
  - completed task cannot be restarted
  - route delegates to TaskLifecycle
  - route does not directly mutate task state (behavioural + source)

  Submit
  - channel_subscription reaches the existing verifier pipeline
  - valid membership completes the task
  - missing membership does not complete the task
  - client cannot override the channel / task data
  - forbidden client fields rejected (reward/status)
  - completed task cannot be completed again
  - verification ERROR surfaces safely

  API
  - invalid task ID → 404 task_not_found
  - inactive task → 404 task_inactive
  - malformed request body → 400
  - authentication failure → 401
  - no reward/wallet/ledger write on completion (PART 12)

Run:
    python3 -m pytest test_task_routes.py -v
"""

import json

import pytest

import db
import serve_miniapp
from config import CHANNELS, Channel
from channel_task_verifier import (
    CHANNEL_TASK_TYPE,
    ChannelTaskVerifier,
    register_channel_task_verifier,
)
from task_start import StartResult

# Reuse the existing, independently implemented Telegram initData helper.
from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

USER_A = 1001
USER_B = 2002

CHANNEL_SLUG = "main"
CHANNEL_ID = -100111
CHANNEL_USERNAME = "taskcoin_ch"


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
    """Environment + isolated database + configured channel + fake
    verifier (no real Telegram API call is ever made)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)

    db_path = str(tmp_path / "task_routes_test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER_A, "alice", "Alice")
    db.register_user(USER_B, "bob", "Bob")

    CHANNELS.clear()
    CHANNELS[CHANNEL_SLUG] = Channel(
        slug=CHANNEL_SLUG,
        channel_id=CHANNEL_ID,
        username=CHANNEL_USERNAME,
        title="TaskCoin",
        required=True,
    )
    register_channel_task_verifier(
        ChannelTaskVerifier(membership_checker=members)
    )

    yield db_path

    CHANNELS.clear()
    register_channel_task_verifier()  # restore the default registration


@pytest.fixture
def client(env):
    serve_miniapp.app.config["TESTING"] = True
    return serve_miniapp.app.test_client()


def _auth(user_id: int = USER_A) -> dict:
    return {INIT_DATA_HEADER: _make_init_data(user_id=user_id)}


def _create_channel_task(active: bool = True) -> int:
    return db.create_task(
        title="اشترك في القناة الرسمية",
        description="انضم إلى قناة تيليجرام وأكّد الاشتراك",
        task_type=CHANNEL_TASK_TYPE,
        reward=50,
        active=active,
        task_data=json.dumps({"channel_slug": CHANNEL_SLUG}),
    )


def _create_plain_task(active: bool = True) -> int:
    return db.create_task(
        title="مهمة بسيطة",
        description="وصف المهمة",
        task_type="deterministic",
        reward=10,
        active=active,
        task_data=json.dumps({"expected": "secret"}),
    )


def _status(user_id: int, task_id: int) -> str | None:
    row = db.get_user_task(user_id, task_id)
    return row["status"] if row else None


# ════════════════════════════════════════════════════════════════════
# Authentication
# ════════════════════════════════════════════════════════════════════


class TestAuthentication:
    def test_list_unauthenticated_rejected(self, client):
        response = client.get("/api/tasks")
        assert response.status_code == 401
        data = response.get_json()
        assert data["ok"] is False
        assert data["error"] == "unauthenticated"
        assert data["message"]  # Arabic, user-facing

    def test_start_unauthenticated_rejected(self, client):
        task_id = _create_channel_task()
        response = client.post(f"/api/tasks/{task_id}/start")
        assert response.status_code == 401
        assert response.get_json()["error"] == "unauthenticated"
        assert _status(USER_A, task_id) is None  # nothing happened

    def test_submit_unauthenticated_rejected(self, client, members):
        task_id = _create_channel_task()
        members.statuses[USER_A] = "member"
        response = client.post(f"/api/tasks/{task_id}/submit")
        assert response.status_code == 401
        assert _status(USER_A, task_id) is None

    def test_invalid_init_data_rejected(self, client):
        response = client.get(
            "/api/tasks", headers={INIT_DATA_HEADER: "not-valid"}
        )
        assert response.status_code == 401

    def test_identity_comes_from_init_data(self, client):
        """The initData user acts; a different user's view stays intact."""
        task_id = _create_channel_task()
        # USER_A (from initData) starts the task.
        response = client.post(
            f"/api/tasks/{task_id}/start", headers=_auth(USER_A)
        )
        assert response.status_code == 200
        assert _status(USER_A, task_id) == "started"

        # USER_B sees the same task as available — statuses are per user.
        response = client.get("/api/tasks", headers=_auth(USER_B))
        tasks = {t["id"]: t for t in response.get_json()["tasks"]}
        assert tasks[task_id]["status"] == "available"

    def test_client_user_id_cannot_impersonate(self, client):
        """A body/query user_id is ignored — initData decides identity."""
        task_id = _create_channel_task()
        response = client.post(
            f"/api/tasks/{task_id}/start",
            headers=_auth(USER_A),
            json={"user_id": USER_B, "user": USER_B},
        )
        assert response.status_code == 200
        assert _status(USER_A, task_id) == "started"   # the initData user
        assert _status(USER_B, task_id) is None        # never touched


# ════════════════════════════════════════════════════════════════════
# Catalog
# ════════════════════════════════════════════════════════════════════


class TestCatalog:
    def test_active_tasks_returned(self, client):
        task_id = _create_channel_task()
        response = client.get("/api/tasks", headers=_auth())
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert [t["id"] for t in data["tasks"]] == [task_id]
        task = data["tasks"][0]
        assert task["title"] == "اشترك في القناة الرسمية"
        assert task["description"]
        assert task["type"] == CHANNEL_TASK_TYPE
        assert task["reward"] == 50
        assert task["status"] == "available"

    def test_inactive_tasks_excluded(self, client):
        active_id = _create_channel_task(active=True)
        _create_channel_task(active=False)
        response = client.get("/api/tasks", headers=_auth())
        assert [t["id"] for t in response.get_json()["tasks"]] == [active_id]

    def test_safe_fields_only(self, client):
        _create_channel_task()
        response = client.get("/api/tasks", headers=_auth())
        task = response.get_json()["tasks"][0]
        assert set(task.keys()) <= {
            "id", "title", "description", "type", "reward",
            "status", "join_url",
        }
        raw = response.get_data(as_text=True)
        assert "task_data" not in raw
        assert "expected" not in raw
        assert "created_at" not in raw
        assert "active" not in raw
        assert CHANNEL_SLUG not in raw          # no slug leakage
        assert str(CHANNEL_ID) not in raw       # no numeric channel id

    def test_status_vocabulary_is_exact(self, client):
        task_id = _create_channel_task()
        plain_id = _create_plain_task()
        seen = []

        response = client.get("/api/tasks", headers=_auth())
        seen += [t["status"] for t in response.get_json()["tasks"]]

        client.post(f"/api/tasks/{task_id}/start", headers=_auth())
        members_status = _Members()
        members_status.statuses[USER_A] = "member"
        register_channel_task_verifier(
            ChannelTaskVerifier(membership_checker=members_status)
        )
        client.post(f"/api/tasks/{task_id}/submit", headers=_auth())
        response = client.get("/api/tasks", headers=_auth())
        seen += [t["status"] for t in response.get_json()["tasks"]]

        assert set(seen) <= {"available", "started", "completed"}
        by_id = {t["id"]: t["status"]
                 for t in response.get_json()["tasks"]}
        assert by_id[task_id] == "completed"
        assert by_id[plain_id] == "available"


# ════════════════════════════════════════════════════════════════════
# Channel join destination (PART 7)
# ════════════════════════════════════════════════════════════════════


class TestJoinDestination:
    def test_channel_task_exposes_public_join_url(self, client):
        task_id = _create_channel_task()
        response = client.get("/api/tasks", headers=_auth())
        task = response.get_json()["tasks"][0]
        assert task["join_url"] == f"https://t.me/{CHANNEL_USERNAME}"
        raw = response.get_data(as_text=True)
        assert str(CHANNEL_ID) not in raw   # never the numeric id

    def test_non_channel_task_has_no_join_url(self, client):
        _create_plain_task()
        response = client.get("/api/tasks", headers=_auth())
        task = response.get_json()["tasks"][0]
        assert "join_url" not in task

    def test_unconfigured_channel_has_no_join_url(self, client, env):
        task_id = _create_channel_task()
        CHANNELS.clear()  # channel no longer configured
        response = client.get("/api/tasks", headers=_auth())
        task = response.get_json()["tasks"][0]
        assert "join_url" not in task


# ════════════════════════════════════════════════════════════════════
# Start
# ════════════════════════════════════════════════════════════════════


class TestStart:
    def test_available_task_starts(self, client):
        task_id = _create_channel_task()
        response = client.post(
            f"/api/tasks/{task_id}/start", headers=_auth()
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["status"] == "started"
        row = db.get_user_task(USER_A, task_id)
        assert row["status"] == "started"
        assert row["started_at"] is not None

    def test_already_started_rejected(self, client):
        task_id = _create_channel_task()
        client.post(f"/api/tasks/{task_id}/start", headers=_auth())
        response = client.post(
            f"/api/tasks/{task_id}/start", headers=_auth()
        )
        assert response.status_code == 409
        data = response.get_json()
        assert data["error"] == "task_already_started"
        assert data["message"]
        assert _status(USER_A, task_id) == "started"

    def test_completed_task_cannot_restart(self, client, members):
        task_id = _create_channel_task()
        members.statuses[USER_A] = "member"
        client.post(f"/api/tasks/{task_id}/start", headers=_auth())
        client.post(f"/api/tasks/{task_id}/submit", headers=_auth())
        assert _status(USER_A, task_id) == "completed"

        response = client.post(
            f"/api/tasks/{task_id}/start", headers=_auth()
        )
        assert response.status_code == 409
        assert response.get_json()["error"] == "task_already_completed"
        assert _status(USER_A, task_id) == "completed"

    def test_route_delegates_to_task_lifecycle(self, client, monkeypatch):
        """The HTTP handler only calls TaskLifecycle — never the gate."""
        import task_routes

        calls: list[tuple[int, int]] = []

        class _FakeLifecycle:
            def start_task(self, user_id, task_id):
                calls.append((user_id, task_id))
                return StartResult(
                    success=True, message="ok", status="started"
                )

        monkeypatch.setattr(task_routes, "TaskLifecycle", _FakeLifecycle)
        task_id = _create_channel_task()
        response = client.post(
            f"/api/tasks/{task_id}/start", headers=_auth()
        )
        assert response.status_code == 200
        assert calls == [(USER_A, task_id)]
        # The route itself mutated nothing.
        assert db.get_user_task(USER_A, task_id) is None

    def test_route_source_has_no_direct_mutation(self):
        """Source-level guard: no user_tasks writes, no CompletionGate."""
        with open("task_routes.py", "r", encoding="utf-8") as fh:
            source = fh.read()
        assert "UPDATE user_tasks" not in source
        assert "INSERT INTO user_tasks" not in source
        assert "update_user_task_status" not in source
        assert "CompletionGate(" not in source
        assert "TaskLifecycle().start_task" in source
        assert "TaskLifecycle().submit_task" in source

    def test_invalid_task_id_rejected(self, client):
        response = client.post(
            "/api/tasks/999999/start", headers=_auth()
        )
        assert response.status_code == 404
        assert response.get_json()["error"] == "task_not_found"

    def test_inactive_task_rejected(self, client):
        task_id = _create_channel_task(active=False)
        response = client.post(
            f"/api/tasks/{task_id}/start", headers=_auth()
        )
        assert response.status_code == 404
        assert response.get_json()["error"] == "task_inactive"
        assert _status(USER_A, task_id) is None

    def test_malformed_body_rejected(self, client):
        task_id = _create_channel_task()
        response = client.post(
            f"/api/tasks/{task_id}/start",
            headers=_auth(),
            json=[1, 2, 3],
        )
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_request"
        assert _status(USER_A, task_id) is None


# ════════════════════════════════════════════════════════════════════
# Submit (channel_subscription through the existing pipeline)
# ════════════════════════════════════════════════════════════════════


class TestSubmit:
    def test_membership_completes_task(self, client, members):
        task_id = _create_channel_task()
        members.statuses[USER_A] = "member"

        client.post(f"/api/tasks/{task_id}/start", headers=_auth())
        response = client.post(
            f"/api/tasks/{task_id}/submit", headers=_auth()
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["status"] == "completed"
        assert _status(USER_A, task_id) == "completed"

        # The existing verifier pipeline performed the lookup against
        # the configured channel for the authenticated user.
        assert members.calls == [(CHANNEL_ID, USER_A)]

    def test_admin_membership_also_counts(self, client, members):
        task_id = _create_channel_task()
        members.statuses[USER_A] = "administrator"
        client.post(f"/api/tasks/{task_id}/start", headers=_auth())
        response = client.post(
            f"/api/tasks/{task_id}/submit", headers=_auth()
        )
        assert response.status_code == 200
        assert _status(USER_A, task_id) == "completed"

    def test_missing_membership_does_not_complete(self, client, members):
        task_id = _create_channel_task()
        members.statuses[USER_A] = "left"  # default

        client.post(f"/api/tasks/{task_id}/start", headers=_auth())
        response = client.post(
            f"/api/tasks/{task_id}/submit", headers=_auth()
        )
        assert response.status_code == 409
        data = response.get_json()
        assert data["error"] == "verification_failed"
        assert data["status"] == "started"
        assert data["message"]
        assert _status(USER_A, task_id) == "started"

    def test_client_cannot_override_channel(self, client, members):
        """A client channel_slug/id never chooses the verified channel."""
        task_id = _create_channel_task()
        members.statuses[USER_A] = "member"

        client.post(f"/api/tasks/{task_id}/start", headers=_auth())
        response = client.post(
            f"/api/tasks/{task_id}/submit",
            headers=_auth(),
            json={"channel_slug": "attacker", "channel_id": -999},
        )
        assert response.status_code == 200
        # The verifier was called with the SERVER-configured channel.
        assert members.calls == [(CHANNEL_ID, USER_A)]
        assert _status(USER_A, task_id) == "completed"

    def test_forbidden_client_fields_rejected(self, client, members):
        task_id = _create_channel_task()
        members.statuses[USER_A] = "member"
        client.post(f"/api/tasks/{task_id}/start", headers=_auth())

        response = client.post(
            f"/api/tasks/{task_id}/submit",
            headers=_auth(),
            json={"reward": 999999, "status": "completed"},
        )
        assert response.status_code == 400
        data = response.get_json()
        assert data["error"] == "invalid_submission"
        assert "reward" not in response.get_data(as_text=True)
        # The forged completion did not happen.
        assert _status(USER_A, task_id) == "started"

    def test_completed_task_cannot_complete_again(self, client, members):
        task_id = _create_channel_task()
        members.statuses[USER_A] = "member"
        client.post(f"/api/tasks/{task_id}/start", headers=_auth())
        client.post(f"/api/tasks/{task_id}/submit", headers=_auth())
        assert _status(USER_A, task_id) == "completed"

        response = client.post(
            f"/api/tasks/{task_id}/submit", headers=_auth()
        )
        assert response.status_code == 409
        data = response.get_json()
        assert data["error"] == "task_already_completed"
        assert data["status"] == "completed"
        # Still exactly one completion — the terminal state is stable.
        assert _status(USER_A, task_id) == "completed"

    def test_submit_without_start_rejected(self, client, members):
        task_id = _create_channel_task()
        members.statuses[USER_A] = "member"
        response = client.post(
            f"/api/tasks/{task_id}/submit", headers=_auth()
        )
        assert response.status_code == 409
        assert response.get_json()["error"] == "task_not_available"
        assert _status(USER_A, task_id) is None

    def test_submit_available_row_requires_start(self, client, members):
        task_id = _create_channel_task()
        db.create_user_task(USER_A, task_id)  # explicit 'available' row
        members.statuses[USER_A] = "member"
        response = client.post(
            f"/api/tasks/{task_id}/submit", headers=_auth()
        )
        assert response.status_code == 409
        assert response.get_json()["error"] == "task_not_started"
        assert _status(USER_A, task_id) == "available"

    def test_verification_error_is_safe(self, client, members):
        task_id = _create_channel_task()
        members.error = RuntimeError("secret-internal-boom")
        client.post(f"/api/tasks/{task_id}/start", headers=_auth())

        response = client.post(
            f"/api/tasks/{task_id}/submit", headers=_auth()
        )
        assert response.status_code == 502
        data = response.get_json()
        assert data["error"] == "verification_error"
        assert data["message"]
        raw = response.get_data(as_text=True)
        assert "secret-internal-boom" not in raw   # no internal leakage
        assert "Traceback" not in raw
        assert _status(USER_A, task_id) == "started"

    def test_malformed_submit_body_rejected(self, client):
        task_id = _create_channel_task()
        response = client.post(
            f"/api/tasks/{task_id}/submit",
            headers=_auth(),
            json=[1, 2],
        )
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_request"

    def test_submit_invalid_task_id(self, client):
        response = client.post(
            "/api/tasks/999999/submit", headers=_auth()
        )
        assert response.status_code == 404
        assert response.get_json()["error"] == "task_not_found"


# ════════════════════════════════════════════════════════════════════
# No reward credit (PART 12)
# ════════════════════════════════════════════════════════════════════


class TestNoRewardCredit:
    def test_completion_touches_no_wallet_or_ledger(self, client, members):
        task_id = _create_channel_task()
        members.statuses[USER_A] = "member"
        client.post(f"/api/tasks/{task_id}/start", headers=_auth())
        response = client.post(
            f"/api/tasks/{task_id}/submit", headers=_auth()
        )
        assert response.status_code == 200
        assert _status(USER_A, task_id) == "completed"

        with db.get_connection() as conn:
            wallets = conn.execute(
                "SELECT COUNT(*) AS c FROM wallets WHERE user_id = ?",
                (USER_A,),
            ).fetchone()["c"]
            ledger_rows = conn.execute(
                "SELECT COUNT(*) AS c FROM ledger WHERE user_id = ?",
                (USER_A,),
            ).fetchone()["c"]
        assert wallets == 0
        assert ledger_rows == 0
