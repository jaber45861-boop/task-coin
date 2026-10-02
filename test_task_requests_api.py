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

  Rate limit (POST only)
  - exceeding the per-verified-user limit returns a clear HTTP 429;
    GET, PATCH (the resend-after-changes flow), the admin workflow
    and the state machine are never consulted by the limiter

  No privilege escalation
  - there is NO user-side approve/reject endpoint at all

Run:
    python3 -m pytest test_task_requests_api.py -v
"""

import threading

import pytest

import db
import serve_miniapp
import task_request_rate_limit as rate_limit
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


# ════════════════════════════════════════════════════════════════════
# Rate limit — POST only, per verified user, race-free
# ════════════════════════════════════════════════════════════════════


class TestRateLimit:
    """POST /api/task-requests alone carries a per-user rate limit.

    GET list/detail, PATCH resubmit (the resend-after-changes flow),
    the admin workflow and the request state machine must behave
    exactly as before — proven here both at HTTP level and in the
    module sources.
    """

    def test_normal_post_succeeds_within_limit(self, client, monkeypatch):
        monkeypatch.setattr(rate_limit, "MAX_ATTEMPTS_PER_WINDOW", 3)
        for i in range(3):
            response = client.post(
                "/api/task-requests",
                headers=_auth(),
                json=_payload(title=f"طلب رقم {i}"),
            )
            assert response.status_code == 200, response.get_data(
                as_text=True
            )
        # The budget is now spent — the very next attempt is limited.
        response = client.post(
            "/api/task-requests", headers=_auth(), json=_payload()
        )
        assert response.status_code == 429

    def test_exceeding_limit_returns_clear_429(self, client, monkeypatch):
        monkeypatch.setattr(rate_limit, "MAX_ATTEMPTS_PER_WINDOW", 2)
        for _ in range(2):
            assert client.post(
                "/api/task-requests", headers=_auth(), json=_payload()
            ).status_code == 200

        response = client.post(
            "/api/task-requests", headers=_auth(), json=_payload()
        )
        assert response.status_code == 429
        body = response.get_json()
        assert body["ok"] is False
        assert body["error"] == "rate_limited"
        assert body["message"]  # user-facing Arabic message, not a blank
        assert body["retry_after"] >= 1
        # Standard Retry-After header mirrors the JSON hint.
        assert response.headers.get("Retry-After") == str(body["retry_after"])

    def test_blocked_post_creates_no_state(self, client, monkeypatch):
        """A rejected attempt must leave the store untouched: the
        limiter rejects BEFORE the state machine can run."""
        monkeypatch.setattr(rate_limit, "MAX_ATTEMPTS_PER_WINDOW", 1)
        created = client.post(
            "/api/task-requests", headers=_auth(), json=_payload()
        )
        assert created.status_code == 200
        request_id = created.get_json()["request"]["request_id"]

        for _ in range(3):
            blocked = client.post(
                "/api/task-requests", headers=_auth(), json=_payload()
            )
            assert blocked.status_code == 429

        own = task_request_store.list_for_user(USER_A)
        assert [r.request_id for r in own] == [request_id]

    def test_rate_limit_is_isolated_per_user(self, client, monkeypatch):
        monkeypatch.setattr(rate_limit, "MAX_ATTEMPTS_PER_WINDOW", 1)
        assert client.post(
            "/api/task-requests", headers=_auth(USER_A), json=_payload()
        ).status_code == 200
        assert client.post(
            "/api/task-requests", headers=_auth(USER_A), json=_payload()
        ).status_code == 429

        # User B has their own budget — A's exhaustion cannot touch it.
        assert client.post(
            "/api/task-requests", headers=_auth(USER_B), json=_payload()
        ).status_code == 200
        assert client.post(
            "/api/task-requests", headers=_auth(USER_B), json=_payload()
        ).status_code == 429

        # …and A is still blocked afterwards.
        assert client.post(
            "/api/task-requests", headers=_auth(USER_A), json=_payload()
        ).status_code == 429

    def test_unauthenticated_attempts_never_consume_budget(
        self, client, monkeypatch
    ):
        """The counter is keyed by VERIFIED identity: strangers
        hammering the endpoint without valid initData cannot spend
        a real user's budget (and never reach the limiter at all)."""
        monkeypatch.setattr(rate_limit, "MAX_ATTEMPTS_PER_WINDOW", 1)
        for _ in range(4):
            assert client.post(
                "/api/task-requests", json=VALID_PAYLOAD
            ).status_code == 401
            assert client.post(
                "/api/task-requests",
                headers={INIT_DATA_HEADER: "not-valid"},
                json=VALID_PAYLOAD,
            ).status_code == 401

        assert client.post(
            "/api/task-requests", headers=_auth(), json=_payload()
        ).status_code == 200

    def test_get_unaffected_while_post_is_blocked(self, client, monkeypatch):
        monkeypatch.setattr(rate_limit, "MAX_ATTEMPTS_PER_WINDOW", 1)
        created = _create(client)  # spends the whole budget
        assert client.post(
            "/api/task-requests", headers=_auth(), json=_payload()
        ).status_code == 429

        listing = client.get("/api/task-requests", headers=_auth())
        assert listing.status_code == 200
        assert len(listing.get_json()["requests"]) == 1

        detail = client.get(
            f"/api/task-requests/{created['request_id']}",
            headers=_auth(),
        )
        assert detail.status_code == 200

    def test_patch_resend_unaffected_while_post_is_blocked(
        self, client, monkeypatch
    ):
        """The natural resend-after-changes_requested flow is PATCH
        and must never be blocked by the POST limiter."""
        monkeypatch.setattr(rate_limit, "MAX_ATTEMPTS_PER_WINDOW", 1)
        created = _create(client)  # spends the whole budget
        task_request_store.admin_return_request(
            created["request_id"], 999, "عدّل الوصف"
        )
        assert client.post(
            "/api/task-requests", headers=_auth(), json=_payload()
        ).status_code == 429  # POST blocked…

        response = client.patch(
            f"/api/task-requests/{created['request_id']}",
            headers=_auth(),
            json=_payload(title="عنوان معدل بعد التعديل"),
        )
        assert response.status_code == 200  # …PATCH still works
        assert response.get_json()["request"]["status"] == "pending"
        # No state-machine regression: the store recorded the resubmit.
        stored = task_request_store.get_request(created["request_id"])
        assert stored.status == "pending"
        assert stored.payload["title"] == "عنوان معدل بعد التعديل"

    def test_window_lifts_after_window_seconds(self, client, monkeypatch):
        """The block is temporary: once the window passes, the next
        attempt is admitted again (rejected attempts never extend it)."""
        monkeypatch.setattr(rate_limit, "MAX_ATTEMPTS_PER_WINDOW", 1)
        assert client.post(
            "/api/task-requests", headers=_auth(), json=_payload()
        ).status_code == 200
        assert client.post(
            "/api/task-requests", headers=_auth(), json=_payload()
        ).status_code == 429

        real_now = rate_limit._now
        monkeypatch.setattr(
            rate_limit,
            "_now",
            lambda: real_now() + rate_limit.WINDOW_SECONDS + 1,
        )
        assert client.post(
            "/api/task-requests", headers=_auth(), json=_payload()
        ).status_code == 200

    def test_concurrent_requests_cannot_bypass_limit(self, env, monkeypatch):
        """A synchronized burst of simultaneous POSTs admits EXACTLY
        the limit: check+count is one locked step, so no interleaving
        of the two can squeeze an extra attempt through."""
        limit = 5
        total = 30
        monkeypatch.setattr(rate_limit, "MAX_ATTEMPTS_PER_WINDOW", limit)
        serve_miniapp.app.config["TESTING"] = True

        barrier = threading.Barrier(total)
        statuses: list[int] = []
        statuses_lock = threading.Lock()

        def worker():
            client = serve_miniapp.app.test_client()
            barrier.wait()
            # Invalid body: an ADMITTED attempt stops at the 400 (no
            # store write), a DENIED one at the 429 — the split is
            # exactly the limiter's decision.
            response = client.post(
                "/api/task-requests", headers=_auth(), json=[]
            )
            with statuses_lock:
                statuses.append(response.status_code)

        threads = [
            threading.Thread(target=worker) for _ in range(total)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(statuses) == total
        assert statuses.count(400) == limit, \
            f"admitted {statuses.count(400)} attempts, limit was {limit}"
        assert statuses.count(429) == total - limit

    def test_rate_limit_is_confined_to_the_post_handler(self):
        """GET/PATCH handlers, the store and the admin workflow must
        never consult the limiter."""
        import inspect
        import task_request_admin
        import task_routes

        source = inspect.getsource(task_routes)
        # Exactly ONE call site in the whole module.
        assert source.count("check_attempt(") == 1
        # …and it sits inside create_task_request (POST).
        post_only = source.split("def create_task_request()")[1].split(
            "def list_task_requests()"
        )[0]
        assert "check_attempt(" in post_only
        # Everything after POST (GET list, GET detail, PATCH) is clean.
        assert "check_attempt(" not in source.split(
            "def list_task_requests()"
        )[1]
        # The store and the Telegram admin workflow never see it.
        assert "task_request_rate_limit" not in inspect.getsource(
            task_request_admin
        )
        assert "task_request_rate_limit" not in inspect.getsource(
            task_request_store
        )
