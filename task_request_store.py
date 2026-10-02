"""
User Task Request Store (Mini App «إضافة مهمة»)
================================================

Durable persistence + validation for user-proposed tasks awaiting
admin review.  The ``user_task_requests`` table (created by
``db.init_db``) is the ONLY place a proposal lives until an admin
approves it — pending requests never touch the ``tasks`` table, so
they can never appear in the public task catalog
(``TaskCatalog.list_available_tasks`` reads ``tasks`` exclusively).

State machine (only these transitions exist)::

    user POST/PATCH ──→ pending ──admin approve──→ approved (published)
                            │
                            ├──admin reject──────→ rejected   (terminal)
                            │
                            └──admin return──────→ changes_requested
                                                       │
                                     user PATCH ────────┘ → pending

Boundaries — this module must NOT:
- create ``tasks`` rows: approval orchestration (claim CAS →
  ``task_creation.create_task_from_spec`` → mark published, all in
  ONE ``db.transaction()``) lives in ``task_request_admin``; here we
  only expose :func:`claim_for_approval` + :func:`mark_published`
  helpers that run on the CALLER's connection (the same pattern as
  ``task_draft_store``)
- decide anything over HTTP: the Mini App API only creates, reads
  and resubmits the caller's OWN requests (identity from verified
  initData); approve/reject/return/edit are Telegram-admin-only
- authorize users (ownership is enforced on every read/write here;
  the caller re-checks ``config.is_admin`` for admin operations)
- trust any client-supplied identity: ``user_id`` always comes from
  the authenticated caller, never from a payload
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

import db
from task_creation import (
    parse_reward_units,
    reward_units_to_text,
    whole_usdt_reward,
)
from task_taxonomy import (
    ACTIONS_BY_PROVIDER,
    GENERIC_PROVIDER_SET,
    validate_instructions,
    validate_target_ref,
    validate_title,
)

logger = logging.getLogger(__name__)

# ── Status vocabulary (the ONLY four states) ──────────────────────────

STATUS_PENDING = "pending"                    # قيد المراجعة
STATUS_APPROVED = "approved"                  # منشورة
STATUS_REJECTED = "rejected"                  # مرفوضة
STATUS_CHANGES_REQUESTED = "changes_requested"  # تحتاج إلى تعديل
REQUEST_STATUSES = (
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_CHANGES_REQUESTED,
)

# Fields a proposal may carry (client-sent keys are validated against
# exactly this set — status/user_id/history and friends are rejected).
PAYLOAD_FIELDS = ("title", "description", "provider", "action",
                  "target_ref", "reward")
# Admin-editable fields exposed by the Telegram review surface.
ADMIN_EDITABLE_FIELDS = ("title", "description", "target_ref", "reward")

# Rejection/return notes stay bounded plain text (no markup needed).
MAX_REASON_LENGTH = 500

_COLUMNS = (
    "request_id, user_id, status, payload_json, history_json, "
    "decision_reason, decided_by, decided_at, published_task_id, "
    "created_at, updated_at"
)


class TaskRequestError(ValueError):
    """The request cannot be created/transitioned.

    Message is user/admin-displayable Arabic; never carries internals.
    """


@dataclass(frozen=True)
class TaskRequest:
    """Immutable snapshot of one user task request."""

    request_id: int
    user_id: int
    status: str
    payload: dict
    history: list
    decision_reason: str | None
    decided_by: int | None
    decided_at: str | None
    published_task_id: int | None
    created_at: str
    updated_at: str

    @property
    def reward_units(self) -> int:
        return int(self.payload.get("reward_units") or 0)

    @property
    def reward(self) -> int:
        """Whole-USDT compatibility/display value (int math only)."""
        return whole_usdt_reward(self.reward_units)


def _row_to_request(row: sqlite3.Row | None) -> TaskRequest | None:
    """Convert a row; a corrupt payload/history fails safe (missing)."""
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (ValueError, TypeError):
        logger.warning(
            "Corrupt task request payload: request_id=%s",
            row["request_id"],
        )
        return None
    if not isinstance(payload, dict):
        logger.warning(
            "Non-object task request payload: request_id=%s",
            row["request_id"],
        )
        return None
    try:
        history = json.loads(row["history_json"] or "[]")
    except (ValueError, TypeError):
        history = []
    if not isinstance(history, list):
        history = []
    return TaskRequest(
        request_id=row["request_id"],
        user_id=row["user_id"],
        status=row["status"],
        payload=payload,
        history=history,
        decision_reason=row["decision_reason"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        published_task_id=row["published_task_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Payload validation (server-side, shared by user + admin paths) ────


def validate_payload(raw: object) -> dict:
    """Validate an untrusted proposal → the stored payload shape.

    Every field is validated HERE with the same validators the
    creation path uses (``task_taxonomy`` bounds, provider/action
    whitelists, the canonical exact reward parser), so an approved
    payload can always become a valid ``TaskSpec``.

    Returns the canonical stored payload::

        {title, description, provider, action, target_ref, reward_units}

    Raises:
        TaskRequestError: wrong type, missing/unknown/invalid field
        (Arabic, displayable — never internals).
    """
    if not isinstance(raw, dict):
        raise TaskRequestError("بيانات الطلب غير صالحة.")
    unknown = set(raw) - set(PAYLOAD_FIELDS)
    if unknown:
        # Never accept status/user_id/history — identity and state
        # are server-owned, not client-supplied.
        raise TaskRequestError(
            "حقول غير مسموحة في طلب المهمة: "
            + ", ".join(sorted(str(k) for k in unknown))
        )
    # target_ref is optional (may be empty/absent); all others required.
    absent = [
        f for f in PAYLOAD_FIELDS
        if f != "target_ref" and f not in raw
    ]
    if absent:
        raise TaskRequestError(
            "حقول ناقصة في طلب المهمة: " + ", ".join(absent)
        )

    try:
        title = validate_title(raw.get("title"))
        description = validate_instructions(raw.get("description"))
    except ValueError as exc:
        raise TaskRequestError(str(exc).lstrip("❌ ")) from exc

    provider = raw.get("provider")
    if not isinstance(provider, str) or provider not in GENERIC_PROVIDER_SET:
        raise TaskRequestError("نوع المنصة غير مدعوم.")
    action = raw.get("action")
    allowed_actions = ACTIONS_BY_PROVIDER.get(provider, ())
    if not isinstance(action, str) or action not in allowed_actions:
        raise TaskRequestError(
            "الإجراء غير مدعوم لهذه المنصة."
        )

    target_ref = raw.get("target_ref", "")
    if target_ref is None:
        target_ref = ""
    try:
        target_ref = validate_target_ref(target_ref) if str(target_ref).strip() else ""
    except ValueError as exc:
        raise TaskRequestError(str(exc).lstrip("❌ ")) from exc

    try:
        reward_units = parse_reward_units(raw.get("reward"), field="المكافأة")
    except ValueError as exc:
        raise TaskRequestError(str(exc).lstrip("❌ ")) from exc

    return {
        "title": title,
        "description": description,
        "provider": provider,
        "action": action,
        "target_ref": target_ref,
        "reward_units": reward_units,
    }


def _to_input_shape(payload: dict) -> dict:
    """Stored payload → the client input shape ``validate_payload``
    accepts (``reward_units`` int → exact decimal text), so a field
    edit revalidates through the ONE canonical parser."""
    out = {
        key: payload.get(key)
        for key in ("title", "description", "provider", "action",
                    "target_ref")
    }
    try:
        units = int(payload.get("reward_units") or 0)
    except (TypeError, ValueError):
        units = 0
    out["reward"] = reward_units_to_text(units)
    return out


def validate_field(field: str, value: object, current: dict) -> dict:
    """Validate ONE admin-edited field against the current payload.

    Returns the full updated payload.  Unknown fields raise — the
    admin UI only ever offers :data:`ADMIN_EDITABLE_FIELDS`.
    """
    if field not in ADMIN_EDITABLE_FIELDS:
        raise TaskRequestError("حقل التعديل غير مدعوم.")
    raw = _to_input_shape(current)
    raw[field] = value
    return validate_payload(raw)


# ── Audit history (append-only JSON array) ────────────────────────────


def _event(event: str, actor: int | None = None, **extra) -> dict:
    item = {"event": event}
    if actor is not None:
        item["actor"] = actor
    item.update(extra)
    return item


# ── Reads ─────────────────────────────────────────────────────────────


def get_request(request_id: object) -> TaskRequest | None:
    """Fetch one request by id (ownership is checked by the caller)."""
    if isinstance(request_id, bool) or not isinstance(request_id, int):
        return None
    with db.get_connection() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM user_task_requests "
            "WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    return _row_to_request(row)


def list_for_user(user_id: int) -> list[TaskRequest]:
    """The caller's own requests, newest first."""
    with db.get_connection() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM user_task_requests "
            "WHERE user_id = ? ORDER BY request_id DESC",
            (user_id,),
        ).fetchall()
    return [r for r in (_row_to_request(row) for row in rows) if r]


def list_pending(limit: int = 20, offset: int = 0) -> list[TaskRequest]:
    """Admin queue: pending requests, oldest first (deterministic)."""
    limit = max(1, min(int(limit), 50))
    offset = max(0, int(offset))
    with db.get_connection() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM user_task_requests "
            "WHERE status = ? ORDER BY request_id ASC LIMIT ? OFFSET ?",
            (STATUS_PENDING, limit, offset),
        ).fetchall()
    return [r for r in (_row_to_request(row) for row in rows) if r]


def count_pending() -> int:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM user_task_requests "
            "WHERE status = ?",
            (STATUS_PENDING,),
        ).fetchone()
    return int(row["n"])


# ── User transitions (create / edit + resubmit) ───────────────────────


def create_request(user_id: int, raw: object) -> TaskRequest:
    """Validate + insert a fresh ``pending`` request for ``user_id``."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise TaskRequestError("مستخدم غير صالح.")
    payload = validate_payload(raw)
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO user_task_requests "
            "(user_id, status, payload_json, history_json) "
            "VALUES (?, ?, ?, ?)",
            (
                user_id,
                STATUS_PENDING,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(
                    [_event("submitted")], ensure_ascii=False
                ),
            ),
        )
        request_id = cursor.lastrowid
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM user_task_requests "
            "WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    logger.info(
        "Task request created: request_id=%s user=%s",
        request_id, user_id,
    )
    return _row_to_request(row)


def resubmit_request(
    request_id: object, user_id: object, raw: object
) -> TaskRequest:
    """Owner edit + resubmit: ``changes_requested`` → ``pending``.

    Identity is the ownership column — a foreign or missing id is
    indistinguishable from "not found" for the caller.  Validation
    runs BEFORE the write, so an invalid edit changes nothing.
    """
    if isinstance(request_id, bool) or not isinstance(request_id, int):
        raise TaskRequestError("الطلب غير موجود.")
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise TaskRequestError("الطلب غير موجود.")
    payload = validate_payload(raw)
    with db.transaction() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM user_task_requests "
            "WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        current = _row_to_request(row)
        if current is None or current.user_id != user_id:
            raise TaskRequestError("الطلب غير موجود.")
        if current.status != STATUS_CHANGES_REQUESTED:
            raise TaskRequestError(
                "لا يمكن تعديل هذا الطلب في وضعه الحالي."
            )
        history = list(current.history) + [
            _event("resubmitted"),
            _event("submitted"),
        ]
        cursor = conn.execute(
            "UPDATE user_task_requests "
            "SET status = ?, payload_json = ?, history_json = ?, "
            "    decision_reason = NULL, decided_by = NULL, "
            "    decided_at = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE request_id = ? AND user_id = ? AND status = ?",
            (
                STATUS_PENDING,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(history, ensure_ascii=False),
                request_id,
                user_id,
                STATUS_CHANGES_REQUESTED,
            ),
        )
        if cursor.rowcount != 1:
            raise TaskRequestError("الطلب غير موجود.")
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM user_task_requests "
            "WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    logger.info(
        "Task request resubmitted: request_id=%s user=%s",
        request_id, user_id,
    )
    return _row_to_request(row)


# ── Admin transitions (edit / return / reject) ────────────────────────


def _require_pending(
    conn, request_id: int
) -> TaskRequest:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM user_task_requests "
        "WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    request = _row_to_request(row)
    if request is None:
        raise TaskRequestError("الطلب غير موجود.")
    if request.status != STATUS_PENDING:
        raise TaskRequestError("تم اتخاذ قرار على هذا الطلب مسبقاً.")
    return request


def admin_edit_field(
    request_id: object, admin_id: object, field: str, value: object
) -> TaskRequest:
    """Admin edits ONE field of a still-pending request.

    The edited payload replaces the request's payload (the detail
    view shows the edited version), and the PREVIOUS payload is
    appended to ``history_json`` for auditability.
    """
    if isinstance(request_id, bool) or not isinstance(request_id, int):
        raise TaskRequestError("الطلب غير موجود.")
    if isinstance(admin_id, bool) or not isinstance(admin_id, int):
        raise TaskRequestError("لا تملك صلاحية هذا الإجراء.")
    with db.transaction() as conn:
        request = _require_pending(conn, request_id)
        payload = validate_field(field, value, request.payload)
        history = list(request.history) + [
            _event(
                "admin_edit",
                actor=admin_id,
                field=field,
                previous_payload=dict(request.payload),
            )
        ]
        conn.execute(
            "UPDATE user_task_requests "
            "SET payload_json = ?, history_json = ?, "
            "    updated_at = CURRENT_TIMESTAMP "
            "WHERE request_id = ? AND status = ?",
            (
                json.dumps(payload, ensure_ascii=False),
                json.dumps(history, ensure_ascii=False),
                request_id,
                STATUS_PENDING,
            ),
        )
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM user_task_requests "
            "WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    logger.info(
        "Task request edited by admin: request_id=%s admin=%s field=%s",
        request_id, admin_id, field,
    )
    return _row_to_request(row)


def admin_return_request(
    request_id: object, admin_id: object, note: object
) -> TaskRequest:
    """Admin returns a pending request to the user for editing."""
    if isinstance(request_id, bool) or not isinstance(request_id, int):
        raise TaskRequestError("الطلب غير موجود.")
    if isinstance(admin_id, bool) or not isinstance(admin_id, int):
        raise TaskRequestError("لا تملك صلاحية هذا الإجراء.")
    reason = _clean_reason(note)
    with db.transaction() as conn:
        request = _require_pending(conn, request_id)
        history = list(request.history) + [
            _event("returned", actor=admin_id, reason=reason)
        ]
        conn.execute(
            "UPDATE user_task_requests "
            "SET status = ?, decision_reason = ?, decided_by = ?, "
            "    history_json = ?, decided_at = CURRENT_TIMESTAMP, "
            "    updated_at = CURRENT_TIMESTAMP "
            "WHERE request_id = ? AND status = ?",
            (
                STATUS_CHANGES_REQUESTED,
                reason,
                admin_id,
                json.dumps(history, ensure_ascii=False),
                request_id,
                STATUS_PENDING,
            ),
        )
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM user_task_requests "
            "WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    logger.info(
        "Task request returned for changes: request_id=%s admin=%s",
        request_id, admin_id,
    )
    return _row_to_request(row)


def admin_reject_request(
    request_id: object, admin_id: object, reason: object
) -> TaskRequest:
    """Admin rejects a pending request (terminal, with a reason)."""
    if isinstance(request_id, bool) or not isinstance(request_id, int):
        raise TaskRequestError("الطلب غير موجود.")
    if isinstance(admin_id, bool) or not isinstance(admin_id, int):
        raise TaskRequestError("لا تملك صلاحية هذا الإجراء.")
    reason = _clean_reason(reason)
    with db.transaction() as conn:
        request = _require_pending(conn, request_id)
        history = list(request.history) + [
            _event("rejected", actor=admin_id, reason=reason)
        ]
        conn.execute(
            "UPDATE user_task_requests "
            "SET status = ?, decision_reason = ?, decided_by = ?, "
            "    history_json = ?, decided_at = CURRENT_TIMESTAMP, "
            "    updated_at = CURRENT_TIMESTAMP "
            "WHERE request_id = ? AND status = ?",
            (
                STATUS_REJECTED,
                reason,
                admin_id,
                json.dumps(history, ensure_ascii=False),
                request_id,
                STATUS_PENDING,
            ),
        )
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM user_task_requests "
            "WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    logger.info(
        "Task request rejected: request_id=%s admin=%s",
        request_id, admin_id,
    )
    return _row_to_request(row)


def _clean_reason(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:MAX_REASON_LENGTH]


# ── Approval CAS (caller owns the transaction) ────────────────────────
# These two helpers run on the connection yielded by the caller's
# ``db.transaction()`` so claim + task INSERT + mark land in ONE atomic
# transaction — the exact pattern of ``task_draft_store``
# claim_for_publish / mark_published.  They never begin/commit
# anything themselves.


def claim_for_approval(
    conn: sqlite3.Connection, request_id: int, admin_id: int
) -> TaskRequest | None:
    """CAS: flip ``pending → approved`` for exactly ONE caller.

    Returns the freshly claimed request for the winner; ``None`` when
    the request is missing or no longer pending (a replayed or
    concurrent approve must NOT create a second task).  The claim
    stamps ``decided_by`` immediately — a later failure rolls the
    whole claim back with the task INSERT.
    """
    cursor = conn.execute(
        "UPDATE user_task_requests "
        "SET status = ?, decided_by = ?, decided_at = CURRENT_TIMESTAMP, "
        "    updated_at = CURRENT_TIMESTAMP "
        "WHERE request_id = ? AND status = ?",
        (STATUS_APPROVED, admin_id, request_id, STATUS_PENDING),
    )
    if cursor.rowcount != 1:
        return None
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM user_task_requests "
        "WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    return _row_to_request(row)


def mark_approved(
    conn: sqlite3.Connection,
    request_id: int,
    admin_id: int,
    task_id: int,
) -> None:
    """Record the decision + published task id (same transaction)."""
    history = None
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM user_task_requests "
        "WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    request = _row_to_request(row)
    if request is not None:
        history = list(request.history) + [
            _event("approved", actor=admin_id, task_id=task_id)
        ]
    conn.execute(
        "UPDATE user_task_requests "
        "SET status = ?, decided_by = ?, decided_at = CURRENT_TIMESTAMP, "
        "    published_task_id = ?, history_json = ?, "
        "    decision_reason = NULL, updated_at = CURRENT_TIMESTAMP "
        "WHERE request_id = ?",
        (
            STATUS_APPROVED,
            admin_id,
            task_id,
            json.dumps(history or [], ensure_ascii=False),
            request_id,
        ),
    )


def read_published_task_id(
    conn: sqlite3.Connection, request_id: int
) -> int | None:
    """The task id an already-approved request published, or None."""
    row = conn.execute(
        "SELECT published_task_id FROM user_task_requests "
        "WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    return row["published_task_id"]


def spec_from_request(request: TaskRequest, approver_id: int):
    """Build the canonical ``TaskSpec`` for an approved request.

    Pure: no writes.  The payload was validated on every write path,
    so this only shapes it for the ONE creation service — manual
    verification (the existing MT-TASK-15 contract) with the
    approving admin as the task's approver.
    """
    from task_creation import TaskSpec
    from task_taxonomy import VERIFICATION_MANUAL

    payload = request.payload
    reward_units = request.reward_units
    target_ref = payload.get("target_ref") or ""
    target = {"ref": target_ref} if target_ref else {}
    return TaskSpec(
        title=payload.get("title") or "",
        description=payload.get("description") or "",
        provider=payload.get("provider") or "",
        action=payload.get("action") or "",
        target=target,
        verification=VERIFICATION_MANUAL,
        reward=whole_usdt_reward(reward_units),
        reward_units=reward_units,
        approver_id=approver_id,
    )
