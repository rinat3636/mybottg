"""Start command, /balance, tariffs screen, and main menu handlers."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot_api.keyboards import (
    main_menu_keyboard,
    back_to_menu_keyboard,
    topup_keyboard,
)
from services.user_service import (
    get_or_create_user,
    get_user_by_referral_code,
    get_user_by_telegram_id,
)
from shared.config import GENERATION_COST, CREDIT_PACKAGES
from shared.redis_client import clear_user_state
from shared.errors import log_exception, safe_user_message, generate_trace_id

logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "Добро пожаловать, *{name}*!\n\n"
    "Вы подключились к *Nano Banana* — профессиональной AI-платформе для создания и трансформации визуального контента.\n\n"
    "*Наши возможности:*\n"
    "✦ **Редактирование изображений** — трансформация стиля, фона, деталей с сохранением исходной композиции\n"
    "✦ **Генерация с нуля** — создание уникальных изображений по текстовому описанию\n"
    "✦ **Видео из фото** — превращение статичных кадров в динамичные видеоролики\n\n"
    "Выберите нужный инструмент ниже, чтобы начать работу."
)

HELP_TEXT = (
    "📖 *Руководство по использованию*\n\n"
    "**Как работать с изображениями:**\n"
    "• Отправьте фото и текстовое описание желаемого результата\n"
    "• Порядок не важен — система автоматически определит последовательность\n\n"
    "**Тарифы:**\n"
    "• Nano Banana — 5 кредитов (быстрая генерация)\n"
    "• Flux 2 Pro — 9 кредитов (профессиональное качество)\n"
    "• Nano Banana Pro — 11 кредитов (премиум-результат)\n"
    "• Riverflow 2.0 PRO — 45 кредитов (максимальная детализация)\n"
    "• Видео 5 сек — 70 кредитов\n"
    "• Видео 10 сек — 140 кредитов\n\n"
    "**Баланс и пополнение:**\n"
    "• Новым пользователям — 11 кредитов в подарок\n"
    "• Реферальная программа — по 5 кредитов за каждого приглашенного\n"
    "• Пополнение доступно в разделе «💰 Пополнить»\n\n"
    "**Команды:**\n"
    "/start — Главное меню\n"
    "/help — Справка\n"
    "/balance — Проверить баланс\n"
    "/cancel — Отменить текущую операцию\n"
)

BALANCE_TEXT = (
    "💎 **Ваш баланс:** {balance} кредитов\n\n"
    "**Тарифы на генерацию:**\n"
    "• Nano Banana — 5 кредитов\n"
    "• Flux 2 Pro — 9 кредитов\n"
    "• Nano Banana Pro — 11 кредитов\n"
    "• Riverflow 2.0 PRO — 45 кредитов\n"
    "• Видео (5 сек) — 70 кредитов\n"
    "• Видео (10 сек) — 140 кредитов\n\n"
    "_1 кредит = 1 рубль_"
)


def _build_tariffs_text() -> str:
    """Build a beautiful tariffs screen text."""
    packages_lines = []
    for rub, credits in sorted(CREDIT_PACKAGES.items()):
        packages_lines.append(f"    💳 {rub} ₽ → {credits} кредитов")
    packages_block = "\n".join(packages_lines)

    return (
        "💼 **Тарифы и услуги**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "**🎨 Редактирование и генерация:**\n\n"
        "⚡ *Nano Banana* — 5 кредитов\n"
        "   Быстрая генерация, стандартное качество\n\n"
        "✨ *Flux 2 Pro* — 9 кредитов\n"
        "   Профессиональное качество, отличный рендеринг текста\n\n"
        "🔥 *Nano Banana Pro* — 11 кредитов\n"
        "   Премиум-качество, фотореализм\n\n"
        "💎 *Riverflow 2.0 PRO* — 45 кредитов\n"
        "   Максимальная детализация, студийное качество\n\n"
        "**🎬 Видео из изображения:**\n\n"
        "⚡ *5 секунд* — 70 кредитов\n"
        "   Короткий динамичный клип\n\n"
        "⭐ *10 секунд* — 140 кредитов\n"
        "   Полноценный видеоролик с плавными переходами\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "**💰 Пакеты пополнения:**\n\n"
        f"{packages_block}\n\n"
        "_Соотношение: 1 кредит = 1 рубль_\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "**🎁 Бонусная программа:**\n\n"
        "✦ **Новым пользователям** — 11 кредитов в подарок\n"
        "✦ **Реферальная программа** — по 5 кредитов за каждого приглашенного друга\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━"
    )


def _tariffs_keyboard():
    """Inline keyboard for the tariffs screen with top-up buttons."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    buttons = []
    for rub in sorted(CREDIT_PACKAGES.keys()):
        buttons.append([InlineKeyboardButton(f"💳 Пополнить {rub} ₽", callback_data=f"topup_{rub}")])
    buttons.append([InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(buttons)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command, optionally with referral code."""
    if not update.effective_user or not update.message:
        logger.warning("start_command: no effective_user or message")
        return

    trace_id = generate_trace_id()
    logger.info("trace_id=%s | start_command called for user %s", trace_id, update.effective_user.id)

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

        logger.info("trace_id=%s | Creating/fetching user %s", trace_id, telegram_id)
        user, created = await get_or_create_user(
            telegram_id=telegram_id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            referrer_telegram_id=referrer_tg_id,
        )
        logger.info("trace_id=%s | User fetched, created=%s", trace_id, created)

        # Clear any FSM state
        await clear_user_state(telegram_id)

        name = tg_user.first_name or tg_user.username or "друг"

        extra = ""
        if created:
            extra = "\n\n🎁 Вам начислено 11 бесплатных кредитов для теста бота!"
            if referrer_tg_id:
                extra += "\n🎁 По реферальной ссылке начислено ещё 11 кредитов!"

        logger.info("trace_id=%s | Sending welcome message", trace_id)
        await update.message.reply_text(
            WELCOME_TEXT.format(name=name) + extra,
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        logger.info("trace_id=%s | Welcome message sent successfully", trace_id)
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


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /balance command."""
    if not update.message or not update.effective_user:
        return

    trace_id = generate_trace_id()
    try:
        telegram_id = update.effective_user.id
        user = await get_user_by_telegram_id(telegram_id)
        balance = user.balance if user else 0

        await update.message.reply_text(
            BALANCE_TEXT.format(
                balance=balance,
                pro_cost=GENERATION_COST["nano_banana_pro"],
            ),
            parse_mode="Markdown",
            reply_markup=topup_keyboard(),
        )
    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="balance_command")
        await update.message.reply_text(safe_user_message(trace_id))


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle menu_balance, menu_topup, menu_tariffs, and back_to_menu callbacks."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    trace_id = generate_trace_id()

    try:
        telegram_id = query.from_user.id

        if query.data == "menu_balance":
            user = await get_user_by_telegram_id(telegram_id)
            balance = user.balance if user else 0
            await query.edit_message_text(
                BALANCE_TEXT.format(
                    balance=balance,
                    pro_cost=GENERATION_COST["nano_banana_pro"],
                ),
                parse_mode="Markdown",
                reply_markup=topup_keyboard(),
            )

        elif query.data == "menu_topup":
            await query.edit_message_text(
                "💰 *Пополнение баланса*\n\nВыберите пакет кредитов:",
                parse_mode="Markdown",
                reply_markup=topup_keyboard(),
            )

        elif query.data == "menu_tariffs":
            await query.edit_message_text(
                _build_tariffs_text(),
                parse_mode="Markdown",
                reply_markup=_tariffs_keyboard(),
            )

        elif query.data == "back_to_menu":
            name = query.from_user.first_name or query.from_user.username or "друг"
            await clear_user_state(telegram_id)
            await query.edit_message_text(
                WELCOME_TEXT.format(name=name),
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="menu_callback")
