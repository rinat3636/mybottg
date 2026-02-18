"""User payment status check handler ("✅ Я оплатил")."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from services.payment_service import confirm_payment_and_process
from shared.errors import log_exception, generate_trace_id

logger = logging.getLogger(__name__)


async def check_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    trace_id = generate_trace_id()

    try:
        # callback_data: checkpay_<payment_id>
        yookassa_id = query.data.replace("checkpay_", "", 1).strip()
        if not yookassa_id:
            await query.edit_message_text("❌ Не удалось определить платёж.")
            return

        telegram_id = query.from_user.id
        res = await confirm_payment_and_process(telegram_id, yookassa_id)

        if res is None:
            await query.edit_message_text("❌ Оплата сейчас недоступна (YooKassa не настроена).")
            return

        if res is True:
            await query.edit_message_text("✅ Платёж подтверждён! Кредиты начислены.")
        else:
            await query.edit_message_text(
                "⏳ Платёж пока не подтверждён.\n\n"
                "Если вы оплатили только что — подождите 1–2 минуты и нажмите ещё раз.",
            )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="check_payment_callback")
        try:
            await query.edit_message_text("Произошла ошибка проверки оплаты. Попробуйте позже.")
        except Exception:
            pass
