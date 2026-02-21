"""Handler for photo animation via ComfyUI WanVideo (photo + prompt → 10 sec video)."""
from __future__ import annotations

import logging
import uuid
from io import BytesIO

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from shared.redis_client import (
    set_user_state,
    clear_user_state,
    set_user_data,
    get_user_data,
    enqueue_task,
    QueueLimitError,
)
from services.user_service import get_or_create_user
from bot_api.keyboards import back_to_menu_keyboard, cancel_keyboard
from shared.errors import log_exception, generate_trace_id

logger = logging.getLogger(__name__)

WAITING_FOR_PHOTO = 1
WAITING_FOR_PROMPT = 2


async def start_animate_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start photo animation flow — triggered by menu button."""
    query = update.callback_query
    await query.answer()
    telegram_id = query.from_user.id
    await set_user_state(telegram_id, "animate_photo_waiting_photo")
    await query.edit_message_text(
        "🎬 *Оживление фото*\n\n"
        "Отправьте фото, которое хотите оживить.\n\n"
        "_После фото я попрошу вас описать движение — и создам видео 10 секунд._",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard(),
    )
    return WAITING_FOR_PHOTO


async def receive_photo_for_animation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive photo from user."""
    if not update.message or not update.message.photo:
        return WAITING_FOR_PHOTO
    telegram_id = update.effective_user.id
    photo = update.message.photo[-1]
    photo_file = await context.bot.get_file(photo.file_id)
    buf = BytesIO()
    await photo_file.download_to_memory(buf)
    photo_bytes = buf.getvalue()
    await set_user_data(telegram_id, {
        "photo_hex": photo_bytes.hex(),
        "task_type": "animate_photo",
    })
    await set_user_state(telegram_id, "animate_photo_waiting_prompt")
    await update.message.reply_text(
        "✅ Фото получено!\n\n"
        "Теперь опишите *движение* — что должно происходить в видео.\n\n"
        "*Примеры:*\n"
        "• «Волосы развеваются на ветру»\n"
        "• «Камера медленно приближается»\n"
        "• «Человек улыбается и моргает»\n"
        "• «Листья деревьев колышутся»",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard(),
    )
    return WAITING_FOR_PROMPT


async def receive_prompt_for_animation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive prompt and enqueue animation task."""
    if not update.message or not update.message.text:
        return WAITING_FOR_PROMPT
    telegram_id = update.effective_user.id
    prompt = update.message.text.strip()
    if not prompt:
        await update.message.reply_text(
            "⚠️ Пожалуйста, опишите движение.",
            reply_markup=cancel_keyboard(),
        )
        return WAITING_FOR_PROMPT
    user_data = await get_user_data(telegram_id)
    photo_hex = user_data.get("photo_hex") if user_data else None
    if not photo_hex:
        await update.message.reply_text(
            "❌ Фото не найдено. Начните заново.",
            reply_markup=back_to_menu_keyboard(),
        )
        await clear_user_state(telegram_id)
        return ConversationHandler.END
    user, _ = await get_or_create_user(telegram_id)
    task_id = uuid.uuid4().hex
    trace_id = generate_trace_id()
    try:
        await enqueue_task(task_id, {
            "task_id": task_id,
            "task_type": "animate_photo",
            "telegram_id": telegram_id,
            "user_id": user.id,
            "chat_id": update.message.chat_id,
            "photo_hex": photo_hex,
            "prompt": prompt,
            "duration_seconds": 10,
        })
        await update.message.reply_text(
            f"⏳ *Задача принята!*\n\n"
            f"Промт: _{prompt}_\n"
            f"Длительность: *10 секунд*\n\n"
            f"Генерирую видео через ComfyUI WanVideo...\n"
            f"⏱️ Это займёт 2–5 минут.",
            parse_mode="Markdown",
            reply_markup=back_to_menu_keyboard(),
        )
    except QueueLimitError:
        await update.message.reply_text(
            "⏳ Очередь переполнена. Попробуйте через несколько секунд.",
            reply_markup=back_to_menu_keyboard(),
        )
    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="animate_photo.receive_prompt")
        await update.message.reply_text(
            "❌ Ошибка при создании задачи. Попробуйте позже.",
            reply_markup=back_to_menu_keyboard(),
        )
    await clear_user_state(telegram_id)
    return ConversationHandler.END


async def cancel_animate_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel photo animation."""
    query = update.callback_query
    if query:
        await query.answer()
        telegram_id = query.from_user.id
        await clear_user_state(telegram_id)
        from bot_api.keyboards import main_menu_keyboard
        from shared.config import settings
        is_admin = telegram_id in settings.ADMIN_IDS
        await query.edit_message_text(
            "❌ Отменено.",
            reply_markup=main_menu_keyboard(is_admin=is_admin),
        )
    return ConversationHandler.END
