"""Image generation handler — ComfyUI based, no credits required."""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from io import BytesIO

from telegram import Update
from telegram.ext import ContextTypes

from bot_api.keyboards import (
    cancel_keyboard,
    back_to_menu_keyboard,
    main_menu_keyboard,
    generation_done_keyboard,
)
from services.user_service import get_user_by_telegram_id
from shared.config import DEFAULT_CMD_RATE_LIMIT, DEFAULT_MEDIA_RATE_LIMIT, settings
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
    add_media_group_item,
    get_media_group,
    delete_media_group,
    acquire_media_group_process_lock,
    get_last_job,
)
from shared.errors import log_exception, safe_user_message, generate_trace_id

logger = logging.getLogger(__name__)

_AR_RE = re.compile(r"\b(1:1|3:4|4:3|16:9|9:16|2:3|3:2|21:9)\b")

GEN_START_TEXT = (
    "🧙 *Создание изображения*\n\n"
    "Напишите текстовый промт — что хотите получить.\n\n"
    "*Примеры:*\n"
    "• «Неоновый город ночью, киберпанк, 9:16»\n"
    "• «Минималистичный логотип, белый фон, 1:1»\n"
    "• «Фотореалистичный портрет, мягкий свет, 3:4»\n\n"
    "Можно указать соотношение сторон: `1:1`, `3:4`, `4:3`, `16:9`, `9:16`"
)


def _parse_prompt_and_ar(text: str) -> tuple[str, str | None]:
    txt = (text or "").strip()
    if not txt:
        return "", None
    m = _AR_RE.search(txt)
    ar = m.group(1) if m else None
    if ar:
        txt = _AR_RE.sub("", txt, count=1).strip()
        txt = re.sub(r"\s{2,}", " ", txt)
    return txt, ar


async def generate_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: user pressed 'Создать изображение'."""
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
                "⏳ У вас уже есть активная генерация. Дождитесь завершения или отмените командой /cancel.",
                reply_markup=cancel_keyboard(),
            )
            return
        await set_user_state(telegram_id, "waiting_for_generation")
        await update_user_data(telegram_id, mode="generate")
        await query.edit_message_text(
            GEN_START_TEXT,
            parse_mode="Markdown",
            reply_markup=cancel_keyboard(),
        )
    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="generate_start_callback")


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos sent by user — route to correct flow based on state."""
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

        # Edit photo flow
        if state == "edit_photo_waiting_photo":
            from bot_api.handlers.edit_photo import receive_photo
            await receive_photo(update, context)
            return

        # Animate photo flow
        if state == "animate_photo_waiting_photo":
            from bot_api.handlers.animate_photo import receive_photo_for_animation
            await receive_photo_for_animation(update, context)
            return

        # Generate / edit flow — collect photos
        user = await get_user_by_telegram_id(telegram_id)
        if not user or user.is_banned:
            return
        active = await get_active_generation(telegram_id)
        if active:
            await update.message.reply_text(
                "⏳ У вас уже есть активная генерация. Дождитесь завершения.",
                reply_markup=cancel_keyboard(),
            )
            return
        await set_user_state(telegram_id, "waiting_for_generation")
        await update_user_data(telegram_id, mode="edit")

        # Handle album (media group)
        if update.message.media_group_id:
            media_group_id = update.message.media_group_id
            photo = update.message.photo[-1]
            await add_media_group_item(telegram_id, media_group_id, photo.file_id)
            lock = await acquire_media_group_process_lock(telegram_id, media_group_id)
            if lock:
                context.application.job_queue.run_once(
                    _process_media_group_job,
                    when=2.0,
                    data={"telegram_id": telegram_id, "media_group_id": media_group_id},
                )
            return

        # Single photo
        photo = update.message.photo[-1]
        await update_user_data(telegram_id, image_file_ids=[photo.file_id])
        caption = (update.message.caption or "").strip()
        if caption:
            prompt, ar = _parse_prompt_and_ar(caption)
            await _process_generation_by_file_ids(update, context, [photo.file_id], prompt, ar, trace_id)
            return
        await update.message.reply_text(
            "✅ Фото получено!\n\nТеперь напишите *что изменить*.\n\n"
            "*Примеры:*\n"
            "• «Убери фон, сделай студийный свет»\n"
            "• «Сделай в стиле аниме, сохрани лицо»",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard(),
        )
    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="photo_handler")
        await update.message.reply_text(safe_user_message(trace_id))


async def document_image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle images sent as documents."""
    if not update.message or not update.effective_user or not update.message.document:
        return
    doc = update.message.document
    mime = (doc.mime_type or "").lower()
    if not mime.startswith("image/"):
        return
    telegram_id = update.effective_user.id
    await update_user_data(telegram_id, image_file_ids=[doc.file_id])
    caption = (update.message.caption or "").strip()
    if caption:
        trace_id = generate_trace_id()
        prompt, ar = _parse_prompt_and_ar(caption)
        await _process_generation_by_file_ids(update, context, [doc.file_id], prompt, ar, trace_id)
        return
    await update.message.reply_text(
        "📎 Изображение получил. Теперь напишите *что изменить*.",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard(),
    )


async def prompt_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages — route to correct flow based on state."""
    if not update.message or not update.effective_user:
        return
    trace_id = generate_trace_id()
    try:
        telegram_id = update.effective_user.id
        allowed = await check_rate_limit(telegram_id, "cmd", DEFAULT_CMD_RATE_LIMIT, 60)
        if not allowed:
            await update.message.reply_text("⏳ Подождите немного, вы отправляете слишком много запросов.")
            return
        state = await get_user_state(telegram_id)

        # Support flows
        if state and state.startswith("support_reply_"):
            from bot_api.handlers.support import support_reply_text_handler
            await support_reply_text_handler(update, context)
            return
        if state == "waiting_for_support_message":
            from bot_api.handlers.support import support_message_handler
            await support_message_handler(update, context)
            return

        # Edit photo flow — waiting for prompt
        if state == "edit_photo_waiting_prompt":
            from bot_api.handlers.edit_photo import receive_prompt
            await receive_prompt(update, context)
            return

        # Animate photo flow — waiting for prompt
        if state == "animate_photo_waiting_prompt":
            from bot_api.handlers.animate_photo import receive_prompt_for_animation
            await receive_prompt_for_animation(update, context)
            return

        text = update.message.text.strip() if update.message.text else ""
        if not text:
            return

        prompt, ar = _parse_prompt_and_ar(text)

        # If user is in generation flow
        if state == "waiting_for_generation":
            data = await get_user_data(telegram_id)
            image_file_ids = data.get("image_file_ids") or []
            if image_file_ids:
                await _process_generation_by_file_ids(update, context, image_file_ids, prompt, ar, trace_id)
                return
            # Text-only generation
            await _enqueue_generation(
                telegram_id=telegram_id,
                chat_id=update.message.chat_id,
                user_message_reply=update.message,
                context=context,
                prompt=prompt,
                aspect_ratio=ar,
                image_bytes_list=[],
                image_file_ids=[],
            )
            return

        # Not in any flow — start generate flow implicitly
        user = await get_user_by_telegram_id(telegram_id)
        if not user:
            await update.message.reply_text("Нажмите /start для начала работы.")
            return
        if user.is_banned:
            await update.message.reply_text("🚫 Ваш аккаунт заблокирован.")
            return
        active = await get_active_generation(telegram_id)
        if active:
            await update.message.reply_text(
                "⏳ У вас уже есть активная генерация. Дождитесь завершения.",
                reply_markup=cancel_keyboard(),
            )
            return
        await set_user_state(telegram_id, "waiting_for_generation")
        await update_user_data(telegram_id, mode="generate")
        await _enqueue_generation(
            telegram_id=telegram_id,
            chat_id=update.message.chat_id,
            user_message_reply=update.message,
            context=context,
            prompt=prompt,
            aspect_ratio=ar,
            image_bytes_list=[],
            image_file_ids=[],
        )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="prompt_text_handler")
        await update.message.reply_text(safe_user_message(trace_id))


async def gen_again_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: repeat last generation."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    telegram_id = query.from_user.id
    trace_id = generate_trace_id()
    try:
        last = await get_last_job(telegram_id)
        if not last:
            await query.edit_message_text(
                "❌ Нет предыдущей задачи для повтора.",
                reply_markup=back_to_menu_keyboard(),
            )
            return
        prompt = last.get("prompt", "")
        ar = last.get("aspect_ratio")
        file_ids = last.get("image_file_ids") or []
        if file_ids:
            await _process_generation_by_file_ids_for_query(query, context, file_ids, prompt, ar)
        else:
            await _enqueue_generation(
                telegram_id=telegram_id,
                chat_id=query.message.chat_id if query.message else telegram_id,
                user_message_edit=query,
                context=context,
                prompt=prompt,
                aspect_ratio=ar,
                image_bytes_list=[],
                image_file_ids=[],
            )
    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="gen_again_callback")


async def gen_new_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: start new generation."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    telegram_id = query.from_user.id
    await set_user_state(telegram_id, "waiting_for_generation")
    await update_user_data(telegram_id, mode="generate", prompt="", aspect_ratio=None, image_file_ids=[])
    await query.edit_message_text(
        GEN_START_TEXT,
        parse_mode="Markdown",
        reply_markup=cancel_keyboard(),
    )


async def _process_generation_by_file_ids(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_ids: list[str],
    prompt: str,
    aspect_ratio: str | None,
    trace_id: str,
) -> None:
    telegram_id = update.effective_user.id  # type: ignore[union-attr]
    chat_id = update.effective_chat.id if update.effective_chat else telegram_id  # type: ignore[union-attr]
    images: list[bytes] = []
    for fid in file_ids[:8]:
        f = await context.bot.get_file(fid)
        b = await f.download_as_bytearray()
        images.append(bytes(b))
    await _enqueue_generation(
        telegram_id=telegram_id,
        chat_id=chat_id,
        user_message_reply=update.message,
        context=context,
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        image_bytes_list=images,
        image_file_ids=file_ids[:8],
    )


async def _process_generation_by_file_ids_for_query(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    file_ids: list[str],
    prompt: str,
    aspect_ratio: str | None,
) -> None:
    telegram_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else telegram_id
    images: list[bytes] = []
    for fid in file_ids[:8]:
        f = await context.bot.get_file(fid)
        b = await f.download_as_bytearray()
        images.append(bytes(b))
    await _enqueue_generation(
        telegram_id=telegram_id,
        chat_id=chat_id,
        user_message_edit=query,
        context=context,
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        image_bytes_list=images,
        image_file_ids=file_ids[:8],
    )


async def _enqueue_generation(
    *,
    telegram_id: int,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    aspect_ratio: str | None,
    image_bytes_list: list[bytes],
    image_file_ids: list[str],
    user_message_reply=None,
    user_message_edit=None,
) -> None:
    """Enqueue a generation task to the ComfyUI queue."""
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        msg = "❌ Ошибка. Нажмите /start"
        if user_message_reply:
            await user_message_reply.reply_text(msg)
        elif user_message_edit:
            is_admin = telegram_id in settings.ADMIN_IDS
            await user_message_edit.edit_message_text(msg, reply_markup=main_menu_keyboard(is_admin=is_admin))
        return

    request_id = uuid.uuid4().hex
    locked = await acquire_generation_lock(telegram_id, request_id)
    if not locked:
        msg = "⏳ У вас уже есть активная генерация. Дождитесь завершения."
        kb = cancel_keyboard()
        if user_message_reply:
            await user_message_reply.reply_text(msg, reply_markup=kb)
        elif user_message_edit:
            await user_message_edit.edit_message_text(msg, reply_markup=kb)
        return

    task_type = "edit_photo" if image_bytes_list else "generate_image"
    mode_text = "редактирование фото" if image_bytes_list else "генерацию изображения"

    try:
        position_ahead = await enqueue_task(
            request_id,
            {
                "telegram_id": telegram_id,
                "user_id": user.id,
                "chat_id": chat_id,
                "images_hex": [b.hex() for b in image_bytes_list],
                "image_file_ids": image_file_ids,
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "task_type": task_type,
                "request_id": request_id,
            },
        )
        ar_text = f" ({aspect_ratio})" if aspect_ratio else ""
        queue_text = f"\n\n📊 В очереди: {position_ahead} задач впереди" if position_ahead > 0 else ""
        msg = (
            f"⏳ *Задача принята!*\n\n"
            f"Тип: {mode_text}\n"
            f"Промт: _{prompt}{ar_text}_"
            f"{queue_text}\n\n"
            f"Генерирую через ComfyUI... Это займёт 30–90 секунд."
        )
        kb = back_to_menu_keyboard()
        if user_message_reply:
            await user_message_reply.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
        elif user_message_edit:
            await user_message_edit.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)
        await clear_user_state(telegram_id)
    except QueueLimitError:
        msg = "⏳ Очередь переполнена. Попробуйте через несколько секунд."
        kb = back_to_menu_keyboard()
        if user_message_reply:
            await user_message_reply.reply_text(msg, reply_markup=kb)
        elif user_message_edit:
            await user_message_edit.edit_message_text(msg, reply_markup=kb)
        await clear_user_state(telegram_id)
    except Exception as exc:
        trace_id = generate_trace_id()
        log_exception(exc, trace_id=trace_id, context="_enqueue_generation")
        msg = safe_user_message(trace_id)
        if user_message_reply:
            await user_message_reply.reply_text(msg)
        elif user_message_edit:
            await user_message_edit.edit_message_text(msg)
        await clear_user_state(telegram_id)


async def _process_media_group_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process media group after a short delay to collect all photos."""
    job = context.job
    if not job or not job.data:
        return
    telegram_id = job.data["telegram_id"]
    media_group_id = job.data["media_group_id"]
    items = await get_media_group(telegram_id, media_group_id)
    await delete_media_group(telegram_id, media_group_id)
    if not items:
        return
    file_ids = list(items.keys())[:8] if isinstance(items, dict) else items[:8]
    data = await get_user_data(telegram_id)
    prompt = (data.get("prompt") or "").strip()
    ar = data.get("aspect_ratio")
    if not prompt:
        await update_user_data(telegram_id, image_file_ids=file_ids)
        try:
            await context.bot.send_message(
                chat_id=telegram_id,
                text=(
                    f"✅ Получил {len(file_ids)} фото!\n\n"
                    "Теперь напишите *что изменить*."
                ),
                parse_mode="Markdown",
                reply_markup=cancel_keyboard(),
            )
        except Exception:
            pass
        return
    images: list[bytes] = []
    for fid in file_ids:
        try:
            f = await context.bot.get_file(fid)
            b = await f.download_as_bytearray()
            images.append(bytes(b))
        except Exception:
            pass
    await _enqueue_generation(
        telegram_id=telegram_id,
        chat_id=telegram_id,
        context=context,
        prompt=prompt,
        aspect_ratio=ar,
        image_bytes_list=images,
        image_file_ids=file_ids,
    )
