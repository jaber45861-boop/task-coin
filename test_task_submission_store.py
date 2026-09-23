"""
Focused tests — submission store & service persistence (MT-TASK-04)
===================================================================

Covers PART 14 store/service requirements:

Store:
- create submission / retrieve / list user+task submissions
- same idempotency key returns the original (DB-enforced)
- conflicting idempotency context never leaks another record
- failed / error / passed submissions are all persisted

Service:
- persistence occurs for production submissions
- verifier called once for a new key; NOT called again on replay
- FAILED does not complete, ERROR does not complete, PASSED completes
- submission records remain after completion (audit trail survives)
- no wallet mutation, no ledger mutation (PART 16)

Run:
    python3 -m pytest test_task_submission_store.py -v
"""

import json

import pytest

import db
from completion_bridge import CompletionBridge
from task_completion import VerificationResult, VerificationStatus
from task_lifecycle import TaskLifecycle
from task_start import TaskStartGate, StartGateError
from task_submission import (
    IdempotentReplayError,
    SubmissionError,
    TaskSubmissionService,
)
from task_submission_store import TaskSubmissionStore
from task_verifier import (
    DeterministicTaskVerifier,
    clear_verifiers,
    register_verifier,
)

USER = 7
OTHER_USER = 8


# ── Fake verifiers (side-effect free, call-counted) ───────────────────


class _PassVerifier(DeterministicTaskVerifier):
    def __init__(self):
        self.calls = 0

    def verify(self, context):
        self.calls += 1
        return VerificationResult(status=VerificationStatus.PASSED)


class _FailVerifier(DeterministicTaskVerifier):
    def __init__(self):
        self.calls = 0

    def verify(self, context):
        self.calls += 1
        return VerificationResult(
            status=VerificationStatus.FAILED, reason="not subscribed"
        )


class _ErrorVerifier(DeterministicTaskVerifier):
    def __init__(self):
        self.calls = 0

    def verify(self, context):
        self.calls += 1
        return VerificationResult(
            status=VerificationStatus.ERROR, reason="api timeout"
        )


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def path(monkeypatch, tmp_path):
    db_path = str(tmp_path / "store_test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER, "alice", "Alice")
    db.register_user(OTHER_USER, "bob", "Bob")
    yield db_path
    clear_verifiers()


@pytest.fixture
def verifier():
    """Deterministic verifier the tests can inspect; registers itself."""
    v = _PassVerifier()
    clear_verifiers()
    register_verifier("deterministic", v)
    return v


def _task(repeatable: bool = False) -> int:
    kwargs = {}
    if repeatable:
        kwargs = {"repeat_policy": "repeatable", "repeat_hours": 24}
    return db.create_task(
        "Enter Code", "Enter the correct code", "deterministic", 100,
        task_data=json.dumps({"expected": "secret123"}), **kwargs,
    )


def _wallet_ledger_counts(user_id: int) -> tuple[int, int]:
    with db.get_connection() as conn:
        wallets = conn.execute(
            "SELECT COUNT(*) AS c FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()["c"]
        ledger = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger WHERE user_id = ?",
            (user_id,),
        ).fetchone()["c"]
    return wallets, ledger


# ════════════════════════════════════════════════════════════════════
# Store basics
# ════════════════════════════════════════════════════════════════════


class TestStoreBasics:
    def test_create_submission(self, path):
        tid = _task()
        record, created = TaskSubmissionStore.create_submission(
            USER, tid, "key-1"
        )
        assert created is True
        assert record.submission_id > 0
        assert record.user_id == USER
        assert record.task_id == tid
        assert record.attempt_number == 1
        assert record.status == "submitted"
        assert record.idempotency_key == "key-1"
        assert record.submitted_at is not None
        assert record.created_at is not None
        assert record.completed_at is None
        assert record.is_terminal is False

    def test_second_key_increments_attempt(self, path):
        tid = _task()
        TaskSubmissionStore.create_submission(USER, tid, "k1")
        record2, created2 = TaskSubmissionStore.create_submission(
            USER, tid, "k2"
        )
        assert created2 is True
        assert record2.attempt_number == 2

    def test_same_key_returns_original(self, path):
        tid = _task()
        first, created1 = TaskSubmissionStore.create_submission(
            USER, tid, "same"
        )
        second, created2 = TaskSubmissionStore.create_submission(
            USER, tid, "same"
        )
        assert created1 is True
        assert created2 is False, "DB must enforce idempotency uniqueness"
        assert second.submission_id == first.submission_id
        assert second.attempt_number == first.attempt_number

    def test_conflicting_context_never_returns_foreign_record(self, path):
        """A key used by (USER, task) is invisible to another user or
        another task — context confusion is impossible."""
        tid = _task()
        TaskSubmissionStore.create_submission(USER, tid, "shared")
        assert (
            TaskSubmissionStore.get_by_idempotency_key(
                OTHER_USER, tid, "shared"
            )
            is None
        )
        other_tid = _task()
        assert (
            TaskSubmissionStore.get_by_idempotency_key(
                USER, other_tid, "shared"
            )
            is None
        )
        assert (
            TaskSubmissionStore.get_by_idempotency_key(
                USER, tid, "other-key"
            )
            is None
        )

    def test_retrieve_list_and_count(self, path):
        tid = _task()
        r1, _ = TaskSubmissionStore.create_submission(USER, tid, "a")
        r2, _ = TaskSubmissionStore.create_submission(USER, tid, "b")
        TaskSubmissionStore.create_submission(OTHER_USER, tid, "c")

        fetched = TaskSubmissionStore.get_submission(r1.submission_id)
        assert fetched.submission_id == r1.submission_id

        history = TaskSubmissionStore.list_user_task_submissions(USER, tid)
        assert [r.submission_id for r in history] == [
            r1.submission_id, r2.submission_id,
        ]
        assert TaskSubmissionStore.count_attempts(USER, tid) == 2
        assert TaskSubmissionStore.count_attempts(OTHER_USER, tid) == 1
        assert len(TaskSubmissionStore.list_user_submissions(USER)) == 2

    def test_terminal_transition_is_cas(self, path):
        tid = _task()
        record, _ = TaskSubmissionStore.create_submission(USER, tid, "x")
        updated = TaskSubmissionStore.record_verification_result(
            record.submission_id, "failed", "nope"
        )
        assert updated.status == "failed"
        assert updated.verification_reason == "nope"
        # A second writer can never overwrite the stored outcome.
        overwrite = TaskSubmissionStore.record_verification_result(
            record.submission_id, "passed", "later"
        )
        assert overwrite is None
        assert TaskSubmissionStore.get_submission(
            record.submission_id
        ).status == "failed"

    def test_invalid_terminal_status_rejected(self, path):
        tid = _task()
        record, _ = TaskSubmissionStore.create_submission(USER, tid, "y")
        with pytest.raises(ValueError):
            TaskSubmissionStore.record_verification_result(
                record.submission_id, "pending"
            )

    def test_stamp_completion_targets_the_keyed_record(self, path):
        tid = _task()
        passed, _ = TaskSubmissionStore.create_submission(USER, tid, "win")
        TaskSubmissionStore.record_verification_result(
            passed.submission_id, "passed"
        )
        other, _ = TaskSubmissionStore.create_submission(USER, tid, "lose")
        TaskSubmissionStore.record_verification_result(
            other.submission_id, "passed"
        )

        assert TaskSubmissionStore.stamp_completion(USER, tid, "win") is True
        assert TaskSubmissionStore.get_submission(
            passed.submission_id
        ).completed_at is not None
        assert TaskSubmissionStore.get_submission(
            other.submission_id
        ).completed_at is None


# ════════════════════════════════════════════════════════════════════
# Service persistence
# ════════════════════════════════════════════════════════════════════


class TestServicePersistence:
    def test_persistence_occurs_for_new_key(self, path, verifier):
        tid = _task()
        TaskStartGate().start(USER, tid)
        result = TaskSubmissionService.submit(
            USER, tid, {"actual": "secret123"}, idempotency_key="p1"
        )
        assert result.passed
        history = TaskSubmissionStore.list_user_task_submissions(USER, tid)
        assert len(history) == 1
        assert history[0].status == "passed"
        assert history[0].idempotency_key == "p1"
        assert history[0].attempt_number == 1

    def test_failed_submission_persisted(self, path, verifier):
        verifier.calls = 0
        clear_verifiers()
        fail = _FailVerifier()
        register_verifier("deterministic", fail)
        tid = _task()
        TaskStartGate().start(USER, tid)

        result = TaskSubmissionService.submit(
            USER, tid, {"actual": "wrong"}, idempotency_key="f1"
        )
        assert result.status == VerificationStatus.FAILED
        history = TaskSubmissionStore.list_user_task_submissions(USER, tid)
        assert [r.status for r in history] == ["failed"]
        assert history[0].verification_reason == "not subscribed"
        assert db.get_user_task(USER, tid)["status"] == "started"

    def test_error_submission_persisted(self, path):
        clear_verifiers()
        register_verifier("deterministic", _ErrorVerifier())
        tid = _task()
        TaskStartGate().start(USER, tid)

        result = TaskSubmissionService.submit(
            USER, tid, {"actual": "x"}, idempotency_key="e1"
        )
        assert result.status == VerificationStatus.ERROR
        history = TaskSubmissionStore.list_user_task_submissions(USER, tid)
        assert [r.status for r in history] == ["error"]
        assert db.get_user_task(USER, tid)["status"] == "started"

    def test_verifier_not_registered_persists_error(self, path):
        clear_verifiers()  # no verifier registered at all
        tid = _task()
        TaskStartGate().start(USER, tid)
        result = TaskSubmissionService.submit(
            USER, tid, {"actual": "x"}, idempotency_key="e2"
        )
        assert result.status == VerificationStatus.ERROR
        history = TaskSubmissionStore.list_user_task_submissions(USER, tid)
        assert [r.status for r in history] == ["error"]

    def test_forbidden_payload_leaves_no_record(self, path, verifier):
        tid = _task()
        TaskStartGate().start(USER, tid)
        with pytest.raises(SubmissionError):
            TaskSubmissionService.submit(
                USER, tid,
                {"actual": "x", "reward": 999999, "status": "completed"},
                idempotency_key="bad",
            )
        assert TaskSubmissionStore.count_attempts(USER, tid) == 0

    def test_invalid_idempotency_key_rejected(self, path, verifier):
        tid = _task()
        TaskStartGate().start(USER, tid)
        for bad in ("", "   ", "has space", "x" * 200, 123, ["a"]):
            with pytest.raises(SubmissionError):
                TaskSubmissionService.submit(
                    USER, tid, {"actual": "x"}, idempotency_key=bad
                )
        assert TaskSubmissionStore.count_attempts(USER, tid) == 0

    def test_server_generates_key_when_absent(self, path, verifier):
        tid = _task()
        TaskStartGate().start(USER, tid)
        TaskSubmissionService.submit(USER, tid, {"actual": "secret123"})
        TaskSubmissionService.submit(USER, tid, {"actual": "secret123"})
        # Two calls without keys → two distinct attempts, never a
        # spurious replay collision.
        history = TaskSubmissionStore.list_user_task_submissions(USER, tid)
        assert len(history) == 2
        assert history[0].idempotency_key != history[1].idempotency_key


# ════════════════════════════════════════════════════════════════════
# Idempotent replay — the verifier runs exactly once per key
# ════════════════════════════════════════════════════════════════════


class TestIdempotentReplay:
    def test_replay_of_passed_key_skips_verifier(self, path, verifier):
        tid = _task()
        lifecycle = TaskLifecycle()
        lifecycle.start_task(USER, tid)

        first = lifecycle.submit_task(
            USER, tid, {"actual": "secret123"}, idempotency_key="r1"
        )
        assert first.passed
        assert verifier.calls == 1
        assert db.get_user_task(USER, tid)["status"] == "completed"

        # Replaying after completion returns the ORIGINAL result —
        # no policy error, no second Telegram/verifier call.
        second = lifecycle.submit_task(
            USER, tid, {"actual": "secret123"}, idempotency_key="r1"
        )
        assert second.passed
        assert verifier.calls == 1, "verifier must not run twice"
        assert TaskSubmissionStore.count_attempts(USER, tid) == 1

    def test_replay_of_failed_key_skips_verifier(self, path):
        clear_verifiers()
        fail = _FailVerifier()
        register_verifier("deterministic", fail)
        tid = _task()
        lifecycle = TaskLifecycle()
        lifecycle.start_task(USER, tid)

        first = lifecycle.submit_task(
            USER, tid, {"actual": "wrong"}, idempotency_key="r2"
        )
        assert first.status == VerificationStatus.FAILED
        assert fail.calls == 1

        second = lifecycle.submit_task(
            USER, tid, {"actual": "wrong"}, idempotency_key="r2"
        )
        assert second.status == VerificationStatus.FAILED
        assert second.reason == first.reason
        assert fail.calls == 1, "verifier must not run twice"
        assert TaskSubmissionStore.count_attempts(USER, tid) == 1

    def test_direct_service_replay_raises_with_original_result(
        self, path, verifier
    ):
        tid = _task()
        TaskStartGate().start(USER, tid)
        first = TaskSubmissionService.submit(
            USER, tid, {"actual": "secret123"}, idempotency_key="r3"
        )
        assert first.passed
        with pytest.raises(IdempotentReplayError) as exc:
            TaskSubmissionService.submit(
                USER, tid, {"actual": "secret123"}, idempotency_key="r3"
            )
        assert exc.value.result.passed
        assert verifier.calls == 1

    def test_different_keys_are_different_attempts(self, path):
        tid = _task()
        clear_verifiers()
        fail = _FailVerifier()
        register_verifier("deterministic", fail)
        lifecycle = TaskLifecycle()
        lifecycle.start_task(USER, tid)

        first = lifecycle.submit_task(
            USER, tid, {"actual": "wrong"}, idempotency_key="k-a"
        )
        assert first.status == VerificationStatus.FAILED
        assert fail.calls == 1

        # Task still started after FAILED → a NEW key may attempt again
        # (the operator swapped in a passing verifier meanwhile).
        clear_verifiers()
        passer = _PassVerifier()
        register_verifier("deterministic", passer)
        second = lifecycle.submit_task(
            USER, tid, {"actual": "secret123"}, idempotency_key="k-b"
        )
        assert second.passed
        assert passer.calls == 1
        history = TaskSubmissionStore.list_user_task_submissions(USER, tid)
        assert [r.status for r in history] == ["failed", "passed"]
        assert TaskSubmissionStore.count_attempts(USER, tid) == 2


# ════════════════════════════════════════════════════════════════════
# Outcome matrix + audit survival (PART 9 / 16)
# ════════════════════════════════════════════════════════════════════


class TestOutcomeMatrix:
    def test_passed_completes_and_stamps(self, path, verifier):
        tid = _task()
        lifecycle = TaskLifecycle()
        lifecycle.start_task(USER, tid)
        result = lifecycle.submit_task(
            USER, tid, {"actual": "secret123"}, idempotency_key="m1"
        )
        assert result.passed
        assert db.get_user_task(USER, tid)["status"] == "completed"
        record = TaskSubmissionStore.get_by_idempotency_key(
            USER, tid, "m1"
        )
        assert record.status == "passed"
        assert record.completed_at is not None, \
            "passed submission must identify its completion transition"

    def test_failed_does_not_complete(self, path):
        clear_verifiers()
        register_verifier("deterministic", _FailVerifier())
        tid = _task()
        lifecycle = TaskLifecycle()
        lifecycle.start_task(USER, tid)
        result = lifecycle.submit_task(
            USER, tid, {"actual": "no"}, idempotency_key="m2"
        )
        assert result.status == VerificationStatus.FAILED
        assert db.get_user_task(USER, tid)["status"] == "started"
        record = TaskSubmissionStore.get_by_idempotency_key(
            USER, tid, "m2"
        )
        assert record.status == "failed"
        assert record.completed_at is None

    def test_error_does_not_complete(self, path):
        clear_verifiers()
        register_verifier("deterministic", _ErrorVerifier())
        tid = _task()
        lifecycle = TaskLifecycle()
        lifecycle.start_task(USER, tid)
        result = lifecycle.submit_task(
            USER, tid, {"actual": "x"}, idempotency_key="m3"
        )
        assert result.status == VerificationStatus.ERROR
        assert db.get_user_task(USER, tid)["status"] == "started"
        record = TaskSubmissionStore.get_by_idempotency_key(
            USER, tid, "m3"
        )
        assert record.status == "error"
        assert record.completed_at is None

    def test_history_survives_failures_then_completion(self, path):
        clear_verifiers()
        fail = _FailVerifier()
        register_verifier("deterministic", fail)
        tid = _task()
        lifecycle = TaskLifecycle()
        lifecycle.start_task(USER, tid)
        lifecycle.submit_task(
            USER, tid, {"actual": "no"}, idempotency_key="h1"
        )
        lifecycle.submit_task(
            USER, tid, {"actual": "no"}, idempotency_key="h2"
        )
        # Operator swaps in a passing verifier for the final attempt —
        # just like the channel task when the user finally subscribes.
        clear_verifiers()
        register_verifier("deterministic", _PassVerifier())
        final = lifecycle.submit_task(
            USER, tid, {"actual": "secret123"}, idempotency_key="h3"
        )
        assert final.passed

        history = TaskSubmissionStore.list_user_task_submissions(USER, tid)
        assert [r.status for r in history] == [
            "failed", "failed", "passed",
        ], "failed attempts must never be erased"
        assert sum(1 for r in history if r.completed_at) == 1

    def test_records_remain_after_completion(self, path, verifier):
        tid = _task()
        lifecycle = TaskLifecycle()
        lifecycle.start_task(USER, tid)
        lifecycle.submit_task(
            USER, tid, {"actual": "secret123"}, idempotency_key="keep"
        )
        # Even though the task is terminal, the audit record survives.
        record = TaskSubmissionStore.get_by_idempotency_key(
            USER, tid, "keep"
        )
        assert record is not None
        assert record.status == "passed"
        assert TaskSubmissionStore.count_attempts(USER, tid) == 1

    def test_no_wallet_or_ledger_mutation(self, path, verifier):
        tid = _task()
        assert _wallet_ledger_counts(USER) == (0, 0)
        lifecycle = TaskLifecycle()
        lifecycle.start_task(USER, tid)
        lifecycle.submit_task(
            USER, tid, {"actual": "secret123"}, idempotency_key="fin"
        )
        assert db.get_user_task(USER, tid)["status"] == "completed"
        assert _wallet_ledger_counts(USER) == (0, 0), \
            "completion must never touch wallet/ledger (PART 16)"

    def test_bridge_replay_returns_without_gate_rerun(
        self, path, verifier
    ):
        """Direct bridge call: a replayed key never re-completes."""
        tid = _task()
        lifecycle = TaskLifecycle()
        lifecycle.start_task(USER, tid)
        lifecycle.submit_task(
            USER, tid, {"actual": "secret123"}, idempotency_key="b1"
        )
        row_before = db.get_user_task(USER, tid)
        result = CompletionBridge.complete_after_verification(
            USER, tid, {"actual": "secret123"}, "b1"
        )
        assert result.passed
        assert verifier.calls == 1
        assert db.get_user_task(USER, tid) == row_before
