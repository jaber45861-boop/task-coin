"""
Automatic Rate Refresh Worker
=============================

Single background daemon that keeps the authoritative EGP-per-USDT
quote fresh — the NORMAL operating mode of the rate policy.

Loop contract:

- one fetch + persist cycle every ``REFRESH_INTERVAL_SECONDS`` (840 s,
  deliberately below the 900 s freshness TTL so a successful refresh
  always lands before the previous quote expires);
- the FIRST cycle runs immediately at ``start()`` — a startup failure
  simply leaves ``current_rate`` missing/aged, and
  ``rate_store.get_current_quote`` fails closed (EGP deposit creation
  is refused until a fresh quote exists);
- a FAILED cycle writes nothing: the previous quote keeps its
  original ``captured_at`` and therefore ages out on schedule —
  failure never extends freshness;
- a SUCCESSFUL cycle overwrites any row (including a manual
  emergency quote) via ``rate_store.set_automatic_rate`` — that is
  the automatic return-to-normal path after provider recovery;
- the worker never validates freshness itself (``rate_store`` owns
  the single staleness policy) and never touches wallet/ledger/
  deposit state.

Only ONE thread ever runs (``start`` is idempotent); ``stop`` exists
for controlled shutdown and tests.  The thread is a daemon: it never
blocks process exit.

Run:
    python3 -m pytest test_rate_worker.py -v
"""

from __future__ import annotations

import logging
import threading

import rate_api
import rate_store

logger = logging.getLogger(__name__)

# Refresh before the 900-second freshness boundary (rate_store.RATE_TTL_SECONDS).
REFRESH_INTERVAL_SECONDS = 840

_worker_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _refresh_once() -> bool:
    """One fetch + persist cycle.  Returns True only on success.

    Any failure is logged and swallowed: nothing is persisted, the
    previous quote is untouched and its TTL keeps ticking.
    """
    try:
        quote = rate_api.fetch_usdt_egp_quote()
        rate_store.set_automatic_rate(quote)
    except Exception:
        logger.exception("Automatic USDT/EGP quote refresh failed")
        return False
    logger.info(
        "Automatic USDT/EGP quote refreshed: provider=%s captured_at=%s",
        quote.provider, quote.captured_at.isoformat(),
    )
    return True


def _run() -> None:
    while not _stop_event.is_set():
        _refresh_once()
        if _stop_event.wait(REFRESH_INTERVAL_SECONDS):
            break


def start() -> None:
    """Start one daemon worker; safe to call more than once."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _stop_event.clear()
        _worker_thread = threading.Thread(
            target=_run, name="taskcoin-rate-refresh", daemon=True
        )
        _worker_thread.start()


def stop(timeout: float = 2.0) -> None:
    """Ask the worker to stop, primarily for controlled shutdown/tests."""
    _stop_event.set()
    thread = _worker_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
