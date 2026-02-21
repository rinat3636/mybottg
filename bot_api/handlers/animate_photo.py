"""Handler for photo animation (LivePortrait)."""

from __future__ import annotations

import logging
from io import BytesIO

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
from services.generation_service import deduct_for_generation
from bot_api.keyboards import (
    back_to_menu_keyboard,
    insufficient_funds_keyboard,
    cancel_keyboard,
)

logger = logging.getLogger(__name__)

# Conversation states
WAITING_FOR_PHOTO = 1
WAITING_FOR_DURATION = 2

# Credit costs for different durations
ANIMATE_COSTS = {
    10: 50,  # 10 seconds = 50 credits
    15: 70,  # 15 seconds = 70 credits
}


def duration_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for selecting animation duration."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"⚡ 10 секунд — {ANIMATE_COSTS[10]} кредитов",
                callback_data="animate_duration_10"
            ),
        ],
        [
            InlineKeyboardButton(
                f"⭐ 15 секунд — {ANIMATE_COSTS[15]} кредитов",
                callback_data="animate_duration_15"
            ),
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")],
    ])


async def start_animate_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start photo animation flow."""
    query = update.callback_query
    await query.answer()
    
    telegram_id = query.from_user.id
    user = await get_or_create_user(telegram_id)
    
    # Check credits (minimum for 10 seconds)
    min_cost = ANIMATE_COSTS[10]
    if user.credits < min_cost:
        await query.edit_message_text(
            f"❌ Недостаточно кредитов!\n\n"
            f"Для оживления фото нужно минимум {min_cost} кредитов.\n"
            f"Ваш баланс: {user.credits} кредитов.",
            reply_markup=insufficient_funds_keyboard(),
        )
        return ConversationHandler.END
    
    # Set state
    await set_user_state(telegram_id, "animate_photo_waiting_photo")
    
    await query.edit_message_text(
        "🎬 **Оживление фото (LivePortrait)**\n\n"
        "Загрузите фото с лицом, которое хотите оживить.\n\n"
        "✨ **Что получится:**\n"
        "• Естественное моргание\n"
        "• Легкие движения головы\n"
        "• Мимика лица\n"
        "• Плавная анимация\n\n"
        "⚠️ **Требования:**\n"
        "• На фото должно быть четкое лицо\n"
        "• Лицо хорошо освещено\n"
        "• Лицо не закрыто\n\n"
        f"💎 Стоимость: от {min_cost} кредитов",
        reply_markup=cancel_keyboard(),
        parse_mode="Markdown",
    )
    
    return WAITING_FOR_PHOTO


async def receive_photo_for_animation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive photo from user."""
    telegram_id = update.effective_user.id
    
    # Get the largest photo
    photo = update.message.photo[-1]
    
    # Download photo
    photo_file = await context.bot.get_file(photo.file_id)
    photo_bytes = await photo_file.download_as_bytearray()
    
    # Store photo in user data
    await set_user_data(telegram_id, {
        "photo_bytes": bytes(photo_bytes),
        "photo_file_id": photo.file_id,
    })
    
    # Update state
    await set_user_state(telegram_id, "animate_photo_waiting_duration")
    
    await update.message.reply_text(
        "✅ Фото получено!\n\n"
        "Теперь выберите длительность видео:",
        reply_markup=duration_keyboard(),
    )
    
    return WAITING_FOR_DURATION


async def select_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle duration selection and start generation."""
    query = update.callback_query
    await query.answer()
    
    telegram_id = query.from_user.id
    user = await get_or_create_user(telegram_id)
    
    # Parse duration from callback_data
    duration_str = query.data.split("_")[-1]
    duration = int(duration_str)
    
    if duration not in ANIMATE_COSTS:
        await query.edit_message_text(
            "❌ Неверная длительность.",
            reply_markup=back_to_menu_keyboard(),
        )
        await clear_user_state(telegram_id)
        return ConversationHandler.END
    
    cost = ANIMATE_COSTS[duration]
    
    # Get stored photo
    user_data = await get_user_data(telegram_id)
    photo_bytes = user_data.get("photo_bytes")
    
    if not photo_bytes:
        await query.edit_message_text(
            "❌ Фото не найдено. Начните заново.",
            reply_markup=back_to_menu_keyboard(),
        )
        await clear_user_state(telegram_id)
        return ConversationHandler.END
    
    # Check credits
    if user.credits < cost:
        await query.edit_message_text(
            f"❌ Недостаточно кредитов!\n\n"
            f"Нужно: {cost} кредитов\n"
            f"Ваш баланс: {user.credits} кредитов",
            reply_markup=insufficient_funds_keyboard(),
        )
        await clear_user_state(telegram_id)
        return ConversationHandler.END
    
    # Deduct credits
    import uuid as _uuid
    _req_id = _uuid.uuid4().hex
    success = await deduct_for_generation(user.id, cost, "animate_photo", _req_id)
    if not success:
        await query.edit_message_text(
            "❌ Ошибка списания кредитов. Попробуйте позже.",
            reply_markup=back_to_menu_keyboard(),
        )
        await clear_user_state(telegram_id)
        return ConversationHandler.END
    
    # Create task
    import uuid
    task_id = uuid.uuid4().hex
    
    payload = {
        "task_id": task_id,
        "task_type": "animate_photo",
        "telegram_id": telegram_id,
        "chat_id": update.effective_chat.id,
        "user_id": user.id,
        "photo_bytes": photo_bytes,
        "duration_seconds": duration,
        "credits_cost": cost,
    }
    
    try:
        position = await enqueue_task(task_id, payload)
        
        await query.edit_message_text(
            f"✅ Задача принята!\n\n"
            f"🎬 Оживляем ваше фото...\n"
            f"⏱️ Длительность: {duration} секунд\n\n"
            f"⏳ В очереди: {position} задач\n"
            f"⏱️ Примерное время: {position * 60 + 60} сек\n\n"
            f"💎 Списано: {cost} кредитов\n"
            f"💰 Осталось: {user.credits - cost} кредитов\n\n"
            f"⚠️ Генерация видео может занять до 2 минут.",
            reply_markup=back_to_menu_keyboard(),
        )
        
    except QueueLimitError:
        # Refund credits
        from shared.admin_guard import refund_if_needed
        await refund_if_needed(user.id, cost, "animate_photo", task_id)
        
        await query.edit_message_text(
            "❌ Очередь переполнена. Попробуйте через несколько минут.\n"
            "Кредиты возвращены.",
            reply_markup=back_to_menu_keyboard(),
        )
    
    except Exception as exc:
        logger.error("Failed to enqueue animate_photo task: %s", exc)
        
        # Refund credits
        from shared.admin_guard import refund_if_needed
        await refund_if_needed(user.id, cost, "animate_photo", task_id)
        
        await query.edit_message_text(
            "❌ Ошибка при создании задачи. Кредиты возвращены.",
            reply_markup=back_to_menu_keyboard(),
        )
    
    await clear_user_state(telegram_id)
    return ConversationHandler.END


async def cancel_animate_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel photo animation."""
    query = update.callback_query
    await query.answer()
    
    telegram_id = query.from_user.id
    await clear_user_state(telegram_id)
    
    await query.edit_message_text(
        "❌ Оживление отменено.",
        reply_markup=back_to_menu_keyboard(),
    )
    
    return ConversationHandler.END
