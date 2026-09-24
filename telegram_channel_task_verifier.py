"""
Telegram Channel Task Verifier (MT-TASK-05)
===========================================

Automatic Telegram membership verification for the user-facing Manual
Telegram Channel Task family.

Task type:
    telegram_channel

This is a DISTINCT task family from ``channel_subscription``:

    channel_subscription  = mandatory platform/channel requirement gate
    telegram_channel      = ordinary user-facing task

Both verify Telegram membership, but they are different task families
and must remain separately registered.

Task data contract (server-set, trusted, validated on every read):

    {
        "provider": "telegram",
        "action": "join_channel",
        "target": {
            "channel_slug": "<configured channel slug>"
        },
        "instructions": "<task-specific instructions>"
    }

Reward is deliberately NOT part of task_data — it stays in
``tasks.reward`` and is settled downstream by TaskRewardService.

Flow:
    TaskStartGate → TaskAttemptPolicy → TaskSubmissionService
        → VerificationContext (immutable)
        → TelegramChannelTaskVerifier.verify(context)
        → VerificationResult(PASSED | FAILED | ERROR)
        → CompletionGate → TaskRewardService   (downstream, never here)

Status mapping (existing verifier contract):
    - valid member (member / administrator / creator)   → PASSED
    - not a valid member (left / kicked / restricted)   → FAILED
    - malformed task definition, unconfigured channel,
      Telegram API / configuration failure              → ERROR

Security:
    - The expected channel comes ONLY from the trusted server-side
      task definition (``tasks.task_data``) — never from client
      submission data, and never from a client-supplied channel id,
      username, slug, join URL, "verified" or "is_member" flag.
    - The slug resolves through the existing channel configuration
      (``config.get_channel``); no second channel configuration
      system and no duplicated channel data.
    - Targets are channel slugs only — URLs, @usernames and numeric
      ids are rejected as unsafe/ambiguous; the domain expects a
      configured channel identifier.
    - This module NEVER mutates tasks, user_tasks, task_submissions,
      wallets or ledgers; it never calls CompletionGate and never
      grants rewards.  Verification is side-effect free.
"""

from __future__ import annotations

import json
import logging
import re

from telegram.error import TelegramError

import db
from config import get_channel
from subscription import _is_chat_member
from task_completion import VerificationResult, VerificationStatus
from task_verifier import TaskVerifier, VerificationContext, register_verifier

logger = logging.getLogger(__name__)

# The single documented task type this verifier handles.  No aliases.
# Deliberately NOT "channel_subscription" — the families stay separate.
TELEGRAM_CHANNEL_TASK_TYPE = "telegram_channel"

# ── Server-side task_data contract (MT-TASK-05) ─────────────────────

TELEGRAM_PROVIDER = "telegram"
JOIN_CHANNEL_ACTION = "join_channel"
TARGET_KEY = "target"
CHANNEL_SLUG_KEY = "channel_slug"
INSTRUCTIONS_KEY = "instructions"

# Strict whitelists: anything else in the definition is ambiguous and
# rejected (including "reward", which lives in tasks.reward only).
ALLOWED_TASK_DATA_KEYS = frozenset(
    { "provider", "action", "target", "instructions" }
)
ALLOWED_TARGET_KEYS = frozenset({CHANNEL_SLUG_KEY})

# A target must be a configured-channel slug: letters, digits and
# underscore only.  URLs, @usernames, whitespace and numeric Telegram
# ids never match, so they can never slip through as "targets".
MAX_SLUG_LENGTH = 64
_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")

# Instructions are presentation data with a hard bound.
MAX_INSTRUCTIONS_LENGTH = 1000


class TelegramChannelTaskDataError(ValueError):
    """The task_data violates the telegram_channel server-side contract."""


def validate_telegram_channel_task_data(task_data: object) -> dict:
    """Validate a parsed telegram_channel task_data object.

    Enforces the full server-side contract: provider, action, target
    and instructions must all be present and well-formed; nothing else
    is accepted.

    Args:
        task_data: the parsed (JSON-decoded) task definition payload.

    Returns:
        The same dict, validated.

    Raises:
        TelegramChannelTaskDataError: on any contract violation —
        missing/wrong provider, missing/wrong action, missing/malformed
        target, missing/unsafe channel identifier, missing/malformed
        instructions, unexpected keys, or a non-object payload.
    """
    if not isinstance(task_data, dict):
        raise TelegramChannelTaskDataError("task_data must be a JSON object")

    unexpected = set(task_data) - ALLOWED_TASK_DATA_KEYS
    if unexpected:
        if "reward" in unexpected:
            raise TelegramChannelTaskDataError(
                "reward must not appear in task_data (reward lives in "
                "tasks.reward)"
            )
        raise TelegramChannelTaskDataError(
            "unexpected task_data keys: "
            + ", ".join(sorted(str(k) for k in unexpected))
        )

    # ── provider ────────────────────────────────────────────────
    if "provider" not in task_data:
        raise TelegramChannelTaskDataError("missing 'provider' in task_data")
    provider = task_data["provider"]
    if provider != TELEGRAM_PROVIDER:
        raise TelegramChannelTaskDataError(
            f"'provider' must be '{TELEGRAM_PROVIDER}', got {provider!r}"
        )

    # ── action ──────────────────────────────────────────────────
    if "action" not in task_data:
        raise TelegramChannelTaskDataError("missing 'action' in task_data")
    action = task_data["action"]
    if action != JOIN_CHANNEL_ACTION:
        raise TelegramChannelTaskDataError(
            f"'action' must be '{JOIN_CHANNEL_ACTION}', got {action!r}"
        )

    # ── target ──────────────────────────────────────────────────
    if "target" not in task_data:
        raise TelegramChannelTaskDataError("missing 'target' in task_data")
    target = task_data["target"]
    if not isinstance(target, dict):
        raise TelegramChannelTaskDataError("'target' must be a JSON object")

    extra_target = set(target) - ALLOWED_TARGET_KEYS
    if extra_target:
        raise TelegramChannelTaskDataError(
            "unexpected target keys: "
            + ", ".join(sorted(str(k) for k in extra_target))
            + " — a target is a configured channel_slug only "
            "(no channel ids, usernames or join URLs)"
        )

    if CHANNEL_SLUG_KEY not in target:
        raise TelegramChannelTaskDataError(
            "missing channel identifier 'target.channel_slug'"
        )
    slug = target[CHANNEL_SLUG_KEY]
    if not isinstance(slug, str):
        raise TelegramChannelTaskDataError(
            "'target.channel_slug' must be a string"
        )
    if not _SLUG_PATTERN.fullmatch(slug):
        raise TelegramChannelTaskDataError(
            "'target.channel_slug' is not a safe channel slug "
            "(letters, digits, underscore only — URLs, @usernames and "
            "numeric ids are rejected)"
        )

    # ── instructions ────────────────────────────────────────────
    if INSTRUCTIONS_KEY not in task_data:
        raise TelegramChannelTaskDataError(
            "missing 'instructions' in task_data"
        )
    instructions = task_data[INSTRUCTIONS_KEY]
    if not isinstance(instructions, str):
        raise TelegramChannelTaskDataError("'instructions' must be a string")
    if not instructions.strip():
        raise TelegramChannelTaskDataError("'instructions' must not be empty")
    if len(instructions) > MAX_INSTRUCTIONS_LENGTH:
        raise TelegramChannelTaskDataError(
            f"'instructions' exceeds {MAX_INSTRUCTIONS_LENGTH} characters"
        )

    return task_data


def parse_telegram_channel_task_data(raw: object) -> dict:
    """JSON-parse a task row's ``task_data`` column and validate it.

    Raises:
        TelegramChannelTaskDataError: missing/blank/non-string/
        non-JSON payload, or any contract violation.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise TelegramChannelTaskDataError("task_data is missing")
    if not isinstance(raw, str):
        raise TelegramChannelTaskDataError("task_data must be a JSON string")
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise TelegramChannelTaskDataError(
            f"task_data is not valid JSON: {exc}"
        ) from exc
    return validate_telegram_channel_task_data(parsed)


def task_channel_slug(task: dict | None) -> str | None:
    """Trusted channel slug of a ``telegram_channel`` task row, or None.

    Safe non-raising accessor used by the task API to resolve the
    public join destination from the server-side definition.  Returns
    None whenever the row or its task_data is unusable — client input
    is never involved.
    """
    if not isinstance(task, dict):
        return None
    try:
        data = parse_telegram_channel_task_data(task.get("task_data"))
    except TelegramChannelTaskDataError:
        return None
    return data[TARGET_KEY][CHANNEL_SLUG_KEY]


# ── Telegram membership lookup (default, injectable for tests) ───────


def _default_membership_checker(channel_id: int, user_id: int) -> str:
    """Live Telegram lookup of *user_id*'s status in *channel_id*.

    Uses the exact same ``Bot.get_chat_member`` mechanism as the
    existing subscription gate and the channel task verifier — one
    membership implementation, not two.  Imported lazily so
    module-import order can never break verifier registration.
    Raises on any configuration/Telegram failure; the verifier maps
    those to VerificationResult(ERROR).
    """
    from channel_task_verifier import _fetch_member_status

    return _fetch_member_status(channel_id, user_id)


# ── Verifier ─────────────────────────────────────────────────────────


class TelegramChannelTaskVerifier(TaskVerifier):
    """Verifies Telegram channel membership for one telegram_channel task.

    Verification component only: receives the immutable
    VerificationContext, reads and validates the trusted server-side
    task definition, resolves the configured channel, checks live
    membership, and returns a VerificationResult.  It performs no
    writes of any kind and never calls CompletionGate — completion and
    reward remain entirely downstream.
    """

    def __init__(self, membership_checker=None) -> None:
        """Create a verifier.

        Args:
            membership_checker: optional ``f(channel_id, user_id) ->
                telegram_chat_member_status`` used instead of the
                default live Telegram lookup.  Tests inject fakes here
                so no real Telegram API call is ever made.
        """
        self._membership_checker = membership_checker or _default_membership_checker

    def verify(self, context: VerificationContext) -> VerificationResult:
        # 1. The trusted server-side task definition is the ONLY source
        #    of the expected channel.  context.actual_data / any
        #    client-supplied channel field is deliberately ignored.
        task = db.get_task(context.task_id)
        if task is None:
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason=f"Task {context.task_id} not found",
            )

        # 2. Validate the server-side task_data contract.  Malformed,
        #    wrong-provider, wrong-action or unsafe-target definitions
        #    are configuration errors — ERROR, never a pass.
        try:
            task_data = parse_telegram_channel_task_data(task.get("task_data"))
        except TelegramChannelTaskDataError as exc:
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason=f"invalid telegram_channel task_data: {exc}",
            )
        slug = task_data[TARGET_KEY][CHANNEL_SLUG_KEY]

        # 3. Resolve through the existing channel configuration.
        channel = get_channel(slug)
        if channel is None:
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason=f"channel '{slug}' is not configured",
            )

        # 4. Membership check through the existing Telegram mechanism.
        try:
            status = self._membership_checker(channel.channel_id, context.user_id)
        except TelegramError as exc:
            logger.warning(
                "Telegram membership check failed: user=%d channel=%s — %s",
                context.user_id, slug, exc,
            )
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason=f"Telegram membership check failed: {exc}",
            )
        except Exception as exc:
            logger.warning(
                "Membership verification failed: user=%d channel=%s — %s",
                context.user_id, slug, exc,
            )
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason=f"membership verification failed: {exc}",
            )

        if not isinstance(status, str):
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason=f"membership check returned invalid status: "
                       f"{type(status).__name__}",
            )

        # 5. Same membership semantics as the subscription gate and the
        #    channel_subscription task family (channels and supergroups
        #    alike): member / administrator / creator count.
        if _is_chat_member(status):
            logger.info(
                "Telegram channel task verified: user=%d task=%d "
                "channel=%s status=%s",
                context.user_id, context.task_id, slug, status,
            )
            return VerificationResult(status=VerificationStatus.PASSED)

        return VerificationResult(
            status=VerificationStatus.FAILED,
            reason=f"user {context.user_id} is not a member of '{slug}' "
                   f"(status={status})",
        )


# ── Registration ─────────────────────────────────────────────────────


def register_telegram_channel_task_verifier(
    verifier: TaskVerifier | None = None,
) -> None:
    """(Re)register the telegram channel task verifier in the registry.

    Called once on import and available to tests (which may clear the
    registry in setUp/teardown, matching existing test conventions).
    """
    register_verifier(
        TELEGRAM_CHANNEL_TASK_TYPE,
        verifier or TelegramChannelTaskVerifier(),
    )


# Importing this module registers telegram_channel →
# TelegramChannelTaskVerifier; task_verifier also triggers this module
# on import so the central registry entry point resolves the type.
register_telegram_channel_task_verifier()
