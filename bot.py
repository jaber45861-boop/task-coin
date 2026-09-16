import os
import random
import logging
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from config import ADMINS, CHANNELS, Channel, is_admin, get_required_channels
from subscription import (
    check_subscription_access,
    is_locked,
    lock_user,
    unlock_user,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

ANTI_BOT = 0


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send a simple math question as anti-bot step."""
    a = random.randint(1, 20)
    b = random.randint(1, 20)
    op = random.choice(["+", "-"])

    # For subtraction, ensure non-negative result
    if op == "-" and a < b:
        a, b = b, a

    correct = a + b if op == "+" else a - b
    context.user_data["anti_bot_answer"] = correct

    await update.message.reply_text(
        f"🔒 للتحقق أنك لست بوت:\n\nما ناتج: {a} {op} {b}؟"
    )
    return ANTI_BOT


async def check_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Check the user's answer, then verify channel subscriptions."""
    expected = context.user_data.get("anti_bot_answer")
    text = update.message.text.strip()

    try:
        user_answer = int(text)
    except ValueError:
        await update.message.reply_text(
            "❌ إجابة غير صحيحة. حاول مرة أخرى."
        )
        context.user_data.pop("anti_bot_answer", None)
        return ConversationHandler.END

    if user_answer != expected:
        await update.message.reply_text("❌ إجابة غير صحيحة.")
        context.user_data.pop("anti_bot_answer", None)
        return ConversationHandler.END

    # ── Anti-bot passed — check channel subscriptions ───────────────
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


# ── Admin: Add Channel ───────────────────────────────────────────────

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a mandatory subscription channel. Admin only.

    Usage: /addchannel slug|channel_id|username|title
    Example: /addchannel main|-1001234567890|mychannel|My Channel
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

    if len(parts) != 4:
        await update.message.reply_text(
            "❌ صيغة خاطئة. استخدم:\n"
            "/addchannel slug|channel_id|username|title\n\n"
            "مثال:\n"
            "/addchannel main|-1001234567890|mychannel|My Channel"
        )
        return

    slug, channel_id_str, username, title = parts

    # Validate slug
    if not slug or not slug.isalnum() and "_" not in slug:
        await update.message.reply_text(
            "❌ الـslug يجب أن يحتوي على أحرف وأرقام فقط (والشرطة السفلية _)."
        )
        return

    # Validate channel_id
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        await update.message.reply_text("❌ channel_id يجب أن يكون رقمًا.")
        return

    # Prevent duplicate slug
    if slug in CHANNELS:
        await update.message.reply_text(
            f"❌ الـslug '{slug}' موجود مسبقًا."
        )
        return

    # Prevent duplicate channel_id
    for existing in CHANNELS.values():
        if existing.channel_id == channel_id:
            await update.message.reply_text(
                f"❌ القناة بالمعرف {channel_id} موجودة مسبقًا "
                f"(slug: {existing.slug})."
            )
            return

    # ── Validate channel access via Telegram Bot API ────────────────
    try:
        chat = await context.bot.get_chat(channel_id)
    except TelegramError as exc:
        await update.message.reply_text(
            "❌ تعذر الوصول للقناة. تأكد من:\n"
            "• المعرف صحيح (يبدأ بـ -100 للقنوات العامة)\n"
            "• البوت مضاف للقناة\n\n"
            f"تفاصيل الخطأ: {exc}"
        )
        return

    # Verify the bot can read membership info (must be admin in channel)
    try:
        bot_member = await context.bot.get_chat_member(
            channel_id, context.bot.id
        )
    except TelegramError as exc:
        await update.message.reply_text(
            "❌ تعذر التحقق من عضوية البوت في القناة.\n"
            "تأكد أن البوت مشرف (admin) في القناة\n"
            "ليتمكن من فحص اشتراك المستخدمين لاحقًا.\n\n"
            f"تفاصيل الخطأ: {exc}"
        )
        return

    if bot_member.status not in ("administrator", "creator"):
        await update.message.reply_text(
            f"❌ البوت ليس مشرفًا في القناة (حالته: {bot_member.status})\n"
            "يجب أن يكون البوت *مشرفًا* (admin) في القناة\n"
            "ليتمكن من فحص اشتراك المستخدمين لاحقًا."
        )
        return

    # Add the channel
    new_channel = Channel(
        slug=slug,
        channel_id=channel_id,
        username=username,
        title=title,
        required=True,
    )
    CHANNELS[slug] = new_channel

    await update.message.reply_text(
        "✅ تمت إضافة القناة:\n\n"
        f"📌 Slug: {slug}\n"
        f"🆔 ID: {channel_id}\n"
        f"📛 Username: @{username}\n"
        f"📝 Title: {title}\n"
        f"🔒 Required: نعم"
    )
    logger.info("Channel added: %s (id=%d) by admin %d", slug, channel_id, user_id)


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

    await update.message.reply_text(
        "✅ تم حذف القناة:\n\n"
        f"📌 Slug: {removed.slug}\n"
        f"🆔 ID: {removed.channel_id}\n"
        f"📛 Username: @{removed.username}\n"
        f"📝 Title: {removed.title}"
    )
    logger.info("Channel removed: %s (id=%d) by admin %d", slug, removed.channel_id, user_id)

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

    Placed before the anti-bot MessageHandler so locked users cannot
    proceed through the anti-bot conversation.
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


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set")

    _refresh_required_ids()

    app = ApplicationBuilder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ANTI_BOT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, check_answer),
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

    # 3. Subscription gate for non-command messages (before anti-bot).
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            subscription_message_gate,
        ),
        group=2,
    )

    # 4. Protected commands: subscription gate + admin logic combined.
    #    Non-admin locked users are blocked before admin handlers fire.
    app.add_handler(CommandHandler("addchannel", add_channel), group=3)
    app.add_handler(CommandHandler("listchannels", list_channels), group=3)
    app.add_handler(CommandHandler("removechannel", remove_channel), group=3)

    # 6. Verify callback (re-checks all channels, unlocks if subscribed).
    app.add_handler(CallbackQueryHandler(
        verify_subscription, pattern="^verify_subscription$",
    ), group=4)

    logger.info("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
