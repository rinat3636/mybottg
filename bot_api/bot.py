"""Telegram bot application initialization and handler registration."""

from __future__ import annotations

import logging
import re

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from shared.config import settings

logger = logging.getLogger(__name__)

bot_app: Application | None = None


async def create_bot() -> Application:
    """Create and configure the Telegram bot application."""
    global bot_app

    app = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .build()
    )

    # --- Import handlers ---
    from bot_api.handlers.start import (
        start_command,
        help_command,
        balance_command,
        menu_callback,
    )
    from bot_api.handlers.generate import (
        generate_start_callback,
        photo_handler,
        document_image_handler,
        prompt_text_handler,
        text_only_generate_callback,
        gen_again_callback,
        gen_new_callback,
        edit_model_selection_callback,
    )
    from bot_api.handlers.video_generation import (
        video_start_callback,
        video_photo_handler,
        video_prompt_handler,
        video_duration_callback,
    )
    from bot_api.handlers.edit_photo import (
        start_edit_photo,
        receive_photo,
        receive_prompt,
        cancel_edit_photo,
    )
    from bot_api.handlers.animate_photo import (
        start_animate_photo,
        receive_photo_for_animation,
        select_duration,
        cancel_animate_photo,
    )
    from bot_api.handlers.cancel import cancel_command, cancel_callback
    from bot_api.handlers.topup import topup_callback
    from bot_api.handlers.payment_check import check_payment_callback
    from bot_api.handlers.referral import referral_callback
    from bot_api.handlers.support import (
        support_callback,
        support_reply_callback,
    )
    from bot_api.handlers.examples import (
        examples_menu_callback,
        example_pick_callback,
        example_use_callback,
    )
    from bot_api.handlers.admin import (
        stats_command,
        addadmin_command,
        removeadmin_command,
        ban_command,
        unban_command,
        broadcast_command,
    )

    # --- Commands ---
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("addadmin", addadmin_command))
    app.add_handler(CommandHandler("removeadmin", removeadmin_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))

    # --- /reply_TICKET_ID command (regex-based) ---
    from bot_api.handlers.support import reply_command
    app.add_handler(
        MessageHandler(
            filters.Regex(r"^/reply_[A-Za-z0-9]+") & filters.TEXT,
            reply_command,
        )
    )

    # --- Callback queries ---
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu_balance$"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu_topup$"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu_tariffs$"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^back_to_menu$"))
    app.add_handler(CallbackQueryHandler(generate_start_callback, pattern=r"^menu_generate$"))
    app.add_handler(CallbackQueryHandler(start_edit_photo, pattern=r"^menu_edit_photo$"))
    app.add_handler(CallbackQueryHandler(start_animate_photo, pattern=r"^menu_animate_photo$"))
    app.add_handler(CallbackQueryHandler(select_duration, pattern=r"^animate_duration_"))
    app.add_handler(CallbackQueryHandler(video_duration_callback, pattern=r"^video_duration_"))
    app.add_handler(CallbackQueryHandler(examples_menu_callback, pattern=r"^menu_examples$"))
    app.add_handler(CallbackQueryHandler(example_pick_callback, pattern=r"^ex_[a-z]+$"))
    app.add_handler(CallbackQueryHandler(example_use_callback, pattern=r"^ex_use_[a-z]+$"))
    app.add_handler(CallbackQueryHandler(text_only_generate_callback, pattern=r"^gen_text_only$"))
    app.add_handler(CallbackQueryHandler(gen_again_callback, pattern=r"^gen_again$"))
    app.add_handler(CallbackQueryHandler(gen_new_callback, pattern=r"^gen_new$"))
    app.add_handler(CallbackQueryHandler(edit_model_selection_callback, pattern=r"^edit_model_"))
    app.add_handler(CallbackQueryHandler(topup_callback, pattern=r"^topup_\d+$"))
    app.add_handler(CallbackQueryHandler(check_payment_callback, pattern=r"^checkpay_"))
    app.add_handler(CallbackQueryHandler(referral_callback, pattern=r"^menu_referral$"))
    app.add_handler(CallbackQueryHandler(support_callback, pattern=r"^menu_support$"))
    app.add_handler(CallbackQueryHandler(support_reply_callback, pattern=r"^support_reply_"))
    app.add_handler(CallbackQueryHandler(cancel_callback, pattern=r"^cancel_action$"))

    # --- Photo handlers ---
    # Note: These will be handled by state-based logic in the handlers
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))

    # --- Images as documents ---
    app.add_handler(MessageHandler(filters.Document.IMAGE, document_image_handler))

    # --- Text handler (prompt, support messages, etc.) ---
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_text_handler))

    # Initialize and start the application (we do not use polling, but PTB expects start())
    await app.initialize()
    await app.start()

    bot_app = app
    logger.info("Bot application created and initialized")

    return app


async def setup_webhook() -> None:
    """Set up the Telegram webhook."""
    if not bot_app:
        logger.error("Bot not initialized, cannot set webhook")
        return

    if not settings.TELEGRAM_WEBHOOK_URL:
        logger.warning("TELEGRAM_WEBHOOK_URL not set, skipping webhook setup")
        return

    try:
        url = settings.full_webhook_url
        await bot_app.bot.set_webhook(
            url=url,
            secret_token=settings.TELEGRAM_WEBHOOK_SECRET,
            drop_pending_updates=True,
        )
        logger.info("Webhook set: %s", url)
    except Exception:
        logger.exception("Failed to set webhook")


async def shutdown_bot() -> None:
    """Gracefully shut down the bot application."""
    global bot_app
    if bot_app:
        try:
            try:
                await bot_app.stop()
            except Exception:
                logger.exception("Error during bot stop")
            await bot_app.shutdown()
        except Exception:
            logger.exception("Error during bot shutdown")
        bot_app = None
