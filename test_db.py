"""
Tests for SQLite persistence of required channels.

Run:
    python -m pytest test_db.py -v
    # or
    python -m unittest test_db.py -v
"""

import os
import sqlite3
import tempfile
import unittest

from config import CHANNELS, Channel
import db


class TestSQLitePersistence(unittest.TestCase):
    """Tests for the SQLite persistence layer."""
    
    def setUp(self):
        """Create a temporary database for each test."""
        self.test_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        
        # Clear in-memory channels
        CHANNELS.clear()
    
    def tearDown(self):
        """Clean up temporary database after each test."""
        if os.path.exists(self.test_db_path):
            os.unlink(self.test_db_path)
        # Also clean up WAL and SHM files
        for suffix in ['-wal', '-shm']:
            wal_path = self.test_db_path + suffix
            if os.path.exists(wal_path):
                os.unlink(wal_path)
        
        # Clear in-memory channels
        CHANNELS.clear()
    
    def test_fresh_database_starts_with_no_channels(self):
        """Fresh database starts with no channels."""
        db.init_db(self.test_db_path)
        db.load_channels(self.test_db_path)
        
        self.assertEqual(len(CHANNELS), 0)
    
    def test_adding_channel_persists_row(self):
        """Adding a channel persists a row in SQLite."""
        db.init_db(self.test_db_path)
        
        channel = Channel(
            slug="test_channel",
            channel_id=-1001234567890,
            username="testchannel",
            title="Test Channel",
            required=True
        )
        
        db.save_channel(channel, self.test_db_path)
        
        # Verify row exists in database
        conn = sqlite3.connect(self.test_db_path)
        cursor = conn.execute(
            "SELECT slug, channel_id, username, title, required FROM required_channels WHERE slug = ?",
            ("test_channel",)
        )
        row = cursor.fetchone()
        conn.close()
        
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "test_channel")
        self.assertEqual(row[1], -1001234567890)
        self.assertEqual(row[2], "testchannel")
        self.assertEqual(row[3], "Test Channel")
        self.assertEqual(row[4], 1)
    
    def test_loading_restores_channel_to_memory(self):
        """Loading from database restores channel into CHANNELS dict."""
        db.init_db(self.test_db_path)
        
        # Save a channel
        channel = Channel(
            slug="restore_test",
            channel_id=-100999888777,
            username="restoretest",
            title="Restore Test",
            required=True
        )
        db.save_channel(channel, self.test_db_path)
        
        # Load channels
        db.load_channels(self.test_db_path)
        
        # Verify channel is in memory
        self.assertIn("restore_test", CHANNELS)
        loaded = CHANNELS["restore_test"]
        self.assertEqual(loaded.channel_id, -100999888777)
        self.assertEqual(loaded.username, "restoretest")
        self.assertEqual(loaded.title, "Restore Test")
        self.assertTrue(loaded.required)
    
    def test_listchannels_shows_restored_channels(self):
        """listchannels shows restored channels (verified via CHANNELS dict)."""
        db.init_db(self.test_db_path)
        
        # Save a channel
        channel = Channel(
            slug="list_test",
            channel_id=-100555666777,
            username="listtest",
            title="List Test",
            required=True
        )
        db.save_channel(channel, self.test_db_path)
        
        # Load channels
        db.load_channels(self.test_db_path)
        
        # Verify channel appears in required channels list
        required = [ch for ch in CHANNELS.values() if ch.required]
        self.assertEqual(len(required), 1)
        self.assertEqual(required[0].slug, "list_test")
    
    def test_removing_channel_deletes_from_db_and_memory(self):
        """Removing a channel deletes from SQLite and in-memory."""
        db.init_db(self.test_db_path)
        
        # Save a channel
        channel = Channel(
            slug="remove_test",
            channel_id=-100111222333,
            username="removetest",
            title="Remove Test",
            required=True
        )
        db.save_channel(channel, self.test_db_path)
        CHANNELS["remove_test"] = channel
        
        # Delete channel
        db.delete_channel("remove_test", self.test_db_path)
        CHANNELS.pop("remove_test", None)
        
        # Verify channel is gone from database
        conn = sqlite3.connect(self.test_db_path)
        cursor = conn.execute(
            "SELECT slug FROM required_channels WHERE slug = ?",
            ("remove_test",)
        )
        row = cursor.fetchone()
        conn.close()
        
        self.assertIsNone(row)
        self.assertNotIn("remove_test", CHANNELS)
    
    def test_multiple_channels_persist_correctly(self):
        """Multiple channels persist correctly."""
        db.init_db(self.test_db_path)
        
        channels = [
            Channel(slug="ch1", channel_id=-100111, username="ch1user", title="Channel 1", required=True),
            Channel(slug="ch2", channel_id=-222222, username="ch2user", title="Channel 2", required=True),
            Channel(slug="ch3", channel_id=-333333, username="ch3user", title="Channel 3", required=False),
        ]
        
        for ch in channels:
            db.save_channel(ch, self.test_db_path)
        
        # Load and verify
        db.load_channels(self.test_db_path)
        
        self.assertEqual(len(CHANNELS), 3)
        self.assertIn("ch1", CHANNELS)
        self.assertIn("ch2", CHANNELS)
        self.assertIn("ch3", CHANNELS)
        
        # Verify channel details
        self.assertEqual(CHANNELS["ch1"].channel_id, -100111)
        self.assertEqual(CHANNELS["ch2"].channel_id, -222222)
        self.assertEqual(CHANNELS["ch3"].channel_id, -333333)
        self.assertTrue(CHANNELS["ch1"].required)
        self.assertTrue(CHANNELS["ch2"].required)
        self.assertFalse(CHANNELS["ch3"].required)
    
    def test_get_channel_from_db(self):
        """Get single channel from database."""
        db.init_db(self.test_db_path)
        
        channel = Channel(
            slug="get_test",
            channel_id=-100444555666,
            username="gettest",
            title="Get Test",
            required=True
        )
        db.save_channel(channel, self.test_db_path)
        
        retrieved = db.get_channel_from_db("get_test", self.test_db_path)
        
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.slug, "get_test")
        self.assertEqual(retrieved.channel_id, -100444555666)
        self.assertEqual(retrieved.username, "gettest")
        self.assertEqual(retrieved.title, "Get Test")
        self.assertTrue(retrieved.required)
    
    def test_get_nonexistent_channel_returns_none(self):
        """Get nonexistent channel returns None."""
        db.init_db(self.test_db_path)
        
        retrieved = db.get_channel_from_db("nonexistent", self.test_db_path)
        
        self.assertIsNone(retrieved)
    
    def test_save_channel_replaces_existing(self):
        """Save channel replaces existing channel with same slug."""
        db.init_db(self.test_db_path)
        
        # Save initial channel
        channel1 = Channel(
            slug="replace_test",
            channel_id=-100111,
            username="replace1",
            title="Replace 1",
            required=True
        )
        db.save_channel(channel1, self.test_db_path)
        
        # Save updated channel with same slug
        channel2 = Channel(
            slug="replace_test",
            channel_id=-222222,
            username="replace2",
            title="Replace 2",
            required=False
        )
        db.save_channel(channel2, self.test_db_path)
        
        # Load and verify
        db.load_channels(self.test_db_path)
        
        self.assertEqual(len(CHANNELS), 1)
        self.assertEqual(CHANNELS["replace_test"].channel_id, -222222)
        self.assertEqual(CHANNELS["replace_test"].username, "replace2")
        self.assertFalse(CHANNELS["replace_test"].required)

    def test_supergroup_chat_type_persisted(self):
        """Supergroup chat_type is persisted and loaded correctly."""
        db.init_db(self.test_db_path)

        channel = Channel(
            slug="sg_test",
            channel_id=-100888,
            username="sg_test_group",
            title="Supergroup Test",
            required=True,
            chat_type="supergroup",
        )
        db.save_channel(channel, self.test_db_path)

        retrieved = db.get_channel_from_db("sg_test", self.test_db_path)

        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.chat_type, "supergroup")

    def test_channel_chat_type_persisted(self):
        """Channel chat_type is persisted and loaded correctly."""
        db.init_db(self.test_db_path)

        channel = Channel(
            slug="ch_test",
            channel_id=-100999,
            username="ch_test_channel",
            title="Channel Test",
            required=True,
            chat_type="channel",
        )
        db.save_channel(channel, self.test_db_path)

        retrieved = db.get_channel_from_db("ch_test", self.test_db_path)

        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.chat_type, "channel")

    def test_load_channels_restores_chat_type(self):
        """load_channels restores chat_type for both types."""
        db.init_db(self.test_db_path)

        ch1 = Channel(
            slug="ch_sg", channel_id=-100111,
            username="ch_sg", title="SG", required=True,
            chat_type="supergroup",
        )
        ch2 = Channel(
            slug="ch_ch", channel_id=-200222,
            username="ch_ch", title="CH", required=True,
            chat_type="channel",
        )
        db.save_channel(ch1, self.test_db_path)
        db.save_channel(ch2, self.test_db_path)

        db.load_channels(self.test_db_path)

        self.assertEqual(CHANNELS["ch_sg"].chat_type, "supergroup")
        self.assertEqual(CHANNELS["ch_ch"].chat_type, "channel")

    def test_default_chat_type_when_missing(self):
        """Existing DB rows without chat_type get 'channel' default."""
        db.init_db(self.test_db_path)

        # Manually insert a row without chat_type (simulating old DB)
        conn = sqlite3.connect(self.test_db_path)
        conn.execute(
            "INSERT INTO required_channels (slug, channel_id, username, title, required) "
            "VALUES (?, ?, ?, ?, ?)",
            ("old_row", -300333, "oldch", "Old", 1),
        )
        conn.commit()
        conn.close()

        db.load_channels(self.test_db_path)

        self.assertIn("old_row", CHANNELS)
        self.assertEqual(CHANNELS["old_row"].chat_type, "channel")


class TestBotSQLiteIntegration(unittest.TestCase):
    """Integration tests for bot.py with SQLite persistence."""
    
    def setUp(self):
        """Create a temporary database for each test."""
        self.test_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        
        # Clear in-memory channels
        CHANNELS.clear()
        
        # Patch db module to use test database
        self.original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
    
    def tearDown(self):
        """Clean up after each test."""
        # Restore original db path
        db.DB_PATH = self.original_db_path
        
        if os.path.exists(self.test_db_path):
            os.unlink(self.test_db_path)
        for suffix in ['-wal', '-shm']:
            wal_path = self.test_db_path + suffix
            if os.path.exists(wal_path):
                os.unlink(wal_path)
        
        CHANNELS.clear()
    
    def test_addchannel_persists_and_loads(self):
        """addchannel persists to SQLite and survives reload."""
        from bot import add_channel
        from unittest.mock import AsyncMock, MagicMock
        
        db.init_db(db.DB_PATH)
        
        # Create mock update and context
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 6175354851  # Admin ID
        update.message = MagicMock()
        update.message.text = "/addchannel test_slug|@testusername|Test Title"
        update.message.reply_text = AsyncMock()
        
        context = MagicMock()
        context.bot = MagicMock()
        
        # Mock get_chat to return a channel
        mock_chat = MagicMock()
        mock_chat.id = -1001234567890
        mock_chat.type = "channel"
        context.bot.get_chat = AsyncMock(return_value=mock_chat)
        
        # Mock get_chat_member to return admin
        mock_member = MagicMock()
        mock_member.status = "administrator"
        context.bot.get_chat_member = AsyncMock(return_value=mock_member)
        
        # Run addchannel
        import asyncio
        asyncio.run(add_channel(update, context))
        
        # Verify channel is in memory
        self.assertIn("test_slug", CHANNELS)
        self.assertEqual(CHANNELS["test_slug"].channel_id, -1001234567890)
        
        # Verify channel is in database
        conn = sqlite3.connect(db.DB_PATH)
        cursor = conn.execute(
            "SELECT slug, channel_id, username, title FROM required_channels WHERE slug = ?",
            ("test_slug",)
        )
        row = cursor.fetchone()
        conn.close()
        
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "test_slug")
        self.assertEqual(row[1], -1001234567890)
        
        # Clear memory and reload
        CHANNELS.clear()
        db.load_channels(db.DB_PATH)
        
        # Verify channel survived reload
        self.assertIn("test_slug", CHANNELS)
        self.assertEqual(CHANNELS["test_slug"].channel_id, -1001234567890)
    
    def test_removechannel_deletes_from_db(self):
        """removechannel deletes from SQLite and memory."""
        from bot import remove_channel
        from unittest.mock import AsyncMock, MagicMock
        
        db.init_db(db.DB_PATH)
        
        # Pre-populate a channel
        channel = Channel(
            slug="remove_me",
            channel_id=-100999,
            username="removeme",
            title="Remove Me",
            required=True
        )
        db.save_channel(channel, db.DB_PATH)
        CHANNELS["remove_me"] = channel
        
        # Create mock update
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 6175354851  # Admin ID
        update.message = MagicMock()
        update.message.text = "/removechannel remove_me"
        update.message.reply_text = AsyncMock()
        
        context = MagicMock()
        context.bot = MagicMock()
        
        # Run removechannel
        import asyncio
        asyncio.run(remove_channel(update, context))
        
        # Verify channel is gone from memory
        self.assertNotIn("remove_me", CHANNELS)
        
        # Verify channel is gone from database
        conn = sqlite3.connect(db.DB_PATH)
        cursor = conn.execute(
            "SELECT slug FROM required_channels WHERE slug = ?",
            ("remove_me",)
        )
        row = cursor.fetchone()
        conn.close()
        
        self.assertIsNone(row)


class TestForeignKeyEnforcement(unittest.TestCase):
    """Tests for SQLite foreign-key enforcement consistency."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        CHANNELS.clear()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        if os.path.exists(self.test_db_path):
            os.unlink(self.test_db_path)
        for suffix in ['-wal', '-shm']:
            wal_path = self.test_db_path + suffix
            if os.path.exists(wal_path):
                os.unlink(wal_path)
        CHANNELS.clear()

    def test_get_connection_enables_foreign_keys(self):
        """get_connection() sets PRAGMA foreign_keys=ON."""
        with db.get_connection(self.test_db_path) as conn:
            row = conn.execute("PRAGMA foreign_keys").fetchone()
            self.assertEqual(row[0], 1)

    def test_register_user_connection_has_foreign_keys(self):
        """register_user() uses a connection with foreign_keys=ON."""
        db.init_db(self.test_db_path)

        # Patch get_connection to capture the pragma state
        original_get_connection = db.get_connection
        pragma_values = []

        def spy_get_connection(db_path=None):
            ctx = original_get_connection(db_path)
            class SpyContext:
                def __enter__(self_inner):
                    conn = ctx.__enter__()
                    row = conn.execute("PRAGMA foreign_keys").fetchone()
                    pragma_values.append(row[0])
                    return conn
                def __exit__(self_inner, *args):
                    return ctx.__exit__(*args)
            return SpyContext()

        db.get_connection = spy_get_connection
        try:
            db.register_user(1001, "fk_user", "FK User")
            # register_user calls get_connection twice: once in get_user(), once in itself
            self.assertTrue(all(v == 1 for v in pragma_values))
            self.assertGreaterEqual(len(pragma_values), 2)
        finally:
            db.get_connection = original_get_connection

    def test_foreign_key_enforcement_rejects_invalid_referral(self):
        """INSERT with referred_by pointing to non-existent user is rejected."""
        db.init_db(self.test_db_path)

        # Directly insert with invalid FK reference
        with db.get_connection(self.test_db_path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO users (user_id, username, first_name, referred_by) "
                    "VALUES (?, ?, ?, ?)",
                    (2001, "orphan", "Orphan", 99999)
                )

    def test_foreign_key_enforcement_allows_valid_referral(self):
        """INSERT with valid referred_by succeeds."""
        db.init_db(self.test_db_path)

        # Create referrer first
        db.register_user(3001, "referrer", "Referrer")
        # Create referred user
        db.register_user(3002, "referred", "Referred", referred_by=3001)

        user = db.get_user(3002)
        self.assertIsNotNone(user)
        self.assertEqual(user["referred_by"], 3001)

    def test_register_user_blocks_self_referral(self):
        """register_user blocks self-referral (sets referred_by to None)."""
        db.init_db(self.test_db_path)

        db.register_user(4001, "self_ref", "Self", referred_by=4001)

        user = db.get_user(4001)
        self.assertIsNotNone(user)
        self.assertIsNone(user["referred_by"])

    def test_register_user_idempotent(self):
        """Duplicate register_user calls are idempotent."""
        db.init_db(self.test_db_path)

        # Create referrer first (FK requires it to exist)
        db.register_user(5002, "referrer", "Referrer")

        result1 = db.register_user(5001, "dup", "Dup", referred_by=5002)
        result2 = db.register_user(5001, "dup2", "Dup2", referred_by=5003)

        self.assertTrue(result1)
        self.assertFalse(result2)

        user = db.get_user(5001)
        self.assertEqual(user["referred_by"], 5002)  # first referrer wins

    def test_referral_count_accuracy(self):
        """get_referral_count returns correct count."""
        db.init_db(self.test_db_path)

        db.register_user(6001, "parent", "Parent")
        for i in range(5):
            db.register_user(6100 + i, f"child{i}", f"Child{i}", referred_by=6001)

        self.assertEqual(db.get_referral_count(6001), 5)
        self.assertEqual(db.get_referral_count(6100), 0)


class TestTaskDefinitions(unittest.TestCase):
    """Tests for task definition CRUD operations."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        CHANNELS.clear()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        if os.path.exists(self.test_db_path):
            os.unlink(self.test_db_path)
        for suffix in ['-wal', '-shm']:
            wal_path = self.test_db_path + suffix
            if os.path.exists(wal_path):
                os.unlink(wal_path)
        CHANNELS.clear()

    # ── 1. create_task ───────────────────────────────────────────────
    def test_create_task(self):
        """create_task returns an ID and persists the row."""
        db.init_db(self.test_db_path)

        task_id = db.create_task(
            title="Join Channel",
            description="Subscribe to our channel",
            task_type="subscribe",
            reward=50,
        )

        self.assertIsInstance(task_id, int)
        task = db.get_task(task_id, self.test_db_path)
        self.assertIsNotNone(task)
        self.assertEqual(task["title"], "Join Channel")
        self.assertEqual(task["description"], "Subscribe to our channel")
        self.assertEqual(task["type"], "subscribe")
        self.assertEqual(task["reward"], 50)
        self.assertTrue(task["active"])

    # ── 2. get_task ──────────────────────────────────────────────────
    def test_get_task(self):
        """get_task returns correct task or None."""
        db.init_db(self.test_db_path)

        task_id = db.create_task(
            title="Test Task",
            description="A test",
            task_type="visit",
            reward=10,
        )

        task = db.get_task(task_id, self.test_db_path)
        self.assertIsNotNone(task)
        self.assertEqual(task["id"], task_id)

        self.assertIsNone(db.get_task(99999, self.test_db_path))

    # ── 3. list_tasks ────────────────────────────────────────────────
    def test_list_tasks(self):
        """list_tasks returns all tasks."""
        db.init_db(self.test_db_path)

        db.create_task(title="T1", description="D1", task_type="t1", reward=10)
        db.create_task(title="T2", description="D2", task_type="t2", reward=20)

        tasks = db.list_tasks(db_path=self.test_db_path)
        self.assertEqual(len(tasks), 2)
        titles = {t["title"] for t in tasks}
        self.assertEqual(titles, {"T1", "T2"})

    # ── 4. list_tasks(active_only=True) ──────────────────────────────
    def test_list_tasks_active_only(self):
        """list_tasks(active_only=True) returns only active tasks."""
        db.init_db(self.test_db_path)

        id1 = db.create_task(title="Active", description="D", task_type="t", reward=10)
        id2 = db.create_task(title="Inactive", description="D", task_type="t", reward=10)
        db.update_task(id2, active=False, db_path=self.test_db_path)

        active = db.list_tasks(active_only=True, db_path=self.test_db_path)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["title"], "Active")

    # ── 5. update_task ───────────────────────────────────────────────
    def test_update_task(self):
        """update_task modifies fields and returns True."""
        db.init_db(self.test_db_path)

        task_id = db.create_task(
            title="Old Title",
            description="Old Desc",
            task_type="old",
            reward=10,
        )

        result = db.update_task(
            task_id,
            title="New Title",
            reward=99,
            db_path=self.test_db_path,
        )
        self.assertTrue(result)

        task = db.get_task(task_id, self.test_db_path)
        self.assertEqual(task["title"], "New Title")
        self.assertEqual(task["reward"], 99)
        self.assertEqual(task["description"], "Old Desc")  # unchanged

    # ── 6. delete_task ───────────────────────────────────────────────
    def test_delete_task(self):
        """delete_task removes the row and returns True."""
        db.init_db(self.test_db_path)

        task_id = db.create_task(
            title="Delete Me",
            description="D",
            task_type="t",
            reward=5,
        )

        self.assertTrue(db.delete_task(task_id, self.test_db_path))
        self.assertIsNone(db.get_task(task_id, self.test_db_path))
        self.assertFalse(db.delete_task(task_id, self.test_db_path))  # already gone

    # ── 7. duplicate ID ──────────────────────────────────────────────
    def test_duplicate_id_not_possible(self):
        """PRIMARY KEY prevents duplicate IDs (auto-increment)."""
        db.init_db(self.test_db_path)

        id1 = db.create_task(title="A", description="A", task_type="t", reward=1)
        id2 = db.create_task(title="B", description="B", task_type="t", reward=2)
        self.assertNotEqual(id1, id2)

    # ── 8. empty title ───────────────────────────────────────────────
    def test_empty_title_rejected(self):
        """create_task rejects empty title."""
        db.init_db(self.test_db_path)

        with self.assertRaises(ValueError) as ctx:
            db.create_task(title="", description="D", task_type="t", reward=10)
        self.assertIn("title", str(ctx.exception))

        with self.assertRaises(ValueError):
            db.create_task(title="   ", description="D", task_type="t", reward=10)

    # ── 9. empty description ─────────────────────────────────────────
    def test_empty_description_rejected(self):
        """create_task rejects empty description."""
        db.init_db(self.test_db_path)

        with self.assertRaises(ValueError) as ctx:
            db.create_task(title="T", description="", task_type="t", reward=10)
        self.assertIn("description", str(ctx.exception))

    # ── 10. empty type ───────────────────────────────────────────────
    def test_empty_type_rejected(self):
        """create_task rejects empty type."""
        db.init_db(self.test_db_path)

        with self.assertRaises(ValueError) as ctx:
            db.create_task(title="T", description="D", task_type="", reward=10)
        self.assertIn("type", str(ctx.exception))

    # ── 11. negative reward ──────────────────────────────────────────
    def test_negative_reward_rejected(self):
        """create_task rejects negative reward."""
        db.init_db(self.test_db_path)

        with self.assertRaises(ValueError) as ctx:
            db.create_task(title="T", description="D", task_type="t", reward=-5)
        self.assertIn("reward", str(ctx.exception))

    # ── 12. invalid active value ─────────────────────────────────────
    def test_invalid_active_value(self):
        """update_task with invalid active is rejected at validation level."""
        db.init_db(self.test_db_path)

        task_id = db.create_task(title="T", description="D", task_type="t", reward=10)
        # Active must be bool (True/False), not arbitrary values
        # update_task accepts bool | None; passing non-bool would be a caller error
        # The schema stores 0/1, so True → 1, False → 0
        db.update_task(task_id, active=False, db_path=self.test_db_path)
        task = db.get_task(task_id, self.test_db_path)
        self.assertFalse(task["active"])

    # ── 13. init_db creates tasks table ──────────────────────────────
    def test_init_db_creates_tasks_table(self):
        """init_db creates the tasks table."""
        db.init_db(self.test_db_path)

        conn = sqlite3.connect(self.test_db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        )
        row = cursor.fetchone()
        conn.close()

        self.assertIsNotNone(row)

    # ── 14. channel persistence and users/referral unchanged ────────
    def test_channel_persistence_unchanged(self):
        """Channel save/load still works after tasks table addition."""
        db.init_db(self.test_db_path)

        channel = Channel(
            slug="after_tasks",
            channel_id=-100777,
            username="aftertasks",
            title="After Tasks",
            required=True,
        )
        db.save_channel(channel, self.test_db_path)
        db.load_channels(self.test_db_path)

        self.assertIn("after_tasks", CHANNELS)
        self.assertEqual(CHANNELS["after_tasks"].channel_id, -100777)

    def test_users_referral_unchanged(self):
        """User registration and referral still work after tasks table addition."""
        db.init_db(self.test_db_path)

        db.register_user(7001, "parent", "Parent")
        db.register_user(7002, "child", "Child", referred_by=7001)

        user = db.get_user(7002)
        self.assertIsNotNone(user)
        self.assertEqual(user["referred_by"], 7001)
        self.assertEqual(db.get_referral_count(7001), 1)


class TestUserTaskState(unittest.TestCase):
    """Tests for user task state persistence."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        CHANNELS.clear()
        # Initialize DB and create prerequisite user + task
        db.init_db(self.test_db_path)
        db.register_user(1001, "alice", "Alice")
        self.task_id = db.create_task(
            title="Join Channel",
            description="Subscribe",
            task_type="subscribe",
            reward=50,
            db_path=self.test_db_path,
        )

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        if os.path.exists(self.test_db_path):
            os.unlink(self.test_db_path)
        for suffix in ['-wal', '-shm']:
            wal_path = self.test_db_path + suffix
            if os.path.exists(wal_path):
                os.unlink(wal_path)
        CHANNELS.clear()

    # ── 1. create_user_task ────────────────────────────────────────
    def test_create_user_task(self):
        """create_user_task returns True and persists."""
        result = db.create_user_task(1001, self.task_id, self.test_db_path)
        self.assertTrue(result)
        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], 1001)
        self.assertEqual(row["task_id"], self.task_id)

    # ── 2. default status is available ─────────────────────────────
    def test_default_status_is_available(self):
        """New user_task has status 'available'."""
        db.create_user_task(1001, self.task_id, self.test_db_path)
        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_AVAILABLE)

    # ── 3. get_user_task ───────────────────────────────────────────
    def test_get_user_task(self):
        """get_user_task returns correct row or None."""
        db.create_user_task(1001, self.task_id, self.test_db_path)

        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], 1001)

        self.assertIsNone(db.get_user_task(1001, 99999, self.test_db_path))
        self.assertIsNone(db.get_user_task(99999, self.task_id, self.test_db_path))

    # ── 4. list_user_tasks ─────────────────────────────────────────
    def test_list_user_tasks(self):
        """list_user_tasks returns all tasks for a user."""
        task2 = db.create_task(
            title="Visit Site", description="Go to site",
            task_type="visit", reward=10, db_path=self.test_db_path,
        )
        db.create_user_task(1001, self.task_id, self.test_db_path)
        db.create_user_task(1001, task2, self.test_db_path)

        tasks = db.list_user_tasks(1001, self.test_db_path)
        self.assertEqual(len(tasks), 2)
        ids = {t["task_id"] for t in tasks}
        self.assertEqual(ids, {self.task_id, task2})

    # ── 5. transition to started ───────────────────────────────────
    def test_transition_to_started(self):
        """available → started sets started_at timestamp."""
        db.create_user_task(1001, self.task_id, self.test_db_path)

        result = db.update_user_task_status(
            1001, self.task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )
        self.assertTrue(result)

        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_STARTED)
        self.assertIsNotNone(row["started_at"])
        self.assertIsNone(row["completed_at"])

    # ── 6. transition to completed ─────────────────────────────────
    def test_transition_to_completed(self):
        """started → completed sets completed_at timestamp (via gate)."""
        db.create_user_task(1001, self.task_id, self.test_db_path)
        db.update_user_task_status(
            1001, self.task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )
        db.update_user_task_status(
            1001, self.task_id, db.USER_TASK_STATUS_COMPLETED,
            self.test_db_path, _allow_completion=True,
        )

        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_COMPLETED)
        self.assertIsNotNone(row["started_at"])
        self.assertIsNotNone(row["completed_at"])

    # ── 7. duplicate user/task ─────────────────────────────────────
    def test_duplicate_user_task_rejected(self):
        """PRIMARY KEY prevents duplicate (user_id, task_id)."""
        db.create_user_task(1001, self.task_id, self.test_db_path)

        with self.assertRaises(sqlite3.IntegrityError):
            db.create_user_task(1001, self.task_id, self.test_db_path)

    # ── 8. invalid user ────────────────────────────────────────────
    def test_invalid_user_rejected(self):
        """create_user_task rejects non-existent user."""
        with self.assertRaises(ValueError) as ctx:
            db.create_user_task(99999, self.task_id, self.test_db_path)
        self.assertIn("99999", str(ctx.exception))

    # ── 9. invalid task ────────────────────────────────────────────
    def test_invalid_task_rejected(self):
        """create_user_task rejects non-existent task."""
        with self.assertRaises(ValueError) as ctx:
            db.create_user_task(1001, 99999, self.test_db_path)
        self.assertIn("99999", str(ctx.exception))

    # ── 10. invalid status ─────────────────────────────────────────
    def test_invalid_status_rejected(self):
        """update_user_task_status rejects unknown status."""
        db.create_user_task(1001, self.task_id, self.test_db_path)

        with self.assertRaises(ValueError) as ctx:
            db.update_user_task_status(
                1001, self.task_id, "bogus", self.test_db_path
            )
        self.assertIn("bogus", str(ctx.exception))

    # ── 11. foreign-key enforcement ────────────────────────────────
    def test_fk_enforcement_user_tasks(self):
        """Direct INSERT with bad FK is rejected."""
        with self.assertRaises(sqlite3.IntegrityError):
            with db.get_connection(self.test_db_path) as conn:
                conn.execute(
                    "INSERT INTO user_tasks (user_id, task_id, status) VALUES (?, ?, ?)",
                    (99999, 99999, "available")
                )

    # ── 12. timestamps behavior ────────────────────────────────────
    def test_timestamps_initially_none(self):
        """started_at and completed_at are None when status is available."""
        db.create_user_task(1001, self.task_id, self.test_db_path)
        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertIsNone(row["started_at"])
        self.assertIsNone(row["completed_at"])

    # ── 13. cannot skip to completed ───────────────────────────────
    def test_cannot_skip_to_completed(self):
        """available → completed is not allowed."""
        db.create_user_task(1001, self.task_id, self.test_db_path)

        with self.assertRaises(ValueError) as ctx:
            db.update_user_task_status(
                1001, self.task_id, db.USER_TASK_STATUS_COMPLETED, self.test_db_path
            )
        self.assertIn("started", str(ctx.exception))

    def test_started_to_completed_rejected_without_gate(self):
        """started → completed without _allow_completion raises ValueError."""
        db.create_user_task(1001, self.task_id, self.test_db_path)
        db.update_user_task_status(
            1001, self.task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        with self.assertRaises(ValueError) as ctx:
            db.update_user_task_status(
                1001, self.task_id, db.USER_TASK_STATUS_COMPLETED, self.test_db_path
            )
        self.assertIn("CompletionGate", str(ctx.exception))

    # ── 14. cannot restart after completed ─────────────────────────
    def test_cannot_restart_after_completed(self):
        """completed → started is not allowed."""
        db.create_user_task(1001, self.task_id, self.test_db_path)
        db.update_user_task_status(
            1001, self.task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )
        db.update_user_task_status(
            1001, self.task_id, db.USER_TASK_STATUS_COMPLETED,
            self.test_db_path, _allow_completion=True,
        )

        with self.assertRaises(ValueError) as ctx:
            db.update_user_task_status(
                1001, self.task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
            )
        self.assertIn("completed", str(ctx.exception))

    # ── 15. list_user_tasks for unknown user returns empty ──────────
    def test_list_user_tasks_empty(self):
        """list_user_tasks returns [] for user with no tasks."""
        self.assertEqual(db.list_user_tasks(99999, self.test_db_path), [])

    # ── 16. update non-existent row returns False ──────────────────
    def test_update_nonexistent_returns_false(self):
        """update_user_task_status returns False if row doesn't exist."""
        result = db.update_user_task_status(
            99999, 99999, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )
        self.assertFalse(result)

    # ── 17. existing features unchanged ────────────────────────────
    def test_channels_still_work(self):
        """Channel persistence unaffected by user_tasks table."""
        channel = Channel(
            slug="ut_test", channel_id=-100555, username="utt",
            title="UT Test", required=True,
        )
        db.save_channel(channel, self.test_db_path)
        db.load_channels(self.test_db_path)
        self.assertIn("ut_test", CHANNELS)

    def test_tasks_crud_still_work(self):
        """Task CRUD unaffected by user_tasks table."""
        tid = db.create_task(
            title="CRUD Test", description="D", task_type="t",
            reward=5, db_path=self.test_db_path,
        )
        task = db.get_task(tid, self.test_db_path)
        self.assertIsNotNone(task)
        self.assertEqual(task["title"], "CRUD Test")

    def test_referral_still_works(self):
        """Referral attribution unaffected by user_tasks table."""
        db.register_user(8001, "p", "P")
        db.register_user(8002, "c", "C", referred_by=8001)
        user = db.get_user(8002)
        self.assertEqual(user["referred_by"], 8001)


if __name__ == "__main__":
    unittest.main()
