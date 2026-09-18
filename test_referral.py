"""
Unit tests for TaskCoin Referral Attribution System
"""

import os
import sqlite3
import tempfile
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Update, User
from telegram.ext import ContextTypes

import db


@pytest.fixture(autouse=True)
def setup_test_db():
    """Create a fresh test database for each test."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        test_db_path = f.name
    
    # Patch the DB_PATH
    original_db_path = db.DB_PATH
    db.DB_PATH = test_db_path
    
    # Initialize database
    db.init_db()
    
    yield test_db_path
    
    # Cleanup
    db.DB_PATH = original_db_path
    os.unlink(test_db_path)


@pytest.fixture
def mock_update():
    """Create a mock Update object."""
    update = MagicMock(spec=Update)
    update.effective_user = MagicMock(spec=User)
    update.effective_user.id = 12345
    update.effective_user.username = "testuser"
    update.effective_user.first_name = "Test"
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


@pytest.fixture
def mock_context():
    """Create a mock Context object."""
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.args = []
    return context


class TestReferralAttribution:
    """Test suite for referral attribution system."""

    def test_start_without_referral(self, mock_update, mock_context):
        """Test /start without any referral payload."""
        from bot import start
        
        # Run the handler
        import asyncio
        asyncio.run(start(mock_update, mock_context))
        
        # Verify user was registered without referrer
        user = db.get_user(mock_update.effective_user.id)
        assert user is not None
        assert user["user_id"] == mock_update.effective_user.id
        assert user["referred_by"] is None

    def test_start_with_valid_referral(self, mock_update, mock_context):
        """Test /start with a valid referral payload."""
        from bot import start
        
        referrer_id = 99999
        mock_context.args = [str(referrer_id)]
        
        # First, create the referrer user
        db.register_user(referrer_id, "referrer", "Referrer")
        
        # Run the handler
        import asyncio
        asyncio.run(start(mock_update, mock_context))
        
        # Verify user was registered with referrer
        user = db.get_user(mock_update.effective_user.id)
        assert user is not None
        assert user["referred_by"] == referrer_id

    def test_self_referral_blocked(self, mock_update, mock_context):
        """Test that self-referral is blocked."""
        from bot import start
        
        user_id = mock_update.effective_user.id
        mock_context.args = [str(user_id)]  # Try to refer yourself
        
        # Run the handler
        import asyncio
        asyncio.run(start(mock_update, mock_context))
        
        # Verify user was registered without referrer (self-referral blocked)
        user = db.get_user(user_id)
        assert user is not None
        assert user["referred_by"] is None

    def test_duplicate_referral_idempotent(self, mock_update, mock_context):
        """Test that duplicate referrals are idempotent."""
        from bot import start
        
        referrer_id = 88888
        mock_context.args = [str(referrer_id)]
        
        # Create referrer
        db.register_user(referrer_id, "referrer", "Referrer")
        
        # First registration
        import asyncio
        asyncio.run(start(mock_update, mock_context))
        
        # Try to register again with same referrer
        asyncio.run(start(mock_update, mock_context))
        
        # Verify user still exists with same referrer
        user = db.get_user(mock_update.effective_user.id)
        assert user is not None
        assert user["referred_by"] == referrer_id

    def test_cannot_change_referrer(self, mock_update, mock_context):
        """Test that referrer cannot be changed after initial registration."""
        from bot import start
        
        first_referrer = 77777
        second_referrer = 66666
        
        # Create both referrers
        db.register_user(first_referrer, "first", "First")
        db.register_user(second_referrer, "second", "Second")
        
        # Register with first referrer
        mock_context.args = [str(first_referrer)]
        import asyncio
        asyncio.run(start(mock_update, mock_context))
        
        # Try to change to second referrer
        mock_context.args = [str(second_referrer)]
        asyncio.run(start(mock_update, mock_context))
        
        # Verify referrer didn't change
        user = db.get_user(mock_update.effective_user.id)
        assert user is not None
        assert user["referred_by"] == first_referrer

    def test_new_referred_user_with_existing_referrer(self, mock_update, mock_context):
        """Test new referred user when referrer already exists."""
        from bot import start
        
        referrer_id = 55555
        mock_context.args = [str(referrer_id)]
        
        # Create referrer
        db.register_user(referrer_id, "referrer", "Referrer")
        
        # Register new user with referral
        import asyncio
        asyncio.run(start(mock_update, mock_context))
        
        # Verify user was registered with referrer
        user = db.get_user(mock_update.effective_user.id)
        assert user is not None
        assert user["referred_by"] == referrer_id
        
        # Verify referrer exists
        referrer = db.get_user(referrer_id)
        assert referrer is not None

    def test_no_username(self, mock_context):
        """Test registration without username (optional field)."""
        from bot import start
        
        update = MagicMock(spec=Update)
        update.effective_user = MagicMock(spec=User)
        update.effective_user.id = 44444
        update.effective_user.username = None  # No username
        update.effective_user.first_name = "NoUsername"
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        
        mock_context.args = []
        
        # Run the handler
        import asyncio
        asyncio.run(start(update, mock_context))
        
        # Verify user was registered with None username
        user = db.get_user(44444)
        assert user is not None
        assert user["username"] is None
        assert user["first_name"] == "NoUsername"

    def test_invalid_referral_payload(self, mock_update, mock_context):
        """Test with invalid (non-numeric) referral payload."""
        from bot import start
        
        mock_context.args = ["invalid_payload"]
        
        # Run the handler
        import asyncio
        asyncio.run(start(mock_update, mock_context))
        
        # Verify user was registered without referrer
        user = db.get_user(mock_update.effective_user.id)
        assert user is not None
        assert user["referred_by"] is None

    def test_referral_count(self):
        """Test referral counting."""
        referrer_id = 33333
        db.register_user(referrer_id, "referrer", "Referrer")
        
        # Register multiple users referred by same person
        for i in range(3):
            db.register_user(30000 + i, f"user{i}", f"User{i}", referred_by=referrer_id)
        
        # Verify count
        count = db.get_referral_count(referrer_id)
        assert count == 3

    def test_get_referrer(self):
        """Test getting referrer for a user."""
        referrer_id = 22222
        user_id = 22223
        
        db.register_user(referrer_id, "referrer", "Referrer")
        db.register_user(user_id, "user", "User", referred_by=referrer_id)
        
        # Get referrer
        referrer = db.get_referrer(user_id)
        assert referrer is not None
        assert referrer["user_id"] == referrer_id

    def test_get_referrer_without_referrer(self):
        """Test getting referrer for user without one."""
        user_id = 11111
        db.register_user(user_id, "user", "User")
        
        # Get referrer (should be None)
        referrer = db.get_referrer(user_id)
        assert referrer is None
