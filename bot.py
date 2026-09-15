import os
import random
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from config import ADMINS, CHANNELS, Channel, is_admin, get_required_channels

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
            # Cannot verify this channel — treat as missing
            missing.append(ch)

    context.user_data.pop("anti_bot_answer", None)

    if not missing:
        await update.message.reply_text(
            "✅ تحقق ناجح! أنت لست بوت.\n"
            "✅ أنت مشترك في جميع القنوات المطلوبة."
        )
    else:
        buttons = [
            [InlineKeyboardButton(
                text=f"📢 {ch.title}",
                url=f"https://t.me/{ch.username}",
            )]
            for ch in missing
        ]
        names = "\n".join(f"  • {ch.title}" for ch in missing)
        await update.message.reply_text(
            "✅ تحقق ناجح! أنت لست بوت.\n\n"
            "⚠️ أنت غير مشترك في القنوات التالية:\n"
            f"{names}\n\n"
            "اضغط الزر للاشتراك ثم أعد المحاولة:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    return ConversationHandler.END


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


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set")

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

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("addchannel", add_channel))
    app.add_handler(CommandHandler("listchannels", list_channels))
    app.add_handler(CommandHandler("removechannel", remove_channel))

    logger.info("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
