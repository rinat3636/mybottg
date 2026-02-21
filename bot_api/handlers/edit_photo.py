"""Handler for photo editing with face preservation (IP-Adapter + SDXL)."""

from __future__ import annotations

import logging
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
from services.generation_service import deduct_for_generation
from bot_api.keyboards import (
    back_to_menu_keyboard,
    insufficient_funds_keyboard,
    cancel_keyboard,
)

logger = logging.getLogger(__name__)

# Conversation states
WAITING_FOR_PHOTO = 1
WAITING_FOR_PROMPT = 2

# Credit cost for photo editing
EDIT_PHOTO_COST = 25  # кредитов


async def start_edit_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start photo editing flow."""
    query = update.callback_query
    await query.answer()
    
    telegram_id = query.from_user.id
    user = await get_or_create_user(telegram_id)
    
    # Check credits
    if user.credits < EDIT_PHOTO_COST:
        await query.edit_message_text(
            f"❌ Недостаточно кредитов!\n\n"
            f"Для редактирования фото нужно {EDIT_PHOTO_COST} кредитов.\n"
            f"Ваш баланс: {user.credits} кредитов.",
            reply_markup=insufficient_funds_keyboard(),
        )
        return ConversationHandler.END
    
    # Set state
    await set_user_state(telegram_id, "edit_photo_waiting_photo")
    
    await query.edit_message_text(
        "📸 **Редактирование фото с сохранением лица**\n\n"
        "Загрузите фото, которое хотите улучшить или изменить.\n\n"
        "✨ **Что можно сделать:**\n"
        "• Изменить фон\n"
        "• Изменить стиль (арт, 3D, реализм)\n"
        "• Улучшить качество\n"
        "• Добавить эффекты\n\n"
        "⚠️ **Важно:** Лицо на фото будет сохранено!\n\n"
        f"💎 Стоимость: {EDIT_PHOTO_COST} кредитов",
        reply_markup=cancel_keyboard(),
        parse_mode="Markdown",
    )
    
    return WAITING_FOR_PHOTO


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    await set_user_state(telegram_id, "edit_photo_waiting_prompt")
    
    await update.message.reply_text(
        "✅ Фото получено!\n\n"
        "Теперь опишите, что хотите изменить:\n\n"
        "**Примеры:**\n"
        "• `на фоне гор и заката`\n"
        "• `в стиле аниме`\n"
        "• `в костюме супергероя`\n"
        "• `профессиональное фото, студийное освещение`\n\n"
        "💡 Лицо останется таким же, изменится только фон и стиль.",
        reply_markup=cancel_keyboard(),
        parse_mode="Markdown",
    )
    
    return WAITING_FOR_PROMPT


async def receive_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive prompt and start generation."""
    telegram_id = update.effective_user.id
    user = await get_or_create_user(telegram_id)
    
    prompt = update.message.text.strip()
    
    if not prompt:
        await update.message.reply_text(
            "❌ Пожалуйста, опишите что хотите изменить.",
            reply_markup=cancel_keyboard(),
        )
        return WAITING_FOR_PROMPT
    
    # Get stored photo
    user_data = await get_user_data(telegram_id)
    photo_bytes = user_data.get("photo_bytes")
    
    if not photo_bytes:
        await update.message.reply_text(
            "❌ Фото не найдено. Начните заново.",
            reply_markup=back_to_menu_keyboard(),
        )
        await clear_user_state(telegram_id)
        return ConversationHandler.END
    
    # Check credits again
    if user.credits < EDIT_PHOTO_COST:
        await update.message.reply_text(
            f"❌ Недостаточно кредитов!\n\n"
            f"Нужно: {EDIT_PHOTO_COST} кредитов\n"
            f"Ваш баланс: {user.credits} кредитов",
            reply_markup=insufficient_funds_keyboard(),
        )
        await clear_user_state(telegram_id)
        return ConversationHandler.END
    
    # Deduct credits
    import uuid as _uuid
    _req_id = _uuid.uuid4().hex
    success = await deduct_for_generation(user.id, EDIT_PHOTO_COST, "edit_photo", _req_id)
    if not success:
        await update.message.reply_text(
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
        "task_type": "edit_photo",
        "telegram_id": telegram_id,
        "chat_id": update.effective_chat.id,
        "user_id": user.id,
        "prompt": prompt,
        "photo_bytes": photo_bytes,
        "credits_cost": EDIT_PHOTO_COST,
    }
    
    try:
        position = await enqueue_task(task_id, payload)
        
        await update.message.reply_text(
            f"✅ Задача принята!\n\n"
            f"🎨 Редактируем ваше фото...\n"
            f"📝 Промт: {prompt}\n\n"
            f"⏳ В очереди: {position} задач\n"
            f"⏱️ Примерное время: {position * 30 + 30} сек\n\n"
            f"💎 Списано: {EDIT_PHOTO_COST} кредитов\n"
            f"💰 Осталось: {user.credits - EDIT_PHOTO_COST} кредитов",
            reply_markup=back_to_menu_keyboard(),
        )
        
    except QueueLimitError:
        # Refund credits
        from shared.admin_guard import refund_if_needed
        await refund_if_needed(user.id, EDIT_PHOTO_COST, "edit_photo", task_id)
        
        await update.message.reply_text(
            "❌ Очередь переполнена. Попробуйте через несколько минут.\n"
            "Кредиты возвращены.",
            reply_markup=back_to_menu_keyboard(),
        )
    
    except Exception as exc:
        logger.error("Failed to enqueue edit_photo task: %s", exc)
        
        # Refund credits
        from shared.admin_guard import refund_if_needed
        await refund_if_needed(user.id, EDIT_PHOTO_COST, "edit_photo", task_id)
        
        await update.message.reply_text(
            "❌ Ошибка при создании задачи. Кредиты возвращены.",
            reply_markup=back_to_menu_keyboard(),
        )
    
    await clear_user_state(telegram_id)
    return ConversationHandler.END


async def cancel_edit_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel photo editing."""
    query = update.callback_query
    await query.answer()
    
    telegram_id = query.from_user.id
    await clear_user_state(telegram_id)
    
    await query.edit_message_text(
        "❌ Редактирование отменено.",
        reply_markup=back_to_menu_keyboard(),
    )
    
    return ConversationHandler.END
