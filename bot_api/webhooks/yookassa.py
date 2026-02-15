"""YooKassa webhook endpoint for payment notifications.

The endpoint URL includes a secret token from ENV (YOOKASSA_WEBHOOK_SECRET)
to prevent unauthorized access, analogous to the Telegram webhook approach.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from services.payment_service import process_yookassa_webhook, get_payment_user_telegram_id, get_payment_credits
from shared.config import settings
from shared.errors import log_exception, generate_trace_id

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/yookassa/webhook/{secret}")
async def yookassa_webhook(secret: str, request: Request) -> Response:
    """Receive YooKassa payment notifications.

    Validates the secret path parameter against YOOKASSA_WEBHOOK_SECRET.
    Returns 403 if the secret does not match.
    Always returns 200 on valid requests to acknowledge receipt.
    """
    trace_id = generate_trace_id()

    # --- Verify webhook secret ---
    if not settings.YOOKASSA_WEBHOOK_SECRET or secret != settings.YOOKASSA_WEBHOOK_SECRET:
        logger.warning(
            "trace_id=%s | YooKassa webhook: invalid secret in URL", trace_id
        )
        return Response(status_code=403)

    try:
        data = await request.json()
        logger.info("trace_id=%s | YooKassa webhook received: event=%s", trace_id, data.get("event"))

        success = await process_yookassa_webhook(data)

        if success:
            # Try to notify user
            yookassa_id = data.get("object", {}).get("id")
            if yookassa_id:
                telegram_id = await get_payment_user_telegram_id(yookassa_id)
                credits = await get_payment_credits(yookassa_id)
                if telegram_id and credits:
                    try:
                        from bot_api.bot import bot_app
                        from bot_api.keyboards import main_menu_keyboard
                        if bot_app:
                            await bot_app.bot.send_message(
                                chat_id=telegram_id,
                                text=(
                                    f"✅ Оплата прошла успешно!\n\n"
                                    f"💎 Начислено: {credits} кредитов\n"
                                    f"Спасибо за покупку!"
                                ),
                                reply_markup=main_menu_keyboard(),
                            )
                    except Exception:
                        logger.exception("Failed to notify user about payment")

        return Response(status_code=200)

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="yookassa_webhook")
        return Response(status_code=200)
