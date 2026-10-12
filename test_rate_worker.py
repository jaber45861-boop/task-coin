"""
Tests — Automatic Rate Refresh Worker (rate_worker)
====================================================

Requirement coverage:

- a successful cycle persists the quote (provider, capture instant,
  system actor id 0);
- the FIRST cycle after startup failing writes nothing (no quote →
  EGP deposits fail closed upstream);
- a LATER failed cycle never rewrites ``captured_at`` — freshness is
  not silently extended; the existing TTL still rejects the old row;
- after provider recovery a successful cycle overwrites a manual
  emergency quote and re-closes the manual gate (return to normal);
- ``start()`` spawns exactly one daemon thread (idempotent) and
  ``stop()`` joins it;
- the refresh interval is strictly below the freshness TTL.

No test performs real network I/O — ``rate_api.fetch_usdt_egp_quote``
is always patched.

Run:
    python3 -m pytest test_rate_worker.py -v
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

import config
import db
import rate_api
import rate_store
import rate_worker
from rate_quote import PROVIDER_COINGECKO, RateQuote
from rate_store import (
    ManualRateBlockedError,
    RATE_TTL_SECONDS,
    RateStaleError,
    RateUnavailableError,
)

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
_ADMIN = 777_001


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated database + admin list (set_rate needs one)."""
    db_path = str(tmp_path / "rate_worker.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    monkeypatch.setattr(config, "ADMINS", [_ADMIN])
    yield db_path


def _quote(rate: str = "48.6", at: datetime = NOW) -> RateQuote:
    return RateQuote(Decimal(rate), PROVIDER_COINGECKO, at)


def _raw_rate_row(db_path: str):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT rate_usdt_egp, provider, captured_at, updated_by "
            "FROM current_rate WHERE id = 1"
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


# ── One refresh cycle ─────────────────────────────────────────────────


def test_refresh_once_success_persists_quote(env):
    with patch.object(
        rate_api, "fetch_usdt_egp_quote", return_value=_quote()
    ) as fetch:
        assert rate_worker._refresh_once() is True
    fetch.assert_called_once()

    row = _raw_rate_row(env)
    assert row["provider"] == PROVIDER_COINGECKO
    assert row["rate_usdt_egp"] == "48.6"
    assert row["updated_by"] == 0                # system actor, not admin
    assert row["captured_at"]                    # capture instant stored
    # The stored quote is readable as a fresh quote at its capture time.
    quote = rate_store.get_current_quote(db_path=env, now=NOW)
    assert quote.provider == PROVIDER_COINGECKO
    assert quote.rate_usdt_egp == Decimal("48.6")


def test_first_refresh_failure_leaves_no_quote(env):
    """Startup failure: nothing is written, and reading fails closed."""
    with patch.object(
        rate_api, "fetch_usdt_egp_quote",
        side_effect=rate_api.RateProviderError("down"),
    ):
        assert rate_worker._refresh_once() is False

    assert _raw_rate_row(env) is None
    with pytest.raises(RateUnavailableError):
        rate_store.get_current_quote(db_path=env, now=NOW)


def test_failed_refresh_never_extends_quote_freshness(env):
    """A later failure keeps the old capture instant — the TTL still
    rejects the aged quote (no silent freshness extension)."""
    with patch.object(
        rate_api, "fetch_usdt_egp_quote", return_value=_quote()
    ):
        assert rate_worker._refresh_once() is True
    before = _raw_rate_row(env)

    with patch.object(
        rate_api, "fetch_usdt_egp_quote",
        side_effect=rate_api.RateProviderError("down"),
    ):
        assert rate_worker._refresh_once() is False

    after = _raw_rate_row(env)
    assert after == before                        # byte-for-byte untouched

    with pytest.raises(RateStaleError):
        rate_store.get_current_quote(
            db_path=env, now=NOW + timedelta(seconds=RATE_TTL_SECONDS)
        )


def test_recovery_overwrites_manual_emergency_quote(env):
    """Return to normal: a successful automatic cycle replaces the
    emergency manual quote and the manual gate re-closes."""
    rate_store.set_rate("47.9", admin_user_id=_ADMIN, db_path=env)
    # While the manual quote is current, another manual update works.
    rate_store.set_rate("47.8", admin_user_id=_ADMIN, db_path=env)
    assert _raw_rate_row(env)["provider"] == "manual"

    # A REAL capture instant (like production): genuinely fresh.
    fresh = _quote("48.7", at=datetime.now(timezone.utc))
    with patch.object(
        rate_api, "fetch_usdt_egp_quote", return_value=fresh
    ):
        assert rate_worker._refresh_once() is True

    row = _raw_rate_row(env)
    assert row["provider"] == PROVIDER_COINGECKO
    assert row["rate_usdt_egp"] == "48.7"

    # Normal mode: the emergency path is closed again.
    with pytest.raises(ManualRateBlockedError):
        rate_store.set_rate("48.0", admin_user_id=_ADMIN, db_path=env)
    assert _raw_rate_row(env)["rate_usdt_egp"] == "48.7"


def test_interval_is_below_the_freshness_ttl():
    assert rate_worker.REFRESH_INTERVAL_SECONDS < RATE_TTL_SECONDS


# ── Thread lifecycle ──────────────────────────────────────────────────


def test_start_is_idempotent_and_stop_joins(env):
    fetched = threading.Event()

    def fake_fetch():
        fetched.set()
        return _quote(at=datetime.now(timezone.utc))

    try:
        with patch.object(
            rate_api, "fetch_usdt_egp_quote", side_effect=fake_fetch
        ):
            rate_worker.start()
            first = rate_worker._worker_thread
            assert first is not None and first.is_alive()
            assert first.daemon is True
            # The first cycle actually ran while patched (no real I/O).
            assert fetched.wait(timeout=5)
            rate_worker.start()                   # no second thread
            assert rate_worker._worker_thread is first
    finally:
        rate_worker.stop(timeout=5)

    assert not first.is_alive()
    # The first cycle persisted a quote before the loop parked.
    assert _raw_rate_row(env) is not None
