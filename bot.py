import os
import random
import re
import logging
from dotenv import load_dotenv
from telegram import (
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Update,
    WebAppInfo,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from config import ADMINS, CHANNELS, Channel, is_admin, get_required_channels, get_mini_app_url
from subscription import (
    check_subscription_access,
    is_locked,
    lock_user,
    unlock_user,
)
import asyncio
import signal
import threading

from flask import Flask, send_from_directory
from waitress import create_server

import db

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

ANTI_BOT = 0
ANTI_BOT_BLOCKED = 1

MAX_ANTI_BOT_ATTEMPTS = 3

# States for the interactive /addchannel conversation
ADDCHANNEL_USERNAME = 20
ADDCHANNEL_TITLE = 21

# States for the interactive /removechannel conversation
REMOVECHANNEL_SELECT = 30
REMOVECHANNEL_CONFIRM = 31


async def _send_math_question(message) -> tuple[str, int]:
    """Generate a math question and return (question_text, answer)."""
    a = random.randint(1, 20)
    b = random.randint(1, 20)
    op = random.choice(["+", "-"])

    if op == "-" and a < b:
        a, b = b, a

    correct = a + b if op == "+" else a - b
    return f"🔒 للتحقق أنك لست بوت:\n\nما ناتج: {a} {op} {b}؟", correct


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send a simple math question as anti-bot step.
    
    Also captures referral payload from deep link if present.
    """
    # ── Referral attribution (capture before anti-bot flow) ──────────
    user = update.effective_user
    user_id = user.id
    username = user.username
    first_name = user.first_name

    # Extract referral payload if present
    referred_by = None
    if context.args and len(context.args) > 0:
        try:
            referred_by = int(context.args[0])
        except (ValueError, TypeError):
            referred_by = None

    # Register user with referral attribution (idempotent)
    db.register_user(
        user_id=user_id,
        username=username,
        first_name=first_name,
        referred_by=referred_by,
    )

    # ── Anti-bot challenge ──────────────────────────────────────────
    question, correct = await _send_math_question(update.message)
    context.user_data["anti_bot_answer"] = correct
    context.user_data["anti_bot_attempts"] = 0

    await update.message.reply_text(question)
    return ANTI_BOT


async def _blocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle messages from users who exhausted their anti-bot attempts."""
    await update.message.reply_text(
        "🚫 لقد تجاوزت الحد الأقصى للمحاولات. "
        "أرسل /start للبدء من جديد."
    )
    return ANTI_BOT_BLOCKED


async def check_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Check the user's answer with up to 3 attempts."""
    expected = context.user_data.get("anti_bot_answer")
    attempts = context.user_data.get("anti_bot_attempts", 0)
    text = update.message.text.strip()

    try:
        user_answer = int(text)
    except ValueError:
        attempts += 1
        context.user_data["anti_bot_attempts"] = attempts
        if attempts >= MAX_ANTI_BOT_ATTEMPTS:
            context.user_data.pop("anti_bot_answer", None)
            context.user_data.pop("anti_bot_attempts", None)
            await update.message.reply_text(
                "🚫 لقد تجاوزت الحد الأقصى للمحاولات. "
                "أرسل /start للبدء من جديد."
            )
            return ANTI_BOT_BLOCKED
        question, new_correct = await _send_math_question(update.message)
        context.user_data["anti_bot_answer"] = new_correct
        remaining = MAX_ANTI_BOT_ATTEMPTS - attempts
        await update.message.reply_text(
            "❌ إجابة غير صحيحة. "
            f"متبقي {remaining} محاولة."
        )
        await update.message.reply_text(question)
        return ANTI_BOT

    if user_answer != expected:
        attempts += 1
        context.user_data["anti_bot_attempts"] = attempts
        if attempts >= MAX_ANTI_BOT_ATTEMPTS:
            context.user_data.pop("anti_bot_answer", None)
            context.user_data.pop("anti_bot_attempts", None)
            await update.message.reply_text(
                "🚫 لقد تجاوزت الحد الأقصى للمحاولات. "
                "أرسل /start للبدء من جديد."
            )
            return ANTI_BOT_BLOCKED
        question, new_correct = await _send_math_question(update.message)
        context.user_data["anti_bot_answer"] = new_correct
        remaining = MAX_ANTI_BOT_ATTEMPTS - attempts
        await update.message.reply_text(
            "❌ إجابة غير صحيحة. "
            f"متبقي {remaining} محاولة."
        )
        await update.message.reply_text(question)
        return ANTI_BOT

    # ── Anti-bot passed — check channel subscriptions ───────────────
    context.user_data.pop("anti_bot_attempts", None)
    required = get_required_channels()
    if not required:
        await update.message.reply_text("✅ تحقق ناجح! أنت لست بوت.")
        context.user_data.pop("anti_bot_answer", None)
        return ConversationHandler.END

    user_id = update.effective_user.id
    subscribed, missing = await check_subscription_access(
        context.bot, user_id
    )

    context.user_data.pop("anti_bot_answer", None)

    if subscribed:
        unlock_user(user_id)
        await update.message.reply_text(
            "✅ تحقق ناجح! أنت لست بوت.\n"
            "✅ أنت مشترك في جميع القنوات المطلوبة."
        )
    else:
        lock_user(user_id)
        text, markup = _build_missing_message(missing)
        await update.message.reply_text(text, reply_markup=markup)

    return ConversationHandler.END


# ── Subscription helpers ────────────────────────────────────────────

def _build_missing_message(
    missing: list[Channel],
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the text + keyboard for missing-channel messages."""
    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=f"📢 {ch.title}",
            url=f"https://t.me/{ch.username}",
        )]
        for ch in missing
    ]
    buttons.append(
        [InlineKeyboardButton(
            text="✅ تحقق من الاشتراك",
            callback_data="verify_subscription",
        )]
    )
    names = "\n".join(f"  • {ch.title}" for ch in missing)
    text = (
        "✅ تحقق ناجح! أنت لست بوت.\n\n"
        "⚠️ أنت غير مشترك في القنوات التالية:\n"
        f"{names}\n\n"
        "اضغط الزر للاشتراك ثم اضغط \"تحقق من الاشتراك\":"
    )
    return text, InlineKeyboardMarkup(buttons)


async def verify_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-check all required channels when the verify button is pressed."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    required = get_required_channels()

    if not required:
        await query.edit_message_text(
            "✅ لا توجد قنوات مطلوبة حالياً."
        )
        return

    missing: list[Channel] = []
    for ch in required:
        try:
            member = await context.bot.get_chat_member(ch.channel_id, user_id)
            if member.status not in (
                "member",
                "administrator",
                "creator",
            ):
                missing.append(ch)
        except TelegramError:
            missing.append(ch)

    if not missing:
        unlock_user(user_id)
        await query.edit_message_text(
            "✅ تحقق ناجح! أنت مشترك في جميع القنوات المطلوبة."
        )
    else:
        lock_user(user_id)
        text, markup = _build_missing_message(missing)
        await query.edit_message_text(text, reply_markup=markup)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the anti-bot check."""
    context.user_data.pop("anti_bot_answer", None)
    await update.message.reply_text("تم الإلغاء.")
    return ConversationHandler.END


# ── Channel-reference normalizer ────────────────────────────────────

# Matches @username, t.me/username, and bare username strings.
# Accepts https://, http://, bare t.me/, www.t.me/ prefixes.
_USERNAME_RE = re.compile(
    r"^(?:(?:https?://)?(?:www\.)?t\.me/)?@?([A-Za-z0-9_]{5,})$",
)


def _normalize_channel_ref(raw: str) -> str | None:
    """Extract a clean Telegram username from various input formats.

    Accepted inputs:
        @channelusername
        https://t.me/channelusername
        http://t.me/channelusername
        www.t.me/channelusername
        t.me/channelusername
        channelusername

    Returns the bare username (without @) on success, or None.
    """
    m = _USERNAME_RE.match(raw.strip())
    if m:
        return m.group(1)
    return None


# ── Admin: Add Channel ───────────────────────────────────────────────

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a mandatory subscription channel. Admin only.

    Usage: /addchannel slug|@username|title
           /addchannel slug|https://t.me/username|title
    The bot resolves the channel automatically via get_chat().
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        subscribed, missing = await check_subscription_access(
            context.bot, user_id
        )
        if not subscribed:
            lock_user(user_id)
            text, markup = _build_missing_message(missing)
            await update.message.reply_text(text, reply_markup=markup)
            return
        unlock_user(user_id)
        await update.message.reply_text("⛔ هذا الأمر للمشرفين فقط.")
        return

    text = update.message.text.replace("/addchannel", "", 1).strip()
    parts = [p.strip() for p in text.split("|")]

    if len(parts) != 3:
        await update.message.reply_text(
            "❌ صيغة خاطئة. استخدم:\n"
            "/addchannel slug|@username|title\n\n"
            "أو:\n"
            "/addchannel slug|https://t.me/username|title\n\n"
            "مثال:\n"
            "/addchannel main|@mychannel|My Channel"
        )
        return

    slug, channel_ref, title = parts

    # Validate slug
    if not slug or not slug.isalnum() and "_" not in slug:
        await update.message.reply_text(
            "❌ الـslug يجب أن يحتوي على أحرف وأرقام فقط (والشرطة السفلية _)."
        )
        return

    # Normalize channel reference
    username = _normalize_channel_ref(channel_ref)
    if not username:
        await update.message.reply_text(
            "❌ صيغة غير صحيحة لاسم القناة.\n"
            "استخدم @username أو https://t.me/username"
        )
        return

    # Prevent duplicate slug
    if slug in CHANNELS:
        await update.message.reply_text(
            f"❌ الـslug '{slug}' موجود مسبقًا."
        )
        return

    # ── Resolve channel via Telegram Bot API ────────────────────────
    try:
        chat = await context.bot.get_chat(f"@{username}")
    except TelegramError as exc:
        await update.message.reply_text(
            "❌ تعذر الوصول للقناة. تأكد من:\n"
            "• اسم القناة صحيح\n"
            "• البوت مضاف للقناة\n\n"
            f"تفاصيل الخطأ: {exc}"
        )
        return

    # Verify it's a channel or supergroup
    if chat.type not in ("channel", "supergroup"):
        await update.message.reply_text(
            f'❌ "{chat.type}" ليست قناة أو supergroup.\n'
            "يجب أن يكون المعرف الخاص بقناة أو supergroup Telegram."
        )
        return

    channel_id = chat.id

    # Prevent duplicate channel_id
    for existing in CHANNELS.values():
        if existing.channel_id == channel_id:
            await update.message.reply_text(
                f"❌ القناة @{username} (ID: {channel_id}) موجودة مسبقًا "
                f"(slug: {existing.slug})."
            )
            return

    # Verify the bot has access in the channel/supergroup.
    # For channels the bot must be admin (Telegram API requirement).
    # For supergroups a regular member is enough.
    try:
        bot_member = await context.bot.get_chat_member(
            channel_id, context.bot.id
        )
    except TelegramError as exc:
        await update.message.reply_text(
            "❌ تعذر التحقق من عضوية البوت في القناة/المجموعة.\n"
            "تأكد أن البوت مضاف للقناة أو المجموعة\n"
            "ليتمكن من فحص اشتراك المستخدمين لاحقًا.\n\n"
            f"تفاصيل الخطأ: {exc}"
        )
        return

    if chat.type == "channel":
        _bot_ok = bot_member.status in ("administrator", "creator")
    else:
        _bot_ok = bot_member.status in (
            "member", "administrator", "creator",
        )

    if not _bot_ok:
        await update.message.reply_text(
            f"❌ البوت ليس عضوًا كافيًا في {chat.type} (حالته: {bot_member.status})\n"
            "يجب أن يكون البوت *مشرفًا* (admin) في القناة\n"
            "أو *عضوًا* (member) في المجموعة."
        )
        return

    # Add the channel
    new_channel = Channel(
        slug=slug,
        channel_id=channel_id,
        username=username,
        title=title,
        required=True,
        chat_type=chat.type,
    )
    CHANNELS[slug] = new_channel
    db.save_channel(new_channel)

    await update.message.reply_text(
        "✅ تمت إضافة القناة:\n\n"
        f"📌 Slug: {slug}\n"
        f"🆔 ID: {channel_id}\n"
        f"📛 Username: @{username}\n"
        f"📝 Title: {title}\n"
        f"🔒 Required: نعم"
    )
    logger.info("Channel added: %s (id=%d) by admin %d", slug, channel_id, user_id)


# NOTE: The legacy add_channel handler is retained for backward-compatible
# unit tests.  The interactive /addchannel ConversationHandler below
# replaces it in the running bot.


def _derive_slug(username: str) -> str:
    """Derive a safe channel slug from a Telegram username."""
    slug = re.sub(r"[^a-z0-9_]", "", username.lower())
    return slug or "channel"


async def addchannel_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Entry point for the interactive /addchannel workflow.

    Checks admin privileges, then prompts for the channel username/link.
    Works for both /addchannel command and admin panel callback.
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        subscribed, missing = await check_subscription_access(
            context.bot, user_id
        )
        if not subscribed:
            lock_user(user_id)
            text, markup = _build_missing_message(missing)
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(
                    text, reply_markup=markup
                )
            else:
                await update.message.reply_text(text, reply_markup=markup)
            return ConversationHandler.END
        unlock_user(user_id)
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                "⛔ هذا الأمر للمشرفين فقط."
            )
        else:
            await update.message.reply_text("⛔ هذا الأمر للمشرفين فقط.")
        return ConversationHandler.END

    # Clean up any leftover state from a previous interrupted flow.
    context.user_data.pop("addchannel_channel_id", None)
    context.user_data.pop("addchannel_username", None)
    context.user_data.pop("addchannel_chat_type", None)

    prompt = (
        "أرسل Username القناة مثل @Crypto1583 أو رابط القناة مثل "
        "https://t.me/Crypto1583"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(prompt)
    else:
        await update.message.reply_text(prompt)
    return ADDCHANNEL_USERNAME


async def addchannel_username(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Validate the channel username/link and prompt for the title."""
    text = update.message.text.strip()
    username = _normalize_channel_ref(text)

    if not username:
        await update.message.reply_text(
            "❌ صيغة غير صحيحة. أرسل Username مثل @Crypto1583\n"
            "أو رابط مثل https://t.me/Crypto1583"
        )
        return ADDCHANNEL_USERNAME

    # ── Validate via Telegram Bot API (same as the original handler) ──
    try:
        chat = await context.bot.get_chat(f"@{username}")
    except TelegramError as exc:
        await update.message.reply_text(
            "❌ تعذر الوصول للقناة. تأكد من:\n"
            "• اسم القناة صحيح\n"
            "• البوت مضاف للقناة\n\n"
            f"تفاصيل الخطأ: {exc}"
        )
        return ADDCHANNEL_USERNAME

    # Verify it's a channel or supergroup
    if chat.type not in ("channel", "supergroup"):
        await update.message.reply_text(
            f'❌ "{chat.type}" ليست قناة أو supergroup.\n'
            "يجب أن يكون المعرف الخاص بقناة أو supergroup Telegram."
        )
        return ADDCHANNEL_USERNAME

    # Prevent duplicate channel_id
    for existing in CHANNELS.values():
        if existing.channel_id == chat.id:
            await update.message.reply_text(
                f"❌ القناة @{username} (ID: {chat.id}) موجودة مسبقًا "
                f"(slug: {existing.slug})."
            )
            return ADDCHANNEL_USERNAME

    # Verify the bot has access in the channel/supergroup.
    # For channels the bot must be admin (Telegram API requirement).
    # For supergroups a regular member is enough.
    try:
        bot_member = await context.bot.get_chat_member(
            chat.id, context.bot.id
        )
    except TelegramError as exc:
        await update.message.reply_text(
            "❌ تعذر التحقق من عضوية البوت في القناة/المجموعة.\n"
            "تأكد أن البوت مضاف للقناة أو المجموعة\n"
            "ليتمكن من فحص اشتراك المستخدمين لاحقًا.\n\n"
            f"تفاصيل الخطأ: {exc}"
        )
        return ADDCHANNEL_USERNAME

    if chat.type == "channel":
        _bot_ok = bot_member.status in ("administrator", "creator")
    else:
        _bot_ok = bot_member.status in (
            "member", "administrator", "creator",
        )

    if not _bot_ok:
        await update.message.reply_text(
            f"❌ البوت ليس عضوًا كافيًا في {chat.type} (حالته: {bot_member.status})\n"
            "يجب أن يكون البوت *مشرفًا* (admin) في القناة\n"
            "أو *عضوًا* (member) في المجموعة."
        )
        return ADDCHANNEL_USERNAME

    # Store validated data for the next step
    context.user_data["addchannel_channel_id"] = chat.id
    context.user_data["addchannel_username"] = username
    context.user_data["addchannel_chat_type"] = chat.type

    await update.message.reply_text("أرسل اسم القناة")
    return ADDCHANNEL_TITLE


async def addchannel_title(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receive the title, persist the channel, and finish."""
    title = update.message.text.strip()
    if not title:
        await update.message.reply_text(
            "❌ اسم القناة لا يمكن أن يكون فارغًا.\nأرسل اسم القناة."
        )
        return ADDCHANNEL_TITLE

    channel_id = context.user_data.pop("addchannel_channel_id")
    username = context.user_data.pop("addchannel_username")
    chat_type = context.user_data.pop("addchannel_chat_type", "channel")

    slug = _derive_slug(username)
    # Ensure slug uniqueness
    if slug in CHANNELS:
        slug = f"{slug}_{abs(channel_id)}"

    new_channel = Channel(
        slug=slug,
        channel_id=channel_id,
        username=username,
        title=title,
        required=True,
        chat_type=chat_type,
    )
    CHANNELS[slug] = new_channel
    db.save_channel(new_channel)

    await update.message.reply_text(
        "✅ تمت إضافة القناة:\n\n"
        f"📌 Slug: {slug}\n"
        f"🆔 ID: {channel_id}\n"
        f"📛 Username: @{username}\n"
        f"📝 Title: {title}\n"
        "🔒 Required: نعم"
    )
    logger.info(
        "Channel added: %s (id=%d) via interactive flow", slug, channel_id
    )
    return ConversationHandler.END


async def addchannel_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Cancel the /addchannel conversation."""
    context.user_data.pop("addchannel_channel_id", None)
    context.user_data.pop("addchannel_username", None)
    context.user_data.pop("addchannel_chat_type", None)
    await update.message.reply_text("تم الإلغاء.")
    return ConversationHandler.END


# ── Admin: List Channels ─────────────────────────────────────────────

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all mandatory subscription channels. Admin only."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        subscribed, missing = await check_subscription_access(
            context.bot, user_id
        )
        if not subscribed:
            lock_user(user_id)
            text, markup = _build_missing_message(missing)
            await update.message.reply_text(text, reply_markup=markup)
            return
        unlock_user(user_id)
        await update.message.reply_text("⛔ هذا الأمر للمشرفين فقط.")
        return

    if not CHANNELS:
        await update.message.reply_text("📭 لا توجد قنوات اشتراك إجباري حالياً.")
        return

    lines = ["📋 *قنوات الاشتراك الإجباري:*\n"]
    for ch in CHANNELS.values():
        required_text = "نعم" if ch.required else "لا"
        lines.append(
            f"📌 *{ch.title}*\n"
            f"   slug: `{ch.slug}`\n"
            f"   ID: `{ch.channel_id}`\n"
            f"   username: @{ch.username}\n"
            f"   required: {required_text}\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    logger.info("Channels listed by admin %d", user_id)


# ── Admin: Remove Channel ────────────────────────────────────────────

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a mandatory subscription channel by slug. Admin only.

    Usage: /removechannel slug
    Example: /removechannel main
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        subscribed, missing = await check_subscription_access(
            context.bot, user_id
        )
        if not subscribed:
            lock_user(user_id)
            text, markup = _build_missing_message(missing)
            await update.message.reply_text(text, reply_markup=markup)
            return
        unlock_user(user_id)
        await update.message.reply_text("⛔ هذا الأمر للمشرفين فقط.")
        return

    slug = update.message.text.replace("/removechannel", "", 1).strip()

    if not slug:
        await update.message.reply_text(
            "❌ صيغة خاطئة. استخدم:\n"
            "/removechannel slug\n\n"
            "مثال:\n"
            "/removechannel main"
        )
        return

    if slug not in CHANNELS:
        await update.message.reply_text(
            f"❌ القناة بالـslug '{slug}' غير موجودة."
        )
        return

    removed = CHANNELS.pop(slug)
    db.delete_channel(slug)

    await update.message.reply_text(
        "✅ تم حذف القناة:\n\n"
        f"📌 Slug: {removed.slug}\n"
        f"🆔 ID: {removed.channel_id}\n"
        f"📛 Username: @{removed.username}\n"
        f"📝 Title: {removed.title}"
    )
    logger.info("Channel removed: %s (id=%d) by admin %d", slug, removed.channel_id, user_id)


# NOTE: The legacy remove_channel handler is retained for backward-compatible
# unit tests.  The interactive /removechannel ConversationHandler below
# replaces it in the running bot.


async def removechannel_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Entry point for /removechannel.  Handles both direct and interactive.

    /removechannel <slug>  → direct deletion (legacy behaviour).
    /removechannel         → interactive inline-button workflow.
    admin_panel:remove     → interactive inline-button workflow (from panel).
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        subscribed, missing = await check_subscription_access(
            context.bot, user_id
        )
        if not subscribed:
            lock_user(user_id)
            text, markup = _build_missing_message(missing)
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(
                    text, reply_markup=markup
                )
            else:
                await update.message.reply_text(text, reply_markup=markup)
            return ConversationHandler.END
        unlock_user(user_id)
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                "⛔ هذا الأمر للمشرفين فقط."
            )
        else:
            await update.message.reply_text("⛔ هذا الأمر للمشرفين فقط.")
        return ConversationHandler.END

    # ── Direct deletion (legacy path) — only for text commands ─────
    if not update.callback_query:
        slug = update.message.text.replace("/removechannel", "", 1).strip()
        if slug:
            if slug not in CHANNELS:
                await update.message.reply_text(
                    f"❌ القناة بالـslug '{slug}' غير موجودة."
                )
                return ConversationHandler.END

            removed = CHANNELS.pop(slug)
            db.delete_channel(slug)

            await update.message.reply_text(
                "✅ تم حذف القناة:\n\n"
                f"📌 Slug: {removed.slug}\n"
                f"🆔 ID: {removed.channel_id}\n"
                f"📛 Username: @{removed.username}\n"
                f"📝 Title: {removed.title}"
            )
            logger.info(
                "Channel removed: %s (id=%d) by admin %d",
                slug, removed.channel_id, user_id,
            )
            return ConversationHandler.END

    # ── Interactive flow ─────────────────────────────────────────────
    if not CHANNELS:
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                "لا توجد قنوات إجبارية للحذف."
            )
        else:
            await update.message.reply_text(
                "لا توجد قنوات إجبارية للحذف."
            )
        return ConversationHandler.END

    buttons: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"📢 {ch.title} (@{ch.username})",
                callback_data=f"rmch:{ch.slug}",
            )
        ]
        for ch in CHANNELS.values()
    ]

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "🔄 اختر القناة المراد حذفها:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:
        await update.message.reply_text(
            "🔄 اختر القناة المراد حذفها:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    return REMOVECHANNEL_SELECT


async def removechannel_select(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle channel-selection callback → show confirmation."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ هذا الأمر للمشرفين فقط.")
        return ConversationHandler.END

    slug = query.data.split(":", 1)[1]

    if slug not in CHANNELS:
        await query.edit_message_text(
            "❌ القناة لم تعد متاحة. ربما تم حذفها بالفعل."
        )
        return ConversationHandler.END

    ch = CHANNELS[slug]
    buttons = [
        [
            InlineKeyboardButton(
                "✅ تأكيد الحذف", callback_data=f"rmch_yes:{slug}"
            ),
            InlineKeyboardButton(
                "❌ إلغاء", callback_data="rmch_no"
            ),
        ]
    ]
    await query.edit_message_text(
        f"⚠️ أنت على وشك حذف القناة:\n\n"
        f"📌 {ch.title}\n"
        f"📛 @{ch.username}\n"
        f"🆔 {ch.channel_id}\n\n"
        "هل أنت متأكد؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return REMOVECHANNEL_CONFIRM


async def removechannel_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle confirmation callback → delete the channel."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ هذا الأمر للمشرفين فقط.")
        return ConversationHandler.END

    slug = query.data.split(":", 1)[1]

    if slug not in CHANNELS:
        await query.edit_message_text(
            "❌ القناة لم تعد متاحة. ربما تم حذفها بالفعل."
        )
        return ConversationHandler.END

    removed = CHANNELS.pop(slug)
    db.delete_channel(slug)

    await query.edit_message_text(
        "✅ تم حذف القناة بنجاح:\n\n"
        f"📌 {removed.title}\n"
        f"📛 @{removed.username}\n"
        f"🆔 {removed.channel_id}"
    )
    logger.info(
        "Channel removed: %s (id=%d) via interactive flow",
        slug, removed.channel_id,
    )
    return ConversationHandler.END


async def removechannel_cancel_cb(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle cancel callback."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ تم الإلغاء. لم يتم حذف أي قناة.")
    return ConversationHandler.END


async def removechannel_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle /cancel during the /removechannel conversation."""
    await update.message.reply_text("تم الإلغاء.")
    return ConversationHandler.END


# ── Subscription gate for protected bot commands ─────────────────────

async def subscription_gate(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Block non-admin users who are missing required channels.

    Placed *before* other CommandHandlers in main() so locked users
    never reach protected functionality.
    """
    user_id = update.effective_user.id
    if is_admin(user_id):
        return

    subscribed, missing = await check_subscription_access(
        context.bot, user_id
    )
    if subscribed:
        unlock_user(user_id)
        return

    lock_user(user_id)
    text, markup = _build_missing_message(missing)
    await update.message.reply_text(text, reply_markup=markup)


# ── Chat-member update handler ───────────────────────────────────────

_REQUIRED_CHANNEL_IDS: set[int] = set()


def _refresh_required_ids() -> None:
    """Rebuild the set of required channel IDs from the live CHANNELS dict."""
    global _REQUIRED_CHANNEL_IDS
    _REQUIRED_CHANNEL_IDS = {
        ch.channel_id for ch in get_required_channels()
    }


async def on_chat_member_update(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Detect when a user leaves or is removed from a required channel."""
    chat_member_update = update.chat_member
    if chat_member_update is None:
        return

    chat = chat_member_update.chat
    if chat is None or chat.id not in _REQUIRED_CHANNEL_IDS:
        return

    new_status = chat_member_update.new_chat_member.status
    user = chat_member_update.new_chat_member.user
    if user is None or user.id is None:
        return

    if new_status in ("left", "kicked"):
        lock_user(user.id)
        logger.info(
            "User %d left/was removed from required channel %d — locked",
            user.id,
            chat.id,
        )


# ── Subscription gate for non-command messages ────────────────────────

async def subscription_message_gate(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Block non-command messages from locked non-admin users.

    Registered in the same handler group as the ConversationHandler
    (group=1), so the ConversationHandler consumes anti-bot answer
    messages before this gate ever sees them.
    """
    if update.message is None or update.message.text is None:
        return

    user_id = update.effective_user.id
    if is_admin(user_id):
        return

    subscribed, missing = await check_subscription_access(
        context.bot, user_id
    )
    if subscribed:
        unlock_user(user_id)
        return

    lock_user(user_id)
    text, markup = _build_missing_message(missing)
    await update.message.reply_text(text, reply_markup=markup)


# ── Clear legacy command menus on startup ──────────────────────────


async def clear_command_menus(application: Application) -> None:
    """Remove previously registered bot commands from Telegram.

    Clears commands from the default scope and every configured admin
    BotCommandScopeChat so old /addchannel /removechannel /listchannels
    entries no longer appear in the native command menu.
    """
    bot = application.bot

    # 1. Clear default scope
    await bot.delete_my_commands(scope=BotCommandScopeDefault())
    logger.info("Cleared default command menu")

    # 2. Clear each admin's per-chat scope
    for admin_id in ADMINS:
        await bot.delete_my_commands(
            scope=BotCommandScopeChat(chat_id=admin_id),
        )
        logger.info("Cleared command menu for admin %d", admin_id)


# ── Admin Channel Panel (inline buttons) ────────────────────────────


async def admin_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Admin-only /admin command that shows the channel management panel."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ هذا الأمر للمشرفين فقط.")
        return

    buttons = [
        [InlineKeyboardButton("➕ إضافة قناة أو مجموعة", callback_data="admin_panel:add")],
        [InlineKeyboardButton("🗑️ حذف قناة", callback_data="admin_panel:remove")],
        [InlineKeyboardButton("📋 عرض القنوات", callback_data="admin_panel:list")],
    ]
    await update.message.reply_text(
        "⚙️ إدارة القنوات",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    logger.info("Admin panel opened by admin %d", user_id)


async def admin_panel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle admin panel callbacks (add, remove, list)."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("⛔ هذا الأمر للمشرفين فقط.")
        return

    action = query.data.split(":", 1)[1]

    if action == "remove":
        # Route to the interactive removechannel workflow.
        await removechannel_start(update, context)
    elif action == "list":
        if not CHANNELS:
            await query.edit_message_text(
                "📭 لا توجد قنوات اشتراك إجباري حالياً."
            )
            return

        lines = ["📋 *قنوات الاشتراك الإجباري:*\n"]
        for ch in CHANNELS.values():
            required_text = "نعم" if ch.required else "لا"
            lines.append(
                f"📌 *{ch.title}*\n"
                f"   slug: `{ch.slug}`\n"
                f"   ID: `{ch.channel_id}`\n"
                f"   username: @{ch.username}\n"
                f"   required: {required_text}\n"
            )

        await query.edit_message_text(
            "\n".join(lines), parse_mode="Markdown"
        )
        logger.info("Channels listed by admin %d via panel", user_id)


# ── WispByte single-entry runtime ─────────────────────────────────
# WispByte starts the project with `python bot.py` only — it does not run
# the Procfile. This process must therefore serve the Mini App over HTTP
# (0.0.0.0:$PORT, Waitress) *and* keep the Telegram bot polling, without
# a second server or process manager.

_SINGLE_ENTRY_RUNNING = False


def create_mini_app() -> Flask:
    """Create the Flask application that serves the Mini App static files.

    Serves ``miniapp/index.html`` at ``/`` and all static assets (CSS, JS,
    images) under ``/<path>``.
    """
    mini_app = Flask(__name__)
    miniapp_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "miniapp"
    )

    @mini_app.route("/")
    def mini_app_index():
        """Serve the Mini App entry point."""
        return send_from_directory(miniapp_dir, "index.html")

    @mini_app.route("/<path:path>")
    def mini_app_static(path):
        """Serve static assets (CSS, JS, images, etc.)."""
        return send_from_directory(miniapp_dir, path)

    return mini_app


def create_mini_app_server(host: str = "0.0.0.0", port: "int | None" = None):
    """Create and bind the Waitress production WSGI server for the Mini App.

    Reads ``PORT`` from the environment (default ``5000``) unless *port* is
    given, and binds ``0.0.0.0`` as required by WispByte. The socket is bound
    during creation, so an ``OSError`` (e.g. address already in use) is
    raised immediately — deterministic startup-error reporting before any
    Telegram resource is created. Use ``server.run()`` to start accepting
    requests and ``server.close()`` for a clean shutdown.
    """
    if port is None:
        port = int(os.environ.get("PORT", 5000))
    return create_server(create_mini_app(), host=host, port=port)


def _handle_shutdown_signal(signum, frame) -> None:
    """Turn OS shutdown signals into a graceful single-entry shutdown.

    Raised on the main thread while ``server.run()`` is executing. Waitress
    catches ``KeyboardInterrupt``/``SystemExit`` and stops accepting requests,
    which lands in ``run_single_entry()``'s teardown: the Telegram lifecycle
    is stopped and the HTTP socket is closed, in that order.
    """
    logger.info(
        "Shutdown signal received (%s) — stopping Mini App HTTP server and Telegram bot",
        signum,
    )
    raise KeyboardInterrupt


async def _stop_telegram_application(application: Application) -> None:
    """Stop the Telegram application in ``run_polling``'s teardown order.

    Mirrors ``Application.__run``: ``updater.stop`` → ``stop`` → ``post_stop``
    → ``shutdown`` → ``post_shutdown``, each step guarded so a failure in one
    step never prevents the others — no PTB resources are left hanging.
    """
    try:
        if application.updater is not None and application.updater.running:
            await application.updater.stop()
    except Exception:
        logger.exception("Error while stopping Telegram updater")
    try:
        if application.running:
            await application.stop()
            if application.post_stop:
                await application.post_stop(application)
    except Exception:
        logger.exception("Error while stopping Telegram application")
    try:
        await application.shutdown()
        if application.post_shutdown:
            await application.post_shutdown(application)
    except Exception:
        logger.exception("Error while shutting down Telegram application")


def _run_telegram_bot(application: Application, stop_event: threading.Event) -> None:
    """Run the Telegram bot in a controlled background execution context.

    Mirrors ``Application.run_polling``'s internal lifecycle on a dedicated
    event loop:

    ``initialize → post_init → updater.start_polling → start`` … wait for
    *stop_event* … ``updater.stop → stop → post_stop → shutdown → post_shutdown``

    Polling errors are forwarded to the application's registered error
    handlers via the same ``error_callback`` wrapper ``run_polling`` installs.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _error_callback(exc: TelegramError) -> None:
        application.create_task(application.process_error(error=exc, update=None))

    async def _lifecycle() -> None:
        try:
            await application.initialize()
            if application.post_init:
                await application.post_init(application)
            await application.updater.start_polling(
                error_callback=_error_callback,
            )
            await application.start()
            logger.info("Telegram bot polling started (background thread)")
            while not stop_event.is_set():
                await asyncio.sleep(0.5)
        finally:
            logger.info("Telegram bot stopping...")
            await _stop_telegram_application(application)

    try:
        loop.run_until_complete(_lifecycle())
    except Exception:
        logger.exception("Telegram bot background thread error")
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()


def run_single_entry(application: Application) -> None:
    """Run the Mini App HTTP server and the Telegram bot from this process.

    WispByte's Startup Command is ``python bot.py`` and it does not execute
    the Procfile, so this one process provides both:

    * **HTTP** — Waitress serves the Mini App on ``0.0.0.0:$PORT`` in the
      main thread; this is the listener WispByte's reverse proxy routes to.
    * **Telegram** — polling runs in a background thread with PTB's full
      lifecycle, stopped cleanly via *stop_event*.

    The HTTP listener is bound **before** any Telegram resource is created:
    if the bind fails, ``OSError`` propagates immediately (logged clearly)
    and nothing else has been started. Exactly one HTTP server is created
    per process — a concurrent second call raises ``RuntimeError``.
    """
    global _SINGLE_ENTRY_RUNNING
    if _SINGLE_ENTRY_RUNNING:
        raise RuntimeError(
            "Mini App HTTP server is already running — duplicate startup prevented"
        )
    _SINGLE_ENTRY_RUNNING = True
    try:
        # 1. Bind the HTTP listener first: fail fast, fail clearly.
        try:
            server = create_mini_app_server()
        except OSError as exc:
            logger.error(
                "Mini App HTTP server failed to bind on 0.0.0.0:%s — %s",
                os.environ.get("PORT", "5000"),
                exc,
            )
            raise
        logger.info(
            "Mini App server bound on %s:%s",
            server.effective_host,
            server.effective_port,
        )

        # 2. Telegram bot: controlled background execution context.
        stop_event = threading.Event()
        bot_thread = threading.Thread(
            target=_run_telegram_bot,
            args=(application, stop_event),
            name="telegram-bot",
        )

        # 3. Graceful OS-signal handling while the main thread serves HTTP.
        previous_handlers: dict = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGABRT):
                previous_handlers[sig] = signal.signal(sig, _handle_shutdown_signal)

        thread_started = False
        try:
            bot_thread.start()
            thread_started = True
            # Waitress owns the main thread until a shutdown signal arrives;
            # its run() converts KeyboardInterrupt/SystemExit into a clean
            # return, landing in the teardown below.
            server.run()
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
            # Ordered shutdown: Telegram first, then the HTTP listener.
            logger.info("HTTP server stopped. Shutting down Telegram bot...")
            stop_event.set()
            if thread_started:
                bot_thread.join(timeout=10)
                if bot_thread.is_alive():
                    logger.warning(
                        "Telegram bot thread did not stop within timeout"
                    )
            try:
                server.close()
            except Exception:
                logger.exception("Error while closing the Mini App HTTP server")
    finally:
        _SINGLE_ENTRY_RUNNING = False


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set")

    # Initialize SQLite database and load persisted channels
    db.init_db()
    db.load_channels()
    _refresh_required_ids()

    app = ApplicationBuilder().token(token).build()

    # Clear legacy command menus registered by earlier bot versions.
    app.post_init = clear_command_menus

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ANTI_BOT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, check_answer),
            ],
            ANTI_BOT_BLOCKED: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _blocked),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # 1. Detect channel departures immediately.
    required_ids = {
        ch.channel_id for ch in get_required_channels()
    }
    if required_ids:
        app.add_handler(
            ChatMemberHandler(
                on_chat_member_update,
                ChatMemberHandler.CHAT_MEMBER,
                block=False,
            ),
            group=0,
        )

    # 2. Anti-bot conversation (entry: /start).
    app.add_handler(conv_handler, group=1)

    # 3. Subscription gate for non-command messages.
    #    Same group as the ConversationHandler so only one fires per
    #    update: when the ConversationHandler matches (ANTI_BOT state)
    #    it consumes the update and the gate never fires for it.
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            subscription_message_gate,
        ),
        group=1,
    )

    # 4. Interactive /addchannel conversation (group 2, before other commands).
    #    Admin check + subscription gate are inside addchannel_start.
    #    Also accepts admin panel "Add" callback as entry point.
    addchannel_conv = ConversationHandler(
        entry_points=[
            CommandHandler("addchannel", addchannel_start),
            CallbackQueryHandler(
                addchannel_start, pattern="^admin_panel:add$"
            ),
        ],
        states={
            ADDCHANNEL_USERNAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, addchannel_username
                ),
            ],
            ADDCHANNEL_TITLE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, addchannel_title
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", addchannel_cancel)],
    )

    # 5. Interactive /removechannel conversation (group 3).
    #    Legacy /removechannel <slug> is handled inside removechannel_start.
    #    Also accepts admin panel "Remove" callback as entry point.
    removechannel_conv = ConversationHandler(
        entry_points=[
            CommandHandler("removechannel", removechannel_start),
        ],
        states={
            REMOVECHANNEL_SELECT: [
                CallbackQueryHandler(
                    removechannel_select, pattern=r"^rmch:"
                ),
            ],
            REMOVECHANNEL_CONFIRM: [
                CallbackQueryHandler(
                    removechannel_confirm, pattern=r"^rmch_yes:"
                ),
                CallbackQueryHandler(
                    removechannel_cancel_cb, pattern=r"^rmch_no$"
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", removechannel_cancel)],
    )

    # Register add/remove conversations in group 0 (BEFORE the anti-bot
    # ConversationHandler in group 1).  This ensures admin-panel callback
    # queries (admin_panel:add / admin_panel:remove) are matched by these
    # ConversationHandler entry points before the anti-bot handler can
    # intercept them when per_callback_query is not supported.
    app.add_handler(addchannel_conv, group=0)
    app.add_handler(removechannel_conv, group=0)
    app.add_handler(CommandHandler("listchannels", list_channels), group=0)

    # 6. Verify callback (re-checks all channels, unlocks if subscribed).
    app.add_handler(CallbackQueryHandler(
        verify_subscription, pattern="^verify_subscription$",
    ), group=4)

    # 7. Admin panel: add/remove/list callbacks + /admin command.
    #    /admin is NOT added to the BotCommand menu.
    app.add_handler(CallbackQueryHandler(
        admin_panel_callback, pattern=r"^admin_panel:(add|remove|list)$",
    ), group=5)
    app.add_handler(CommandHandler("admin", admin_command), group=5)

    # Register the Mini App menu button (Open button) via post_init.
    # We chain it with the admin command menu setup.
    original_post_init = app.post_init

    async def _combined_post_init(application: Application) -> None:
        if original_post_init is not None:
            await original_post_init(application)
        await setup_menu_button(application)

    app.post_init = _combined_post_init

    logger.info("Bot is starting...")
    # WispByte runs only `python bot.py` — no Procfile, no second process.
    # This process serves the Mini App HTTP on 0.0.0.0:$PORT (Waitress, main
    # thread) while Telegram polling runs in a controlled background thread.
    run_single_entry(app)


# ── Telegram Mini App Menu Button ───────────────────────────────────


async def setup_menu_button(application: Application) -> None:
    """Configure the official Telegram Menu Button (Web App) globally.

    Uses Bot.set_chat_menu_button (without chat_id) to set a MenuButtonWebApp
    with text "Open" that opens the configured MINI_APP_URL.  Telegram places
    this button natively in the composer area for all bot users — no custom
    keyboard is created.
    """
    mini_app_url = get_mini_app_url()
    menu_button = MenuButtonWebApp(
        text="Open",
        web_app=WebAppInfo(url=mini_app_url),
    )
    bot = application.bot
    await bot.set_chat_menu_button(
        menu_button=menu_button,
    )
    logger.info("Set global Mini App menu button → %s", mini_app_url)


if __name__ == "__main__":
    main()
