"""
Tests for Task Verification Context Boundary (Micro-task 2.8).

Covers:
  - VerificationContext immutability (frozen dataclass)
  - FrozenDict/FrozenList mutation prevention
  - nested verification data cannot mutate the source
  - verifier receives a safe execution context
  - verifier cannot mutate original task verification data
  - verifier cannot mutate task/user state through context
  - reward/active metadata cannot be used as accidental completion authority
  - Deterministic verifier: same-type expected/actual → PASSED
  - Deterministic verifier: different same-type values → FAILED
  - Deterministic verifier: cross-type equal values → FAILED
  - Deterministic verifier: malformed data → ERROR
  - verify_task() returns VerificationResult
  - successful verification compatible with CompletionGate
  - failed/error verification cannot complete a task
  - verifier cannot directly complete a task

Run:
    python -m unittest test_verification_boundary -v
"""

import json
import os
import tempfile
import unittest

from config import CHANNELS
import db
from task_completion import (
    CompletionGate,
    CompletionGateError,
    VerificationResult,
    VerificationStatus,
)
from task_verifier import (
    DeterministicTaskVerifier,
    FrozenDict,
    FrozenList,
    VerificationContext,
    freeze_value,
    clear_verifiers,
    get_verifier,
    register_verifier,
    verify_task,
)


# ── FrozenDict Tests ──────────────────────────────────────────────


class TestFrozenDict(unittest.TestCase):
    """Tests for FrozenDict immutability."""

    def test_read_access(self):
        """FrozenDict supports dict-like read access."""
        fd = FrozenDict({"a": 1, "b": 2})
        self.assertEqual(fd["a"], 1)
        self.assertEqual(fd["b"], 2)
        self.assertEqual(len(fd), 2)

    def test_immutability_setitem(self):
        """FrozenDict rejects item assignment."""
        fd = FrozenDict({"a": 1})
        with self.assertRaises(TypeError):
            fd["a"] = 999  # type: ignore[index]

    def test_immutability_delitem(self):
        """FrozenDict rejects item deletion."""
        fd = FrozenDict({"a": 1})
        with self.assertRaises(TypeError):
            del fd["a"]

    def test_immutability_update(self):
        """FrozenDict rejects update()."""
        fd = FrozenDict({"a": 1})
        with self.assertRaises(TypeError):
            fd.update({"b": 2})

    def test_equality_with_dict(self):
        """FrozenDict equals a regular dict with same contents."""
        fd = FrozenDict({"x": 10})
        self.assertEqual(fd, {"x": 10})
        self.assertEqual(fd, FrozenDict({"x": 10}))

    def test_deep_copy_source(self):
        """FrozenDict deep-copies the source dict — mutating source doesn't affect it."""
        source = {"nested": {"inner": 42}}
        fd = FrozenDict(source)
        source["nested"]["inner"] = 999
        self.assertEqual(fd["nested"]["inner"], 42)

    def test_iteration(self):
        """FrozenDict supports iteration."""
        fd = FrozenDict({"a": 1, "b": 2})
        self.assertEqual(sorted(fd), ["a", "b"])

    def test_repr(self):
        """FrozenDict has a repr."""
        fd = FrozenDict({"key": "val"})
        self.assertIn("FrozenDict", repr(fd))

    def test_nested_frozen_dict(self):
        """Nested dicts inside FrozenDict are also frozen."""
        fd = FrozenDict({"nested": {"inner": 42}})
        self.assertIsInstance(fd["nested"], FrozenDict)
        with self.assertRaises(TypeError):
            fd["nested"]["inner"] = 999  # type: ignore[index]


# ── FrozenList Tests ──────────────────────────────────────────────


class TestFrozenList(unittest.TestCase):
    """Tests for FrozenList immutability."""

    def test_read_access(self):
        """FrozenList supports list-like read access."""
        fl = FrozenList([1, 2, 3])
        self.assertEqual(fl[0], 1)
        self.assertEqual(len(fl), 3)

    def test_immutability_setitem(self):
        """FrozenList rejects item assignment."""
        fl = FrozenList([1, 2])
        with self.assertRaises(TypeError):
            fl[0] = 999

    def test_immutability_delitem(self):
        """FrozenList rejects item deletion."""
        fl = FrozenList([1, 2])
        with self.assertRaises(TypeError):
            del fl[0]

    def test_immutability_append(self):
        """FrozenList rejects append()."""
        fl = FrozenList([1])
        with self.assertRaises(TypeError):
            fl.append(2)

    def test_immutability_extend(self):
        """FrozenList rejects extend()."""
        fl = FrozenList([1])
        with self.assertRaises(TypeError):
            fl.extend([2, 3])

    def test_immutability_pop(self):
        """FrozenList rejects pop()."""
        fl = FrozenList([1, 2])
        with self.assertRaises(TypeError):
            fl.pop()

    def test_equality_with_list(self):
        """FrozenList equals a regular list with same contents."""
        fl = FrozenList([1, 2])
        self.assertEqual(fl, [1, 2])
        self.assertEqual(fl, FrozenList([1, 2]))

    def test_deep_copy_source(self):
        """FrozenList deep-copies the source list."""
        source = [[1, 2], [3, 4]]
        fl = FrozenList(source)
        source[0][0] = 999
        self.assertEqual(fl[0][0], 1)

    def test_iteration(self):
        """FrozenList supports iteration."""
        fl = FrozenList([10, 20])
        self.assertEqual(list(fl), [10, 20])


# ── freeze_value Tests ────────────────────────────────────────────


class TestFreezeValue(unittest.TestCase):
    """Tests for the freeze_value utility."""

    def test_freeze_dict(self):
        """Dicts become FrozenDict."""
        result = freeze_value({"a": 1})
        self.assertIsInstance(result, FrozenDict)

    def test_freeze_list(self):
        """Lists become FrozenList."""
        result = freeze_value([1, 2])
        self.assertIsInstance(result, FrozenList)

    def test_freeze_nested(self):
        """Nested structures are deeply frozen."""
        result = freeze_value({"a": {"b": [1, 2]}})
        self.assertIsInstance(result, FrozenDict)
        self.assertIsInstance(result["a"], FrozenDict)
        self.assertIsInstance(result["a"]["b"], FrozenList)

    def test_freeze_primitives(self):
        """Primitives pass through unchanged."""
        self.assertEqual(freeze_value(42), 42)
        self.assertEqual(freeze_value("hello"), "hello")
        self.assertEqual(freeze_value(True), True)
        self.assertIsNone(freeze_value(None))


# ── VerificationContext Immutability Tests ────────────────────────


class TestVerificationContextImmutability(unittest.TestCase):
    """Tests for VerificationContext immutability and safety."""

    def test_frozen_dataclass(self):
        """VerificationContext is a frozen dataclass."""
        ctx = VerificationContext(user_id=1, task_id=2, task_type="sub")
        with self.assertRaises(AttributeError):
            ctx.user_id = 999  # type: ignore[misc]

    def test_task_data_is_frozen_dict(self):
        """task_data field is a FrozenDict."""
        ctx = VerificationContext(
            user_id=1, task_id=2, task_type="sub",
            task_data=FrozenDict({"key": "val"}),
        )
        self.assertIsInstance(ctx.task_data, FrozenDict)

    def test_expected_data_is_frozen_dict(self):
        """expected_data field is a FrozenDict."""
        ctx = VerificationContext(
            user_id=1, task_id=2, task_type="sub",
            expected_data=FrozenDict({"expected": "code"}),
        )
        self.assertIsInstance(ctx.expected_data, FrozenDict)

    def test_actual_data_is_frozen_dict(self):
        """actual_data field is a FrozenDict."""
        ctx = VerificationContext(
            user_id=1, task_id=2, task_type="sub",
            actual_data=FrozenDict({"actual": "code"}),
        )
        self.assertIsInstance(ctx.actual_data, FrozenDict)

    def test_task_data_immutable(self):
        """Cannot mutate task_data through the context."""
        ctx = VerificationContext(
            user_id=1, task_id=2, task_type="sub",
            task_data=FrozenDict({"key": "val"}),
        )
        with self.assertRaises(TypeError):
            ctx.task_data["key"] = "new"  # type: ignore[index]

    def test_expected_data_immutable(self):
        """Cannot mutate expected_data through the context."""
        ctx = VerificationContext(
            user_id=1, task_id=2, task_type="sub",
            expected_data=FrozenDict({"expected": "val"}),
        )
        with self.assertRaises(TypeError):
            ctx.expected_data["expected"] = "new"  # type: ignore[index]

    def test_actual_data_immutable(self):
        """Cannot mutate actual_data through the context."""
        ctx = VerificationContext(
            user_id=1, task_id=2, task_type="sub",
            actual_data=FrozenDict({"actual": "val"}),
        )
        with self.assertRaises(TypeError):
            ctx.actual_data["actual"] = "new"  # type: ignore[index]

    def test_nested_immutable(self):
        """Nested dicts in context are also frozen."""
        ctx = VerificationContext(
            user_id=1, task_id=2, task_type="sub",
            task_data=FrozenDict({"nested": {"inner": 42}}),
        )
        with self.assertRaises(TypeError):
            ctx.task_data["nested"]["inner"] = 999  # type: ignore[index]

    def test_default_fields_are_empty_frozen_dicts(self):
        """Default expected_data, actual_data, task_data are empty FrozenDicts."""
        ctx = VerificationContext(user_id=1, task_id=2, task_type="sub")
        self.assertEqual(ctx.expected_data, FrozenDict())
        self.assertEqual(ctx.actual_data, FrozenDict())
        self.assertEqual(ctx.task_data, FrozenDict())

    def test_source_dict_not_mutated_by_freeze(self):
        """freezing data doesn't mutate the original dict."""
        source = {"key": "val", "nested": [1, 2]}
        frozen = freeze_value(source)
        source["key"] = "changed"
        source["nested"][0] = 999
        self.assertEqual(frozen["key"], "val")
        self.assertEqual(frozen["nested"][0], 1)


# ── Boundary Tests ────────────────────────────────────────────────


class TestVerificationBoundary(unittest.TestCase):
    """Tests for verifier boundary: what verifiers can and cannot do."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(self.test_db_path)
        db.register_user(1001, "alice", "Alice")
        self.task_id = db.create_task(
            title="Join Channel",
            description="Subscribe",
            task_type="subscribe",
            reward=50,
            db_path=self.test_db_path,
            task_data=json.dumps({"expected": "abc", "actual": "abc"}),
        )
        clear_verifiers()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        clear_verifiers()
        CHANNELS.clear()
        if os.path.exists(self.test_db_path):
            os.unlink(self.test_db_path)
        for suffix in ("-wal", "-shm"):
            p = self.test_db_path + suffix
            if os.path.exists(p):
                os.unlink(p)

    # ── Verifier cannot mutate context data ───────────────────
    def test_verifier_cannot_mutate_task_data(self):
        """Verifier cannot modify task_data through context."""

        class _MutatingVerifier:
            def verify(self, context):
                # Attempt to mutate — should fail
                try:
                    context.task_data["injected"] = True  # type: ignore[index]
                except TypeError:
                    pass  # Expected
                return VerificationResult(status=VerificationStatus.PASSED)

        # Manually test the mutation prevention
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="sub",
            task_data=FrozenDict({"key": "val"}),
        )
        with self.assertRaises(TypeError):
            ctx.task_data["injected"] = True  # type: ignore[index]

    def test_verifier_cannot_mutate_expected_data(self):
        """Verifier cannot modify expected_data through context."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="sub",
            expected_data=FrozenDict({"expected": "correct"}),
        )
        with self.assertRaises(TypeError):
            ctx.expected_data["expected"] = "hacked"  # type: ignore[index]

    def test_verifier_cannot_mutate_actual_data(self):
        """Verifier cannot modify actual_data through context."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="sub",
            actual_data=FrozenDict({"actual": "submitted"}),
        )
        with self.assertRaises(TypeError):
            ctx.actual_data["actual"] = "spoofed"  # type: ignore[index]

    # ── Verifier cannot modify database state through context ─
    def test_context_does_not_expose_db_connection(self):
        """VerificationContext does not contain db connections."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="sub",
            task_data=FrozenDict({"key": "val"}),
        )
        # No db connection attributes
        self.assertFalse(hasattr(ctx, "conn"))
        self.assertFalse(hasattr(ctx, "cursor"))
        self.assertFalse(hasattr(ctx, "db_path"))

    # ── Metadata not in context ───────────────────────────────
    def test_reward_not_in_expected_data(self):
        """reward metadata is not included in expected_data."""
        register_verifier("subscribe", _CaptureVerifier())
        verify_task(1001, self.task_id)
        # The verifier should not see reward in expected_data
        v = get_verifier("subscribe")
        self.assertIsNotNone(v)
        self.assertNotIn("reward", v.last_context.expected_data)
        self.assertNotIn("reward", v.last_context.actual_data)

    def test_active_not_in_context(self):
        """active metadata is not included in any context field."""
        register_verifier("subscribe", _CaptureVerifier())
        verify_task(1001, self.task_id)
        v = get_verifier("subscribe")
        ctx = v.last_context
        self.assertNotIn("active", ctx.task_data)
        self.assertNotIn("active", ctx.expected_data)
        self.assertNotIn("active", ctx.actual_data)

    def test_created_at_not_in_context(self):
        """created_at metadata is not included in any context field."""
        register_verifier("subscribe", _CaptureVerifier())
        verify_task(1001, self.task_id)
        v = get_verifier("subscribe")
        ctx = v.last_context
        self.assertNotIn("created_at", ctx.task_data)
        self.assertNotIn("created_at", ctx.expected_data)
        self.assertNotIn("created_at", ctx.actual_data)

    # ── verifier cannot complete a task ───────────────────────
    def test_verifier_cannot_complete_task(self):
        """verify_task never transitions user_task to completed."""
        db.create_user_task(1001, self.task_id, self.test_db_path)
        db.update_user_task_status(
            1001, self.task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        register_verifier("subscribe", _CaptureVerifier())
        result = verify_task(1001, self.task_id)
        self.assertTrue(result.passed)

        utask = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    # ── verify_task returns VerificationResult ────────────────
    def test_verify_returns_verification_result(self):
        """verify_task returns a VerificationResult."""
        register_verifier("subscribe", _CaptureVerifier())
        result = verify_task(1001, self.task_id)
        self.assertIsInstance(result, VerificationResult)

    # ── Successful verification compatible with CompletionGate ─
    def test_successful_verification_completes_via_gate(self):
        """PASSED verification feeds into CompletionGate → completed."""
        db.create_user_task(1001, self.task_id, self.test_db_path)
        db.update_user_task_status(
            1001, self.task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        register_verifier("subscribe", _PassVerifier())
        verification = verify_task(1001, self.task_id)
        self.assertTrue(verification.passed)

        gate = CompletionGate()
        completed = gate.complete(1001, self.task_id, verification)
        self.assertTrue(completed)

        utask = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_COMPLETED)

    # ── Failed verification cannot complete ───────────────────
    def test_failed_verification_blocks_gate(self):
        """FAILED verification is rejected by CompletionGate."""
        db.create_user_task(1001, self.task_id, self.test_db_path)
        db.update_user_task_status(
            1001, self.task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        register_verifier("subscribe", _FailVerifier())
        verification = verify_task(1001, self.task_id)
        self.assertFalse(verification.passed)

        gate = CompletionGate()
        with self.assertRaises(CompletionGateError):
            gate.complete(1001, self.task_id, verification)

    def test_error_verification_blocks_gate(self):
        """ERROR verification is rejected by CompletionGate."""
        db.create_user_task(1001, self.task_id, self.test_db_path)
        db.update_user_task_status(
            1001, self.task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        register_verifier("subscribe", _ErrorVerifier())
        verification = verify_task(1001, self.task_id)
        self.assertFalse(verification.passed)

        gate = CompletionGate()
        with self.assertRaises(CompletionGateError):
            gate.complete(1001, self.task_id, verification)


# ── Deterministic Verifier Focused Tests ──────────────────────────


class TestDeterministicVerifierBoundary(unittest.TestCase):
    """Focused tests for DeterministicTaskVerifier with hardened context."""

    def setUp(self):
        self.verifier = DeterministicTaskVerifier()

    # ── valid exact same-type expected/actual → PASSED ────────
    def test_same_type_string_match(self):
        """Same-type string match → PASSED."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=FrozenDict({"expected": "hello", "actual": "hello"}),
        )
        result = self.verifier.verify(ctx)
        self.assertTrue(result.passed)
        self.assertEqual(result.status, VerificationStatus.PASSED)

    def test_same_type_int_match(self):
        """Same-type int match → PASSED."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=FrozenDict({"expected": 42, "actual": 42}),
        )
        result = self.verifier.verify(ctx)
        self.assertTrue(result.passed)

    def test_same_type_bool_match(self):
        """Same-type bool match → PASSED."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=FrozenDict({"expected": True, "actual": True}),
        )
        result = self.verifier.verify(ctx)
        self.assertTrue(result.passed)

    def test_same_type_float_match(self):
        """Same-type float match → PASSED."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=FrozenDict({"expected": 3.14, "actual": 3.14}),
        )
        result = self.verifier.verify(ctx)
        self.assertTrue(result.passed)

    # ── different same-type values → FAILED ───────────────────
    def test_same_type_string_mismatch(self):
        """Same-type string mismatch → FAILED."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=FrozenDict({"expected": "hello", "actual": "world"}),
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_same_type_int_mismatch(self):
        """Same-type int mismatch → FAILED."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=FrozenDict({"expected": 42, "actual": 43}),
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.FAILED)

    # ── cross-type equal values → FAILED ──────────────────────
    def test_bool_vs_int_fails(self):
        """True (bool) vs 1 (int) → FAILED (different types)."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=FrozenDict({"expected": True, "actual": 1}),
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_int_vs_bool_fails(self):
        """1 (int) vs True (bool) → FAILED (different types)."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=FrozenDict({"expected": 1, "actual": True}),
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_int_vs_float_fails(self):
        """1 (int) vs 1.0 (float) → FAILED (different types)."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=FrozenDict({"expected": 1, "actual": 1.0}),
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_zero_vs_false_fails(self):
        """0 (int) vs False (bool) → FAILED (different types)."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=FrozenDict({"expected": 0, "actual": False}),
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_string_vs_int_fails(self):
        """'1' (str) vs 1 (int) → FAILED (different types)."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=FrozenDict({"expected": "1", "actual": 1}),
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.FAILED)

    # ── malformed data → ERROR ────────────────────────────────
    def test_missing_expected_error(self):
        """Missing 'expected' key → ERROR."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=FrozenDict({"actual": "val"}),
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("expected", result.reason)

    def test_missing_actual_error(self):
        """Missing 'actual' key → ERROR."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=FrozenDict({"expected": "val"}),
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("actual", result.reason)

    def test_empty_task_data_error(self):
        """Empty task_data → ERROR."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=FrozenDict(),
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)

    def test_non_dict_task_data_error(self):
        """Non-dict task_data → ERROR."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data="not a dict",  # type: ignore[assignment]
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)

    def test_extra_keys_do_not_bypass(self):
        """Extra keys like 'completed', 'admin' do not bypass the check."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=FrozenDict({
                "expected": "secret",
                "actual": "wrong",
                "completed": True,
                "admin": True,
            }),
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.FAILED)


# ── Helper Verifiers ──────────────────────────────────────────────


class _CaptureVerifier:
    """Captures the context for inspection."""

    def __init__(self):
        self.last_context = None

    def verify(self, context):
        self.last_context = context
        return VerificationResult(status=VerificationStatus.PASSED)


class _PassVerifier:
    """Always returns PASSED."""

    def verify(self, context):
        return VerificationResult(status=VerificationStatus.PASSED)


class _FailVerifier:
    """Always returns FAILED."""

    def verify(self, context):
        return VerificationResult(status=VerificationStatus.FAILED, reason="not subscribed")


class _ErrorVerifier:
    """Always returns ERROR."""

    def verify(self, context):
        return VerificationResult(status=VerificationStatus.ERROR, reason="api timeout")


# Need to register these as TaskVerifier subclasses for register_verifier
from task_verifier import TaskVerifier as _TV


class _CaptureVerifierCls(_TV):
    """Captures the context for inspection."""

    def __init__(self):
        self.last_context = None

    def verify(self, context):
        self.last_context = context
        return VerificationResult(status=VerificationStatus.PASSED)


class _PassVerifierCls(_TV):
    """Always returns PASSED."""

    def verify(self, context):
        return VerificationResult(status=VerificationStatus.PASSED)


class _FailVerifierCls(_TV):
    """Always returns FAILED."""

    def verify(self, context):
        return VerificationResult(status=VerificationStatus.FAILED, reason="not subscribed")


class _ErrorVerifierCls(_TV):
    """Always returns ERROR."""

    def verify(self, context):
        return VerificationResult(status=VerificationStatus.ERROR, reason="api timeout")


# Re-assign for use in tests
_CaptureVerifier = _CaptureVerifierCls
_PassVerifier = _PassVerifierCls
_FailVerifier = _FailVerifierCls
_ErrorVerifier = _ErrorVerifierCls


if __name__ == "__main__":
    unittest.main()
