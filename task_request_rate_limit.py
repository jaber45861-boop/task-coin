"""
Per-user rate limit for ``POST /api/task-requests``
===================================================

Defense-in-depth against scripted floods of task proposals: the
validation layer already rejects bad payloads — this layer bounds
how fast a single verified user may *attempt* them.

Scope — deliberately narrow:
- consulted by EXACTLY ONE handler: ``create_task_request`` in
  ``task_routes`` (POST only).  GET list/detail, PATCH resubmit, the
  Telegram admin workflow (``task_request_admin``) and the request
  state machine (``task_request_store``) never touch it, so the
  natural create → ``changes_requested`` → resubmit (PATCH) cycle
  can never be blocked here — PATCH is never counted.
- the key is the VERIFIED Telegram ``user_id`` (initData), never an
  IP address: users behind one NAT can never starve each other, and
  a spoofed header cannot move the counter because identity is
  already verified before this module is consulted.

Concurrency:
- check + count happen atomically inside ONE ``threading.Lock`` —
  there is no separate read-then-write window for a burst of
  simultaneous requests to slip through — and a rejected attempt is
  NOT counted, so the block always lifts after ``WINDOW_SECONDS``.

State is in-process: the Mini App HTTP surface runs in the single
``serve_miniapp`` web process (the ``bot.py`` worker never calls
this module).  ``reset()`` exists only so every test starts from an
empty window (see the autouse fixture in ``conftest.py``).

Run:
    python3 -m pytest test_task_requests_api.py -v
"""

from __future__ import annotations

import math
import threading
import time

# ── The only knobs ───────────────────────────────────────────────────
# Authenticated POST attempts (valid OR invalid payloads — every
# attempt) allowed per user inside one window.  Deliberately generous:
# proposing tasks is a human-paced flow, and resubmitting a request
# the admin returned for changes goes through PATCH, which this
# limiter never sees.
MAX_ATTEMPTS_PER_WINDOW = 10
WINDOW_SECONDS = 60.0

# The windows dict is pruned wholesale once it grows past this many
# users, so a long-lived process never accumulates stale entries.
_PRUNE_THRESHOLD = 1024

_lock = threading.Lock()
# user_id -> (window start on the monotonic clock, attempts counted)
_windows: dict[int, tuple[float, int]] = {}


def _now() -> float:
    """Monotonic clock seam (tests shift time through this hook)."""
    return time.monotonic()


def _prune(now: float) -> None:
    """Drop expired windows (only ever called past _PRUNE_THRESHOLD)."""
    expired = [
        uid
        for uid, (start, _) in _windows.items()
        if now - start >= WINDOW_SECONDS
    ]
    for uid in expired:
        del _windows[uid]


def check_attempt(user_id: int) -> int | None:
    """Atomically count one POST attempt for ``user_id``.

    Returns ``None`` when the attempt is allowed (the window now
    holds one more attempt), otherwise the number of whole seconds
    until the window resets — the caller rejects the attempt with
    HTTP 429.  Rejected attempts are never counted, so the counter
    cannot ratchet a user into a longer block.
    """
    now = _now()
    with _lock:
        entry = _windows.get(user_id)
        if entry is None or now - entry[0] >= WINDOW_SECONDS:
            entry = (now, 0)
        start, count = entry
        if count >= MAX_ATTEMPTS_PER_WINDOW:
            remaining = WINDOW_SECONDS - (now - start)
            return max(1, math.ceil(remaining))
        _windows[user_id] = (start, count + 1)
        if len(_windows) > _PRUNE_THRESHOLD:
            _prune(now)
        return None


def reset() -> None:
    """Forget every per-user window (test isolation only — no route
    or service ever calls this at runtime)."""
    with _lock:
        _windows.clear()
