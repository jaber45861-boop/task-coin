"""
Focused tests — BSC USDT Deposit Verification Adapter (MT-ADMIN-30)
====================================================================

The READ-ONLY chain adapter that obtains blockchain facts from an
authoritative, configured provider and produces the exact facts for
the unchanged MT-ADMIN-29 credit boundary:

    verify_deposit_transaction(tx_hash, expected_amount_units,
                               config=..., client=...)
        -> chain 56 -> tx exists (hash cross-checked) -> tx targets
           the configured token -> receipt exists and matches ->
           status success -> exactly one ERC-20 Transfer of the
           configured token to the configured deposit address ->
           exact amount (decimals read from the chain, EXACT 8-dp
           conversion) -> finality >= configured confirmations
        -> ChainDepositFacts (amount_units, external_tx_id,
           facts={"source": ...})

    verify_and_credit_deposit(request_id, tx_hash, ...)
        -> amount read from the PERSISTED request (never the client)
           -> verify_deposit_transaction(...)
           -> deposit_verification.verify_and_credit(...) unchanged

Coverage required by MT-ADMIN-30:

A. CONFIGURATION (env only, fails closed, nothing in source)
B. SUCCESS PATH (exact facts + end-to-end credit + read-only calls)
C. WRONG CHAIN / TOKEN / DESTINATION / AMOUNT verdicts
D. FAILED + UNFINALIZED transactions
E. DUPLICATE tx id across requests + idempotent replay
F. MALFORMED provider data + provider/transport failures
G. NOT FOUND / invalid client locator / client amount never trusted
H. Contract regression (verify_and_credit signature untouched,
   deterministic error codes)

Every provider response is fully MOCKED (injected transport); no
network access, no production endpoints, no production addresses.

Run:
    python3 -m pytest test_bsc_deposit_adapter.py -v
"""

from __future__ import annotations

import inspect
import re
import sqlite3
import urllib.error
import urllib.request

import pytest

import bsc_deposit_adapter as adapter
import config
import db
import deposit_store
import deposit_verification
from bsc_deposit_adapter import (
    AdapterConfigError,
    AmountMismatchError,
    ChainMismatchError,
    ChainDepositFacts,
    DestinationMismatchError,
    InvalidInputError,
    JsonRpcClient,
    MalformedProviderDataError,
    ProviderError,
    TokenMismatchError,
    TransactionFailedError,
    TransactionNotFoundError,
    UnfinalizedTransactionError,
    VERIFICATION_SOURCE,
    DEPOSIT_ADDRESS_ENV,
    REQUIRED_CONFIRMATIONS_ENV,
    RPC_URL_ENV,
    TOKEN_CONTRACT_ENV,
    load_bsc_adapter_config,
    verify_and_credit_deposit,
    verify_deposit_transaction,
)
from deposit_verification import (
    DepositNotFoundError,
    ExternalTxIdAlreadyUsedError,
)
from test_deposit import ADMIN_ID, _make_pm, _seed_minimum
from test_miniapp_auth import _TEST_BOT_TOKEN

# ── Test constants (obviously fake — no production values) ───────────

RPC_URL = "https://rpc.test.invalid"

TOKEN_CONTRACT = "0x" + "ac" * 20
DEPOSIT_ADDRESS = "0x" + "bd" * 20
OTHER_ADDRESS = "0x" + "33" * 20
WRONG_TOKEN = "0x" + "44" * 20
SENDER_ADDRESS = "0x" + "ef" * 20
TX_HASH = "0x" + "ab" * 32
OTHER_TX_HASH = "0x" + "cd" * 32

TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

USER_A = 4401
AMOUNT = "1.5"
AMOUNT_UNITS = 150_000_000            # 1.5 USDT at 8 dp
TOKEN_DECIMALS = 18
RAW_UNITS = AMOUNT_UNITS * 10 ** (TOKEN_DECIMALS - 8)

RECEIPT_BLOCK = 0x0FE0                # 4064
BLOCK_HEAD = 0x1000                   # 4096 -> 33 confirmations

VALID_ENV = {
    RPC_URL_ENV: RPC_URL,
    TOKEN_CONTRACT_ENV: TOKEN_CONTRACT,
    DEPOSIT_ADDRESS_ENV: DEPOSIT_ADDRESS,
}


# ── Mocked provider ──────────────────────────────────────────────────


def _transfer_log(token: str, recipient: str, raw_value: int) -> dict:
    return {
        "address": token,
        "topics": [
            TRANSFER_TOPIC,
            "0x" + "0" * 24 + SENDER_ADDRESS[2:],
            "0x" + "0" * 24 + recipient[2:],
        ],
        "data": "0x" + f"{raw_value:064x}",
    }


def _checksum(addr: str) -> str:
    """Checksum-cased spelling of an address (EIP-55 style)."""
    return addr[:2] + addr[2:].upper()


def _receipt(**overrides) -> dict:
    base = {
        "transactionHash": TX_HASH,
        "status": "0x1",
        "blockNumber": hex(RECEIPT_BLOCK),
        "logs": [
            _transfer_log(TOKEN_CONTRACT, DEPOSIT_ADDRESS, RAW_UNITS)
        ],
    }
    base.update(overrides)
    return base


def _results(**overrides) -> dict:
    base = {
        "eth_chainId": "0x38",
        "eth_blockNumber": hex(BLOCK_HEAD),
        "eth_getTransactionByHash": {
            "hash": TX_HASH,
            "to": TOKEN_CONTRACT,
            "blockNumber": hex(RECEIPT_BLOCK),
        },
        "eth_getTransactionReceipt": _receipt(),
        "eth_call": hex(TOKEN_DECIMALS),
    }
    base.update(overrides)
    return base


class FakeRpc:
    """Fully mocked provider responses behind a transport callable."""

    def __init__(
        self,
        results: dict | None = None,
        errors: dict | None = None,
    ) -> None:
        self.results = _results() if results is None else results
        self.errors = dict(errors or {})
        self.calls: list[tuple[str, list]] = []

    def transport(self, payload: dict) -> dict:
        method = payload["method"]
        params = payload.get("params") or []
        self.calls.append((method, params))
        if method in self.errors:
            return {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "error": self.errors[method],
            }
        if method not in self.results:
            return {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "error": {
                    "code": -32601,
                    "message": f"fixture has no response for {method}",
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "result": self.results[method],
        }

    @property
    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


def _client(fake: FakeRpc) -> JsonRpcClient:
    return JsonRpcClient(RPC_URL, transport=fake.transport)


def _verify(
    tx: object = TX_HASH,
    expected: object = AMOUNT_UNITS,
    *,
    fake: FakeRpc | None = None,
    config_obj=None,
):
    fake = fake if fake is not None else FakeRpc()
    facts = verify_deposit_transaction(
        tx, expected, config=config_obj, client=_client(fake)
    )
    return facts, fake


# ── Database fixtures / helpers (mirror MT-ADMIN-29 tests) ───────────


@pytest.fixture
def clean_env(monkeypatch):
    """All adapter configuration absent."""
    for name in (
        RPC_URL_ENV,
        TOKEN_CONTRACT_ENV,
        DEPOSIT_ADDRESS_ENV,
        REQUIRED_CONFIRMATIONS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config_env(monkeypatch):
    """Valid adapter configuration in the environment."""
    for name, value in VALID_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(REQUIRED_CONFIRMATIONS_ENV, raising=False)


@pytest.fixture
def env(config_env, monkeypatch, tmp_path):
    """Isolated database + registered user + configured minimum."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    db_path = str(tmp_path / "bsc_adapter.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER_A, "alice", "Alice")
    monkeypatch.setattr(config, "ADMINS", [ADMIN_ID])
    _seed_minimum(db_path, 1)
    yield db_path


def _request(db_path: str, *, amount: str = AMOUNT, **pm_overrides):
    """One PENDING deposit intent through the production creator."""
    pm = _make_pm(db_path, **pm_overrides)
    return deposit_store.create_deposit_request(
        user_id=USER_A,
        payment_method_id=pm.id,
        amount=amount,
        db_path=db_path,
    )


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


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ════════════════════════════════════════════════════════════════════
# A. Configuration — environment only, fails closed, nothing in source
# ════════════════════════════════════════════════════════════════════


class TestConfiguration:

    def test_config_loaded_from_environment(self, monkeypatch):
        for name, value in VALID_ENV.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv(REQUIRED_CONFIRMATIONS_ENV, "25")
        cfg = load_bsc_adapter_config()
        assert cfg.token_contract == TOKEN_CONTRACT
        assert cfg.deposit_address == DEPOSIT_ADDRESS
        assert cfg.required_confirmations == 25

    def test_config_repr_hides_endpoint(self, monkeypatch):
        for name, value in VALID_ENV.items():
            monkeypatch.setenv(name, value)
        cfg = load_bsc_adapter_config()
        assert RPC_URL not in repr(cfg)
        assert "token_contract" in repr(cfg)

    def test_confirmations_default_when_unset(self, config_env):
        cfg = load_bsc_adapter_config()
        assert cfg.required_confirmations == (
            adapter.DEFAULT_REQUIRED_CONFIRMATIONS
        )

    @pytest.mark.parametrize("missing", sorted(VALID_ENV))
    def test_missing_required_env_fails_closed(
        self, clean_env, monkeypatch, missing
    ):
        for name, value in VALID_ENV.items():
            if name != missing:
                monkeypatch.setenv(name, value)
        with pytest.raises(AdapterConfigError) as excinfo:
            load_bsc_adapter_config()
        assert excinfo.value.code == "adapter_config_error"
        assert missing in str(excinfo.value)

    @pytest.mark.parametrize(
        "name,value",
        [
            (RPC_URL_ENV, ""),
            (RPC_URL_ENV, "not-a-url"),
            (RPC_URL_ENV, "ftp://rpc.invalid"),
            (RPC_URL_ENV, "https:///nohost"),
            (TOKEN_CONTRACT_ENV, ""),
            (TOKEN_CONTRACT_ENV, "0x123"),
            (TOKEN_CONTRACT_ENV, "0x" + "zz" * 20),
            (DEPOSIT_ADDRESS_ENV, ""),
            (DEPOSIT_ADDRESS_ENV, TOKEN_CONTRACT[:-1]),
            (REQUIRED_CONFIRMATIONS_ENV, "abc"),
            (REQUIRED_CONFIRMATIONS_ENV, "0"),
            (REQUIRED_CONFIRMATIONS_ENV, "1001"),
            (REQUIRED_CONFIRMATIONS_ENV, "-5"),
        ],
    )
    def test_malformed_env_value_rejected(
        self, config_env, monkeypatch, name, value
    ):
        monkeypatch.setenv(name, value)
        with pytest.raises(AdapterConfigError) as excinfo:
            load_bsc_adapter_config()
        assert excinfo.value.code == "adapter_config_error"

    def test_adapter_fails_closed_before_any_provider_call(
        self, clean_env
    ):
        fake = FakeRpc()
        with pytest.raises(AdapterConfigError):
            verify_deposit_transaction(
                TX_HASH, AMOUNT_UNITS, client=_client(fake)
            )
        assert fake.calls == []

    def test_no_configuration_literals_in_source(self):
        """Credentials/config live in the environment, never in code."""
        src = _read(adapter.__file__)
        # No literal 20-byte hex address anywhere (the TRANSFER_TOPIC
        # is a 32-byte topic — the lookahead keeps it excluded).
        assert not re.search(
            r"0x[0-9a-fA-F]{40}(?![0-9a-fA-F])", src
        )
        # No literal endpoint/scheme anywhere (an RPC URL may embed
        # an API key).
        assert "http://" not in src
        assert "https://" not in src
        # The configuration channel is explicitly the environment.
        for name in VALID_ENV:
            assert name in src


# ════════════════════════════════════════════════════════════════════
# B. Success path — exact facts + end-to-end credit
# ════════════════════════════════════════════════════════════════════


class TestSuccessPath:

    pytestmark = pytest.mark.usefixtures("config_env")

    def test_successful_verification_produces_exact_facts(self):
        facts, fake = _verify()
        assert isinstance(facts, ChainDepositFacts)
        assert facts.tx_hash == TX_HASH
        assert facts.external_tx_id == TX_HASH
        assert facts.chain_id == 56
        assert facts.token_contract == TOKEN_CONTRACT
        assert facts.deposit_address == DEPOSIT_ADDRESS
        assert facts.token_decimals == TOKEN_DECIMALS
        assert facts.amount_units == AMOUNT_UNITS
        assert facts.block_number == RECEIPT_BLOCK
        assert facts.confirmations == (
            BLOCK_HEAD - RECEIPT_BLOCK + 1
        )
        assert facts.source == VERIFICATION_SOURCE
        assert facts.verification_facts() == {
            "source": VERIFICATION_SOURCE
        }
        # every observed fact came from the mocked provider
        assert set(fake.methods) == {
            "eth_chainId",
            "eth_getTransactionByHash",
            "eth_getTransactionReceipt",
            "eth_call",
            "eth_blockNumber",
        }

    def test_tx_hash_normalizes_to_lowercase(self):
        facts, _ = _verify(TX_HASH.upper().replace("0X", "0x"))
        assert facts.external_tx_id == TX_HASH

    def test_end_to_end_credit_through_unchanged_boundary(self, env):
        req = _request(env)
        fake = FakeRpc()
        result, facts = verify_and_credit_deposit(
            req.request_id, TX_HASH, client=_client(fake)
        )
        assert result.status == "credited"
        assert result.already_credited is False
        assert result.amount_units == AMOUNT_UNITS
        assert result.external_tx_id == TX_HASH
        assert facts.amount_units == AMOUNT_UNITS
        # wallet + ledger + persisted tx id, exactly once
        assert _wallet_units(env) == (AMOUNT_UNITS, 0)
        entries = _deposit_ledger(env, req.request_id)
        assert len(entries) == 1
        assert entries[0]["entry_type"] == "credit"
        assert entries[0]["amount_units"] == AMOUNT_UNITS
        row = _row(env, req.request_id)
        assert row["status"] == "credited"
        assert row["external_tx_id"] == TX_HASH

    def test_adapter_calls_are_read_only(self):
        _, fake = _verify()
        assert fake.methods  # provider was consulted
        assert all(
            method in adapter_read_only_methods()
            for method in fake.methods
        )

    def test_missing_request_fails_before_any_provider_call(self, env):
        fake = FakeRpc()
        with pytest.raises(DepositNotFoundError):
            verify_and_credit_deposit(
                "no-such-request", TX_HASH, client=_client(fake)
            )
        assert fake.calls == []


def adapter_read_only_methods() -> set:
    return {
        "eth_chainId",
        "eth_getTransactionByHash",
        "eth_getTransactionReceipt",
        "eth_call",
        "eth_blockNumber",
    }


# ════════════════════════════════════════════════════════════════════
# C. Wrong chain / token / destination / amount
# ════════════════════════════════════════════════════════════════════


class TestWrongChain:

    pytestmark = pytest.mark.usefixtures("config_env")

    def test_wrong_chain_rejected(self):
        fake = FakeRpc(_results(eth_chainId="0x1"))
        with pytest.raises(ChainMismatchError) as excinfo:
            _verify(fake=fake)
        assert excinfo.value.code == "chain_mismatch"
        assert excinfo.value.observed_chain_id == 1
        # rejected before any transaction data is fetched
        assert fake.methods == ["eth_chainId"]

    def test_chain_id_malformed_rejected(self):
        fake = FakeRpc(_results(eth_chainId="mainnet"))
        with pytest.raises(MalformedProviderDataError) as excinfo:
            _verify(fake=fake)
        assert excinfo.value.code == "malformed_provider_data"


class TestWrongToken:

    pytestmark = pytest.mark.usefixtures("config_env")

    def test_transfer_from_other_token_contract_rejected(self):
        fake = FakeRpc(_results(
            eth_getTransactionReceipt=_receipt(logs=[
                _transfer_log(
                    WRONG_TOKEN, DEPOSIT_ADDRESS, RAW_UNITS
                )
            ])
        ))
        with pytest.raises(TokenMismatchError) as excinfo:
            _verify(fake=fake)
        assert excinfo.value.code == "token_mismatch"

    def test_transaction_not_targeting_token_rejected(self):
        fake = FakeRpc(_results(
            eth_getTransactionByHash={
                "hash": TX_HASH,
                "to": DEPOSIT_ADDRESS,
                "blockNumber": hex(RECEIPT_BLOCK),
            }
        ))
        with pytest.raises(TokenMismatchError) as excinfo:
            _verify(fake=fake)
        assert excinfo.value.code == "token_mismatch"

    def test_contract_creation_transaction_rejected(self):
        fake = FakeRpc(_results(
            eth_getTransactionByHash={
                "hash": TX_HASH,
                "to": None,
                "blockNumber": hex(RECEIPT_BLOCK),
            }
        ))
        with pytest.raises(TokenMismatchError):
            _verify(fake=fake)

    def test_transaction_without_transfer_event_rejected(self):
        fake = FakeRpc(_results(
            eth_getTransactionReceipt=_receipt(logs=[])
        ))
        with pytest.raises(TokenMismatchError) as excinfo:
            _verify(fake=fake)
        assert excinfo.value.code == "token_mismatch"


class TestWrongDestination:

    pytestmark = pytest.mark.usefixtures("config_env")

    def test_transfer_to_other_address_rejected(self):
        fake = FakeRpc(_results(
            eth_getTransactionReceipt=_receipt(logs=[
                _transfer_log(TOKEN_CONTRACT, OTHER_ADDRESS, RAW_UNITS)
            ])
        ))
        with pytest.raises(DestinationMismatchError) as excinfo:
            _verify(fake=fake)
        assert excinfo.value.code == "destination_mismatch"

    def test_destination_is_case_insensitive(self):
        """Checksum-cased provider addresses still match."""
        fake = FakeRpc(_results(
            eth_getTransactionReceipt=_receipt(logs=[
                _transfer_log(
                    _checksum(TOKEN_CONTRACT),
                    _checksum(DEPOSIT_ADDRESS),
                    RAW_UNITS,
                )
            ]),
            eth_getTransactionByHash={
                "hash": TX_HASH.upper().replace("0X", "0x"),
                "to": _checksum(TOKEN_CONTRACT),
                "blockNumber": hex(RECEIPT_BLOCK),
            },
        ))
        facts, _ = _verify(fake=fake)
        assert facts.amount_units == AMOUNT_UNITS
        assert facts.token_contract == TOKEN_CONTRACT
        assert facts.deposit_address == DEPOSIT_ADDRESS


class TestWrongAmount:

    pytestmark = pytest.mark.usefixtures("config_env")

    def test_wrong_amount_rejected(self):
        other_raw = 200_000_000 * 10 ** (TOKEN_DECIMALS - 8)
        fake = FakeRpc(_results(
            eth_getTransactionReceipt=_receipt(logs=[
                _transfer_log(
                    TOKEN_CONTRACT, DEPOSIT_ADDRESS, other_raw
                )
            ])
        ))
        with pytest.raises(AmountMismatchError) as excinfo:
            _verify(fake=fake)
        assert excinfo.value.code == "amount_mismatch"
        assert excinfo.value.expected_units == AMOUNT_UNITS
        assert excinfo.value.observed_units == 200_000_000

    def test_client_supplied_amount_is_never_trusted(self):
        """The provider amount wins — the caller's expectation is
        only a comparison target, never the returned fact."""
        with pytest.raises(AmountMismatchError) as excinfo:
            _verify(expected=999)
        assert excinfo.value.observed_units == AMOUNT_UNITS
        assert excinfo.value.expected_units == 999

    @pytest.mark.parametrize("bad", [1.5, True, "150000000", 0, -1])
    def test_non_exact_expected_amount_rejected_before_provider(
        self, bad
    ):
        fake = FakeRpc()
        with pytest.raises(InvalidInputError) as excinfo:
            _verify(expected=bad, fake=fake)
        assert excinfo.value.code == "invalid_input"
        assert fake.calls == []

    def test_sub_precision_on_chain_amount_rejected(self):
        """Raw value not landing exactly on the 8-dp grid is never
        rounded — it cannot equal the expected amount."""
        jagged = RAW_UNITS + 1
        fake = FakeRpc(_results(
            eth_getTransactionReceipt=_receipt(logs=[
                _transfer_log(
                    TOKEN_CONTRACT, DEPOSIT_ADDRESS, jagged
                )
            ])
        ))
        with pytest.raises(AmountMismatchError) as excinfo:
            _verify(fake=fake)
        assert excinfo.value.observed_units is None
        assert "representable" in str(excinfo.value)

    def test_six_decimal_token_converts_exactly(self):
        raw_6dp = 1_500_000          # 1.5 USDT at 6 dp
        fake = FakeRpc(_results(
            eth_call="0x6",
            eth_getTransactionReceipt=_receipt(logs=[
                _transfer_log(
                    TOKEN_CONTRACT, DEPOSIT_ADDRESS, raw_6dp
                )
            ]),
        ))
        facts, _ = _verify(fake=fake)
        assert facts.token_decimals == 6
        assert facts.amount_units == AMOUNT_UNITS


# ════════════════════════════════════════════════════════════════════
# D. Failed + unfinalized transactions
# ════════════════════════════════════════════════════════════════════


class TestFailedAndUnfinalized:

    pytestmark = pytest.mark.usefixtures("config_env")

    def test_reverted_transaction_rejected(self):
        fake = FakeRpc(_results(
            eth_getTransactionReceipt=_receipt(status="0x0")
        ))
        with pytest.raises(TransactionFailedError) as excinfo:
            _verify(fake=fake)
        assert excinfo.value.code == "transaction_failed"

    @pytest.mark.parametrize("status", ["0x2", 3])
    def test_non_success_status_rejected(self, status):
        fake = FakeRpc(_results(
            eth_getTransactionReceipt=_receipt(status=status)
        ))
        with pytest.raises(TransactionFailedError):
            _verify(fake=fake)

    def test_unfinalized_transaction_rejected(self):
        head = RECEIPT_BLOCK + 3      # only 4 confirmations
        fake = FakeRpc(_results(eth_blockNumber=hex(head)))
        with pytest.raises(UnfinalizedTransactionError) as excinfo:
            _verify(fake=fake)
        assert excinfo.value.code == "unfinalized_transaction"
        assert excinfo.value.confirmations == 4
        assert excinfo.value.required == (
            adapter.DEFAULT_REQUIRED_CONFIRMATIONS
        )

    def test_finality_threshold_is_configurable(self, config_env, monkeypatch):
        monkeypatch.setenv(REQUIRED_CONFIRMATIONS_ENV, "2")
        head = RECEIPT_BLOCK + 1      # exactly 2 confirmations
        fake = FakeRpc(_results(eth_blockNumber=hex(head)))
        facts, _ = _verify(fake=fake)
        assert facts.confirmations == 2
        # one confirmation less still fails
        fake_low = FakeRpc(_results(eth_blockNumber=hex(RECEIPT_BLOCK)))
        with pytest.raises(UnfinalizedTransactionError):
            _verify(fake=fake_low)

    def test_receipt_block_above_head_is_malformed(self):
        fake = FakeRpc(_results(eth_blockNumber=hex(RECEIPT_BLOCK - 1)))
        with pytest.raises(MalformedProviderDataError):
            _verify(fake=fake)


# ════════════════════════════════════════════════════════════════════
# E. Duplicate tx + idempotent replay
# ════════════════════════════════════════════════════════════════════


class TestDuplicateTransaction:

    pytestmark = pytest.mark.usefixtures("config_env")

    def test_same_tx_cannot_credit_a_second_deposit(self, env):
        first = _request(env)
        fake = FakeRpc()
        verify_and_credit_deposit(
            first.request_id, TX_HASH, client=_client(fake)
        )
        second = _request(env)
        with pytest.raises(ExternalTxIdAlreadyUsedError) as excinfo:
            verify_and_credit_deposit(
                second.request_id, TX_HASH, client=_client(fake)
            )
        assert excinfo.value.code == "external_tx_id_already_used"
        # exactly one credit happened
        assert _wallet_units(env) == (AMOUNT_UNITS, 0)
        assert _row(env, second.request_id)["status"] == "pending"
        assert _row(env, second.request_id)["external_tx_id"] is None

    def test_repeated_verification_is_idempotent(self, env):
        req = _request(env)
        fake = FakeRpc()
        verify_and_credit_deposit(
            req.request_id, TX_HASH, client=_client(fake)
        )
        result, _ = verify_and_credit_deposit(
            req.request_id, TX_HASH, client=_client(fake)
        )
        assert result.already_credited is True
        assert _wallet_units(env) == (AMOUNT_UNITS, 0)
        assert len(_deposit_ledger(env, req.request_id)) == 1

    def test_foreign_tx_hash_on_credited_request_conflicts(self, env):
        req = _request(env)
        fake = FakeRpc()
        verify_and_credit_deposit(
            req.request_id, TX_HASH, client=_client(fake)
        )
        other = FakeRpc(_results(
            eth_getTransactionByHash={
                "hash": OTHER_TX_HASH,
                "to": TOKEN_CONTRACT,
                "blockNumber": hex(RECEIPT_BLOCK),
            },
            eth_getTransactionReceipt=_receipt(
                transactionHash=OTHER_TX_HASH
            ),
        ))
        from deposit_verification import DepositAlreadyCreditedError

        with pytest.raises(DepositAlreadyCreditedError):
            verify_and_credit_deposit(
                req.request_id, OTHER_TX_HASH, client=_client(other)
            )


# ════════════════════════════════════════════════════════════════════
# F. Malformed provider data + provider/transport failures
# ════════════════════════════════════════════════════════════════════


def _receipt_without(key: str) -> dict:
    receipt = _receipt()
    del receipt[key]
    return receipt


class TestMalformedProviderData:

    pytestmark = pytest.mark.usefixtures("config_env")

    @pytest.mark.parametrize(
        "results",
        [
            # transaction is not an object
            _results(eth_getTransactionByHash="0xdead"),
            # provider answers with a DIFFERENT transaction
            _results(eth_getTransactionByHash={
                "hash": OTHER_TX_HASH,
                "to": TOKEN_CONTRACT,
            }),
            # transaction to-field is not an address
            _results(eth_getTransactionByHash={
                "hash": TX_HASH,
                "to": "0x1234",
            }),
            # receipt is not an object
            _results(eth_getTransactionReceipt=["nope"]),
            # receipt belongs to another transaction
            _results(eth_getTransactionReceipt=_receipt(
                transactionHash=OTHER_TX_HASH
            )),
            # receipt without status
            _results(eth_getTransactionReceipt=_receipt_without(
                "status"
            )),
            # receipt without blockNumber
            _results(eth_getTransactionReceipt=_receipt_without(
                "blockNumber"
            )),
            # receipt logs are not a list
            _results(eth_getTransactionReceipt=_receipt(logs="nope")),
            # log entry is not an object
            _results(eth_getTransactionReceipt=_receipt(logs=["x"])),
            # log address is not an address
            _results(eth_getTransactionReceipt=_receipt(logs=[{
                "address": "0x1",
                "topics": [TRANSFER_TOPIC],
                "data": "0x" + f"{RAW_UNITS:064x}",
            }])),
            # Transfer log with wrong topic count
            _results(eth_getTransactionReceipt=_receipt(logs=[{
                "address": TOKEN_CONTRACT,
                "topics": [TRANSFER_TOPIC],
                "data": "0x" + f"{RAW_UNITS:064x}",
            }])),
            # Transfer data is not one 32-byte word
            _results(eth_getTransactionReceipt=_receipt(logs=[{
                "address": TOKEN_CONTRACT,
                "topics": [
                    TRANSFER_TOPIC,
                    "0x" + "0" * 24 + SENDER_ADDRESS[2:],
                    "0x" + "0" * 24 + DEPOSIT_ADDRESS[2:],
                ],
                "data": "0x1234",
            }])),
            # receipt block number is not hex
            _results(eth_getTransactionReceipt=_receipt(
                blockNumber="0xzz"
            )),
            # decimals() result is garbage
            _results(eth_call="banana"),
            # decimals() out of sane range
            _results(eth_call="0xff"),
        ],
        ids=lambda value: (
            str(value)[:60] if not isinstance(value, dict) else "dict"
        ),
    )
    def test_malformed_responses_rejected(self, results):
        fake = FakeRpc(results)
        with pytest.raises(MalformedProviderDataError) as excinfo:
            _verify(fake=fake)
        assert excinfo.value.code == "malformed_provider_data"

    def test_multiple_matching_transfers_are_ambiguous(self):
        fake = FakeRpc(_results(
            eth_getTransactionReceipt=_receipt(logs=[
                _transfer_log(
                    TOKEN_CONTRACT, DEPOSIT_ADDRESS, RAW_UNITS
                ),
                _transfer_log(
                    TOKEN_CONTRACT, DEPOSIT_ADDRESS, RAW_UNITS
                ),
            ])
        ))
        with pytest.raises(MalformedProviderDataError):
            _verify(fake=fake)


class TestProviderFailures:

    pytestmark = pytest.mark.usefixtures("config_env")

    def test_json_rpc_error_object_is_provider_error(self):
        fake = FakeRpc(
            errors={
                "eth_chainId": {"code": -32000, "message": "limit"}
            }
        )
        with pytest.raises(ProviderError) as excinfo:
            _verify(fake=fake)
        assert excinfo.value.code == "provider_error"
        assert "limit" in str(excinfo.value)

    def test_transport_exception_is_wrapped(self):
        def boom(_payload):
            raise OSError("socket closed")

        client = JsonRpcClient(RPC_URL, transport=boom)
        with pytest.raises(ProviderError) as excinfo:
            verify_deposit_transaction(
                TX_HASH, AMOUNT_UNITS, client=client
            )
        assert excinfo.value.code == "provider_error"

    def test_non_object_response_is_malformed(self):
        client = JsonRpcClient(RPC_URL, transport=lambda _p: [])
        with pytest.raises(MalformedProviderDataError):
            verify_deposit_transaction(
                TX_HASH, AMOUNT_UNITS, client=client
            )

    def test_response_without_result_or_error_is_malformed(self):
        client = JsonRpcClient(RPC_URL, transport=lambda _p: {})
        with pytest.raises(MalformedProviderDataError):
            verify_deposit_transaction(
                TX_HASH, AMOUNT_UNITS, client=client
            )

    def test_default_transport_wraps_network_failure(
        self, monkeypatch
    ):
        def urlopen(*_args, **_kwargs):
            raise urllib.error.URLError("name or service not known")

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        client = JsonRpcClient(RPC_URL)
        with pytest.raises(ProviderError) as excinfo:
            client.call("eth_chainId", [])
        # the endpoint (possibly carrying an API key) never leaks
        assert RPC_URL not in str(excinfo.value)
        assert "provider request failed" == str(excinfo.value)

    def test_default_transport_wraps_non_json_body(self, monkeypatch):
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self):
                return b"<html>gateway error</html>"

        monkeypatch.setattr(
            urllib.request, "urlopen", lambda *_a, **_k: _Response()
        )
        client = JsonRpcClient(RPC_URL)
        with pytest.raises(ProviderError):
            client.call("eth_chainId", [])


# ════════════════════════════════════════════════════════════════════
# G. Not found / invalid client locator
# ════════════════════════════════════════════════════════════════════


class TestNotFoundAndLocator:

    pytestmark = pytest.mark.usefixtures("config_env")

    def test_unknown_transaction_rejected(self):
        fake = FakeRpc(_results(eth_getTransactionByHash=None))
        with pytest.raises(TransactionNotFoundError) as excinfo:
            _verify(fake=fake)
        assert excinfo.value.code == "transaction_not_found"

    def test_missing_receipt_rejected(self):
        fake = FakeRpc(_results(eth_getTransactionReceipt=None))
        with pytest.raises(TransactionNotFoundError) as excinfo:
            _verify(fake=fake)
        assert excinfo.value.code == "transaction_not_found"

    @pytest.mark.parametrize(
        "bad_hash",
        [
            "",
            "deadbeef",
            "0x" + "ab" * 31,       # too short
            "0x" + "ab" * 33,       # too long
            "0x" + "zz" * 32,       # not hex
            TX_HASH + "ab",         # too long
            None,
            12345,
        ],
    )
    def test_invalid_tx_hash_rejected_before_provider(
        self, bad_hash
    ):
        fake = FakeRpc()
        with pytest.raises(InvalidInputError) as excinfo:
            _verify(tx=bad_hash, fake=fake)
        assert excinfo.value.code == "invalid_input"
        assert fake.calls == []


# ════════════════════════════════════════════════════════════════════
# H. Contract regression
# ════════════════════════════════════════════════════════════════════


class TestContractRegression:

    pytestmark = pytest.mark.usefixtures("config_env")

    def test_error_codes_are_deterministic(self):
        cases = [
            (AdapterConfigError(), "adapter_config_error"),
            (InvalidInputError(), "invalid_input"),
            (ProviderError(), "provider_error"),
            (MalformedProviderDataError(), "malformed_provider_data"),
            (ChainMismatchError(1), "chain_mismatch"),
            (TokenMismatchError(), "token_mismatch"),
            (DestinationMismatchError(), "destination_mismatch"),
            (
                AmountMismatchError(1, 2),
                "amount_mismatch",
            ),
            (TransactionNotFoundError(), "transaction_not_found"),
            (TransactionFailedError(), "transaction_failed"),
            (
                UnfinalizedTransactionError(1, 12),
                "unfinalized_transaction",
            ),
        ]
        for exc, code in cases:
            assert exc.code == code
            assert isinstance(
                exc, adapter.BscDepositAdapterError
            )

    def test_verify_and_credit_signature_untouched(self):
        """The MT-ADMIN-29 financial contract is NOT modified."""
        sig = inspect.signature(deposit_verification.verify_and_credit)
        assert list(sig.parameters) == [
            "request_id",
            "amount_units",
            "external_tx_id",
            "facts",
            "now",
            "db_path",
        ]
        assert (
            sig.parameters["amount_units"].kind
            is inspect.Parameter.KEYWORD_ONLY
        )
        assert (
            sig.parameters["external_tx_id"].kind
            is inspect.Parameter.KEYWORD_ONLY
        )
        assert sig.parameters["facts"].kind is (
            inspect.Parameter.KEYWORD_ONLY
        )

    def test_credit_boundary_does_not_import_the_adapter(self):
        src = _read(deposit_verification.__file__)
        assert "bsc_deposit_adapter" not in src
        assert "import bsc" not in src

    def test_adapter_adds_no_public_route(self):
        src = _read(adapter.__file__)
        assert "@app.route" not in src
        assert "init_data" not in src
        assert "initData" not in src

    def test_facts_shape_matches_verify_and_credit(self):
        """The produced facts are accepted VERBATIM by the boundary
        validators (pure, no DB): normalize + amount + facts."""
        facts, _ = _verify()
        tx_id = deposit_verification.normalize_external_tx_id(
            facts.external_tx_id
        )
        assert tx_id == TX_HASH
        # verify_and_credit's own validators accept the facts as-is
        result = deposit_verification.verify_and_credit
        assert inspect.signature(result).parameters["facts"]
        validated = facts.verification_facts()
        assert validated["source"].strip() == VERIFICATION_SOURCE
        assert isinstance(facts.amount_units, int)
        assert not isinstance(facts.amount_units, bool)
        assert facts.amount_units > 0
