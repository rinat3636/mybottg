"""Telegram webhook endpoint with secret header verification."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from shared.config import settings
from shared.errors import log_exception, generate_trace_id

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(f"/webhook/telegram/{settings.TELEGRAM_WEBHOOK_SECRET}")
async def telegram_webhook(request: Request) -> Response:
    """Receive Telegram updates via webhook.

    Validates ``X-Telegram-Bot-Api-Secret-Token`` header.
    """
    trace_id = generate_trace_id()
    
    logger.info("trace_id=%s | Received webhook request", trace_id)

    try:
        # Verify secret header
        secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        logger.info("trace_id=%s | Secret header present: %s", trace_id, bool(secret_header))
        
        if secret_header != settings.TELEGRAM_WEBHOOK_SECRET:
            logger.warning(
                "trace_id=%s | Telegram webhook: invalid secret header (expected=%s, got=%s)", 
                trace_id, 
                settings.TELEGRAM_WEBHOOK_SECRET[:4] + "...", 
                secret_header[:4] + "..." if secret_header else "empty"
            )
            return Response(status_code=403)

        from bot_api.bot import bot_app
        from telegram import Update

        if bot_app is None:
            logger.error("trace_id=%s | Bot application not initialized", trace_id)
            return Response(status_code=500)

        data = await request.json()
        logger.info("trace_id=%s | Webhook data received: %s", trace_id, str(data)[:200])
        
        update = Update.de_json(data, bot_app.bot)
        logger.info("trace_id=%s | Update parsed, processing...", trace_id)

        # Process update
        await bot_app.process_update(update)
        
        logger.info("trace_id=%s | Update processed successfully", trace_id)

        return Response(status_code=200)

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="telegram_webhook")
        return Response(status_code=200)  # Always return 200 to Telegram
