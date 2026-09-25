"""
Focused tests — manual task reviewer surface (MT-TASK-16)
==========================================================

Backend-only coverage for the reviewer surface of manual proof
tasks.  The surface itself is the existing pair of routes:

- GET  /api/tasks/<task_id>/claims          list pending claims
- POST /api/tasks/<task_id>/claims/<sid>/decision   approve|reject

Both reuse the existing services — no approval logic lives in the
routes:

- authorization for listing comes ONLY from
  task_data.approver.telegram_user_id vs the verified initData
  identity (no admin fallback, no body-supplied ids)
- decisions are applied ONLY by ManualReviewService.decide()
  (CAS/idempotency from TaskSubmissionStore preserved)

Coverage required by MT-TASK-16
- authorized approver can list pending manual claims
- unauthorized user cannot list them
- worker cannot list them
- unauthenticated user cannot list them
- only safe review fields are returned (claim id, task id,
  submitted_at, proof_ref — never worker identity)
- authorized approver can approve
- authorized approver can reject
- worker cannot decide
- stranger cannot decide
- repeated/conflicting decisions remain protected by existing CAS
- routes do not duplicate approval logic / add an admin fallback

Regressions for referral/telegram_channel/channel_subscription run
in their own suites (test_referral_task.py,
test_telegram_channel_task_verifier.py, test_channel_task_verifier.py)
and are executed alongside this file.

Run:
    python3 -m pytest test_manual_review.py -v
"""

from __future__ import annotations

import json
import os

import pytest

import db
import serve_miniapp
import wallet
from manual_task import (
    MANUAL_TASK_TYPE,
    ManualProofService,
    worker_awaiting_decision,
)
from task_start import TaskStartGate
from task_submission_store import TaskSubmissionStore

# Reuse the existing, independently implemented Telegram initData helper.
from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

APPROVER = 5101
WORKER = 6202
STRANGER = 7303

REWARD_USDT = 30
REWARD_UNITS = REWARD_USDT * wallet.USDT_SCALE

PROOF = "https://t.me/c/1234567890/99"

# The exact safe review payload allowed for a manual claim.
SAFE_REVIEW_FIELDS = {"claim_id", "task_id", "submitted_at", "proof_ref"}

# Fields that would leak worker identity or internals — never allowed.
FORBIDDEN_REVIEW_FIELDS = {
    "user_id", "username", "first_name", "worker_id", "worker",
    "idempotency_key", "reward", "task_data", "approver",
}


def _valid_task_data() -> dict:
    return {
        "provider": "telegram",
        "action": "proof",
        "approver": {"telegram_user_id": APPROVER},
    }


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Temp DB, users, and one valid manual proof task."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    db_path = str(tmp_path / "manual_review_test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)

    db.register_user(APPROVER, "approver", "Approver")
    db.register_user(WORKER, "worker", "Worker")
    db.register_user(STRANGER, "stranger", "Stranger")

    task_id = db.create_task(
        title="مهمة إثبات يدوي",
        description="أرسل إثباتاً وانتظر مراجعة المشرف",
        task_type=MANUAL_TASK_TYPE,
        reward=REWARD_USDT,
        task_data=json.dumps(_valid_task_data()),
    )

    yield {"db_path": db_path, "task_id": task_id}


@pytest.fixture
def client(env):
    serve_miniapp.app.config["TESTING"] = True
    return serve_miniapp.app.test_client()


# ── Helpers ───────────────────────────────────────────────────────


def _auth(user_id: int) -> dict:
    return {INIT_DATA_HEADER: _make_init_data(user_id=user_id)}


def _start(user_id: int, task_id: int) -> None:
    TaskStartGate().start(user_id, task_id)


def _status(user_id: int, task_id: int) -> str | None:
    row = db.get_user_task(user_id, task_id)
    return row["status"] if row else None


def _wallet_units(user_id: int) -> int:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT available_units FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return 0 if row is None else row["available_units"]


def _task_credits(user_id: int) -> list[dict]:
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT amount_units FROM ledger "
            "WHERE user_id = ? AND entry_type = 'credit' "
            "AND reference_type = 'task' ORDER BY id",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _claim_row(submission_id: int) -> dict:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT user_id, task_id, status, approval_status, "
            "       approver_user_id, proof_ref "
            "FROM task_submissions WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
    assert row is not None, f"claim {submission_id} missing"
    return dict(row)


def _open_claim(env) -> int:
    """Start WORKER's cycle and open one pending manual claim."""
    tid = env["task_id"]
    _start(WORKER, tid)
    return ManualProofService.submit(
        WORKER, tid, PROOF, "k1").submission_id


def _list_claims(client, tid, user_id=None, **kwargs):
    headers = _auth(user_id) if user_id is not None else {}
    return client.get(f"/api/tasks/{tid}/claims",
                      headers=headers, **kwargs)


def _decide(client, tid, sid, decision, user_id=None, **kwargs):
    headers = _auth(user_id) if user_id is not None else {}
    return client.post(
        f"/api/tasks/{tid}/claims/{sid}/decision",
        headers=headers, json={"decision": decision}, **kwargs)


# ══════════════════════════════════════════════════════════════════
# Listing — approver-only, safe review fields only
# ══════════════════════════════════════════════════════════════════


class TestReviewerList:

    def test_authorized_approver_lists_pending_claims(self, client, env):
        tid = env["task_id"]
        sid = _open_claim(env)
        resp = _list_claims(client, tid, APPROVER)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["claims"]) == 1
        claim = data["claims"][0]
        assert claim["claim_id"] == sid
        assert claim["task_id"] == tid
        assert claim["proof_ref"] == PROOF
        assert claim["submitted_at"] is not None

    def test_unauthorized_stranger_cannot_list(self, client, env):
        tid = env["task_id"]
        _open_claim(env)
        resp = _list_claims(client, tid, STRANGER)
        assert resp.status_code == 403
        body = resp.get_json()
        assert body["error"] == "not_approver"
        assert "claims" not in body

    def test_worker_cannot_list(self, client, env):
        tid = env["task_id"]
        _open_claim(env)
        resp = _list_claims(client, tid, WORKER)
        assert resp.status_code == 403
        body = resp.get_json()
        assert body["error"] == "not_approver"
        assert "claims" not in body

    def test_unauthenticated_cannot_list(self, client, env):
        tid = env["task_id"]
        _open_claim(env)
        resp = _list_claims(client, tid)
        assert resp.status_code == 401
        body = resp.get_json()
        assert body["error"] == "unauthenticated"
        assert "claims" not in body

    def test_only_safe_review_fields_returned(self, client, env):
        tid = env["task_id"]
        _open_claim(env)
        resp = _list_claims(client, tid, APPROVER)
        assert resp.status_code == 200
        claim = resp.get_json()["claims"][0]
        # Exactly the safe review data needed to decide — nothing else.
        assert set(claim.keys()) == SAFE_REVIEW_FIELDS
        assert claim["task_id"] == tid
        assert claim["proof_ref"] == PROOF
        for field in FORBIDDEN_REVIEW_FIELDS:
            assert field not in claim

    def test_decided_claims_are_not_listed(self, client, env):
        tid = env["task_id"]
        sid = _open_claim(env)
        resp = _decide(client, tid, sid, "approve", APPROVER)
        assert resp.status_code == 200
        resp = _list_claims(client, tid, APPROVER)
        assert resp.status_code == 200
        assert resp.get_json()["claims"] == []

    def test_authorization_comes_from_definition_not_body(
        self, client, env
    ):
        """A body claiming to be the approver grants nothing."""
        tid = env["task_id"]
        _open_claim(env)
        resp = client.get(f"/api/tasks/{tid}/claims",
                          headers=_auth(STRANGER),
                          json={"approver": APPROVER,
                                "user_id": APPROVER})
        assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════
# Decisions — server-authorized via ManualReviewService.decide()
# ══════════════════════════════════════════════════════════════════


class TestReviewerDecision:

    def test_authorized_approver_approves(self, client, env):
        tid = env["task_id"]
        sid = _open_claim(env)
        resp = _decide(client, tid, sid, "approve", APPROVER)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["claim_id"] == sid
        assert data["approval"] == "approved"

        # Completion + exactly one credit — the existing service path.
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_COMPLETED
        claim = _claim_row(sid)
        assert claim["approval_status"] == (
            db.SUBMISSION_APPROVAL_APPROVED)
        assert claim["approver_user_id"] == APPROVER
        assert claim["status"] == db.SUBMISSION_STATUS_PASSED
        assert _wallet_units(WORKER) == REWARD_UNITS
        assert len(_task_credits(WORKER)) == 1

    def test_authorized_approver_rejects(self, client, env):
        tid = env["task_id"]
        sid = _open_claim(env)
        resp = _decide(client, tid, sid, "reject", APPROVER)
        assert resp.status_code == 200
        assert resp.get_json()["approval"] == "rejected"

        claim = _claim_row(sid)
        assert claim["approval_status"] == (
            db.SUBMISSION_APPROVAL_REJECTED)
        assert claim["status"] == db.SUBMISSION_STATUS_FAILED
        # No completion, no reward; the worker may retry.
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_STARTED
        assert _wallet_units(WORKER) == 0
        assert _task_credits(WORKER) == []

    def test_worker_cannot_decide(self, client, env):
        tid = env["task_id"]
        sid = _open_claim(env)
        resp = _decide(client, tid, sid, "approve", WORKER)
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "not_approver"
        # Nothing changed: still pending, no completion, no credit.
        assert _claim_row(sid)["approval_status"] == (
            db.SUBMISSION_APPROVAL_PENDING)
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_STARTED
        assert _wallet_units(WORKER) == 0

    def test_stranger_cannot_decide(self, client, env):
        tid = env["task_id"]
        sid = _open_claim(env)
        # Impersonation fields in the body are ignored entirely.
        resp = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(STRANGER),
            json={"decision": "approve",
                  "user_id": APPROVER, "approver": APPROVER},
        )
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "not_approver"
        assert _claim_row(sid)["approval_status"] == (
            db.SUBMISSION_APPROVAL_PENDING)
        assert _wallet_units(WORKER) == 0

    def test_unauthenticated_cannot_decide(self, client, env):
        tid = env["task_id"]
        sid = _open_claim(env)
        resp = _decide(client, tid, sid, "approve")
        assert resp.status_code == 401
        assert _claim_row(sid)["approval_status"] == (
            db.SUBMISSION_APPROVAL_PENDING)


# ══════════════════════════════════════════════════════════════════
# CAS — repeated / conflicting decisions stay protected
# ══════════════════════════════════════════════════════════════════


class TestCasProtection:

    def test_repeated_approve_idempotent_one_credit(self, client, env):
        tid = env["task_id"]
        sid = _open_claim(env)
        first = _decide(client, tid, sid, "approve", APPROVER)
        second = _decide(client, tid, sid, "approve", APPROVER)
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.get_json()["approval"] == "approved"
        # CAS + idempotent finalization: one completion, one credit.
        assert _wallet_units(WORKER) == REWARD_UNITS
        assert len(_task_credits(WORKER)) == 1

    def test_repeated_reject_idempotent_no_credit(self, client, env):
        tid = env["task_id"]
        sid = _open_claim(env)
        first = _decide(client, tid, sid, "reject", APPROVER)
        second = _decide(client, tid, sid, "reject", APPROVER)
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.get_json()["approval"] == "rejected"
        assert _claim_row(sid)["approval_status"] == (
            db.SUBMISSION_APPROVAL_REJECTED)
        assert _wallet_units(WORKER) == 0

    def test_conflicting_reject_after_approve_409(self, client, env):
        tid = env["task_id"]
        sid = _open_claim(env)
        assert _decide(
            client, tid, sid, "approve", APPROVER).status_code == 200
        conflict = _decide(client, tid, sid, "reject", APPROVER)
        assert conflict.status_code == 409
        assert conflict.get_json()["error"] == "claim_already_approved"
        # The approved outcome stands — exactly one credit.
        assert _claim_row(sid)["approval_status"] == (
            db.SUBMISSION_APPROVAL_APPROVED)
        assert len(_task_credits(WORKER)) == 1

    def test_conflicting_approve_after_reject_409(self, client, env):
        tid = env["task_id"]
        sid = _open_claim(env)
        assert _decide(
            client, tid, sid, "reject", APPROVER).status_code == 200
        conflict = _decide(client, tid, sid, "approve", APPROVER)
        assert conflict.status_code == 409
        assert conflict.get_json()["error"] == "claim_already_rejected"
        # The rejection stands — no completion, no credit.
        assert _claim_row(sid)["approval_status"] == (
            db.SUBMISSION_APPROVAL_REJECTED)
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_STARTED
        assert _wallet_units(WORKER) == 0

    def test_decided_claim_no_longer_pending(self, client, env):
        """The pending list and the CAS agree after a decision."""
        tid = env["task_id"]
        sid = _open_claim(env)
        assert _decide(
            client, tid, sid, "approve", APPROVER).status_code == 200
        assert TaskSubmissionStore.list_pending_claims_for_task(
            tid) == []
        assert worker_awaiting_decision(WORKER, tid) is False
        resp = _list_claims(client, tid, APPROVER)
        assert resp.get_json()["claims"] == []


# ══════════════════════════════════════════════════════════════════
# Source guards — the route layer never owns approval logic
# ══════════════════════════════════════════════════════════════════


class TestSurfaceSourceGuards:

    def _source(self, module) -> str:
        with open(module.__file__, encoding="utf-8") as fh:
            return fh.read()

    def test_routes_delegate_to_manual_review_service(self):
        import task_routes
        src = self._source(task_routes)
        # The decision endpoint dispatches to the service — it never
        # applies the decision itself.
        assert "ManualReviewService.decide" in src
        assert "mark_approval_decision" not in src
        assert "SUBMISSION_APPROVAL_APPROVED" not in src

    def test_no_admin_fallback_anywhere_in_surface(self):
        import manual_task
        import task_routes
        assert "is_admin" not in self._source(manual_task)
        assert "is_admin" not in self._source(task_routes)

    def test_routes_never_write_user_tasks_or_wallets(self):
        import task_routes
        src = self._source(task_routes)
        assert "UPDATE user_tasks" not in src
        assert "CompletionGate(" not in src
        assert "INSERT INTO ledger" not in src
