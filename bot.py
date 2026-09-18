import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

import db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    # Register user with referral attribution
    is_new = db.register_user(
        user_id=user_id,
        username=username,
        first_name=first_name,
        referred_by=referred_by
    )

    # Send welcome message
    await update.message.reply_text("مرحبًا! أنا TaskCoin Bot 🪙\nأرسل /start للبدء.")


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set")

    # Initialize database
    db.init_db()

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))

    logger.info("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
