"""
Rate Admin Command (MT-ADMIN-26)
================================

Private-Telegram Admin command for the authoritative manual
EGP-per-USDT rate source.  Follows the established MT-ADMIN
conventions (admin-only authorization via ``config.is_admin``,
MT-ADMIN-02 private-chat isolation, thin handler over a store layer,
plain-text rendering — no parse modes).

Workflow (admin's private chat only)::

    /setrate 48.5   → validate + atomically persist the current rate

Security:

- every invocation re-checks private chat + ``is_admin``; groups and
  channels get zero replies, non-admins get the standard refusal;
- the ONLY input is the rate text — ``captured_at``/``updated_at`` are
  generated server-side by ``rate_store`` and the provider is hard-
  wired to ``"manual"``; a Telegram message can never select a source,
  a timestamp, or bypass ``rate_quote.parse_rate`` (float/exponent/
  sign/zero/malformed all rejected by the ONE existing parser);
- no client/Mini App endpoint exists for setting the rate, and no
  unrelated financial data is ever shown or logged (the reply carries
  only the canonical rate, provider, capture instant and TTL/status).
"""

from __future__ import annotations

import logging

import rate_store
from config import is_admin
from rate_quote import RateQuoteError, canonical_timestamp_text
from rate_store import RATE_TTL_SECONDS, RateStoreError

logger = logging.getLogger(__name__)

# ── Arabic UI strings ─────────────────────────────────────────────────
MSG_ADMIN_ONLY = "⛔ هذا الأمر للمشرفين فقط."
MSG_ERROR = "⛔ حدث خطأ، حاول مرة أخرى."
MSG_USAGE = (
    "❌ الصيغة غير صحيحة.\n"
    "استخدم:\n"
    "/setrate <السعر>\n\n"
    "مثال:\n"
    "/setrate 48.5\n\n"
    "السعر بالجنيه المصري لكل 1 USDT — أرقام عشرية عادية فقط\n"
    "(بدون scientific notation، بدون إشارة، بدون صفر)."
)


def _non_private_chat(update) -> bool:
    """True only when *update* positively targets a group/channel.

    MT-ADMIN-02 isolation semantics, inlined (no import cycle with
    bot.py): unknown chat types are NOT treated as groups.
    """
    chat = getattr(update, "effective_chat", None)
    chat_type = getattr(chat, "type", None)
    return isinstance(chat_type, str) and chat_type != "private"


def _actor_id(value: object) -> int | None:
    """A trusted positive int id, or None (untrusted identities die)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _command_body(message) -> str:
    """Everything after the command word (``/setrate 48.5`` → ``48.5``)."""
    text = getattr(message, "text", None)
    if not isinstance(text, str):
        return ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def build_set_rate_text(quote) -> str:
    """Admin confirmation: canonical rate, provider, capture instant,
    TTL + current status — and nothing else (no other financial data).
    """
    ttl_minutes = RATE_TTL_SECONDS // 60
    return (
        "✅ تم تحديث سعر الصرف:\n\n"
        f"💱 السعر: {quote.rate_text} EGP / 1 USDT\n"
        f"🛰 المصدر: {quote.provider}\n"
        f"🕒 وقت الالتقاط: "
        f"{canonical_timestamp_text(quote.captured_at)}\n"
        f"♻️ مدة الصلاحية: {RATE_TTL_SECONDS} ثانية "
        f"({ttl_minutes} دقيقة)\n"
        "📊 الحالة: ✅ ساري حاليًا"
    )


# ── PTB handler ───────────────────────────────────────────────────────


async def setrate_command(update, context) -> None:
    """``/setrate <rate>`` — atomically set the current EGP/USDT rate.

    Private admin chat ONLY; groups/channels stay silent and
    non-admins get the standard admin-only refusal.  The rate text is
    validated by ``rate_store.set_rate`` (the existing ``rate_quote``
    contract) before anything is persisted — a rejected value never
    touches the database.
    """
    if _non_private_chat(update):
        return
    message = getattr(update, "message", None)
    if message is None:
        return
    actor = _actor_id(getattr(getattr(update, "effective_user", None), "id", None))
    if actor is None:
        return
    if not is_admin(actor):
        await message.reply_text(MSG_ADMIN_ONLY)
        return

    body = _command_body(message)
    # Exactly one token: "/setrate 48.5".  Extra words, missing args
    # and whitespace-wrapped junk are malformed input, not rates.
    if not body or len(body.split()) != 1:
        await message.reply_text(MSG_USAGE)
        return

    try:
        quote = rate_store.set_rate(body, admin_user_id=actor)
    except (RateQuoteError, RateStoreError) as exc:
        await message.reply_text(f"❌ {exc}")
        return
    except Exception:
        # Log actor id + failure only — never echo request payloads.
        logger.exception("setrate failed: admin=%d", actor)
        await message.reply_text(MSG_ERROR)
        return

    await message.reply_text(build_set_rate_text(quote))
