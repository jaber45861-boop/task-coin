"""
Owner Task Management API (Phase 17/18 — server-side ownership + edit rules)
===========================================================================

HTTP contract for the ``/api/owner/tasks/<id>`` surface, exercising the
server-side ownership check and the owner edit rules on every request:

  Authentication
  - every owner endpoint rejects unauthenticated / invalid initData

  Ownership (server-side, authoritative)
  - the verified initData user must OWN the published task; a foreign
    task is indistinguishable from a missing one (same 404 body)
  - a non-owner's edit/pause/resume/archive writes NOTHING (no payload
    change, no history event)

  Edit rules (the ONLY writable fields are title + description)
  - financial fields (reward / reward_units) rejected — wallet, ledger
    and funding rows unchanged by any accepted edit
  - admin-review fields (provider / action / target_ref) rejected —
    execution semantics stay an admin decision, and an accepted edit
    never mutates them in the stored payload
  - identity / state keys (user_id, status, history, payload,
    published_task_id, ...) rejected outright
  - non-dict bodies and blank/non-string values rejected
  - every accepted edit is audited (append-only ``owner_edit`` history
    event with the acting owner)

  Task states (append-only history lifecycle)
  - pause → participation blocked (workers 409, catalog hidden),
    edit blocked (400), owner read surfaces keep working, idempotent
  - resume → everything restored, idempotent
  - archive → logical hide (owner list + catalog), workers 404,
    idempotent, no delete / no financial mutation

No production database, no payment/withdrawal/wallet mutation: the
wallet assertions below only prove an accepted edit leaves money rows
untouched.

Run:
    python3 -m pytest test_task_owner_management.py -v
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

import config
import db
import serve_miniapp
import task_owner_management as owner
import task_request_admin as admin
import task_request_store as store
import task_routes
import wallet

from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

OWNER = 4201
STRANGER = 9999
ADMIN_ID = 5101

USDT = wallet.USDT_SCALE
SEED = 1000 * USDT

VALID_BODY = {"title": "عنوان معدل", "description": "وصف معدل"}

# Every body key an owner must NEVER be able to write, with the reason
# it is forbidden (financial / identity / admin-review / state).
FORBIDDEN_FIELDS = {
    "reward": "financial",
    "reward_units": "financial",
    "provider": "admin-review",
    "action": "admin-review",
    "target_ref": "admin-review",
    "user_id": "identity",
    "status": "state",
    "history": "state",
    "payload": "state",
    "published_task_id": "state",
    "task_id": "state",
}


# ── Fixtures & helpers ─────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated DB + verified Telegram identities + funded users."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    db_path = str(tmp_path / "task_owner_mgmt.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(OWNER, "owner", "Owner")
    db.register_user(STRANGER, "stranger", "Stranger")
    monkeypatch.setattr(config, "ADMINS", [ADMIN_ID])
    wallet.credit_units(OWNER, SEED)
    wallet.credit_units(STRANGER, SEED)
    yield db_path


@pytest.fixture
def client(env):
    serve_miniapp.app.config["TESTING"] = True
    return serve_miniapp.app.test_client()


def _auth(user_id: int) -> dict:
    return {INIT_DATA_HEADER: _make_init_data(user_id=user_id)}


def _approve(request_id: int) -> None:
    """Approve through the EXISTING admin implementation (one task,
    funded from the owner's wallet) — fixture plumbing, not a second
    approval path."""
    query = mock.MagicMock()
    query.answer = mock.AsyncMock()
    query.edit_message_text = mock.AsyncMock()
    asyncio.run(admin._approve(query, ADMIN_ID, request_id))


def _publish(client) -> tuple[int, int]:
    """Create + approve an owner task.  Returns ``(request_id, task_id)``."""
    response = client.post(
        "/api/task-requests",
        headers=_auth(OWNER),
        json={
            "title": "متابعة حسابي على Instagram",
            "description": "تابع الحساب ثم أرسل إثبات المتابعة",
            "provider": "instagram",
            "action": "follow",
            "target_ref": "https://instagram.com/example",
            "reward": "0.5",
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    request_id = response.get_json()["request"]["request_id"]
    _approve(request_id)
    stored = store.get_request(request_id)
    assert stored.status == store.STATUS_APPROVED
    assert isinstance(stored.published_task_id, int)
    return request_id, stored.published_task_id


def _get(client, task_id, user=STRANGER):
    return client.get(f"/api/owner/tasks/{task_id}", headers=_auth(user))


def _op(client, task_id, op, body=None, user=STRANGER):
    return client.post(
        f"/api/owner/tasks/{task_id}/{op}", headers=_auth(user), json=body
    )


def _events(request_id: int) -> list[str]:
    return [e.get("event") for e in store.get_request(request_id).history]


def _wallet_units(user_id: int) -> int | None:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT available_units FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return None if row is None else int(row["available_units"])


def _count(sql: str, params: tuple = ()) -> int:
    with db.get_connection() as conn:
        return int(conn.execute(sql, params).fetchone()["n"])


def _catalog_ids(client, user=STRANGER) -> list[int]:
    response = client.get("/api/tasks", headers=_auth(user))
    assert response.status_code == 200, response.get_data(as_text=True)
    return [t["id"] for t in response.get_json()["tasks"]]


# ════════════════════════════════════════════════════════════════════════
# Authentication
# ════════════════════════════════════════════════════════════════════════


class TestAuthentication:
    def test_every_endpoint_rejects_unauthenticated(self, client):
        assert client.get("/api/owner/tasks/1").status_code == 401
        for op in ("edit", "pause", "resume", "archive"):
            response = client.post(
                f"/api/owner/tasks/1/{op}", json=VALID_BODY
            )
            assert response.status_code == 401, op
            assert response.get_json()["error"] == "unauthenticated"

    def test_invalid_init_data_rejected(self, client):
        response = client.get(
            "/api/owner/tasks/1",
            headers={INIT_DATA_HEADER: "not-valid"},
        )
        assert response.status_code == 401


# ════════════════════════════════════════════════════════════════════════
# Ownership — server-side, verified on EVERY request
# ════════════════════════════════════════════════════════════════════════


class TestOwnership:
    def test_owner_reads_own_published_task(self, client):
        request_id, task_id = _publish(client)
        response = _get(client, task_id, user=OWNER)
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["published_task_id"] == task_id
        assert data["request"]["request_id"] == request_id
        assert data["request"]["status"] == "approved"
        assert data["request"]["paused"] is False
        assert data["request"]["archived"] is False

    def test_foreign_task_is_404_and_indistinguishable(self, client):
        _request_id, task_id = _publish(client)
        foreign = _get(client, task_id, user=STRANGER)
        missing = _get(client, task_id + 10_000, user=STRANGER)
        assert foreign.status_code == 404
        assert missing.status_code == 404
        assert foreign.get_json() == missing.get_json()
        assert foreign.get_json()["error"] == "owner_task_not_found"

    def test_stranger_edit_writes_nothing(self, client):
        request_id, task_id = _publish(client)
        before = store.get_request(request_id)
        response = _op(client, task_id, "edit", VALID_BODY, user=STRANGER)
        assert response.status_code == 404
        after = store.get_request(request_id)
        assert after.payload == before.payload
        assert after.history == before.history
        assert "owner_edit" not in _events(request_id)

    def test_stranger_lifecycle_ops_write_nothing(self, client):
        _request_id, task_id = _publish(client)
        for op in ("pause", "resume", "archive"):
            response = _op(client, task_id, op, {}, user=STRANGER)
            assert response.status_code == 404, op
            assert response.get_json()["error"] == "owner_task_not_found"
        # The published task stays open and no owner event was written.
        assert owner.participation_block(task_id) is None
        request_id = store.list_for_user(OWNER)[0].request_id
        assert all(
            e not in ("owner_pause", "owner_resume", "owner_archive")
            for e in _events(request_id)
        )

    def test_unpublished_request_not_addressable(self, client):
        """A pending request has no published task: its id can never be
        reached through the owner surface."""
        response = client.post(
            "/api/task-requests",
            headers=_auth(OWNER),
            json={
                "title": "مهمة معلقة",
                "description": "وصف المهمة المعلقة",
                "provider": "instagram",
                "action": "follow",
                "target_ref": "https://instagram.com/x",
                "reward": "0.5",
            },
        )
        assert response.status_code == 200
        pending_id = response.get_json()["request"]["request_id"]
        assert store.get_request(pending_id).published_task_id is None
        assert _get(client, pending_id, user=OWNER).status_code == 404
        assert _get(client, 0, user=OWNER).status_code == 404


# ════════════════════════════════════════════════════════════════════════
# Edit rules — only title + description, everything else rejected
# ════════════════════════════════════════════════════════════════════════


class TestOwnerEditRules:
    def test_owner_edits_title_and_description(self, client):
        request_id, task_id = _publish(client)
        response = _op(client, task_id, "edit", VALID_BODY, user=OWNER)
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["request"]["title"] == VALID_BODY["title"]
        assert data["request"]["description"] == VALID_BODY["description"]
        assert data["message"] == task_routes._MSG_OWNER_EDIT_OK

        # Persisted: a fresh read and the owner GET agree.
        stored = store.get_request(request_id)
        assert stored.payload["title"] == VALID_BODY["title"]
        assert stored.payload["description"] == VALID_BODY["description"]
        fetched = _get(client, task_id, user=OWNER).get_json()["request"]
        assert fetched["title"] == VALID_BODY["title"]

    def test_values_are_validated_and_stripped(self, client):
        _request_id, task_id = _publish(client)
        response = _op(
            client,
            task_id,
            "edit",
            {"title": "   عنوان مزيّت   ", "description": "  وصف  "},
            user=OWNER,
        )
        assert response.status_code == 200
        assert response.get_json()["request"]["title"] == "عنوان مزيّت"

    @pytest.mark.parametrize("bad_body", [None, [], "text", 5])
    def test_non_dict_bodies_rejected(self, client, bad_body):
        """No JSON at all, or any non-object body, is a 400."""
        _request_id, task_id = _publish(client)
        response = client.post(
            f"/api/owner/tasks/{task_id}/edit",
            headers=_auth(OWNER),
            json=bad_body,
        )
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_payload"

    @pytest.mark.parametrize(
        "field", sorted(FORBIDDEN_FIELDS), ids=FORBIDDEN_FIELDS
    )
    def test_forbidden_field_alone_is_rejected(self, client, field):
        """Every non-whitelisted key — financial, identity, state or
        admin-review — is rejected, with zero state change."""
        request_id, task_id = _publish(client)
        before = store.get_request(request_id)
        response = _op(
            client,
            task_id,
            "edit",
            {field: "x", **VALID_BODY},
            user=OWNER,
        )
        assert response.status_code == 400
        assert response.get_json()["message"] == owner.MSG_INVALID_FIELD_ENGLISH
        after = store.get_request(request_id)
        assert after.payload == before.payload
        assert after.history == before.history
        assert "owner_edit" not in _events(request_id)

    def test_unknown_extra_key_rejected(self, client):
        _request_id, task_id = _publish(client)
        response = _op(
            client,
            task_id,
            "edit",
            {**VALID_BODY, "sneaky": True},
            user=OWNER,
        )
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "body",
        [
            {"title": "", "description": "ok"},
            {"title": "   ", "description": "ok"},
            {"title": "ok", "description": ""},
            {"title": "ok", "description": "   "},
            {"title": None, "description": "ok"},
            {"title": "ok", "description": 123},
            {"description": "missing title"},
            {"title": "missing description"},
        ],
    )
    def test_blank_or_non_string_values_rejected(self, client, body):
        request_id, task_id = _publish(client)
        before = store.get_request(request_id)
        response = _op(client, task_id, "edit", body, user=OWNER)
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_payload"
        assert store.get_request(request_id).payload == before.payload

    def test_admin_review_fields_survive_an_edit(self, client):
        """An accepted edit rewrites ONLY title/description — the stored
        provider/action/target_ref keep their original values."""
        request_id, task_id = _publish(client)
        before = store.get_request(request_id).payload
        assert _op(
            client, task_id, "edit", VALID_BODY, user=OWNER
        ).status_code == 200
        after = store.get_request(request_id).payload
        assert after["title"] == VALID_BODY["title"]
        for key in ("provider", "action", "target_ref"):
            assert after[key] == before[key], key

    def test_edit_is_audited_in_history(self, client):
        request_id, task_id = _publish(client)
        _op(client, task_id, "edit", VALID_BODY, user=OWNER)
        events = [
            e for e in store.get_request(request_id).history
            if e.get("event") == "owner_edit"
        ]
        assert len(events) == 1
        assert events[0]["actor"] == OWNER
        # The audit records the full stored payload keys (title and
        # description are the EDITED subset; the untouched
        # admin-review keys stay part of the snapshot).
        assert events[0]["fields"] == sorted(
            store.get_request(request_id).payload
        )
        assert {"title", "description"} <= set(events[0]["fields"])
        assert "previous_payload" in events[0]

    def test_edit_touches_no_money_rows(self, client):
        """Financial invariance: an accepted edit changes no wallet,
        ledger, funding or request-reward value."""
        _request_id, task_id = _publish(client)
        wallet_before = _wallet_units(OWNER)
        ledger_before = _count("SELECT COUNT(*) AS n FROM ledger")
        reward_before = store.get_request(
            _request_id
        ).reward_units
        assert _op(
            client, task_id, "edit", VALID_BODY, user=OWNER
        ).status_code == 200
        assert _wallet_units(OWNER) == wallet_before
        assert _count("SELECT COUNT(*) AS n FROM ledger") == ledger_before
        assert store.get_request(_request_id).reward_units == reward_before


# ════════════════════════════════════════════════════════════════════════
# Task states — pause / resume / archive (append-only history)
# ════════════════════════════════════════════════════════════════════════


class TestTaskStates:
    def test_open_task_is_discoverable_and_unblocked(self, client):
        """REGRESSION: a freshly published, running task must answer
        ``None`` from the ownership-blind gate and stay in the catalog
        (the reference implementation returned "running" here, which
        the catalog's ``is not None`` filter treated as "hidden" —
        every approved request's task vanished from discovery)."""
        _request_id, task_id = _publish(client)
        assert owner.participation_block(task_id) is None
        assert task_id in _catalog_ids(client)

    def test_pause_blocks_participation_and_edit(self, client):
        request_id, task_id = _publish(client)
        assert task_id in _catalog_ids(client)

        response = _op(client, task_id, "pause", {}, user=OWNER)
        assert response.status_code == 200
        assert response.get_json()["message"] == task_routes._MSG_OWNER_PAUSED

        # Ownership-blind gate: discovery + worker entry points blocked.
        assert owner.participation_block(task_id) == "paused"
        assert task_id not in _catalog_ids(client)
        start = client.post(
            f"/api/tasks/{task_id}/start", headers=_auth(STRANGER)
        )
        assert start.status_code == 409
        assert start.get_json()["error"] == "task_paused"

        # Edit is refused while paused; the payload stays untouched.
        before = store.get_request(request_id).payload
        edit = _op(client, task_id, "edit", VALID_BODY, user=OWNER)
        assert edit.status_code == 400
        assert (
            edit.get_json()["message"]
            == task_routes._MSG_OWNER_CANNOT_EDIT_PAUSED
        )
        assert store.get_request(request_id).payload == before

        # Owner/reviewer read surfaces keep working while paused.
        assert _get(client, task_id, user=OWNER).status_code == 200
        flagged = client.get(
            "/api/task-requests", headers=_auth(OWNER)
        ).get_json()["requests"]
        row = next(r for r in flagged if r["request_id"] == request_id)
        assert row["paused"] is True

    def test_pause_is_idempotent(self, client):
        request_id, task_id = _publish(client)
        for _ in range(2):
            assert _op(client, task_id, "pause", {}, user=OWNER).status_code == 200
        assert _events(request_id).count("owner_pause") == 1

    def test_resume_restores_everything(self, client):
        request_id, task_id = _publish(client)
        _op(client, task_id, "pause", {}, user=OWNER)
        response = _op(client, task_id, "resume", {}, user=OWNER)
        assert response.status_code == 200
        assert response.get_json()["message"] == task_routes._MSG_OWNER_RESUMED
        assert owner.participation_block(task_id) is None
        assert task_id in _catalog_ids(client)
        # Editing works again after resume.
        assert _op(
            client, task_id, "edit", VALID_BODY, user=OWNER
        ).status_code == 200
        assert _events(request_id).count("owner_resume") == 1

    def test_resume_is_idempotent_while_running(self, client):
        request_id, task_id = _publish(client)
        for _ in range(2):
            assert (
                _op(client, task_id, "resume", {}, user=OWNER).status_code
                == 200
            )
        assert "owner_resume" not in _events(request_id)

    def test_archive_hides_logically_and_blocks_workers(self, client):
        request_id, task_id = _publish(client)
        response = _op(client, task_id, "archive", {}, user=OWNER)
        assert response.status_code == 200
        assert response.get_json()["message"] == task_routes._MSG_OWNER_ARCHIVED

        assert owner.participation_block(task_id) == "archived"
        assert task_id not in _catalog_ids(client)
        start = client.post(
            f"/api/tasks/{task_id}/start", headers=_auth(STRANGER)
        )
        assert start.status_code == 404
        assert start.get_json()["error"] == "task_archived"

        # Hidden from the OWNER's own list, but the row and its money
        # are untouched — no delete, no financial mutation.
        visible = client.get(
            "/api/task-requests", headers=_auth(OWNER)
        ).get_json()["requests"]
        assert all(r["request_id"] != request_id for r in visible)
        assert _get(client, task_id, user=OWNER).status_code == 200
        assert store.get_request(request_id).status == store.STATUS_APPROVED

    def test_archive_is_idempotent(self, client):
        request_id, task_id = _publish(client)
        for _ in range(2):
            assert (
                _op(client, task_id, "archive", {}, user=OWNER).status_code
                == 200
            )
        assert _events(request_id).count("owner_archive") == 1

    def test_owner_lifecycle_never_touches_money(self, client):
        _request_id, task_id = _publish(client)
        wallet_before = _wallet_units(OWNER)
        ledger_before = _count("SELECT COUNT(*) AS n FROM ledger")
        for op in ("pause", "resume", "archive"):
            assert _op(client, task_id, op, {}, user=OWNER).status_code == 200, op
        assert _wallet_units(OWNER) == wallet_before
        assert _count("SELECT COUNT(*) AS n FROM ledger") == ledger_before


# ════════════════════════════════════════════════════════════════════════
# Store-level CAS guards (defense in depth behind the HTTP layer)
# ════════════════════════════════════════════════════════════════════════


class TestStoreGuards:
    def test_store_edit_refuses_foreign_actor(self, client):
        """Even a direct store call re-guards ownership in SQL."""
        request_id, _task_id = _publish(client)
        before = store.get_request(request_id)
        with db.get_connection() as conn:
            result = store.owner_edit_payload(
                conn, request_id, STRANGER, {"title": "x"}
            )
        assert result is None
        assert store.get_request(request_id).payload == before.payload

    def test_store_history_event_refuses_foreign_actor(self, client):
        request_id, _task_id = _publish(client)
        with db.get_connection() as conn:
            result = store.owner_append_history_event(
                conn, request_id, STRANGER, "owner_pause"
            )
        assert result is None
        assert "owner_pause" not in _events(request_id)
        assert owner.participation_block(
            store.get_request(request_id).published_task_id
        ) is None
