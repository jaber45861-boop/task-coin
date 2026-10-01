"""Shared pytest fixtures.

The verifier registry in ``task_verifier`` is module-global mutable
state.  The default ``deterministic`` verifier (plus the built-in
channel verifier families) is registered at import time, while
``clear_verifiers()`` empties the registry — and many tests clear it
in ``setUp``/``tearDown``.  Without isolation that mutation leaks into
every later test, so the suite's result depended on execution order:
a test running after any clearing test could find no ``deterministic``
verifier at all.

Fixtures here reset the registry to the documented defaults before
every test (:func:`task_verifier.reset_verifiers`), giving each test a
deterministic starting point while keeping ``clear_verifiers()`` fully
meaningful *inside* the test that calls it.  The fixture runs before
unittest ``setUp`` (and finalises after ``tearDown``), so per-test
seeding done by a test's own ``setUp`` or fixtures still wins within
that test.
"""

import pytest


@pytest.fixture(autouse=True)
def verifier_registry_isolated():
    """Start every test from the documented default registrations."""
    # Imported lazily so merely collecting tests never triggers the
    # task_verifier import chain.
    from task_verifier import reset_verifiers

    reset_verifiers()
    yield
