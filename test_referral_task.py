"""
Focused tests — paid referral task family (MT-TASK-06)
======================================================

Adapts the old repo's buyer-mediated Paid Referral Task into the new
Task architecture, covered end to end:

Task definition (server-side task_data contract)
- referral_task is a distinct task type
- valid task_data validates (provider/action/target/approver)
- invalid provider / action / target / approver rejected
- unsafe bot_username (URLs, @handles, numeric ids) rejected
- reward inside task_data rejected (reward lives in tasks.reward)

Referral identity (existing users.referred_by only)
- valid existing referral → claimable
- no referral → claim rejected
- self-referral row → claim rejected
- client cannot override referral identity
- first-referrer-wins registration remains intact

Submission (claim = a task_submissions row born approval-pending)
- claim persisted with worker/task/key/timestamps
- same idempotency key → same claim (no duplicate)
- different key while a claim is open → duplicate prevented
- submission does NOT complete the task
- submission does NOT credit any reward
- claim pre-conditions (started only, referral type only, active
  definition, valid definition, buyer cannot work own task)

Approval (server-authorized buyer decision only)
- unauthorized actor cannot approve/reject
- worker cannot decide their own claim
- authorized buyer approves → persisted decision, exactly one
  completion, exactly one TaskRewardService credit
- repeated approval is idempotent (never a second credit)
- rejected claim → zero reward, task stays started, durable rejection
- rejected claim cannot be approved later (conflict)
- retry after rejection opens a new claim → at most one reward ever

Security (malicious payloads)
- worker cannot force approval / reward / identity / completion
- forbidden fields rejected; unknown approval-shaped fields ignored
- identity comes from initData, never from body (impersonation fails)

API (minimal extension)
- GET /api/tasks exposes only a narrow own-state boolean for referral
  tasks (no task_data, no approval vocabulary, no other identities)
- worker GET claims → 403, buyer GET claims → safe fields only
- decision endpoint: 401/403/404/400/409 mappings, approve/reject
- worker page has no decision capability (server enforces; the page
  JS is guarded by test_miniapp_tasks_page's word bans)

Repeat
- one_time referral tasks work (terminal after completion)
- repeatable referral definitions are rejected server-side (no safe
  cycle identity exists in the current data model — reported, not
  guessed)
- the same referral identity can never yield unlimited rewards

No real Telegram API call is ever made; no complaint system is
invented; quantity is not faked.

Run:
    python3 -m pytest test_referral_task.py -v
"""

from __future__ import annotations

import json

import pytest

import db
import serve_miniapp
import wallet
from referral_task import (
    REFERRAL_TASK_TYPE,
    ReferralApprovalService,
    ReferralClaimError,
    ReferralClaimService,
    ReferralDecisionError,
    referral_identity,
    task_approver_user_id,
    validate_referral_task_data,
    ReferralTaskDataError,
    parse_referral_task_data,
    worker_claim_state,
)
from task_lifecycle import TaskLifecycle
from task_start import StartGateError, TaskStartGate
from task_submission import FORBIDDEN_FIELDS
from task_submission_store import TaskSubmissionStore

# Reuse the existing, independently implemented Telegram initData helper.
from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

BUYER = 5001
WORKER = 6002
REFERRED = 7003   # referred_by = WORKER (genuine referral)
STRANGER = 8004   # no referral attribution
OTHER = 9005

REWARD_USDT = 50
REWARD_UNITS = REWARD_USDT * wallet.USDT_SCALE  # 5,000,000,000

BOT_USERNAME = "my_task_bot"


# ── Contract helpers ──────────────────────────────────────────────


def _valid_task_data(bot_username: str = BOT_USERNAME,
                     approver: int = BUYER) -> dict:
    """A contract-conformant referral_task task_data payload."""
    return {
        "provider": "telegram",
        "action": "referral",
        "target": {"bot_username": bot_username},
        "approver": {"telegram_user_id": approver},
    }


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Temp DB, users, a genuine referral, and one valid referral task."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    db_path = str(tmp_path / "referral_task_test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)

    db.register_user(BUYER, "buyer", "Buyer")
    db.register_user(WORKER, "worker", "Worker")
    db.register_user(STRANGER, "stranger", "Stranger")
    db.register_user(OTHER, "other", "Other")
    # Genuine referral established through the existing registration
    # mechanism (first-referrer-wins, self-referral block untouched).
    db.register_user(REFERRED, "referred", "Referred", referred_by=WORKER)

    task_id = db.create_task(
        title="مهمة إحالة مدفوعة",
        description="أحضر مستخدمًا جديدًا واحصل على الموافقة",
        task_type=REFERRAL_TASK_TYPE,
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
            "       approver_user_id, approval_decided_at, "
            "       idempotency_key, completed_at, verification_reason "
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
        title="Extra Referral Task",
        description="Extra",
        task_type=task_type or REFERRAL_TASK_TYPE,
        reward=reward,
        task_data=raw,
        **kwargs,
    )


# ══════════════════════════════════════════════════════════════════
# Task definition — server-side task_data contract
# ══════════════════════════════════════════════════════════════════


class TestTaskDataContract:
    def test_task_type_is_distinct(self, env):
        assert REFERRAL_TASK_TYPE == "referral_task"
        assert REFERRAL_TASK_TYPE != "telegram_channel"
        assert REFERRAL_TASK_TYPE != "channel_subscription"
        assert db.get_task(env["task_id"])["type"] == REFERRAL_TASK_TYPE

    def test_valid_contract_validates(self):
        data = _valid_task_data()
        assert validate_referral_task_data(data) is data

    # ── provider / action ───────────────────────────────────────

    @pytest.mark.parametrize("mutate,needle", [
        (lambda d: d.pop("provider"), "provider"),
        (lambda d: d.update(provider="web"), "provider"),
        (lambda d: d.pop("action"), "action"),
        (lambda d: d.update(action="join_channel"), "action"),
    ])
    def test_provider_action_violations_rejected(self, mutate, needle):
        data = _valid_task_data()
        mutate(data)
        with pytest.raises(ReferralTaskDataError, match=needle):
            validate_referral_task_data(data)

    # ── target ──────────────────────────────────────────────────

    @pytest.mark.parametrize("mutate,needle", [
        (lambda d: d.pop("target"), "target"),
        (lambda d: d.update(target="somebot"), "target"),
        (lambda d: d.update(target={}), "bot_username"),
        (lambda d: d["target"].pop("bot_username"), "bot_username"),
        (lambda d: d["target"].update(bot_username=123), "bot_username"),
        (lambda d: d["target"].update(bot_username="https://evil.example/bot"),
         "bot_username"),
        (lambda d: d["target"].update(bot_username="@mybot"), "bot_username"),
        (lambda d: d["target"].update(bot_username="-100123456"),
         "bot_username"),
        (lambda d: d["target"].update(channel_slug="main"), "target keys"),
    ])
    def test_target_violations_rejected(self, mutate, needle):
        data = _valid_task_data()
        mutate(data)
        with pytest.raises(ReferralTaskDataError, match=needle):
            validate_referral_task_data(data)

    # ── approver (buyer identity) ───────────────────────────────

    @pytest.mark.parametrize("mutate,needle", [
        (lambda d: d.pop("approver"), "approver"),
        (lambda d: d.update(approver=BUYER), "approver"),
        (lambda d: d["approver"].pop("telegram_user_id"), "approver"),
        (lambda d: d["approver"].update(telegram_user_id="5001"),
         "integer"),
        (lambda d: d["approver"].update(telegram_user_id=True),
         "integer"),
        (lambda d: d["approver"].update(telegram_user_id=0), "positive"),
        (lambda d: d["approver"].update(telegram_user_id=-1), "positive"),
        (lambda d: d["approver"].update(username="buyer"), "approver keys"),
    ])
    def test_approver_violations_rejected(self, mutate, needle):
        data = _valid_task_data()
        mutate(data)
        with pytest.raises(ReferralTaskDataError, match=needle):
            validate_referral_task_data(data)

    # ── reward / unexpected keys ────────────────────────────────

    def test_reward_inside_task_data_rejected(self):
        data = _valid_task_data()
        data["reward"] = 999999
        with pytest.raises(ReferralTaskDataError, match="reward"):
            validate_referral_task_data(data)

    def test_unexpected_key_rejected(self):
        data = _valid_task_data()
        data["quantity"] = 10
        with pytest.raises(ReferralTaskDataError, match="unexpected"):
            validate_referral_task_data(data)

    @pytest.mark.parametrize("payload", [None, "text", 42, ["ref"], []])
    def test_non_object_payload_rejected(self, payload):
        with pytest.raises(ReferralTaskDataError):
            validate_referral_task_data(payload)

    # ── raw parsing / accessor ──────────────────────────────────

    @pytest.mark.parametrize("raw", [None, "   ", "{not json"])
    def test_parse_bad_raw_rejected(self, raw):
        with pytest.raises(ReferralTaskDataError):
            parse_referral_task_data(raw)

    def test_task_approver_accessor(self):
        task = {"task_data": json.dumps(_valid_task_data())}
        assert task_approver_user_id(task) == BUYER

    @pytest.mark.parametrize("raw", [None, "", "{not json", "[]"])
    def test_task_approver_none_on_unusable(self, raw):
        assert task_approver_user_id({"task_data": raw}) is None
        assert task_approver_user_id(None) is None


# ══════════════════════════════════════════════════════════════════
# Referral identity — existing users.referred_by only
# ══════════════════════════════════════════════════════════════════


class TestReferralIdentity:
    def test_valid_existing_referral(self, env):
        assert referral_identity(WORKER) == (1, 0)

    def test_no_referral(self, env):
        assert referral_identity(STRANGER) == (0, 0)

    def test_self_referral_row_not_counted(self, env):
        # A crafted self-attributed row can never act as a referral.
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE users SET referred_by = user_id WHERE user_id = ?",
                (STRANGER,),
            )
        assert referral_identity(STRANGER) == (0, 1)

    def test_registration_blocks_self_referral(self, env):
        db.register_user(11111, "selfy", "Selfy", referred_by=11111)
        assert db.get_user(11111)["referred_by"] is None
        assert referral_identity(11111) == (0, 0)

    def test_first_referrer_wins_intact(self, env):
        db.register_user(22222, "u22222", "U22222")
        created = db.register_user(33333, "u33333", "U33333",
                                   referred_by=22222)
        assert created is True
        # A second registration attempt never re-attributes.
        again = db.register_user(33333, "u33333", "U33333",
                                 referred_by=OTHER)
        assert again is False
        assert db.get_user(33333)["referred_by"] == 22222

    def test_unknown_user_has_no_referral(self, env):
        assert referral_identity(424242) == (0, 0)


# ══════════════════════════════════════════════════════════════════
# Submission — buyer-pending claim
# ══════════════════════════════════════════════════════════════════


class TestClaimSubmission:
    def test_claim_persisted_with_pending_approval(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        out = ReferralClaimService.submit(WORKER, tid, "key-1")
        assert out.state == "pending"
        row = _claim_row(out.submission_id)
        assert row["user_id"] == WORKER
        assert row["task_id"] == tid
        # The four-state vocabulary is untouched; approval is separate.
        assert row["status"] == db.SUBMISSION_STATUS_SUBMITTED
        assert row["approval_status"] == db.SUBMISSION_APPROVAL_PENDING
        assert row["approver_user_id"] is None      # no decision yet
        assert row["approval_decided_at"] is None
        assert row["idempotency_key"] == "key-1"

    def test_claim_does_not_complete(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        ReferralClaimService.submit(WORKER, tid, "key-1")
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_STARTED

    def test_claim_does_not_reward(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        ReferralClaimService.submit(WORKER, tid, "key-1")
        assert _wallet_units(WORKER) == 0
        assert _task_credits(WORKER) == []

    def test_same_key_is_idempotent(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        first = ReferralClaimService.submit(WORKER, tid, "same-key")
        second = ReferralClaimService.submit(WORKER, tid, "same-key")
        assert first.submission_id == second.submission_id
        assert second.state == "pending"
        assert len(_claim_rows(tid)) == 1

    def test_duplicate_open_claim_prevented(self, env):
        """At most ONE open claim per (user, task) — old
        UNIQUE(task_id, worker_id) behavior, regardless of key."""
        tid = env["task_id"]
        _start(WORKER, tid)
        first = ReferralClaimService.submit(WORKER, tid, "key-1")
        second = ReferralClaimService.submit(WORKER, tid, "key-2")
        assert second.submission_id == first.submission_id
        assert len(_claim_rows(tid)) == 1

    def test_claim_requires_started_task(self, env):
        with pytest.raises(ReferralClaimError):
            ReferralClaimService.submit(WORKER, env["task_id"], "key-1")
        assert _claim_rows(env["task_id"]) == []

    def test_claim_requires_referral_task_type(self, env):
        other = _make_task(_valid_task_data(), task_type="deterministic")
        _start(WORKER, other)
        with pytest.raises(ReferralClaimError, match="not a referral"):
            ReferralClaimService.submit(WORKER, other, "key-1")

    def test_claim_requires_active_task(self, env):
        tid = _make_task(_valid_task_data(), active=False)
        # The attempt policy rejects inactive tasks before any claim.
        with pytest.raises(ReferralClaimError, match="not active"):
            ReferralClaimService.submit(WORKER, tid, "key-1")

    def test_invalid_definition_rejected(self, env):
        tid = _make_task({"provider": "telegram"})  # broken on purpose
        _start(WORKER, tid)
        with pytest.raises(ReferralClaimError, match="invalid"):
            ReferralClaimService.submit(WORKER, tid, "key-1")

    def test_no_referral_cannot_claim(self, env):
        tid = env["task_id"]
        _start(STRANGER, tid)
        with pytest.raises(ReferralClaimError, match="no referral"):
            ReferralClaimService.submit(STRANGER, tid, "key-1")
        assert _claim_rows(tid, user_id=STRANGER) == []

    def test_self_referral_cannot_claim(self, env):
        tid = env["task_id"]
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE users SET referred_by = user_id WHERE user_id = ?",
                (STRANGER,),
            )
        _start(STRANGER, tid)
        with pytest.raises(ReferralClaimError, match="self-referral"):
            ReferralClaimService.submit(STRANGER, tid, "key-1")

    def test_buyer_cannot_work_own_task(self, env):
        tid = env["task_id"]
        _start(BUYER, tid)
        with pytest.raises(ReferralClaimError, match="own referral task"):
            ReferralClaimService.submit(BUYER, tid, "key-1")

    @pytest.mark.parametrize("key", ["", "   ", "x" * 200, "bad key!"])
    def test_invalid_idempotency_key_rejected(self, env, key):
        tid = env["task_id"]
        _start(WORKER, tid)
        with pytest.raises(ReferralClaimError, match="idempotency"):
            ReferralClaimService.submit(WORKER, tid, key)
        assert _claim_rows(tid) == []

    def test_server_generates_key_when_absent(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        out = ReferralClaimService.submit(WORKER, tid, None)
        row = _claim_row(out.submission_id)
        assert row["idempotency_key"]  # non-empty server key
        assert len(row["idempotency_key"]) == 32  # uuid4().hex


# ══════════════════════════════════════════════════════════════════
# Approval — server-authorized buyer decision
# ══════════════════════════════════════════════════════════════════


class TestApproval:
    def _pending_claim(self, env) -> int:
        tid = env["task_id"]
        _start(WORKER, tid)
        out = ReferralClaimService.submit(WORKER, tid, "claim-key")
        return out.submission_id

    def test_unauthorized_actor_cannot_approve(self, env):
        tid = env["task_id"]
        sid = self._pending_claim(env)
        with pytest.raises(ReferralDecisionError, match="authorized buyer"):
            ReferralApprovalService.decide(STRANGER, tid, sid, True)

    def test_unauthorized_actor_cannot_reject(self, env):
        tid = env["task_id"]
        sid = self._pending_claim(env)
        with pytest.raises(ReferralDecisionError, match="authorized buyer"):
            ReferralApprovalService.decide(OTHER, tid, sid, False)

    def test_worker_cannot_decide_own_claim(self, env):
        tid = env["task_id"]
        sid = self._pending_claim(env)
        # The worker is never the definition's approver here — the
        # authorization check stops them before anything else.
        with pytest.raises(ReferralDecisionError, match="authorized buyer"):
            ReferralApprovalService.decide(WORKER, tid, sid, True)

    def test_authorized_buyer_approves(self, env):
        tid = env["task_id"]
        sid = self._pending_claim(env)
        out = ReferralApprovalService.decide(BUYER, tid, sid, True)
        assert out.state == "approved"
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_COMPLETED

    def test_approval_is_persisted(self, env):
        tid = env["task_id"]
        sid = self._pending_claim(env)
        ReferralApprovalService.decide(BUYER, tid, sid, True)
        row = _claim_row(sid)
        assert row["approval_status"] == db.SUBMISSION_APPROVAL_APPROVED
        assert row["approver_user_id"] == BUYER      # who decided
        assert row["approval_decided_at"] is not None  # when
        assert row["status"] == db.SUBMISSION_STATUS_PASSED
        assert row["completed_at"] is not None       # tied to completion

    def test_approved_claim_completes_exactly_once(self, env):
        tid = env["task_id"]
        sid = self._pending_claim(env)
        ReferralApprovalService.decide(BUYER, tid, sid, True)
        # Re-approving is idempotent: no second transition possible.
        again = ReferralApprovalService.decide(BUYER, tid, sid, True)
        assert again.state == "approved"
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_COMPLETED
        with db.get_connection() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM user_tasks "
                "WHERE user_id = ? AND task_id = ?",
                (WORKER, tid),
            ).fetchone()["c"]
        assert n == 1

    def test_approved_claim_gets_exactly_one_reward(self, env):
        tid = env["task_id"]
        sid = self._pending_claim(env)
        ReferralApprovalService.decide(BUYER, tid, sid, True)
        ReferralApprovalService.decide(BUYER, tid, sid, True)  # replay
        assert _wallet_units(WORKER) == REWARD_UNITS
        credits = _task_credits(WORKER)
        assert len(credits) == 1
        assert credits[0]["amount_units"] == REWARD_UNITS

    def test_rejected_claim_zero_reward(self, env):
        tid = env["task_id"]
        sid = self._pending_claim(env)
        out = ReferralApprovalService.decide(BUYER, tid, sid, False)
        assert out.state == "rejected"
        assert _wallet_units(WORKER) == 0
        assert _task_credits(WORKER) == []

    def test_rejected_claim_cannot_complete(self, env):
        tid = env["task_id"]
        sid = self._pending_claim(env)
        ReferralApprovalService.decide(BUYER, tid, sid, False)
        # The rejection is durable: the same claim can never flip.
        with pytest.raises(ReferralDecisionError, match="already rejected"):
            ReferralApprovalService.decide(BUYER, tid, sid, True)
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_STARTED
        assert _wallet_units(WORKER) == 0

    def test_rejection_is_persisted(self, env):
        tid = env["task_id"]
        sid = self._pending_claim(env)
        ReferralApprovalService.decide(BUYER, tid, sid, False)
        row = _claim_row(sid)
        assert row["approval_status"] == db.SUBMISSION_APPROVAL_REJECTED
        assert row["approver_user_id"] == BUYER
        assert row["approval_decided_at"] is not None
        assert row["status"] == db.SUBMISSION_STATUS_FAILED
        assert row["completed_at"] is None
        assert row["verification_reason"]  # durable audit reason

    def test_approved_claim_cannot_be_rejected(self, env):
        tid = env["task_id"]
        sid = self._pending_claim(env)
        ReferralApprovalService.decide(BUYER, tid, sid, True)
        with pytest.raises(ReferralDecisionError, match="already approved"):
            ReferralApprovalService.decide(BUYER, tid, sid, False)

    def test_retry_after_rejection_yields_at_most_one_reward(self, env):
        """Old post-rejection re-claim behavior: the worker may open a
        NEW claim, but a one_time cycle still pays exactly once."""
        tid = env["task_id"]
        first = self._pending_claim(env)
        ReferralApprovalService.decide(BUYER, tid, first, False)
        assert _wallet_units(WORKER) == 0

        # Retry (fresh key) → new pending claim.
        second = ReferralClaimService.submit(WORKER, tid, "claim-key-2")
        assert second.state == "pending"
        assert second.submission_id != first
        assert len(_claim_rows(tid)) == 2

        ReferralApprovalService.decide(BUYER, tid, second.submission_id, True)
        assert _wallet_units(WORKER) == REWARD_UNITS
        assert len(_task_credits(WORKER)) == 1

        # The completed cycle can never be re-claimed (policy gate).
        with pytest.raises(ReferralClaimError, match="completed"):
            ReferralClaimService.submit(WORKER, tid, "claim-key-3")

    def test_unknown_claim_not_found(self, env):
        with pytest.raises(ReferralDecisionError, match="not found"):
            ReferralApprovalService.decide(BUYER, env["task_id"],
                                           999999, True)

    def test_foreign_task_claim_not_found(self, env):
        tid = env["task_id"]
        sid = self._pending_claim(env)
        other_task = _make_task(_valid_task_data())
        with pytest.raises(ReferralDecisionError, match="not found"):
            ReferralApprovalService.decide(BUYER, other_task, sid, True)
        # The original claim is untouched.
        assert _claim_row(sid)["approval_status"] == (
            db.SUBMISSION_APPROVAL_PENDING
        )

    def test_decision_on_non_referral_task_rejected(self, env):
        other = _make_task({"channel_slug": "x"},
                           task_type="deterministic")
        with pytest.raises(ReferralDecisionError, match="not a referral"):
            ReferralApprovalService.decide(BUYER, other, 1, True)


# ══════════════════════════════════════════════════════════════════
# Security — malicious payloads can never forge anything
# ══════════════════════════════════════════════════════════════════


class TestSecurity:
    @pytest.mark.parametrize("field", sorted(FORBIDDEN_FIELDS))
    def test_forbidden_fields_still_rejected(self, env, field):
        from task_submission import SubmissionError, TaskSubmissionService

        tid = env["task_id"]
        _start(WORKER, tid)
        with pytest.raises(SubmissionError):
            TaskSubmissionService.submit(WORKER, tid, {field: "x"})

    def test_client_cannot_change_referral_identity(self, env):
        """A stranger smuggling referrer fields still has no valid
        users.referred_by relationship → claim rejected."""
        tid = env["task_id"]
        _start(STRANGER, tid)
        # ReferralClaimService reads nothing from the client; its
        # server-side identity check alone decides.
        with pytest.raises(ReferralClaimError, match="no referral"):
            ReferralClaimService.submit(STRANGER, tid, "key-1")

    def test_client_cannot_impersonate_buyer_at_decision(self, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        sid = ReferralClaimService.submit(WORKER, tid, "k").submission_id
        # A body-supplied buyer_id never reaches the service: it only
        # receives the caller's VERIFIED identity.
        with pytest.raises(ReferralDecisionError, match="authorized buyer"):
            ReferralApprovalService.decide(STRANGER, tid, sid, True)
        assert _claim_row(sid)["approval_status"] == (
            db.SUBMISSION_APPROVAL_PENDING
        )
        assert _wallet_units(WORKER) == 0

    def test_user_tasks_vocabulary_unchanged(self, env):
        """Approval state never leaks into user_tasks."""
        tid = env["task_id"]
        _start(WORKER, tid)
        sid = ReferralClaimService.submit(WORKER, tid, "k").submission_id
        ReferralApprovalService.decide(BUYER, tid, sid, False)
        with db.get_connection() as conn:
            cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(user_tasks)")
            }
        assert cols == {
            "user_id", "task_id", "status", "started_at", "completed_at",
        }
        assert db.ALLOWED_USER_TASK_STATUSES == {
            db.USER_TASK_STATUS_AVAILABLE,
            db.USER_TASK_STATUS_STARTED,
            db.USER_TASK_STATUS_COMPLETED,
        }

    def test_no_quantity_field_anywhere(self, env):
        """Quantity is not faked: no quantity column exists."""
        with db.get_connection() as conn:
            for table in ("tasks", "user_tasks", "task_submissions"):
                cols = {
                    r[1] for r in conn.execute(f"PRAGMA table_info({table})")
                }
                assert not any("quantity" in c for c in cols), table

    def test_store_rejects_unknown_approval_states(self, env):
        from task_submission_store import TaskSubmissionStore as S
        with pytest.raises(ValueError):
            # decide() only ever writes approved/rejected — never a
            # complaint-style state.
            S.mark_approval_decision(1, "complaint_pending", BUYER)


# ══════════════════════════════════════════════════════════════════
# Repeat policy — one_time only for referral claims
# ══════════════════════════════════════════════════════════════════


class TestRepeatPolicy:
    def test_one_time_referral_task_works(self, env):
        tid = env["task_id"]
        assert db.get_task(tid)["repeat_policy"] == db.REPEAT_POLICY_ONE_TIME
        _start(WORKER, tid)
        sid = ReferralClaimService.submit(WORKER, tid, "k1").submission_id
        ReferralApprovalService.decide(BUYER, tid, sid, True)
        # Terminal: cannot start a second cycle, ever.
        with pytest.raises(StartGateError, match="already completed"):
            TaskStartGate().start(WORKER, tid)

    def test_repeatable_referral_definition_rejected(self, env):
        """No safe referral-cycle identity exists in the current data
        model (users.referred_by is permanent), so repeatable referral
        tasks are refused server-side instead of guessed.  Reported as
        a domain dependency — not implemented."""
        tid = _make_task(
            _valid_task_data(),
            repeat_policy=db.REPEAT_POLICY_REPEATABLE,
            repeat_hours=24,
        )
        _start(WORKER, tid)
        with pytest.raises(ReferralClaimError, match="repeatable"):
            ReferralClaimService.submit(WORKER, tid, "k1")

    def test_no_unlimited_reward_from_one_referral(self, env):
        """One worker, one referral identity → at most ONE reward ever
        (claim → approve → completed → policy gate blocks forever)."""
        tid = env["task_id"]
        _start(WORKER, tid)
        sid = ReferralClaimService.submit(WORKER, tid, "k1").submission_id
        ReferralApprovalService.decide(BUYER, tid, sid, True)
        ReferralApprovalService.decide(BUYER, tid, sid, True)
        for key in ("k2", "k3", None):
            with pytest.raises(ReferralClaimError):
                ReferralClaimService.submit(WORKER, tid, key)
        assert _wallet_units(WORKER) == REWARD_UNITS
        assert len(_task_credits(WORKER)) == 1


# ══════════════════════════════════════════════════════════════════
# API — minimal safe extension
# ══════════════════════════════════════════════════════════════════


class TestTaskListApi:
    def test_referral_entry_has_narrow_boolean_only(self, client, env):
        tid = env["task_id"]
        response = client.get("/api/tasks", headers=_auth(WORKER))
        assert response.status_code == 200
        tasks = {t["id"]: t for t in response.get_json()["tasks"]}
        entry = tasks[tid]
        assert entry["awaiting_decision"] is False  # no claim yet
        # Nothing internal or approval-vocabulary leaks to the page.
        assert "approval" not in entry
        assert "task_data" not in entry
        assert "approver" not in entry
        dumped = json.dumps(entry)
        for banned in ("task_data", "approver", "bot_username",
                       "telegram_user_id", "referred_by"):
            assert banned not in dumped

    def test_awaiting_flag_true_while_claim_open(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        ReferralClaimService.submit(WORKER, tid, "k1")
        response = client.get("/api/tasks", headers=_auth(WORKER))
        tasks = {t["id"]: t for t in response.get_json()["tasks"]}
        assert tasks[tid]["awaiting_decision"] is True

    def test_awaiting_flag_false_after_decision(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        sid = ReferralClaimService.submit(WORKER, tid, "k1").submission_id
        ReferralApprovalService.decide(BUYER, tid, sid, True)
        response = client.get("/api/tasks", headers=_auth(WORKER))
        tasks = {t["id"]: t for t in response.get_json()["tasks"]}
        assert tasks[tid]["awaiting_decision"] is False
        assert tasks[tid]["status"] == db.USER_TASK_STATUS_COMPLETED

    def test_non_referral_tasks_have_no_boolean(self, client, env):
        _make_task({"channel_slug": "x"}, task_type="deterministic")
        response = client.get("/api/tasks", headers=_auth(WORKER))
        for entry in response.get_json()["tasks"]:
            if entry["type"] != REFERRAL_TASK_TYPE:
                assert "awaiting_decision" not in entry


class TestSubmitRoute:
    def test_submit_opens_pending_claim(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        response = client.post(f"/api/tasks/{tid}/submit",
                               headers=_auth(WORKER))
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["approval"] == "pending"
        assert data["awaiting_decision"] is True
        assert data["status"] == db.USER_TASK_STATUS_STARTED
        assert data["message"]  # Arabic, user-facing
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_STARTED
        assert _wallet_units(WORKER) == 0

    def test_malicious_body_cannot_force_approval(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        response = client.post(
            f"/api/tasks/{tid}/submit",
            headers=_auth(WORKER),
            json={
                "approved": True,
                "verified": True,
                "referrer_id": STRANGER,
                "buyer_id": STRANGER,
                "referred_user_id": STRANGER,
            },
        )
        assert response.status_code == 200
        assert response.get_json()["approval"] == "pending"
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_STARTED
        assert _wallet_units(WORKER) == 0
        assert _task_credits(WORKER) == []

    @pytest.mark.parametrize("field,value", [
        ("status", "approved"),
        ("reward", 999999),
        ("user_id", OTHER),
        ("completed", True),
        ("task_id", 1),
    ])
    def test_forbidden_body_fields_rejected_400(self, client, env, field,
                                                value):
        tid = env["task_id"]
        _start(WORKER, tid)
        response = client.post(f"/api/tasks/{tid}/submit",
                               headers=_auth(WORKER),
                               json={field: value})
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_submission"
        assert _wallet_units(WORKER) == 0

    def test_stranger_without_referral_gets_safe_error(self, client, env):
        tid = env["task_id"]
        _start(STRANGER, tid)
        response = client.post(f"/api/tasks/{tid}/submit",
                               headers=_auth(STRANGER))
        assert response.status_code == 409
        assert response.get_json()["error"] == "no_referral"
        assert _claim_rows(tid, user_id=STRANGER) == []

    def test_unauthenticated_submit_rejected(self, client, env):
        tid = env["task_id"]
        response = client.post(f"/api/tasks/{tid}/submit")
        assert response.status_code == 401


class TestClaimsRoute:
    def test_buyer_lists_pending_claims_safely(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        ReferralClaimService.submit(WORKER, tid, "k1")
        response = client.get(f"/api/tasks/{tid}/claims",
                              headers=_auth(BUYER))
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert len(data["claims"]) == 1
        claim = data["claims"][0]
        # Safe fields ONLY — no worker identity, no task_data.
        assert set(claim.keys()) == {"claim_id", "submitted_at"}
        assert isinstance(claim["claim_id"], int)

    def test_worker_cannot_list_claims(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        ReferralClaimService.submit(WORKER, tid, "k1")
        response = client.get(f"/api/tasks/{tid}/claims",
                              headers=_auth(WORKER))
        assert response.status_code == 403
        assert response.get_json()["error"] == "not_approver"

    def test_stranger_cannot_list_claims(self, client, env):
        response = client.get(f"/api/tasks/{env['task_id']}/claims",
                              headers=_auth(STRANGER))
        assert response.status_code == 403

    def test_unauthenticated_list_rejected(self, client, env):
        response = client.get(f"/api/tasks/{env['task_id']}/claims")
        assert response.status_code == 401

    def test_non_referral_task_rejected(self, client, env):
        other = _make_task({"channel_slug": "x"}, task_type="deterministic")
        response = client.get(f"/api/tasks/{other}/claims",
                              headers=_auth(BUYER))
        assert response.status_code == 409

    def test_unknown_task_404(self, client, env):
        response = client.get("/api/tasks/424242/claims",
                              headers=_auth(BUYER))
        assert response.status_code == 404

    def test_empty_after_decision(self, client, env):
        tid = env["task_id"]
        _start(WORKER, tid)
        sid = ReferralClaimService.submit(WORKER, tid, "k1").submission_id
        ReferralApprovalService.decide(BUYER, tid, sid, True)
        response = client.get(f"/api/tasks/{tid}/claims",
                              headers=_auth(BUYER))
        assert response.get_json()["claims"] == []


class TestDecisionRoute:
    def _open_claim(self, env) -> int:
        tid = env["task_id"]
        _start(WORKER, tid)
        return ReferralClaimService.submit(WORKER, tid, "k1").submission_id

    def test_buyer_approves_via_api(self, client, env):
        tid = env["task_id"]
        sid = self._open_claim(env)
        response = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(BUYER), json={"decision": "approve"},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["approval"] == "approved"
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_COMPLETED
        assert _wallet_units(WORKER) == REWARD_UNITS
        assert len(_task_credits(WORKER)) == 1

    def test_buyer_rejects_via_api(self, client, env):
        tid = env["task_id"]
        sid = self._open_claim(env)
        response = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(BUYER), json={"decision": "reject"},
        )
        assert response.status_code == 200
        assert response.get_json()["approval"] == "rejected"
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_STARTED
        assert _wallet_units(WORKER) == 0

    def test_worker_cannot_approve_via_api(self, client, env):
        tid = env["task_id"]
        sid = self._open_claim(env)
        response = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(WORKER), json={"decision": "approve"},
        )
        assert response.status_code == 403
        assert response.get_json()["error"] == "not_approver"
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_STARTED
        assert _wallet_units(WORKER) == 0

    def test_impersonation_body_ignored_identity_decides(self, client, env):
        tid = env["task_id"]
        sid = self._open_claim(env)
        # Stranger claims to be the buyer in the body — initData wins.
        response = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(STRANGER),
            json={"decision": "approve", "buyer_id": STRANGER,
                  "user_id": BUYER},
        )
        assert response.status_code == 403
        assert _wallet_units(WORKER) == 0

    def test_unauthenticated_decision_rejected(self, client, env):
        tid = env["task_id"]
        sid = self._open_claim(env)
        response = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            json={"decision": "approve"},
        )
        assert response.status_code == 401

    @pytest.mark.parametrize("body", [
        {},
        {"decision": "force"},
        {"decision": "approved"},
        {"decision": True},
        {"decision": 1},
        {"decision": None},
        {"decision": ""},
    ])
    def test_invalid_decision_value_400(self, client, env, body):
        tid = env["task_id"]
        sid = self._open_claim(env)
        response = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(BUYER), json=body,
        )
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_decision"
        assert _claim_row(sid)["approval_status"] == (
            db.SUBMISSION_APPROVAL_PENDING
        )

    def test_missing_body_400(self, client, env):
        tid = env["task_id"]
        sid = self._open_claim(env)
        response = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(BUYER),
        )
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_decision"

    def test_non_json_body_400(self, client, env):
        tid = env["task_id"]
        sid = self._open_claim(env)
        response = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(BUYER),
            data="raw", content_type="text/plain",
        )
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_decision"

    def test_non_object_json_body_400(self, client, env):
        tid = env["task_id"]
        sid = self._open_claim(env)
        response = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(BUYER), json=["approve"],
        )
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_request"
        assert _claim_row(sid)["approval_status"] == (
            db.SUBMISSION_APPROVAL_PENDING
        )

    def test_unknown_claim_404(self, client, env):
        response = client.post(
            f"/api/tasks/{env['task_id']}/claims/999999/decision",
            headers=_auth(BUYER), json={"decision": "approve"},
        )
        assert response.status_code == 404

    def test_double_approve_stays_exactly_one_credit(self, client, env):
        tid = env["task_id"]
        sid = self._open_claim(env)
        for _ in range(2):
            response = client.post(
                f"/api/tasks/{tid}/claims/{sid}/decision",
                headers=_auth(BUYER), json={"decision": "approve"},
            )
            assert response.status_code == 200
        assert _wallet_units(WORKER) == REWARD_UNITS
        assert len(_task_credits(WORKER)) == 1

    def test_conflicting_decision_409(self, client, env):
        tid = env["task_id"]
        sid = self._open_claim(env)
        first = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(BUYER), json={"decision": "approve"},
        )
        assert first.status_code == 200
        conflict = client.post(
            f"/api/tasks/{tid}/claims/{sid}/decision",
            headers=_auth(BUYER), json={"decision": "reject"},
        )
        assert conflict.status_code == 409
        assert conflict.get_json()["error"] == "claim_already_approved"

    def test_worker_page_has_no_decision_capability(self):
        """Guard: the Tasks page JS holds no approval surface —
        neither endpoint is ever called and no approve/reject action
        literal exists.  (Authority lives server-side only.)"""
        content = open("miniapp/js/tasks.js").read()
        assert "/decision" not in content
        assert "/claims" not in content
        for quoted in ("'approve'", "'reject'", '"approve"',
                       '"reject"', "'approved'", "'rejected'"):
            assert quoted not in content, quoted
