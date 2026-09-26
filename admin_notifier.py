"""
Centralized Admin Notification Channel (MT-ADMIN-02)
====================================================

The Telegram *private chat* is the operational/admin control plane for
this bot.  ``AdminNotifier`` is the single sanctioned abstraction for
pushing operational notifications, and it may deliver **exclusively**
to the admin user IDs configured in ``config.ADMINS`` — never to
required channels/groups or any other chat.

Design constraints (MT-ADMIN-02):

- **ADMINS-only targeting.**  Every delivery target must be a member of
  ``config.ADMINS``; anything else raises ``AdminNotifierTargetError``
  before any message is sent.  Required channels/groups can therefore
  never receive operational notifications through this path.
- **Authorization.**  ``notify()`` requires the acting user to hold
  admin authorization (``config.is_admin`` — the same semantics the
  rest of the bot uses).  Non-admin actors raise
  ``AdminNotifierAuthorizationError`` and nothing is sent.
- **Injected transport.**  The send function (``async (chat_id, text)``)
  is provided by the caller, so the notifier is fully testable without
  network access and carries no inbox/review semantics — those belong
  to MT-ADMIN-03.
- **Server-originated events (MT-ADMIN-03).**  ``notify_system()``
  delivers an operational notification on behalf of the server itself
  (a worker's manual-proof submission has no acting admin), with the
  same ADMINS-only targeting rules, an optional inline keyboard, and
  the resulting Telegram ``message_id`` returned for persistent
  linkage.  It grants no user any authority — decisions stay with the
  task-specific approver via the review services.

Usage::

    notifier = AdminNotifier(bot.send_message)
    await notifier.notify(actor_user_id, "Task 12 needs review.")
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional, Sequence

from config import ADMINS, is_admin

# Async transport: deliver *text* to the private chat *chat_id*.
SendFunc = Callable[[int, str], Awaitable[None]]
# Async markup transport: deliver *text* with an optional inline
# keyboard and return the Telegram message id (MT-ADMIN-03) so the
# caller can persist an operation → message linkage.
MarkupSendFunc = Callable[[int, str, Optional[object]], Awaitable[Optional[int]]]


class AdminNotifierError(Exception):
    """Base class for AdminNotifier misuse."""


class AdminNotifierAuthorizationError(AdminNotifierError):
    """Raised when an actor without admin authorization invokes the notifier."""


class AdminNotifierTargetError(AdminNotifierError):
    """Raised when a notification would target a chat outside config.ADMINS."""


class AdminNotifier:
    """Delivers operational notifications to configured admins only.

    The public delivery methods are :meth:`notify` (acting-admin
    events) and :meth:`notify_system` (server-originated events,
    MT-ADMIN-03); both constrain every recipient to ``config.ADMINS``.
    There is intentionally no API that can send to an arbitrary chat.
    """

    def __init__(
        self,
        send: SendFunc,
        markup_send: Optional[MarkupSendFunc] = None,
    ) -> None:
        if not callable(send):
            raise AdminNotifierError(
                "send must be a callable with signature (chat_id, text)"
            )
        if markup_send is not None and not callable(markup_send):
            raise AdminNotifierError(
                "markup_send must be a callable with signature "
                "(chat_id, text, reply_markup) returning a message id"
            )
        self._send = send
        self._markup_send = markup_send

    @property
    def admin_ids(self) -> tuple[int, ...]:
        """The only chat IDs this notifier may ever deliver to."""
        return tuple(ADMINS)

    def is_authorized(self, actor_id: object) -> bool:
        """True when *actor_id* holds admin authorization.

        Delegates to ``config.is_admin`` so admin authorization
        semantics stay identical to the rest of the bot.
        """
        return is_admin(actor_id)  # type: ignore[arg-type]

    async def notify(
        self,
        actor_id: int,
        text: str,
        targets: Optional[Sequence[int]] = None,
    ) -> list[int]:
        """Send *text* to configured admins on behalf of *actor_id*.

        Args:
            actor_id: The user invoking the notifier. Must be a
                configured admin, otherwise nothing is sent and
                ``AdminNotifierAuthorizationError`` is raised.
            text: Non-empty notification body.
            targets: Optional subset of admin IDs to notify. Defaults
                to every configured admin. Any target outside
                ``config.ADMINS`` raises ``AdminNotifierTargetError``
                before anything is sent.

        Returns:
            The chat IDs the notification was delivered to.
        """
        if not self.is_authorized(actor_id):
            raise AdminNotifierAuthorizationError(
                f"actor {actor_id!r} is not a configured admin"
            )
        self._validate_text(text)
        recipients = self._resolve_recipients(targets)

        delivered: list[int] = []
        for chat_id in recipients:
            await self._send(chat_id, text)
            delivered.append(chat_id)
        return delivered

    async def notify_system(
        self,
        text: str,
        *,
        reply_markup: Optional[object] = None,
        targets: Optional[Sequence[int]] = None,
    ) -> list[tuple[int, Optional[int]]]:
        """Deliver a server-originated operational notification (MT-ADMIN-03).

        Unlike :meth:`notify`, this entry point has no acting admin:
        it is invoked by the server when an operational event occurs
        (e.g. a worker's manual-proof submission opens a pending
        claim).  It is NOT an authorization surface — it grants no
        user any decision authority, and it cannot be steered outside
        ``config.ADMINS``: the same text and target validation as
        :meth:`notify` applies, so required channels/groups can never
        receive operational notifications through this path either.

        Args:
            text: Non-empty notification body.
            reply_markup: Optional inline keyboard (Telegram object)
                passed through to the markup transport.
            targets: Optional subset of admin IDs.  Any target outside
                ``config.ADMINS`` raises ``AdminNotifierTargetError``
                before anything is sent.

        Returns:
            ``(chat_id, message_id)`` per delivered chat — the message
            id feeds the persistent operation → message linkage.
            Requires the markup transport configured at construction.
        """
        if self._markup_send is None:
            raise AdminNotifierError(
                "markup_send transport is required for notify_system"
            )
        self._validate_text(text)
        recipients = self._resolve_recipients(targets)

        delivered: list[tuple[int, Optional[int]]] = []
        for chat_id in recipients:
            message_id = await self._markup_send(chat_id, text, reply_markup)
            delivered.append((chat_id, message_id))
        return delivered

    @staticmethod
    def _validate_text(text: object) -> None:
        """Reject empty/non-string notification bodies (before any send)."""
        if not isinstance(text, str) or not text.strip():
            raise AdminNotifierError(
                "notification text must be a non-empty string"
            )

    @staticmethod
    def _resolve_recipients(
        targets: Optional[Sequence[int]],
    ) -> tuple[int, ...]:
        """Resolve delivery targets, refusing anything outside ADMINS.

        Raises ``AdminNotifierTargetError`` before any message is sent
        when a requested target is not a configured admin.
        """
        admins = tuple(ADMINS)
        if targets is None:
            return admins
        recipients = tuple(dict.fromkeys(targets))
        outside = [t for t in recipients if t not in admins]
        if outside:
            raise AdminNotifierTargetError(
                f"refusing to notify non-admin chats: {outside!r}"
            )
        return recipients
