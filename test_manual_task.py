"""
Focused tests — manual/social proof task family (MT-TASK-15)
============================================================

Backend-only coverage for the manual proof family, modeled on the
referral approval architecture:

Task definition (server-side task_data contract)
- manual is a distinct task type
- valid task_data validates (provider/action/approver)
- invalid provider / action / approver rejected
- reward inside task_data rejected (reward lives in tasks.reward)
- safe accessors (approver id, awaiting-decision state)

Proof (bounded text/URL reference only)
- invalid proof rejected (type/empty/control chars)
- proof length bounds (MAX_PROOF_REF_LENGTH)
- proof persisted only through TaskSubmissionStore.proof_ref

Submission (claim = task_submissions row born approval-pending)
- one pending claim per (user, task), incl. concurrent race
- submission never completes, never credits, never touches user_tasks
- pre-conditions reuse TaskAttemptPolicy; approver cannot self-submit

Approval (server-authorized reviewer decision only)
- unauthorized / arbitrary authenticated approver rejected
- authorized approver approves → completion → exactly one credit
- repeated approval idempotent; conflicting decision rejected
- rejection leaves user_task started; retry with new proof allowed

Security
- proof cannot override identity, task, reward, or authorization
- forbidden body fields rejected; identity comes from initData only

Routes
- submit dispatch for manual type; awaiting_decision worker state
- claims view (approver only) carries proof_ref; no worker identity
- decision endpoint 401/403/400/404/409 mappings

Regressions
- telegram_channel / channel_subscription never take the manual path
- referral dispatch, claims fields and decisions unchanged

Run:
    python3 -m pytest test_manual_task.py -v
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

import db
import serve_miniapp
import wallet
from manual_task import (
    MANUAL_TASK_TYPE,
    ManualDecisionError,
    ManualProofError,
    ManualProofService,
    ManualReviewService,
    ManualTaskDataError,
    MAX_PROOF_REF_LENGTH,
    manual_task_approver_user_id,
    normalize_proof_ref,
    parse_manual_task_data,
    validate_manual_task_data,
    worker_awaiting_decision,
)
from referral_task import REFERRAL_TASK_TYPE
from task_start import TaskStartGate
from task_submission import FORBIDDEN_FIELDS
from task_submission_store import TaskSubmissionStore

# Reuse the existing, independently implemented Telegram initData helper.
from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

APPROVER = 5101
WORKER = 6202
STRANGER = 7303
OTHER = 8404
REFERRED = 9105   # referred_by = WORKER (for the referral regression)

REWARD_USDT = 30
REWARD_UNITS = REWARD_USDT * wallet.USDT_SCALE

PROOF = "https://t.me/c/1234567890/99"


# ── Contract helpers ──────────────────────────────────────────────


def _valid_task_data(approver: int = APPROVER) -> dict:
    """A contract-conformant manual task_data payload."""
    return {
        "provider": "telegram",
        "action": "proof",
        "approver": {"telegram_user_id": approver},
    }


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Temp DB, users, and one valid manual proof task."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    db_path = str(tmp_path / "manual_task_test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)

    db.register_user(APPROVER, "approver", "Approver")
    db.register_user(WORKER, "worker", "Worker")
    db.register_user(STRANGER, "stranger", "Stranger")
    db.register_user(OTHER, "other", "Other")
    # Genuine referral attribution so WORKER can claim a referral task
    # in the dispatch-regression test (registration rules untouched).
    db.register_user(REFERRED, "referred", "Referred",
                     referred_by=WORKER)

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
            "SELECT amount_units, reference_id FROM ledger "
            "WHERE user_id = ? AND entry_type = 'credit' "
            "AND reference_type = 'task' ORDER BY id",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _claim_row(submission_id: int) -> dict:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT user_id, task_id, status, approval_status, "
            "       approver_user_id, approval_decided_at, "
            "       idempotency_key, completed_at, verification_reason, "
            "       proof_ref "
            "FROM task_submissions WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
    assert row is not None, f"claim {submission_id} missing"
    return dict(row)


def _claim_rows(task_id: int, user_id: int = WORKER) -> list[dict]:
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT submission_id, status, approval_status, "
            "       idempotency_key FROM task_submissions "
            "WHERE user_id = ? AND task_id = ? ORDER BY submission_id",
            (user_id, task_id),
        ).fetchall()
    return [dict(r) for r in rows]


def _make_task(task_data, *, reward: int = 25,
               task_type: str | None = None, **kwargs) -> int:
    """Create an extra task with the given raw task_data."""
    if task_data is None:
        raw = None
    elif isinstance(task_data, str):
        raw = task_data
    else:
        raw = json.dumps(task_data)
    return db.create_task(
        title="Extra Manual Task",
        description="Extra",
        task_type=task_type or MANUAL_TASK_TYPE,
        reward=reward,
        task_data=raw,
        **kwargs,
    )


# ══════════════════════════════════════════════════════════════════
# Task definition — server-side task_data contract
# ══════════════════════════════════════════════════════════════════


class TestTaskDataContract:
    def test_task_type_is_distinct(self, env):
        assert MANUAL_TASK_TYPE == "manual"
        assert MANUAL_TASK_TYPE != "referral_task"
        assert MANUAL_TASK_TYPE != "telegram_channel"
        assert MANUAL_TASK_TYPE != "channel_subscription"
        assert db.get_task(env["task_id"])["type"] == MANUAL_TASK_TYPE

    def test_valid_contract_validates(self):
        data = _valid_task_data()
        assert validate_manual_task_data(data) is data

    @pytest.mark.parametrize("mutate,needle", [
        (lambda d: d.pop("provider"), "provider"),
        (lambda d: d.update(provider="web"), "provider"),
        (lambda d: d.pop("action"), "action"),
        (lambda d: d.update(action="referral"), "action"),
        (lambda d: d.pop("approver"), "approver"),
        (lambda d: d.update(approver="me"), "approver"),
        (lambda d: d["approver"].pop("telegram_user_id"),
         "telegram_user_id"),
        (lambda d: d["approver"].update(admin=True),
         "unexpected approver keys"),
        (lambda d: d["approver"].update(telegram_user_id="123"),
         "integer"),
        (lambda d: d["approver"].update(telegram_user_id=True),
         "integer"),
        (lambda d: d["approver"].update(telegram_user_id=1.5),
         "integer"),
        (lambda d: d["approver"].update(telegram_user_id=0),
         "positive"),
        (lambda d: d["approver"].update(telegram_user_id=-5),
         "positive"),
        (lambda d: d.update(extra=1), "unexpected task_data keys"),
    ])
    def test_invalid_contract_rejected(self, mutate, needle):
        data = _valid_task_data()
        mutate(data)
        with pytest.raises(ManualTaskDataError, match=needle):
            validate_manual_task_data(data)

    def test_reward_in_task_data_rejected(self):
        data = _valid_task_data()
        data["reward"] = 999
        with pytest.raises(ManualTaskDataError,
                           match="reward must not appear"):
            validate_manual_task_data(data)

    @pytest.mark.parametrize("bad", [None, [], "x", 5, True])
    def test_non_object_rejected(self, bad):
        with pytest.raises(ManualTaskDataError, match="JSON object"):
            validate_manual_task_data(bad)

    def test_parse_valid_string(self):
        parsed = parse_manual_task_data(json.dumps(_valid_task_data()))
        assert parsed["action"] == "proof"

    @pytest.mark.parametrize("raw,needle", [
        (None, "missing"),
        ("", "missing"),
        ("   ", "missing"),
        (123, "JSON string"),
        ("{not json", "not valid JSON"),
        (json.dumps({"provider": "telegram"}), "action"),
    ])
    def test_parse_rejections(self, raw, needle):
        with pytest.raises(ManualTaskDataError, match=needle):
            parse_manual_task_data(raw)

    def test_approver_accessor_reads_definition(self, env):
        task = db.get_task(env["task_id"])
        assert manual_task_approver_user_id(task) == APPROVER

    def test_approver_accessor_none_on_invalid(self):
        assert manual_task_approver_user_id(None) is None
        assert manual_task_approver_user_id({"task_data": None}) is None
        assert manual_task_approver_user_id({"task_data": "{}"}) is None
        assert manual_task_approver_user_id(
            {"task_data": "not json"}) is None

    def test_awaiting_accessor_lifecycle(self, env):
        tid = env["task_id"]
        assert worker_awaiting_decision(WORKER, tid) is False
        _start(WORKER, tid)
        ManualProofService.submit(WORKER, tid, PROOF, "k1")
        assert worker_awaiting_decision(WORKER, tid) is True
        sid = TaskSubmissionStore.get_latest_claim(
            WORKER, tid).submission_id
        ManualReviewService.decide(APPROVER, tid, sid, False)
        assert worker_awaiting_decision(WORKER, tid) is False


# ══════════════════════════════════════════════════════════════════
# Proof — bounded text/URL reference only
# ══════════════════════════════════════════════════════════════════


class TestProofBounds:
    @pytest.mark.parametrize("bad", [
        None, 42, 3.5, True, [], {}, ["https://x"], (),
        "", "   ", "\t", "\n", "\r",
    ])
    def test_invalid_proof_rejected(self, env, bad):
        tid = env["task_id"]
        _start(WORKER, tid)
        with pytest.raises(ManualProofError, match="proof_ref"):
            ManualProofService.submit(WORKER, tid, bad, "k1")
        # No claim was opened for an invalid proof.
        assert _claim_rows(tid) == []

    @pytest.mark.parametrize("text", ["a\nb", "a\x00b", "a\rb", "a\tb"])
    def test_control_characters_rejected(self, env, text):
        tid = env["task_id"]
        _start(WORKER, tid)
        with pytest.raises(ManualProofError,
                           match="control characters"):
            ManualProofService.submit(WORKER, tid, text, "k1")
        assert _claim_rows(tid) == []

    def test_normalize_returns_stripped(self):
        assert normalize_proof_ref("  abc  ") == "abc"

    def test_max_length_accepted(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        proof = "x" * MAX_PROOF_REF_LENGTH
        out = ManualProofService.submit(WORKER, tid, proof, "k1")
        assert out.state == "pending"
        rec = TaskSubmissionStore.get_latest_claim(WORKER, tid)
        assert rec.proof_ref == proof

    def test_over_length_rejected(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        with pytest.raises(ManualProofError, match="exceeds"):
            ManualProofService.submit(
                WORKER, tid, "x" * (MAX_PROOF_REF_LENGTH + 1), "k1")
        assert _claim_rows(tid) == []

    def test_proof_stripped_on_store(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        ManualProofService.submit(WORKER, tid,
                                  "  https://t.me/x  ", "k1")
        rec = TaskSubmissionStore.get_latest_claim(WORKER, tid)
        assert rec.proof_ref == "https://t.me/x"

    def test_plain_text_reference_allowed(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        out = ManualProofService.submit(
            WORKER, tid, "شاهدت المنشور في مجموعة الاختبار", "k1")
        assert out.state == "pending"

    # ── Route-level proof validation ────────────────────────────

    def test_route_missing_proof_400(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        resp = client.post(f"/api/tasks/{tid}/submit",
                           headers=_auth(WORKER), json={})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_proof"
        assert _claim_rows(tid) == []

    @pytest.mark.parametrize("bad", [42, None, [], {}, True])
    def test_route_wrong_proof_type_400(self, client, env, bad):
        tid = env["task_id"]
        _start(WORKER, tid)
        resp = client.post(f"/api/tasks/{tid}/submit",
                           headers=_auth(WORKER),
                           json={"proof_ref": bad})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_proof"
        assert _claim_rows(tid) == []

    def test_route_empty_proof_400(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        resp = client.post(f"/api/tasks/{tid}/submit",
                           headers=_auth(WORKER),
                           json={"proof_ref": "   "})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_proof"

    def test_route_overlong_proof_400(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        resp = client.post(f"/api/tasks/{tid}/submit",
                           headers=_auth(WORKER),
                           json={"proof_ref": "x" * 501})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_proof"


# ══════════════════════════════════════════════════════════════════
# Submission — one pending claim per (user, task)
# ══════════════════════════════════════════════════════════════════


class TestSubmissionFlow:
    def test_submit_opens_pending_claim_with_proof(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        out = ManualProofService.submit(WORKER, tid, PROOF, "k1")
        assert out.state == "pending"
        assert out.submission_id > 0
        rec = TaskSubmissionStore.get_submission(out.submission_id)
        assert rec.user_id == WORKER
        assert rec.task_id == tid
        assert rec.status == db.SUBMISSION_STATUS_SUBMITTED
        assert rec.approval_status == db.SUBMISSION_APPROVAL_PENDING
        assert rec.proof_ref == PROOF
        assert rec.verification_reason is None
        # No completion, no reward, user_task untouched.
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_STARTED
        assert _wallet_units(WORKER) == 0
        assert _task_credits(WORKER) == []

    def test_same_key_resolves_same_claim(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        first = ManualProofService.submit(WORKER, tid, PROOF, "k1")
        second = ManualProofService.submit(WORKER, tid, PROOF, "k1")
        assert second.submission_id == first.submission_id
        assert len(_claim_rows(tid)) == 1

    def test_single_pending_claim_per_user_task(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        first = ManualProofService.submit(
            WORKER, tid, "https://proof.one", "key-1")
        second = ManualProofService.submit(
            WORKER, tid, "https://proof.two", "key-2")
        # A second key while a claim is open resolves the SAME claim —
        # at most ONE pending claim per (user, task) ever.
        assert second.submission_id == first.submission_id
        pending = TaskSubmissionStore.list_pending_claims_for_task(tid)
        assert len(pending) == 1
        # The original proof wins; the loser never overwrites it.
        assert pending[0].proof_ref == "https://proof.one"
        assert len(_claim_rows(tid)) == 1

    def test_concurrent_submits_create_one_pending_claim(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)

        def _try(key: str):
            return ManualProofService.submit(
                WORKER, tid, f"https://proof/{key}", key)

        keys = [f"race-{i}" for i in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(_try, keys))
        assert all(o.state == "pending" for o in outcomes)
        assert len({o.submission_id for o in outcomes}) == 1
        assert len(
            TaskSubmissionStore.list_pending_claims_for_task(tid)) == 1
        assert len(_claim_rows(tid)) == 1

    def test_submit_requires_started(self, env):
        with pytest.raises(ManualProofError,
                           match="No user_task record"):
            ManualProofService.submit(
                WORKER, env["task_id"], PROOF, "k1")

    def test_submit_rejected_after_completion(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        sid = ManualProofService.submit(
            WORKER, tid, PROOF, "k1").submission_id
        ManualReviewService.decide(APPROVER, tid, sid, True)
        with pytest.raises(ManualProofError,
                           match="not in started state"):
            ManualProofService.submit(WORKER, tid, PROOF, "k2")

    def test_inactive_task_rejected(self, env):
        tid = _make_task(_valid_task_data(), active=False)
        with pytest.raises(ManualProofError, match="not active"):
            ManualProofService.submit(WORKER, tid, PROOF, "k1")

    def test_wrong_type_rejected(self, env):
        tid = _make_task({"channel_slug": "x"},
                         task_type="deterministic")
        with pytest.raises(ManualProofError,
                           match="not a manual task"):
            ManualProofService.submit(WORKER, tid, PROOF, "k1")

    def test_unknown_task_rejected(self, env):
        with pytest.raises(ManualProofError, match="not found"):
            ManualProofService.submit(WORKER, 999999, PROOF, "k1")

    def test_invalid_definition_rejected(self, env):
        tid = _make_task({"provider": "nope"})
        _start(WORKER, tid)
        with pytest.raises(ManualProofError,
                           match="invalid manual task definition"):
            ManualProofService.submit(WORKER, tid, PROOF, "k1")

    def test_missing_task_data_rejected(self, env):
        tid = _make_task(None)
        _start(WORKER, tid)
        with pytest.raises(ManualProofError,
                           match="invalid manual task definition"):
            ManualProofService.submit(WORKER, tid, PROOF, "k1")

    def test_approver_cannot_submit_own_task(self, env):
        tid = env["task_id"]
        _start(APPROVER, tid)
        with pytest.raises(ManualProofError,
                           match="own manual task"):
            ManualProofService.submit(APPROVER, tid, PROOF, "k1")
        assert _claim_rows(tid, user_id=APPROVER) == []

    def test_invalid_idempotency_key_rejected(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        with pytest.raises(ManualProofError,
                           match="invalid idempotency key"):
            ManualProofService.submit(WORKER, tid, PROOF, "bad key!")


# ══════════════════════════════════════════════════════════════════
# Reviewer authorization — task_data.approver.telegram_user_id only
# ══════════════════════════════════════════════════════════════════


class TestAuthorization:
    def _open(self, env) -> int:
        tid = env["task_id"]
        _start(WORKER, tid)
        return ManualProofService.submit(
            WORKER, tid, PROOF, "k1").submission_id

    def test_arbitrary_authenticated_user_rejected(self, env):
        sid = self._open(env)
        with pytest.raises(ManualDecisionError,
                           match="authorized approver"):
            ManualReviewService.decide(STRANGER, env["task_id"],
                                       sid, True)
        assert _claim_row(sid)["approval_status"] == (
            db.SUBMISSION_APPROVAL_PENDING)
        assert _wallet_units(WORKER) == 0

    def test_worker_cannot_decide_own_claim(self, env):
        sid = self._open(env)
        with pytest.raises(ManualDecisionError,
                           match="authorized approver"):
            ManualReviewService.decide(WORKER, env["task_id"],
                                       sid, True)
        assert _claim_row(sid)["approval_status"] == (
            db.SUBMISSION_APPROVAL_PENDING)

    def test_no_admin_fallback_in_source(self):
        source = open("manual_task.py", encoding="utf-8").read()
        assert "is_admin" not in source
        source = open("task_routes.py", encoding="utf-8").read()
        assert "is_admin" not in source

    def test_authorized_approver_approves(self, env):
        tid = env["task_id"]
        sid = self._open(env)
        out = ManualReviewService.decide(APPROVER, tid, sid, True)
        assert out.state == "approved"
        row = _claim_row(sid)
        assert row["approval_status"] == db.SUBMISSION_APPROVAL_APPROVED
        assert row["approver_user_id"] == APPROVER
        assert row["status"] == db.SUBMISSION_STATUS_PASSED

    def test_decision_on_foreign_task_claim_rejected(self, env):
        tid = env["task_id"]
        sid = self._open(env)
        other = _make_task(_valid_task_data())
        with pytest.raises(ManualDecisionError, match="claim not found"):
            ManualReviewService.decide(APPROVER, other, sid, True)
        assert _claim_row(sid)["approval_status"] == (
            db.SUBMISSION_APPROVAL_PENDING)

    def test_unknown_claim_rejected(self, env):
        with pytest.raises(ManualDecisionError, match="claim not found"):
            ManualReviewService.decide(APPROVER, env["task_id"],
                                       999999, True)

    def test_conflicting_decision_rejected(self, env):
        tid = env["task_id"]
        sid = self._open(env)
        ManualReviewService.decide(APPROVER, tid, sid, True)
        with pytest.raises(ManualDecisionError,
                           match="already approved"):
            ManualReviewService.decide(APPROVER, tid, sid, False)
        assert _wallet_units(WORKER) == REWARD_UNITS

    def test_conflicting_reverse_decision_rejected(self, env):
        tid = env["task_id"]
        sid = self._open(env)
        ManualReviewService.decide(APPROVER, tid, sid, False)
        with pytest.raises(ManualDecisionError,
                           match="already rejected"):
            ManualReviewService.decide(APPROVER, tid, sid, True)
        assert _wallet_units(WORKER) == 0


# ══════════════════════════════════════════════════════════════════
# Approval — completion + exactly-once reward settlement
# ══════════════════════════════════════════════════════════════════


class TestApprovalFlow:
    def _open(self, env) -> int:
        tid = env["task_id"]
        _start(WORKER, tid)
        return ManualProofService.submit(
            WORKER, tid, PROOF, "k1").submission_id

    def test_approval_completes_task(self, env):
        tid = env["task_id"]
        sid = self._open(env)
        ManualReviewService.decide(APPROVER, tid, sid, True)
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_COMPLETED
        row = db.get_user_task(WORKER, tid)
        assert row["completed_at"] is not None
        assert _claim_row(sid)["completed_at"] is not None

    def test_approval_credits_exactly_once(self, env):
        tid = env["task_id"]
        sid = self._open(env)
        ManualReviewService.decide(APPROVER, tid, sid, True)
        assert _wallet_units(WORKER) == REWARD_UNITS
        assert len(_task_credits(WORKER)) == 1
        assert _task_credits(WORKER)[0]["amount_units"] == REWARD_UNITS

    def test_repeated_approval_idempotent(self, env):
        tid = env["task_id"]
        sid = self._open(env)
        first = ManualReviewService.decide(APPROVER, tid, sid, True)
        second = ManualReviewService.decide(APPROVER, tid, sid, True)
        assert first.state == "approved"
        assert second.state == "approved"
        # Never a second completion and never a second credit.
        assert _wallet_units(WORKER) == REWARD_UNITS
        assert len(_task_credits(WORKER)) == 1
        assert len(_claim_rows(tid)) == 1

    def test_route_approver_approves(self, client, env):
        tid = env["task_id"]
        sid = self._open(env)
        resp = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(APPROVER), json={"decision": "approve"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["approval"] == "approved"
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_COMPLETED
        assert _wallet_units(WORKER) == REWARD_UNITS
        assert len(_task_credits(WORKER)) == 1

    def test_route_repeated_approve_stays_one_credit(self, client, env):
        tid = env["task_id"]
        sid = self._open(env)
        for _ in range(2):
            resp = client.post(
                f"/api/tasks/{tid}/claims/{sid}/decision",
                headers=_auth(APPROVER), json={"decision": "approve"})
            assert resp.status_code == 200
        assert _wallet_units(WORKER) == REWARD_UNITS
        assert len(_task_credits(WORKER)) == 1

    def test_route_conflict_409(self, client, env):
        tid = env["task_id"]
        sid = self._open(env)
        first = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(APPROVER), json={"decision": "approve"})
        assert first.status_code == 200
        conflict = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(APPROVER), json={"decision": "reject"})
        assert conflict.status_code == 409
        assert conflict.get_json()["error"] == "claim_already_approved"
        assert len(_task_credits(WORKER)) == 1


# ══════════════════════════════════════════════════════════════════
# Rejection — durable, task stays started, retry allowed
# ══════════════════════════════════════════════════════════════════


class TestRejectionFlow:
    def _open(self, env, key: str = "k1",
              proof: str = PROOF) -> int:
        tid = env["task_id"]
        _start(WORKER, tid)
        return ManualProofService.submit(
            WORKER, tid, proof, key).submission_id

    def test_rejection_leaves_task_started(self, env):
        tid = env["task_id"]
        sid = self._open(env)
        out = ManualReviewService.decide(APPROVER, tid, sid, False)
        assert out.state == "rejected"
        # user_task stays started — no completion, no reward.
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_STARTED
        row = db.get_user_task(WORKER, tid)
        assert row["completed_at"] is None
        claim = _claim_row(sid)
        assert claim["status"] == db.SUBMISSION_STATUS_FAILED
        assert claim["approval_status"] == (
            db.SUBMISSION_APPROVAL_REJECTED)
        assert claim["approver_user_id"] == APPROVER
        assert claim["completed_at"] is None
        assert _wallet_units(WORKER) == 0
        assert _task_credits(WORKER) == []

    def test_rejected_task_can_submit_new_proof(self, env):
        tid = env["task_id"]
        sid1 = self._open(env, key="k1", proof="https://proof.one")
        ManualReviewService.decide(APPROVER, tid, sid1, False)

        out2 = ManualProofService.submit(
            WORKER, tid, "https://proof.two", "k2")
        assert out2.state == "pending"
        assert out2.submission_id != sid1
        # The rejected claim is a durable audit row, not deleted.
        assert len(_claim_rows(tid)) == 2
        assert _claim_row(sid1)["status"] == db.SUBMISSION_STATUS_FAILED
        assert len(
            TaskSubmissionStore.list_pending_claims_for_task(tid)) == 1

        # Approving the retry completes it — exactly one credit ever.
        ManualReviewService.decide(
            APPROVER, tid, out2.submission_id, True)
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_COMPLETED
        assert _wallet_units(WORKER) == REWARD_UNITS
        assert len(_task_credits(WORKER)) == 1

    def test_route_reject_flow(self, client, env):
        tid = env["task_id"]
        sid = self._open(env)
        resp = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(APPROVER), json={"decision": "reject"})
        assert resp.status_code == 200
        assert resp.get_json()["approval"] == "rejected"
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_STARTED
        assert _wallet_units(WORKER) == 0

    def test_route_retry_after_rejection(self, client, env):
        tid = env["task_id"]
        sid = self._open(env)
        client.post(f"/api/tasks/{tid}/claims/{sid}/decision",
                    headers=_auth(APPROVER),
                    json={"decision": "reject"})
        resp = client.post(f"/api/tasks/{tid}/submit",
                           headers=_auth(WORKER),
                           json={"proof_ref": "https://retry"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["approval"] == "pending"
        assert data["awaiting_decision"] is True
        assert data["status"] == db.USER_TASK_STATUS_STARTED


# ══════════════════════════════════════════════════════════════════
# Security — proof can never override identity / task / reward
# ══════════════════════════════════════════════════════════════════


class TestProofCannotOverride:
    @pytest.mark.parametrize("field", sorted(FORBIDDEN_FIELDS))
    def test_forbidden_body_fields_rejected(self, client, env, field):
        tid = env["task_id"]
        _start(WORKER, tid)
        resp = client.post(f"/api/tasks/{tid}/submit",
                           headers=_auth(WORKER),
                           json={"proof_ref": PROOF, field: "x"})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_submission"
        assert _claim_rows(tid) == []
        assert _wallet_units(WORKER) == 0

    def test_unknown_approval_shaped_fields_ignored(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        resp = client.post(
            f"/api/tasks/{tid}/submit",
            headers=_auth(WORKER),
            json={"proof_ref": PROOF, "approved": True,
                  "verified": True, "approver": OTHER},
        )
        assert resp.status_code == 200
        assert resp.get_json()["approval"] == "pending"
        rec = TaskSubmissionStore.get_latest_claim(WORKER, tid)
        assert rec.user_id == WORKER
        assert rec.task_id == tid
        # Server-side definition and reward untouched.
        task = db.get_task(tid)
        assert task["reward"] == REWARD_USDT
        assert json.loads(task["task_data"]) == _valid_task_data()
        assert _wallet_units(WORKER) == 0
        assert _task_credits(WORKER) == []

    def test_proof_text_cannot_impersonate(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        proof = json.dumps(
            {"user_id": OTHER, "reward": 999,
             "approver": OTHER, "task_id": 999999})
        out = ManualProofService.submit(WORKER, tid, proof, "k1")
        assert out.state == "pending"
        rec = TaskSubmissionStore.get_submission(out.submission_id)
        # Stored as an opaque reference — identity stays server-side.
        assert rec.user_id == WORKER
        assert rec.task_id == tid
        ManualReviewService.decide(APPROVER, tid,
                                   out.submission_id, True)
        assert _wallet_units(WORKER) == REWARD_UNITS
        assert _wallet_units(OTHER) == 0
        assert _task_credits(OTHER) == []

    def test_identity_comes_from_initdata_not_body(self, client, env):
        tid = env["task_id"]
        _start(STRANGER, tid)
        resp = client.post(
            f"/api/tasks/{tid}/submit",
            headers=_auth(STRANGER),
            json={"proof_ref": PROOF, "user_id": APPROVER},
        )
        assert resp.status_code == 400  # user_id is forbidden
        resp = client.post(f"/api/tasks/{tid}/submit",
                           headers=_auth(STRANGER),
                           json={"proof_ref": PROOF})
        assert resp.status_code == 200
        rec = TaskSubmissionStore.get_latest_claim(STRANGER, tid)
        assert rec.user_id == STRANGER


# ══════════════════════════════════════════════════════════════════
# Routes — dispatch, awaiting-decision state, claims, decisions
# ══════════════════════════════════════════════════════════════════


class TestAwaitingDecisionState:
    def test_route_submit_opens_pending(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        resp = client.post(f"/api/tasks/{tid}/submit",
                           headers=_auth(WORKER),
                           json={"proof_ref": PROOF})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["approval"] == "pending"
        assert data["awaiting_decision"] is True
        assert data["status"] == db.USER_TASK_STATUS_STARTED
        assert data["message"]  # Arabic, user-facing
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_STARTED
        assert _wallet_units(WORKER) == 0

    def test_awaiting_true_while_pending(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        ManualProofService.submit(WORKER, tid, PROOF, "k1")
        resp = client.get("/api/tasks", headers=_auth(WORKER))
        tasks = {t["id"]: t for t in resp.get_json()["tasks"]}
        assert tasks[tid]["awaiting_decision"] is True
        assert tasks[tid]["status"] == db.USER_TASK_STATUS_STARTED

    def test_awaiting_false_without_claim(self, client, env):
        tid = env["task_id"]
        resp = client.get("/api/tasks", headers=_auth(WORKER))
        tasks = {t["id"]: t for t in resp.get_json()["tasks"]}
        assert tasks[tid]["awaiting_decision"] is False

    def test_awaiting_false_after_decision(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        sid = ManualProofService.submit(
            WORKER, tid, PROOF, "k1").submission_id
        ManualReviewService.decide(APPROVER, tid, sid, True)
        resp = client.get("/api/tasks", headers=_auth(WORKER))
        tasks = {t["id"]: t for t in resp.get_json()["tasks"]}
        assert tasks[tid]["awaiting_decision"] is False
        assert tasks[tid]["status"] == db.USER_TASK_STATUS_COMPLETED

    def test_non_manual_tasks_have_no_boolean(self, client, env):
        _make_task({"expected": "x"}, task_type="deterministic")
        resp = client.get("/api/tasks", headers=_auth(WORKER))
        for entry in resp.get_json()["tasks"]:
            if entry["type"] != MANUAL_TASK_TYPE:
                assert "awaiting_decision" not in entry

    def test_unauthenticated_submit_rejected(self, client, env):
        resp = client.post(f"/api/tasks/{env['task_id']}/submit")
        assert resp.status_code == 401


class TestClaimsRoute:
    def test_approver_lists_claims_with_proof(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        ManualProofService.submit(WORKER, tid, PROOF, "k1")
        resp = client.get(f"/api/tasks/{tid}/claims",
                          headers=_auth(APPROVER))
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["claims"]) == 1
        claim = data["claims"][0]
        # Safe review fields ONLY (MT-TASK-16): claim id, task id,
        # submitted_at, proof_ref — no worker identity.
        assert set(claim.keys()) == {
            "claim_id", "task_id", "submitted_at", "proof_ref"}
        assert claim["task_id"] == tid
        assert claim["proof_ref"] == PROOF
        assert "user_id" not in claim

    def test_worker_cannot_list_claims(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        ManualProofService.submit(WORKER, tid, PROOF, "k1")
        resp = client.get(f"/api/tasks/{tid}/claims",
                          headers=_auth(WORKER))
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "not_approver"

    def test_stranger_cannot_list_claims(self, client, env):
        resp = client.get(f"/api/tasks/{env['task_id']}/claims",
                          headers=_auth(STRANGER))
        assert resp.status_code == 403

    def test_unauthenticated_list_rejected(self, client, env):
        resp = client.get(f"/api/tasks/{env['task_id']}/claims")
        assert resp.status_code == 401

    def test_non_manual_task_rejected(self, client, env):
        other = _make_task({"expected": "x"},
                           task_type="deterministic")
        resp = client.get(f"/api/tasks/{other}/claims",
                          headers=_auth(APPROVER))
        assert resp.status_code == 409

    def test_unknown_task_404(self, client, env):
        resp = client.get("/api/tasks/424242/claims",
                          headers=_auth(APPROVER))
        assert resp.status_code == 404

    def test_empty_after_decision(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        sid = ManualProofService.submit(
            WORKER, tid, PROOF, "k1").submission_id
        ManualReviewService.decide(APPROVER, tid, sid, True)
        resp = client.get(f"/api/tasks/{tid}/claims",
                          headers=_auth(APPROVER))
        assert resp.get_json()["claims"] == []


class TestDecisionRoute:
    def _open(self, env) -> int:
        tid = env["task_id"]
        _start(WORKER, tid)
        return ManualProofService.submit(
            WORKER, tid, PROOF, "k1").submission_id

    def test_worker_cannot_decide_via_api(self, client, env):
        tid = env["task_id"]
        sid = self._open(env)
        resp = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(WORKER), json={"decision": "approve"})
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "not_approver"
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_STARTED
        assert _wallet_units(WORKER) == 0

    def test_impersonation_body_ignored_identity_decides(self,
                                                         client, env):
        tid = env["task_id"]
        sid = self._open(env)
        resp = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(STRANGER),
            json={"decision": "approve", "user_id": APPROVER,
                  "approver": APPROVER},
        )
        assert resp.status_code == 403
        assert _wallet_units(WORKER) == 0

    def test_unauthenticated_decision_rejected(self, client, env):
        tid = env["task_id"]
        sid = self._open(env)
        resp = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            json={"decision": "approve"})
        assert resp.status_code == 401

    @pytest.mark.parametrize("body", [
        {}, {"decision": "force"}, {"decision": "approved"},
        {"decision": True}, {"decision": 1}, {"decision": None},
        {"decision": ""},
    ])
    def test_invalid_decision_value_400(self, client, env, body):
        tid = env["task_id"]
        sid = self._open(env)
        resp = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(APPROVER), json=body)
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_decision"
        assert _claim_row(sid)["approval_status"] == (
            db.SUBMISSION_APPROVAL_PENDING)

    def test_non_object_json_body_400(self, client, env):
        tid = env["task_id"]
        sid = self._open(env)
        resp = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(APPROVER), json=["approve"])
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_request"

    def test_unknown_claim_404(self, client, env):
        resp = client.post(
            f"/api/tasks/{env['task_id']}/claims/999999/decision",
            headers=_auth(APPROVER), json={"decision": "approve"})
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════
# Regressions — other task families are untouched by the dispatch
# ══════════════════════════════════════════════════════════════════


def _assert_not_manual_path(client, env, tid, auth_user=WORKER):
    """A non-manual task never enters the manual approval path."""
    resp = client.post(f"/api/tasks/{tid}/submit",
                       headers=_auth(auth_user),
                       json={"proof_ref": PROOF})
    data = resp.get_json()
    assert "approval" not in data
    for rec in TaskSubmissionStore.list_user_task_submissions(
            auth_user, tid):
        assert rec.approval_status is None
        assert rec.proof_ref is None
    return resp


class TestRegressions:
    def test_telegram_channel_not_dispatched(self, client, env):
        tid = _make_task(_valid_task_data(),
                         task_type="telegram_channel")
        _start(WORKER, tid)
        resp = _assert_not_manual_path(client, env, tid)
        # ERROR (invalid telegram_channel definition) — NOT the
        # manual approval dispatch and NOT a verifier contract change.
        assert resp.status_code in (200, 409, 502)

    def test_channel_subscription_not_dispatched(self, client, env):
        tid = _make_task({"channel_slug": "nonexistent"},
                         task_type="channel_subscription")
        _start(WORKER, tid)
        resp = _assert_not_manual_path(client, env, tid)
        assert resp.status_code in (200, 409, 502)

    def test_referral_dispatch_unchanged(self, client, env):
        tid = _make_task(
            {
                "provider": "telegram",
                "action": "referral",
                "target": {"bot_username": "my_task_bot"},
                "approver": {"telegram_user_id": APPROVER},
            },
            task_type=REFERRAL_TASK_TYPE,
        )
        _start(WORKER, tid)
        resp = client.post(f"/api/tasks/{tid}/submit",
                           headers=_auth(WORKER))
        assert resp.status_code == 200
        assert resp.get_json()["approval"] == "pending"

        # Referral claims never write a proof reference.
        claim = TaskSubmissionStore.get_latest_claim(WORKER, tid)
        assert claim.proof_ref is None

        # The buyer's claims view is unchanged: no proof_ref key.
        resp = client.get(f"/api/tasks/{tid}/claims",
                          headers=_auth(APPROVER))
        assert resp.status_code == 200
        claim = resp.get_json()["claims"][0]
        assert set(claim.keys()) == {"claim_id", "submitted_at"}

        # The referral decision path still completes the task.
        sid = TaskSubmissionStore.get_latest_claim(
            WORKER, tid).submission_id
        resp = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(APPROVER), json={"decision": "approve"})
        assert resp.status_code == 200
        assert resp.get_json()["approval"] == "approved"
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_COMPLETED
        assert len(_task_credits(WORKER)) == 1
