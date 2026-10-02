"""
Roadmap #5 — Referral Tasks: audit boundaries + race regression
================================================================

AUDIT RESULT (Roadmap #5 evaluation, no production code changed):

  The three Roadmap #5 items were evaluated against the CURRENT data
  model and every one of them lacks a representable contract, so none
  of them is implemented here — this file pins the boundary and the
  deferred behaviour instead of inventing a design:

  (1) Paid seat / quantity reservation — DEFERRED.
      No quantity/seats/capacity/reservation column exists on tasks,
      user_tasks or task_submissions; no creation or claim path reads
      or writes capacity; referral_task.py documents "task quantity /
      slot reservation is NOT implemented … pending task quantity
      domain decision".  Implementing would require inventing a
      quantity domain plus exhaustion/refund semantics (no contract).
      Pinned below: schema has no capacity vocabulary, claims are
      unbounded across workers (no seat gate exists today), and the
      referral production code contains no partial capacity
      implementation.

  (2) Repeatable referral cycle — DEFERRED.
      referral_task.py rejects repeatable definitions server-side: a
      referral-cycle identity is not representable (users.referred_by
      is a permanent relationship; task_submissions carries no cycle
      binding; a repeatable campaign would re-pay the same worker for
      identical evidence with no cycle-scoped referral event to
      verify).  The generic repeatable machinery (task_start cooldown)
      is untouched and unaffected.  Pinned below: the rejection
      happens with ZERO side effects (no claim row, no user_tasks
      mutation, no wallet/ledger movement) — fail-closed, retry-safe.

  (3) Complaints / arbitration — DEFERRED.
      The approval vocabulary is exactly (pending, approved,
      rejected); the product has no complaint authority workflow
      (support_service is user↔admin inquiry messaging, and the admin
      review queue explicitly EXCLUDES referral claims — the buyer
      decides).  Pinned below: the store rejects every dispute-shaped
      state, the vocabulary is exact, and the referral production code
      contains no hidden dispute/refund semantics.

Coverage map for the mandated Roadmap #5 test list:

  successful referral claim ....... test_referral_task.py (existing,
                                     145-test suite — unchanged)
  duplicate claim / idempotency ... existing (same-key + single-open
                                     claim) + NEW same-key race below
  concurrent claims / race ........ NEW (this file: 2 claim races)
  capacity reservation ............ not implementable → deferred
                                     boundary pinned (schema + code +
                                     unbounded behaviour)
  insufficient capacity → zero
    partial mutation .............. not applicable (no capacity);
                                     NEW zero-partial-mutation suite
                                     covers every claim rejection point
  approve/reject state machine .... existing + NEW concurrent
                                     approve-vs-reject consistency race
  authorization / foreign claim ... test_referral_task.py TestSecurity
                                     + TestApproval (existing)
  cycle behaviour ................. model exists only as one_time →
                                     existing terminal tests + NEW
                                     repeatable zero-side-effect proof
  dispute/arbitration boundary .... no contract → NEW vocabulary +
                                     store-rejection + source guards
  no duplicate financial movement . existing (sequential replay) +
                                     NEW concurrent double-approve race
  wallet/ledger atomicity ......... NEW race asserts exactly one
                                     credit row == reward units
  source guards (no service
    bypass) ....................... NEW (this file)
  old referral tests unchanged .... test_referral_task.py and
                                     test_referral.py are untouched

Rules honoured: no asyncio.create_task anywhere; no sleeps or
pytest-order (barrier-synchronised threads only, the same pattern as
test_db_transactions.TestConcurrency); no secrets, message bodies or
recipient ids logged; protected files (wallet.py, ledger.py,
withdrawal_service.py, withdrawal_rules.py, task_lifecycle.py) are not
modified — this task adds tests/guards only.

Run:
    python3 -m pytest test_referral_roadmap5.py -v
"""

from __future__ import annotations

import json
import re
import threading

import pytest

import db
import referral_task
from referral_task import (
    ReferralApprovalService,
    ReferralClaimError,
    ReferralClaimService,
    ReferralDecisionError,
)
from task_submission_store import TaskSubmissionStore

# Shared fixtures/helpers from the existing referral suite — imported,
# never modified (the old tests stay byte-for-byte identical).
from test_referral_task import (
    BUYER,
    REWARD_UNITS,
    STRANGER,
    WORKER,
    _claim_row,
    _claim_rows,
    _make_task,
    _start,
    _status,
    _task_credits,
    _valid_task_data,
    _wallet_units,
)


# ── Fixture (mirrors the existing env: same users, same task) ──────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Temp DB, users, a genuine referral, and one valid referral task."""
    db_path = str(tmp_path / "referral_roadmap5_test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)

    db.register_user(BUYER, "buyer", "Buyer")
    db.register_user(WORKER, "worker", "Worker")
    db.register_user(STRANGER, "stranger", "Stranger")
    db.register_user(7003, "referred", "Referred", referred_by=WORKER)

    task_id = db.create_task(
        title="مهمة إحالة مدفوعة",
        description="Roadmap #5 boundary fixture",
        task_type="referral_task",
        reward=50,
        task_data=json.dumps(_valid_task_data()),
    )
    yield {"db_path": db_path, "task_id": task_id}


# ── Helpers ────────────────────────────────────────────────────────


def _parallel(*fns):
    """Run *fns* concurrently, released together by a barrier.

    Returns ``(results, errors, alive_threads)`` — no sleeps, no
    timing assertions, only start-synchronisation and join timeouts.
    Assertions are written as interleaving-independent invariants, so
    no ordering of the two threads can fail the test spuriously.
    """
    barrier = threading.Barrier(len(fns), timeout=15)
    lock = threading.Lock()
    results: list = []
    errors: list[BaseException] = []

    def wrap(fn):
        def run():
            try:
                barrier.wait()
                out = fn()
                with lock:
                    results.append(out)
            except BaseException as exc:  # noqa: BLE001 — collected for assert
                with lock:
                    errors.append(exc)
        return threading.Thread(target=run)

    threads = [wrap(fn) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    alive = [t for t in threads if t.is_alive()]
    return results, errors, alive


def _snapshot(user_id: int, task_id: int) -> dict:
    """Full observable state of one (user, task) claim context."""
    claims = _claim_rows(task_id, user_id=user_id)
    with db.get_connection() as conn:
        ut = conn.execute(
            "SELECT status, started_at, completed_at FROM user_tasks "
            "WHERE user_id = ? AND task_id = ?",
            (user_id, task_id),
        ).fetchone()
        ledger_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger WHERE user_id = ?",
            (user_id,),
        ).fetchone()["c"]
    return {
        "claims": [
            (r["submission_id"], r["status"], r["approval_status"])
            for r in claims
        ],
        "user_task": dict(ut) if ut is not None else None,
        "wallet": _wallet_units(user_id),
        "ledger_rows": ledger_rows,
    }


# ══════════════════════════════════════════════════════════════════
# 1. Concurrent claim races — the store decides, never the app
# ══════════════════════════════════════════════════════════════════


class TestConcurrentClaimRaces:
    def test_different_key_claims_yield_exactly_one_open_claim(self, env):
        """Two simultaneous claims with DIFFERENT idempotency keys:
        the single-open-claim rule is decided inside BEGIN IMMEDIATE
        (never application-level SELECT-then-INSERT), so exactly ONE
        submission row exists and both callers resolve to it."""
        tid = env["task_id"]
        _start(WORKER, tid)

        results, errors, alive = _parallel(
            lambda: ReferralClaimService.submit(WORKER, tid, "race-key-1"),
            lambda: ReferralClaimService.submit(WORKER, tid, "race-key-2"),
        )
        assert alive == [], "claim thread hung"
        assert errors == []
        assert len(results) == 2
        # Same record for both callers — a second row would mean the
        # pending check was bypassed.
        assert len({r.submission_id for r in results}) == 1
        assert {r.state for r in results} == {"pending"}
        rows = _claim_rows(tid)
        assert len(rows) == 1
        assert rows[0]["approval_status"] == db.SUBMISSION_APPROVAL_PENDING

    def test_same_key_claims_resolve_to_one_record(self, env):
        """Two simultaneous claims with the SAME key: the UNIQUE
        (user, task, idempotency_key) constraint decides — exactly one
        row, and both callers receive that same submission id."""
        tid = env["task_id"]
        _start(WORKER, tid)

        results, errors, alive = _parallel(
            lambda: ReferralClaimService.submit(WORKER, tid, "race-same"),
            lambda: ReferralClaimService.submit(WORKER, tid, "race-same"),
        )
        assert alive == [], "claim thread hung"
        assert errors == []
        assert len(results) == 2
        assert len({r.submission_id for r in results}) == 1
        assert {r.state for r in results} == {"pending"}
        assert len(_claim_rows(tid)) == 1


# ══════════════════════════════════════════════════════════════════
# 2. Concurrent decision races — money moves at most once
# ══════════════════════════════════════════════════════════════════


class TestConcurrentDecisionRaces:
    def test_concurrent_double_approve_credits_exactly_once(self, env):
        """Two simultaneous approvals of the same claim: the approval
        CAS, the completion compare-and-set and the ledger idempotency
        key together guarantee EXACTLY ONE credit — never a double
        payout, under any interleaving."""
        tid = env["task_id"]
        _start(WORKER, tid)
        sid = ReferralClaimService.submit(WORKER, tid, "k").submission_id

        results, errors, alive = _parallel(
            lambda: ReferralApprovalService.decide(BUYER, tid, sid, True),
            lambda: ReferralApprovalService.decide(BUYER, tid, sid, True),
        )
        assert alive == [], "decision thread hung"
        assert errors == []
        assert len(results) == 2
        # Approving twice is idempotent: both callers see 'approved'.
        assert {r.state for r in results} == {"approved"}

        # Exactly one completion and exactly one credit.
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_COMPLETED
        assert _wallet_units(WORKER) == REWARD_UNITS
        credits = _task_credits(WORKER)
        assert len(credits) == 1
        assert credits[0]["amount_units"] == REWARD_UNITS

        row = _claim_row(sid)
        assert row["approval_status"] == db.SUBMISSION_APPROVAL_APPROVED
        assert row["approver_user_id"] == BUYER
        assert row["status"] == db.SUBMISSION_STATUS_PASSED
        assert row["completed_at"] is not None

        # A post-race replay still cannot double-credit.
        ReferralApprovalService.decide(BUYER, tid, sid, True)
        assert len(_task_credits(WORKER)) == 1
        assert _wallet_units(WORKER) == REWARD_UNITS

    def test_concurrent_approve_vs_reject_stays_consistent(self, env):
        """Two simultaneous OPPOSITE decisions: exactly one wins the
        CAS, the loser gets the deterministic already-decided
        conflict, and the final state is financially consistent —
        approved ⇒ completed + exactly one credit; rejected ⇒ started
        + zero credits.  No third state, no partial money."""
        tid = env["task_id"]
        _start(WORKER, tid)
        sid = ReferralClaimService.submit(WORKER, tid, "k").submission_id

        results, errors, alive = _parallel(
            lambda: ReferralApprovalService.decide(BUYER, tid, sid, True),
            lambda: ReferralApprovalService.decide(BUYER, tid, sid, False),
        )
        assert alive == [], "decision thread hung"
        # Exactly one winner returned an outcome; exactly one loser
        # hit the already-decided conflict (the sequential contract
        # mirrored under true concurrency).
        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], ReferralDecisionError)
        assert "already" in str(errors[0])

        row = _claim_row(sid)
        assert row["approver_user_id"] == BUYER
        assert row["approval_decided_at"] is not None
        if row["approval_status"] == db.SUBMISSION_APPROVAL_APPROVED:
            assert results[0].state == "approved"
            assert _status(WORKER, tid) == db.USER_TASK_STATUS_COMPLETED
            assert _wallet_units(WORKER) == REWARD_UNITS
            credits = _task_credits(WORKER)
            assert len(credits) == 1
            assert credits[0]["amount_units"] == REWARD_UNITS
        else:
            assert row["approval_status"] == db.SUBMISSION_APPROVAL_REJECTED
            assert results[0].state == "rejected"
            assert _status(WORKER, tid) == db.USER_TASK_STATUS_STARTED
            assert _wallet_units(WORKER) == 0
            assert _task_credits(WORKER) == []


# ══════════════════════════════════════════════════════════════════
# 3. Failed claims leave ZERO partial mutation
# ══════════════════════════════════════════════════════════════════


def _case_not_started(env):
    return env["task_id"], WORKER, None


def _case_stranger_no_referral(env):
    tid = env["task_id"]
    _start(STRANGER, tid)
    return tid, STRANGER, "no referral"


def _case_buyer_own_task(env):
    tid = env["task_id"]
    _start(BUYER, tid)
    return tid, BUYER, "own referral task"


def _case_inactive_task(env):
    tid = _make_task(_valid_task_data(), active=False)
    return tid, WORKER, "not active"


def _case_non_referral_type(env):
    tid = _make_task(_valid_task_data(), task_type="deterministic")
    _start(WORKER, tid)
    return tid, WORKER, "not a referral"


def _case_invalid_definition(env):
    tid = _make_task({"provider": "telegram"})  # broken on purpose
    _start(WORKER, tid)
    return tid, WORKER, "invalid"


def _case_self_referral(env):
    tid = env["task_id"]
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE users SET referred_by = user_id WHERE user_id = ?",
            (STRANGER,),
        )
    _start(STRANGER, tid)
    return tid, STRANGER, "self-referral"


def _case_invalid_idempotency_key(env):
    tid = env["task_id"]
    _start(WORKER, tid)
    return tid, WORKER, "idempotency"


class TestFailedClaimZeroPartialMutation:
    """Every claim rejection point is validated BEFORE any write: no
    submission row, no user_tasks change, no wallet/ledger movement —
    zero partial mutation on failure (the no-capacity analogue of
    "insufficient capacity → zero partial mutation")."""

    @pytest.mark.parametrize(
        "prepare,needle,key",
        [
            (_case_not_started, None, "reject-key"),
            (_case_stranger_no_referral, "no referral", "reject-key"),
            (_case_buyer_own_task, "own referral task", "reject-key"),
            (_case_inactive_task, "not active", "reject-key"),
            (_case_non_referral_type, "not a referral", "reject-key"),
            (_case_invalid_definition, "invalid", "reject-key"),
            (_case_self_referral, "self-referral", "reject-key"),
            # Spaces/"!" are outside the allowed key alphabet, so key
            # normalization rejects it before any write occurs.
            (_case_invalid_idempotency_key, "idempotency", "bad key!"),
        ],
        ids=[
            "not-started",
            "no-referral",
            "buyer-own-task",
            "inactive-task",
            "non-referral-type",
            "invalid-definition",
            "self-referral",
            "invalid-key",
        ],
    )
    def test_rejected_claim_mutates_nothing(self, env, prepare, needle, key):
        tid, user, match = prepare(env)
        before = _snapshot(user, tid)
        if needle is None:
            with pytest.raises(ReferralClaimError):
                ReferralClaimService.submit(user, tid, key)
        else:
            with pytest.raises(ReferralClaimError, match=match):
                ReferralClaimService.submit(user, tid, key)
        after = _snapshot(user, tid)
        assert after == before, "a rejected claim must mutate nothing"
        assert _wallet_units(user) == 0
        assert _task_credits(user) == []


# ══════════════════════════════════════════════════════════════════
# 4. Roadmap #5 (1): paid seats / capacity — DEFERRED, pinned
# ══════════════════════════════════════════════════════════════════


class TestDeferredPaidSeatBoundary:
    def test_no_capacity_seat_or_reservation_columns_exist(self, env):
        """Roadmap #5 seat reservation is deferred: the schema has NO
        capacity vocabulary anywhere (superset check of the existing
        quantity-only guard in test_referral_task)."""
        forbidden = ("quantity", "seat", "capacit", "reserv", "quota")
        with db.get_connection() as conn:
            for table in ("tasks", "user_tasks", "task_submissions"):
                cols = [
                    r[1].lower()
                    for r in conn.execute(f"PRAGMA table_info({table})")
                ]
                for col in cols:
                    for word in forbidden:
                        assert word not in col, f"{table}.{col} ~ '{word}'"

    def test_pending_claims_are_unbounded_no_seat_gate(self, env):
        """Behavioural proof of the DEFERRED state: no capacity gate
        exists — every authorized worker holds their OWN pending claim
        simultaneously, and deciding one never cascades to the others
        (there is no shared seat pool to release)."""
        tid = env["task_id"]
        workers = [11001, 11002, 11003, 11004, 11005]
        for i, worker in enumerate(workers):
            db.register_user(worker, f"w{i}", f"W{i}")
            db.register_user(22000 + i, f"r{i}", f"R{i}", referred_by=worker)
            _start(worker, tid)
            out = ReferralClaimService.submit(worker, tid, f"seat-{i}")
            assert out.state == "pending"

        pending = TaskSubmissionStore.list_pending_claims_for_task(tid)
        assert len(pending) == len(workers)
        assert all(
            p.approval_status == db.SUBMISSION_APPROVAL_PENDING
            for p in pending
        )

        # Deciding one claim touches ONLY that claim.
        first = pending[0]
        ReferralApprovalService.decide(BUYER, tid, first.submission_id, True)
        still = TaskSubmissionStore.list_pending_claims_for_task(tid)
        assert len(still) == len(workers) - 1
        assert all(p.submission_id != first.submission_id for p in still)


# ══════════════════════════════════════════════════════════════════
# 5. Roadmap #5 (2): repeatable referral cycle — DEFERRED, pinned
# ══════════════════════════════════════════════════════════════════


class TestDeferredRepeatableCycleBoundary:
    def test_repeatable_rejection_has_zero_side_effects(self, env):
        """A repeatable referral definition is refused fail-closed:
        the claim raises BEFORE any write — no claim row, user_tasks
        unchanged, wallet/ledger untouched, the definition itself
        unchanged — and every retry behaves identically (the deferred
        cycle feature can never half-exist)."""
        tid = _make_task(
            _valid_task_data(),
            repeat_policy=db.REPEAT_POLICY_REPEATABLE,
            repeat_hours=24,
        )
        _start(WORKER, tid)
        before = _snapshot(WORKER, tid)

        with pytest.raises(ReferralClaimError, match="repeatable"):
            ReferralClaimService.submit(WORKER, tid, "cycle-key")
        assert _snapshot(WORKER, tid) == before

        # Retries with any key fail identically — still zero rows.
        for key in ("cycle-key-2", None):
            with pytest.raises(ReferralClaimError, match="repeatable"):
                ReferralClaimService.submit(WORKER, tid, key)
        assert _claim_rows(tid) == []
        assert _wallet_units(WORKER) == 0
        assert _task_credits(WORKER) == []
        assert _status(WORKER, tid) == db.USER_TASK_STATUS_STARTED
        assert db.get_task(tid)["repeat_policy"] == (
            db.REPEAT_POLICY_REPEATABLE
        )


# ══════════════════════════════════════════════════════════════════
# 6. Roadmap #5 (3): complaints / arbitration — DEFERRED, pinned
# ══════════════════════════════════════════════════════════════════


class TestDeferredDisputeBoundary:
    def test_approval_vocabulary_is_exactly_three_states(self):
        """No dispute/arbitration state exists or can be named: the
        whole approval vocabulary is (pending, approved, rejected)."""
        assert db.SUBMISSION_APPROVAL_STATES == (
            db.SUBMISSION_APPROVAL_PENDING,
            db.SUBMISSION_APPROVAL_APPROVED,
            db.SUBMISSION_APPROVAL_REJECTED,
        )
        assert set(db.SUBMISSION_APPROVAL_STATES) == {
            "pending", "approved", "rejected",
        }

    @pytest.mark.parametrize(
        "state",
        [
            "complaint_pending",
            "complaint_open",
            "complaint",
            "disputed",
            "dispute_open",
            "under_review",
            "arbitration",
            "arbitration_pending",
            "escalated",
            "refunded",
        ],
    )
    def test_store_rejects_dispute_shaped_states(self, env, state):
        """The decision CAS writes approved/rejected ONLY — every
        complaint/arbitration/refund-shaped state is refused before
        any SQL runs (no complaint system can grow by stealth)."""
        with pytest.raises(ValueError):
            TaskSubmissionStore.mark_approval_decision(1, state, BUYER)


# ══════════════════════════════════════════════════════════════════
# 7. Source guards — nothing bypasses the referral services
# ══════════════════════════════════════════════════════════════════


def _stripped_source(module) -> str:
    """Module source WITHOUT docstrings or comments — so word bans
    bind executable code only, never the documented-gap notes."""
    with open(module.__file__, encoding="utf-8") as fh:
        src = fh.read()
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"'''[\s\S]*?'''", "", src)
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


class TestReferralSourceGuards:
    def test_routes_delegate_claims_and_decisions_to_services(self):
        """The HTTP layer only dispatches: it never opens claims or
        CASes decisions itself (idempotency belongs to the
        service/database, never to the interface)."""
        with open("task_routes.py", encoding="utf-8") as fh:
            src = fh.read()
        assert "ReferralClaimService.submit" in src
        assert "ReferralApprovalService.decide" in src
        assert "create_approval_claim" not in src
        assert "mark_approval_decision" not in src

    def test_referral_service_never_writes_state_or_money_tables(self):
        """referral_task.py contains NO SQL mutation and never names
        wallet/ledger primitives — money moves only through
        CompletionGate → TaskRewardService inside the gate's
        transaction (the service cannot bypass that path)."""
        src = _stripped_source(referral_task)
        assert not re.search(r"\bINSERT\b|\bUPDATE\b|\bDELETE\b", src, re.I)
        for token in ("wallet", "LedgerService", "credit_units",
                      "available_units"):
            assert token not in src, token

    def test_referral_service_has_no_admin_or_async_bypass(self):
        """Authorization is the server-side definition approver only
        (no is_admin fallback in the service), and no asyncio task
        primitive exists on this path at all."""
        src = _stripped_source(referral_task)
        assert "is_admin" not in src
        assert "asyncio" not in src
        assert "create_task" not in src
        # Sanity: we stripped the right module.
        assert "task_approver_user_id" in src

    def test_referral_service_contains_no_partial_deferred_feature(self):
        """None of the three deferred Roadmap #5 features exists even
        partially in executable code: no seat/capacity/quantity/
        reservation vocabulary, no dispute/arbitration/complaint/
        refund vocabulary — the documented gaps are docstring-only."""
        src = _stripped_source(referral_task)
        pattern = (
            r"\bseats?\b|\bcapacit\w*\b|\bquantity\b|\breserv\w*\b"
            r"|\bquota\b|\bdisput\w*\b|\barbitrat\w*\b"
            r"|\bcomplaint\w*\b|\brefund\w*\b"
        )
        match = re.search(pattern, src, re.I)
        assert match is None, f"deferred vocabulary in code: {match and match.group(0)}"
