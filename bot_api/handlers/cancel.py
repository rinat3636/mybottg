"""Cancel action handler — cancels FSM state and queued/processing tasks.

Real cancellation:
- If task is still in queue → removed, credits refunded immediately.
- If task is already processing → marked as cancelled, result discarded,
  credits refunded by the worker.
- User always gets a confirmation message.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot_api.keyboards import main_menu_keyboard
from shared.admin_guard import refund_if_needed
from shared.redis_client import (
    clear_user_state,
    get_active_generation,
    get_task_payload,
    cancel_task,
    cancel_processing_task,
    release_generation_lock,
)
from shared.errors import log_exception, generate_trace_id

logger = logging.getLogger(__name__)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel command."""
    if not update.message or not update.effective_user:
        return

    trace_id = generate_trace_id()
    try:
        telegram_id = update.effective_user.id
        result = await _do_cancel(telegram_id)

        if result == "cancelled_queued":
            await update.message.reply_text(
                "❌ Задача отменена. Кредиты возвращены.",
                reply_markup=main_menu_keyboard(),
            )
        elif result == "cancelled_processing":
            await update.message.reply_text(
                "❌ Генерация отменена. Результат не будет отправлен, кредиты будут возвращены.",
                reply_markup=main_menu_keyboard(),
            )
        else:
            await clear_user_state(telegram_id)
            await update.message.reply_text(
                "❌ Действие отменено.",
                reply_markup=main_menu_keyboard(),
            )
    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="cancel_command")
        await update.message.reply_text("Произошла ошибка, попробуйте позже.")


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle cancel_action callback."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    trace_id = generate_trace_id()
    try:
        telegram_id = query.from_user.id
        result = await _do_cancel(telegram_id)

        if result == "cancelled_queued":
            await query.edit_message_text(
                "❌ Задача отменена. Кредиты возвращены.\n\nВыберите действие из меню:",
                reply_markup=main_menu_keyboard(),
            )
        elif result == "cancelled_processing":
            await query.edit_message_text(
                "❌ Генерация отменена. Результат не будет отправлен, кредиты будут возвращены.\n\n"
                "Выберите действие из меню:",
                reply_markup=main_menu_keyboard(),
            )
        else:
            await clear_user_state(telegram_id)
            await query.edit_message_text(
                "❌ Действие отменено.\n\nВыберите действие из меню:",
                reply_markup=main_menu_keyboard(),
            )
    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="cancel_callback")


async def _do_cancel(telegram_id: int) -> str:
    """Try to cancel the active generation task.

    Returns:
        "cancelled_queued"     — task was in queue, removed, credits refunded.
        "cancelled_processing" — task was processing, marked cancelled.
        "no_task"              — no active task found.
    """
    task_id = await get_active_generation(telegram_id)
    if not task_id:
        return "no_task"

    # Try to cancel a queued task first
    cancelled_queued = await cancel_task(task_id)
    if cancelled_queued:
        # Refund credits immediately
        payload = await get_task_payload(task_id)
        if payload:
            await refund_if_needed(
                user_id=payload.get("user_id", 0),
                is_admin=payload.get("is_admin", False),
                cost=payload.get("cost", 0),
                request_id=payload.get("request_id", task_id),
                tariff=payload.get("tariff", "nano_banana_pro"),
            )
        await release_generation_lock(telegram_id)
        await clear_user_state(telegram_id)
        return "cancelled_queued"

    # Try to cancel a processing task (worker will handle refund)
    cancelled_processing = await cancel_processing_task(task_id)
    if cancelled_processing:
        await release_generation_lock(telegram_id)
        await clear_user_state(telegram_id)
        return "cancelled_processing"

    # Task already completed/failed — just clean up
    await release_generation_lock(telegram_id)
    await clear_user_state(telegram_id)
    return "no_task"
