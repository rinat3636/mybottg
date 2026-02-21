"""Start command and main menu handlers."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot_api.keyboards import (
    main_menu_keyboard,
    back_to_menu_keyboard,
    support_link_keyboard,
)
from services.user_service import (
    get_or_create_user,
    get_user_by_referral_code,
)
from shared.config import settings
from shared.redis_client import clear_user_state
from shared.errors import log_exception, safe_user_message, generate_trace_id

logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "Привет, *{name}*! 👋\n\n"
    "Я AI-бот для работы с изображениями и видео на базе *ComfyUI* + *RunPod*.\n\n"
    "*Что я умею:*\n"
    "🖼️ *Редактировать фото* — изменить стиль, фон, детали по текстовому описанию\n"
    "🎬 *Оживить фото* — создать 10-секундное видео из вашего фото\n"
    "🧙 *Создать изображение* — генерация с нуля по текстовому промту\n\n"
    "Выберите действие в меню ниже:"
)

HELP_TEXT = (
    "📖 *Справка*\n\n"
    "*🖼️ Редактирование фото:*\n"
    "Нажмите «Редактировать фото», отправьте фото и опишите что изменить.\n\n"
    "*🎬 Оживление фото (видео):*\n"
    "Нажмите «Оживить фото», отправьте фото и опишите движение.\n"
    "Результат: видео 10 секунд.\n\n"
    "*🧙 Создание изображения:*\n"
    "Нажмите «Создать изображение», напишите текстовый промт.\n\n"
    "*Команды:*\n"
    "/start — Главное меню\n"
    "/help — Справка\n"
    "/cancel — Отменить текущую операцию\n"
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    if not update.effective_user or not update.message:
        return
    trace_id = generate_trace_id()
    try:
        tg_user = update.effective_user
        telegram_id = tg_user.id

        # Parse referral code from deep link: /start ref_XXXXXX
        referrer_tg_id = None
        if context.args and len(context.args) > 0:
            arg = context.args[0]
            if arg.startswith("ref_"):
                ref_code = arg[4:]
                referrer = await get_user_by_referral_code(ref_code)
                if referrer and referrer.telegram_id != telegram_id:
                    referrer_tg_id = referrer.telegram_id

        _user, _created = await get_or_create_user(
            telegram_id=telegram_id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            referrer_telegram_id=referrer_tg_id,
        )

        await clear_user_state(telegram_id)

        name = tg_user.first_name or tg_user.username or "друг"
        is_admin = telegram_id in settings.ADMIN_IDS

        await update.message.reply_text(
            WELCOME_TEXT.format(name=name),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(is_admin=is_admin),
        )
    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="start_command")
        try:
            await update.message.reply_text(safe_user_message(trace_id))
        except Exception:
            logger.exception("Failed to send error message")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    if not update.message:
        return
    await update.message.reply_text(
        HELP_TEXT,
        parse_mode="Markdown",
        reply_markup=back_to_menu_keyboard(),
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle back_to_menu and menu_support callbacks."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    trace_id = generate_trace_id()
    try:
        telegram_id = query.from_user.id

        if query.data == "back_to_menu":
            name = query.from_user.first_name or query.from_user.username or "друг"
            await clear_user_state(telegram_id)
            is_admin = telegram_id in settings.ADMIN_IDS
            await query.edit_message_text(
                WELCOME_TEXT.format(name=name),
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(is_admin=is_admin),
            )

        elif query.data == "menu_support":
            await query.edit_message_text(
                "💬 *Поддержка*\n\nЕсли у вас возникли вопросы или проблемы — напишите нам.",
                parse_mode="Markdown",
                reply_markup=support_link_keyboard(),
            )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="menu_callback")
