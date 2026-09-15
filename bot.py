import os
import random
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from config import ADMINS, CHANNELS, Channel, is_admin

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
    """Check the user's answer and stop the flow."""
    expected = context.user_data.get("anti_bot_answer")
    text = update.message.text.strip()

    try:
        user_answer = int(text)
    except ValueError:
        await update.message.reply_text(
            "❌ إجابة غير صحيحة. حاول مرة أخرى."
        )
        return ConversationHandler.END

    if user_answer == expected:
        await update.message.reply_text(
            "✅ تحقق ناجح! أنت لست بوت.\n\n(تم إيقاف التدفق هنا)"
        )
    else:
        await update.message.reply_text(
            "❌ إجابة غير صحيحة.\n\n(تم إيقاف التدفق هنا)"
        )

    # Stop the flow here — no further steps
    context.user_data.pop("anti_bot_answer", None)
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

    logger.info("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
