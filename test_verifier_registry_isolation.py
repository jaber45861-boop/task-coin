"""
Verifier registry isolation & order independence
=================================================

Regression tests for the verifier-registry order-dependence bug.

The registry in ``task_verifier`` is module-global mutable state.  The
default ``deterministic`` verifier (plus the built-in channel verifier
families) is registered at import time, while ``clear_verifiers()``
empties the registry.  Tests that cleared in ``setUp``/``tearDown``
therefore removed the defaults for every test that ran afterwards, so
the suite's result depended on execution order:

- ``test_admin_task_wizard.py::TestTelegramCompatibility::
  test_existing_telegram_verifier_is_registered`` failed whenever a
  clearing test file ran before it (``AssertionError: unexpectedly
  None``).
- ``test_task_lifecycle.py::TestStart::test_already_completed_rejected``
  failed in the full suite because the submission's ``deterministic``
  lookup returned ``None`` (``task_submission`` logged "No verifier
  registered for task type 'deterministic'"), so the task never
  reached ``completed``.

Pinned behaviour:

- the documented defaults are present at the start of every test
  (``conftest.py`` fixture) and restorable at any time via
  ``reset_verifiers()``
- ``clear_verifiers()`` still clears *everything* — never a no-op
- explicit custom registrations never leak between tests
- the production ``deterministic`` path is unchanged

Run:
    python3 -m pytest test_verifier_registry_isolation.py -q

Without the isolation fixture the order-dependent cases fail — the old
failure mode:
    python3 -m pytest test_verifier_registry_isolation.py -q --noconftest
"""

import json

import pytest

import db
from channel_task_verifier import CHANNEL_TASK_TYPE
from task_completion import VerificationStatus
from task_verifier import (
    DeterministicTaskVerifier,
    clear_verifiers,
    get_verifier,
    register_verifier,
    reset_verifiers,
    verify_task,
)
from telegram_channel_task_verifier import TELEGRAM_CHANNEL_TASK_TYPE

USER_ID = 42_001


def _assert_default_registry() -> None:
    """The registry matches the documented import-time defaults."""
    assert isinstance(get_verifier("deterministic"), DeterministicTaskVerifier)
    assert get_verifier(CHANNEL_TASK_TYPE) is not None
    assert get_verifier(TELEGRAM_CHANNEL_TASK_TYPE) is not None


# ════════════════════════════════════════════════════════════════════
# Test A — clear does not poison later default behaviour
# ════════════════════════════════════════════════════════════════════


def test_a_default_present_clear_stays_meaningful_reset_restores_defaults():
    """default exists → clear → reset → default available again."""
    _assert_default_registry()

    clear_verifiers()
    # clear_verifiers() must remain a real clear, never a no-op:
    assert get_verifier("deterministic") is None
    assert get_verifier(CHANNEL_TASK_TYPE) is None
    assert get_verifier(TELEGRAM_CHANNEL_TASK_TYPE) is None

    reset_verifiers()
    _assert_default_registry()


# ════════════════════════════════════════════════════════════════════
# Test B — order independence
# ════════════════════════════════════════════════════════════════════


class TestOrderIndependence:
    """A clearing test must not affect the test that runs after it.

    ``test_1_clears_registry`` runs first and empties the registry
    (exactly like a legacy ``tearDown``); ``test_2_*`` runs second.
    Before the fix, running this file after any other clearing test
    file made the second test fail — the order-dependent failure.
    """

    def test_1_clears_registry(self):
        clear_verifiers()
        assert get_verifier("deterministic") is None

    def test_2_defaults_available_again(self):
        # OLD BEHAVIOUR: AssertionError — get_verifier(...) was None
        # whenever a clearing test had run first.
        _assert_default_registry()


# ════════════════════════════════════════════════════════════════════
# Test C — custom verifier isolation
# ════════════════════════════════════════════════════════════════════


class _ProbeVerifier(DeterministicTaskVerifier):
    """Marker verifier used only to detect cross-test leakage."""


class TestCustomVerifierIsolation:
    """A registration made in one test must not reach the next test."""

    def test_1_registers_custom_verifier(self):
        custom = _ProbeVerifier()
        register_verifier("deterministic", custom)
        register_verifier("isolation_probe_type", custom)
        assert get_verifier("deterministic") is custom
        assert get_verifier("isolation_probe_type") is custom

    def test_2_does_not_inherit_custom_verifier(self):
        # OLD BEHAVIOUR: both registrations leaked from test_1.
        assert get_verifier("isolation_probe_type") is None
        default = get_verifier("deterministic")
        assert type(default) is DeterministicTaskVerifier
        assert not isinstance(default, _ProbeVerifier)


# ════════════════════════════════════════════════════════════════════
# Test D — deterministic production path unchanged
# ════════════════════════════════════════════════════════════════════


@pytest.fixture
def verifier_db(monkeypatch, tmp_path):
    """Isolated DB with one user, mirroring the repo's fixture style."""
    db_path = str(tmp_path / "registry_isolation.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER_ID, "carol", "Carol")
    yield db_path


def _deterministic_task(task_data: dict) -> int:
    return db.create_task(
        "Enter Code",
        "Enter the correct code",
        "deterministic",
        25,
        task_data=json.dumps(task_data),
    )


def test_d_verify_task_production_path_unchanged(verifier_db):
    """verify_task + default deterministic behaves exactly as before."""
    _assert_default_registry()

    passed = _deterministic_task(
        {"expected": "secret123", "actual": "secret123"}
    )
    failed = _deterministic_task({"expected": "secret123", "actual": "wrong"})
    cross_type = _deterministic_task({"expected": "5", "actual": 5})
    missing_actual = _deterministic_task({"expected": "secret123"})

    result = verify_task(USER_ID, passed)
    assert result.status == VerificationStatus.PASSED
    assert result.passed

    result = verify_task(USER_ID, failed)
    assert result.status == VerificationStatus.FAILED
    assert not result.passed

    # Type-strict comparison (no cross-type equality shortcut):
    result = verify_task(USER_ID, cross_type)
    assert result.status == VerificationStatus.FAILED
    assert not result.passed

    result = verify_task(USER_ID, missing_actual)
    assert result.status == VerificationStatus.ERROR
    assert not result.passed
