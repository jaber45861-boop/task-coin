"""
Mini App Task-Request API («إضافة مهمة ➕»)
===========================================

User-side HTTP contract for the user-proposed task workflow:

  Authentication
  - unauthenticated / invalid initData rejected on every endpoint
  - identity comes only from verified initData; a client-supplied
    user_id can never impersonate (it is rejected as an unknown
    payload field)

  Create
  - valid proposal → pending request, safe response fields only
  - every invalid field rejected server-side (title, description,
    provider, action, reward, unknown keys)
  - status/user_id/history client keys are rejected outright

  Catalog isolation
  - a pending request NEVER appears in GET /api/tasks and creates
    no `tasks` row

  Read
  - the caller sees exactly their own requests (list + detail)
  - another user's request is indistinguishable from a missing one

  Edit + resubmit (PATCH)
  - only the owner, only when the admin returned it for changes
  - invalid data rejected with no state change
  - client cannot set status/approval through the body

  No privilege escalation
  - there is NO user-side approve/reject endpoint at all

Run:
    python3 -m pytest test_task_requests_api.py -v
"""

import pytest

import db
import serve_miniapp
import task_request_store

from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

USER_A = 3101
USER_B = 3102

VALID_PAYLOAD = {
    "title": "متابعة حسابي على Instagram",
    "description": "تابع الحساب ثم أرسل إثبات المتابعة",
    "provider": "instagram",
    "action": "follow",
    "target_ref": "https://instagram.com/example",
    "reward": "0.5",
}


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Environment + isolated database + two registered users."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    db_path = str(tmp_path / "task_requests_test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER_A, "alice", "Alice")
    db.register_user(USER_B, "bob", "Bob")
    yield db_path


@pytest.fixture
def client(env):
    serve_miniapp.app.config["TESTING"] = True
    return serve_miniapp.app.test_client()


def _auth(user_id: int = USER_A) -> dict:
    return {INIT_DATA_HEADER: _make_init_data(user_id=user_id)}


def _payload(**overrides) -> dict:
    payload = dict(VALID_PAYLOAD)
    payload.update(overrides)
    return payload


def _create(client, user_id: int = USER_A, **overrides) -> dict:
    response = client.post(
        "/api/task-requests",
        headers=_auth(user_id),
        json=_payload(**overrides),
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["request"]


# ════════════════════════════════════════════════════════════════════
# Authentication & identity
# ════════════════════════════════════════════════════════════════════


class TestAuthentication:
    def test_create_unauthenticated_rejected(self, client):
        response = client.post("/api/task-requests", json=VALID_PAYLOAD)
        assert response.status_code == 401
        assert response.get_json()["error"] == "unauthenticated"

    def test_list_unauthenticated_rejected(self, client):
        assert client.get("/api/task-requests").status_code == 401

    def test_detail_unauthenticated_rejected(self, client):
        assert client.get("/api/task-requests/1").status_code == 401

    def test_patch_unauthenticated_rejected(self, client):
        response = client.patch(
            "/api/task-requests/1", json=VALID_PAYLOAD
        )
        assert response.status_code == 401

    def test_invalid_init_data_rejected(self, client):
        response = client.post(
            "/api/task-requests",
            headers={INIT_DATA_HEADER: "not-valid"},
            json=VALID_PAYLOAD,
        )
        assert response.status_code == 401

    def test_identity_comes_from_init_data(self, client):
        """The stored owner is the verified initData user, never a
        body field."""
        response = client.post(
            "/api/task-requests",
            headers=_auth(USER_A),
            json=_payload(user_id=USER_B),
        )
        # "user_id" is not a proposal field → rejected outright.
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_payload"

        created = _create(client, USER_A)
        stored = task_request_store.get_request(created["request_id"])
        assert stored.user_id == USER_A


# ════════════════════════════════════════════════════════════════════
# Create — validation (server-side, authoritative)
# ════════════════════════════════════════════════════════════════════


class TestCreateValidation:
    def test_valid_proposal_becomes_pending(self, client):
        created = _create(client)
        assert created["status"] == "pending"
        assert created["title"] == VALID_PAYLOAD["title"]
        assert created["reward_units"] == 50_000_000  # 0.5 USDT
        assert created["task_id"] is None
        assert created["reason"] is None

    def test_missing_title_rejected(self, client):
        response = client.post(
            "/api/task-requests",
            headers=_auth(),
            json=_payload(title=""),
        )
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_payload"

    def test_missing_description_rejected(self, client):
        response = client.post(
            "/api/task-requests",
            headers=_auth(),
            json=_payload(description="   "),
        )
        assert response.status_code == 400

    def test_title_with_newline_rejected(self, client):
        response = client.post(
            "/api/task-requests",
            headers=_auth(),
            json=_payload(title="سطر أول\nسطر ثانٍ"),
        )
        assert response.status_code == 400

    def test_oversized_description_rejected(self, client):
        response = client.post(
            "/api/task-requests",
            headers=_auth(),
            json=_payload(description="د" * 1001),
        )
        assert response.status_code == 400

    def test_unknown_provider_rejected(self, client):
        response = client.post(
            "/api/task-requests",
            headers=_auth(),
            json=_payload(provider="nope"),
        )
        assert response.status_code == 400

    def test_action_not_allowed_for_provider_rejected(self, client):
        # "join_channel" is not an instagram action.
        response = client.post(
            "/api/task-requests",
            headers=_auth(),
            json=_payload(action="join_channel"),
        )
        assert response.status_code == 400

    def test_non_numeric_reward_rejected(self, client):
        response = client.post(
            "/api/task-requests",
            headers=_auth(),
            json=_payload(reward="abc"),
        )
        assert response.status_code == 400

    def test_negative_reward_rejected(self, client):
        response = client.post(
            "/api/task-requests",
            headers=_auth(),
            json=_payload(reward="-1"),
        )
        assert response.status_code == 400

    def test_overprecise_reward_rejected(self, client):
        response = client.post(
            "/api/task-requests",
            headers=_auth(),
            json=_payload(reward="0.000000001"),
        )
        assert response.status_code == 400

    def test_unknown_field_rejected(self, client):
        response = client.post(
            "/api/task-requests",
            headers=_auth(),
            json=_payload(bonus="extra"),
        )
        assert response.status_code == 400

    def test_client_cannot_set_status(self, client):
        """A body `status` is an unknown field — never a state lever."""
        response = client.post(
            "/api/task-requests",
            headers=_auth(),
            json=_payload(status="approved"),
        )
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_payload"

    def test_non_object_body_rejected(self, client):
        response = client.post(
            "/api/task-requests", headers=_auth(), json=["x"]
        )
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_request"

    def test_response_exposes_safe_fields_only(self, client):
        created = _create(client)
        assert set(created) == {
            "request_id", "status", "title", "description", "provider",
            "action", "target_ref", "reward", "reward_units", "reason",
            "task_id", "created_at", "updated_at",
        }


# ════════════════════════════════════════════════════════════════════
# Catalog isolation — pending never becomes a public task
# ════════════════════════════════════════════════════════════════════


class TestCatalogIsolation:
    def test_pending_request_not_in_public_catalog(self, client):
        _create(client)
        response = client.get("/api/tasks", headers=_auth())
        assert response.status_code == 200
        assert response.get_json()["tasks"] == []

    def test_pending_request_creates_no_task_row(self, client):
        _create(client)
        with db.get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks"
            ).fetchone()["n"]
        assert count == 0


# ════════════════════════════════════════════════════════════════════
# Read — own requests only
# ════════════════════════════════════════════════════════════════════


class TestReadOwnership:
    def test_list_returns_own_requests(self, client):
        _create(client, USER_A, title="طلب أليس")
        _create(client, USER_B, title="طلب بوب")

        response = client.get("/api/task-requests", headers=_auth(USER_A))
        items = response.get_json()["requests"]
        assert len(items) == 1
        assert items[0]["title"] == "طلب أليس"

    def test_empty_list_is_ok_with_empty_array(self, client):
        response = client.get("/api/task-requests", headers=_auth())
        assert response.status_code == 200
        assert response.get_json() == {"ok": True, "requests": []}

    def test_detail_own_request_ok(self, client):
        created = _create(client)
        response = client.get(
            f"/api/task-requests/{created['request_id']}",
            headers=_auth(),
        )
        assert response.status_code == 200
        assert response.get_json()["request"]["request_id"] == \
            created["request_id"]

    def test_detail_foreign_request_is_404(self, client):
        created = _create(client, USER_A)
        response = client.get(
            f"/api/task-requests/{created['request_id']}",
            headers=_auth(USER_B),
        )
        assert response.status_code == 404
        assert response.get_json()["error"] == "request_not_found"

    def test_detail_missing_request_is_404(self, client):
        assert client.get(
            "/api/task-requests/999999", headers=_auth()
        ).status_code == 404


# ════════════════════════════════════════════════════════════════════
# Edit + resubmit (PATCH)
# ════════════════════════════════════════════════════════════════════


class TestResubmit:
    def test_pending_request_cannot_be_edited(self, client):
        created = _create(client)
        response = client.patch(
            f"/api/task-requests/{created['request_id']}",
            headers=_auth(),
            json=_payload(title="تعديل مبكر"),
        )
        assert response.status_code == 409
        assert response.get_json()["error"] == "invalid_status"

    def test_returned_request_resubmits_to_pending(self, client):
        created = _create(client)
        task_request_store.admin_return_request(
            created["request_id"], 999, "عدّل الوصف"
        )
        response = client.patch(
            f"/api/task-requests/{created['request_id']}",
            headers=_auth(),
            json=_payload(title="عنوان معدل", reward="1.25"),
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["request"]["status"] == "pending"
        assert body["request"]["title"] == "عنوان معدل"
        assert body["request"]["reward_units"] == 125_000_000
        assert body["request"]["reason"] is None
        assert body["message"] == "تم إرسال المهمة للمراجعة من الإدارة."

    def test_invalid_resubmit_rejected_without_state_change(self, client):
        created = _create(client)
        task_request_store.admin_return_request(
            created["request_id"], 999, "ملاحظة"
        )
        response = client.patch(
            f"/api/task-requests/{created['request_id']}",
            headers=_auth(),
            json=_payload(title=""),
        )
        assert response.status_code == 400
        stored = task_request_store.get_request(created["request_id"])
        assert stored.status == "changes_requested"
        assert stored.payload["title"] == VALID_PAYLOAD["title"]

    def test_cannot_resubmit_foreign_request(self, client):
        created = _create(client, USER_A)
        task_request_store.admin_return_request(
            created["request_id"], 999, "ملاحظة"
        )
        response = client.patch(
            f"/api/task-requests/{created['request_id']}",
            headers=_auth(USER_B),
            json=_payload(),
        )
        assert response.status_code == 404
        stored = task_request_store.get_request(created["request_id"])
        assert stored.status == "changes_requested"

    def test_cannot_resubmit_missing_request(self, client):
        response = client.patch(
            "/api/task-requests/999999",
            headers=_auth(),
            json=_payload(),
        )
        assert response.status_code == 404

    def test_patch_cannot_force_approval(self, client):
        """`status` in the PATCH body is an unknown field → 400; the
        request keeps its server-decided state."""
        created = _create(client)
        task_request_store.admin_return_request(
            created["request_id"], 999, "ملاحظة"
        )
        response = client.patch(
            f"/api/task-requests/{created['request_id']}",
            headers=_auth(),
            json=_payload(status="approved"),
        )
        assert response.status_code == 400
        stored = task_request_store.get_request(created["request_id"])
        assert stored.status == "changes_requested"


# ════════════════════════════════════════════════════════════════════
# No user-side privilege: approve/reject do not exist over HTTP
# ════════════════════════════════════════════════════════════════════


class TestNoUserApprovalPower:
    def test_no_approve_route_exists(self, client):
        created = _create(client)
        request_id = created["request_id"]
        for suffix in ("approve", "reject", "decision", "review"):
            response = client.post(
                f"/api/task-requests/{request_id}/{suffix}",
                headers=_auth(),
                json={"decision": "approve"},
            )
            # 404 = no such route; 405 = only the app's static GET
            # catch-all matches the path. Either way NO decision
            # endpoint exists over HTTP.
            assert response.status_code in (404, 405), suffix
        # The request is untouched.
        stored = task_request_store.get_request(request_id)
        assert stored.status == "pending"

    def test_task_routes_source_has_no_request_decision_route(self):
        import inspect
        import task_routes

        source = inspect.getsource(task_routes)
        for forbidden in (
            "def approve_task_request",
            "def reject_task_request",
            '@tasks_bp.post("/api/task-requests/<int:request_id>/approve")',
            '@tasks_bp.post("/api/task-requests/<int:request_id>/reject")',
        ):
            assert forbidden not in source
