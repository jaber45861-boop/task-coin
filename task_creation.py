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
- reward input goes through the ONE canonical exact parser
  (``parse_reward_units``, MT-ADMIN-14): decimal text → atomic
  ``reward_units`` int (the accounting authority written at creation);
  ``reward`` stays the whole-USDT compatibility/display field.
- the advertiser commission is resolved from the admin-mutable
  platform setting ``advertiser_commission`` on EVERY creation (fresh
  read, no global cache, no restart) and snapshotted as the exact
  atomic ``commission_units`` value for THIS task (MT-ADMIN-16):
  integer basis points (10,000 = 100 %), ceiling rounding, no float;
  a later setting change never alters an already-created task.
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
- write wallet/ledger SQL directly: advertiser funding goes through
  ``task_funding.fund_task`` — wallet reserve+settle, the matching
  ledger hold+settlement and the persisted ``task_funding`` record —
  inside the SAME transaction as this task's INSERT whenever the
  creation surface supplies its authenticated ``funding_advertiser_id``
  (roadmap 4).  With no advertiser (direct/system callers) creation
  stays the classic unfunded write, exactly as before.
- complete tasks
- invent provider APIs or verifiers
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import db
import platform_settings
import task_funding
import wallet
from task_funding import TaskFundingError
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
    normalize_digits,
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


# ── Canonical exact reward input (MT-ADMIN-14) ────────────────────────
# The ONE parser for every human reward input (legacy /addtask pipe,
# admin wizard, spec re-validation).  Exact decimal text → atomic
# integer units (1 USDT = 100,000,000): no float, no round(), no
# truncation — over-precision is REJECTED, never rounded.

_REWARD_TEXT_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")

# Mirrors db._SQLITE_INT64_MAX — the signed SQLite INTEGER bound the
# stored atomic value must fit (reward_units stays INTEGER, never REAL).
_SQLITE_INT64_MAX = 9_223_372_036_854_775_807


def _reward_invalid(field: str) -> ValueError:
    """Admin-displayable Arabic error for a malformed reward input."""
    return ValueError(
        f"❌ {field} يجب أن تكون قيمة USDT رقمية "
        f"(مثال: 1 أو 0.0001 — حتى 8 منازل عشرية)."
    )


def parse_reward_units(value: object, *, field: str = "المكافأة") -> int:
    """The canonical exact USDT reward input → atomic units.

    1 USDT = ``wallet.USDT_SCALE`` (100,000,000) atomic units; sub-cent
    rewards are exact.  The input stays a string until the exact
    integer conversion completes — this function never sees a float:

    Accepts:
        str  — exact decimal text up to 8 decimal places
               (``"1"``, ``"0.5"``, ``"0.00000001"``; Arabic-Indic
               digits are normalized first, matching the other
               admin-input parsers)
        int  — whole USDT for compatibility (``1`` → ``100_000_000``)

    Rejects (ValueError, admin-displayable Arabic message):
        bool / float / None / any other type, empty or whitespace-only
        text, negatives, more than 8 decimal places (never rounded),
        scientific notation (``"1e-8"``), NaN / Infinity, malformed
        text, and any atomic result outside the signed SQLite INTEGER
        range.

    Returns:
        int — exact atomic units (never rounded, never truncated).
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise _reward_invalid(field)
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"❌ {field} لا يمكن أن تكون سالبة.")
        units = value * wallet.USDT_SCALE
    elif isinstance(value, str):
        text = normalize_digits(value.strip())
        if not text:
            raise ValueError(f"❌ {field} لا يمكن أن تكون فارغة.")
        if text.startswith("-"):
            raise ValueError(f"❌ {field} لا يمكن أن تكون سالبة.")
        if not _REWARD_TEXT_RE.fullmatch(text):
            raise _reward_invalid(field)
        if "." in text and len(text.split(".", 1)[1]) > wallet.USDT_DECIMALS:
            raise ValueError(
                f"❌ {field}: الحد الأقصى {wallet.USDT_DECIMALS} "
                f"منازل عشرية بعد الفاصلة."
            )
        try:
            # Existing exact wallet primitive: Decimal text → int
            # units (float/bool/NaN/negative/precision-proof).
            units = wallet.decimal_to_units(text, field=field)
        except wallet.InvalidWalletAmountError as exc:
            raise _reward_invalid(field) from exc
    else:
        raise _reward_invalid(field)
    if units > _SQLITE_INT64_MAX:
        raise ValueError(f"❌ {field}: القيمة تتجاوز الحد المسموح.")
    return units


def whole_usdt_reward(units: int) -> int:
    """Whole-USDT compatibility display derived from exact units.

    ``tasks.reward`` keeps its legacy whole-USDT INTEGER meaning; the
    accounting value stays ``reward_units``.  Pure integer division
    (display only — never an accounting source).
    """
    return units // wallet.USDT_SCALE


def reward_units_to_text(units: int) -> str:
    """Exact display text for atomic units (integer math only).

    ``10000`` → ``"0.0001"``, ``100000000`` → ``"1"``,
    ``1`` → ``"0.00000001"``.  Display only, never accounting.
    """
    if isinstance(units, bool) or not isinstance(units, int) or units < 0:
        raise ValueError("reward_units must be a non-negative int")
    whole, fraction = divmod(units, wallet.USDT_SCALE)
    if not fraction:
        return str(whole)
    return f"{whole}.{fraction:0{wallet.USDT_DECIMALS}d}".rstrip("0")


def reward_payload_value(units: int) -> int | str:
    """Draft-payload form of exact units (admin wizard storage).

    Whole-USDT values stay the legacy ``int`` (existing payloads and
    callers keep their meaning); sub-cent values stay an exact decimal
    string — never a float, never rounded.
    """
    whole, fraction = divmod(units, wallet.USDT_SCALE)
    if not fraction:
        return whole
    return reward_units_to_text(units)


# ── Advertiser commission snapshot (MT-ADMIN-16) ────────────────────
# The commission RATE is admin-mutable DATA — platform_settings key
# ``advertiser_commission``, integer basis points on the MT-ADMIN-15
# scale (COMMISSION_SCALE = 10,000 bp = 100 %, 3000 bp = 30 %).  It is
# read fresh from SQLite on every creation (the settings service never
# caches), so an admin change applies to the NEXT task with no bot
# restart.  The value written to ``tasks.commission_units`` is the
# exact atomic commission for THIS task's reward, resolved ONCE at
# creation — an immutable snapshot: changing the setting later never
# alters an existing task.
#
# This snapshot is the ONLY commission amount funding ever charges:
# ``task_funding.fund_task`` reads ``tasks.commission_units`` back from
# the persisted row (never this setting) when it moves the advertiser's
# money — a later rate change cannot reprice an existing task.  The
# wallet movement itself lives in ``task_funding`` (roadmap 4),
# composed inside the creation transaction; this module only computes
# and snapshots the amount.  No float, no round(): exact Python int
# math.

def commission_units_for(reward_units: int, commission_bp: int) -> int:
    """Exact advertiser commission in atomic units for a reward.

    Integer math only — never a float, never ``round()``:

        commission_units = ceil(reward_units * commission_bp / 10,000)

    computed exactly as ``(r * bp + SCALE - 1) // SCALE`` (a ceiling,
    so the platform never under-collects — the same "round UP"
    convention as ``withdrawal_rules.egp_to_usdt``).  The rule is
    deterministic at every boundary:

    * an exact multiple is NEVER bumped (``r * bp`` divisible by
      10,000 → the quotient, unchanged);
    * a partial unit always rounds UP to exactly 1 unit
      (``commission_units_for(1, 3000) == 1``);
    * zero rate or zero reward → exactly 0;
    * ``commission_bp == 10000`` (100 %) → exactly ``reward_units``.

    Args:
        reward_units: exact atomic reward — non-negative ``int``.
        commission_bp: commission in basis points, ``0..10,000``.
            The caller resolves it from platform_settings
            (``get_required_setting``), which already range-checks;
            the guard here keeps this helper total and coercion-free.

    Returns:
        int — exact atomic commission units (always ≤ reward_units).

    Raises:
        ValueError: bool / float / negative / non-int / out-of-range
            input — rejected, never coerced, never rounded.
    """
    if (
        isinstance(reward_units, bool)
        or not isinstance(reward_units, int)
        or reward_units < 0
        or reward_units > _SQLITE_INT64_MAX
    ):
        raise ValueError(
            "reward_units must be a non-negative int of atomic units"
        )
    scale = platform_settings.COMMISSION_SCALE
    if (
        isinstance(commission_bp, bool)
        or not isinstance(commission_bp, int)
        or commission_bp < 0
        or commission_bp > scale
    ):
        raise ValueError(
            f"commission_bp must be an int in 0..{scale} basis points"
        )
    return (reward_units * commission_bp + scale - 1) // scale


@dataclass(frozen=True)
class TaskSpec:
    """Fully-resolved task definition — server-side, validated here.

    Built by the wizard from its PERSISTED draft (never from callback
    data) or by the legacy /addtask parser.  ``target`` is the shaped
    task_data target: ``{"channel_slug": ...}`` for auto Telegram joins
    or ``{"ref": ...}`` (``label`` optional) for generic tasks.

    ``reward`` is the whole-USDT compatibility/display int;
    ``reward_units`` (when given) is the exact atomic accounting value
    written to ``tasks.reward_units`` at creation (MT-ADMIN-14).
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
    reward_units: int | None = None


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
    if spec.reward_units is not None and (
        isinstance(spec.reward_units, bool)
        or not isinstance(spec.reward_units, int)
        or spec.reward_units < 0
        or spec.reward_units > _SQLITE_INT64_MAX
    ):
        raise TaskCreationError(
            "reward_units must be a non-negative integer within the "
            "SQLite INTEGER range"
        )

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
    spec: TaskSpec,
    *,
    conn=None,
    funding_advertiser_id: int | None = None,
) -> int:
    """Validate the spec, then create exactly one task. Returns its id.

    All validation happens BEFORE any write: a rejected spec creates
    zero rows.  With ``conn`` the INSERT joins the caller's open
    transaction (wizard publish CAS); without it this is a classic
    self-contained creation.

    MT-ADMIN-16: before the write, the advertiser commission is
    resolved from the runtime platform setting (fresh read on ``conn``
    when given, so the wizard's transaction reads its own snapshot)
    and computed into this task's exact ``commission_units`` snapshot.
    A missing setting raises ``platform_settings.SettingNotFoundError``
    BEFORE any row is written — explicit and safe, never a silent 0.

    Roadmap 4 — advertiser funding: when the creation surface supplies
    ``funding_advertiser_id`` (the AUTHENTICATED creator: the wizard's
    ownership-checked actor or the /addtask sender — never draft
    payload or callback data), the task is funded atomically with its
    INSERT: ``task_funding.fund_task`` charges ``reward_units +
    commission_units`` (the just-persisted immutable snapshot) from
    that advertiser's wallet — reserve → ledger hold → settle →
    ledger settlement → ``task_funding`` record — on the SAME
    connection.  Either everything commits (task exists, advertiser
    charged exactly once, ledger rows committed) or nothing does: an
    insufficient balance, an unknown advertiser or any financial write
    failure rolls the INSERT back and zero rows remain.  Without an
    advertiser (direct/system callers, legacy behavior) nothing is
    charged and creation behaves exactly as before.

    Raises:
        TaskCreationError: invalid spec, invalid funding advertiser, or
            the funding failure itself (insufficient balance / unknown
            account) — admin-displayable message, zero rows written.
    """
    task_type, task_data = build_task_definition(spec)

    # Funding identity is validated pre-write like every other input.
    # The value itself always comes from the authenticated creation
    # surface — this module never reads it from stored payloads.
    if funding_advertiser_id is not None and (
        isinstance(funding_advertiser_id, bool)
        or not isinstance(funding_advertiser_id, int)
        or funding_advertiser_id <= 0
    ):
        raise TaskCreationError(
            "funding advertiser must be a positive user id"
        )

    # Resolve the admin-mutable commission (no global cache: runtime
    # changes affect NEW creations only) and compute the snapshot —
    # still pre-write, so any failure leaves zero rows behind.
    commission_bp = platform_settings.get_required_setting(
        platform_settings.ADVERTISER_COMMISSION, conn=conn
    )
    effective_units = spec.reward_units
    if effective_units is None:
        # Legacy TaskSpec(reward=...) without explicit units: the same
        # exact whole-USDT derivation db.create_task applies (int math).
        effective_units = spec.reward * wallet.USDT_SCALE
    commission_units = commission_units_for(effective_units, commission_bp)

    if conn is None and funding_advertiser_id is not None:
        # Funding without a caller transaction: the INSERT and the
        # charge must commit or roll back as one unit, so this service
        # opens the ONE repository transaction itself (db.transaction —
        # never a second transaction helper).
        with db.transaction() as tx:
            task_id = _insert_and_fund(
                tx,
                spec,
                task_type,
                task_data,
                commission_units,
                advertiser_id=funding_advertiser_id,
            )
    else:
        task_id = _insert_and_fund(
            conn,
            spec,
            task_type,
            task_data,
            commission_units,
            advertiser_id=funding_advertiser_id,
        )
    logger.info(
        "Task created from spec: id=%d type=%s provider=%s "
        "action=%s verification=%s reward=%d reward_units=%s "
        "commission_units=%d (commission_bp=%d) funded_by=%s",
        task_id, task_type, spec.provider, spec.action,
        spec.verification, spec.reward, spec.reward_units,
        commission_units, commission_bp,
        funding_advertiser_id,
    )
    return task_id


def _insert_and_fund(
    conn,
    spec: TaskSpec,
    task_type: str,
    task_data: str,
    commission_units: int,
    *,
    advertiser_id: int | None,
) -> int:
    """INSERT the task on ``conn``, then fund it on that same conn.

    With no ``advertiser_id`` this is the classic unfunded INSERT
    (``conn`` may be None → the db layer's self-contained scope).
    With one, ``conn`` is ALWAYS the open transaction connection (the
    caller's or the one ``create_task_from_spec`` just opened), so a
    funding failure rolls the INSERT back with it.
    """
    task_id = db.create_task(
        spec.title,
        spec.description,
        task_type,
        spec.reward,
        task_data=task_data,
        repeat_policy=spec.repeat_policy,
        repeat_hours=spec.repeat_hours,
        conn=conn,
        reward_units=spec.reward_units,
        commission_units=commission_units,
    )
    if advertiser_id is not None:
        _fund_created_task(conn, task_id, advertiser_id)
    return task_id


def _fund_created_task(conn, task_id: int, advertiser_id: int) -> None:
    """Charge the advertiser inside the creation transaction.

    Translates the financial failure vocabulary into this module's
    admin-displayable ``TaskCreationError`` (Arabic, no internals) so
    the existing surfaces (wizard "تعذر النشر", /addtask
    "تعذر إنشاء المهمة") report it like any other creation rejection;
    the original exception stays chained on ``__cause__`` for logs and
    tests.  ``conn``'s transaction rolls every write back.
    """
    try:
        task_funding.fund_task(
            conn, task_id=task_id, advertiser_id=advertiser_id
        )
    except wallet.InsufficientBalanceError as exc:
        raise TaskCreationError(
            "رصيد المعلن غير كافٍ لتكلفة هذه المهمة "
            "(المكافأة + العمولة)."
        ) from exc
    except wallet.UserNotFoundError as exc:
        raise TaskCreationError(
            "حساب المعلن غير مسجّل في البوت — شغّل /start أولًا."
        ) from exc
    except wallet.WalletError as exc:
        raise TaskCreationError("تعذر تمويل المهمة من محفظة المعلن.") from exc
    except TaskFundingError as exc:
        raise TaskCreationError("تعذر تمويل المهمة.") from exc
