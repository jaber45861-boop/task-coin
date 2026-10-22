"""Owner-management lifecycle for published task requests (Phase 17).

A published task's requester can PAUSE / RESUME / LOGICALLY ARCHIVE their
own request, and edit its title/description — from the Mini App — without
touching funding, ledger, claims, published-task rows, or any DB schema.
State is recorded as append-only history events on the already-approved
``user_task_requests`` row:

* ``"paused"`` — NEW participation from all entry points (catalog
  discovery, worker start, worker submit) is blocked with a 409;
  reviewer surfaces keep working so existing claims/reviews stay
  accessible.
* ``"archived"`` — the request is hidden from the owner's Mini App list
  and from catalog discovery (404 to workers), but the row / funding /
  ledger / claim data stay intact.

The ``request.status`` column keeps its schema-constrained value
(``approved``), so these operations require NO migration and alter NO
financial or task state.

The public API (see ``task_routes``) uses this module exclusively:

* ``participation_block(task_id)`` — ownership-blind predicate used by
  the discovery surface and worker entry points; returns ``None``,
  ``"paused"``, or ``"archived"``.
* ``request_is_paused(req)`` / ``request_is_archived(req)`` — read-only
  views of the append-only history, surfaced to the Mini App as the
  ``paused`` / ``archived`` response fields.
* ``_owner_published_pair(conn, user_id, published_task_id)`` — the
  SERVER-SIDE ownership lookup returning ``(request, task_id)`` or
  ``(None, None)``; every owner endpoint starts here, so a foreign task
  is indistinguishable from a missing one.
* ``apply_edit / apply_pause / apply_resume / apply_archive`` — the
  mutating owner endpoints, each keyed on the verified owner (and, for
  edits, on the lifecycle state).

Edit rules (enforced here, server-side — the Mini App is never
trusted): the ONLY writable fields are ``title`` and ``description``.
Every other body key — provider / action / target_ref (execution
semantics: an admin-review decision through the existing
``task_request_admin`` workflow), reward / reward_units (financial:
owned by the funding + ledger services, never present in the payload
write path), and identity/state keys (user_id, status, history, …) —
is rejected outright.  The store write itself re-guards ownership and
``status = approved`` with a CAS, so a raced or forged request can
never write a foreign row.

Adapted from the Phase-17 reference implementation (all-project-files.zip);
the store-side helpers this module relies on (``get_request_by_published_task``,
``owner_edit_payload``, ``owner_append_history_event``) are already present
in ``task_request_store`` on this branch.

``TaskRequestError`` is the only exception path; ``task_routes`` maps
``str(exc)`` to a user-facing response by comparing against the
``*_ENGLISH`` identity keys below — keep them stable.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

logger = logging.getLogger(__name__)


# ── Exceptions / stable message identifiers ───────────────────────────────
# ``task_routes`` compares ``str(exc)`` to these in equality checks, so
# they must stay stable.  The non-ENGLISH names are display strings
# surfaced to the Mini App (Arabic, matching the codebase).

class TaskRequestError(Exception):
    """Raised by owner-management mutators when a guard is lost.

    Translated by ``task_routes`` to a user-facing response code.  The
    payload is the English identity string that routes key off.
    """


# Stable identity keys (English).  Routes compare ``str(exc)`` to these.
MSG_NOT_OWNER_ENGLISH = "not_the_owner_of_this_published_task"
MSG_CANNOT_EDIT_PAUSED_ENGLISH = "cannot_edit_while_paused"
MSG_INVALID_FIELD_ENGLISH = (
    "Only title and description may be edited by the owner.  "
    "Provider, action and target_ref edits are an admin-review decision "
    "and can never be changed here.  // "
    "يمكن تعديل العنوان والوصف فقط من قبل صاحب المهمة؛ "
    "تعديلات المنصة والإجراء والمرجع هي قرار إداري ولا يمكن تغييرها هنا."
)
MSG_INVALID_PAYLOAD_ENGLISH = "invalid_edit_payload"


# ── Persistence (task_request_store's Phase-17 helpers) ───────────────────
# The mutating writes below use ``task_request_store``'s owner helpers:
# ``owner_edit_payload`` (CAS-guarded on user_id + approved status) and
# ``owner_append_history_event`` (append-only audit).  The import is
# guarded so a degenerate import failure degrades to the safe
# invalid-payload error instead of an unhandled crash; every write path
# checks the helper exists before mutating anything.

try:
    import task_request_store as _store
except Exception:  # pragma: no cover - degenerate import failure
    _store = None  # type: ignore[assignment]


def _require_store() -> object:
    if _store is None:
        raise TaskRequestError(MSG_INVALID_PAYLOAD_ENGLISH)
    return _store


# ── Read-only lifecycle views (participation gate + Mini App flags) ────────

def participation_block(task_id: int) -> str | None:  # noqa: A003
    """Whether NEW participation on a published task should be blocked.

    Returns ``None`` (open), ``"paused"``, or ``"archived"``.
    Ownership-blind: a worker learns only the state, never who owns
    the task.
    """
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
        return None
    store = _require_store()
    req = getattr(store, "get_request_by_published_task", None)
    if req is None:  # pragma: no cover - defensive
        return None
    row = req(task_id)
    if row is None:
        return None
    # ``None`` means OPEN.  ``task_routes`` treats any non-None answer
    # as "block" (the catalog ``is not None: continue`` filter and the
    # paused/archived worker gates), so a running request must answer
    # None — only the two blocking markers are surfaced.
    state = _lifecycle_state(row)
    return None if state == "running" else state


def request_is_paused(req) -> bool:
    """Read-only view: is this (approved) request owner-paused."""
    if req is None:
        return False
    return _lifecycle_state(req) == "paused"


def request_is_archived(req) -> bool:
    """Read-only view: is this request logically hidden from the owner's
    list / from catalog discovery."""
    if req is None:
        return False
    return _lifecycle_state(req) == "archived"


def _lifecycle_state(req) -> str:  # private
    """Derive paused/archived/running from the append-only history.

    The request ``status`` column keeps its approved value — these are
    operational markers only.  A request whose history carries no owner
    event (or is corrupt) is conservatively ``"running"``.
    """
    history = getattr(req, "history", None)
    if history is None or not isinstance(history, list):
        return "running"
    state = "running"
    for item in history:
        if not isinstance(item, dict):
            continue
        event = item.get("event")
        if event == "owner_pause":
            state = "paused"
        elif event == "owner_resume":
            state = "running"
        elif event == "owner_archive":
            state = "archived"
    return state


# ── Ownership lookup (used by owner_get_task + shared by mutators) ─────────

def _owner_published_pair(
    conn: "sqlite3.Connection",
    user_id: int,
    published_task_id: int,
):
    """Return ``(request, task_id)`` for the caller's OWN published
    request, or ``(None, None)`` when the task does not belong to them
    (or does not exist / is not published under that id).

    THIS is the server-side ownership check: identity comes from the
    verified initData user id handed down by the route, and a foreign
    task is indistinguishable from "not found" for this caller.
    """
    store = _store
    if store is None:
        return (None, None)
    get = getattr(store, "get_request_by_published_task", None)
    if get is None:  # pragma: no cover - defensive
        return (None, None)
    req = get(published_task_id)
    if req is None:
        return (None, None)
    if getattr(req, "user_id", None) != user_id:
        return (None, None)
    tid = getattr(req, "published_task_id", None)
    if not isinstance(tid, int) or isinstance(tid, bool):
        return (None, None)
    return (req, tid)


# ── Owner mutators (edit / pause / resume / archive) ──────────────────────

# The ONLY fields an owner may edit on their own published request.
OWNER_EDITABLE_FIELDS = frozenset({"title", "description"})


def apply_edit(
    conn: "sqlite3.Connection",
    user_id: int,
    published_task_id: int,
    body: dict,
):
    """Owner edits the title/description of their OWN published request.

    Guards, in order: verified ownership → not paused → whitelist →
    value validation.  Provider/action/target_ref are never writable
    here (admin-review path only), and financial fields never exist in
    the payload write path at all.  The store write re-guards
    ownership + ``status = approved`` (CAS), so a raced request can
    never write a foreign row.
    """
    store = _store
    if store is None:
        raise TaskRequestError(MSG_INVALID_PAYLOAD_ENGLISH)
    try:
        request, task_id = _owner_published_pair(
            conn, user_id, published_task_id
        )
    except Exception:  # noqa: S110
        logger.exception("owner_edit pair lookup failed")
        raise TaskRequestError(MSG_INVALID_PAYLOAD_ENGLISH)

    if request is None or task_id is None:
        raise TaskRequestError(MSG_NOT_OWNER_ENGLISH)

    if _lifecycle_state(request) == "paused":
        raise TaskRequestError(MSG_CANNOT_EDIT_PAUSED_ENGLISH)

    rejected = set(body) - OWNER_EDITABLE_FIELDS
    if rejected:
        raise TaskRequestError(MSG_INVALID_FIELD_ENGLISH)

    title = body.get("title")
    description = body.get("description")
    if not isinstance(title, str) or not title.strip():
        raise TaskRequestError(MSG_INVALID_PAYLOAD_ENGLISH)
    if not isinstance(description, str) or not description.strip():
        raise TaskRequestError(MSG_INVALID_PAYLOAD_ENGLISH)

    payload = {"title": title.strip(), "description": description.strip()}
    # Keep the existing server-side shape for any fields we are not
    # touching (provider/action/target_ref) untouched.
    existing = getattr(request, "payload", None)
    if isinstance(existing, dict):
        payload = {**existing, **payload}

    updated = getattr(store, "owner_edit_payload", None)
    if updated is None:  # pragma: no cover - defensive
        raise TaskRequestError(MSG_INVALID_PAYLOAD_ENGLISH)
    result = updated(
        conn, getattr(request, "request_id", None), user_id, payload
    )
    if result is None:
        raise TaskRequestError(MSG_INVALID_PAYLOAD_ENGLISH)
    return result


def apply_pause(
    conn: "sqlite3.Connection", user_id: int, published_task_id: int
):
    """Owner pause (idempotent): re-pausing a paused request is a quiet
    success."""
    store = _store
    if store is None:
        raise TaskRequestError(MSG_INVALID_PAYLOAD_ENGLISH)
    request, _tid = _owner_published_pair(conn, user_id, published_task_id)
    if request is None or _tid is None:
        raise TaskRequestError(MSG_NOT_OWNER_ENGLISH)
    if _lifecycle_state(request) == "paused":
        return request
    append = getattr(store, "owner_append_history_event", None)
    if append is None:  # pragma: no cover - defensive
        raise TaskRequestError(MSG_INVALID_PAYLOAD_ENGLISH)
    result = append(
        conn, getattr(request, "request_id", None), user_id, "owner_pause"
    )
    if result is None:
        raise TaskRequestError(MSG_INVALID_PAYLOAD_ENGLISH)
    return result


def apply_resume(
    conn: "sqlite3.Connection", user_id: int, published_task_id: int
):
    """Owner resume (idempotent): resuming a running request is a quiet
    success."""
    store = _store
    if store is None:
        raise TaskRequestError(MSG_INVALID_PAYLOAD_ENGLISH)
    request, _tid = _owner_published_pair(conn, user_id, published_task_id)
    if request is None or _tid is None:
        raise TaskRequestError(MSG_NOT_OWNER_ENGLISH)
    if _lifecycle_state(request) == "running":
        return request
    append = getattr(store, "owner_append_history_event", None)
    if append is None:  # pragma: no cover - defensive
        raise TaskRequestError(MSG_INVALID_PAYLOAD_ENGLISH)
    result = append(
        conn, getattr(request, "request_id", None), user_id, "owner_resume"
    )
    if result is None:
        raise TaskRequestError(MSG_INVALID_PAYLOAD_ENGLISH)
    return result


def apply_archive(
    conn: "sqlite3.Connection", user_id: int, published_task_id: int
):
    """Owner logical archive (idempotent): hides the request from the
    owner's Mini App list and from catalog discovery.  No delete, no
    financial mutation."""
    store = _store
    if store is None:
        raise TaskRequestError(MSG_INVALID_PAYLOAD_ENGLISH)
    request, _tid = _owner_published_pair(conn, user_id, published_task_id)
    if request is None or _tid is None:
        raise TaskRequestError(MSG_NOT_OWNER_ENGLISH)
    if _lifecycle_state(request) == "archived":
        return request
    append = getattr(store, "owner_append_history_event", None)
    if append is None:  # pragma: no cover - defensive
        raise TaskRequestError(MSG_INVALID_PAYLOAD_ENGLISH)
    result = append(
        conn, getattr(request, "request_id", None), user_id, "owner_archive"
    )
    if result is None:
        raise TaskRequestError(MSG_INVALID_PAYLOAD_ENGLISH)
    return result
