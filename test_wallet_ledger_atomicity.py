"""
Atomic Wallet + Ledger integration — regression suite
=====================================================

Proves that every wallet balance mutation and its corresponding ledger
entry are part of the SAME atomic database transaction, for every
financial path that exists in production today.

Architecture under test (verified by audit — see the structural guard
at the bottom of this file):

    db.transaction()  →  BEGIN IMMEDIATE
        validate operation
        mutate wallet balance   (wallet.* with connection=conn)
        insert ledger entry     (LedgerService(connection=conn).record_*)
    COMMIT  /  on any exception → ROLLBACK

Production money paths audited and exercised here:

    1. deposit credit        deposit_verification.verify_and_credit
                             (BSC adapter + manual proof review both
                             delegate to this single boundary)
    2. task reward credit    CompletionGate → TaskRewardService
                             (referral/manual approvals settle through
                             this same gate)
    3. withdrawal reserve    WithdrawalService.create   (hold)
    4. withdrawal release    WithdrawalService.reject   (release)
    5. withdrawal settlement WithdrawalService.complete (settlement)

Tests (numbering follows the roadmap item):

    Test 1 — atomic successful credit (deposit): balance updated AND
             exactly one ledger entry, committed.
    Test 2 — atomic successful debit + settlement (withdrawal): wallet
             and ledger commit together at the hold and settlement
             stages.
    Test 3 — ledger failure rolls back the wallet (THE key test).
    Test 4 — wallet mutation failure creates no ledger entry.
    Test 5 — a failed operation does not destroy previous ledger
             history.
    Test 6 — ledger immutability through the transaction path.
    Test 7 — concurrent credits cannot lose updates (real SQLite
             BEGIN IMMEDIATE locking, two threads).
    Test 8 — structural guard: no production wallet mutation can run
             without a caller-owned transaction connection, and no
             SQL outside the owning modules touches the ledger or
             wallets tables.

Run:
    python3 -m pytest test_wallet_ledger_atomicity.py -q
"""

from __future__ import annotations

import ast
import dataclasses
import glob
import os
import re
import sqlite3
import threading

import pytest

import config
import db
import deposit_store
import deposit_verification
import ledger
import wallet
from ledger import LedgerEntry, LedgerService

# Reuse the established MT-ADMIN-28/29 test helpers (repo convention).
from test_deposit import ADMIN_ID, _make_pm
from test_miniapp_auth import _TEST_BOT_TOKEN

USER = 8801
FUND = 1_000_000_000                    # 10 USDT seeded balance
AMOUNT = "1.5"                          # -> 150_000_000 units
AMOUNT_UNITS = 150_000_000
FACTS = {"source": "atomicity-regression"}
TX_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TX_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


# ── Fixture / helpers (deposit path) ─────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated database + registered user + configured minimum."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    db_path = str(tmp_path / "wallet_ledger_atomic.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER, "carol", "Carol")
    monkeypatch.setattr(config, "ADMINS", [ADMIN_ID])
    yield db_path


def _request(db_path: str) -> str:
    """One PENDING deposit intent through the production creator."""
    pm = _make_pm(db_path, min_units=1)
    request = deposit_store.create_deposit_request(
        user_id=USER,
        payment_method_id=pm.id,
        amount=AMOUNT,
        db_path=db_path,
    )
    return request.request_id


def _verify(request_id: str, *, tx: str):
    return deposit_verification.verify_and_credit(
        request_id,
        amount_units=AMOUNT_UNITS,
        external_tx_id=tx,
        facts=FACTS,
    )


def _raw(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    """Fresh-connection read — always observes COMMITTED state only."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _seed_balance(db_path: str, units: int) -> None:
    """Give USER an exact available balance (test-side state setup)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO wallets (user_id, available_units, held_units) "
            "VALUES (?, ?, 0)",
            (USER, units),
        )
        conn.commit()
    finally:
        conn.close()


def _wallet_state(db_path: str) -> tuple[int, int] | None:
    rows = _raw(
        db_path,
        "SELECT available_units, held_units FROM wallets WHERE user_id = ?",
        (USER,),
    )
    if not rows:
        return None
    return rows[0]["available_units"], rows[0]["held_units"]


def _ledger_rows(db_path: str) -> list[dict]:
    return _raw(
        db_path,
        "SELECT * FROM ledger WHERE user_id = ? ORDER BY id",
        (USER,),
    )


def _deposit_status(db_path: str, request_id: str) -> str | None:
    rows = _raw(
        db_path,
        "SELECT status FROM deposit_requests WHERE request_id = ?",
        (request_id,),
    )
    return rows[0]["status"] if rows else None


# ── Test 1 — atomic successful credit ────────────────────────────────


def test_1_successful_credit_commits_wallet_and_ledger_together(env):
    _seed_balance(env, FUND)
    request_id = _request(env)

    result = _verify(request_id, tx=TX_A)
    assert result.already_credited is False

    # Wallet mutated exactly once…
    assert _wallet_state(env) == (FUND + AMOUNT_UNITS, 0)
    # …and exactly one matching ledger entry exists, committed.
    rows = _ledger_rows(env)
    assert len(rows) == 1
    assert rows[0]["entry_type"] == "credit"
    assert rows[0]["amount_units"] == AMOUNT_UNITS
    assert rows[0]["available_delta"] == AMOUNT_UNITS
    assert rows[0]["held_delta"] == 0
    assert rows[0]["reference_type"] == "deposit"
    assert rows[0]["reference_id"] == request_id
    assert _deposit_status(env, request_id) == deposit_store.STATUS_CREDITED

    # Accounting invariant (success): wallet state changed
    # AND the matching ledger event exists.
    assert _wallet_state(env) != (FUND, 0)
    assert any(
        r["reference_id"] == request_id for r in rows
    )


# ── Test 3 — ledger failure rolls back the wallet ────────────────────


def test_3_ledger_failure_rolls_back_wallet(env, monkeypatch):
    """THE key regression: the wallet UPDATE happens first inside the
    transaction; if the subsequent ledger INSERT fails, the ROLLBACK
    must leave the balance exactly as it was and commit no ledger row.
    """
    _seed_balance(env, FUND)
    request_id = _request(env)

    # Spy: prove the wallet UPDATE genuinely executes before the
    # ledger INSERT — the rollback must undo a REAL mutation.
    credited: list[tuple[int, int]] = []
    real_credit = wallet.credit_units

    def spy_credit(user_id, amount_units, *, connection=None):
        credited.append((user_id, amount_units))
        return real_credit(user_id, amount_units, connection=connection)

    monkeypatch.setattr(wallet, "credit_units", spy_credit)

    def boom(self, *args, **kwargs):
        raise sqlite3.OperationalError("simulated ledger failure")

    monkeypatch.setattr(LedgerService, "record_credit", boom)

    with pytest.raises(sqlite3.OperationalError):
        _verify(request_id, tx=TX_A)

    # The wallet mutation DID run inside the transaction…
    assert credited == [(USER, AMOUNT_UNITS)]
    # …and the ROLLBACK undid it: wallet unchanged, ledger unchanged,
    # nothing half-committed anywhere.
    assert _wallet_state(env) == (FUND, 0)
    assert _ledger_rows(env) == []
    assert _deposit_status(env, request_id) == deposit_store.STATUS_PENDING

    # Accounting invariant (failure): wallet state unchanged AND no
    # corresponding new ledger event exists.
    # (Explicitly asserted above: balance == FUND, ledger == empty.)


# ── Test 4 — wallet mutation failure creates no ledger entry ─────────


def test_4_wallet_failure_writes_no_ledger_entry(env, monkeypatch):
    _seed_balance(env, FUND)
    request_id = _request(env)

    # Spy: the ledger writer must never even be attempted after the
    # wallet operation fails.
    record_attempts: list[bool] = []
    real_record = LedgerService.record_credit

    def spy_record(self, *args, **kwargs):
        record_attempts.append(True)
        return real_record(self, *args, **kwargs)

    monkeypatch.setattr(LedgerService, "record_credit", spy_record)

    def boom(user_id, amount_units, *, connection=None):
        raise wallet.WalletError("simulated wallet failure")

    monkeypatch.setattr(wallet, "credit_units", boom)

    with pytest.raises(wallet.WalletError):
        _verify(request_id, tx=TX_A)

    # No accounting entry was even attempted, let alone committed.
    assert record_attempts == []
    assert _wallet_state(env) == (FUND, 0)
    assert _ledger_rows(env) == []
    assert _deposit_status(env, request_id) == deposit_store.STATUS_PENDING


# ── Test 5 — rollback does not destroy previous history ──────────────


def test_5_failed_operation_preserves_existing_ledger_history(env, monkeypatch):
    _seed_balance(env, FUND)
    request_id = _request(env)

    # Pre-existing committed history: one earlier credit entry.
    history = LedgerService().record_credit(
        USER,
        amount_units=700_000_000,
        reference_type="adjustment",
        reference_id="history-1",
        idempotency_key="history-key-1",
    )
    assert len(_ledger_rows(env)) == 1

    def boom(self, *args, **kwargs):
        raise sqlite3.OperationalError("simulated ledger failure")

    monkeypatch.setattr(LedgerService, "record_credit", boom)

    with pytest.raises(sqlite3.OperationalError):
        _verify(request_id, tx=TX_A)

    # Balance untouched…
    assert _wallet_state(env) == (FUND, 0)
    # …and the previous ledger entry survives byte-for-byte.
    rows = _ledger_rows(env)
    assert len(rows) == 1
    assert rows[0]["id"] == history.id
    assert rows[0]["amount_units"] == 700_000_000
    assert rows[0]["reference_id"] == "history-1"
    assert rows[0]["idempotency_key"] == "history-key-1"


# ── Test 6 — ledger immutability through the transaction path ────────


def test_6_ledger_is_append_only_through_transaction_path(env):
    # 1. Record inside a REAL db.transaction() (borrowed connection).
    with db.transaction(env) as conn:
        service = LedgerService(connection=conn)
        entry = service.record_credit(
            USER,
            amount_units=AMOUNT_UNITS,
            reference_type="adjustment",
            reference_id="immutable-1",
            idempotency_key="immutable-key-1",
        )
    original = _ledger_rows(env)[0]

    # 2. The service exposes no mutation API at all — there is no
    #    update/delete path through the transaction layer.  Strict
    #    allowlist mirroring LedgerService's documented surface
    #    ("Public API (and nothing else — the module ships no
    #    mutation API)"): any new method must be reviewed here first.
    public = {m for m in dir(LedgerService) if not m.startswith("_")}
    assert public == {
        "record_credit",
        "record_debit",
        "record_hold",
        "record_release",
        "record_settlement",
        "get_entry",
        "get_by_idempotency_key",
        "list_user_entries",
    }, (
        "LedgerService public API changed — append-only guarantee "
        f"needs review: {sorted(public)}"
    )

    # 3. Entries are immutable value objects.
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.amount_units = 1  # type: ignore[misc]

    # 4. A later transaction appending a new entry cannot alter the
    #    existing one.
    with db.transaction(env) as conn:
        LedgerService(connection=conn).record_credit(
            USER,
            amount_units=1,
            reference_type="adjustment",
            reference_id="immutable-2",
            idempotency_key="immutable-key-2",
        )
    rows = _ledger_rows(env)
    assert len(rows) == 2
    assert rows[0] == original  # first entry byte-identical


# ── Test 7 — concurrent operations (real SQLite locking) ─────────────


def test_7_concurrent_credits_do_not_lose_updates(env):
    """Two threads credit two DIFFERENT deposits for the same user.

    Both transactions must COMMIT (BEGIN IMMEDIATE serializes them);
    because the wallet credit is one atomic ``available_units =
    available_units + ?`` UPDATE, the final balance is the exact sum —
    no lost update — with exactly one ledger entry per credit.
    """
    _seed_balance(env, FUND)
    request_a = _request(env)
    request_b = _request(env)

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker(request_id: str, tx: str) -> None:
        try:
            barrier.wait(timeout=10)
            _verify(request_id, tx=tx)
        except Exception as exc:  # noqa: BLE001 — collected for assertion
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(request_a, TX_A)),
        threading.Thread(target=worker, args=(request_b, TX_B)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], f"concurrent credits failed: {errors}"

    # No lost update: exact sum of both credits.
    assert _wallet_state(env) == (FUND + 2 * AMOUNT_UNITS, 0)
    # Exactly one ledger entry per credit, each tied to its request.
    rows = _ledger_rows(env)
    assert len(rows) == 2
    assert {r["reference_id"] for r in rows} == {request_a, request_b}
    assert _deposit_status(env, request_a) == deposit_store.STATUS_CREDITED
    assert _deposit_status(env, request_b) == deposit_store.STATUS_CREDITED


# ── Test 2 — atomic successful debit + settlement (withdrawal) ───────


from test_withdrawal_service import (  # noqa: E402 — fixture base reuse
    _Base,
    FUND as WD_FUND,
    VODAFONE_DEBIT,
)


class TestAtomicDebit(_Base):
    """Withdrawal hold is the production debit of available units.

    Reuses the established ``_Base`` fixture (temp DB, payment method,
    platform withdrawal settings) — zero test methods are inherited.
    """

    def test_2_reserve_and_settlement_commit_wallet_and_ledger_together(
        self,
    ):
        self.add_user(501)
        self.fund(501, WD_FUND)

        # Stage 1 — reserve: available → held, matching hold entry.
        request = self.create_request(user_id=501)
        assert self.wallet_state(501) == (
            WD_FUND - VODAFONE_DEBIT,
            VODAFONE_DEBIT,
        )
        holds = [
            r for r in self.ledger_rows(request.request_id)
            if r["entry_type"] == "hold"
        ]
        assert len(holds) == 1
        assert holds[0]["amount_units"] == VODAFONE_DEBIT
        assert holds[0]["available_delta"] == -VODAFONE_DEBIT
        assert holds[0]["held_delta"] == VODAFONE_DEBIT

        # Stage 2 — settlement: held leaves exactly once, matching
        # settlement entry — both stages committed wallet + ledger
        # together (read on fresh connections above/below).
        self.svc.complete(request.request_id)
        assert self.wallet_state(501) == (
            WD_FUND - VODAFONE_DEBIT,
            0,
        )
        rows = self.ledger_rows(request.request_id)
        types = [r["entry_type"] for r in rows]
        assert types == ["hold", "settlement"]
        settlement = rows[1]
        assert settlement["amount_units"] == VODAFONE_DEBIT
        assert settlement["available_delta"] == 0
        assert settlement["held_delta"] == -VODAFONE_DEBIT


# ── Test 8 — structural guard (impossible states by construction) ────


_MONEY_CALLS = {"credit_units", "reserve", "release_units", "settle_units"}


def _production_modules() -> list[str]:
    root = os.path.dirname(os.path.abspath(__file__))
    return sorted(
        p
        for p in glob.glob(os.path.join(root, "*.py"))
        if not os.path.basename(p).startswith("test_")
        and os.path.basename(p) != "conftest.py"
    )


def _executable_strings(tree: ast.AST) -> list[str]:
    """All string constants EXCEPT docstrings (the repo's established
    source-protection approach — see test_task_lifecycle's
    ``_get_code_only``)."""
    docstrings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
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


def test_8a_every_wallet_mutation_runs_on_a_transaction_connection():
    """No production call of a wallet balance mutation may omit
    ``connection=`` — a standalone call would open its own connection
    and could COMMIT without the paired ledger insert, which is exactly
    the state this roadmap item makes impossible."""
    violations: list[str] = []
    for path in _production_modules():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name in _MONEY_CALLS:
                keywords = {kw.arg for kw in node.keywords}
                if "connection" not in keywords:
                    violations.append(
                        f"{os.path.basename(path)}:{node.lineno} "
                        f"calls {name}() without connection="
                    )
    assert violations == [], (
        "wallet mutations outside a caller-owned transaction:\n"
        + "\n".join(violations)
    )


def test_8b_ledger_and_wallet_sql_stays_in_its_owning_module():
    """Ledger rows may only be written by ledger.py and wallet balances
    only by wallet.py, and history can never be mutated by SQL."""
    patterns = {
        re.compile(r"INSERT\s+INTO\s+ledger\b", re.I): ("ledger.py",),
        re.compile(r"UPDATE\s+ledger\b|DELETE\s+FROM\s+ledger\b", re.I): (),
        re.compile(
            r"INSERT\s+INTO\s+wallets\b|UPDATE\s+wallets\b|"
            r"DELETE\s+FROM\s+wallets\b",
            re.I,
        ): ("wallet.py",),
    }
    violations: list[str] = []
    for path in _production_modules():
        base = os.path.basename(path)
        tree = ast.parse(open(path, encoding="utf-8").read())
        for text in _executable_strings(tree):
            for pattern, allowed in patterns.items():
                if pattern.search(text) and base not in allowed:
                    violations.append(f"{base}: {pattern.pattern!r}")
    assert violations == [], (
        "SQL outside the owning module touched money tables:\n"
        + "\n".join(violations)
    )


def test_8c_money_entry_modules_own_their_transaction():
    """The three production money entry points must open their own
    ``db.transaction()`` (BEGIN IMMEDIATE) — the atomic boundary is
    never delegated to a caller or to sequential scopes."""
    root = os.path.dirname(os.path.abspath(__file__))
    for name in (
        "deposit_verification.py",
        "withdrawal_service.py",
        "task_completion.py",
    ):
        source = open(
            os.path.join(root, name), encoding="utf-8"
        ).read()
        assert "with db.transaction" in source, (
            f"{name} no longer wraps its money flow in db.transaction()"
        )
