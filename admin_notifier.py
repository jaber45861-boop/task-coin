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

Usage::

    notifier = AdminNotifier(bot.send_message)
    await notifier.notify(actor_user_id, "Task 12 needs review.")
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional, Sequence

from config import ADMINS, is_admin

# Async transport: deliver *text* to the private chat *chat_id*.
SendFunc = Callable[[int, str], Awaitable[None]]


class AdminNotifierError(Exception):
    """Base class for AdminNotifier misuse."""


class AdminNotifierAuthorizationError(AdminNotifierError):
    """Raised when an actor without admin authorization invokes the notifier."""


class AdminNotifierTargetError(AdminNotifierError):
    """Raised when a notification would target a chat outside config.ADMINS."""


class AdminNotifier:
    """Delivers operational notifications to configured admins only.

    The only public delivery method is :meth:`notify`, which both
    authorizes the acting admin and constrains every recipient to
    ``config.ADMINS``.  There is intentionally no API that can send to
    an arbitrary chat.
    """

    def __init__(self, send: SendFunc) -> None:
        if not callable(send):
            raise AdminNotifierError(
                "send must be a callable with signature (chat_id, text)"
            )
        self._send = send

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
        if not isinstance(text, str) or not text.strip():
            raise AdminNotifierError(
                "notification text must be a non-empty string"
            )

        admins = tuple(ADMINS)
        if targets is None:
            recipients = admins
        else:
            recipients = tuple(dict.fromkeys(targets))
            outside = [t for t in recipients if t not in admins]
            if outside:
                raise AdminNotifierTargetError(
                    f"refusing to notify non-admin chats: {outside!r}"
                )

        delivered: list[int] = []
        for chat_id in recipients:
            await self._send(chat_id, text)
            delivered.append(chat_id)
        return delivered
