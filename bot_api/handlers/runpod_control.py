"""Handler for RunPod pod start/stop control (admin only)."""
from __future__ import annotations

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from shared.config import settings
from shared.errors import generate_trace_id, log_exception, safe_user_message
from services.runpod_service import get_pod_status, start_pod, stop_pod, PodStatus

logger = logging.getLogger(__name__)


def _is_admin(telegram_id: int) -> bool:
    """Check if user is admin."""
    return telegram_id in settings.ADMIN_IDS


def pod_control_keyboard(status: PodStatus) -> InlineKeyboardMarkup:
    """Keyboard with Start/Stop buttons depending on current pod status."""
    buttons = []

    if status == PodStatus.RUNNING:
        buttons.append([
            InlineKeyboardButton("⏹ Остановить под", callback_data="pod_stop"),
        ])
    elif status in (PodStatus.EXITED, PodStatus.PAUSED):
        buttons.append([
            InlineKeyboardButton("▶️ Запустить под", callback_data="pod_start"),
        ])
    else:
        # Unknown status — show both buttons
        buttons.append([
            InlineKeyboardButton("▶️ Запустить", callback_data="pod_start"),
            InlineKeyboardButton("⏹ Остановить", callback_data="pod_stop"),
        ])

    buttons.append([
        InlineKeyboardButton("🔄 Обновить статус", callback_data="pod_status"),
    ])
    buttons.append([
        InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu"),
    ])
    return InlineKeyboardMarkup(buttons)


def _status_emoji(status: PodStatus) -> str:
    return {
        PodStatus.RUNNING: "🟢",
        PodStatus.EXITED: "🔴",
        PodStatus.PAUSED: "🟡",
        PodStatus.DEAD: "💀",
        PodStatus.UNKNOWN: "❓",
    }.get(status, "❓")


def _status_text(status: PodStatus) -> str:
    return {
        PodStatus.RUNNING: "Работает",
        PodStatus.EXITED: "Остановлен",
        PodStatus.PAUSED: "На паузе",
        PodStatus.DEAD: "Недоступен",
        PodStatus.UNKNOWN: "Неизвестно",
    }.get(status, "Неизвестно")


async def pod_control_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle menu_pod_control callback — show pod status panel."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    telegram_id = query.from_user.id
    if not _is_admin(telegram_id):
        await query.answer("⛔ Только для администраторов", show_alert=True)
        return

    trace_id = generate_trace_id()
    try:
        await query.edit_message_text("⏳ Получаю статус пода...")
        status, comfyui_url = await get_pod_status()

        emoji = _status_emoji(status)
        status_str = _status_text(status)

        url_line = ""
        if comfyui_url and status == PodStatus.RUNNING:
            url_line = f"\n🔗 ComfyUI: `{comfyui_url}`"

        text = (
            f"🖥 *Управление RunPod подом*\n\n"
            f"Статус: {emoji} *{status_str}*{url_line}\n\n"
            f"Используйте кнопки ниже для управления:"
        )

        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=pod_control_keyboard(status),
        )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="pod_control_callback")
        await query.edit_message_text(safe_user_message(trace_id))


async def pod_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle pod_start callback — start the RunPod pod."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    telegram_id = query.from_user.id
    if not _is_admin(telegram_id):
        await query.answer("⛔ Только для администраторов", show_alert=True)
        return

    trace_id = generate_trace_id()
    try:
        await query.edit_message_text("⏳ Запускаю под... Это займёт 1-3 минуты.")

        success = await start_pod()

        if success:
            text = (
                "✅ *Команда запуска отправлена!*\n\n"
                "⏳ Под запускается — обычно это занимает 1-3 минуты.\n"
                "Нажмите «Обновить статус» через минуту."
            )
        else:
            text = (
                "❌ *Не удалось запустить под*\n\n"
                "Проверьте настройки RUNPOD_API_KEY и RUNPOD_POD_ID.\n"
                "Попробуйте запустить вручную через console.runpod.io"
            )

        # Get updated status
        status, _ = await get_pod_status()

        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=pod_control_keyboard(status),
        )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="pod_start_callback")
        await query.edit_message_text(safe_user_message(trace_id))


async def pod_stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle pod_stop callback — stop the RunPod pod."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    telegram_id = query.from_user.id
    if not _is_admin(telegram_id):
        await query.answer("⛔ Только для администраторов", show_alert=True)
        return

    trace_id = generate_trace_id()
    try:
        await query.edit_message_text("⏳ Останавливаю под...")

        success = await stop_pod()

        if success:
            text = (
                "✅ *Под остановлен!*\n\n"
                "💰 Биллинг за GPU остановлен.\n"
                "Данные сохранены на Volume диске.\n\n"
                "Нажмите «Запустить» когда нужна генерация."
            )
        else:
            text = (
                "❌ *Не удалось остановить под*\n\n"
                "Проверьте настройки RUNPOD_API_KEY и RUNPOD_POD_ID.\n"
                "Попробуйте остановить вручную через console.runpod.io"
            )

        # Get updated status
        status, _ = await get_pod_status()

        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=pod_control_keyboard(status),
        )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="pod_stop_callback")
        await query.edit_message_text(safe_user_message(trace_id))


async def pod_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle pod_status callback — refresh pod status."""
    query = update.callback_query
    if not query:
        return
    await query.answer("🔄 Обновляю...")

    telegram_id = query.from_user.id
    if not _is_admin(telegram_id):
        await query.answer("⛔ Только для администраторов", show_alert=True)
        return

    # Reuse the main control callback
    await pod_control_callback(update, context)
