"""Telegram bot application initialization and handler registration."""
from __future__ import annotations

import logging

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
        menu_callback,
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
        receive_prompt_for_animation,
        cancel_animate_photo,
    )
    from bot_api.handlers.generate import (
        generate_start_callback,
        photo_handler,
        document_image_handler,
        prompt_text_handler,
        gen_again_callback,
        gen_new_callback,
    )
    from bot_api.handlers.cancel import cancel_command, cancel_callback
    from bot_api.handlers.support import (
        support_callback,
        support_reply_callback,
        reply_command,
    )
    from bot_api.handlers.admin import (
        stats_command,
        addadmin_command,
        removeadmin_command,
        ban_command,
        unban_command,
        broadcast_command,
    )
    from bot_api.handlers.runpod_control import (
        pod_control_callback,
        pod_start_callback,
        pod_stop_callback,
        pod_status_callback,
    )

    # --- Commands ---
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("addadmin", addadmin_command))
    app.add_handler(CommandHandler("removeadmin", removeadmin_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))

    # --- /reply_TICKET_ID command (regex-based) ---
    app.add_handler(
        MessageHandler(
            filters.Regex(r"^/reply_[A-Za-z0-9]+") & filters.TEXT,
            reply_command,
        )
    )

    # --- Main menu ---
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^back_to_menu$"))

    # --- Edit photo ---
    app.add_handler(CallbackQueryHandler(start_edit_photo, pattern=r"^menu_edit_photo$"))

    # --- Animate photo (video 10 sec) ---
    app.add_handler(CallbackQueryHandler(start_animate_photo, pattern=r"^menu_animate_photo$"))

    # --- Generate image ---
    app.add_handler(CallbackQueryHandler(generate_start_callback, pattern=r"^menu_generate$"))
    app.add_handler(CallbackQueryHandler(gen_again_callback, pattern=r"^gen_again$"))
    app.add_handler(CallbackQueryHandler(gen_new_callback, pattern=r"^gen_new$"))

    # --- Support ---
    app.add_handler(CallbackQueryHandler(support_callback, pattern=r"^menu_support$"))
    app.add_handler(CallbackQueryHandler(support_reply_callback, pattern=r"^support_reply_"))

    # --- Cancel ---
    app.add_handler(CallbackQueryHandler(cancel_callback, pattern=r"^cancel_action$"))

    # --- RunPod pod control (admin only) ---
    app.add_handler(CallbackQueryHandler(pod_control_callback, pattern=r"^menu_pod_control$"))
    app.add_handler(CallbackQueryHandler(pod_start_callback, pattern=r"^pod_start$"))
    app.add_handler(CallbackQueryHandler(pod_stop_callback, pattern=r"^pod_stop$"))
    app.add_handler(CallbackQueryHandler(pod_status_callback, pattern=r"^pod_status$"))

    # --- Photo / document / text message handlers ---
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.Document.IMAGE, document_image_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_text_handler))

    # Initialize and start the application
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
