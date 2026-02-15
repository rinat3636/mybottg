"""Referral program handler."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot_api.keyboards import back_to_menu_keyboard
from services.user_service import get_user_by_telegram_id
from shared.errors import log_exception, generate_trace_id

logger = logging.getLogger(__name__)


async def referral_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle menu_referral callback — show referral link."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    trace_id = generate_trace_id()

    try:
        telegram_id = query.from_user.id
        user = await get_user_by_telegram_id(telegram_id)

        if not user:
            await query.edit_message_text("❌ Нажмите /start")
            return

        bot_me = await context.bot.get_me()
        bot_username = bot_me.username

        ref_link = f"https://t.me/{bot_username}?start=ref_{user.referral_code}"

        await query.edit_message_text(
            "👥 *Реферальная программа*\n\n"
            "Пригласи друга — оба получите по *11 бесплатных кредитов*!\n\n"
            f"🔗 Твоя ссылка:\n`{ref_link}`\n\n"
            "Просто отправь эту ссылку другу.",
            parse_mode="Markdown",
            reply_markup=back_to_menu_keyboard(),
        )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="referral_callback")
