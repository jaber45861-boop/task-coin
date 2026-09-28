"""
Manual Deposit Review Service (MT-ADMIN-31)
===========================================

The trusted service behind an admin's manual decision on a submitted
deposit-proof screenshot::

    admin Telegram private chat (config.is_admin)
        ↓
    deposit_proof_admin:  ✅ اعتماد الدفع  /  ❌ رفض الإثبات
        ↓
    deposit_manual_review.approve / .reject        ← this module
        ↓ approve ONLY
    deposit_verification.verify_and_credit(...)    ← the EXISTING
      atomic credit boundary (ONE transaction: pending CAS → exact
      persisted amount → snapshot compatibility → external-tx
      exclusivity → wallet.credit_units → ONE ledger credit →
      external_tx_id → credited), with its idempotency intact
        ↓ reject
    deposit_proof_store.mark_reviewed(...)         ← evidence only;
      NO wallet, NO ledger, NO deposit status change

Admin approval semantics (the manual verification rule):

    The admin confirms, as the verified actor, that the payment was
    actually received — the system NEVER infers receipt from the
    screenshot's existence, filename, contents or any claimed
    amount/transaction id.  The screenshot is evidence for that human
    decision and can never credit anything by itself.

Hard boundaries of THIS module:

- the credited amount is ALWAYS the persisted
  ``deposit_requests.amount_units`` — never a client/admin-entered
  number, never derived from the image;
- the external id is SERVER-GENERATED and deterministic
  (``manual-review:<request_id>``): no client-supplied transaction
  id exists anywhere in this flow, and the deterministic shape makes
  every replay an idempotent no-op instead of a second credit;
- this module never writes ``wallet``/``ledger`` directly and never
  re-implements the credit transaction — there is exactly ONE
  financial path in the repository;
- approval is safe against double-click, callback replay, stale
  cards, concurrent admins, approval after an automatic BSC credit
  and approval after another admin already approved: exactly one
  financial credit may ever occur, everything else returns a
  deterministic already-processed outcome with ZERO mutation;
- rejection never credits, never rejects the deposit itself and
  never deletes the stored evidence.

Error codes: ``proof_not_found``, ``proof_state_changed``,
``manual_review_error`` (bad actor id), plus the existing store and
credit-boundary codes passed through unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import deposit_proof_store
import deposit_store
import deposit_verification
from deposit_proof_store import (
    DepositProof,
    STATUS_APPROVED,
    STATUS_REJECTED,
)

logger = logging.getLogger(__name__)

# Server-generated, deterministic external id for a manual credit —
# one per deposit request, so every approval attempt maps to the SAME
# idempotency key (replay-safe) and no client can ever supply one.
MANUAL_TX_ID_PREFIX = "manual-review:"

# Ledger evidence source naming the manual path + deciding admin.
_VERIFICATION_SOURCE_PREFIX = "manual_admin_review"


class ManualReviewError(Exception):
    """Base class for manual-review failures."""

    code = "manual_review_error"


class ProofReviewNotFoundError(ManualReviewError):
    """No proof matches the supplied id."""

    code = "proof_not_found"


class ProofReviewConflictError(ManualReviewError):
    """The proof/deposit is no longer in a reviewable state."""

    code = "proof_state_changed"


class ManualReviewerError(ManualReviewError):
    """The acting reviewer id is unusable."""

    code = "manual_review_error"


@dataclass(frozen=True)
class ManualApprovalOutcome:
    """Deterministic result of one approval attempt.

    ``credited=True`` only when THIS call performed the (first and
    only) financial credit.  ``already_processed=True`` means zero
    financial mutation happened: a replay, a stale press, or a
    deposit already credited through the automatic BSC path.
    """

    request_id: str
    proof_id: str
    deposit_status: str
    credited: bool
    already_processed: bool


def _require_reviewer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ManualReviewerError(
            "admin_id must be a positive int (enforced by the caller's "
            "config.is_admin gate)"
        )
    return value


def approve(
    proof_id: object,
    *,
    admin_id: object,
    db_path: str | None = None,
) -> ManualApprovalOutcome:
    """Approve a pending proof: the admin's manual confirmation that
    the payment was received — credited through the EXISTING trusted
    boundary only.

    Args:
        proof_id: the proof record the admin reviewed (a stale card
            whose proof was already decided/replaced is rejected
            before any credit).
        admin_id: the authorized admin (caller enforces
            ``config.is_admin``; recorded as the reviewer).

    Returns:
        ManualApprovalOutcome — ``credited`` for the single real
        credit, ``already_processed`` for every idempotent replay or
        already-credited deposit (zero mutation).

    Raises:
        ProofReviewNotFoundError: unknown/malformed proof id.
        ProofReviewConflictError: proof rejected/replaced, or the
            deposit is no longer creditable (e.g. rejected).
        deposit_verification.DepositVerificationError: the unchanged
            credit-boundary verdicts.
    """
    reviewer = _require_reviewer(admin_id)
    if not isinstance(proof_id, str) or not proof_id:
        raise ProofReviewNotFoundError("no deposit proof matches")

    proof = deposit_proof_store.get_proof(proof_id, db_path=db_path)
    if proof is None:
        raise ProofReviewNotFoundError(
            f"no deposit proof matches {proof_id!r}"
        )
    if proof.status == STATUS_REJECTED:
        raise ProofReviewConflictError(
            "the proof was rejected — no financial action is possible"
        )

    request = deposit_store.get_deposit_request(
        proof.request_id, db_path=db_path
    )
    if request is None:
        raise ProofReviewConflictError(
            "the deposit request no longer exists"
        )

    # Already approved (double-click / stale replay / concurrent
    # admin): deterministic already-processed result, ZERO mutation.
    if proof.status == STATUS_APPROVED:
        logger.info(
            "Manual deposit approval replay: request=%s proof=%s "
            "admin=%s result=already_processed",
            proof.request_id, proof_id, reviewer,
        )
        return ManualApprovalOutcome(
            request_id=proof.request_id,
            proof_id=proof_id,
            deposit_status=request.status,
            credited=False,
            already_processed=True,
        )

    # proof is pending_review from here — the ONE delegation to the
    # existing atomic credit boundary.  The amount is the PERSISTED
    # deposit amount; the external id is server-generated.
    try:
        result = deposit_verification.verify_and_credit(
            proof.request_id,
            amount_units=request.amount_units,
            external_tx_id=(
                f"{MANUAL_TX_ID_PREFIX}{proof.request_id}"
            ),
            facts={
                "source": f"{_VERIFICATION_SOURCE_PREFIX}:{reviewer}"
            },
            db_path=db_path,
        )
    except deposit_verification.DepositAlreadyCreditedError:
        # e.g. the automatic BSC adapter credited this deposit first:
        # NO second credit, only the evidence decision is recorded.
        deposit_proof_store.mark_reviewed(
            proof_id, STATUS_APPROVED, reviewer, db_path=db_path
        )
        logger.info(
            "Manual deposit approval after existing credit: "
            "request=%s proof=%s admin=%s result=already_processed",
            proof.request_id, proof_id, reviewer,
        )
        return ManualApprovalOutcome(
            request_id=proof.request_id,
            proof_id=proof_id,
            deposit_status=deposit_store.STATUS_CREDITED,
            credited=False,
            already_processed=True,
        )
    except deposit_verification.DepositNotPendingError as exc:
        # Rejected deposit: no mutation of any kind (the admin may
        # still reject the evidence record itself).
        raise ProofReviewConflictError(
            "the deposit request is no longer pending"
        ) from exc

    # Evidence decision AFTER the committed credit (convergent: a
    # failure here leaves the proof pending and a retry lands on the
    # already-processed path — never a second credit).  A concurrent
    # decision makes the CAS return None — logged, never fatal.
    updated = deposit_proof_store.mark_reviewed(
        proof_id, STATUS_APPROVED, reviewer, db_path=db_path
    )
    if updated is None:
        logger.warning(
            "Deposit proof state changed during approval: "
            "request=%s proof=%s admin=%s",
            proof.request_id, proof_id, reviewer,
        )

    credited = not result.already_credited
    logger.info(
        "Manual deposit approval: request=%s proof=%s user=%s "
        "units=%d admin=%s result=%s",
        proof.request_id, proof_id, result.user_id,
        result.amount_units, reviewer,
        "credited" if credited else "already_processed",
    )
    return ManualApprovalOutcome(
        request_id=proof.request_id,
        proof_id=proof_id,
        deposit_status=result.status,
        credited=credited,
        already_processed=not credited,
    )


def reject(
    proof_id: object,
    *,
    admin_id: object,
    review_note: object | None = None,
    db_path: str | None = None,
) -> DepositProof:
    """Reject a pending proof — an EVIDENCE decision only.

    Records reviewer + review time (+ optional safe note).  Never
    credits, never creates a ledger entry, never changes the
    deposit's financial status (it stays ``pending`` and the user
    may submit a new proof).  The stored evidence is never deleted.

    Raises:
        ProofReviewNotFoundError: unknown proof.
        ProofReviewConflictError: the proof was already decided.
        ManualReviewerError: unusable admin id.
    """
    reviewer = _require_reviewer(admin_id)
    if not isinstance(proof_id, str) or not proof_id:
        raise ProofReviewNotFoundError("no deposit proof matches")

    updated = deposit_proof_store.mark_reviewed(
        proof_id,
        STATUS_REJECTED,
        reviewer,
        review_note=review_note,
        db_path=db_path,
    )
    if updated is None:
        current = deposit_proof_store.get_proof(
            proof_id, db_path=db_path
        )
        if current is None:
            raise ProofReviewNotFoundError(
                f"no deposit proof matches {proof_id!r}"
            )
        raise ProofReviewConflictError(
            "the proof has already been reviewed"
        )
    logger.info(
        "Deposit proof rejected: proof=%s admin=%s result=rejected",
        proof_id, reviewer,
    )
    return updated
