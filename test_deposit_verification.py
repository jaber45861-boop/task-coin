"""
Focused tests — Authoritative Deposit Verification Contract (MT-ADMIN-29)
=========================================================================

The INTERNAL trusted boundary that a future verifier calls to verify
and credit a pending deposit:

    verify_and_credit(request_id, amount_units, external_tx_id,
                      facts={"source": ...})
        -> ONE db.transaction(): re-read -> pending CAS -> exact amount
           -> snapshot compatibility -> external-tx exclusivity ->
           wallet.credit_units -> ONE deposit ledger credit ->
           persist external_tx_id -> pending -> credited -> commit

Coverage required by MT-ADMIN-29 (20 tests):

A. SUCCESS PATH (1–5)
   1  successful pending deposit verification
   2  wallet balance increases exactly once
   3  exactly one deposit ledger credit (reference_type='deposit',
      reference_id=request_id)
   4  deposit becomes credited
   5  external_tx_id is persisted

B. AMOUNTS (6–9)
   6  amount mismatch rejected
   7  zero amount rejected
   8  negative amount rejected
   9  malformed amount rejected

C. STATE / IDEMPOTENCY (10–15)
   10 missing request rejected
   11 non-pending request rejected
   12 repeated identical verification is idempotent
   13 already-credited with different tx id rejected (domain conflict)
   14 same external tx id on another deposit rejected
   15 rejected verification leaves wallet/ledger/deposit unchanged

D. ATOMICITY (16–18)
   16 wallet failure rolls back deposit + ledger
   17 ledger failure rolls back wallet + deposit
   18 concurrent/double verification cannot double-credit

E. FACTS / SECURITY (19–20 + extras)
   19 external transaction ID normalization is deterministic
   20 no Mini App/public route is introduced
   21 invalid verification facts rejected (extra)
   22 payment-method snapshot incompatibility conflicts (rule 7, extra)
   23 deactivating a method does not block a verifiable pending credit
      (extra — pins the operational-vs-facts boundary)
   24 error codes are deterministic and stable (extra)

F. EGP FROZEN-SNAPSHOT GATE (extra — automatic-rate policy)
   creation freezes rate/provider/capture/units → credit uses the
   frozen units with full ledger metadata; an old request keeps its
   rate after a market move; legacy/corrupt snapshots are rejected
   with ZERO mutation; the credit is idempotent (once, ever); the
   amount check stays in the request's own EGP units; USDT is
   untouched.

Temp databases only; no production destinations or balances are used.
Money math is int-only — floats are rejected everywhere.

Run:
    python3 -m pytest test_deposit_verification.py -v
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import config
import db
import deposit_store
import deposit_verification
import ledger
import rate_store
import wallet
from rate_quote import PROVIDER_COINGECKO, RateQuote
from deposit_verification import (
    DepositAlreadyCreditedError,
    DepositAmountMismatchError,
    DepositAssetNotCreditableError,
    DepositConflictError,
    DepositNotFoundError,
    DepositNotPendingError,
    DepositVerificationError,
    ExternalTxIdAlreadyUsedError,
    InvalidExternalTxIdError,
    InvalidVerificationFactsError,
    normalize_external_tx_id,
)

# Reuse the MT-ADMIN-28 helpers (independently implemented).
from test_deposit import ADMIN_ID, _make_pm
from test_miniapp_auth import _TEST_BOT_TOKEN

USER_A = 4401
FUND = 1_000_000_000                 # 10 USDT
AMOUNT = "1.5"                       # -> 150_000_000 units
AMOUNT_UNITS = 150_000_000
TX_OK = "0xdeadbeefcafe0123456789abcdef"
FACTS = {"source": "unit-test-verifier"}


# ── Fixtures / helpers ───────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated database + registered user (the minimum is
    per-method and configured by the request helper below)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    db_path = str(tmp_path / "deposit_verify.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER_A, "alice", "Alice")
    monkeypatch.setattr(config, "ADMINS", [ADMIN_ID])
    yield db_path


def _request(db_path: str, *, amount: str = AMOUNT, **pm_overrides):
    """One PENDING deposit intent through the production creator."""
    # Floor of 1 atomic unit — the precision focus these tests had
    # with the old global minimum, now per-method (min_deposit_units).
    pm_overrides.setdefault("min_units", 1)
    pm = _make_pm(db_path, **pm_overrides)
    return deposit_store.create_deposit_request(
        user_id=USER_A,
        payment_method_id=pm.id,
        amount=amount,
        db_path=db_path,
    )


def _verify(
    request_id: str,
    units: object = AMOUNT_UNITS,
    *,
    tx: object = TX_OK,
    facts: object = FACTS,
    **kwargs,
):
    return deposit_verification.verify_and_credit(
        request_id,
        amount_units=units,
        external_tx_id=tx,
        facts=facts,
        **kwargs,
    )


def _exec(db_path: str, sql: str, params: tuple = ()) -> None:
    """Committed raw write — test-only state setup / inspection."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _raw(db_path: str, sql: str, params: tuple = ()) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _row(db_path: str, request_id: str) -> dict:
    rows = _raw(
        db_path,
        "SELECT * FROM deposit_requests WHERE request_id = ?",
        (request_id,),
    )
    assert rows, f"deposit {request_id!r} not found"
    return rows[0]


def _wallet_units(db_path: str, user_id: int = USER_A):
    rows = _raw(
        db_path,
        "SELECT available_units, held_units FROM wallets "
        "WHERE user_id = ?",
        (user_id,),
    )
    if not rows:
        return None
    return rows[0]["available_units"], rows[0]["held_units"]


def _deposit_ledger(db_path: str, request_id: str) -> list:
    return _raw(
        db_path,
        "SELECT * FROM ledger WHERE reference_type = 'deposit' "
        "AND reference_id = ?",
        (request_id,),
    )


def _fund(units: int = FUND, user_id: int = USER_A) -> int:
    wallet.ensure_wallet(user_id)
    return wallet.credit_units(user_id, units)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ── 1–5: success path ────────────────────────────────────────────────


class TestSuccessPath:
    def test_01_successful_pending_deposit_verification(self, env):
        req = _request(env)
        result = _verify(req.request_id)
        assert isinstance(result, deposit_verification.DepositVerificationResult)
        assert result.request_id == req.request_id
        assert result.user_id == USER_A
        assert result.amount_units == AMOUNT_UNITS
        assert result.external_tx_id == TX_OK
        assert result.status == "credited"
        assert result.already_credited is False

    def test_02_wallet_balance_increases_exactly_once(self, env):
        _fund()
        req = _request(env)
        assert _wallet_units(env) == (FUND, 0)
        _verify(req.request_id)
        assert _wallet_units(env) == (FUND + AMOUNT_UNITS, 0)
        # replay never moves money again
        _verify(req.request_id)
        assert _wallet_units(env) == (FUND + AMOUNT_UNITS, 0)

    def test_03_exactly_one_deposit_ledger_credit(self, env):
        req = _request(env)
        _verify(req.request_id)
        rows = _deposit_ledger(env, req.request_id)
        assert len(rows) == 1
        entry = rows[0]
        assert entry["entry_type"] == "credit"
        assert entry["reference_type"] == "deposit"
        assert entry["reference_id"] == req.request_id
        assert entry["amount_units"] == AMOUNT_UNITS
        assert entry["available_delta"] == AMOUNT_UNITS
        assert entry["held_delta"] == 0
        assert entry["currency"] == "USDT"
        assert entry["idempotency_key"] == f"deposit:{req.request_id}"
        # replay never appends a second entry
        _verify(req.request_id)
        assert len(_deposit_ledger(env, req.request_id)) == 1

    def test_04_deposit_becomes_credited(self, env):
        req = _request(env)
        _verify(req.request_id)
        row = _row(env, req.request_id)
        assert row["status"] == "credited"
        assert row["updated_at"] is not None
        assert deposit_store.get_deposit_request(
            req.request_id
        ).status == "credited"

    def test_05_external_tx_id_persisted(self, env):
        req = _request(env)
        _verify(req.request_id)
        assert _row(env, req.request_id)["external_tx_id"] == TX_OK


# ── 6–9: amounts ─────────────────────────────────────────────────────


class TestAmountValidation:
    def test_06_amount_mismatch_rejected(self, env):
        _fund()
        req = _request(env)
        with pytest.raises(DepositAmountMismatchError) as excinfo:
            _verify(req.request_id, AMOUNT_UNITS + 1)
        assert excinfo.value.code == "deposit_amount_mismatch"
        assert excinfo.value.verified_units == AMOUNT_UNITS + 1
        assert excinfo.value.persisted_units == AMOUNT_UNITS
        row = _row(env, req.request_id)
        assert row["status"] == "pending"
        assert row["external_tx_id"] is None
        assert _wallet_units(env) == (FUND, 0)
        assert _deposit_ledger(env, req.request_id) == []

    @pytest.mark.parametrize("bad", [0], ids=["zero"])
    def test_07_zero_amount_rejected(self, env, bad):
        _fund()
        req = _request(env)
        with pytest.raises(InvalidVerificationFactsError):
            _verify(req.request_id, bad)
        assert _wallet_units(env) == (FUND, 0)
        assert _row(env, req.request_id)["status"] == "pending"

    @pytest.mark.parametrize("bad", [-1, -AMOUNT_UNITS], ids=["neg1", "neg-units"])
    def test_08_negative_amount_rejected(self, env, bad):
        _fund()
        req = _request(env)
        with pytest.raises(InvalidVerificationFactsError):
            _verify(req.request_id, bad)
        assert _wallet_units(env) == (FUND, 0)
        assert _row(env, req.request_id)["status"] == "pending"

    @pytest.mark.parametrize(
        "bad",
        [1.5, "1.5", "150000000", True, None, b"150000000"],
        ids=["float", "str-dec", "str-int", "bool", "none", "bytes"],
    )
    def test_09_malformed_amount_rejected(self, env, bad):
        _fund()
        req = _request(env)
        with pytest.raises(InvalidVerificationFactsError):
            _verify(req.request_id, bad)
        row = _row(env, req.request_id)
        assert row["status"] == "pending"
        assert row["external_tx_id"] is None
        assert _wallet_units(env) == (FUND, 0)
        assert _deposit_ledger(env, req.request_id) == []


# ── 10–15: state & idempotency ───────────────────────────────────────


class TestStateAndIdempotency:
    def test_10_missing_request_rejected(self, env):
        _fund()
        with pytest.raises(DepositNotFoundError) as excinfo:
            _verify("no-such-request-000000000000000000000000")
        assert excinfo.value.code == "deposit_not_found"
        assert _wallet_units(env) == (FUND, 0)

    def test_11_non_pending_request_rejected(self, env):
        _fund()
        req = _request(env)
        _exec(
            env,
            "UPDATE deposit_requests SET status = 'rejected' "
            "WHERE request_id = ?",
            (req.request_id,),
        )
        with pytest.raises(DepositNotPendingError) as excinfo:
            _verify(req.request_id)
        assert excinfo.value.code == "deposit_not_pending"
        row = _row(env, req.request_id)
        assert row["status"] == "rejected"
        assert row["external_tx_id"] is None
        assert _wallet_units(env) == (FUND, 0)
        assert _deposit_ledger(env, req.request_id) == []

    def test_12_repeated_identical_verification_is_idempotent(self, env):
        _fund()
        req = _request(env)
        first = _verify(req.request_id)
        assert first.already_credited is False
        for _ in range(3):
            replay = _verify(req.request_id)
            assert replay.already_credited is True
            assert replay.status == "credited"
            assert replay.external_tx_id == TX_OK
        assert _wallet_units(env) == (FUND + AMOUNT_UNITS, 0)
        assert len(_deposit_ledger(env, req.request_id)) == 1

    def test_13_already_credited_different_tx_rejected(self, env):
        _fund()
        req = _request(env)
        _verify(req.request_id)
        with pytest.raises(DepositAlreadyCreditedError) as excinfo:
            _verify(req.request_id, tx="0xOTHERTX")
        # a domain conflict, deterministically coded
        assert excinfo.value.code == "deposit_already_credited"
        assert isinstance(excinfo.value, DepositConflictError)
        assert _wallet_units(env) == (FUND + AMOUNT_UNITS, 0)
        assert len(_deposit_ledger(env, req.request_id)) == 1
        assert _row(env, req.request_id)["external_tx_id"] == TX_OK

    def test_14_same_external_tx_on_another_deposit_rejected(self, env):
        _fund()
        req_a = _request(env)
        req_b = _request(env)
        _verify(req_a.request_id)
        balance = _wallet_units(env)
        with pytest.raises(ExternalTxIdAlreadyUsedError) as excinfo:
            _verify(req_b.request_id, tx=TX_OK)
        assert excinfo.value.code == "external_tx_id_already_used"
        # the other request is untouched
        row_b = _row(env, req_b.request_id)
        assert row_b["status"] == "pending"
        assert row_b["external_tx_id"] is None
        assert _wallet_units(env) == balance
        assert _deposit_ledger(env, req_b.request_id) == []
        assert len(_deposit_ledger(env, req_a.request_id)) == 1

    def test_15_rejected_verification_leaves_everything_unchanged(self, env):
        _fund()
        req = _request(env)
        before_row = _row(env, req.request_id)
        with pytest.raises(DepositAmountMismatchError):
            _verify(req.request_id, AMOUNT_UNITS * 2)
        assert _row(env, req.request_id) == before_row
        assert _wallet_units(env) == (FUND, 0)
        assert _deposit_ledger(env, req.request_id) == []
        # and the request is still verifiable afterwards
        _verify(req.request_id)
        assert _row(env, req.request_id)["status"] == "credited"


# ── 16–18: atomicity ─────────────────────────────────────────────────


class TestAtomicity:
    def test_16_wallet_failure_rolls_back_deposit_and_ledger(
        self, env, monkeypatch
    ):
        _fund()
        req = _request(env)

        def boom(*args, **kwargs):
            raise RuntimeError("wallet backend down")

        monkeypatch.setattr(wallet, "credit_units", boom)
        with pytest.raises(RuntimeError):
            _verify(req.request_id)

        row = _row(env, req.request_id)
        assert row["status"] == "pending"
        assert row["external_tx_id"] is None
        assert _deposit_ledger(env, req.request_id) == []
        assert _wallet_units(env) == (FUND, 0)

    def test_17_ledger_failure_rolls_back_wallet_and_deposit(
        self, env, monkeypatch
    ):
        _fund()
        req = _request(env)

        def boom(self, *args, **kwargs):
            raise RuntimeError("ledger backend down")

        monkeypatch.setattr(ledger.LedgerService, "record_credit", boom)
        with pytest.raises(RuntimeError):
            _verify(req.request_id)

        row = _row(env, req.request_id)
        assert row["status"] == "pending"
        assert row["external_tx_id"] is None
        assert _wallet_units(env) == (FUND, 0)
        assert _deposit_ledger(env, req.request_id) == []

    def test_18_concurrent_double_verification_cannot_double_credit(
        self, env
    ):
        _fund()
        req = _request(env)
        barrier = threading.Barrier(2)
        outcomes = []

        def worker():
            barrier.wait()
            try:
                result = _verify(req.request_id)
                outcomes.append(("ok", result))
            except Exception as exc:  # pragma: no cover - diagnostic
                outcomes.append(("err", exc))

        threads = [
            threading.Thread(target=worker) for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(outcomes) == 2, outcomes
        assert all(label == "ok" for label, _ in outcomes), outcomes
        credited = [r for _, r in outcomes if r.already_credited is False]
        replays = [r for _, r in outcomes if r.already_credited is True]
        assert len(credited) == 1, outcomes
        assert len(replays) == 1, outcomes
        # exactly ONE money movement and ONE ledger entry
        assert _wallet_units(env) == (FUND + AMOUNT_UNITS, 0)
        assert len(_deposit_ledger(env, req.request_id)) == 1
        row = _row(env, req.request_id)
        assert row["status"] == "credited"
        assert row["external_tx_id"] == TX_OK


# ── 19–24: facts, security, contract extras ──────────────────────────


class TestFactsAndSecurity:
    def test_19_external_tx_id_normalization_deterministic(self, env):
        # deterministic pure normalization: padded variants agree
        assert normalize_external_tx_id("  0xABC123\t\n") == "0xABC123"
        assert normalize_external_tx_id("0xABC123") == "0xABC123"
        assert normalize_external_tx_id("  0xABC123  ") == (
            normalize_external_tx_id("0xABC123")
        )
        # unusable ids are rejected, never fabricated
        for bad in ["", "   ", "0x bad tx", "0x\nnewline", 123, None, b"tx"]:
            with pytest.raises(InvalidExternalTxIdError):
                normalize_external_tx_id(bad)

        # a padded retry of the credited id is the SAME identity
        req = _request(env)
        _verify(req.request_id, tx="  0xCHAIN111  ")
        assert _row(env, req.request_id)["external_tx_id"] == "0xCHAIN111"
        replay = _verify(req.request_id, tx="0xCHAIN111")
        assert replay.already_credited is True

    def test_20_no_miniapp_or_public_route_introduced(self):
        import ast

        # AST-level check: the service imports no web/HTTP stack at all
        # (docstring prose is irrelevant — this scans real imports).
        src = _read("deposit_verification.py")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(
                    alias.name.split(".")[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        web_stack = {"flask", "requests", "urllib", "http", "aiohttp", "httpx"}
        assert not (imported & web_stack), imported & web_stack
        # no route registration is even present in the module
        assert ".route(" not in src
        assert "add_url_rule" not in src
        # no public endpoint imports or routes to it
        for path in (
            "deposit_routes.py", "serve_miniapp.py", "bot.py",
        ):
            assert "deposit_verification" not in _read(path), path

    def test_21_invalid_verification_facts_rejected(self, env):
        _fund()
        req = _request(env)
        bad_facts = [
            None,
            {},
            "verify",
            {"source": ""},
            {"source": "   "},
            {"evidence": "chain said so"},
            {"source": 42},
        ]
        for facts in bad_facts:
            with pytest.raises(InvalidVerificationFactsError):
                _verify(req.request_id, facts=facts)
        row = _row(env, req.request_id)
        assert row["status"] == "pending"
        assert row["external_tx_id"] is None
        assert _wallet_units(env) == (FUND, 0)
        assert _deposit_ledger(env, req.request_id) == []

    def test_22_method_snapshot_incompatibility_conflicts(self, env):
        _fund()
        req = _request(env)
        assert req.pm_asset == "USDT"
        # admin redefines the method's money-movement facts AFTER the
        # request snapshot
        _exec(
            env,
            "UPDATE payment_methods SET asset = 'USDC' WHERE id = ?",
            (req.payment_method_id,),
        )
        with pytest.raises(DepositConflictError) as excinfo:
            _verify(req.request_id)
        assert excinfo.value.code == "deposit_conflict"
        row = _row(env, req.request_id)
        assert row["status"] == "pending"
        assert row["external_tx_id"] is None
        assert _wallet_units(env) == (FUND, 0)
        assert _deposit_ledger(env, req.request_id) == []

    def test_23_deactivation_does_not_block_pending_credit(self, env):
        import payment_method_store

        _fund()
        req = _request(env)
        # operational flag only — the snapshot facts still match
        payment_method_store.set_payment_method_active(
            req.payment_method_id, False, updated_by=900_028,
            db_path=env,
        )
        result = _verify(req.request_id)
        assert result.status == "credited"
        assert _wallet_units(env) == (FUND + AMOUNT_UNITS, 0)

    def test_24_error_codes_are_deterministic(self):
        expected = {
            DepositNotFoundError: "deposit_not_found",
            DepositNotPendingError: "deposit_not_pending",
            DepositAmountMismatchError: "deposit_amount_mismatch",
            DepositAssetNotCreditableError: "deposit_asset_not_creditable",
            InvalidExternalTxIdError: "invalid_external_tx_id",
            ExternalTxIdAlreadyUsedError: "external_tx_id_already_used",
            DepositAlreadyCreditedError: "deposit_already_credited",
            DepositConflictError: "deposit_conflict",
            InvalidVerificationFactsError: "invalid_verification_facts",
        }
        for cls, code in expected.items():
            assert cls.code == code, cls
            assert issubclass(cls, DepositVerificationError)
        # already-credited IS a domain conflict
        assert issubclass(DepositAlreadyCreditedError, DepositConflictError)


# ── EGP frozen-snapshot helpers (automatic-rate policy) ──────────────


def _seed_rate(
    db_path: str, rate: str, *, age_seconds: float = 0.0
) -> RateQuote:
    """A fresh (or backdated) AUTOMATIC quote — the normal mode."""
    captured = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    quote = RateQuote(Decimal(rate), PROVIDER_COINGECKO, captured)
    rate_store.set_automatic_rate(quote, db_path=db_path)
    return quote


def _egp_request(db_path: str, *, rate: str | None = "48.5"):
    """One PENDING EGP deposit; ``rate=None`` creates it with NO
    quote present (legacy/missing-rate simulation at creation)."""
    pm = _make_pm(
        db_path,
        category="cash",
        asset="EGP",
        network=None,
        min_units=5000,            # 50.00 EGP at the 2-dp scale
    )
    if rate is not None:
        _seed_rate(db_path, rate)
    return deposit_store.create_deposit_request(
        user_id=USER_A,
        payment_method_id=pm.id,
        amount="50",
        db_path=db_path,
    )


class TestAssetCreditGate:
    """The credit boundary's asset rules under the automatic-rate
    policy: USDT stays byte-for-byte; EGP credits ONLY the frozen
    snapshot captured at creation (exact units + full ledger
    metadata); a request WITHOUT a valid snapshot — legacy or
    corrupted — is rejected BEFORE any wallet/ledger write with zero
    mutations; and an old request keeps its rate and amount even
    after the market moves."""

    # 50 EGP at 48.5 EGP/USDT = 1.030927835051546… USDT → ×1e8 →
    # 103_092_783.505… atomic units → ROUND_CEILING (MT-ADMIN-17) →
    # 103_092_784 — asserted as a literal, independent of the
    # production Decimal code path.
    FROZEN_UNITS = 103_092_784
    FROZEN_RATE = "48.5"
    NEW_RATE = "49.75"
    # What 50 EGP would be worth at NEW_RATE (50/49.75×1e8 =
    # 100_502_512.5… → ceil) — deliberately DIFFERENT from
    # FROZEN_UNITS so any recalculation is detectable.
    RECALCULATED_UNITS = 100_502_513

    def test_egp_creation_freezes_conversion_snapshot(self, env):
        """Creation freezes exact rate text, provider, capture instant
        and the converted unit count — while the request amount stays
        EGP cents."""
        request = _egp_request(env, rate=self.FROZEN_RATE)
        assert request.pm_asset == "EGP"
        assert request.amount_units == 5000          # EGP cents, exact
        assert request.status == "pending"
        assert request.wallet_rate_usdt_egp == self.FROZEN_RATE
        assert request.rate_provider == PROVIDER_COINGECKO
        assert request.rate_captured_at              # frozen instant
        assert request.credited_usdt_units == self.FROZEN_UNITS

    def test_egp_credit_uses_frozen_snapshot_exactly(self, env):
        """The credit lands as EXACT frozen USDT units with the full
        conversion audit trail in the ledger — the EGP amount itself
        never changes."""
        _fund()
        request = _egp_request(env, rate=self.FROZEN_RATE)

        result = _verify(request.request_id, units=5000)
        assert result.already_credited is False
        assert result.amount_units == self.FROZEN_UNITS
        assert result.status == "credited"

        # Wallet: funded balance + exactly the frozen units.
        assert _wallet_units(env) == (FUND + self.FROZEN_UNITS, 0)

        # One ledger credit: converted units, frozen rate, metadata.
        entries = _deposit_ledger(env, request.request_id)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["entry_type"] == "credit"
        assert entry["currency"] == "USDT"
        assert entry["amount_units"] == self.FROZEN_UNITS
        assert entry["rate_usdt_egp"] == self.FROZEN_RATE
        meta = json.loads(entry["metadata"])
        assert meta["conversion"] == "egp_to_usdt"
        assert meta["source_asset"] == "EGP"
        assert meta["source_amount_units"] == 5000
        assert meta["rate_usdt_egp"] == self.FROZEN_RATE
        assert meta["rate_provider"] == PROVIDER_COINGECKO
        assert meta["credited_usdt_units"] == self.FROZEN_UNITS

        # The request keeps its EGP amount and its frozen snapshot.
        row = _row(env, request.request_id)
        assert row["status"] == "credited"
        assert row["amount_units"] == 5000
        assert row["credited_usdt_units"] == self.FROZEN_UNITS
        assert row["wallet_rate_usdt_egp"] == self.FROZEN_RATE

    def test_old_request_keeps_its_rate_after_market_move(self, env):
        """A market move NEVER rewrites an existing request: credit
        uses the frozen rate/units, not the new quote."""
        _fund()
        request = _egp_request(env, rate=self.FROZEN_RATE)
        snapshot_before = (
            request.wallet_rate_usdt_egp,
            request.rate_captured_at,
            request.credited_usdt_units,
        )

        # The market moves after creation.
        _seed_rate(env, self.NEW_RATE)
        current = rate_store.get_current_quote(db_path=env)
        assert str(current.rate_usdt_egp) == self.NEW_RATE

        result = _verify(request.request_id, units=5000)
        assert result.amount_units == self.FROZEN_UNITS
        assert result.amount_units != self.RECALCULATED_UNITS
        assert _wallet_units(env) == (FUND + self.FROZEN_UNITS, 0)

        # Row snapshot is byte-for-byte what creation froze.
        row_after = _row(env, request.request_id)
        assert (
            row_after["wallet_rate_usdt_egp"],
            row_after["rate_captured_at"],
            row_after["credited_usdt_units"],
        ) == snapshot_before
        entries = _deposit_ledger(env, request.request_id)
        assert len(entries) == 1
        assert entries[0]["rate_usdt_egp"] == self.FROZEN_RATE
        assert entries[0]["amount_units"] == self.FROZEN_UNITS

    def test_legacy_row_without_snapshot_rejected(self, env):
        """A pre-migration row (NULL snapshot) can never be credited:
        zero mutation and the wallet row is never even created."""
        request = _egp_request(env, rate=self.FROZEN_RATE)
        _exec(
            env,
            "UPDATE deposit_requests SET wallet_rate_usdt_egp = NULL, "
            "rate_provider = NULL, rate_captured_at = NULL, "
            "credited_usdt_units = NULL WHERE request_id = ?",
            (request.request_id,),
        )

        with pytest.raises(DepositAssetNotCreditableError) as excinfo:
            _verify(request.request_id, units=5000)
        assert excinfo.value.code == "deposit_asset_not_creditable"

        assert _wallet_units(env) is None
        assert _deposit_ledger(env, request.request_id) == []
        row = _row(env, request.request_id)
        assert row["status"] == "pending"
        assert row["external_tx_id"] is None

    @pytest.mark.parametrize(
        "column, value",
        [
            ("wallet_rate_usdt_egp", "not-a-rate"),
            ("rate_provider", "bogus-exchange"),
            ("rate_captured_at", "not-a-timestamp"),
            # credited_usdt_units=0 is impossible even by raw SQL:
            # the column carries CHECK (IS NULL OR > 0).
        ],
    )
    def test_corrupt_snapshot_rejected_with_zero_mutation(
        self, env, column, value
    ):
        """A corrupted frozen snapshot is refused BEFORE any
        wallet/ledger write — nothing is guessed, the current rate is
        never substituted."""
        _fund()
        request = _egp_request(env, rate=self.FROZEN_RATE)
        _exec(
            env,
            f"UPDATE deposit_requests SET {column} = ? "
            "WHERE request_id = ?",
            (value, request.request_id),
        )

        with pytest.raises(DepositAssetNotCreditableError):
            _verify(request.request_id, units=5000)

        assert _wallet_units(env) == (FUND, 0)
        assert _deposit_ledger(env, request.request_id) == []
        assert _row(env, request.request_id)["status"] == "pending"

    def test_egp_amount_check_stays_in_egp_units(self, env):
        """The verified amount must equal the request's OWN units
        (EGP cents) — a USDT-style unit count is a mismatch, with
        zero mutation."""
        _fund()
        request = _egp_request(env, rate=self.FROZEN_RATE)

        with pytest.raises(DepositAmountMismatchError):
            _verify(request.request_id, units=AMOUNT_UNITS)

        assert _wallet_units(env) == (FUND, 0)
        assert _deposit_ledger(env, request.request_id) == []
        assert _row(env, request.request_id)["status"] == "pending"

    def test_egp_credit_is_idempotent_exactly_once(self, env):
        """A replayed verification with the SAME tx id reports the
        SAME credited units without touching the wallet or ledger
        again; a different tx id is a conflict — credited once, ever."""
        _fund()
        request = _egp_request(env, rate=self.FROZEN_RATE)

        first = _verify(request.request_id, units=5000)
        assert first.already_credited is False
        assert first.amount_units == self.FROZEN_UNITS

        replay = _verify(request.request_id, units=5000)
        assert replay.already_credited is True
        assert replay.amount_units == self.FROZEN_UNITS

        assert _wallet_units(env) == (FUND + self.FROZEN_UNITS, 0)
        assert len(_deposit_ledger(env, request.request_id)) == 1

        with pytest.raises(DepositAlreadyCreditedError):
            _verify(
                request.request_id,
                units=5000,
                tx="0xfeedfacecafe0123456789abcdef",
            )
        assert _wallet_units(env) == (FUND + self.FROZEN_UNITS, 0)
        assert len(_deposit_ledger(env, request.request_id)) == 1
