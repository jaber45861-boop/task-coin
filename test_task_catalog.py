"""
Tests for Secure Task Catalog (Micro-task 2.13).

Verifies:
  - read-only boundary is enforced
  - only active tasks are returned
  - TaskSummary exposes only safe fields
  - no database mutation from catalog operations
  - existing task behavior is unchanged

Run:
    python -m unittest test_task_catalog -v
"""

import dataclasses
import os
import tempfile
import unittest

import db
from task_catalog import TaskCatalog, TaskSummary


class TestCatalogContent(unittest.TestCase):
    """Tests for catalog returning correct task content."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db = self._tmp.name
        self._tmp.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self._db
        db.init_db(self._db)
        self._catalog = TaskCatalog()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        for sfx in ("", "-wal", "-shm"):
            p = self._db + sfx
            if os.path.exists(p):
                os.unlink(p)

    def test_active_tasks_are_returned(self):
        """Active tasks appear in the catalog."""
        db.create_task(
            title="Task A", description="Desc A",
            task_type="deterministic", reward=10,
        )
        tasks = self._catalog.list_available_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "Task A")

    def test_inactive_tasks_are_excluded(self):
        """Inactive tasks do NOT appear in the catalog."""
        db.create_task(
            title="Active", description="yes",
            task_type="deterministic", reward=10, active=True,
        )
        db.create_task(
            title="Inactive", description="no",
            task_type="deterministic", reward=20, active=False,
        )
        tasks = self._catalog.list_available_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "Active")

    def test_empty_catalog_returns_empty_list(self):
        """No tasks → empty list, not None."""
        tasks = self._catalog.list_available_tasks()
        self.assertEqual(tasks, [])

    def test_multiple_active_tasks_returned(self):
        """All active tasks are returned."""
        for i in range(5):
            db.create_task(
                title=f"Task {i}", description=f"Desc {i}",
                task_type="deterministic", reward=i * 10,
            )
        tasks = self._catalog.list_available_tasks()
        self.assertEqual(len(tasks), 5)

    def test_id_title_description_type_reward_preserved(self):
        """All five public fields are preserved exactly."""
        tid = db.create_task(
            title="Exact Title", description="Exact Desc",
            task_type="subscribe", reward=42,
        )
        tasks = self._catalog.list_available_tasks()
        t = tasks[0]
        self.assertEqual(t.id, tid)
        self.assertEqual(t.title, "Exact Title")
        self.assertEqual(t.description, "Exact Desc")
        self.assertEqual(t.type, "subscribe")
        self.assertEqual(t.reward, 42)

    def test_ordering_matches_database(self):
        """Catalog preserves db.list_tasks ordering."""
        db.create_task(
            title="First", description="d",
            task_type="deterministic", reward=1,
        )
        db.create_task(
            title="Second", description="d",
            task_type="deterministic", reward=2,
        )
        tasks = self._catalog.list_available_tasks()
        self.assertEqual(tasks[0].title, "First")
        self.assertEqual(tasks[1].title, "Second")


class TestCatalogSecurity(unittest.TestCase):
    """Tests for data boundary and security guarantees."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db = self._tmp.name
        self._tmp.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self._db
        db.init_db(self._db)
        self._catalog = TaskCatalog()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        for sfx in ("", "-wal", "-shm"):
            p = self._db + sfx
            if os.path.exists(p):
                os.unlink(p)

    def test_task_data_not_exposed(self):
        """task_data field is NOT in TaskSummary."""
        db.create_task(
            title="Secret", description="d",
            task_type="deterministic", reward=10,
            task_data='{"expected": "secret123"}',
        )
        tasks = self._catalog.list_available_tasks()
        t = tasks[0]
        self.assertFalse(hasattr(t, "task_data"))

    def test_active_flag_not_exposed(self):
        """The 'active' flag is NOT in TaskSummary."""
        db.create_task(
            title="T", description="d",
            task_type="deterministic", reward=10,
        )
        tasks = self._catalog.list_available_tasks()
        t = tasks[0]
        self.assertFalse(hasattr(t, "active"))

    def test_created_at_not_exposed(self):
        """created_at is NOT in TaskSummary."""
        db.create_task(
            title="T", description="d",
            task_type="deterministic", reward=10,
        )
        tasks = self._catalog.list_available_tasks()
        t = tasks[0]
        self.assertFalse(hasattr(t, "created_at"))

    def test_task_summary_is_frozen(self):
        """TaskSummary is immutable (frozen dataclass)."""
        db.create_task(
            title="T", description="d",
            task_type="deterministic", reward=10,
        )
        t = self._catalog.list_available_tasks()[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            t.title = "hacked"  # type: ignore[misc]

    def test_summaries_are_independent_copies(self):
        """Each call returns independent objects (no shared references)."""
        db.create_task(
            title="T", description="d",
            task_type="deterministic", reward=10,
        )
        list1 = self._catalog.list_available_tasks()
        list2 = self._catalog.list_available_tasks()
        self.assertIsNot(list1, list2)
        self.assertIsNot(list1[0], list2[0])

    def test_expected_data_not_leaked_via_type(self):
        """TaskSummary type field does not expose expected/actual data."""
        db.create_task(
            title="T", description="d",
            task_type="deterministic", reward=10,
            task_data='{"expected": "leaked"}',
        )
        tasks = self._catalog.list_available_tasks()
        self.assertNotIn("expected", tasks[0].type.lower())
        self.assertNotIn("leaked", tasks[0].type.lower())


class TestCatalogNoWrites(unittest.TestCase):
    """Tests proving the catalog performs NO database writes."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db = self._tmp.name
        self._tmp.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self._db
        db.init_db(self._db)
        self._catalog = TaskCatalog()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        for sfx in ("", "-wal", "-shm"):
            p = self._db + sfx
            if os.path.exists(p):
                os.unlink(p)

    def test_catalog_performs_no_writes(self):
        """Listing tasks does not create any rows."""
        count_before = len(self._catalog.list_available_tasks())
        self._catalog.list_available_tasks()
        count_after = len(self._catalog.list_available_tasks())
        self.assertEqual(count_before, count_after)

    def test_catalog_does_not_migrate_schema(self):
        """Catalog does not ALTER or CREATE tables."""
        import sqlite3
        with sqlite3.connect(self._db) as conn:
            tables_before = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self._catalog.list_available_tasks()
        with sqlite3.connect(self._db) as conn:
            tables_after = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertEqual(tables_before, tables_after)


class TestCatalogArchitecture(unittest.TestCase):
    """Architecture tests — verify no forbidden patterns in task_catalog.py."""

    def test_no_direct_sqlite3_connection(self):
        """TaskCatalog must not open sqlite3 connections."""
        import task_catalog as mod
        with open(mod.__file__) as f:
            src = f.read()
        self.assertNotIn("sqlite3.connect", src)

    def test_no_direct_db_mutation(self):
        """TaskCatalog must not write to the database."""
        import task_catalog as mod
        with open(mod.__file__) as f:
            src = f.read()
        self.assertNotIn("INSERT INTO", src)
        self.assertNotIn("UPDATE ", src)
        self.assertNotIn("DELETE FROM", src)
        self.assertNotIn("CREATE TABLE", src)
        self.assertNotIn("db.create_task", src)
        self.assertNotIn("db.update_user_task_status", src)

    def test_no_expected_data_in_summary(self):
        """TaskSummary fields must not include expected_data."""
        fields = {f.name for f in dataclasses.fields(TaskSummary)}
        self.assertNotIn("expected_data", fields)
        self.assertNotIn("task_data", fields)

    def test_task_summary_has_only_five_fields(self):
        """TaskSummary must expose exactly: id, title, description, type, reward."""
        fields = {f.name for f in dataclasses.fields(TaskSummary)}
        self.assertEqual(fields, {"id", "title", "description", "type", "reward"})


class TestExistingBehaviorUnchanged(unittest.TestCase):
    """Tests proving existing task DB behavior is unaffected."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db = self._tmp.name
        self._tmp.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self._db
        db.init_db(self._db)

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        for sfx in ("", "-wal", "-shm"):
            p = self._db + sfx
            if os.path.exists(p):
                os.unlink(p)

    def test_creating_tasks_still_works(self):
        """db.create_task is unaffected by TaskCatalog."""
        tid = db.create_task(
            title="T", description="d",
            task_type="deterministic", reward=10,
        )
        self.assertGreater(tid, 0)

    def test_getting_tasks_still_works(self):
        """db.get_task still returns full row with task_data."""
        tid = db.create_task(
            title="T", description="d",
            task_type="deterministic", reward=10,
            task_data='{"expected": "secret"}',
        )
        row = db.get_task(tid)
        self.assertEqual(row["title"], "T")
        self.assertEqual(row["task_data"], '{"expected": "secret"}')

    def test_deleting_tasks_still_works(self):
        """Deleting via other APIs is unaffected by TaskCatalog."""
        import sqlite3
        tid = db.create_task(
            title="T", description="d",
            task_type="deterministic", reward=10,
        )
        with sqlite3.connect(self._db) as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
        self.assertIsNone(db.get_task(tid))


if __name__ == "__main__":
    unittest.main()
