"""
Focused tests — task reward settlement (MT-REWARD-01)
======================================================

Covers PART 21 requirements:

Reward calculation
- exact Decimal/USDT-unit conversion (int reward = whole USDT)
- invalid reward rejected (negative / smuggled float), completion rolled back
- no float anywhere in the settlement source (AST scan)

Successful completion
- task completes, wallet increases exactly by the reward, held unchanged
- exactly one ledger credit; ledger amount == wallet delta
- deterministic metadata with the reward snapshot; reference_type="task"
- stable idempotency key tied to the completion's submission identity

Failed / error verification
- no completion, no wallet mutation, no ledger credit

Already completed
- no second credit

Repeatable
- one credit per legitimate cycle, distinct ledger identity per cycle
- direct CompletionGate cycles (no submission rows) also stay distinct

Existing reward entry
- an already-recorded reward for the cycle is returned, wallet not credited again

Rollback
- wallet failure / ledger failure / completion failure each leave
  user_tasks, wallets, ledger and submission history untouched

Concurrency
- simultaneous completions → exactly one credit
- simultaneous same-idempotency-key retries → one logical reward
- simultaneous different-key attempts → one credit

Run:
    python3 -m pytest test_task_reward.py -v
"""

import ast
import inspect
import json
import sqlite3
import threading
from decimal import Decimal

import pytest

import db
import task_reward as task_reward_module
import wallet
from ledger import LedgerService
from task_completion import (
    CompletionGate,
    CompletionGateError,
    VerificationResult,
    VerificationStatus,
)
from task_lifecycle import TaskLifecycle
from task_reward import TaskRewardService, TaskRewardError
from task_start import TaskStartGate
from task_submission import TaskSubmissionService
from task_submission_store import TaskSubmissionStore
from task_verifier import (
    DeterministicTaskVerifier,
    clear_verifiers,
    register_verifier,
)
from wallet import USDT_SCALE, decimal_to_units, units_to_decimal

USER = 71
REWARD_USDT = 50
REWARD_UNITS = REWARD_USDT * USDT_SCALE          # 5,000,000,000

PASSED = VerificationResult(status=VerificationStatus.PASSED)


# ── Verifiers ──────────────────────────────────────────────────────────


class _PassVerifier(DeterministicTaskVerifier):
    def verify(self, context):
        return VerificationResult(status=VerificationStatus.PASSED)


class _FailVerifier(DeterministicTaskVerifier):
    def verify(self, context):
        return VerificationResult(
            status=VerificationStatus.FAILED, reason="not subscribed"
        )


class _ErrorVerifier(DeterministicTaskVerifier):
    def verify(self, context):
        return VerificationResult(
            status=VerificationStatus.ERROR, reason="api timeout"
        )


# ── Fixtures / helpers ─────────────────────────────────────────────────


@pytest.fixture
def path(monkeypatch, tmp_path):
    db_path = str(tmp_path / "task_reward_test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER, "rewarder", "Rewarder")
    clear_verifiers()
    register_verifier("deterministic", _PassVerifier())
    yield db_path
    # Restore the default registry entry so this file never leaks a
    # custom verifier into other suites.
    clear_verifiers()
    register_verifier("deterministic", DeterministicTaskVerifier())


def _task(reward=REWARD_USDT, repeatable=False, expected="secret123") -> int:
    kwargs = {}
    if repeatable:
        kwargs = {"repeat_policy": "repeatable", "repeat_hours": 1}
    return db.create_task(
        "Reward Task", "Complete it", "deterministic", reward,
        task_data=json.dumps({"expected": expected}), **kwargs,
    )


def _start(tid: int) -> None:
    TaskStartGate().start(USER, tid)


def _submit(tid: int, key: str, actual: str = "secret123"):
    return TaskLifecycle().submit_task(
        USER, tid, {"actual": actual}, key
    )


def _backdate(tid: int, hours: int = 2) -> None:
    """Move the persisted completed_at into the past (server-side data)."""
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE user_tasks SET completed_at = datetime('now', ?) "
            "WHERE user_id = ? AND task_id = ?",
            (f"-{hours} hours", USER, tid),
        )


def _wallet_rows() -> list[dict]:
    with db.get_connection() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT available_units, held_units FROM wallets "
                "WHERE user_id = ?",
                (USER,),
            ).fetchall()
        ]


def _wallet_count() -> int:
    return len(_wallet_rows())


def _ledger_rows() -> list[dict]:
    with db.get_connection() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM ledger WHERE user_id = ? ORDER BY id",
                (USER,),
            ).fetchall()
        ]


# ════════════════════════════════════════════════════════════════════
# Reward calculation
# ════════════════════════════════════════════════════════════════════


class TestRewardCalculation:
    def test_reward_is_whole_usdt_converted_exactly(self, path):
        """Integer task rewards are whole USDT via the existing
        decimal_to_units convention — exact, never rounded."""
        assert decimal_to_units(REWARD_USDT) == REWARD_UNITS
        assert units_to_decimal(REWARD_UNITS) == Decimal("50")

        tid = _task(reward=50)
        assert (
            TaskRewardService.reward_units(db.get_task(tid))
            == 50 * USDT_SCALE
        )
        tid_small = _task(reward=1)
        assert (
            TaskRewardService.reward_units(db.get_task(tid_small))
            == USDT_SCALE
        )
        assert TaskRewardService.reward_units({"id": 1, "reward": 0}) == 0

    def test_invalid_reward_rejected_never_coerced(self, path):
        """Negative, float, bool, None and over-precision rewards are
        rejected — never silently reinterpreted as money."""
        for bad in (-1, 1.5, True, None, "abc", Decimal("0.000000001")):
            with pytest.raises(TaskRewardError):
                TaskRewardService.reward_units({"id": 1, "reward": bad})
        with pytest.raises(TaskRewardError):
            TaskRewardService.reward_units(None)

    def test_negative_reward_rolls_back_completion(self, path):
        """A negative reward smuggled past application validation makes
        the whole completion roll back: not completed, no money."""
        tid = _task(reward=10)
        _start(tid)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET reward = -10 WHERE id = ?", (tid,)
            )
        with pytest.raises(TaskRewardError):
            CompletionGate().complete(USER, tid, PASSED)
        row = db.get_user_task(USER, tid)
        assert row["status"] == db.USER_TASK_STATUS_STARTED
        assert row["completed_at"] is None
        assert _wallet_count() == 0
        assert _ledger_rows() == []

    def test_float_reward_rolls_back_completion(self, path):
        """A REAL value smuggled into tasks.reward is rejected (no
        float money) and nothing is written."""
        tid = _task(reward=10)
        _start(tid)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET reward = 1.5 WHERE id = ?", (tid,)
            )
        task = db.get_task(tid)
        assert isinstance(task["reward"], float)   # stored REAL
        with pytest.raises(TaskRewardError):
            CompletionGate().complete(USER, tid, PASSED)
        row = db.get_user_task(USER, tid)
        assert row["status"] == db.USER_TASK_STATUS_STARTED
        assert _wallet_count() == 0
        assert _ledger_rows() == []

    def test_source_contains_no_float_and_uses_existing_conversion(self):
        """The settlement source has no float literal / float() / float
        name and delegates conversion to the existing wallet helper."""
        source = inspect.getsource(task_reward_module)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(
                node.value, float
            ):
                pytest.fail(f"float literal at line {node.lineno}")
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Name
            ) and node.func.id == "float":
                pytest.fail(f"float() call at line {node.lineno}")
            if isinstance(node, ast.Name) and node.id == "float":
                pytest.fail(f"float reference at line {node.lineno}")
        assert "decimal_to_units" in source
        assert "import withdrawal_rules" not in source
        assert "from withdrawal_rules" not in source


# ════════════════════════════════════════════════════════════════════
# Successful completion
# ════════════════════════════════════════════════════════════════════


class TestSuccessfulSettlement:
    def test_passed_completion_credits_exactly_once(self, path):
        """PASSED → completed + wallet credited exactly by the reward +
        exactly one ledger credit whose amount equals the wallet delta."""
        tid = _task(reward=REWARD_USDT)
        _start(tid)
        result = _submit(tid, "pay1")
        assert result.passed
        assert (
            db.get_user_task(USER, tid)["status"]
            == db.USER_TASK_STATUS_COMPLETED
        )

        wallet_rows = _wallet_rows()
        assert len(wallet_rows) == 1
        row = wallet_rows[0]
        assert row["available_units"] == REWARD_UNITS
        assert row["held_units"] == 0
        assert units_to_decimal(row["available_units"]) == Decimal("50")

        entries = _ledger_rows()
        assert len(entries) == 1
        entry = entries[0]
        sub = TaskSubmissionStore.get_by_idempotency_key(USER, tid, "pay1")
        expected_ref = (
            f"task_reward:{USER}:{tid}:submission:{sub.submission_id}"
        )
        assert entry["entry_type"] == "credit"
        assert entry["amount_units"] == REWARD_UNITS
        assert entry["available_delta"] == REWARD_UNITS
        assert entry["held_delta"] == 0
        assert entry["currency"] == "USDT"
        assert entry["reference_type"] == "task"
        # ledger amount is exactly the wallet delta (no fees/rounding)
        assert entry["available_delta"] == row["available_units"]
        # stable identity: reference and idempotency key are the same
        # deterministic string, free of whitespace
        assert entry["reference_id"] == expected_ref
        assert entry["idempotency_key"] == expected_ref
        assert (
            entry["idempotency_key"].strip() == entry["idempotency_key"]
        )

    def test_metadata_carries_deterministic_reward_snapshot(self, path):
        """Ledger metadata is deterministic JSON with the auditable
        reward snapshot and completion identity — no secrets."""
        tid = _task(reward=REWARD_USDT)
        _start(tid)
        _submit(tid, "meta1")
        entry = _ledger_rows()[0]
        meta = json.loads(entry["metadata"])
        sub = TaskSubmissionStore.get_by_idempotency_key(USER, tid, "meta1")
        assert meta == {
            "attempt_number": 1,
            "cycle": f"submission:{sub.submission_id}",
            "reward": REWARD_USDT,
            "reward_units": REWARD_UNITS,
            "submission_id": sub.submission_id,
            "task_id": tid,
            "user_id": USER,
        }
        # deterministic serialization (sorted keys, compact separators)
        assert entry["metadata"] == json.dumps(
            meta, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    def test_recorded_reward_survives_task_definition_change(self, path):
        """A later task-definition change never alters an existing
        ledger entry or its metadata snapshot."""
        tid = _task(reward=REWARD_USDT)
        _start(tid)
        _submit(tid, "snap")
        before = _ledger_rows()
        assert len(before) == 1

        assert db.update_task(tid, reward=999) is True

        after = _ledger_rows()
        assert after == before
        assert after[0]["amount_units"] == REWARD_UNITS
        assert json.loads(after[0]["metadata"])["reward"] == REWARD_USDT

    def test_existing_reward_entry_prevents_second_credit(self, path):
        """If the cycle's ledger entry already exists, the completion
        proceeds but the wallet is NOT credited a second time."""
        tid = _task(reward=10)
        _start(tid)
        TaskSubmissionService.submit(
            USER, tid, {"actual": "secret123"}, "guard-key"
        )
        sub = TaskSubmissionStore.get_by_idempotency_key(
            USER, tid, "guard-key"
        )
        key = f"task_reward:{USER}:{tid}:submission:{sub.submission_id}"
        # Simulate that this completion's reward was already recorded.
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO ledger (user_id, entry_type, amount_units, "
                " available_delta, held_delta, currency, reference_type, "
                " reference_id, idempotency_key) "
                "VALUES (?, 'credit', 1000000000, 1000000000, 0, 'USDT', "
                " 'task', ?, ?)",
                (USER, key, key),
            )

        CompletionGate().complete(USER, tid, PASSED)

        assert (
            db.get_user_task(USER, tid)["status"]
            == db.USER_TASK_STATUS_COMPLETED
        )
        assert _wallet_count() == 0, (
            "existing reward entry → wallet must not be credited again"
        )
        entries = _ledger_rows()
        assert len(entries) == 1
        assert entries[0]["idempotency_key"] == key

    def test_already_completed_never_credits_twice(self, path):
        """A completed task can never produce a second credit."""
        tid = _task(reward=REWARD_USDT)
        _start(tid)
        _submit(tid, "once")
        wallet_before = _wallet_rows()
        ledger_before = _ledger_rows()
        assert len(ledger_before) == 1

        with pytest.raises(CompletionGateError):
            CompletionGate().complete(USER, tid, PASSED)
        assert _wallet_rows() == wallet_before
        assert _ledger_rows() == ledger_before

        # The lifecycle refuses a second attempt outright as well.
        again = _submit(tid, "twice")
        assert not again.passed
        assert _wallet_rows() == wallet_before
        assert _ledger_rows() == ledger_before

    def test_zero_reward_completes_without_credit(self, path):
        """A reward of 0 completes the task but credits nothing (the
        ledger only accepts positive amounts)."""
        tid = _task(reward=0)
        _start(tid)
        result = _submit(tid, "zero")
        assert result.passed
        assert (
            db.get_user_task(USER, tid)["status"]
            == db.USER_TASK_STATUS_COMPLETED
        )
        assert _wallet_count() == 0
        assert _ledger_rows() == []


# ════════════════════════════════════════════════════════════════════
# Failed / error verification — zero reward
# ════════════════════════════════════════════════════════════════════


class TestNoCreditOnNonPassed:
    def test_failed_verification_no_completion_no_credit(self, path):
        clear_verifiers()
        register_verifier("deterministic", _FailVerifier())
        tid = _task(reward=REWARD_USDT)
        _start(tid)
        result = _submit(tid, "f1", actual="wrong")
        assert result.status == VerificationStatus.FAILED

        row = db.get_user_task(USER, tid)
        assert row["status"] == db.USER_TASK_STATUS_STARTED
        assert row["completed_at"] is None
        assert _wallet_count() == 0
        assert _ledger_rows() == []
        rec = TaskSubmissionStore.get_by_idempotency_key(USER, tid, "f1")
        assert rec.status == "failed"          # history stays consistent

    def test_error_verification_no_completion_no_credit(self, path):
        clear_verifiers()
        register_verifier("deterministic", _ErrorVerifier())
        tid = _task(reward=REWARD_USDT)
        _start(tid)
        result = _submit(tid, "e1")
        assert result.status == VerificationStatus.ERROR

        row = db.get_user_task(USER, tid)
        assert row["status"] == db.USER_TASK_STATUS_STARTED
        assert row["completed_at"] is None
        assert _wallet_count() == 0
        assert _ledger_rows() == []
        rec = TaskSubmissionStore.get_by_idempotency_key(USER, tid, "e1")
        assert rec.status == "error"


# ════════════════════════════════════════════════════════════════════
# Repeatable tasks — one reward per legitimate cycle
# ════════════════════════════════════════════════════════════════════


class TestRepeatableCycles:
    def test_each_cycle_credited_once_with_distinct_identity(self, path):
        tid = _task(reward=10, repeatable=True)
        _start(tid)
        _submit(tid, "c1")
        first = _ledger_rows()
        assert len(first) == 1
        assert _wallet_rows()[0]["available_units"] == 10 * USDT_SCALE

        # cooldown elapsed (repeat_hours=1), new cycle completes again
        _backdate(tid, hours=2)
        _start(tid)
        _submit(tid, "c2")

        rows = _ledger_rows()
        assert len(rows) == 2
        assert rows[0]["reference_id"] != rows[1]["reference_id"]
        assert rows[0]["idempotency_key"] != rows[1]["idempotency_key"]
        for entry in rows:
            assert entry["amount_units"] == 10 * USDT_SCALE
            assert entry["reference_type"] == "task"
            assert entry["reference_id"].startswith(
                f"task_reward:{USER}:{tid}:submission:"
            )

        passed = [
            r for r in TaskSubmissionStore.list_user_task_submissions(
                USER, tid
            )
            if r.status == "passed"
        ]
        assert len(passed) == 2
        assert rows[0]["reference_id"].endswith(
            f"submission:{passed[0].submission_id}"
        )
        assert rows[1]["reference_id"].endswith(
            f"submission:{passed[1].submission_id}"
        )

        wallet = _wallet_rows()[0]
        assert wallet["available_units"] == 20 * USDT_SCALE
        assert wallet["held_units"] == 0

    def test_direct_gate_cycles_have_distinct_identity(self, path):
        """Direct CompletionGate completions (no submission records)
        still get a distinct, stable identity per cycle."""
        tid = _task(reward=10, repeatable=True)
        _start(tid)
        CompletionGate().complete(USER, tid, PASSED)
        _backdate(tid, hours=2)
        _start(tid)
        CompletionGate().complete(USER, tid, PASSED)

        rows = _ledger_rows()
        assert len(rows) == 2
        assert rows[0]["reference_id"] == f"task_reward:{USER}:{tid}:cycle:1"
        assert rows[1]["reference_id"] == f"task_reward:{USER}:{tid}:cycle:2"
        assert rows[0]["idempotency_key"] != rows[1]["idempotency_key"]
        assert _wallet_rows()[0]["available_units"] == 20 * USDT_SCALE
        assert TaskSubmissionStore.count_attempts(USER, tid) == 0


# ════════════════════════════════════════════════════════════════════
# Rollback — every financial failure leaves no partial state
# ════════════════════════════════════════════════════════════════════


class TestRollback:
    def test_wallet_failure_rolls_back_everything(self, path, monkeypatch):
        tid = _task(reward=REWARD_USDT)
        _start(tid)

        def boom(user_id, amount_units, *, connection=None):
            raise wallet.WalletError("simulated wallet failure")

        monkeypatch.setattr(wallet, "credit_units", boom)
        result = _submit(tid, "rb1")

        assert result.status == VerificationStatus.ERROR
        assert "Completion gate failed" in result.reason
        assert "simulated wallet failure" in result.reason
        row = db.get_user_task(USER, tid)
        assert row["status"] == db.USER_TASK_STATUS_STARTED
        assert row["completed_at"] is None
        # ensure_wallet ran inside the transaction — rolled back too
        assert _wallet_count() == 0
        assert _ledger_rows() == []
        # submission history remains consistent
        rec = TaskSubmissionStore.get_by_idempotency_key(USER, tid, "rb1")
        assert rec.status == "passed"
        assert rec.completed_at is None

    def test_ledger_failure_rolls_back_wallet_credit(
        self, path, monkeypatch
    ):
        tid = _task(reward=REWARD_USDT)
        _start(tid)
        TaskSubmissionService.submit(
            USER, tid, {"actual": "secret123"}, "ledger-key"
        )
        # A pre-existing balance must survive the rollback untouched.
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO wallets (user_id, available_units, held_units) "
                "VALUES (?, 700000000, 0)",
                (USER,),
            )

        def boom(self, *args, **kwargs):
            raise sqlite3.OperationalError("simulated ledger failure")

        monkeypatch.setattr(LedgerService, "record_credit", boom)
        with pytest.raises(sqlite3.OperationalError):
            CompletionGate().complete(USER, tid, PASSED)

        # completion, wallet credit and ledger insert all rolled back
        row = db.get_user_task(USER, tid)
        assert row["status"] == db.USER_TASK_STATUS_STARTED
        assert row["completed_at"] is None
        wallet_rows = _wallet_rows()
        assert len(wallet_rows) == 1
        assert wallet_rows[0]["available_units"] == 700000000
        assert wallet_rows[0]["held_units"] == 0
        assert _ledger_rows() == []
        rec = TaskSubmissionStore.get_by_idempotency_key(
            USER, tid, "ledger-key"
        )
        assert rec.status == "passed"
        assert rec.completed_at is None

    def test_completion_failure_writes_no_money(self, path, monkeypatch):
        """If the completion transition cannot happen, the money code
        is never even reached."""
        tid = _task(reward=REWARD_USDT)
        db.create_user_task(USER, tid)          # 'available', never started

        calls: list[int] = []
        original = wallet.credit_units

        def spy(user_id, amount_units, *, connection=None):
            calls.append(amount_units)
            return original(user_id, amount_units, connection=connection)

        monkeypatch.setattr(wallet, "credit_units", spy)
        with pytest.raises(CompletionGateError):
            CompletionGate().complete(USER, tid, PASSED)

        assert calls == []
        assert (
            db.get_user_task(USER, tid)["status"]
            == db.USER_TASK_STATUS_AVAILABLE
        )
        assert _wallet_count() == 0
        assert _ledger_rows() == []


# ════════════════════════════════════════════════════════════════════
# Concurrency — exactly one credit per logical reward
# ════════════════════════════════════════════════════════════════════


class TestConcurrency:
    def _run_two(self, target, *args):
        barrier = threading.Barrier(2)
        results: list = []
        errors: list = []

        def wrapper(*a):
            try:
                barrier.wait(timeout=30)
                results.append(target(*a))
            except BaseException as exc:    # noqa: BLE001 — collected
                errors.append(exc)

        threads = [
            threading.Thread(target=wrapper, args=(arg,)) for arg in args
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads), "thread hung"
        return results, errors

    def test_simultaneous_gate_completions_credit_exactly_once(self, path):
        tid = _task(reward=REWARD_USDT)
        _start(tid)

        def attempt(_tag):
            CompletionGate().complete(USER, tid, PASSED)
            return "success"

        results, errors = self._run_two(attempt, "a", "b")
        rejected = [
            e for e in errors if isinstance(e, CompletionGateError)
        ]
        assert results.count("success") == 1
        assert len(rejected) == 1            # the loser is rejected
        assert errors == rejected            # no unexpected crashes

        wallet_rows = _wallet_rows()
        assert len(wallet_rows) == 1
        assert wallet_rows[0]["available_units"] == REWARD_UNITS
        assert wallet_rows[0]["held_units"] == 0
        entries = _ledger_rows()
        assert len(entries) == 1
        assert entries[0]["amount_units"] == REWARD_UNITS
        assert (
            db.get_user_task(USER, tid)["status"]
            == db.USER_TASK_STATUS_COMPLETED
        )

    def test_simultaneous_same_key_retries_credit_once(self, path):
        tid = _task(reward=REWARD_USDT)
        _start(tid)

        def attempt(_tag):
            return TaskLifecycle().submit_task(
                USER, tid, {"actual": "secret123"}, "race-key"
            )

        results, errors = self._run_two(attempt, "a", "b")
        assert errors == []
        assert len(results) == 2
        assert any(r.passed for r in results)
        # one logical reward: one submission record, one credit
        assert TaskSubmissionStore.count_attempts(USER, tid) == 1
        assert (
            db.get_user_task(USER, tid)["status"]
            == db.USER_TASK_STATUS_COMPLETED
        )
        assert _wallet_rows()[0]["available_units"] == REWARD_UNITS
        assert len(_ledger_rows()) == 1

    def test_simultaneous_different_key_attempts_credit_once(self, path):
        tid = _task(reward=REWARD_USDT)
        _start(tid)

        def attempt(key):
            return TaskLifecycle().submit_task(
                USER, tid, {"actual": "secret123"}, key
            )

        results, errors = self._run_two(attempt, "race-a", "race-b")
        assert errors == []
        assert len(results) == 2
        passed = [r for r in results if r.passed]
        assert len(passed) == 1              # exactly one winner
        assert TaskSubmissionStore.count_attempts(USER, tid) == 2
        assert (
            db.get_user_task(USER, tid)["status"]
            == db.USER_TASK_STATUS_COMPLETED
        )
        assert _wallet_rows()[0]["available_units"] == REWARD_UNITS
        entries = _ledger_rows()
        assert len(entries) == 1
        assert entries[0]["amount_units"] == REWARD_UNITS
