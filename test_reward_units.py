"""
Focused tests — Sub-cent Task Reward Foundation (MT-ADMIN-13)
=============================================================

``tasks.reward_units`` becomes the authoritative stored accounting
value for task rewards (exact USDT atomic units, 1 USDT = 100,000,000),
while ``tasks.reward`` keeps its whole-USDT display meaning and the
existing input contract (whole-USDT non-negative ints) is unchanged.

Coverage:

  * schema: fresh DB has a nullable INTEGER ``reward_units`` column
  * migration: a pre-migration (old-shape) DB gains the column
  * backfill: ``reward_units = reward * USDT_SCALE`` exactly once
    - reward = 3   -> 300_000_000
    - reward = 0   -> 0
    - re-running init_db() never re-multiplies (3e8 stays 3e8)
    - already-populated rows are never touched
    - out-of-int64 results are NEVER stored (no silent REAL/float)
    - non-integer rewards stay NULL (rejected later, not corrupted)
  * creation writes BOTH ``reward`` and ``reward_units``; edits keep
    them in step
  * settlement reads the authoritative ``reward_units``
    - reward_units = 10_000  settles exactly 0.0001 USDT (10,000 units)
    - reward_units = 1       settles exactly 1 atomic unit
    - exactly-once / idempotent retry (no double credit)
    - whole-USDT tasks settle exactly as before
    - invalid stored values (REAL/TEXT/negative) roll everything back
    - NULL/absent ``reward_units`` falls back to the validated legacy
      whole-USDT conversion (pre-migration rows, in-memory fixtures)
  * no float, no rounding: Decimal round-trips are exact

Run:
    python -m pytest test_reward_units.py -v
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

import db
from task_completion import CompletionGate, CompletionGateError, VerificationResult
from task_completion import VerificationStatus
from task_reward import TaskRewardError, TaskRewardService
from task_start import TaskStartGate
from task_submission import TaskSubmissionService
from task_lifecycle import TaskLifecycle
from task_verifier import (
    DeterministicTaskVerifier,
    clear_verifiers,
    register_verifier,
)
from wallet import USDT_SCALE, decimal_to_units, units_to_decimal

USER = 1731

PASSED = VerificationResult(status=VerificationStatus.PASSED)

# The exact sub-cent example from the task contract.
SUB_CENT_UNITS = 10_000          # == 0.0001 USDT
SINGLE_UNIT = 1                  # == 0.00000001 USDT


# ── Verifier (production settlement path needs a passing verifier) ────


class _PassVerifier(DeterministicTaskVerifier):
    def verify(self, context):
        return VerificationResult(status=VerificationStatus.PASSED)


# ── Fixtures / helpers (same shape as test_task_reward.py) ────────────


@pytest.fixture
def path(monkeypatch, tmp_path):
    db_path = str(tmp_path / "reward_units_test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER, "units", "Units")
    clear_verifiers()
    register_verifier("deterministic", _PassVerifier())
    yield db_path
    # Never leak a custom verifier into other suites.
    clear_verifiers()
    register_verifier("deterministic", DeterministicTaskVerifier())


def _task(reward=10, expected="secret123") -> int:
    return db.create_task(
        "Units Task", "Complete it", "deterministic", reward,
        task_data=json.dumps({"expected": expected}),
    )


def _start(tid: int) -> None:
    TaskStartGate().start(USER, tid)


def _submit(tid: int, key: str, actual: str = "secret123"):
    return TaskLifecycle().submit_task(USER, tid, {"actual": actual}, key)


def _raw(sql: str, params: tuple = ()):
    with db.get_connection() as conn:
        return conn.execute(sql, params).fetchall()


def _set_units(tid: int, value) -> None:
    """Smuggle an exact stored value (test-only, server-side data)."""
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET reward_units = ? WHERE id = ?", (value, tid)
        )


def _wallet_available() -> int:
    rows = _raw(
        "SELECT available_units FROM wallets WHERE user_id = ?", (USER,)
    )
    return rows[0]["available_units"] if rows else 0


def _ledger_credits() -> list[dict]:
    return [
        dict(r)
        for r in _raw(
            "SELECT * FROM ledger WHERE user_id = ? ORDER BY id", (USER,)
        )
    ]


def _column_info(table: str) -> dict:
    rows = _raw(f"PRAGMA table_info({table})")
    return {row["name"]: dict(row) for row in rows}


# ════════════════════════════════════════════════════════════════════
# Schema + migration + backfill
# ════════════════════════════════════════════════════════════════════


class TestSchemaAndMigration:

    def test_fresh_db_has_reward_units_column(self, path):
        info = _column_info("tasks")["reward_units"]
        assert info["type"].upper() == "INTEGER"
        assert info["notnull"] == 0
        assert info["dflt_value"] is None

    def test_old_shape_db_gains_column_and_backfills(self, path):
        """A pre-MT-ADMIN-13 tasks table gains the column; reward=3
        backfills to exactly 300,000,000 and reward=0 to 0."""
        with db.get_connection() as conn:
            conn.execute("DROP TABLE tasks")
            conn.execute(
                """CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    type TEXT NOT NULL,
                    reward INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    task_data TEXT,
                    repeat_policy TEXT NOT NULL DEFAULT 'one_time',
                    repeat_hours INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            for tid, reward in ((1, 3), (2, 0)):
                conn.execute(
                    "INSERT INTO tasks (id, title, description, type, reward)"
                    " VALUES (?, 'T', 'D', 'deterministic', ?)",
                    (tid, reward),
                )
        # Not there yet on the old shape.
        assert "reward_units" not in _column_info("tasks")

        db.init_db(path)

        assert "reward_units" in _column_info("tasks")
        rows = {r["id"]: r["reward_units"] for r in _raw("SELECT id, reward_units FROM tasks")}
        assert rows[1] == 3 * USDT_SCALE          # 300_000_000
        assert rows[1] == 300_000_000
        assert rows[2] == 0                       # zero reward -> zero units
        # The legacy display field is untouched.
        rewards = {r["id"]: r["reward"] for r in _raw("SELECT id, reward FROM tasks")}
        assert rewards == {1: 3, 2: 0}

    def test_backfill_rerun_never_remultiplies(self, path):
        """init_db() repeated after backfill keeps 300,000,000 — it
        must never become 300,000,000 * 1e8 again."""
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO tasks (title, description, type, reward)"
                " VALUES ('T', 'D', 'deterministic', 3)"
            )
            tid = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
            conn.execute(
                "UPDATE tasks SET reward_units = NULL WHERE id = ?", (tid,)
            )
        db.init_db(path)
        for _ in range(3):
            db.init_db(path)
        row = _raw("SELECT reward_units FROM tasks WHERE id = ?", (tid,))[0]
        assert row["reward_units"] == 300_000_000
        assert row["reward_units"] != 300_000_000 * USDT_SCALE

    def test_backfill_never_touches_populated_rows(self, path):
        tid = _task(reward=3)
        assert db.get_task(tid)["reward_units"] == 300_000_000
        # reward edited behind the authority's back: NULL-only backfill
        # must not recompute (or double) a populated value.
        with db.get_connection() as conn:
            conn.execute("UPDATE tasks SET reward = 7 WHERE id = ?", (tid,))
        db.init_db(path)
        assert db.get_task(tid)["reward_units"] == 300_000_000
        # Once NULLed, the backfill derives from the CURRENT reward.
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET reward_units = NULL WHERE id = ?", (tid,)
            )
        db.init_db(path)
        assert db.get_task(tid)["reward_units"] == 700_000_000

    def test_backfill_overflow_never_stores_real(self, path):
        """A reward whose atomic value exceeds int64 must never be
        stored as a wrapped or REAL (float) value — the row stays
        NULL and init_db() keeps succeeding."""
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO tasks (title, description, type, reward)"
                " VALUES ('Big', 'D', 'deterministic', 1000000000000)"
            )
            tid = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        db.init_db(path)
        row = _raw(
            "SELECT reward, reward_units, typeof(reward_units) AS t"
            " FROM tasks WHERE id = ?",
            (tid,),
        )[0]
        assert row["reward"] == 1_000_000_000_000   # display value intact
        assert row["reward_units"] is None          # never corrupted
        assert row["t"] == "null"                   # never REAL, never int-wrap
        # still idempotent
        db.init_db(path)
        row = _raw(
            "SELECT reward_units, typeof(reward_units) AS t"
            " FROM tasks WHERE id = ?",
            (tid,),
        )[0]
        assert row["reward_units"] is None
        assert row["t"] == "null"

    def test_backfill_non_integer_reward_stays_null(self, path):
        """A smuggled REAL reward is never multiplied into float
        units: it stays NULL for settlement to reject as before."""
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO tasks (title, description, type, reward)"
                " VALUES ('Bad', 'D', 'deterministic', 1.5)"
            )
            tid = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        db.init_db(path)
        row = _raw(
            "SELECT reward_units, typeof(reward_units) AS t"
            " FROM tasks WHERE id = ?",
            (tid,),
        )[0]
        assert row["reward_units"] is None
        assert row["t"] == "null"


# ════════════════════════════════════════════════════════════════════
# Creation / edit write both fields
# ════════════════════════════════════════════════════════════════════


class TestCreationWritesBoth:

    def test_create_task_writes_reward_units(self, path):
        tid = _task(reward=5)
        task = db.get_task(tid)
        assert task["reward"] == 5
        assert task["reward_units"] == 5 * USDT_SCALE
        assert task["reward_units"] == 500_000_000

    def test_create_task_zero_reward_writes_zero_units(self, path):
        tid = _task(reward=0)
        task = db.get_task(tid)
        assert task["reward"] == 0
        assert task["reward_units"] == 0

    def test_update_task_keeps_units_in_step(self, path):
        tid = _task(reward=5)
        assert db.update_task(tid, reward=9) is True
        task = db.get_task(tid)
        assert task["reward"] == 9
        assert task["reward_units"] == 900_000_000
        # Non-reward edits never touch the authority.
        assert db.update_task(tid, active=False) is True
        task = db.get_task(tid)
        assert task["reward"] == 9
        assert task["reward_units"] == 900_000_000

    def test_whole_usdt_settlement_unchanged(self, path):
        """Existing whole-USDT tasks settle exactly as before: 50
        USDT -> 5,000,000,000 atomic units, one ledger credit."""
        tid = _task(reward=50)
        _start(tid)
        result = _submit(tid, "whole")
        assert result.passed
        assert db.get_user_task(USER, tid)["status"] == db.USER_TASK_STATUS_COMPLETED
        assert _wallet_available() == 50 * USDT_SCALE
        entries = _ledger_credits()
        assert len(entries) == 1
        assert entries[0]["amount_units"] == 5_000_000_000
        assert entries[0]["available_delta"] == 5_000_000_000
        assert units_to_decimal(_wallet_available()) == Decimal("50")


# ════════════════════════════════════════════════════════════════════
# Sub-cent settlement from the authoritative field
# ════════════════════════════════════════════════════════════════════


class TestSubCentSettlement:

    def test_settles_ten_thousand_units_exactly(self, path):
        """reward_units = 10_000 settles exactly 0.0001 USDT."""
        tid = _task(reward=0)
        _set_units(tid, SUB_CENT_UNITS)
        assert db.get_task(tid)["reward_units"] == 10_000

        _start(tid)
        result = _submit(tid, "subcent")
        assert result.passed
        assert (
            db.get_user_task(USER, tid)["status"]
            == db.USER_TASK_STATUS_COMPLETED
        )
        # wallet: exactly 10,000 units, held untouched
        assert _wallet_available() == 10_000
        # ledger: exactly one credit of exactly 10,000 units
        entries = _ledger_credits()
        assert len(entries) == 1
        assert entries[0]["entry_type"] == "credit"
        assert entries[0]["amount_units"] == 10_000
        assert entries[0]["available_delta"] == 10_000
        assert entries[0]["held_delta"] == 0
        # snapshot metadata carries the exact atomic amount
        meta = json.loads(entries[0]["metadata"])
        assert meta["reward_units"] == 10_000
        # exact Decimal round-trip (no rounding anywhere)
        assert units_to_decimal(10_000) == Decimal("0.0001")
        assert decimal_to_units(Decimal("0.0001")) == 10_000

    def test_settles_single_atomic_unit(self, path):
        """reward_units = 1 settles exactly 0.00000001 USDT."""
        tid = _task(reward=0)
        _set_units(tid, SINGLE_UNIT)
        _start(tid)
        result = _submit(tid, "atomic")
        assert result.passed
        assert _wallet_available() == 1
        entries = _ledger_credits()
        assert len(entries) == 1
        assert entries[0]["amount_units"] == 1
        assert units_to_decimal(1) == Decimal("0.00000001")
        assert decimal_to_units(Decimal("0.00000001")) == 1

    def test_sub_cent_retry_never_double_credits(self, path):
        """Exactly-once: a same-key replay returns the original
        result, a second completion attempt is rejected by the CAS,
        and the wallet/ledger never move again."""
        tid = _task(reward=0)
        _set_units(tid, SUB_CENT_UNITS)
        _start(tid)
        assert _submit(tid, "once").passed

        # Idempotent retry of the same submission key.
        replay = TaskSubmissionService.replay_result(USER, tid, "once")
        assert replay is not None
        assert replay.passed
        # A direct second completion attempt must be rejected outright.
        with pytest.raises(CompletionGateError):
            CompletionGate().complete(USER, tid, PASSED)

        assert _wallet_available() == 10_000
        entries = _ledger_credits()
        assert len(entries) == 1
        assert entries[0]["amount_units"] == 10_000

    def test_decimal_roundtrip_is_exact(self, path):
        for units in (1, SUB_CENT_UNITS, 100_000_000, 5_000_000_000):
            dec = units_to_decimal(units)
            assert decimal_to_units(dec) == units
        assert units_to_decimal(SUB_CENT_UNITS) == Decimal("0.0001")


# ════════════════════════════════════════════════════════════════════
# Validation: invalid stored / legacy values never settle
# ════════════════════════════════════════════════════════════════════


class TestValidation:

    def _assert_rolled_back(self, tid: int) -> None:
        assert db.get_user_task(USER, tid)["status"] == db.USER_TASK_STATUS_STARTED
        assert db.get_user_task(USER, tid)["completed_at"] is None
        assert _wallet_available() == 0
        assert _ledger_credits() == []

    def test_real_stored_units_rollback_completion(self, path):
        tid = _task(reward=10)
        _set_units(tid, 1.5)          # REAL smuggled into the authority
        _start(tid)
        with pytest.raises(TaskRewardError):
            CompletionGate().complete(USER, tid, PASSED)
        self._assert_rolled_back(tid)

    def test_text_stored_units_rollback_completion(self, path):
        tid = _task(reward=10)
        # Well-formed integer text would be converted by INTEGER
        # affinity; non-numeric text genuinely survives as TEXT.
        _set_units(tid, "not-a-unit")
        row = _raw(
            "SELECT typeof(reward_units) AS t FROM tasks WHERE id = ?",
            (tid,),
        )[0]
        assert row["t"] == "text"
        _start(tid)
        with pytest.raises(TaskRewardError):
            CompletionGate().complete(USER, tid, PASSED)
        self._assert_rolled_back(tid)

    def test_negative_stored_units_rollback_completion(self, path):
        tid = _task(reward=10)
        _set_units(tid, -5)
        _start(tid)
        with pytest.raises(TaskRewardError):
            CompletionGate().complete(USER, tid, PASSED)
        self._assert_rolled_back(tid)

    def test_invalid_legacy_reward_values_still_fail(self, path):
        """The current contract on ``reward`` is preserved: negative,
        float, bool, None, junk and over-precision all raise, and a
        missing task dict raises — even without a stored authority."""
        for bad in (-1, 1.5, True, None, "abc", Decimal("0.000000001")):
            with pytest.raises(TaskRewardError):
                TaskRewardService.reward_units({"id": 1, "reward": bad})
        with pytest.raises(TaskRewardError):
            TaskRewardService.reward_units(None)
        # Valid legacy values still convert exactly (whole USDT).
        assert TaskRewardService.reward_units({"id": 1, "reward": 50}) == 5_000_000_000
        assert TaskRewardService.reward_units({"id": 1, "reward": 0}) == 0

    def test_invalid_stored_units_rejected_on_dicts(self, path):
        base = {"id": 1, "reward": 10}
        for bad in (1.5, "100000000", True, -5, Decimal("10")):
            task = dict(base, reward_units=bad)
            with pytest.raises(TaskRewardError):
                TaskRewardService.reward_units(task)

    def test_null_units_row_settles_via_legacy_whole_usdt(self, path):
        """Pre-migration rows (reward_units NULL) settle through the
        validated legacy whole-USDT conversion — exactly as before."""
        tid = _task(reward=50)
        _set_units(tid, None)
        assert db.get_task(tid)["reward_units"] is None
        _start(tid)
        result = _submit(tid, "legacy")
        assert result.passed
        assert _wallet_available() == 5_000_000_000
        entries = _ledger_credits()
        assert len(entries) == 1
        assert entries[0]["amount_units"] == 5_000_000_000

    def test_populated_authority_wins_over_legacy_display(self, path):
        """The atomic field is authoritative: a valid stored value
        determines the credit even when the display field differs."""
        tid = _task(reward=50)
        _set_units(tid, SUB_CENT_UNITS)
        task = db.get_task(tid)
        assert TaskRewardService.reward_units(task) == 10_000
        assert task["reward"] == 50
