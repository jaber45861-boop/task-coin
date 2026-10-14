"""
Account Summary Service (Mini App «حسابي»)
=================================================

Read-only composition of the caller's own account figures
for the Account page.  Every number is assembled from the
EXISTING sources of truth — this module owns no tables, no
mutations and no money logic of its own:

    - available balance   ``wallet.wallet_units()`` →
                          ``wallets.available_units``
                          (a user without a wallet row reads
                          as a real zero — the wallet service
                          contract, never "unknown")
    - task counts         ``db.list_user_tasks()`` → the
                          caller's own ``user_tasks`` rows
    - lifetime earnings   ``LedgerService.list_user_entries()``
                          → the ledger's ``credit``/``task``
                          entries — the one earnings source in
                          the system (``task_reward.py`` is the
                          only writer of such rows; deposits,
                          withdrawals, referrals and admin
                          credits carry other reference types
                          and are excluded)

No XP or progression tier exists anywhere in the backend, so
none is computed here.

The module is strictly read-only: it never opens a write
transaction, never mutates a row and never creates a wallet.
All ledger SQL stays inside ``ledger.py`` — this service only
calls the ledger's public read API.

Security: the caller's identity is decided by the transport
(``task_routes.py``) from verified Telegram initData — this
service only ever sees the already-verified ``user_id``.
"""

import db
import wallet
from ledger import LedgerService

# The one earnings source: task-completion rewards.
# task_reward.py writes exactly these ledger rows
# (TASK_REFERENCE_TYPE = "task").
_EARNINGS_ENTRY_TYPE = "credit"
_EARNINGS_REFERENCE_TYPE = "task"


def _user_has_wallet(user_id: int) -> bool:
    """True when the user has a wallet row.

    A user without a wallet row has no balance at all —
    distinct from a wallet that exists with a zero balance.
    """
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row is not None


def load_summary(user_id: int) -> dict:
    """The caller's own read-only account figures.

    Returns:
        {
            "wallet": {"available_units": int} | null,
            "stats": {
                "completed_tasks": int,
                "in_progress_tasks": int,
                "earned_units": int,
            },
        }

    ``wallet`` is ``null`` when the user has no wallet row at all
    (never had a wallet created).  When the user has a wallet —
    even with a zero balance — ``wallet`` is
    ``{"available_units": <int>}`` with the real available_units
    from ``wallets.available_units``.

    All money is integer USDT units (1 USDT = 100,000,000
    units).  A user with no wallet, no tasks and no earnings
    reads as real zeros — never as unknown.

    Raises:
        UserNotFoundError: invalid user_id or no such user
            (the transport guarantees an existing user).
        InvalidLedgerEntryError: malformed user_id.
    """
    wallet_data = None if not _user_has_wallet(user_id) else {
        "available_units": wallet.wallet_units(user_id).available_units,
    }

    rows = db.list_user_tasks(user_id)
    completed_tasks = sum(
        1 for row in rows
        if row["status"] == db.USER_TASK_STATUS_COMPLETED
    )
    in_progress_tasks = sum(
        1 for row in rows
        if row["status"] == db.USER_TASK_STATUS_STARTED
    )

    earned_units = sum(
        entry.amount_units
        for entry in LedgerService().list_user_entries(user_id)
        if entry.entry_type == _EARNINGS_ENTRY_TYPE
        and entry.reference_type == _EARNINGS_REFERENCE_TYPE
    )

    return {
        "wallet": wallet_data,
        "stats": {
            "completed_tasks": completed_tasks,
            "in_progress_tasks": in_progress_tasks,
            "earned_units": earned_units,
        },
    }
