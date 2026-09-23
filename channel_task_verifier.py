"""
Channel Task Verifier (MT-TASK-02)
==================================

Automatic Telegram channel/supergroup membership verification for the
first production Task family: the Mandatory Channel Task.

Task type:
    channel_subscription

Task data contract (admin-set, server-side, trusted):

    {"channel_slug": "<configured required channel slug>"}

Flow:
    TaskStartGate → TaskAttemptPolicy → TaskSubmissionService
        → VerificationContext (immutable)
        → ChannelTaskVerifier.verify(context)
        → VerificationResult(PASSED | FAILED | ERROR)
        → CompletionGate (only a PASSED result ever reaches it)

Verification rules:
    - The expected ``channel_slug`` is read from the trusted server-side
      task definition (``tasks.task_data``) — never from client
      submission data, and never from any client-supplied channel id,
      username, title, "verified", or "is_member" flag.
    - The slug resolves through the existing required-channel
      configuration (``config.get_channel``); no second channel
      configuration system is introduced.
    - Membership uses the existing Telegram membership semantics
      (``subscription._is_chat_member``): statuses member / administrator
      / creator count as subscribed — identical for channels and
      supergroups, exactly like the subscription gate.

Status mapping (existing verifier contract):
    - valid member                       → PASSED
    - not a valid member                 → FAILED
    - configuration / infrastructure
      error (malformed task_data, unknown
      channel, Telegram API failure)     → ERROR

Security:
    - This module NEVER mutates tasks, user_tasks, wallets, ledgers,
      or channel configuration — verification only.
    - It never calls CompletionGate and never marks a task completed.
    - Client input can never determine the channel under check.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading

from telegram import Bot
from telegram.error import TelegramError

import db
from config import get_channel
from subscription import _is_chat_member
from task_completion import VerificationResult, VerificationStatus
from task_verifier import TaskVerifier, VerificationContext, register_verifier

logger = logging.getLogger(__name__)

# The single documented task type this verifier handles.  No aliases.
CHANNEL_TASK_TYPE = "channel_subscription"

# The only trusted key inside the task's task_data payload.
CHANNEL_SLUG_KEY = "channel_slug"


# ── Telegram membership lookup (default, injectable for tests) ────


def _run_coroutine_sync(coro):
    """Run *coro* to completion from synchronous code.

    Normally there is no running event loop (the submission pipeline is
    synchronous), so ``asyncio.run`` is used directly.  If verify() is
    ever invoked from inside a running loop, the coroutine is executed
    on a short-lived dedicated thread with its own loop instead of
    raising "asyncio.run() cannot be called from a running event loop".
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    outcome: list = []

    def _target() -> None:
        try:
            outcome.append(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            outcome.append(exc)

    worker = threading.Thread(target=_target, name="channel-membership-check")
    worker.start()
    worker.join()
    result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    return result


def _fetch_member_status(channel_id: int, user_id: int) -> str:
    """Live Telegram lookup of *user_id*'s status in *channel_id*.

    Uses the same ``Bot.get_chat_member`` mechanism as the existing
    subscription gate.  Raises on any configuration/Telegram failure —
    the verifier maps those to VerificationResult(ERROR).
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set")

    async def _lookup() -> str:
        bot = Bot(token=token)
        try:
            member = await bot.get_chat_member(channel_id, user_id)
            return member.status
        finally:
            await bot.close()

    return _run_coroutine_sync(_lookup())


# ── Verifier ──────────────────────────────────────────────────────


class ChannelTaskVerifier(TaskVerifier):
    """Verifies mandatory Telegram channel membership for one task.

    Verification component only: receives the immutable
    VerificationContext, reads the trusted server-side task definition,
    resolves the configured channel, checks live membership, and
    returns a VerificationResult.  It performs no writes of any kind
    and never calls CompletionGate.
    """

    def __init__(self, membership_checker=None) -> None:
        """Create a verifier.

        Args:
            membership_checker: optional ``f(channel_id, user_id) ->
                telegram_chat_member_status`` used instead of the default
                live Telegram lookup.  Tests inject fakes here so no real
                Telegram API call is ever made.
        """
        self._membership_checker = membership_checker or _fetch_member_status

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

        raw_task_data = task.get("task_data") or ""
        try:
            task_data = json.loads(raw_task_data) if raw_task_data else {}
        except (ValueError, TypeError):
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason="task_data is not valid JSON",
            )
        if not isinstance(task_data, dict):
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason="task_data is not a dict",
            )

        # 2. Validate the task data contract: {"channel_slug": "..."}.
        slug = task_data.get(CHANNEL_SLUG_KEY)
        if not isinstance(slug, str) or not slug.strip():
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason=f"missing or invalid '{CHANNEL_SLUG_KEY}' in task_data",
            )

        # 3. Resolve through the existing required-channel configuration.
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

        # 5. Same subscribed/not-subscribed semantics as the
        #    subscription gate (channels and supergroups alike).
        if _is_chat_member(status):
            logger.info(
                "Channel task verified: user=%d task=%d channel=%s status=%s",
                context.user_id, context.task_id, slug, status,
            )
            return VerificationResult(status=VerificationStatus.PASSED)

        return VerificationResult(
            status=VerificationStatus.FAILED,
            reason=f"user {context.user_id} is not a member of '{slug}' "
                   f"(status={status})",
        )


# ── Registration ──────────────────────────────────────────────────


def register_channel_task_verifier(
    verifier: TaskVerifier | None = None,
) -> None:
    """(Re)register the channel task verifier in the existing registry.

    Called once on import and available to tests (which may clear the
    registry in setUp/teardown, matching existing test conventions).
    """
    register_verifier(CHANNEL_TASK_TYPE, verifier or ChannelTaskVerifier())


# Importing this module registers channel_subscription →
# ChannelTaskVerifier; task_verifier also triggers this module on
# import so the central registry entry point resolves the type.
register_channel_task_verifier()
