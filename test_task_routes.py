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
  - completion credits the USDT wallet + ledger exactly once,
    server-side, with the server-defined task reward (MT-REWARD-01);
    the route itself adds no second credit and never computes money

Run:
    python3 -m pytest test_task_routes.py -v
"""

import json
import re
import sqlite3

import pytest

import db
import serve_miniapp
from wallet import USDT_SCALE
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
            "reward_units", "status", "join_url",
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
        # MT-TASK-29: the approved cycle leaves the ACTIVE list, so a
        # completed task can never be observed in this vocabulary any
        # more — the remaining statuses are exactly available/started.
        assert task_id not in by_id
        assert by_id[plain_id] == "available"
        assert set(seen) <= {"available", "started"}


class TestCompletedHiddenFromActiveList:
    """MT-TASK-29: approved tasks leave the user's ACTIVE list."""

    def test_completed_task_excluded_from_list(self, client):
        task_id = _create_channel_task()
        client.post(f"/api/tasks/{task_id}/start", headers=_auth())
        members_status = _Members()
        members_status.statuses[USER_A] = "member"
        register_channel_task_verifier(
            ChannelTaskVerifier(membership_checker=members_status)
        )
        client.post(f"/api/tasks/{task_id}/submit", headers=_auth())
        assert _status(USER_A, task_id) == "completed"

        response = client.get("/api/tasks", headers=_auth())
        assert response.status_code == 200
        ids = [t["id"] for t in response.get_json()["tasks"]]
        assert task_id not in ids

    def test_completed_task_stays_hidden_on_reload(self, client):
        """A fresh list read (page reload) never brings it back."""
        task_id = _create_channel_task()
        client.post(f"/api/tasks/{task_id}/start", headers=_auth())
        members_status = _Members()
        members_status.statuses[USER_A] = "member"
        register_channel_task_verifier(
            ChannelTaskVerifier(membership_checker=members_status)
        )
        client.post(f"/api/tasks/{task_id}/submit", headers=_auth())

        for _ in range(2):
            response = client.get("/api/tasks", headers=_auth())
            ids = [t["id"] for t in response.get_json()["tasks"]]
            assert task_id not in ids

    def test_started_and_available_tasks_still_listed(self, client):
        """Pending (started) and untouched tasks keep their behaviour."""
        done_id = _create_channel_task()
        started_id = _create_plain_task()
        client.post(f"/api/tasks/{done_id}/start", headers=_auth())
        members_status = _Members()
        members_status.statuses[USER_A] = "member"
        register_channel_task_verifier(
            ChannelTaskVerifier(membership_checker=members_status)
        )
        client.post(f"/api/tasks/{done_id}/submit", headers=_auth())
        client.post(f"/api/tasks/{started_id}/start", headers=_auth())

        response = client.get("/api/tasks", headers=_auth())
        by_id = {t["id"]: t["status"]
                 for t in response.get_json()["tasks"]}
        assert done_id not in by_id
        assert by_id[started_id] == "started"

    def test_filtering_touches_no_records_or_money(self, client):
        """The list read mutates nothing: row intact, no ledger."""
        import sqlite3

        task_id = _create_channel_task()
        client.post(f"/api/tasks/{task_id}/start", headers=_auth())
        members_status = _Members()
        members_status.statuses[USER_A] = "member"
        register_channel_task_verifier(
            ChannelTaskVerifier(membership_checker=members_status)
        )
        client.post(f"/api/tasks/{task_id}/submit", headers=_auth())

        before = _status(USER_A, task_id)
        client.get("/api/tasks", headers=_auth())
        client.get("/api/tasks", headers=_auth())

        # user_tasks row still completed — the approval is untouched.
        assert _status(USER_A, task_id) == before == "completed"
        with sqlite3.connect(db.DB_PATH) as conn:
            ledger = conn.execute(
                "SELECT COUNT(*) FROM ledger WHERE user_id = ?",
                (USER_A,),
            ).fetchone()[0]
        # The completion credit is still there (exactly one), never
        # created or removed by the list read itself.
        assert ledger == 1


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
# Reward credit — server-side, exactly once (MT-REWARD-01)
# ════════════════════════════════════════════════════════════════════


class TestRewardCredit:
    def test_route_completion_credits_reward_exactly_once(
        self, client, members
    ):
        """A PASSED submission through the HTTP route credits the wallet
        and ledger exactly once with the server-defined reward; the
        route itself never computes money and a repeated completion
        attempt adds no second credit."""
        task_id = _create_channel_task()   # reward=50 (whole USDT)
        members.statuses[USER_A] = "member"
        client.post(f"/api/tasks/{task_id}/start", headers=_auth())
        response = client.post(
            f"/api/tasks/{task_id}/submit", headers=_auth()
        )
        assert response.status_code == 200
        assert _status(USER_A, task_id) == "completed"

        reward_units = 50 * USDT_SCALE
        with db.get_connection() as conn:
            wallets = conn.execute(
                "SELECT available_units, held_units FROM wallets "
                "WHERE user_id = ?",
                (USER_A,),
            ).fetchall()
            entries = conn.execute(
                "SELECT entry_type, amount_units, available_delta, "
                "       held_delta, currency, reference_type "
                "FROM ledger WHERE user_id = ?",
                (USER_A,),
            ).fetchall()
        # Exactly one wallet row, credited exactly once — no more.
        assert len(wallets) == 1
        assert wallets[0]["available_units"] == reward_units
        assert wallets[0]["held_units"] == 0
        # Exactly one ledger credit, equal to the wallet delta.
        assert len(entries) == 1
        entry = dict(entries[0])
        assert entry["entry_type"] == "credit"
        assert entry["amount_units"] == reward_units
        assert entry["available_delta"] == reward_units
        assert entry["held_delta"] == 0
        assert entry["currency"] == "USDT"
        assert entry["reference_type"] == "task"

        # A repeated completion attempt (409) never adds another credit.
        response = client.post(
            f"/api/tasks/{task_id}/submit", headers=_auth()
        )
        assert response.status_code == 409
        with db.get_connection() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS c FROM ledger WHERE user_id = ?",
                (USER_A,),
            ).fetchone()["c"] == 1
            row = conn.execute(
                "SELECT available_units, held_units FROM wallets "
                "WHERE user_id = ?",
                (USER_A,),
            ).fetchone()
        assert row["available_units"] == reward_units
        assert row["held_units"] == 0


# ════════════════════════════════════════════════════════════════════
# Manual task external destination (MT-TASK-17)
#
# The task's own link (``task_data["target"]["ref"]``) is exposed to
# the Mini App as ONE validated field so the user can open it before
# sending a proof.  The raw ``task_data`` — in particular the
# ``approver`` block — stays server-side, and anything that is not a
# well-formed https URL never becomes a clickable link.
# ════════════════════════════════════════════════════════════════════

_REF = "https://www.facebook.com/share/1dLE7nuoMQ/"
_APPROVER_ID = 999            # a distinct id, never the acting user


def _create_manual_task(task_data=None, active: bool = True) -> int:
    return db.create_task(
        title="متابعه حساب فيسبوك",
        description="متابعه الحساب وارسال لقطه شاشه",
        task_type="manual",
        reward=50,
        active=active,
        task_data=json.dumps(
            task_data
            if task_data is not None
            else {
                "provider": "other",
                "action": "follow",
                "approver": {"telegram_user_id": _APPROVER_ID},
                "target": {"ref": _REF},
            }
        ),
    )


def _set_task_data(task_id: int, task_data) -> None:
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute(
            "UPDATE tasks SET task_data = ? WHERE id = ?",
            (json.dumps(task_data), task_id),
        )


def _submissions(user_id, task_id):
    from task_submission_store import TaskSubmissionStore
    return TaskSubmissionStore().list_user_task_submissions(
        user_id, task_id
    )


def _manual_task_of(client, task_id):
    body = client.get("/api/tasks", headers=_auth()).get_json()
    for entry in body["tasks"]:
        if entry["id"] == task_id:
            return entry
    return None


class TestManualTaskUrl:
    """The validated `task_url` field for manual tasks."""

    def test_valid_https_ref_becomes_task_url(self, client):
        task_id = _create_manual_task()

        entry = _manual_task_of(client, task_id)

        assert entry["task_url"] == _REF
        # The field is present alongside the existing ones.
        assert "task_url" in entry

    def test_task_data_and_approver_are_never_exposed(self, client):
        task_id = _create_manual_task()

        body = client.get("/api/tasks", headers=_auth())
        payload = body.get_data(as_text=True)

        assert body.status_code == 200
        assert "task_data" not in payload
        assert "approver" not in payload
        assert "telegram_user_id" not in payload
        # The ONE advertised field is the destination itself.
        assert _REF in payload

    def test_non_manual_task_never_gets_task_url(self, client):
        plain = _create_plain_task()
        channel = _create_channel_task()

        for task_id in (plain, channel):
            entry = _manual_task_of(client, task_id)
            assert "task_url" not in entry

    def test_channel_task_keeps_its_join_url(self, client):
        channel = _create_channel_task()

        entry = _manual_task_of(client, channel)

        assert entry["join_url"] == "https://t.me/" + CHANNEL_USERNAME
        assert "task_url" not in entry

    @pytest.mark.parametrize(
        "ref",
        [
            "http://example.com/x",        # insecure scheme
            "javascript:alert(1)",         # script
            "data:text/html,hi",           # data URI
            "",                            # empty
            "   ",                         # whitespace only
            "https://",                    # no host
            "https://user:pass@host.io/",  # embedded credentials
            "https://bob@example.com/",    # userinfo only
            12345,                         # non-string
            None,                          # missing
            "example.com",                 # scheme-less
        ],
    )
    def test_unusable_refs_yield_no_task_url(self, client, ref):
        task_id = _create_manual_task()
        _set_task_data(task_id, {"target": {"ref": ref}})

        entry = _manual_task_of(client, task_id)

        assert "task_url" not in entry

    def test_target_missing_entirely(self, client):
        task_id = _create_manual_task()
        _set_task_data(task_id, {"provider": "other"})

        entry = _manual_task_of(client, task_id)

        assert "task_url" not in entry

    def test_target_not_an_object(self, client):
        task_id = _create_manual_task()
        _set_task_data(task_id, {"target": _REF})

        entry = _manual_task_of(client, task_id)

        assert "task_url" not in entry

    def test_malformed_task_data_json(self, client):
        task_id = _create_manual_task()
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                "UPDATE tasks SET task_data = ? WHERE id = ?",
                ("{not json", task_id),
            )

        entry = _manual_task_of(client, task_id)

        assert "task_url" not in entry


class TestStartDoesNotSubmitOrReward:
    """Starting a task changes status only — never money, never proof."""

    def test_start_records_no_submission_and_no_ledger(self, client):
        task_id = _create_manual_task()
        before_wallet = db.get_user(USER_A)

        response = client.post(
            f"/api/tasks/{task_id}/start", headers=_auth()
        )

        assert response.status_code == 200
        # 1. the task is merely started
        assert _status(USER_A, task_id) == "started"
        # 2. NO proof submission was created
        submissions = _submissions(USER_A, task_id)
        assert submissions == []
        # 3. no ledger entry / balance movement
        with sqlite3.connect(db.DB_PATH) as conn:
            ledger = conn.execute(
                "SELECT COUNT(*) FROM ledger WHERE user_id = ?", (USER_A,)
            ).fetchone()[0]
            wallets = conn.execute(
                "SELECT COUNT(*) FROM wallets WHERE user_id = ?", (USER_A,)
            ).fetchone()[0]
        assert ledger == 0
        assert wallets == 0
        assert before_wallet is not None

    def test_failed_start_does_not_render_a_started_task(self, client):
        task_id = _create_manual_task()
        # Exhaust the one-time repeat policy so the next start fails.
        client.post(f"/api/tasks/{task_id}/start", headers=_auth())
        db.update_user_task_status(
            USER_A, task_id, "completed", _allow_completion=True
        )

        response = client.post(
            f"/api/tasks/{task_id}/start", headers=_auth()
        )

        assert response.status_code != 200
        assert _status(USER_A, task_id) == "completed"


class TestProofStaysBoundToItsTask:
    """The proof is submitted for the SAME task, by id."""

    def test_proof_targets_the_started_task_only(self, client):
        task_id = _create_manual_task()
        client.post(f"/api/tasks/{task_id}/start", headers=_auth())

        response = client.post(
            f"/api/tasks/{task_id}/submit",
            headers={**_auth(), "Content-Type": "application/json"},
            data=json.dumps({"proof_ref": "https://proof.example/x"}),
        )

        body = response.get_json()
        assert response.status_code == 200
        submissions = _submissions(USER_A, task_id)
        assert len(submissions) == 1
        assert submissions[0].proof_ref == "https://proof.example/x"
        assert body["ok"] is True


class TestTaskUrlUiContract:
    """Source-level guards for the Mini App task-link behaviour.

    These read ``tasks.js`` the same way the Wallet page tests read
    ``wallet-data.js``: the file is the contract.  They assert the
    control is a link (never a submit) and that starting a task never
    navigates or submits automatically.
    """

    def _tasks_js(self) -> str:
        with open("miniapp/js/tasks.js", "r", encoding="utf-8") as fh:
            return fh.read()

    def _start_body(self) -> str:
        source = self._tasks_js()
        start = source.index("async function startTask")
        # the next top-level function ends the body
        end = source.index("async function submitTask", start)
        return source[start:end]

    def test_open_link_is_a_secure_anchor_not_a_submit(self):
        source = self._tasks_js()

        # A separate, server-provided link control.
        assert "task-open-url" in source
        assert "فتح رابط المهمة" in source
        # The secure link pattern the channel join link already uses.
        assert "rel = 'noopener'" in source
        assert "target = '_blank'" in source
        # Only https can ever become the href.
        assert "_isSafeHttpsUrl(task.task_url)" in source

    def test_start_never_submits_proof_or_opens_a_link(self):
        body = self._start_body()

        # Starting a task must NOT call either submit path …
        assert "submitProof" not in body
        assert "submitTask" not in body
        # … nor navigate anywhere …
        assert "window.location" not in body
        assert ".click()" not in body
        # … it only flips the status and shows a notice.
        assert "status: 'started'" in body
        assert "_showNotice" in body

    def test_the_open_control_is_not_wired_to_the_proof_handler(self):
        source = self._tasks_js()

        # The open control is a plain anchor created with no click
        # listener at all — it navigates by its href, so it can never
        # reach a submit path.
        anchor_start = source.index("const openLink")
        anchor_end = source.index("actions.appendChild(openLink)", anchor_start)
        anchor_block = source[anchor_start:anchor_end]
        assert "addEventListener" not in anchor_block
        assert 'setAttribute(\'data-testid\', \'task-open-url\')' in anchor_block
        assert "openLink.href = task.task_url" in anchor_block

        # the proof button is the ONLY control wired to submitProof,
        # and it always receives ITS OWN task object — never a stale or
        # foreign id.  (The control now also carries the file input.)
        assert re.search(
            r"submitProof\(\s*task, proofInput, proofBtn, fileInput, uploadStatus\)",
            source,
        ), "the proof button must pass its own task and its file input"

    def test_proof_button_binds_to_its_own_task_id(self):
        source = self._tasks_js()

        assert re.search(
            r"submitProof\(\s*task, proofInput, proofBtn",
            source,
        ), "the proof button must pass its own task object"
