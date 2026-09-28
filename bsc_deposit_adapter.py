"""
BSC USDT Deposit Verification Adapter (MT-ADMIN-30)
===================================================

The READ-ONLY chain adapter anticipated by the MT-ADMIN-29 deposit
architecture: it obtains blockchain facts from an AUTHORITATIVE,
configured BSC JSON-RPC provider and produces the exact facts
``deposit_verification.verify_and_credit()`` requires — ``amount_units``
(exact integer USDT atomic units), ``external_tx_id`` and the
``facts={"source": ...}`` evidence mapping — nothing more.

Trust model (never trust the client):
--------------------------------------
- The client may contribute only a transaction HASH as a LOCATOR to
  look the transaction up on the provider.  Every proof-bearing fact —
  chain id, token contract, recipient, amount, success and finality —
  is read from the configured provider, and the receipt/transaction
  hashes returned by the provider are cross-checked against the
  requested hash so a confused provider cannot swap transactions.
- A client-supplied amount is never accepted as proof: the expected
  amount is an argument the CALLER must take from the persisted
  deposit intent.  :func:`verify_and_credit_deposit` does exactly that
  — it reads ``amount_units`` from the stored request and passes the
  provider-observed amount to the unchanged MT-ADMIN-29 credit
  boundary.
- The adapter itself never writes to the database, never credits a
  wallet and never sends a transaction.  The single atomic credit
  stays inside ``deposit_verification.verify_and_credit()`` (its
  financial contract is NOT modified — the adapter is a caller).

Configuration — environment only, no source constants, no fallbacks:
---------------------------------------------------------------------
Follows the existing repository convention for provider configuration
(``config.get_mini_app_url`` validation, ``youtube_oauth`` env
credentials, ``miniapp_auth`` numeric env policy):

- ``BSC_RPC_URL``                  absolute http(s) JSON-RPC endpoint.
                                   An endpoint carrying an API key is
                                   a credential: it lives only in the
                                   environment and is never echoed
                                   into errors, logs or reprs.
- ``BSC_USDT_TOKEN_CONTRACT``      the configured USDT BEP-20 token
                                   contract (``0x`` + 40 hex).
- ``BSC_DEPOSIT_ADDRESS``          the configured platform deposit
                                   address (``0x`` + 40 hex).
- ``BSC_REQUIRED_CONFIRMATIONS``   optional finality threshold,
                                   integer 1..1000 (default 12) — a
                                   numeric safety policy, same shape
                                   as ``miniapp_auth`` max-age.

A missing or malformed setting fails CLOSED with
``AdapterConfigError`` (code ``adapter_config_error``); there are no
silent defaults for the endpoint or the addresses.  Protocol facts
that are not deployment configuration are documented constants: chain
id 56 (BNB Smart Chain), the ERC-20 ``Transfer`` topic and the
``decimals()`` selector.

Validation performed, in order (each failure carries a stable error
``code``):
    chain id 56 → tx exists → tx targets the configured token
    contract → receipt exists → receipt matches the tx → status
    success → exactly one ERC-20 Transfer of the configured token to
    the configured deposit address → exact amount (token decimals read
    from the chain; conversion to 8-dp atomic units is EXACT — a
    non-representable or different amount is rejected, never rounded)
    → finality (chain head − receipt block + 1 ≥ configured
    confirmations).

Run:
    python3 -m pytest test_bsc_deposit_adapter.py -v
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import deposit_store
import deposit_verification
from deposit_verification import DepositNotFoundError

# ── Protocol constants (NOT deployment configuration) ────────────────

# BNB Smart Chain mainnet — the chain this adapter verifies.
CHAIN_ID = 56

# keccak256("Transfer(address,address,uint256)") — the ERC-20 event
# every BEP-20 token transfer emits.
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

# bytes4(keccak256("decimals()")) — read from the chain, never assumed.
DECIMALS_CALL_DATA = "0x313ce567"

# USDT atomic units are 8-dp (wallet.USDT_SCALE); on-chain token
# decimals are converted EXACTLY, never rounded.
_INTERNAL_DECIMALS = 8

# Signed SQLite INTEGER bound — amounts must fit, never REAL.
_SQLITE_INT64_MAX = 9_223_372_036_854_775_807

# Sanity bound for an on-chain ``decimals()`` return value.
_MAX_TOKEN_DECIMALS = 36

# Evidence name recorded in the deposit ledger metadata.
VERIFICATION_SOURCE = "bsc_usdt_rpc_adapter"

# ── Environment variable names (the ONLY configuration channel) ──────

RPC_URL_ENV = "BSC_RPC_URL"
TOKEN_CONTRACT_ENV = "BSC_USDT_TOKEN_CONTRACT"
DEPOSIT_ADDRESS_ENV = "BSC_DEPOSIT_ADDRESS"
REQUIRED_CONFIRMATIONS_ENV = "BSC_REQUIRED_CONFIRMATIONS"

# Numeric finality policy default — same env-with-default shape as
# miniapp_auth.MINIAPP_AUTH_MAX_AGE.  Not a secret, not deploy-specific.
DEFAULT_REQUIRED_CONFIRMATIONS = 12
_MIN_REQUIRED_CONFIRMATIONS = 1
_MAX_REQUIRED_CONFIRMATIONS = 1000

DEFAULT_RPC_TIMEOUT_SECONDS = 10.0

# Accept case-insensitive hex (providers may return checksum-cased
# addresses / mixed-case hashes; everything is normalized to lower).
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}", re.IGNORECASE)
_TX_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}", re.IGNORECASE)
_HEX_QUANTITY_RE = re.compile(r"0x[0-9a-fA-F]+", re.IGNORECASE)
_TOPIC_RE = re.compile(r"0x[0-9a-fA-F]{64}", re.IGNORECASE)


# ── Errors (deterministic domain codes) ──────────────────────────────


class BscDepositAdapterError(Exception):
    """Base class for adapter failures; ``code`` is stable and safe
    to surface to internal callers (never contains the endpoint)."""

    code = "bsc_deposit_adapter_error"


class AdapterConfigError(BscDepositAdapterError):
    """Required provider configuration is missing or malformed."""

    code = "adapter_config_error"


class InvalidInputError(BscDepositAdapterError):
    """The caller-supplied locator/expectation is unusable."""

    code = "invalid_input"


class ProviderError(BscDepositAdapterError):
    """The configured provider could not be reached or answered at
    the protocol level (transport/HTTP/JSON-RPC error)."""

    code = "provider_error"


class MalformedProviderDataError(BscDepositAdapterError):
    """The provider answered, but the data is structurally unusable."""

    code = "malformed_provider_data"


class ChainMismatchError(BscDepositAdapterError):
    """The provider serves a chain other than BNB Smart Chain."""

    code = "chain_mismatch"

    def __init__(self, observed_chain_id: int) -> None:
        super().__init__(
            f"expected chain {CHAIN_ID}, provider reports chain "
            f"{observed_chain_id}"
        )
        self.observed_chain_id = observed_chain_id


class TokenMismatchError(BscDepositAdapterError):
    """The transaction does not transfer the configured USDT token."""

    code = "token_mismatch"


class DestinationMismatchError(BscDepositAdapterError):
    """The configured token was not transferred to the configured
    deposit address."""

    code = "destination_mismatch"


class AmountMismatchError(BscDepositAdapterError):
    """The on-chain amount is not the expected exact amount."""

    code = "amount_mismatch"

    def __init__(
        self,
        expected_units: int,
        observed_units: int | None,
        reason: str | None = None,
    ) -> None:
        if reason is None:
            reason = (
                f"on-chain amount {observed_units} does not equal the "
                f"expected deposit amount {expected_units} atomic "
                "USDT units"
            )
        super().__init__(reason)
        self.expected_units = expected_units
        self.observed_units = observed_units


class TransactionNotFoundError(BscDepositAdapterError):
    """The provider does not know the requested transaction."""

    code = "transaction_not_found"


class TransactionFailedError(BscDepositAdapterError):
    """The transaction was mined but reverted (status != 1)."""

    code = "transaction_failed"


class UnfinalizedTransactionError(BscDepositAdapterError):
    """The transaction has not reached the configured finality."""

    code = "unfinalized_transaction"

    def __init__(self, confirmations: int, required: int) -> None:
        super().__init__(
            f"transaction has {confirmations} confirmation(s); "
            f"{required} required"
        )
        self.confirmations = confirmations
        self.required = required


# ── Configuration (environment only; fails closed) ───────────────────


@dataclass(frozen=True)
class BscAdapterConfig:
    """Validated provider configuration.

    ``rpc_url`` is excluded from ``repr`` — an endpoint may embed an
    API key and is treated as a credential (same discipline as
    ``payment_method_store.PaymentMethod.destination``).
    """

    rpc_url: str = field(repr=False)
    token_contract: str
    deposit_address: str
    required_confirmations: int


def _require_address(raw: object, name: str) -> str:
    if not isinstance(raw, str) or not _ADDRESS_RE.fullmatch(raw.strip()):
        raise AdapterConfigError(
            f"{name} must be a 0x-prefixed 20-byte hex address"
        )
    return raw.strip().lower()


def load_bsc_adapter_config(
    environ: Mapping[str, str] | None = None,
) -> BscAdapterConfig:
    """Read and validate the adapter configuration from the environment.

    No endpoint, token contract, deposit address or credential is ever
    read from source; nothing falls back to a built-in value.  The raw
    endpoint is never echoed back in errors (it may carry a key).
    """
    env = os.environ if environ is None else environ

    rpc_url = (env.get(RPC_URL_ENV) or "").strip()
    if not rpc_url:
        raise AdapterConfigError(
            f"{RPC_URL_ENV} environment variable is not set — the "
            "adapter requires an authoritative JSON-RPC endpoint"
        )
    parsed = urlparse(rpc_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise AdapterConfigError(
            f"{RPC_URL_ENV} must be an absolute http(s) URL with a host"
        )

    token_raw = (env.get(TOKEN_CONTRACT_ENV) or "").strip()
    if not token_raw:
        raise AdapterConfigError(
            f"{TOKEN_CONTRACT_ENV} environment variable is not set"
        )
    deposit_raw = (env.get(DEPOSIT_ADDRESS_ENV) or "").strip()
    if not deposit_raw:
        raise AdapterConfigError(
            f"{DEPOSIT_ADDRESS_ENV} environment variable is not set"
        )

    confirmations_raw = (
        env.get(REQUIRED_CONFIRMATIONS_ENV) or ""
    ).strip()
    if confirmations_raw:
        if not re.fullmatch(r"[0-9]+", confirmations_raw):
            raise AdapterConfigError(
                f"{REQUIRED_CONFIRMATIONS_ENV} must be an integer "
                "between "
                f"{_MIN_REQUIRED_CONFIRMATIONS} and "
                f"{_MAX_REQUIRED_CONFIRMATIONS}"
            )
        confirmations = int(confirmations_raw)
        if not (
            _MIN_REQUIRED_CONFIRMATIONS
            <= confirmations
            <= _MAX_REQUIRED_CONFIRMATIONS
        ):
            raise AdapterConfigError(
                f"{REQUIRED_CONFIRMATIONS_ENV} must be between "
                f"{_MIN_REQUIRED_CONFIRMATIONS} and "
                f"{_MAX_REQUIRED_CONFIRMATIONS}"
            )
    else:
        confirmations = DEFAULT_REQUIRED_CONFIRMATIONS

    return BscAdapterConfig(
        rpc_url=rpc_url,
        token_contract=_require_address(token_raw, TOKEN_CONTRACT_ENV),
        deposit_address=_require_address(
            deposit_raw, DEPOSIT_ADDRESS_ENV
        ),
        required_confirmations=confirmations,
    )


# ── Provider transport (read-only JSON-RPC over HTTP) ────────────────

Transport = Callable[[dict], dict]


def _default_transport(endpoint: str, timeout: float) -> Transport:
    """Stdlib HTTP POST transport (same stack as ``youtube_oauth``)."""

    def transport(payload: dict) -> dict:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                body = resp.read()
        except (urllib.error.URLError, OSError) as exc:
            # Never include the endpoint: it may carry an API key.
            raise ProviderError("provider request failed") from exc
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError(
                "provider returned a non-JSON response"
            ) from exc
        if not isinstance(decoded, dict):
            raise MalformedProviderDataError(
                "provider response is not a JSON object"
            )
        return decoded

    return transport


class JsonRpcClient:
    """Minimal read-only JSON-RPC client with an injectable transport.

    The transport is replaceable so tests can serve fully mocked
    provider responses; production uses the stdlib HTTP transport.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        transport: Transport | None = None,
        timeout: float = DEFAULT_RPC_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint:
            raise AdapterConfigError("provider endpoint must be a string")
        self._endpoint = endpoint
        self._transport = (
            transport
            if transport is not None
            else _default_transport(endpoint, timeout)
        )

    def call(self, method: str, params: list | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": list(params or []),
        }
        try:
            response = self._transport(payload)
        except BscDepositAdapterError:
            raise
        except Exception as exc:  # transport bug / socket failure
            raise ProviderError("provider transport failed") from exc
        if not isinstance(response, dict):
            raise MalformedProviderDataError(
                "provider response is not a JSON object"
            )
        error = response.get("error")
        if error is not None:
            if isinstance(error, dict):
                raise ProviderError(
                    f"provider RPC error {error.get('code')}: "
                    f"{error.get('message')}"
                )
            raise ProviderError("provider RPC error")
        if "result" not in response:
            raise MalformedProviderDataError(
                "provider response has neither result nor error"
            )
        return response["result"]


# ── Strict parsing of provider data ──────────────────────────────────


def _parse_quantity(value: object, name: str) -> int:
    """Hex (or int) JSON-RPC quantity → non-negative int."""
    if isinstance(value, bool):
        raise MalformedProviderDataError(f"{name}: not a hex quantity")
    if isinstance(value, int):
        if value < 0:
            raise MalformedProviderDataError(f"{name}: negative quantity")
        return value
    if isinstance(value, str) and _HEX_QUANTITY_RE.fullmatch(value):
        return int(value, 16)
    raise MalformedProviderDataError(
        f"{name}: expected a 0x-prefixed hex quantity"
    )


def _address_or_none(value: object) -> str | None:
    if isinstance(value, str) and _ADDRESS_RE.fullmatch(value):
        return value.lower()
    return None


def _topic_address(topic: str) -> str | None:
    """Left-padded 32-byte address topic → lowercase address."""
    if not _TOPIC_RE.fullmatch(topic):
        return None
    body = topic[2:]
    if body[:24] != "0" * 24:
        return None
    return "0x" + body[24:].lower()


def _require_tx_hash(value: object) -> str:
    if (
        not isinstance(value, str)
        or not _TX_HASH_RE.fullmatch(value.strip())
    ):
        raise InvalidInputError(
            "tx_hash must be a 0x-prefixed 32-byte hex transaction hash"
        )
    return value.strip().lower()


def _require_expected_amount(value: object) -> int:
    """Exact positive int of USDT atomic units — no float, ever.

    The caller must source this from the PERSISTED deposit intent;
    a float/bool (client-shaped input) is rejected outright.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInputError(
            "expected_amount_units must be an exact int of atomic "
            f"USDT units; got {type(value).__name__}"
        )
    if value <= 0:
        raise InvalidInputError("expected_amount_units must be positive")
    if value > _SQLITE_INT64_MAX:
        raise InvalidInputError(
            "expected_amount_units exceeds the supported maximum"
        )
    return value


def _require_matching_hash(
    value: object, expected: str, name: str
) -> None:
    """A provider that answers with a different hash is unusable."""
    if not isinstance(value, str) or value.lower() != expected:
        raise MalformedProviderDataError(
            f"{name} does not match the requested transaction"
        )


# ── Result ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChainDepositFacts:
    """Provider-observed facts of ONE verified deposit transaction.

    Exactly the inputs ``deposit_verification.verify_and_credit()``
    requires: ``amount_units``, ``external_tx_id`` and the
    ``source`` evidence mapping (via :meth:`verification_facts`).
    """

    tx_hash: str
    chain_id: int
    token_contract: str
    deposit_address: str
    token_decimals: int
    amount_units: int
    block_number: int
    confirmations: int
    source: str = VERIFICATION_SOURCE

    @property
    def external_tx_id(self) -> str:
        """The authoritative external transaction id (normalized)."""
        return self.tx_hash

    def verification_facts(self) -> dict:
        """The exact ``facts`` mapping for ``verify_and_credit``."""
        return {"source": self.source}


# ── The verification itself ──────────────────────────────────────────


def _token_decimals(client: JsonRpcClient, token_contract: str) -> int:
    raw = client.call(
        "eth_call", [{"to": token_contract, "data": DECIMALS_CALL_DATA},
                     "latest"]
    )
    decimals = _parse_quantity(raw, "token decimals()")
    if decimals > _MAX_TOKEN_DECIMALS:
        raise MalformedProviderDataError(
            "token decimals() is outside the sane range"
        )
    return decimals


def _raw_to_atomic_units(
    raw_units: int, decimals: int, expected_units: int
) -> int:
    """Exact on-chain units → 8-dp USDT atomic units (no rounding).

    A raw value that does not land exactly on the 8-dp grid cannot
    equal the expected amount, so it is rejected — never rounded.
    """
    if decimals >= _INTERNAL_DECIMALS:
        scale = 10 ** (decimals - _INTERNAL_DECIMALS)
        if raw_units % scale:
            raise AmountMismatchError(
                expected_units,
                None,
                reason=(
                    "on-chain amount is not exactly representable in "
                    "8-decimal USDT atomic units"
                ),
            )
        return raw_units // scale
    return raw_units * 10 ** (_INTERNAL_DECIMALS - decimals)


def _find_transfer_log(
    logs: object,
    *,
    token_contract: str,
    deposit_address: str,
) -> dict:
    """Locate the ONE ERC-20 Transfer of the configured token to the
    configured deposit address, classifying every near-miss.

    Raises:
        TokenMismatchError: a transfer to the deposit address exists
            but from a different token contract (or no transfer of the
            configured token exists at all).
        DestinationMismatchError: the configured token was transferred
            — never reaching the configured deposit address.
        MalformedProviderDataError: structurally unusable logs or an
            ambiguous multiple matching transfer.
    """
    if not isinstance(logs, list):
        raise MalformedProviderDataError("receipt logs must be a list")

    matched: dict | None = None
    token_transfers_elsewhere = False
    transfer_to_deposit_other_token = False

    for entry in logs:
        if not isinstance(entry, dict):
            raise MalformedProviderDataError(
                "receipt log entry is not an object"
            )
        address = _address_or_none(entry.get("address"))
        if address is None:
            raise MalformedProviderDataError(
                "receipt log address is not an address"
            )
        topics = entry.get("topics")
        if not isinstance(topics, list) or not all(
            isinstance(topic, str) for topic in topics
        ):
            raise MalformedProviderDataError(
                "receipt log topics must be a list of strings"
            )
        if not topics:
            # Topic-less (anonymous) log — never an ERC-20 Transfer.
            continue
        if not _TOPIC_RE.fullmatch(topics[0]):
            raise MalformedProviderDataError(
                "receipt log topic is not a 32-byte hex value"
            )
        is_transfer = topics[0].lower() == TRANSFER_TOPIC

        if address == token_contract:
            if not is_transfer:
                continue
            if len(topics) != 3:
                raise MalformedProviderDataError(
                    "ERC-20 Transfer log must carry exactly 3 topics"
                )
            recipient = _topic_address(topics[2])
            if recipient is None:
                raise MalformedProviderDataError(
                    "ERC-20 Transfer recipient topic is malformed"
                )
            if recipient == deposit_address:
                if matched is not None:
                    raise MalformedProviderDataError(
                        "transaction contains multiple matching "
                        "transfers to the deposit address"
                    )
                matched = entry
            else:
                token_transfers_elsewhere = True
        elif is_transfer and len(topics) == 3:
            if _topic_address(topics[2]) == deposit_address:
                transfer_to_deposit_other_token = True

    if matched is None:
        if transfer_to_deposit_other_token:
            raise TokenMismatchError(
                "the transfer to the deposit address is not made with "
                "the configured USDT token contract"
            )
        if token_transfers_elsewhere:
            raise DestinationMismatchError(
                "the configured USDT token was not transferred to the "
                "configured deposit address"
            )
        raise TokenMismatchError(
            "the transaction contains no ERC-20 Transfer of the "
            "configured USDT token contract"
        )
    return matched


def verify_deposit_transaction(
    tx_hash: object,
    expected_amount_units: object,
    *,
    config: BscAdapterConfig | None = None,
    client: JsonRpcClient | None = None,
) -> ChainDepositFacts:
    """Verify ONE transaction against the configured provider.

    Read-only: facts are obtained from the authoritative configured
    provider; the client's tx hash is only a locator and the expected
    amount must come from the persisted deposit intent.

    Args:
        tx_hash: client-supplied locator (validated for shape, then
            re-confirmed against the provider's own hash fields).
        expected_amount_units: the persisted deposit amount — exact
            positive int of 8-dp USDT atomic units.
        config: validated configuration (defaults to
            ``load_bsc_adapter_config()`` from the environment).
        client: JSON-RPC client (defaults to one built from
            ``config.rpc_url``).

    Returns:
        ChainDepositFacts — the exact facts for
        ``deposit_verification.verify_and_credit()``.

    Raises:
        AdapterConfigError / InvalidInputError: unusable caller input
            or environment (raised before any provider call).
        ProviderError: transport or JSON-RPC failure.
        MalformedProviderDataError: structurally unusable provider
            data (or a hash cross-check failure).
        ChainMismatchError / TokenMismatchError /
        DestinationMismatchError / AmountMismatchError /
        TransactionNotFoundError / TransactionFailedError /
        UnfinalizedTransactionError: deterministic verdicts, each with
            a stable ``code``.
    """
    cfg = config

    # Pure validation of caller input BEFORE any configuration or
    # provider work (same discipline as MT-ADMIN-29).
    tx_id = _require_tx_hash(tx_hash)
    expected_units = _require_expected_amount(expected_amount_units)

    if cfg is None:
        cfg = load_bsc_adapter_config()
    rpc = client if client is not None else JsonRpcClient(cfg.rpc_url)

    # 1. Chain: the configured provider must serve BNB Smart Chain.
    observed_chain = _parse_quantity(
        rpc.call("eth_chainId", []), "eth_chainId"
    )
    if observed_chain != CHAIN_ID:
        raise ChainMismatchError(observed_chain)

    # 2. Transaction (locator resolved on the provider, hash checked).
    tx = rpc.call("eth_getTransactionByHash", [tx_id])
    if tx is None:
        raise TransactionNotFoundError(
            f"transaction {tx_id} was not found on the configured "
            "provider"
        )
    if not isinstance(tx, dict):
        raise MalformedProviderDataError(
            "eth_getTransactionByHash result is not an object"
        )
    _require_matching_hash(tx.get("hash"), tx_id, "transaction hash")
    tx_to = tx.get("to")
    if tx_to is None:
        raise TokenMismatchError(
            "the transaction is not a transfer to the configured USDT "
            "token contract"
        )
    tx_to_address = _address_or_none(tx_to)
    if tx_to_address is None:
        raise MalformedProviderDataError(
            "transaction to-field is not an address"
        )
    if tx_to_address != cfg.token_contract:
        raise TokenMismatchError(
            "the transaction does not target the configured USDT token "
            "contract"
        )

    # 3. Receipt (exists, belongs to this tx, structured).
    receipt = rpc.call("eth_getTransactionReceipt", [tx_id])
    if receipt is None:
        raise TransactionNotFoundError(
            f"no receipt is available for transaction {tx_id} yet"
        )
    if not isinstance(receipt, dict):
        raise MalformedProviderDataError(
            "eth_getTransactionReceipt result is not an object"
        )
    _require_matching_hash(
        receipt.get("transactionHash"), tx_id, "receipt transaction hash"
    )
    if "status" not in receipt:
        raise MalformedProviderDataError("receipt has no status field")
    status = _parse_quantity(receipt["status"], "receipt.status")
    if "blockNumber" not in receipt:
        raise MalformedProviderDataError(
            "receipt has no blockNumber field"
        )
    block_number = _parse_quantity(
        receipt["blockNumber"], "receipt.blockNumber"
    )

    # 4. Success: mined and NOT reverted.
    if status != 1:
        raise TransactionFailedError(
            "the transaction is mined but not successful "
            f"(status {status})"
        )

    # 5. Token + destination: exactly one Transfer of the configured
    #    token to the configured deposit address.
    transfer_log = _find_transfer_log(
        receipt.get("logs"),
        token_contract=cfg.token_contract,
        deposit_address=cfg.deposit_address,
    )
    data = transfer_log.get("data")
    if not isinstance(data, str) or not _TX_HASH_RE.fullmatch(data):
        raise MalformedProviderDataError(
            "Transfer log data must be one 32-byte hex word"
        )
    raw_units = int(data, 16)
    decimals = _token_decimals(rpc, cfg.token_contract)
    observed_units = _raw_to_atomic_units(
        raw_units, decimals, expected_units
    )
    if observed_units != expected_units:
        raise AmountMismatchError(expected_units, observed_units)

    # 6. Finality: deep enough confirmations at the provider's head.
    head = _parse_quantity(
        rpc.call("eth_blockNumber", []), "eth_blockNumber"
    )
    if block_number > head:
        raise MalformedProviderDataError(
            "receipt block number exceeds the reported chain head"
        )
    confirmations = head - block_number + 1
    if confirmations < cfg.required_confirmations:
        raise UnfinalizedTransactionError(
            confirmations, cfg.required_confirmations
        )

    return ChainDepositFacts(
        tx_hash=tx_id,
        chain_id=CHAIN_ID,
        token_contract=cfg.token_contract,
        deposit_address=cfg.deposit_address,
        token_decimals=decimals,
        amount_units=observed_units,
        block_number=block_number,
        confirmations=confirmations,
        source=VERIFICATION_SOURCE,
    )


def verify_and_credit_deposit(
    request_id: object,
    tx_hash: object,
    *,
    config: BscAdapterConfig | None = None,
    client: JsonRpcClient | None = None,
    db_path: str | None = None,
    now: datetime | None = None,
) -> tuple[deposit_verification.DepositVerificationResult,
           ChainDepositFacts]:
    """Verify a transaction on-chain, then credit via MT-ADMIN-29.

    The expected amount is read from the PERSISTED deposit intent —
    never from the client — and the credit itself happens exclusively
    inside the unchanged ``verify_and_credit`` boundary (same atomic
    transaction, same idempotency and exclusivity rules).

    Returns:
        ``(result, facts)`` — the credit result and the provider
        facts that produced it.

    Raises:
        DepositNotFoundError: unknown deposit request.
        BscDepositAdapterError: any adapter verdict above.
        deposit_verification.DepositVerificationError: the unchanged
            credit-boundary verdicts (state, amount, exclusivity,
            conflicts).
    """
    request = deposit_store.get_deposit_request(request_id, db_path=db_path)
    if request is None:
        raise DepositNotFoundError(
            f"no deposit request matches {request_id!r}"
        )
    facts = verify_deposit_transaction(
        tx_hash,
        request.amount_units,
        config=config,
        client=client,
    )
    result = deposit_verification.verify_and_credit(
        request_id,
        amount_units=facts.amount_units,
        external_tx_id=facts.external_tx_id,
        facts=facts.verification_facts(),
        now=now,
        db_path=db_path,
    )
    return result, facts
