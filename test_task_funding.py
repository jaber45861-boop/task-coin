"""
Advertiser Task Funding + Commission Collection — regression suite
===================================================================

Roadmap item 4 ("Task creation and commissions"): proves that task
funding, commission collection, wallet movement and ledger entries are
ONE atomic operation and that every failure mode rolls the whole thing
back — no funded task without a wallet mutation, no wallet mutation
without ledger rows and a funding record, no partial state anywhere.

Architecture under test (derived from the repository audit):

    authenticated creation surface (wizard actor / /addtask sender)
        → task_creation.create_task_from_spec(funding_advertiser_id=…)
        → db.transaction()  → BEGIN IMMEDIATE
              INSERT task (immutable reward_units + commission_units)
              task_funding.fund_task(conn, …):
                  load snapshot from the task row
                  wallet.reserve(total)  → ledger.record_hold
                  wallet.settle_units(total) → ledger.record_settlement
                  INSERT task_funding (PRIMARY KEY task_id)
          COMMIT / any exception → ROLLBACK

Coverage (numbered by roadmap phase):

    Test 1  — successful funded creation: snapshot, exact charge,
              wallet secured once, exactly the hold+settlement pair,
              funded record — plus the accounting invariants
              (wallet delta == ledger delta; funded amount == ledger
              amount == reward + commission snapshot).
    Test 2  — commission collection uses the IMMUTABLE snapshot: the
              live setting may change afterwards and never reprices
              the funded task.
    Test 3  — insufficient funds → no task, no wallet mutation, no
              ledger entry, no funding record.
    Test 4  — forced ledger failure AFTER the wallet mutation: the
              spy proves reserve+settle really executed, the rollback
              restores everything.
    Test 5  — forced wallet failure: the ledger writer is never even
              attempted; nothing is created.
    Test 6  — forced task-INSERT failure (funding never attempted) and
              forced funding-record failure (wallet+ledger+task all
              roll back).
    Test 7  — double charging is impossible: second funding refused
              BEFORE any write (spy), replayed/concurrent wizard
              confirm charges exactly once, non-transactional call
              refused, and the PRIMARY KEY backstop's real
              IntegrityError rolls a post-mutation attempt back to
              zero committed effect.
    Test 8  — concurrent independent fundings preserve exact balances
              and ledger totals; a balance covering only one total
              funds exactly one of two racing creations.
    Test 9  — completion still pays the worker exactly once after
              funding (no double reward, advertiser untouched).
    Test 10 — authorization: foreign draft refused with zero charges;
              the advertiser identity comes from the authenticated
              actor, never from stored payload; invalid/unregistered
              advertisers rejected pre-write.
    Test 11 — transport guard: Telegram/Mini App layers contain no
              financial primitive calls (they only reach the service).
    Test 12 — source guards: the entry paths keep their
              ``with db.transaction`` boundary, the funding service
              never opens/commits a transaction, money calls carry
              ``connection=``, no float, the funding table is owned by
              its module, and funding NEVER reads the live commission
              setting.
    Test 13 — zero-cost tasks fund with record-only semantics —
              proven to be FORCED by the existing schema/wallet
              primitives (zero-amount ledger rows and zero wallet
              movements are impossible), not newly invented.
    Test 14 — creation WITHOUT an advertiser stays unfunded (the
              preserved pre-funding semantics for direct/system
              callers).
    Test 15 — deleting a funded task: state (task + funding record)
              follows the deletion, the append-only ledger pair and
              the wallet outflow PERSIST — the same pre-existing
              semantics a task with credited rewards already had.

Cancellation/refund: the repository has NO task cancellation or
expiration workflow (only the pre-existing active on/off toggle), so
there is no refund path to test here — documented as deferred.

Run:
    python3 -m pytest test_task_funding.py -v
"""

from __future__ import annotations

import ast
import glob
import os
import re
import sqlite3
import threading

import pytest

import admin_task_wizard
import db
import platform_settings as ps
import task_creation
import task_draft_store
import task_funding
import task_taxonomy
import wallet
from config import ADMINS
from ledger import LedgerService
from task_completion import (
    CompletionGate,
    CompletionGateError,
    VerificationResult,
    VerificationStatus,
)
from task_creation import TaskCreationError, TaskSpec, create_task_from_spec
from task_funding import TaskFundingError, get_funding
from task_start import TaskStartGate, StartGateError
from task_taxonomy import VERIFICATION_MANUAL

# ── Identities / amounts (exact integer atomic units) ─────────────────

ADVERTISER = 9101          # the authenticated creator who funds
OTHER_USER = 9102          # second account (authorization negatives)
WORKER = 9103              # completes the task for the reward
UNREGISTERED = 424242      # positive id, no users row

USDT = wallet.USDT_SCALE   # 100,000,000 units per USDT
SEED = 100 * USDT          # per-test starting balance (100 USDT)
REWARD = 1 * USDT          # task reward (1 USDT)
# db.init_db seeds advertiser_commission = 3000 bp (30 %):
COMMISSION = 30_000_000                       # 0.3 USDT
TOTAL = REWARD + COMMISSION                   # 130,000,000

# The admin identity platform_settings.set_setting authorizes — the
# same existing model test_task_commission uses (config bootstrap).
ADMIN = ADMINS[0]


# ── Fixture / helpers ────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated database + registered identities. No balance seeded —
    each test seeds exactly what it needs via ``_seed``."""
    db_path = str(tmp_path / "task_funding.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)  # seeds advertiser_commission = 3000 bp
    db.register_user(ADVERTISER, "adv", "Advertiser")
    db.register_user(OTHER_USER, "other", "Other")
    db.register_user(WORKER, "worker", "Worker")
    yield db_path


def _seed(units: int, user_id: int = ADVERTISER) -> None:
    """Test-side balance setup (direct credit, commits itself)."""
    wallet.credit_units(user_id, units)


def _spec(reward_units: int, title: str = "مهمة تمويل") -> TaskSpec:
    """A fully-valid manual TaskSpec with exact atomic reward units."""
    whole, _fraction = divmod(reward_units, USDT)
    return TaskSpec(
        title=title,
        description="وصف المهمة للاختبار",
        provider="instagram",
        action="follow",
        target={},
        verification=VERIFICATION_MANUAL,
        reward=whole,
        approver_id=ADVERTISER,
        reward_units=reward_units,
    )


def _create(
    reward_units: int = REWARD,
    *,
    advertiser=ADVERTISER,
    title: str = "مهمة تمويل",
) -> int:
    """Funded creation through the ONE canonical service."""
    return create_task_from_spec(
        _spec(reward_units, title=title),
        funding_advertiser_id=advertiser,
    )


def _create_unfunded(reward_units: int = REWARD, title: str = "بدون تمويل") -> int:
    """Direct service creation with no advertiser (classic semantics)."""
    return create_task_from_spec(_spec(reward_units, title=title))


def _raw(sql: str, params: tuple = ()) -> list[dict]:
    with db.get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _wallet(user_id: int = ADVERTISER) -> tuple[int, int] | None:
    rows = _raw(
        "SELECT available_units, held_units FROM wallets "
        "WHERE user_id = ?",
        (user_id,),
    )
    if not rows:
        return None
    return rows[0]["available_units"], rows[0]["held_units"]


def _ledger(user_id: int | None = None) -> list[dict]:
    if user_id is None:
        return _raw("SELECT * FROM ledger ORDER BY id")
    return _raw(
        "SELECT * FROM ledger WHERE user_id = ? ORDER BY id", (user_id,)
    )


def _funding() -> list[dict]:
    return _raw("SELECT * FROM task_funding ORDER BY task_id")


def _tasks() -> list[dict]:
    return _raw("SELECT id, title FROM tasks ORDER BY id")


def _draft_at_preview(reward_text: str = "1", owner: int = ADVERTISER) -> int:
    """A complete wizard draft at STEP_PREVIEW (mirrors the established
    test_reward_input helper): persisted validation → ready to publish."""
    draft = task_draft_store.get_or_create_open_draft(
        owner, admin_task_wizard.STEP_TITLE, {}
    )
    payload = {
        "title": "مهمة التمويل",
        "family": "social",
        "provider": "instagram",
        "target_ref": "https://example.com/target",
        "action": "follow",
        "instructions": "نفذ المهمة وأرسل إثباتاً",
        "verification": VERIFICATION_MANUAL,
        "repeat_policy": db.REPEAT_POLICY_ONE_TIME,
    }
    payload = admin_task_wizard._validate_text_step(
        admin_task_wizard.STEP_REWARD, reward_text, payload
    )
    saved = task_draft_store.save_step(
        draft.draft_id, owner, admin_task_wizard.STEP_PREVIEW, payload
    )
    assert saved is not None
    return saved.draft_id


# ════════════════════════════════════════════════════════════════════
# Test 1 — successful funding + accounting invariants (Phases 4, 16, 17)
# ════════════════════════════════════════════════════════════════════


def test_1_successful_creation_funds_atomically(env):
    _seed(SEED)

    tid = _create(REWARD)

    # Task persisted with the immutable creation-time snapshot…
    task = db.get_task(tid)
    assert task is not None
    assert task["reward_units"] == REWARD
    assert task["commission_units"] == COMMISSION
    assert bool(task["active"]) is True  # active at creation

    # …and the persisted funding record charges exactly that snapshot.
    fund = get_funding(tid)
    assert fund is not None
    assert fund["advertiser_id"] == ADVERTISER
    assert fund["reward_units"] == REWARD
    assert fund["commission_units"] == COMMISSION
    assert fund["total_units"] == REWARD + COMMISSION

    # Wallet secured exactly once: available −total, held never lingers.
    assert _wallet(ADVERTISER) == (SEED - TOTAL, 0)

    # Exactly the hold + settlement pair, tied to THIS task.
    rows = _ledger(ADVERTISER)
    assert [r["entry_type"] for r in rows] == ["hold", "settlement"]
    for row in rows:
        assert row["amount_units"] == TOTAL
        assert row["reference_type"] == "task"
        assert row["reference_id"] == f"task_funding:{tid}"
    assert rows[0]["idempotency_key"] == f"task_funding:{tid}:hold"
    assert rows[1]["idempotency_key"] == f"task_funding:{tid}:settlement"

    # ── Invariants (Phase 17) ────────────────────────────────────────
    # wallet delta == sum of the ledger deltas.
    avail_delta = sum(r["available_delta"] for r in rows)
    held_delta = sum(r["held_delta"] for r in rows)
    assert SEED - _wallet(ADVERTISER)[0] == -avail_delta
    assert held_delta == 0
    # funded amount == persisted funding amount == ledger amount.
    settlement_total = sum(
        r["amount_units"] for r in rows if r["entry_type"] == "settlement"
    )
    assert fund["total_units"] == settlement_total == TOTAL
    # commission charged == immutable commission snapshot.
    assert fund["commission_units"] == task["commission_units"]


# ════════════════════════════════════════════════════════════════════
# Test 2 — immutable commission snapshot is what funding charges (Ph. 10)
# ════════════════════════════════════════════════════════════════════


def test_2_funding_charges_snapshot_not_the_live_rate(env):
    _seed(SEED)

    # Created at the seeded 3000 bp, without funding yet.
    tid = _create_unfunded(REWARD, title="قبل التغيير")
    assert db.get_task(tid)["commission_units"] == COMMISSION

    # The admin changes the LIVE setting to 5000 bp (50 %) afterwards.
    assert (
        ps.set_setting(
            ps.ADVERTISER_COMMISSION, 5_000, admin_user_id=ADMIN
        )
        == 5_000
    )

    # Funding later charges the persisted 30 % snapshot — never
    # current_rate × historical_task.
    with db.transaction() as conn:
        result = task_funding.fund_task(
            conn, task_id=tid, advertiser_id=ADVERTISER
        )
    assert result.commission_units == COMMISSION          # 0.3, not 0.5
    assert result.total_units == TOTAL
    assert get_funding(tid)["commission_units"] == COMMISSION
    assert _wallet(ADVERTISER) == (SEED - TOTAL, 0)

    # The live setting only affects NEW tasks (still fresh, no cache).
    assert (
        ps.get_required_setting(ps.ADVERTISER_COMMISSION) == 5_000
    )
    tid2 = _create_unfunded(REWARD, title="بعد التغيير")
    assert db.get_task(tid2)["commission_units"] == REWARD // 2


def test_2b_record_invariants_are_schema_enforced(env):
    """``total_units = reward_units + commission_units`` and
    non-negative amounts are enforced by the TABLE itself — the
    invariant survives even a buggy or bypassing caller — and the
    FOREIGN KEY refuses a funding record for a non-existent task
    (PRAGMA foreign_keys=ON on every connection)."""
    _seed(SEED)
    tid = _create_unfunded(REWARD)

    # 1. total ≠ reward + commission → rejected.
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO task_funding "
                "(task_id, advertiser_id, reward_units, "
                " commission_units, total_units) "
                "VALUES (?, ?, ?, ?, ?)",
                (tid, ADVERTISER, REWARD, COMMISSION, TOTAL + 1),
            )

    # 2. negative amounts → rejected.
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO task_funding "
                "(task_id, advertiser_id, reward_units, "
                " commission_units, total_units) "
                "VALUES (?, ?, ?, ?, ?)",
                (tid, ADVERTISER, -1, COMMISSION, TOTAL - 1),
            )

    # 3. a funding record whose task does not exist → rejected.
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO task_funding "
                "(task_id, advertiser_id, reward_units, "
                " commission_units, total_units) "
                "VALUES (?, ?, ?, ?, ?)",
                (999_999, ADVERTISER, REWARD, COMMISSION, TOTAL),
            )

    # None of the rejected attempts left any committed state.
    assert _wallet(ADVERTISER) == (SEED, 0)
    assert _ledger() == []
    assert _funding() == []


# ════════════════════════════════════════════════════════════════════
# Test 3 — insufficient funds leaves zero state (Phase 9)
# ════════════════════════════════════════════════════════════════════


def test_3_insufficient_funds_zero_mutation_zero_task(env):
    _seed(TOTAL - 1)  # one unit short of the total cost

    with pytest.raises(TaskCreationError) as excinfo:
        _create(REWARD)

    # Repository-native insufficient-funds vocabulary, chained.
    assert isinstance(
        excinfo.value.__cause__, wallet.InsufficientBalanceError
    )
    # insufficient funds → task not funded → no wallet debit/reservation
    # → no ledger entry → no partial task state.
    assert _wallet(ADVERTISER) == (TOTAL - 1, 0)
    assert _ledger() == []
    assert _funding() == []
    assert _tasks() == []


# ════════════════════════════════════════════════════════════════════
# Test 4 — forced ledger failure rolls back the wallet (Phase 16, the
# non-vacuous key test: the spy proves the wallet mutation executed)
# ════════════════════════════════════════════════════════════════════


def test_4_ledger_failure_rolls_back_wallet_task_and_record(
    env, monkeypatch
):
    _seed(SEED)

    # Spy: prove BOTH wallet mutations genuinely execute inside the
    # transaction before the forced ledger failure — the rollback must
    # undo real mutations, not no-ops.
    reserved: list[tuple[int, int]] = []
    real_reserve = wallet.reserve

    def spy_reserve(user_id, amount, *, connection=None):
        out = real_reserve(user_id, amount, connection=connection)
        reserved.append((user_id, out))
        return out

    settled: list[tuple[int, int]] = []
    real_settle = wallet.settle_units

    def spy_settle(user_id, amount_units, *, connection=None):
        out = real_settle(user_id, amount_units, connection=connection)
        settled.append((user_id, out))
        return out

    monkeypatch.setattr(wallet, "reserve", spy_reserve)
    monkeypatch.setattr(wallet, "settle_units", spy_settle)

    # Force the LAST ledger write to fail (after reserve + hold +
    # settle already ran on the transaction connection).
    def boom(self, *args, **kwargs):
        raise sqlite3.OperationalError("simulated ledger failure")

    monkeypatch.setattr(LedgerService, "record_settlement", boom)

    with pytest.raises(sqlite3.OperationalError):
        _create(REWARD)

    # Non-vacuous: the wallet mutation really happened first…
    assert reserved == [(ADVERTISER, TOTAL)]
    assert settled == [(ADVERTISER, TOTAL)]
    # …and the ROLLBACK undid all of it — wallet unchanged, ledger
    # unchanged, no funded record, no task.
    assert _wallet(ADVERTISER) == (SEED, 0)
    assert _ledger() == []
    assert _funding() == []
    assert _tasks() == []


def test_4b_ledger_hold_failure_rolls_back_the_reserve(
    env, monkeypatch
):
    """The OTHER ledger injection point: record_hold fails right
    AFTER the reserve already moved units inside the transaction."""
    _seed(SEED)

    reserved: list[tuple[int, int]] = []
    real_reserve = wallet.reserve

    def spy_reserve(user_id, amount, *, connection=None):
        out = real_reserve(user_id, amount, connection=connection)
        reserved.append((user_id, out))
        return out

    monkeypatch.setattr(wallet, "reserve", spy_reserve)

    def boom(self, *args, **kwargs):
        raise sqlite3.OperationalError("simulated ledger hold failure")

    monkeypatch.setattr(LedgerService, "record_hold", boom)

    with pytest.raises(sqlite3.OperationalError):
        _create(REWARD)

    # Non-vacuous: the reserve genuinely executed first…
    assert reserved == [(ADVERTISER, TOTAL)]
    # …and the ROLLBACK left no committed state on a fresh connection.
    assert _wallet(ADVERTISER) == (SEED, 0)
    assert _ledger() == []
    assert _funding() == []
    assert _tasks() == []


# ════════════════════════════════════════════════════════════════════
# Test 5 — forced wallet failure writes no ledger entry (Phase 16)
# ════════════════════════════════════════════════════════════════════


def test_5_wallet_failure_writes_no_ledger_entry(env, monkeypatch):
    _seed(SEED)

    # Spy: the ledger writer must never even be attempted after the
    # wallet operation fails.
    attempts: list[bool] = []
    real_record = LedgerService.record_hold

    def spy_record(self, *args, **kwargs):
        attempts.append(True)
        return real_record(self, *args, **kwargs)

    monkeypatch.setattr(LedgerService, "record_hold", spy_record)

    def boom(user_id, amount, *, connection=None):
        raise wallet.WalletError("simulated wallet failure")

    monkeypatch.setattr(wallet, "reserve", boom)

    with pytest.raises(TaskCreationError) as excinfo:
        _create(REWARD)
    assert isinstance(excinfo.value.__cause__, wallet.WalletError)

    assert attempts == []
    assert _wallet(ADVERTISER) == (SEED, 0)
    assert _ledger() == []
    assert _funding() == []
    assert _tasks() == []


def test_5b_wallet_settle_failure_rolls_back_reserve_and_hold(
    env, monkeypatch
):
    """The SECOND wallet injection point: settle_units fails AFTER
    reserve + record_hold already succeeded in the transaction."""
    _seed(SEED)

    settle_attempts: list[int] = []

    def boom(user_id, amount_units, *, connection=None):
        settle_attempts.append(amount_units)
        raise wallet.WalletError("simulated settle failure")

    monkeypatch.setattr(wallet, "settle_units", boom)

    # The settlement ledger row must never even be attempted.
    recorded: list[bool] = []
    real_record = LedgerService.record_settlement

    def spy_record(self, *args, **kwargs):
        recorded.append(True)
        return real_record(self, *args, **kwargs)

    monkeypatch.setattr(LedgerService, "record_settlement", spy_record)

    with pytest.raises(TaskCreationError) as excinfo:
        _create(REWARD)
    assert isinstance(excinfo.value.__cause__, wallet.WalletError)

    assert settle_attempts == [TOTAL]  # the settle call ran and failed
    assert recorded == []              # settlement row never attempted
    # Fresh connection: reserve + hold both rolled back, no record,
    # no task.
    assert _wallet(ADVERTISER) == (SEED, 0)
    assert _ledger() == []
    assert _funding() == []
    assert _tasks() == []


# ════════════════════════════════════════════════════════════════════
# Test 6 — persistence failures (Phase 16): task INSERT failure never
# funds; funding-record failure rolls back wallet + ledger + task
# ════════════════════════════════════════════════════════════════════


def test_6a_task_persistence_failure_never_funds(env, monkeypatch):
    _seed(SEED)

    # Spy: funding must not be attempted when the task itself cannot
    # be persisted.
    fund_attempts: list[bool] = []
    real_fund = task_funding.fund_task

    def spy_fund(*args, **kwargs):
        fund_attempts.append(True)
        return real_fund(*args, **kwargs)

    monkeypatch.setattr(task_funding, "fund_task", spy_fund)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("simulated task persistence failure")

    monkeypatch.setattr(db, "create_task", boom)

    with pytest.raises(sqlite3.OperationalError):
        _create(REWARD)

    assert fund_attempts == []
    assert _wallet(ADVERTISER) == (SEED, 0)
    assert _ledger() == []
    assert _funding() == []
    assert _tasks() == []


def test_6b_funding_record_failure_rolls_back_everything(
    env, monkeypatch
):
    _seed(SEED)

    # Spy: prove the wallet settle AND the final ledger settlement
    # genuinely executed before the funding-record INSERT failed.
    settled: list[int] = []
    real_settle = wallet.settle_units

    def spy_settle(user_id, amount_units, *, connection=None):
        out = real_settle(user_id, amount_units, connection=connection)
        settled.append(out)
        return out

    monkeypatch.setattr(wallet, "settle_units", spy_settle)

    recorded: list[int] = []
    real_record = LedgerService.record_settlement

    def spy_record(self, *args, **kwargs):
        entry = real_record(self, *args, **kwargs)
        recorded.append(entry.amount_units)
        return entry

    monkeypatch.setattr(LedgerService, "record_settlement", spy_record)

    def boom(*args, **kwargs):
        raise sqlite3.IntegrityError("simulated funding-record failure")

    monkeypatch.setattr(task_funding, "_insert_record", boom)

    with pytest.raises(sqlite3.IntegrityError):
        _create(REWARD)

    # Non-vacuous: the money movement and the ledger row both ran…
    assert settled == [TOTAL]
    assert recorded == [TOTAL]
    # …and the ROLLBACK restored the wallet, dropped the ledger rows,
    # dropped the funding record AND removed the freshly inserted task.
    assert _wallet(ADVERTISER) == (SEED, 0)
    assert _ledger() == []
    assert _funding() == []
    assert _tasks() == []


# ════════════════════════════════════════════════════════════════════
# Test 7 — duplicate charge / double creation is impossible (Phase 8)
# ════════════════════════════════════════════════════════════════════


def test_7a_second_funding_is_refused_before_any_mutation(
    env, monkeypatch
):
    _seed(SEED)
    tid = _create(REWARD)

    # Spy: the second attempt must be refused BEFORE any wallet write
    # (not "write then rely on rollback").
    attempts: list[bool] = []
    real_reserve = wallet.reserve

    def spy_reserve(*args, **kwargs):
        attempts.append(True)
        return real_reserve(*args, **kwargs)

    monkeypatch.setattr(wallet, "reserve", spy_reserve)

    # The refusal propagates OUT of db.transaction() → the attempt's
    # transaction is really ROLLED BACK; the assertions below then
    # read committed state on FRESH connections only (db.get_connection
    # opens a new sqlite3 connection per call).
    with pytest.raises(TaskFundingError, match="already funded"):
        with db.transaction() as conn:
            task_funding.fund_task(
                conn, task_id=tid, advertiser_id=ADVERTISER
            )

    assert attempts == []  # refused before any wallet mutation
    # No committed effect beyond the ORIGINAL funding.
    assert _wallet(ADVERTISER) == (SEED - TOTAL, 0)
    assert len(_ledger(ADVERTISER)) == 2
    assert len(_funding()) == 1
    assert len(_tasks()) == 1


def test_7a2_primary_key_backstop_rolls_back_real_mutations(env):
    """The PK backstop's REAL IntegrityError (not a simulated one)
    rolls back a wallet mutation that already ran in the same
    transaction — no committed wallet/ledger/funding effect remains.

    Through the public API the earlier SELECT check fires first (BEGIN
    IMMEDIATE serializes writers, so the second tx always SEES the
    committed row); the PRIMARY KEY exists as the database-level
    backstop for any path that slips past application checks.
    """
    _seed(SEED)
    tid = _create(REWARD)

    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            # A REAL wallet mutation on the transaction connection…
            wallet.reserve(
                ADVERTISER,
                wallet.units_to_decimal(REWARD),
                connection=conn,
            )
            # …then the duplicate funding row → PRIMARY KEY violation.
            conn.execute(
                "INSERT INTO task_funding "
                "(task_id, advertiser_id, reward_units, "
                " commission_units, total_units) "
                "VALUES (?, ?, ?, ?, ?)",
                (tid, ADVERTISER, REWARD, COMMISSION, TOTAL),
            )

    # Fresh connection: only the ORIGINAL funding's committed state.
    assert _wallet(ADVERTISER) == (SEED - TOTAL, 0)
    assert len(_ledger(ADVERTISER)) == 2
    assert len(_funding()) == 1
    assert len(_tasks()) == 1


def test_7b_funding_outside_a_transaction_is_refused(env):
    _seed(SEED)
    tid = _create(REWARD)

    with db.get_connection() as conn:
        with pytest.raises(TaskFundingError, match="db.transaction"):
            task_funding.fund_task(
                conn, task_id=tid, advertiser_id=ADVERTISER
            )

    assert _wallet(ADVERTISER) == (SEED - TOTAL, 0)
    assert len(_ledger(ADVERTISER)) == 2
    assert len(_funding()) == 1


def test_7c_replayed_confirm_never_charges_again(env):
    _seed(SEED)
    draft_id = _draft_at_preview()

    first = admin_task_wizard.publish_draft(draft_id, ADVERTISER)
    assert _wallet(ADVERTISER) == (SEED - TOTAL, 0)

    # Telegram redelivery: the SAME logical creation resolves to the
    # same task with NO second charge.
    second = admin_task_wizard.publish_draft(draft_id, ADVERTISER)
    assert second == first
    assert _wallet(ADVERTISER) == (SEED - TOTAL, 0)
    assert len(_tasks()) == 1
    assert len(_funding()) == 1
    assert len(_ledger(ADVERTISER)) == 2  # one hold+settlement pair


def test_7d_concurrent_confirms_charge_exactly_once(env):
    # Balance covers EXACTLY one total cost.
    _seed(TOTAL)
    draft_id = _draft_at_preview()

    results: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def contender() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(
                admin_task_wizard.publish_draft(draft_id, ADVERTISER)
            )
        except BaseException as exc:  # noqa: BLE001 — recorded
            errors.append(exc)

    threads = [
        threading.Thread(target=contender) for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    # Both resolves → one task, charged EXACTLY once.
    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    assert len(_tasks()) == 1
    assert len(_funding()) == 1
    assert _wallet(ADVERTISER) == (0, 0)
    assert len(_ledger(ADVERTISER)) == 2


# ════════════════════════════════════════════════════════════════════
# Test 8 — concurrency: exact balances and ledger totals (Phase 16)
# ════════════════════════════════════════════════════════════════════


def _run_concurrently(tasks: list) -> list[BaseException]:
    """Run callables on threads behind one barrier; return failures."""
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(tasks))

    def runner(fn) -> None:
        try:
            barrier.wait(timeout=10)
            fn()
        except BaseException as exc:  # noqa: BLE001 — recorded
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(fn,)) for fn in tasks]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    return errors


def test_8a_concurrent_independent_fundings_preserve_totals(env):
    start = TOTAL * 2
    _seed(start)

    errors = _run_concurrently(
        [
            lambda: _create(REWARD, title="سباق 1"),
            lambda: _create(REWARD, title="سباق 2"),
        ]
    )
    assert errors == []

    # Exact arithmetic: both charged once, held never left behind.
    assert _wallet(ADVERTISER) == (0, 0)
    assert len(_tasks()) == 2
    assert len(_funding()) == 2
    rows = _ledger(ADVERTISER)
    assert len(rows) == 4  # two hold+settlement pairs
    assert [r["entry_type"] for r in rows] == [
        "hold", "settlement", "hold", "settlement",
    ]
    # One distinct funding reference per task.
    assert len({r["reference_id"] for r in rows}) == 2
    # wallet delta == ledger delta for the pair-of-pairs.
    assert start - _wallet(ADVERTISER)[0] == -sum(
        r["available_delta"] for r in rows
    )


def test_8b_balance_covering_one_total_funds_exactly_one(env):
    _seed(TOTAL)

    errors = _run_concurrently(
        [
            lambda: _create(REWARD, title="ناموس 1"),
            lambda: _create(REWARD, title="ناموس 2"),
        ]
    )
    # Exactly one succeeded; the loser failed on funds with zero
    # partial state (no half-created task, no second charge).
    assert len(errors) == 1
    assert isinstance(errors[0], TaskCreationError)
    assert isinstance(errors[0].__cause__, wallet.InsufficientBalanceError)

    assert _wallet(ADVERTISER) == (0, 0)
    assert len(_tasks()) == 1
    assert len(_funding()) == 1
    assert len(_ledger(ADVERTISER)) == 2


# ════════════════════════════════════════════════════════════════════
# Test 9 — completion still pays exactly once after funding (Phase 11)
# ════════════════════════════════════════════════════════════════════


def test_9_completion_pays_worker_once_and_only_funding_charged(env):
    _seed(SEED)
    tid = _create(REWARD)
    advertiser_after_funding = _wallet(ADVERTISER)
    assert advertiser_after_funding == (SEED - TOTAL, 0)

    # Worker starts and completes through the existing production gate.
    result = TaskStartGate().start(WORKER, tid)
    assert getattr(result, "success", True)
    gate = CompletionGate()
    passed = VerificationResult(status=VerificationStatus.PASSED)
    assert gate.complete(WORKER, tid, passed) is True

    # Worker credited exactly the reward — paid independently, exactly
    # once, exactly as before funding existed.
    assert _wallet(WORKER) == (REWARD, 0)
    # The completion neither touched nor re-charged the advertiser.
    assert _wallet(ADVERTISER) == advertiser_after_funding

    # Ledger: the funding pair for the advertiser + ONE reward credit.
    credit_rows = [
        r for r in _ledger(WORKER) if r["entry_type"] == "credit"
    ]
    assert len(credit_rows) == 1
    assert credit_rows[0]["amount_units"] == REWARD
    assert credit_rows[0]["reference_type"] == "task"
    assert len(_ledger(ADVERTISER)) == 2

    # A second completion attempt can never double-pay.
    with pytest.raises(CompletionGateError):
        gate.complete(WORKER, tid, passed)
    assert _wallet(WORKER) == (REWARD, 0)
    assert len(
        [r for r in _ledger(WORKER) if r["entry_type"] == "credit"]
    ) == 1


# ════════════════════════════════════════════════════════════════════
# Test 10 — authorization (Phase 15/16)
# ════════════════════════════════════════════════════════════════════


def test_10a_foreign_draft_publish_refused_and_charges_nobody(env):
    _seed(SEED, ADVERTISER)
    _seed(SEED, OTHER_USER)
    draft_id = _draft_at_preview(owner=ADVERTISER)

    # Another authenticated account cannot publish (and therefore
    # cannot fund from) someone else's draft.
    with pytest.raises(admin_task_wizard.DraftAccessError):
        admin_task_wizard.publish_draft(draft_id, OTHER_USER)

    # Zero mutation for either party.
    assert _wallet(ADVERTISER) == (SEED, 0)
    assert _wallet(OTHER_USER) == (SEED, 0)
    assert _ledger() == []
    assert _funding() == []
    assert _tasks() == []


def test_10b_advertiser_identity_never_comes_from_payload(env):
    _seed(SEED, ADVERTISER)
    _seed(SEED, OTHER_USER)
    draft_id = _draft_at_preview()

    # An attacker plants an advertiser identity into the stored
    # payload — publish must charge the AUTHENTICATED actor only.
    draft = task_draft_store.get_draft(draft_id)
    payload = dict(draft.payload)
    payload["funding_advertiser_id"] = OTHER_USER
    payload["advertiser_id"] = OTHER_USER
    saved = task_draft_store.save_step(
        draft_id, ADVERTISER, admin_task_wizard.STEP_PREVIEW, payload
    )
    assert saved is not None

    tid = admin_task_wizard.publish_draft(draft_id, ADVERTISER)

    assert get_funding(tid)["advertiser_id"] == ADVERTISER
    assert _wallet(ADVERTISER) == (SEED - TOTAL, 0)
    assert _wallet(OTHER_USER) == (SEED, 0)
    charged = [
        r for r in _ledger() if r["user_id"] == OTHER_USER
    ]
    assert charged == []


def test_10c_invalid_or_unregistered_advertiser_rejected_pre_write(
    env,
):
    _seed(SEED)

    for bad in (True, 0, -1, "9101"):
        with pytest.raises(TaskCreationError):
            create_task_from_spec(
                _spec(REWARD),
                funding_advertiser_id=bad,  # type: ignore[arg-type]
            )

    # A well-formed but unregistered account cannot fund either.
    with pytest.raises(TaskCreationError) as excinfo:
        _create(REWARD, advertiser=UNREGISTERED)
    assert isinstance(
        excinfo.value.__cause__, wallet.UserNotFoundError
    )

    # Nothing was written for any attempt.
    assert _tasks() == []
    assert _funding() == []
    assert _ledger() == []
    assert _wallet(ADVERTISER) == (SEED, 0)


# ════════════════════════════════════════════════════════════════════
# Test 11 — transport guard (Phase 16): Telegram/Mini App layers are
# transport-only; they reach money solely through the creation service
# ════════════════════════════════════════════════════════════════════

_TRANSPORTS = ("bot.py", "admin_task_wizard.py", "task_routes.py")
_MONEY_CALLS = {"credit_units", "reserve", "release_units", "settle_units"}
_LEDGER_NAMES = {
    "LedgerService",
    "record_credit",
    "record_debit",
    "record_hold",
    "record_release",
    "record_settlement",
    "fund_task",
}


def _source(name: str) -> str:
    root = os.path.dirname(os.path.abspath(__file__))
    return open(os.path.join(root, name), encoding="utf-8").read()


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def test_11_transports_never_touch_financial_primitives():
    violations: list[str] = []
    for name in _TRANSPORTS:
        tree = ast.parse(_source(name))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                call = _call_name(node)
                if call in _MONEY_CALLS:
                    violations.append(
                        f"{name}:{node.lineno} calls {call}()"
                    )
                if call == "fund_task":
                    violations.append(
                        f"{name}:{node.lineno} bypasses the creation "
                        "service with a direct fund_task() call"
                    )
            elif isinstance(node, ast.Attribute):
                if node.attr in _LEDGER_NAMES:
                    violations.append(
                        f"{name}:{node.lineno} references "
                        f"{node.attr}"
                    )
            elif isinstance(node, ast.Name):
                if node.id in _LEDGER_NAMES:
                    violations.append(
                        f"{name}:{node.lineno} references {node.id}"
                    )
    assert violations == [], (
        "transport layers performing direct accounting:\n"
        + "\n".join(violations)
    )


# ════════════════════════════════════════════════════════════════════
# Test 12 — source-level regression guards (Phase 18)
# ════════════════════════════════════════════════════════════════════


def _executable_strings(tree: ast.AST) -> list[str]:
    """All string constants EXCEPT docstrings (the repo's established
    source-protection approach — see test_wallet_ledger_atomicity's
    ``_executable_strings``)."""
    docstrings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(body[0].value.value)
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value not in docstrings
    ]


def test_12a_financial_entry_paths_own_their_transaction():
    """The creation service and the wizard publish keep their
    ``with db.transaction`` atomic boundary — never delegated."""
    for name in ("task_creation.py", "admin_task_wizard.py"):
        assert "with db.transaction" in _source(name), (
            f"{name} no longer wraps its money flow in db.transaction()"
        )


def test_12b_funding_service_never_begins_or_commits_a_transaction():
    """``task_funding`` only borrows the caller's connection: no
    BEGIN/COMMIT/ROLLBACK of its own, ever."""
    tree = ast.parse(_source("task_funding.py"))
    for text in _executable_strings(tree):
        assert "BEGIN IMMEDIATE" not in text
        assert ".commit(" not in text
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call = _call_name(node)
        if call in ("commit", "rollback"):
            raise AssertionError(
                f"task_funding.py:{node.lineno} owns a transaction "
                "boundary it must only borrow"
            )
        if call == "transaction":
            raise AssertionError(
                f"task_funding.py:{node.lineno} opens its own "
                "db.transaction() — the caller must own the atomicity"
            )


def test_12c_money_calls_carry_connection():
    """Every wallet mutation in the funding path runs on the caller's
    transaction connection (mirrors guard test_8a, scoped here so this
    suite alone protects the new module)."""
    violations: list[str] = []
    for name in ("task_funding.py", "task_creation.py"):
        tree = ast.parse(_source(name))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in _MONEY_CALLS:
                keywords = {kw.arg for kw in node.keywords}
                if "connection" not in keywords:
                    violations.append(f"{name}:{node.lineno}")
    assert violations == [], violations


def test_12d_no_float_in_financial_code():
    """Integer-only money math in the new funding code."""
    for name in ("task_funding.py", "task_creation.py"):
        tree = ast.parse(_source(name))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(
                node.value, float
            ):
                raise AssertionError(
                    f"float literal in {name}:{node.lineno}"
                )
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Name
            ) and node.func.id in ("float", "round"):
                raise AssertionError(
                    f"{node.func.id}() in {name}:{node.lineno}"
                )


def test_12f_funding_never_reads_the_live_commission_setting():
    """Static proof of Phase 10: the funding movement only consumes
    the persisted ``tasks.commission_units`` — the module cannot even
    IMPORT the settings service, let alone read the live rate."""
    tree = ast.parse(_source("task_funding.py"))
    imported: set[str | None] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert "platform_settings" not in imported
    for text in _executable_strings(tree):
        assert "get_required_setting" not in text
        assert "advertiser_commission" not in text


def test_12e_funding_table_is_owned_by_its_module():
    """``INSERT INTO task_funding`` lives only in task_funding.py —
    same ownership convention as ledger/wallet SQL (guard test_8b)."""
    pattern = re.compile(r"INSERT\s+INTO\s+task_funding\b", re.I)
    violations: list[str] = []
    root = os.path.dirname(os.path.abspath(__file__))
    for path in sorted(glob.glob(os.path.join(root, "*.py"))):
        base = os.path.basename(path)
        if base.startswith("test_") or base == "conftest.py":
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        for text in _executable_strings(tree):
            if pattern.search(text) and base != "task_funding.py":
                violations.append(base)
    assert violations == [], violations


# ════════════════════════════════════════════════════════════════════
# Tests 13–14 — preserved semantics (Phases 2, 9)
# ════════════════════════════════════════════════════════════════════


def test_13_zero_cost_task_funds_with_zero_movement(env):
    _seed(SEED)

    tid = _create(0, title="مهمة بلا تكلفة")

    fund = get_funding(tid)
    assert fund is not None
    assert fund["total_units"] == 0
    assert fund["reward_units"] == 0
    assert fund["commission_units"] == 0
    # Nothing to secure → no wallet movement, no ledger rows.
    assert _wallet(ADVERTISER) == (SEED, 0)
    assert _ledger() == []


def test_13b_zero_cost_record_only_is_forced_by_existing_semantics(
    env,
):
    """Zero-cost funding creates a record and NOTHING else — not a
    design choice but the ONLY schema-legal behavior:

    * the ledger schema CHECKs ``amount_units > 0`` — a zero
      hold/settlement row is impossible;
    * the wallet primitives reject amounts ≤ 0 before any UPDATE.

    Exactly the established ``task_reward`` zero-reward precedent
    (zero → no wallet/ledger rows), so no new semantics were invented.
    """
    _seed(SEED)

    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO ledger "
                "(user_id, entry_type, amount_units, available_delta, "
                " held_delta, reference_type, reference_id) "
                "VALUES (?, 'hold', 0, 0, 0, 'task', 'zero-probe')",
                (ADVERTISER,),
            )

    with pytest.raises(wallet.InvalidWalletAmountError):
        wallet.reserve(ADVERTISER, 0)
    with pytest.raises(wallet.InvalidWalletAmountError):
        wallet.settle_units(ADVERTISER, 0)

    # And the funded zero-cost task really is record-only.
    tid = _create(0, title="صفر")
    assert get_funding(tid)["total_units"] == 0
    assert _wallet(ADVERTISER) == (SEED, 0)
    assert _ledger() == []


def test_14_direct_creation_without_advertiser_stays_unfunded(env):
    """Preserved pre-funding semantics: the service with no advertiser
    (system/direct callers) creates without charging anyone."""
    _seed(SEED)

    tid = _create_unfunded(REWARD)

    assert get_funding(tid) is None
    assert _wallet(ADVERTISER) == (SEED, 0)
    assert _ledger() == []
    assert db.get_task(tid) is not None


# ════════════════════════════════════════════════════════════════════
# Test 15 — funded-task deletion semantics (review point 3)
# ════════════════════════════════════════════════════════════════════


def test_15_deleting_a_funded_task_keeps_ledger_and_wallet_history(
    env,
):
    """``db.delete_task`` on a FUNDED task (ON DELETE CASCADE):

    * the STATE rows follow the task — ``tasks`` row and
      ``task_funding`` record both disappear, so no state ever claims
      funding for a task that no longer exists;
    * the append-only LEDGER pair and the wallet outflow PERSIST on
      fresh connections — money already collected is never silently
      forgotten, and history can never be deleted (guards forbid
      ``DELETE FROM ledger``).

    This is the PRE-EXISTING accounting semantics: deleting a task
    whose completions already credited workers likewise leaves its
    ``task_reward:*`` ledger rows behind — funding behaves exactly the
    same way.  ``db.delete_task`` has no production caller today; a
    refund-on-delete product workflow remains deferred (a future
    refund must reuse this same wallet+ledger+state atomicity).
    """
    _seed(SEED)
    tid = _create(REWARD)

    assert db.delete_task(tid) is True

    # State follows the task (CASCADE), on a fresh connection:
    assert db.get_task(tid) is None
    assert _tasks() == []
    assert get_funding(tid) is None
    assert _funding() == []

    # Ledger + wallet effects persist exactly as committed:
    rows = _ledger(ADVERTISER)
    assert len(rows) == 2
    assert [r["entry_type"] for r in rows] == ["hold", "settlement"]
    assert {r["reference_id"] for r in rows} == {
        f"task_funding:{tid}"
    }
    assert _wallet(ADVERTISER) == (SEED - TOTAL, 0)

    # No orphan STATE anywhere: nothing readable claims the deleted
    # task is funded; the ledger reference is a dead (but permanent)
    # history pointer — same as task_reward references to deleted
    # tasks.
    with db.get_connection() as conn:
        orphans = conn.execute(
            "SELECT COUNT(*) AS c FROM task_funding "
            "WHERE task_id NOT IN (SELECT id FROM tasks)"
        ).fetchone()["c"]
    assert orphans == 0
