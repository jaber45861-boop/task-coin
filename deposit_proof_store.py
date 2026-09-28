"""
Deposit Proof Store (MT-ADMIN-31)
=================================

Persistence for the MANUAL deposit-proof evidence — the smallest
additive model that associates an admin-reviewable screenshot with a
deposit request.

What a proof IS: an EVIDENCE record — request id, the submitting
Telegram user, the server-side storage reference for the image,
upload timestamp and the review state/decision metadata.

What a proof is NOT: a financial fact.

- Creating a proof row NEVER touches ``wallets``, ``ledger`` or
  ``deposit_requests.status`` — a deposit stays ``pending`` exactly
  as before (the financial lifecycle remains pending → credited /
  pending → rejected, nothing else exists).
- Rejecting a proof NEVER credits, never rejects the deposit and
  never deletes the stored evidence — re-submission after a review
  is allowed.
- Approving never happens HERE: the financial credit belongs to the
  trusted approval path (``deposit_manual_review`` → the existing
  atomic credit boundary), never to this store.

Review lifecycle (separate from the financial lifecycle)::

    pending_review → approved
    pending_review → rejected        (evidence decision only)

Schema guarantees (db.init_db, MT-ADMIN-31 additive migration):

- ONE active proof per deposit request — the partial UNIQUE index
  ``ux_deposit_proofs_pending`` allows a single ``pending_review``
  row per request, so a repeat upload is a deterministic business
  error (``proof_pending_review``) instead of ambiguous evidence.
  Reviewed rows (approved/rejected) are never overwritten or
  deleted: rejected evidence stays on file.
- ``storage_key`` is a server-generated relative filename; client
  filenames are never stored.

Conventions: pure input validation runs BEFORE the transaction; the
ONE ``db.transaction()`` re-reads the deposit on its own connection
(ownership + financial state) and inserts with the unique index as
the race backstop.  Logs carry ids/counts only — never image data,
never secrets.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import db
import deposit_store
from deposit_proof_storage import SUPPORTED_IMAGE_MIMES

logger = logging.getLogger(__name__)

# ── Review states (evidence only — never financial states) ───────────

STATUS_PENDING_REVIEW = "pending_review"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
REVIEW_STATUSES = (
    STATUS_PENDING_REVIEW,
    STATUS_APPROVED,
    STATUS_REJECTED,
)
DECIDED_STATUSES = (STATUS_APPROVED, STATUS_REJECTED)

# Bounds for evidence metadata (mirror the store's conservative style).
_MAX_STORAGE_KEY_LENGTH = 64
_MAX_MIME_LENGTH = 32
_MAX_NOTE_LENGTH = 200
_SQLITE_INT64_MAX = 9_223_372_036_854_775_807

_STORAGE_KEY_RE = re.compile(r"[0-9a-f]{32}\.(png|jpg|gif)\Z")
_PROOF_ID_RE = re.compile(r"[0-9a-f]{32}\Z")


# ── Errors (deterministic machine codes for the HTTP edge) ───────────


class DepositProofError(Exception):
    """Base class for proof-store failures."""

    code = "deposit_proof_error"


class ProofRequestNotFoundError(DepositProofError):
    """No deposit request matches the supplied id."""

    code = "request_not_found"


class ProofOwnershipError(DepositProofError):
    """The deposit request belongs to a different user."""

    code = "request_forbidden"


class ProofDepositNotPendingError(DepositProofError):
    """The deposit is no longer financially pending."""

    code = "request_processed"


class ProofAlreadyPendingError(DepositProofError):
    """A pending-review proof already exists for this request."""

    code = "proof_pending_review"


class ProofNotFoundError(DepositProofError):
    """No proof matches the supplied id."""

    code = "proof_not_found"


class ProofStateError(DepositProofError):
    """The proof is no longer in the state the action requires."""

    code = "proof_state_changed"


class ProofValidationError(DepositProofError):
    """Supplied evidence metadata failed validation."""

    code = "invalid_proof_request"


# ── Row snapshot ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class DepositProof:
    """Immutable snapshot of one persisted proof record.

    ``storage_key`` is excluded from ``repr`` — like
    ``PaymentMethod.destination``, it is an internal reference that
    must never leak through logs or error strings.
    """

    proof_id: str
    request_id: str
    user_id: int
    storage_key: str
    mime_type: str
    size_bytes: int
    width: int
    height: int
    status: str
    review_note: str | None
    reviewed_by: int | None
    reviewed_at: str | None
    created_at: str


_COLUMNS = (
    "proof_id, request_id, user_id, storage_key, mime_type, size_bytes, "
    "width, height, status, review_note, reviewed_by, reviewed_at, "
    "created_at"
)


def _row_to_proof(row) -> DepositProof:
    return DepositProof(
        proof_id=row["proof_id"],
        request_id=row["request_id"],
        user_id=int(row["user_id"]),
        storage_key=row["storage_key"],
        mime_type=row["mime_type"],
        size_bytes=int(row["size_bytes"]),
        width=int(row["width"]),
        height=int(row["height"]),
        status=row["status"],
        review_note=row["review_note"],
        reviewed_by=row["reviewed_by"],
        reviewed_at=row["reviewed_at"],
        created_at=row["created_at"],
    )


# ── Pure validation (before any transaction) ─────────────────────────


def _require_text(value: object, name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProofValidationError(f"{name} must be a non-empty string")
    text = value.strip()
    if len(text) > max_length:
        raise ProofValidationError(f"{name} exceeds {max_length} chars")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ProofValidationError(f"{name} contains illegal characters")
    return text


def _require_proof_id(value: object) -> str:
    if not isinstance(value, str) or not _PROOF_ID_RE.fullmatch(value):
        raise ProofNotFoundError(f"no deposit proof matches {value!r}")
    return value


def _require_note(value: object) -> str:
    """A safe human-readable review note: bounded, control-free.

    Unlike ``_require_text`` this allows normal spaces — a rejection
    reason is a sentence — but control characters are never stored.
    """
    if not isinstance(value, str):
        raise ProofValidationError("review_note must be a string")
    text = value.strip()
    if not text:
        raise ProofValidationError("review_note must not be empty")
    if len(text) > _MAX_NOTE_LENGTH:
        raise ProofValidationError(
            f"review_note exceeds {_MAX_NOTE_LENGTH} chars"
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ProofValidationError(
            "review_note contains control characters"
        )
    return text


def _require_admin_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProofValidationError(
            "reviewed_by must be a positive int admin id"
        )
    if value > _SQLITE_INT64_MAX:
        raise ProofValidationError("reviewed_by exceeds the maximum")
    return value


def _utc_timestamp() -> str:
    """UTC wall-clock TEXT in CURRENT_TIMESTAMP shape (no tz)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── Submission (evidence ONLY — never financial) ─────────────────────


def submit_proof(
    *,
    request_id: object,
    user_id: object,
    storage_key: object,
    mime_type: object,
    size_bytes: object,
    width: object,
    height: object,
    db_path: str | None = None,
) -> DepositProof:
    """Persist ONE pending-review proof for the caller's own request.

    ONE ``db.transaction()``: re-read the deposit on the transaction
    connection → exists → belongs to ``user_id`` → financially
    ``pending`` → no pending proof yet → INSERT (the partial UNIQUE
    index is the race backstop).  Nothing else is read or written:
    wallet, ledger and the deposit row are untouched by construction.

    Raises:
        ProofRequestNotFoundError: unknown/malformed request id.
        ProofOwnershipError: the request belongs to someone else.
        ProofDepositNotPendingError: terminal deposit (credited or
            rejected).
        ProofAlreadyPendingError: a pending-review proof exists
            (including the unique-index race).
        ProofValidationError: unusable evidence metadata.
    """
    if not isinstance(request_id, str) or not request_id:
        raise ProofRequestNotFoundError("no deposit request matches")
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise ProofValidationError("user_id must be an int")
    key = _require_text(storage_key, "storage_key", _MAX_STORAGE_KEY_LENGTH)
    if not _STORAGE_KEY_RE.fullmatch(key):
        raise ProofValidationError("storage_key has an invalid shape")
    mime = _require_text(mime_type, "mime_type", _MAX_MIME_LENGTH)
    if mime not in SUPPORTED_IMAGE_MIMES:
        raise ProofValidationError("mime_type is not a supported image")
    for name, value in (("size_bytes", size_bytes),
                        ("width", width),
                        ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProofValidationError(f"{name} must be an int")
        if value <= 0 or value > _SQLITE_INT64_MAX:
            raise ProofValidationError(f"{name} out of range")

    proof_id = uuid.uuid4().hex
    with db.transaction(db_path) as conn:
        request = deposit_store.get_deposit_request(
            request_id, connection=conn
        )
        if request is None:
            raise ProofRequestNotFoundError(
                f"no deposit request matches {request_id!r}"
            )
        if request.user_id != user_id:
            raise ProofOwnershipError(
                "the deposit request belongs to a different user"
            )
        if request.status != deposit_store.STATUS_PENDING:
            raise ProofDepositNotPendingError(
                "the deposit request is no longer pending"
            )
        existing = conn.execute(
            "SELECT proof_id FROM deposit_proofs "
            "WHERE request_id = ? AND status = ?",
            (request_id, STATUS_PENDING_REVIEW),
        ).fetchone()
        if existing is not None:
            raise ProofAlreadyPendingError(
                "a pending-review proof already exists for this request"
            )
        try:
            conn.execute(
                "INSERT INTO deposit_proofs ("
                "proof_id, request_id, user_id, storage_key, "
                "mime_type, size_bytes, width, height, status"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proof_id,
                    request_id,
                    user_id,
                    key,
                    mime,
                    int(size_bytes),
                    int(width),
                    int(height),
                    STATUS_PENDING_REVIEW,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # ux_deposit_proofs_pending — a concurrent upload won the
            # race; the transaction rolls back (no partial evidence).
            raise ProofAlreadyPendingError(
                "a pending-review proof already exists for this request"
            ) from exc

        logger.info(
            "Deposit proof submitted: proof=%s request=%s user=%s "
            "bytes=%d",
            proof_id, request_id, user_id, int(size_bytes),
        )
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM deposit_proofs WHERE proof_id = ?",
            (proof_id,),
        ).fetchone()
        return _row_to_proof(row)


# ── Reads ────────────────────────────────────────────────────────────


def get_proof(
    proof_id: object, db_path: str | None = None
) -> DepositProof | None:
    """Fetch one proof by id; None for invalid/unknown ids."""
    if not isinstance(proof_id, str) or not _PROOF_ID_RE.fullmatch(
        proof_id
    ):
        return None
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM deposit_proofs WHERE proof_id = ?",
            (proof_id,),
        ).fetchone()
    return _row_to_proof(row) if row else None


def get_pending_proof(
    request_id: object, db_path: str | None = None
) -> DepositProof | None:
    """The active pending-review proof for a request (at most one)."""
    if not isinstance(request_id, str) or not request_id:
        return None
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM deposit_proofs "
            "WHERE request_id = ? AND status = ? "
            "ORDER BY created_at DESC, proof_id LIMIT 1",
            (request_id, STATUS_PENDING_REVIEW),
        ).fetchone()
    return _row_to_proof(row) if row else None


def list_pending_proofs(db_path: str | None = None) -> list[DepositProof]:
    """Every pending-review proof, newest first (deterministic)."""
    with db.get_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM deposit_proofs "
            "WHERE status = ? "
            "ORDER BY created_at DESC, proof_id",
            (STATUS_PENDING_REVIEW,),
        ).fetchall()
    return [_row_to_proof(row) for row in rows]


def count_proofs_for_request(
    request_id: object, db_path: str | None = None
) -> int:
    """How many proofs (any state) exist for one request."""
    if not isinstance(request_id, str) or not request_id:
        return 0
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM deposit_proofs "
            "WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    return int(row["n"])


# ── Review decision (evidence only — NO financial mutation) ──────────


def mark_reviewed(
    proof_id: object,
    status: object,
    reviewed_by: object,
    *,
    review_note: object | None = None,
    db_path: str | None = None,
) -> DepositProof | None:
    """CAS ``pending_review → approved | rejected``.

    Records WHO decided and WHEN.  Returns the updated proof, or
    ``None`` when the proof was already decided (concurrent/stale
    press — zero mutation).  This NEVER touches wallet, ledger or
    the deposit's financial status: rejecting an evidence record
    cannot reject a deposit and approving here cannot credit anything
    (the credit lives exclusively in ``deposit_manual_review``).

    Raises:
        ProofNotFoundError: unknown/malformed proof id.
        ProofStateError: status is not a review decision, or the
            reviewer/decision metadata is invalid.
    """
    proof_id = _require_proof_id(proof_id)
    if status not in DECIDED_STATUSES:
        raise ProofStateError(
            f"status must be one of {DECIDED_STATUSES}"
        )
    reviewer = _require_admin_id(reviewed_by)
    if review_note is not None:
        review_note = _require_note(review_note)
    at_text = _utc_timestamp()

    with db.transaction(db_path) as conn:
        cursor = conn.execute(
            "UPDATE deposit_proofs SET status = ?, reviewed_by = ?, "
            "reviewed_at = ?, review_note = ? "
            "WHERE proof_id = ? AND status = ?",
            (
                status,
                reviewer,
                at_text,
                review_note,
                proof_id,
                STATUS_PENDING_REVIEW,
            ),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM deposit_proofs WHERE proof_id = ?",
            (proof_id,),
        ).fetchone()
        logger.info(
            "Deposit proof reviewed: proof=%s result=%s admin=%s",
            proof_id, status, reviewer,
        )
        return _row_to_proof(row)
