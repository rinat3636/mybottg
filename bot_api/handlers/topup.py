"""Top-up handler — create YooKassa payment link."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot_api.keyboards import topup_keyboard, main_menu_keyboard
from services.payment_service import create_payment
from shared.config import CREDIT_PACKAGES
from shared.errors import log_exception, generate_trace_id

logger = logging.getLogger(__name__)


async def topup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle topup_100, topup_200, topup_300, topup_500 callbacks."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    trace_id = generate_trace_id()

    try:
        telegram_id = query.from_user.id

        # topup_100 → 100
        try:
            amount_rub = int(query.data.replace("topup_", "", 1))
        except ValueError:
            await query.edit_message_text("❌ Неверная сумма.")
            return

        if amount_rub not in CREDIT_PACKAGES:
            await query.edit_message_text("❌ Неверный пакет.")
            return

        credits = CREDIT_PACKAGES[amount_rub]

        result = await create_payment(telegram_id, amount_rub)

        if result:
            await query.edit_message_text(
                f"💳 *Оплата {amount_rub}₽ → {credits} кредитов*\n\n"
                f"Нажмите кнопку ниже для перехода к оплате.\n"
                f"После оплаты кредиты будут начислены автоматически.",
                parse_mode="Markdown",
                reply_markup=_payment_link_keyboard(result["payment_url"]),
            )
        else:
            await query.edit_message_text(
                "❌ Не удалось создать платёж. Попробуйте позже.",
                reply_markup=topup_keyboard(),
            )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="topup_callback")
        try:
            await query.edit_message_text("Произошла ошибка, попробуйте позже.")
        except Exception:
            pass


def _payment_link_keyboard(url: str):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 Оплатить", url=url)],
            [InlineKeyboardButton("◀️ Назад", callback_data="menu_topup")],
        ]
    )
