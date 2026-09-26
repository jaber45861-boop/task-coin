"""
Payment Method Store (MT-ADMIN-08)
==================================

Persistent, admin-configurable payment-method / wallet foundation.
The ``payment_methods`` table created by ``db.init_db`` is the ONLY
place payment methods live: nothing about a provider, network or
destination exists in Python constants — every value below is data an
admin writes at runtime through the Admin Control Plane.

Design rules (MT-ADMIN-08):

- **No hard-coding.**  ``provider``, ``network``, ``asset`` and
  ``destination`` are free-form TEXT.  Adding "Exchange X" or a new
  chain requires ZERO code and ZERO migration.  Only ``category`` is a
  closed taxonomy (``crypto`` | ``cash`` — the two supported payment
  concepts), because it drives display semantics, not extensibility.
- **Generic validation only.**  Fields are non-empty, bounded and
  free of unsafe control characters.  Destinations are NOT
  format-checked against any network allow-list (the system is
  intentionally network-agnostic) and entering a destination never
  implies it was verified.
- **Sensitive values.**  ``destination`` carries ``repr=False`` so it
  can never leak through logs/prints, and no function in this module
  logs it.
- **Separation.**  This layer touches ONLY the ``payment_methods``
  table — never ``wallets``, ``ledger`` or ``withdrawal_requests``
  (MT-07/finance accounting stays untouched).
- **Deletion.**  Zero-state production has no financial history, so a
  normal DELETE is used today.  The integer primary key is the stable
  handle a future transaction table can reference (FK RESTRICT) to
  block destructive deletion once real history exists — no archival
  machinery is invented now.

Conventions: writes go through ``db.transaction()`` (BEGIN IMMEDIATE);
reads open their own ``db.get_connection()`` scope.  Every mutation
carries the acting admin id in ``created_by``/``updated_by`` plus a
structured log line (method id + operation + admin — never the
destination).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

import db
from task_taxonomy import has_unsafe_control_chars

logger = logging.getLogger(__name__)

# ── Categories: the ONLY closed set (payment concepts, not providers) ─
CATEGORY_CRYPTO = "crypto"
CATEGORY_CASH = "cash"
CATEGORIES = (CATEGORY_CRYPTO, CATEGORY_CASH)
CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_CRYPTO: "عملات رقمية",
    CATEGORY_CASH: "محفظة إلكترونية",
}

# ── Field bounds (generic, network-agnostic) ─────────────────────────
MAX_DISPLAY_NAME = 100
MAX_PROVIDER = 100
MAX_ASSET = 32
MAX_NETWORK = 64
MAX_DESTINATION = 200
MAX_INSTRUCTIONS = 500

# Sentinel used by the admin form for "no value" (never stored).
EMPTY_FIELD = "-"

# Generic private-key markers (the ONE recognizable secret shape a
# network-agnostic rule can safely catch).  The model never asks for
# keys and must never store them; format-specific validation of any
# kind remains deliberately out of scope.
_PRIVATE_KEY_MARKERS = ("-----begin", "private key")


# ── Errors (Arabic — shown directly to the admin) ─────────────────────


class PaymentMethodError(Exception):
    """Base class for payment-method failures."""


class PaymentMethodValidationError(PaymentMethodError):
    """Untrusted input failed generic validation."""


class PaymentMethodNotFoundError(PaymentMethodError):
    """No payment method matches the given id."""


# ── Row snapshot ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class PaymentMethod:
    """Immutable snapshot of one payment method.

    ``destination`` is excluded from ``repr``: it is sensitive
    financial configuration and must never be printed or logged.
    """

    id: int
    category: str
    display_name: str
    asset: str
    network: str | None
    provider: str
    destination: str = field(repr=False)
    instructions: str | None
    is_active: bool
    sort_order: int
    created_by: int | None
    updated_by: int | None
    created_at: str
    updated_at: str


# ── Generic validation (no network/provider knowledge) ────────────────


def _clean_single_line(
    value: object,
    *,
    label: str,
    max_length: int,
    required: bool,
) -> str | None:
    """Strip + bound one single-line field; None when optional/absent.

    Rejects: non-strings, empty required values, over-long values and
    unsafe control characters (a single-line field never contains
    newlines).  No format/pattern rules beyond that — a destination is
    never checked against a network allow-list.
    """
    if not isinstance(value, str):
        if required:
            raise PaymentMethodValidationError(f"{label}: نص مطلوب")
        return None
    text = value.strip()
    if not text:
        if required:
            raise PaymentMethodValidationError(
                f"{label}: مطلوب ولا يمكن أن يكون فارغاً"
            )
        return None
    if not required and text == EMPTY_FIELD:
        # The admin form's "no value" sentinel is never stored.
        return None
    if len(text) > max_length:
        raise PaymentMethodValidationError(
            f"{label}: يتجاوز الحد {max_length} حرفاً"
        )
    if has_unsafe_control_chars(text, allow_newlines=False):
        raise PaymentMethodValidationError(
            f"{label}: يحتوي على رموز غير مسموحة"
        )
    return text


def validate_category(value: object) -> str:
    """Closed taxonomy: exactly ``crypto`` or ``cash``."""
    if not isinstance(value, str):
        raise PaymentMethodValidationError("الفئة: نص مطلوب")
    category = value.strip().lower()
    if category not in CATEGORIES:
        raise PaymentMethodValidationError(
            "الفئة يجب أن تكون crypto أو cash"
        )
    return category


def validate_display_name(value: object) -> str:
    text = _clean_single_line(
        value, label="الاسم", max_length=MAX_DISPLAY_NAME, required=True
    )
    assert text is not None
    return text


def validate_asset(value: object) -> str:
    text = _clean_single_line(
        value, label="العملة", max_length=MAX_ASSET, required=True
    )
    assert text is not None
    return text


def validate_network(value: object) -> str | None:
    """Free-form network; ``-``/empty means 'not applicable' → NULL."""
    return _clean_single_line(
        value, label="الشبكة", max_length=MAX_NETWORK, required=False
    )


def validate_provider(value: object) -> str:
    text = _clean_single_line(
        value, label="المزود", max_length=MAX_PROVIDER, required=True
    )
    assert text is not None
    return text


def validate_destination(value: object) -> str:
    """Required, bounded, control-free — and format-agnostic.

    This is the payout destination (address / account number).  It is
    deliberately NOT validated against any blockchain/network rule: the
    model is provider- and network-extensible, so only generic rules
    are justified.  Accepting it never implies it was verified.
    """
    # Private-key markers are checked on the RAW value first so a key
    # block (which may contain newlines) is refused as a secret, not as
    # a mere control-character problem.
    if isinstance(value, str):
        _reject_private_key(value, label="العنوان")
    text = _clean_single_line(
        value,
        label="العنوان",
        max_length=MAX_DESTINATION,
        required=True,
    )
    assert text is not None
    return text


def _reject_private_key(text: str, *, label: str) -> None:
    """Refuse the one recognizable secret shape: a private key.

    Generic only (PEM block marker or an explicitly labelled key) —
    never a network-specific address heuristic.
    """
    lowered = text.lower()
    for marker in _PRIVATE_KEY_MARKERS:
        if marker in lowered:
            raise PaymentMethodValidationError(
                f"{label}: لا يمكن أن يحتوي على مفتاح خاص — "
                "أدخل عنوان الدفع فقط"
            )


def validate_instructions(value: object) -> str | None:
    """Optional notes; multi-line allowed; ``-``/empty → None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text == EMPTY_FIELD:
        return None
    if len(text) > MAX_INSTRUCTIONS:
        raise PaymentMethodValidationError(
            f"الملاحظات: يتجاوز الحد {MAX_INSTRUCTIONS} حرفاً"
        )
    if has_unsafe_control_chars(text, allow_newlines=True):
        raise PaymentMethodValidationError(
            "الملاحظات: يحتوي على رموز غير مسموحة"
        )
    _reject_private_key(text, label="الملاحظات")
    return text


@dataclass(frozen=True)
class PaymentMethodForm:
    """Fully validated create/edit payload."""

    category: str
    display_name: str
    asset: str
    network: str | None
    provider: str
    destination: str
    instructions: str | None


def validate_form(
    category: object,
    display_name: object,
    asset: object,
    network: object,
    provider: object,
    destination: object,
    instructions: object = None,
) -> PaymentMethodForm:
    """Validate every field server-side and return the clean payload."""
    return PaymentMethodForm(
        category=validate_category(category),
        display_name=validate_display_name(display_name),
        asset=validate_asset(asset),
        network=validate_network(network),
        provider=validate_provider(provider),
        destination=validate_destination(destination),
        instructions=validate_instructions(instructions),
    )


# ── Row mapping ───────────────────────────────────────────────────────

_COLUMNS = (
    "id, category, display_name, asset, network, provider, destination, "
    "instructions, is_active, sort_order, created_by, updated_by, "
    "created_at, updated_at"
)


def _row_to_method(row) -> PaymentMethod:
    return PaymentMethod(
        id=row["id"],
        category=row["category"],
        display_name=row["display_name"],
        asset=row["asset"],
        network=row["network"],
        provider=row["provider"],
        destination=row["destination"],
        instructions=row["instructions"],
        is_active=bool(row["is_active"]),
        sort_order=row["sort_order"],
        created_by=row["created_by"],
        updated_by=row["updated_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _require_id(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PaymentMethodValidationError(f"{name}: معرف موجب مطلوب")
    return value


def _require_admin_id(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PaymentMethodValidationError(f"{name}: معرف موجب مطلوب")
    return value


# ── Create ────────────────────────────────────────────────────────────


def create_payment_method(
    *,
    category: object,
    display_name: object,
    asset: object,
    network: object = None,
    provider: object,
    destination: object,
    instructions: object = None,
    created_by: object = None,
    sort_order: object = None,
    db_path: str | None = None,
) -> PaymentMethod:
    """Validate and persist one NEW payment method.

    Returns the stored row with its stable integer id.  ``sort_order``
    defaults to "after everything existing" so display order follows
    insertion order deterministically.
    """
    form = validate_form(
        category, display_name, asset, network,
        provider, destination, instructions,
    )
    actor = _require_admin_id(created_by, "created_by")

    if sort_order is None:
        order_value = None  # resolved inside the transaction
    else:
        if (
            isinstance(sort_order, bool)
            or not isinstance(sort_order, int)
            or sort_order < 0
        ):
            raise PaymentMethodValidationError(
                "sort_order: عدد صحيح غير سالب مطلوب"
            )
        order_value = sort_order

    with db.transaction(db_path) as conn:
        if order_value is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order "
                "FROM payment_methods"
            ).fetchone()
            order_value = row["next_order"]
        cursor = conn.execute(
            "INSERT INTO payment_methods "
            "(category, display_name, asset, network, provider, "
            " destination, instructions, is_active, sort_order, "
            " created_by, updated_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                form.category,
                form.display_name,
                form.asset,
                form.network,
                form.provider,
                form.destination,
                form.instructions,
                order_value,
                actor,
                actor,
            ),
        )
        new_id = cursor.lastrowid
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM payment_methods WHERE id = ?",
            (new_id,),
        ).fetchone()

    created = _row_to_method(row)
    # Audit line: ids/operation/actor only — NEVER the destination.
    logger.info(
        "Payment method created: id=%d category=%s admin=%r",
        created.id, created.category, created.created_by,
    )
    return created


# ── Reads ─────────────────────────────────────────────────────────────


def get_payment_method(
    method_id: object, db_path: str | None = None
) -> PaymentMethod | None:
    """Fetch one payment method by id; None for invalid/missing ids."""
    if (
        isinstance(method_id, bool)
        or not isinstance(method_id, int)
        or method_id <= 0
    ):
        return None
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM payment_methods WHERE id = ?",
            (method_id,),
        ).fetchone()
    return _row_to_method(row) if row else None


def list_payment_methods(
    *, active_only: bool = False, db_path: str | None = None
) -> list[PaymentMethod]:
    """Every payment method, deterministically ordered.

    Ordering: ``sort_order ASC, id ASC`` — stable, oldest-first for
    equal sort keys.  Read-only: listing mutates nothing.
    """
    sql = f"SELECT {_COLUMNS} FROM payment_methods"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY sort_order ASC, id ASC"
    with db.get_connection(db_path) as conn:
        rows = conn.execute(sql).fetchall()
    return [_row_to_method(r) for r in rows]


# ── Update (full replace; id, created_at, created_by never change) ────


def update_payment_method(
    method_id: object,
    *,
    category: object,
    display_name: object,
    asset: object,
    network: object = None,
    provider: object,
    destination: object,
    instructions: object = None,
    updated_by: object = None,
    db_path: str | None = None,
) -> PaymentMethod | None:
    """Replace every editable field of one payment method.

    Returns the updated row, or None when the id does not exist.
    The primary key, ``created_at`` and ``created_by`` are untouched —
    ids remain stable across any number of edits.
    """
    mid = _require_id(method_id, "method_id")
    form = validate_form(
        category, display_name, asset, network,
        provider, destination, instructions,
    )
    actor = _require_admin_id(updated_by, "updated_by")

    with db.transaction(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM payment_methods WHERE id = ?", (mid,)
        ).fetchone()
        if existing is None:
            return None
        conn.execute(
            "UPDATE payment_methods SET "
            "category = ?, display_name = ?, asset = ?, network = ?, "
            "provider = ?, destination = ?, instructions = ?, "
            "updated_by = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (
                form.category,
                form.display_name,
                form.asset,
                form.network,
                form.provider,
                form.destination,
                form.instructions,
                actor,
                mid,
            ),
        )
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM payment_methods WHERE id = ?",
            (mid,),
        ).fetchone()

    updated = _row_to_method(row)
    logger.info(
        "Payment method updated: id=%d category=%s admin=%r",
        updated.id, updated.category, updated.updated_by,
    )
    return updated


# ── Activate / deactivate (idempotent) ────────────────────────────────


def set_payment_method_active(
    method_id: object,
    is_active: object,
    *,
    updated_by: object = None,
    db_path: str | None = None,
) -> PaymentMethod | None:
    """Set the active flag on one payment method (idempotent).

    Returns the row afterwards, or None when the id does not exist.
    Running the same activation twice is a harmless no-op write.
    """
    mid = _require_id(method_id, "method_id")
    if not isinstance(is_active, bool):
        raise PaymentMethodValidationError("is_active: قيمة منطقية مطلوبة")
    actor = _require_admin_id(updated_by, "updated_by")

    with db.transaction(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM payment_methods WHERE id = ?", (mid,)
        ).fetchone()
        if existing is None:
            return None
        conn.execute(
            "UPDATE payment_methods SET is_active = ?, updated_by = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (1 if is_active else 0, actor, mid),
        )
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM payment_methods WHERE id = ?",
            (mid,),
        ).fetchone()

    changed = _row_to_method(row)
    logger.info(
        "Payment method %s: id=%d admin=%r",
        "activated" if is_active else "deactivated",
        changed.id, actor,
    )
    return changed


# ── Delete (zero-state normal deletion) ───────────────────────────────


def delete_payment_method(
    method_id: object,
    *,
    deleted_by: object = None,
    db_path: str | None = None,
) -> bool:
    """Delete one payment method.  True when a row was removed.

    Zero-state rule: no financial history references payment methods
    yet, so a plain DELETE is correct today.  Once future transaction
    rows reference ``payment_methods(id)``, the FK (RESTRICT) — not a
    new archive system — will guard this path.
    """
    mid = _require_id(method_id, "method_id")
    actor = _require_admin_id(deleted_by, "deleted_by")
    with db.transaction(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM payment_methods WHERE id = ?", (mid,)
        )
        deleted = cursor.rowcount > 0
    if deleted:
        logger.info("Payment method deleted: id=%d admin=%r", mid, actor)
    return deleted
