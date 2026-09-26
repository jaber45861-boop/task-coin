"""
Canonical task creation service (MT-ADMIN-05)
=============================================

The ONE place a validated ``TaskSpec`` becomes a ``tasks`` row.

Every creation path delegates here — the wizard's confirm step and the
legacy ``/addtask title | description | points | channel_slug`` pipe —
so the product never keeps two independent task-creation
implementations with drifting validation.

What it guarantees on every call:
- title / description bounds + control-character safety
- provider / action / verification compatibility (capability-based):
  * ``auto``     → ONLY the existing Telegram membership verifier;
                   produces the exact MT-TASK-05 ``telegram_channel``
                   contract, additionally requiring the slug to exist
                   in the configured channel registry.
  * ``manual`` / ``approval`` → the existing MT-TASK-15 approval-gated
                   ``manual`` contract (task-specific approver remains
                   the sole decision authority).
- reward / repeat policy are the EXISTING ``tasks.reward`` fields with
  the EXISTING ``db.validate_repeat_policy`` rules — no new currency,
  no new repeat mode, no wallet/ledger involvement here.
- the produced task_data is proven by the very validator the reader
  side uses (``validate_telegram_channel_task_data`` /
  ``validate_manual_task_data``): the writer can never persist a
  contract the reader rejects.

``conn=`` lets the wizard publish inside its own ``db.transaction()``
(claim draft → create task → mark draft, atomically); with ``conn``
None the classic self-contained behavior is used.

Boundaries (this module must NOT):
- send Telegram messages, touch drafts/callbacks (admin_task_wizard
  owns that), or decide/approve claims (ManualReviewService owns that)
- mutate wallets/ledger or complete tasks
- invent provider APIs or verifiers
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import db
from config import CHANNELS
from manual_task import MANUAL_TASK_TYPE, validate_manual_task_data
from task_taxonomy import (
    ACTION_JOIN_CHANNEL,
    GENERIC_PROVIDER_SET,
    MANUAL_TASK_ACTIONS,
    PROVIDER_TELEGRAM,
    VERIFICATION_APPROVAL,
    VERIFICATION_AUTO,
    VERIFICATION_MANUAL,
    VERIFICATION_MODE_SET,
    validate_instructions,
    validate_title,
)
from telegram_channel_task_verifier import (
    JOIN_CHANNEL_ACTION,
    TELEGRAM_CHANNEL_TASK_TYPE,
    validate_telegram_channel_task_data,
)

logger = logging.getLogger(__name__)

class TaskCreationError(ValueError):
    """The spec cannot become a persisted task.

    Message is admin-displayable Arabic; the draft (if any) stays open
    so the admin can correct it.  Never carries internals.
    """


@dataclass(frozen=True)
class TaskSpec:
    """Fully-resolved task definition — server-side, validated here.

    Built by the wizard from its PERSISTED draft (never from callback
    data) or by the legacy /addtask parser.  ``target`` is the shaped
    task_data target: ``{"channel_slug": ...}`` for auto Telegram joins
    or ``{"ref": ...}`` (``label`` optional) for generic tasks.
    """

    title: str
    description: str
    provider: str
    action: str
    target: dict
    verification: str
    reward: int
    approver_id: int | None = None
    repeat_policy: str = db.REPEAT_POLICY_ONE_TIME
    repeat_hours: int | None = None


def build_task_definition(spec: TaskSpec) -> tuple[str, str]:
    """Validate the whole spec and build ``(task_type, task_data_json)``.

    Raises:
        TelegramChannelTaskDataError / ManualTaskDataError: the exact
            reader-side contract violations (ValueError subclasses).
        TaskCreationError: capability/shape violations (wrong
            verification for the provider, unregistered channel slug,
            missing approver, bad reward …).
        ValueError: title/bounds violations.
    """
    # ── Shared shape checks (defense in depth) ────────────────────────
    title = validate_title(spec.title)  # noqa: F841 — validated shape
    if spec.verification not in VERIFICATION_MODE_SET:
        raise TaskCreationError(
            f"unsupported verification mode: {spec.verification!r}"
        )
    if not isinstance(spec.provider, str) or (
        spec.provider not in GENERIC_PROVIDER_SET
    ):
        raise TaskCreationError(f"unsupported provider: {spec.provider!r}")
    if not isinstance(spec.action, str) or (
        spec.action not in MANUAL_TASK_ACTIONS
    ):
        raise TaskCreationError(f"unsupported action: {spec.action!r}")
    if (
        isinstance(spec.reward, bool)
        or not isinstance(spec.reward, int)
        or spec.reward < 0
    ):
        raise TaskCreationError("reward must be a non-negative integer")

    # ── auto: the EXISTING Telegram membership verifier only ──────────
    if spec.verification == VERIFICATION_AUTO:
        if (
            spec.provider != PROVIDER_TELEGRAM
            or spec.action != JOIN_CHANNEL_ACTION
            or spec.action != ACTION_JOIN_CHANNEL
        ):
            raise TaskCreationError(
                "التحقق التلقائي متاح فقط لمهام انضمام قنوات Telegram"
            )
        if not isinstance(spec.target, dict):
            raise TaskCreationError("بيانات هدف القناة غير صالحة")
        slug = spec.target.get("channel_slug")
        if not isinstance(slug, str) or not slug.strip():
            raise TaskCreationError("معرّف القناة (channel_slug) مطلوب")
        slug = slug.strip().lstrip("@")
        if slug not in CHANNELS:
            raise TaskCreationError(
                f"الـ channel_slug '{slug}' غير موجود في سجل القنوات."
            )
        payload = {
            "provider": spec.provider,
            "action": spec.action,
            "target": {"channel_slug": slug},
            "instructions": spec.description,
        }
        # The reader's own validator — writer/reader share ONE contract.
        validate_telegram_channel_task_data(payload)
        return TELEGRAM_CHANNEL_TASK_TYPE, json.dumps(
            payload, ensure_ascii=False
        )

    # ── manual / approval: the EXISTING approval-gated MT-TASK-15 ─────
    if spec.verification not in (VERIFICATION_MANUAL, VERIFICATION_APPROVAL):
        raise TaskCreationError(
            f"unsupported verification mode: {spec.verification!r}"
        )
    if (
        isinstance(spec.approver_id, bool)
        or not isinstance(spec.approver_id, int)
        or spec.approver_id <= 0
    ):
        raise TaskCreationError(
            "يجب تحديد مراجع مختص (approver) للمهمة"
        )
    # Worker instructions land in tasks.description for manual tasks
    # (that is where the existing TaskCatalog/API exposes them); the
    # manual contract itself carries provider/action/target/approver.
    validate_instructions(spec.description)
    payload = {
        "provider": spec.provider,
        "action": spec.action,
        "approver": {"telegram_user_id": spec.approver_id},
    }
    if isinstance(spec.target, dict) and spec.target:
        payload["target"] = dict(spec.target)
    validate_manual_task_data(payload)
    return MANUAL_TASK_TYPE, json.dumps(payload, ensure_ascii=False)


def create_task_from_spec(
    spec: TaskSpec, *, conn=None
) -> int:
    """Validate the spec, then create exactly one task. Returns its id.

    All validation happens BEFORE any write: a rejected spec creates
    zero rows.  With ``conn`` the INSERT joins the caller's open
    transaction (wizard publish CAS); without it this is a classic
    self-contained creation.
    """
    task_type, task_data = build_task_definition(spec)
    task_id = db.create_task(
        spec.title,
        spec.description,
        task_type,
        spec.reward,
        task_data=task_data,
        repeat_policy=spec.repeat_policy,
        repeat_hours=spec.repeat_hours,
        conn=conn,
    )
    logger.info(
        "Task created from spec: id=%d type=%s provider=%s "
        "action=%s verification=%s reward=%d",
        task_id, task_type, spec.provider, spec.action,
        spec.verification, spec.reward,
    )
    return task_id
