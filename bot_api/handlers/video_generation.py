"""Video generation handler (Kling v2.5 Turbo Pro), queue-based.

Supports:
- Image-to-video generation (5 or 10 seconds)
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot_api.keyboards import (
    cancel_keyboard,
    insufficient_funds_keyboard,
    main_menu_keyboard,
    video_duration_keyboard,
)
from services.generation_service import new_request_id, create_generation
from services.user_service import get_user_by_telegram_id
from shared.admin_guard import check_and_charge, refund_if_needed
from shared.config import GENERATION_COST, DEFAULT_CMD_RATE_LIMIT, DEFAULT_MEDIA_RATE_LIMIT
from shared.redis_client import (
    QueueLimitError,
    check_rate_limit,
    get_user_state,
    set_user_state,
    get_user_data,
    update_user_data,
    clear_user_state,
    acquire_generation_lock,
    enqueue_task,
    get_active_generation,
)
from shared.errors import log_exception, safe_user_message, generate_trace_id

logger = logging.getLogger(__name__)

VIDEO_START_TEXT = (
    "🎬 *Видео из изображения*\n\n"
    "Превратите статичное фото в короткое видео!\n\n"
    "*Что умеет:*\n"
    "• Создает плавное реалистичное движение\n"
    "• Понимает сложные инструкции\n"
    "• Сохраняет стиль и цвета исходного изображения\n"
    "• Поддерживает движения камеры\n\n"
    "*Примеры промтов:*\n"
    "• «Женщина поворачивает голову и улыбается»\n"
    "• «Машина едет по дороге, камера следует сбоку»\n"
    "• «Листья на дереве колышутся от ветра»\n"
    "• «Человек идет по улице, камера движется за ним»\n\n"
    "📸 *Отправьте фото*, которое хотите оживить."
)


async def video_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: user pressed 'Видео из изображения' — start flow."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    trace_id = generate_trace_id()

    try:
        telegram_id = query.from_user.id
        user = await get_user_by_telegram_id(telegram_id)

        if not user:
            await query.edit_message_text("❌ Пользователь не найден. Нажмите /start")
            return

        if user.is_banned:
            await query.edit_message_text("🚫 Ваш аккаунт заблокирован.")
            return

        active = await get_active_generation(telegram_id)
        if active:
            await query.edit_message_text(
                "⏳ У вас уже есть активная генерация. Дождитесь завершения или отмените её командой /cancel.",
                reply_markup=cancel_keyboard(),
            )
            return

        # Start video flow
        await set_user_state(telegram_id, "waiting_for_video_image")
        await update_user_data(telegram_id, mode="video")

        await query.edit_message_text(
            VIDEO_START_TEXT,
            parse_mode="Markdown",
            reply_markup=cancel_keyboard(),
        )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="video_start_callback")


async def video_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo for video generation."""
    if not update.message or not update.effective_user:
        return

    trace_id = generate_trace_id()

    try:
        telegram_id = update.effective_user.id

        allowed = await check_rate_limit(telegram_id, "media", DEFAULT_MEDIA_RATE_LIMIT, 60)
        if not allowed:
            await update.message.reply_text("⏳ Подождите немного, вы отправляете слишком много запросов.")
            return

        state = await get_user_state(telegram_id)

        # Only process if user is in video flow
        if state != "waiting_for_video_image":
            return

        photo = update.message.photo[-1]
        
        # Store photo file_id
        await update_user_data(telegram_id, video_image_file_id=photo.file_id)
        await set_user_state(telegram_id, "waiting_for_video_prompt")

        await update.message.reply_text(
            "📸 Фото получил! Теперь опишите *что должно происходить в видео*.\n\n"
            "Примеры:\n"
            "• «Человек поворачивает голову и улыбается в камеру»\n"
            "• «Камера медленно приближается к объекту»\n"
            "• «Волосы развеваются от ветра, человек моргает»",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard(),
        )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="video_photo_handler")
        await update.message.reply_text(safe_user_message(trace_id))


async def video_prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle prompt for video generation."""
    if not update.message or not update.effective_user or not update.message.text:
        return

    trace_id = generate_trace_id()

    try:
        telegram_id = update.effective_user.id

        allowed = await check_rate_limit(telegram_id, "cmd", DEFAULT_CMD_RATE_LIMIT, 60)
        if not allowed:
            await update.message.reply_text("⏳ Подождите немного, вы отправляете слишком много запросов.")
            return

        state = await get_user_state(telegram_id)

        # Only process if user is in video prompt flow
        if state != "waiting_for_video_prompt":
            return

        prompt = update.message.text.strip()
        if not prompt:
            await update.message.reply_text("❌ Промт не может быть пустым. Опишите, что должно происходить в видео.")
            return

        # Store prompt
        await update_user_data(telegram_id, video_prompt=prompt)
        await set_user_state(telegram_id, "waiting_for_video_duration")

        # Show duration selection
        await update.message.reply_text(
            "⏱️ *Выберите длительность видео:*\n\n"
            "⚡ *5 секунд* — 70 кредитов\n"
            "Быстрая генерация, короткое видео.\n\n"
            "⭐ *10 секунд* — 140 кредитов\n"
            "Более длинное видео с большим количеством движения.",
            parse_mode="Markdown",
            reply_markup=video_duration_keyboard(),
        )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="video_prompt_handler")
        await update.message.reply_text(safe_user_message(trace_id))


async def video_duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: user selected video duration."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    trace_id = generate_trace_id()

    try:
        telegram_id = query.from_user.id
        user = await get_user_by_telegram_id(telegram_id)

        if not user:
            await query.edit_message_text("❌ Пользователь не найден. Нажмите /start")
            return

        if user.is_banned:
            await query.edit_message_text("🚫 Ваш аккаунт заблокирован.")
            return

        active = await get_active_generation(telegram_id)
        if active:
            await query.edit_message_text(
                "⏳ У вас уже есть активная генерация. Дождитесь завершения.",
                reply_markup=cancel_keyboard(),
            )
            return

        # Determine duration from callback
        callback_data = query.data or ""
        if callback_data == "video_duration_5":
            duration = 5
            tariff = "kling_video_5s"
        elif callback_data == "video_duration_10":
            duration = 10
            tariff = "kling_video_10s"
        else:
            await query.edit_message_text("❌ Неверный выбор длительности.")
            return

        cost = GENERATION_COST[tariff]

        # Get stored data
        data = await get_user_data(telegram_id)
        video_image_file_id = data.get("video_image_file_id")
        video_prompt = data.get("video_prompt")

        if not video_image_file_id or not video_prompt:
            await query.edit_message_text(
                "❌ Данные не найдены. Начните заново с кнопки 'Видео из изображения'.",
                reply_markup=main_menu_keyboard(),
            )
            return

        # Check and charge credits
        is_admin = telegram_id in context.application.bot_data.get("admin_ids", [])
        
        if not is_admin and user.credits < cost:
            await query.edit_message_text(
                f"❌ Недостаточно кредитов.\n\n"
                f"Требуется: *{cost}* кредитов\n"
                f"У вас: *{user.credits}* кредитов",
                parse_mode="Markdown",
                reply_markup=insufficient_funds_keyboard(),
            )
            return

        # Process video generation
        await _process_video_generation(
            update, context, video_image_file_id, video_prompt, duration, tariff, cost, trace_id
        )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="video_duration_callback")


async def _process_video_generation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    image_file_id: str,
    prompt: str,
    duration: int,
    tariff: str,
    cost: int,
    trace_id: str,
) -> None:
    """Process video generation request."""
    query = update.callback_query
    if not query:
        return

    telegram_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else telegram_id

    try:
        user = await get_user_by_telegram_id(telegram_id)
        if not user:
            await query.edit_message_text("❌ Пользователь не найден.")
            return

        is_admin = telegram_id in context.application.bot_data.get("admin_ids", [])

        # Acquire lock
        lock_acquired = await acquire_generation_lock(telegram_id)
        if not lock_acquired:
            await query.edit_message_text(
                "⏳ У вас уже есть активная генерация. Дождитесь завершения.",
                reply_markup=cancel_keyboard(),
            )
            return

        # Create generation record
        request_id = new_request_id()
        generation = await create_generation(
            user_id=user.id,
            prompt=prompt,
            tariff=tariff,
            cost=cost,
            request_id=request_id,
        )

        # Check and charge
        charged = await check_and_charge(user.id, is_admin, cost, request_id, tariff)
        if not charged:
            await query.edit_message_text(
                f"❌ Недостаточно кредитов.\n\n"
                f"Требуется: *{cost}* кредитов",
                parse_mode="Markdown",
                reply_markup=insufficient_funds_keyboard(),
            )
            return

        # Download image
        file = await context.bot.get_file(image_file_id)
        image_bytes = await file.download_as_bytearray()

        # Enqueue video generation task
        payload = {
            "telegram_id": telegram_id,
            "user_id": user.id,
            "chat_id": chat_id,
            "image_hex": image_bytes.hex(),
            "prompt": prompt,
            "duration": duration,
            "generation_id": generation.id,
            "cost": cost,
            "tariff": tariff,
            "request_id": request_id,
            "is_admin": is_admin,
            "task_type": "video",
        }

        try:
            task_id = await enqueue_task(telegram_id, payload)
            logger.info("Video generation task enqueued: task_id=%s request_id=%s", task_id, request_id)

            await query.edit_message_text(
                f"✅ Видео добавлено в очередь!\n\n"
                f"Длительность: *{duration} секунд*\n"
                f"Стоимость: *{cost}* кредитов\n\n"
                f"⏳ Генерация видео может занять несколько минут...",
                parse_mode="Markdown",
                reply_markup=cancel_keyboard(),
            )

            # Clear state
            await clear_user_state(telegram_id)

        except QueueLimitError:
            await refund_if_needed(user.id, is_admin, cost, request_id, tariff)
            await query.edit_message_text(
                "❌ Очередь переполнена. Попробуйте позже.",
                reply_markup=main_menu_keyboard(),
            )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="_process_video_generation")
        await query.edit_message_text(safe_user_message(trace_id))
