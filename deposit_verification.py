"""
Deposit Verification Service (MT-ADMIN-29)
==========================================

The AUTHORITATIVE deposit verification boundary: the single trusted
domain operation a future verifier (manual ops, chain adapter, provider
adapter) calls to verify a pending deposit request and credit the
internal USDT wallet.

What this module is:

- A DETERMINISTIC domain boundary.  The caller supplies authoritative
  verification facts (verified amount, external transaction id, and the
  named source/evidence of verification).  This service NEVER queries a
  blockchain, exchange or any external provider — it only enforces the
  contract and commits the result atomically.
- An INTERNAL trusted boundary.  It is deliberately NOT routed through
  any Mini App / public HTTP endpoint, never accepts Telegram user
  identity as verification authority, and never reads initData.  The
  future adapter is responsible for obtaining the facts.
- ONE atomic transaction: re-read the request under ``BEGIN
  IMMEDIATE`` → verify pending state → verify every supplied fact →
  ``wallet.credit_units`` → one ledger credit
  (``reference_type='deposit'``, ``reference_id=request_id``) → persist
  ``external_tx_id`` → CAS ``pending → credited`` → commit.  Any
  failure rolls everything back: no partial credit, no orphan ledger
  entry, no state transition.

What this module NEVER does:

- Never credits from the user-facing ``POST /api/deposit`` flow — a
  user request alone is an intent, never proof of payment.
- Never fabricates a transaction id, a confirmation or a conversion:
  amounts are exact integer USDT atomic units; the verified amount
  must equal the persisted ``amount_units`` exactly (no float, no
  Decimal-to-float, no EGP, no rate, no rounding).
- Never touches withdrawals, payment-method semantics or the user Mini
  App.

IDEMPOTENCY (safe against repeated verifier calls):

- same request already credited with the SAME normalized
  ``external_tx_id`` → deterministic already-credited/idempotent
  RESULT (no second wallet credit, no second ledger entry)
- same request already credited with a DIFFERENT
  ``external_tx_id`` → ``DepositAlreadyCreditedError`` (a
  ``DepositConflictError``)
- ``external_tx_id`` already owned by ANOTHER deposit request →
  ``ExternalTxIdAlreadyUsedError``, zero mutation (backed by the
  partial ``ux_deposit_requests_tx`` UNIQUE index)
- repeated rejection / pending-state loss → ``DepositNotPendingError``
- plus the ledger's own ``UNIQUE(reference_type, reference_id,
  entry_type)`` constraint and deterministic idempotency key
  ``deposit:<request_id>`` as the final backstop — existing
  constraints are used, never weakened.

ERRORS: deterministic domain errors, each exposing a stable ``code``
(``deposit_not_found``, ``deposit_not_pending``,
``deposit_amount_mismatch``, ``invalid_external_tx_id``,
``external_tx_id_already_used``, ``deposit_already_credited``,
``deposit_conflict``, ``invalid_verification_facts``).  Nothing here is
an HTTP response: no tracebacks or database internals are formatted
for users, and no destination or secret is ever logged (audit lines
carry request id / user id / units / result only).

Conventions: pure validation runs BEFORE the transaction; all reads
and writes share the ONE transaction connection (no nested
transaction, no second connection, no manual compensation — the
transaction owns the outcome).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

import db
import deposit_store
import ledger
import wallet

logger = logging.getLogger(__name__)

# Signed SQLite INTEGER bound — amounts must fit, never REAL.
_SQLITE_INT64_MAX = 9_223_372_036_854_775_807

# Deterministic bounds for the supplied facts.
_MAX_TX_ID_LENGTH = 256
_MAX_SOURCE_LENGTH = 128

# Ledger idempotency key for a deposit credit — deterministic, one per
# request, so a repeated credit can never record a second entry.
_LEDGER_KEY_PREFIX = "deposit:"

REFERENCE_TYPE = "deposit"


# ── Errors (deterministic domain codes) ──────────────────────────────


class DepositVerificationError(Exception):
    """Base class for deposit verification failures."""

    code = "deposit_verification_error"


class DepositNotFoundError(DepositVerificationError):
    """No deposit request matches the supplied id."""

    code = "deposit_not_found"


class DepositNotPendingError(DepositVerificationError):
    """The request is no longer pending (e.g. rejected)."""

    code = "deposit_not_pending"


class DepositAmountMismatchError(DepositVerificationError):
    """The verified amount does not equal the persisted amount."""

    code = "deposit_amount_mismatch"

    def __init__(self, verified_units: int, persisted_units: int) -> None:
        super().__init__(
            f"verified amount {verified_units} does not match the "
            f"persisted deposit amount {persisted_units}"
        )
        self.verified_units = verified_units
        self.persisted_units = persisted_units


class InvalidExternalTxIdError(DepositVerificationError):
    """The supplied external transaction id is unusable."""

    code = "invalid_external_tx_id"


class ExternalTxIdAlreadyUsedError(DepositVerificationError):
    """Another deposit request already owns this external tx id."""

    code = "external_tx_id_already_used"


class DepositConflictError(DepositVerificationError):
    """Domain conflict: persisted facts clash with current state."""

    code = "deposit_conflict"


class DepositAlreadyCreditedError(DepositConflictError):
    """The request is already credited with a DIFFERENT tx id.

    Subclass of :class:`DepositConflictError` — an already-credited
    request conflicting with the new facts IS a domain conflict.
    """

    code = "deposit_already_credited"


class InvalidVerificationFactsError(DepositVerificationError):
    """The supplied verification facts fail exact validation."""

    code = "invalid_verification_facts"


# ── Result ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DepositVerificationResult:
    """Deterministic outcome of one verification call.

    ``already_credited=True`` marks the idempotent replay of an
    identical earlier verification: the request is credited exactly
    once, no matter how often the verifier retries.
    """

    request_id: str
    user_id: int
    amount_units: int
    external_tx_id: str
    status: str
    already_credited: bool


# ── Pure validation (before any transaction) ─────────────────────────


def normalize_external_tx_id(value: object) -> str:
    """Deterministic normalization of an external transaction id.

    Leading/trailing whitespace is stripped (so padded retries of the
    same id normalize identically); empty, non-string, over-long,
    control-character or internal-whitespace ids are rejected with
    :class:`InvalidExternalTxIdError`.  The id is never altered beyond
    stripping — no case folding, no fabricated values.
    """
    if not isinstance(value, str):
        raise InvalidExternalTxIdError(
            "external_tx_id must be a non-empty string"
        )
    tx_id = value.strip()
    if not tx_id:
        raise InvalidExternalTxIdError("external_tx_id must not be empty")
    if len(tx_id) > _MAX_TX_ID_LENGTH:
        raise InvalidExternalTxIdError(
            f"external_tx_id exceeds {_MAX_TX_ID_LENGTH} characters"
        )
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in tx_id):
        raise InvalidExternalTxIdError(
            "external_tx_id must not contain whitespace or control "
            "characters"
        )
    return tx_id


def _require_verified_amount(value: object) -> int:
    """Exact positive integer atomic units — no float, ever.

    Type violations and non-positive values are INVALID FACTS (rule 3
    of the contract); a valid positive int that differs from the
    persisted amount is a mismatch (rule 4, raised later against the
    persisted row).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidVerificationFactsError(
            "amount_units must be an exact int of atomic USDT units; "
            f"got {type(value).__name__}"
        )
    if value <= 0:
        raise InvalidVerificationFactsError(
            "amount_units must be positive"
        )
    if value > _SQLITE_INT64_MAX:
        raise InvalidVerificationFactsError(
            "amount_units exceeds the supported maximum"
        )
    return value


def _require_facts(facts: object) -> dict:
    """Validate the caller's authoritative verification evidence.

    The caller (an internal, already-trusted verifier) must name the
    authoritative verification ``source`` that performed the off-line
    verification — this service itself checks no chain and trusts no
    end-user identity.
    """
    if not isinstance(facts, Mapping) or not facts:
        raise InvalidVerificationFactsError(
            "verification facts must be a non-empty mapping"
        )
    source = facts.get("source")
    if not isinstance(source, str) or not source.strip():
        raise InvalidVerificationFactsError(
            "verification facts require a non-empty 'source'"
        )
    source = source.strip()
    if len(source) > _MAX_SOURCE_LENGTH:
        raise InvalidVerificationFactsError(
            f"verification source exceeds {_MAX_SOURCE_LENGTH} "
            "characters"
        )
    return {"source": source}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    """UTC wall-clock TEXT (CURRENT_TIMESTAMP shape) — exact, no tz."""
    if value.tzinfo is not None and value.utcoffset() is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S")


# ── Snapshot compatibility (contract rule 7) ─────────────────────────


def _require_method_compatible(
    connection, request: deposit_store.DepositRequest
) -> None:
    """The method's money-movement facts must still match the snapshot.

    The request snapshotted ``pm_asset`` / ``pm_network`` at creation;
    if an admin has since changed those facts, verification must not
    silently proceed against different instructions
    (``DepositConflictError``).  Operational flags (active /
    ``deposits_enabled``) are deliberately NOT part of this check:
    deactivating a method never blocks crediting funds that were
    verifiably sent under the original instructions.

    Read on the caller's transaction connection (the store has no
    borrowed-connection getter for arbitrary columns — no hidden
    second connection is opened).
    """
    row = connection.execute(
        "SELECT asset, network FROM payment_methods WHERE id = ?",
        (request.payment_method_id,),
    ).fetchone()
    if row is None:
        raise DepositConflictError(
            "the request's payment method no longer exists"
        )
    method_asset = row[0]
    method_network = row[1] or None
    if (
        method_asset != request.pm_asset
        or method_network != request.pm_network
    ):
        raise DepositConflictError(
            "the payment method's asset/network no longer match the "
            "persisted deposit snapshot"
        )


# ── The verification boundary ────────────────────────────────────────


def verify_and_credit(
    request_id: object,
    *,
    amount_units: object,
    external_tx_id: object,
    facts: object,
    now: datetime | None = None,
    db_path: str | None = None,
) -> DepositVerificationResult:
    """Verify authoritative facts and credit the pending deposit.

    ONE ``db.transaction()``: re-read the request → pending CAS →
    amount match → snapshot compatibility → external-tx exclusivity →
    ``wallet.credit_units`` → one deposit ledger credit → persist
    ``external_tx_id`` → ``pending → credited`` CAS → commit.  Nothing
    partially applies: any failure leaves wallet, ledger and the
    deposit request exactly as they were.

    This is an INTERNAL trusted boundary: it is not routed publicly,
    performs no external calls and never trusts an end user.

    Args:
        request_id: target deposit request (str id).
        amount_units: verified amount — exact positive int of USDT
            atomic units that must equal the persisted amount.
        external_tx_id: the authoritative external transaction id
            (normalized deterministically; never fabricated here).
        facts: non-empty mapping with a non-empty ``source`` naming
            the authoritative verification that was already performed.
        now: transition timestamp (defaults to current UTC time);
            stored exactly as given on success.
        db_path: database file (defaults to ``db.DB_PATH``).

    Returns:
        The credited request; ``already_credited=True`` on an
        identical replay of an already-completed verification.

    Raises:
        DepositNotFoundError: unknown/malformed request id.
        DepositNotPendingError: request is no longer pending.
        DepositAmountMismatchError: verified != persisted amount.
        InvalidExternalTxIdError / InvalidVerificationFactsError:
            unusable supplied facts (validated before any DB work).
        ExternalTxIdAlreadyUsedError: another request owns the tx id.
        DepositAlreadyCreditedError: credited with a different tx id.
        DepositConflictError: snapshot/state conflict (rolled back).
    """
    # ── Pure validation — before any transaction ────────────────
    if not isinstance(request_id, str) or not request_id:
        raise DepositNotFoundError(
            f"no deposit request matches {request_id!r}"
        )
    tx_id = normalize_external_tx_id(external_tx_id)
    verified_units = _require_verified_amount(amount_units)
    evidence = _require_facts(facts)

    at = _utc_now() if now is None else now
    at_text = _timestamp_text(at)

    with db.transaction(db_path) as conn:
        request = deposit_store.get_deposit_request(
            request_id, connection=conn
        )
        if request is None:
            raise DepositNotFoundError(
                f"no deposit request matches {request_id!r}"
            )

        # ── Idempotent replay: credited with the SAME tx id ─────
        if request.status == deposit_store.STATUS_CREDITED:
            if request.external_tx_id == tx_id:
                logger.info(
                    "Deposit verification replay: request=%s user=%s "
                    "units=%d result=idempotent",
                    request.request_id, request.user_id,
                    request.amount_units,
                )
                return DepositVerificationResult(
                    request_id=request.request_id,
                    user_id=request.user_id,
                    amount_units=request.amount_units,
                    external_tx_id=request.external_tx_id,
                    status=deposit_store.STATUS_CREDITED,
                    already_credited=True,
                )
            raise DepositAlreadyCreditedError(
                f"deposit {request_id!r} was already credited with a "
                "different external transaction"
            )

        # ── State: only PENDING may be credited ─────────────────
        if request.status != deposit_store.STATUS_PENDING:
            raise DepositNotPendingError(
                f"deposit {request_id!r} is not pending "
                f"(status {request.status!r})"
            )

        # ── Amount: exact equality with the persisted intent ────
        if verified_units != request.amount_units:
            raise DepositAmountMismatchError(
                verified_units, request.amount_units
            )

        # ── Snapshot compatibility (rule 7) ─────────────────────
        _require_method_compatible(conn, request)

        # ── One external tx may credit at most ONE request ──────
        owner = deposit_store.find_request_by_external_tx_id(
            tx_id, connection=conn
        )
        if owner is not None:
            raise ExternalTxIdAlreadyUsedError(
                f"external transaction {tx_id!r} already credited "
                f"deposit {owner!r}"
            )

        # ── ONE atomic credit ───────────────────────────────────
        # 1) wallet: available += exact persisted amount on THIS
        #    transaction's connection (row created if missing).
        wallet.ensure_wallet(request.user_id, connection=conn)
        wallet.credit_units(request.user_id, verified_units, connection=conn)

        # 2) ledger: exactly one credit entry, tied to this request.
        ledger.LedgerService(connection=conn).record_credit(
            request.user_id,
            amount_units=verified_units,
            reference_type=REFERENCE_TYPE,
            reference_id=request.request_id,
            idempotency_key=f"{_LEDGER_KEY_PREFIX}{request.request_id}",
            metadata={
                "verification_source": evidence["source"],
                "external_tx_id": tx_id,
            },
        )

        # 3) persist the tx id + CAS pending -> credited.
        transitioned = deposit_store.mark_credited(
            request.request_id, tx_id, at_text, connection=conn
        )
        if not transitioned:
            # Defensive: state changed inside the transaction window.
            raise DepositConflictError(
                f"deposit {request_id!r} could not transition to "
                "credited"
            )

        logger.info(
            "Deposit credited: request=%s user=%s units=%d "
            "result=credited",
            request.request_id, request.user_id, verified_units,
        )
        return DepositVerificationResult(
            request_id=request.request_id,
            user_id=request.user_id,
            amount_units=verified_units,
            external_tx_id=tx_id,
            status=deposit_store.STATUS_CREDITED,
            already_credited=False,
        )
