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

import logging
from typing import Awaitable, Callable, Optional, Sequence

from config import ADMINS, is_admin

logger = logging.getLogger(__name__)

# Async transport: deliver *text* to the private chat *chat_id*.
SendFunc = Callable[[int, str], Awaitable[None]]
# Async markup transport: deliver *text* with an optional inline
# keyboard and return the Telegram message id (MT-ADMIN-03) so the
# caller can persist an operation → message linkage.
MarkupSendFunc = Callable[[int, str, Optional[object]], Awaitable[Optional[int]]]
# Async photo transport: deliver *text* + *image bytes* with an
# optional inline keyboard and return the Telegram message id
# (MT-ADMIN-34).  Used ONLY by notify_system_photo, and only for
# admins — the same recipient constraint as every other path.
PhotoSendFunc = Callable[
    [int, str, bytes, Optional[object]], Awaitable[Optional[int]]
]


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
        photo_send: Optional[PhotoSendFunc] = None,
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
        if photo_send is not None and not callable(photo_send):
            raise AdminNotifierError(
                "photo_send must be a callable with signature "
                "(chat_id, text, image_bytes, reply_markup) returning "
                "a message id"
            )
        self._send = send
        self._markup_send = markup_send
        self._photo_send = photo_send

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

    async def notify_system_photo(
        self,
        text: str,
        image: bytes,
        *,
        reply_markup: Optional[object] = None,
        targets: Optional[Sequence[int]] = None,
    ) -> list[tuple[int, Optional[int]]]:
        """Deliver a server-originated notification WITH an image.

        A strictly ADDITIVE sibling of :meth:`notify_system`: the
        existing text path is untouched, so every current caller keeps
        its exact behaviour.  Use it only where the evidence itself
        must reach the reviewer (manual task proof screenshots).

        Same authorization contract as ``notify_system``:

        - there is no acting admin; this is a server event;
        - delivery is refused outside ``config.ADMINS``
          (:meth:`_resolve_recipients` is reused, so a worker, a
          channel or a group can never receive an evidence image);
        - the text stays the authoritative body — the image is
          evidence, never a payload that grants anything.

        The image is sent through ``_photo_send``.  When no photo
        transport is configured, or a photo send FAILS, this method
        FALLS BACK to the plain text notification so the operational
        event is never lost — the caller still receives the delivered
        message ids it needs to link buttons to messages.  It never
        raises for a transport problem, and it never logs image bytes.

        Args:
            text: Non-empty notification body (same as notify_system).
            image: Raw image bytes, already validated by the storage
                layer (magic bytes, bounded size and dimensions).
            reply_markup: Optional inline keyboard (Telegram object).
            targets: Optional subset of admin IDs; anything outside
                ``config.ADMINS`` raises ``AdminNotifierTargetError``
                before anything is sent.

        Returns:
            ``(chat_id, message_id)`` per delivered chat.
        """
        if not isinstance(image, (bytes, bytearray)) or not image:
            # No usable image — deliver the text event unchanged.
            logger.info(
                "notify_system_photo called without image bytes; "
                "falling back to text-only delivery"
            )
            return await self.notify_system(
                text, reply_markup=reply_markup, targets=targets
            )
        # Reuse the SAME authorization + text validation.  A target
        # error must still surface to the caller (and abort delivery).
        self._validate_text(text)
        recipients = self._resolve_recipients(targets)

        if self._photo_send is None:
            logger.info(
                "photo transport not configured; falling back to "
                "text-only delivery"
            )
            return await self.notify_system(
                text, reply_markup=reply_markup, targets=targets
            )

        delivered: list[tuple[int, Optional[int]]] = []
        for chat_id in recipients:
            message_id: Optional[int] = None
            try:
                message_id = await self._photo_send(
                    chat_id, text, bytes(image), reply_markup
                )
            except Exception:
                logger.exception(
                    "Evidence photo delivery failed for chat=%s; "
                    "falling back to text",
                    chat_id,
                )
                message_id = None
            if message_id is None:
                # The evidence did not arrive — still surface the event.
                text_ids = await self.notify_system(
                    text, reply_markup=reply_markup, targets=(chat_id,)
                )
                message_id = text_ids[0][1] if text_ids else None
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
