"""
Focused tests — task_submissions & repeat-policy schema (MT-TASK-04)
====================================================================

Covers PART 14 schema requirements:

- table exists with required columns
- foreign keys follow db.py conventions (users, tasks)
- valid submission statuses accepted; invalid statuses rejected
  (including every forbidden future state: pending/approved/rejected/
  reserved/expired)
- repeat_policy constraints (one_time/repeatable CHECK + validation)
- repeat_hours constraints (integer, positive for repeatable, no floats)
- idempotency uniqueness enforced BY THE DATABASE
- no REAL/money float fields anywhere in the new schema
- existing schema remains compatible (additive migration, defaults
  for legacy rows, users/user_tasks/wallets/ledger untouched)

Run:
    python3 -m pytest test_task_submissions_schema.py -v
"""

import sqlite3
import tempfile

import pytest

import db


@pytest.fixture
def path(tmp_path):
    """Fresh isolated database with the full current schema."""
    db_path = str(tmp_path / "schema_test.db")
    db.init_db(db_path)
    return db_path


def _columns(conn, table: str) -> dict:
    return {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_types(conn, table: str) -> set:
    return {row[2] for row in conn.execute(f"PRAGMA table_info({table})")}


# ════════════════════════════════════════════════════════════════════
# task_submissions table
# ════════════════════════════════════════════════════════════════════


class TestSubmissionsTable:
    def test_table_exists(self, path):
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='task_submissions'"
            ).fetchone()
        assert row is not None, "task_submissions table missing"

    def test_required_columns_exist(self, path):
        with sqlite3.connect(path) as conn:
            cols = _columns(conn, "task_submissions")
        for name in (
            "submission_id", "user_id", "task_id", "attempt_number",
            "status", "idempotency_key", "verification_reason",
            "submitted_at", "completed_at", "created_at",
        ):
            assert name in cols, f"missing column: {name}"

    def test_submission_id_is_primary_key(self, path):
        with sqlite3.connect(path) as conn:
            cols = _columns(conn, "task_submissions")
        # PRAGMA table_info: index 5 is the pk ordinal (1 = primary key).
        assert cols["submission_id"][5] == 1, "submission_id must be PK"

    def test_foreign_keys_follow_conventions(self, path):
        with sqlite3.connect(path) as conn:
            # foreign_key_list row: (id, seq, table, from, to, ...).
            fks = {
                row[3]: row[2]
                for row in conn.execute(
                    "PRAGMA foreign_key_list(task_submissions)"
                )
            }
        assert fks.get("user_id") == "users"
        assert fks.get("task_id") == "tasks"

    def test_no_real_or_money_float_fields(self, path):
        with sqlite3.connect(path) as conn:
            sub_types = _table_types(conn, "task_submissions")
            task_types = _table_types(conn, "tasks")
        assert "REAL" not in sub_types
        assert "REAL" not in task_types
        assert sub_types <= {"INTEGER", "TEXT", "TIMESTAMP", ""}

    def test_existing_lifecycle_tables_untouched(self, path):
        with sqlite3.connect(path) as conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            ut_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(user_tasks)")
            }
        for table in ("users", "tasks", "user_tasks", "wallets", "ledger"):
            assert table in tables, f"existing table missing: {table}"
        # user_tasks gains nothing: still exactly the MT-TASK-02 shape.
        assert ut_cols == {
            "user_id", "task_id", "status", "started_at", "completed_at",
        }

    def test_user_task_status_vocabulary_unchanged(self):
        assert db.ALLOWED_USER_TASK_STATUSES == {
            db.USER_TASK_STATUS_AVAILABLE,
            db.USER_TASK_STATUS_STARTED,
            db.USER_TASK_STATUS_COMPLETED,
        }


# ════════════════════════════════════════════════════════════════════
# Submission status constraints
# ════════════════════════════════════════════════════════════════════


class TestSubmissionStatusConstraints:
    def _insert(self, path, status: str, key: str = "k") -> None:
        with sqlite3.connect(path) as conn:
            conn.execute(
                "INSERT INTO tasks (title, description, type, reward) "
                "VALUES ('t', 'd', 'x', 1)"
            )
            task_id = conn.execute(
                "SELECT id FROM tasks ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO users (user_id) VALUES (555)"
            )
            conn.execute(
                "INSERT INTO task_submissions "
                "(user_id, task_id, attempt_number, status, idempotency_key) "
                "VALUES (555, ?, 1, ?, ?)",
                (task_id, status, key),
            )

    @pytest.mark.parametrize("status", list(db.SUBMISSION_STATUSES))
    def test_valid_statuses_accepted(self, path, status):
        self._insert(path, status, key=f"k-{status}")
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT status FROM task_submissions WHERE status = ?",
                (status,),
            ).fetchone()
        assert row is not None

    @pytest.mark.parametrize(
        "status", ["pending", "approved", "rejected", "reserved",
                   "expired", "in_review", "", None]
    )
    def test_invalid_statuses_rejected(self, path, status):
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(path, status, key=f"bad-{status}")

    def test_status_vocabulary_is_exactly_the_four(self):
        assert set(db.SUBMISSION_STATUSES) == {
            "submitted", "passed", "failed", "error",
        }


# ════════════════════════════════════════════════════════════════════
# Idempotency uniqueness (database-enforced)
# ════════════════════════════════════════════════════════════════════


class TestIdempotencyUniqueness:
    def test_duplicate_key_same_context_rejected_by_db(self, path):
        with sqlite3.connect(path) as conn:
            conn.execute("INSERT INTO users (user_id) VALUES (7)")
            conn.execute(
                "INSERT INTO tasks (title, description, type, reward) "
                "VALUES ('t', 'd', 'x', 1)"
            )
            task_id = conn.execute(
                "SELECT id FROM tasks LIMIT 1"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO task_submissions "
                "(user_id, task_id, attempt_number, idempotency_key) "
                "VALUES (7, ?, 1, 'same-key')",
                (task_id,),
            )
        with sqlite3.connect(path) as conn, pytest.raises(
            sqlite3.IntegrityError
        ):
            conn.execute(
                "INSERT INTO task_submissions "
                "(user_id, task_id, attempt_number, idempotency_key) "
                "VALUES (7, ?, 2, 'same-key')",
                (task_id,),
            )

    def test_unique_index_is_on_the_triple(self, path):
        with sqlite3.connect(path) as conn:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='task_submissions'"
            ).fetchone()[0].upper()
        assert "UNIQUE (USER_ID, TASK_ID, IDEMPOTENCY_KEY)" in sql

    def test_same_key_different_context_allowed(self, path):
        """Keys are scoped: another task/user with the same string is a
        different record, never a collision."""
        from task_submission_store import TaskSubmissionStore

        with sqlite3.connect(path) as conn:
            conn.execute("INSERT INTO users (user_id) VALUES (7)")
            conn.execute("INSERT INTO users (user_id) VALUES (8)")
            conn.execute(
                "INSERT INTO tasks (title, description, type, reward) "
                "VALUES ('t1', 'd', 'x', 1)"
            )
            conn.execute(
                "INSERT INTO tasks (title, description, type, reward) "
                "VALUES ('t2', 'd', 'x', 1)"
            )
        r1, created1 = TaskSubmissionStore.create_submission(
            7, 1, "shared", db_path=path
        )
        r2, created2 = TaskSubmissionStore.create_submission(
            8, 1, "shared", db_path=path
        )
        r3, created3 = TaskSubmissionStore.create_submission(
            7, 2, "shared", db_path=path
        )
        assert created1 and created2 and created3
        assert len({r1.submission_id, r2.submission_id,
                    r3.submission_id}) == 3


# ════════════════════════════════════════════════════════════════════
# repeat_policy / repeat_hours constraints
# ════════════════════════════════════════════════════════════════════


class TestRepeatConstraints:
    def test_default_is_one_time_with_null_hours(self, path):
        tid = db.create_task("t", "d", "x", 1, db_path=path)
        task = db.get_task(tid, path)
        assert task["repeat_policy"] == "one_time"
        assert task["repeat_hours"] is None

    def test_repeatable_with_hours_accepted(self, path):
        tid = db.create_task(
            "t", "d", "x", 1, db_path=path,
            repeat_policy="repeatable", repeat_hours=24,
        )
        task = db.get_task(tid, path)
        assert task["repeat_policy"] == "repeatable"
        assert task["repeat_hours"] == 24

    @pytest.mark.parametrize("policy", ["banana", "", "ONE_TIME", "repeat"])
    def test_invalid_policy_rejected(self, path, policy):
        with pytest.raises(ValueError):
            db.create_task(
                "t", "d", "x", 1, db_path=path, repeat_policy=policy
            )

    @pytest.mark.parametrize("hours", [0, -1, -24])
    def test_non_positive_hours_rejected(self, path, hours):
        with pytest.raises(ValueError):
            db.create_task(
                "t", "d", "x", 1, db_path=path,
                repeat_policy="repeatable", repeat_hours=hours,
            )

    @pytest.mark.parametrize("hours", [1.5, "24", True, None])
    def test_non_integer_hours_rejected(self, path, hours):
        with pytest.raises(ValueError):
            db.create_task(
                "t", "d", "x", 1, db_path=path,
                repeat_policy="repeatable", repeat_hours=hours,
            )

    def test_repeatable_requires_hours(self, path):
        with pytest.raises(ValueError):
            db.create_task(
                "t", "d", "x", 1, db_path=path, repeat_policy="repeatable"
            )

    def test_one_time_rejects_hours(self, path):
        with pytest.raises(ValueError):
            db.create_task(
                "t", "d", "x", 1, db_path=path,
                repeat_policy="one_time", repeat_hours=5,
            )

    def test_db_check_rejects_orphan_hours(self, path):
        """Direct SQL cannot bypass the cross-column CHECK."""
        with sqlite3.connect(path) as conn, pytest.raises(
            sqlite3.IntegrityError
        ):
            conn.execute(
                "INSERT INTO tasks "
                "(title, description, type, reward, repeat_policy, "
                " repeat_hours) VALUES ('t', 'd', 'x', 1, 'one_time', 5)"
            )

    def test_db_check_rejects_unknown_policy(self, path):
        with sqlite3.connect(path) as conn, pytest.raises(
            sqlite3.IntegrityError
        ):
            conn.execute(
                "INSERT INTO tasks "
                "(title, description, type, reward, repeat_policy) "
                "VALUES ('t', 'd', 'x', 1, 'forever')"
            )

    def test_update_task_validates_merged_pair(self, path):
        tid = db.create_task("t", "d", "x", 1, db_path=path)
        # Switching to repeatable requires hours at the same time.
        with pytest.raises(ValueError):
            db.update_task(tid, repeat_policy="repeatable", db_path=path)
        # Passing both is accepted.
        assert db.update_task(
            tid, repeat_policy="repeatable", repeat_hours=3,
            db_path=path,
        )
        task = db.get_task(tid, path)
        assert (task["repeat_policy"], task["repeat_hours"]) == (
            "repeatable", 3
        )
        # Setting hours alone on a one_time task is rejected.
        tid2 = db.create_task("t2", "d", "x", 1, db_path=path)
        with pytest.raises(ValueError):
            db.update_task(tid2, repeat_hours=12, db_path=path)


# ════════════════════════════════════════════════════════════════════
# Additive migration of a legacy database
# ════════════════════════════════════════════════════════════════════


class TestLegacyMigration:
    def _make_legacy_db(self) -> str:
        handle = tempfile.NamedTemporaryFile(
            suffix=".db", delete=False
        )
        handle.close()
        conn = sqlite3.connect(handle.name)
        conn.execute(
            "CREATE TABLE tasks ("
            " id INTEGER PRIMARY KEY,"
            " title TEXT NOT NULL,"
            " description TEXT NOT NULL,"
            " type TEXT NOT NULL,"
            " reward INTEGER NOT NULL,"
            " active INTEGER NOT NULL DEFAULT 1,"
            " task_data TEXT,"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO tasks (title, description, type, reward) "
            "VALUES ('legacy', 'old row', 'x', 7)"
        )
        conn.commit()
        conn.close()
        return handle.name

    def test_migration_adds_columns_with_safe_defaults(self):
        db_path = self._make_legacy_db()
        try:
            db.init_db(db_path)
            task = db.get_task(1, db_path)
            assert task["repeat_policy"] == "one_time"
            assert task["repeat_hours"] is None
            assert task["reward"] == 7  # existing row untouched
        finally:
            import os
            os.unlink(db_path)

    def test_migration_creates_submissions_table(self):
        db_path = self._make_legacy_db()
        try:
            db.init_db(db_path)
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='task_submissions'"
                ).fetchone()
            assert row is not None
        finally:
            import os
            os.unlink(db_path)

    def test_init_db_is_idempotent(self, path):
        db.init_db(path)  # second run must not raise
        tid = db.create_task("t", "d", "x", 1, db_path=path)
        assert db.get_task(tid, path)["repeat_policy"] == "one_time"
