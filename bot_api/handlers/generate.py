"""Image generation & editing handler (Nano Banana Pro), queue-based.

Supports:
1) Text-only generation
2) One image + caption (prompt)
3) Album up to 8 images + caption
"""

from __future__ import annotations

import asyncio
import logging
import re

from telegram import Update
from telegram.ext import ContextTypes

from bot_api.keyboards import (
    cancel_keyboard,
    insufficient_funds_keyboard,
    main_menu_keyboard,
)
from services.generation_service import new_request_id, create_generation
from services.user_service import get_user_by_telegram_id
from shared.admin_guard import check_and_charge, refund_if_needed
from shared.config import GENERATION_COST, DEFAULT_CMD_RATE_LIMIT, DEFAULT_MEDIA_RATE_LIMIT, settings
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

TARIFF_KEY = "nano_banana_pro"


INSTRUCTION_TEXT = (
    "*Nano Banana Pro включён* 🍌\n\n"
    "Два сценария — выбирайте как удобнее:\n"
    "• *Редактирование:* фото → затем текст\n"
    "• *Генерация с нуля:* текст (без фото)\n\n"
    "Поддерживаю альбом до *8* фото.\n\n"
    "Можно указать соотношение сторон прямо в тексте: `1:1`, `3:4`, `4:3`, `16:9`, `9:16`, `2:3`, `3:2`, `21:9`."
)

EDIT_START_TEXT = (
    "🖼️ *Редактирование фото*\n\n"
    "Отправьте *фото* (или альбом до 8), а затем сообщением напишите, *что изменить*.\n\n"
    "Примеры:\n"
    "• «Убери фон, сделай студийный свет, сохрани лицо»\n"
    "• «Сделай в стиле аниме, сохрани лицо, 1:1»\n"
    "• «Сделай как постер фильма, 3:4»"
)

GEN_START_TEXT = (
    "🪄 *Генерация с нуля*\n\n"
    "Напишите текстом, что хотите получить. Можно без фото.\n\n"
    "Примеры:\n"
    "• «Неоновый город ночью, киберпанк, 9:16»\n"
    "• «Минималистичный логотип, белый фон, 1:1»\n"
    "• «Фотореалистичный портрет, мягкий свет, 3:4»"
)


_AR_RE = re.compile(r"\b(1:1|3:4|4:3|16:9|9:16|2:3|3:2|21:9)\b")


def _parse_prompt_and_ar(text: str) -> tuple[str, str | None]:
    txt = (text or "").strip()
    if not txt:
        return "", None
    m = _AR_RE.search(txt)
    ar = m.group(1) if m else None
    # Keep prompt readable: remove aspect ratio token from the prompt
    if ar:
        txt = _AR_RE.sub("", txt, count=1).strip()
        txt = re.sub(r"\s{2,}", " ", txt)
    return txt, ar


async def generate_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: user pressed "Генерация" — start flow."""
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

        cost = GENERATION_COST[TARIFF_KEY]

        # Admins don't need balance check - they get free generations
        # Balance will be checked later in check_and_charge() for non-admins

        entry = query.data or "menu_generate"
        mode = "edit" if entry == "menu_edit" else "generate"

        # For edit mode, show quality selection
        if mode == "edit":
            from bot_api.keyboards import edit_quality_keyboard
            await query.edit_message_text(
                "📸 *Выберите качество редактирования:*\n\n"
                "⚡ *Nano Banana PRO* — 30 кредитов\n"
                "Быстрое редактирование с поддержкой до 14 изображений.\n"
                "Отлично рисует текст, работает с Google Search.\n\n"
                "🎨 *Flux 2 Pro* — 24 кредита\n"
                "Профессиональное редактирование с фотореализмом.\n"
                "Идеально для продуктовой фотографии и точного текста.\n\n"
                "💎 *Riverflow 2.0 PRO* — 32 кредита\n"
                "Максимальное качество и лучшее сохранение лица.",
                parse_mode="Markdown",
                reply_markup=edit_quality_keyboard(),
            )
            return

        # For generate mode, start flow directly with nano_banana_pro
        await set_user_state(telegram_id, "waiting_for_generation")
        await update_user_data(telegram_id, tariff=TARIFF_KEY, cost=cost, mode=mode)

        header = GEN_START_TEXT
        
        # Show cost only for non-admins
        from shared.config import settings
        is_admin = telegram_id in settings.ADMIN_IDS
        cost_text = "" if is_admin else f"\n\nСтоимость: *{cost}* кредитов"
        
        await query.edit_message_text(
            f"{header}\n\n{INSTRUCTION_TEXT}{cost_text}",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard(),
        )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="generate_start_callback")


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

        # Video generation flow
        if state == "waiting_for_video_image":
            from bot_api.handlers.video_generation import video_photo_handler
            await video_photo_handler(update, context)
            return
        
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

        # If user is not in generation flow, start it implicitly
        if state not in ("waiting_for_generation",):
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

            cost = GENERATION_COST[TARIFF_KEY]
            # Admins get free generations - balance checked in check_and_charge()

            await set_user_state(telegram_id, "waiting_for_generation")
            await update_user_data(telegram_id, tariff=TARIFF_KEY, cost=cost)

        media_group_id = update.message.media_group_id

        # Album support: buffer and process once (best-effort)
        if media_group_id:
            photo = update.message.photo[-1]
            await add_media_group_item(
                telegram_id,
                media_group_id,
                photo.file_id,
                caption=update.message.caption,
            )
            # Process the album after a short delay (messages arrive in bursts)
            asyncio.create_task(_process_media_group_after_delay(context, telegram_id, media_group_id))
            return

        photo = update.message.photo[-1]
        caption = update.message.caption

        # If caption present: process immediately as "image + caption"
        if caption and caption.strip():
            prompt, ar = _parse_prompt_and_ar(caption.strip())
            await update_user_data(telegram_id, prompt=prompt, aspect_ratio=ar, image_file_ids=[photo.file_id])
            await _process_generation_by_file_ids(update, context, [photo.file_id], prompt, ar, trace_id)
            return

        data = await get_user_data(telegram_id)
        prompt = (data.get("prompt") or "").strip()
        ar = data.get("aspect_ratio")

        if prompt:
            await _process_generation_by_file_ids(update, context, [photo.file_id], prompt, ar, trace_id)
            return

        await update_user_data(telegram_id, image_file_ids=[photo.file_id])
        await update.message.reply_text(
            "📸 Фото получил. Теперь напишите *что изменить*.\n\n"
            "Примеры: «убери фон», «сохрани лицо, сделай аниме», «как кинопостер 3:4».",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard(),
        )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="photo_handler")
        await update.message.reply_text(safe_user_message(trace_id))


async def document_image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle images sent as documents (files)."""
    if not update.message or not update.effective_user or not update.message.document:
        return
    doc = update.message.document
    mime = (doc.mime_type or "").lower()
    if not mime.startswith("image/"):
        return

    # Reuse the same logic as photo handler by treating the document as one image.
    telegram_id = update.effective_user.id
    await update_user_data(telegram_id, image_file_ids=[doc.file_id])

    caption = (update.message.caption or "").strip()
    if caption:
        trace_id = generate_trace_id()
        prompt, ar = _parse_prompt_and_ar(caption)
        await _process_generation_by_file_ids(update, context, [doc.file_id], prompt, ar, trace_id)
        return

    await update.message.reply_text(
        "📎 Изображение получил. Теперь напишите *что изменить*.\n\n"
        "Пример: «замени фон на студию, сохрани лицо, 1:1».",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard(),
    )


async def prompt_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

        # Video generation flow
        if state == "waiting_for_video_prompt":
            from bot_api.handlers.video_generation import video_prompt_handler
            await video_prompt_handler(update, context)
            return
        
        # Edit photo flow - waiting for prompt
        if state == "edit_photo_waiting_prompt":
            from bot_api.handlers.edit_photo import receive_prompt
            await receive_prompt(update, context)
            return

        text = update.message.text.strip() if update.message.text else ""
        if not text:
            return

        prompt, ar = _parse_prompt_and_ar(text)

        # If user is not in generation flow, start it implicitly
        if state not in ("waiting_for_generation",):
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

            cost = GENERATION_COST[TARIFF_KEY]
            # Admins get free generations - balance checked in check_and_charge()

            await set_user_state(telegram_id, "waiting_for_generation")
            await update_user_data(telegram_id, tariff=TARIFF_KEY, cost=cost)

        data = await get_user_data(telegram_id)
        image_file_ids = data.get("image_file_ids") or []

        # If we already have images — run edit now
        if image_file_ids:
            await _process_generation_by_file_ids(update, context, image_file_ids, prompt, ar, trace_id)
            return

        # Text-only: store prompt and offer quick "generate now" button
        await update_user_data(telegram_id, prompt=prompt, aspect_ratio=ar)
        await update.message.reply_text(
            "✍️ Ок, промт сохранил.\n\n"
            "Дальше:\n"
            "• *Сгенерировать без фото* → кнопка ниже\n"
            "• *Редактировать фото* → просто отправьте фото (или альбом до 8).",
            parse_mode="Markdown",
            reply_markup=_text_only_choice_keyboard(),
        )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="prompt_text_handler")
        await update.message.reply_text(safe_user_message(trace_id))


def _text_only_choice_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎨 Сгенерировать без фото", callback_data="gen_text_only")],
            [InlineKeyboardButton("◀️ В меню", callback_data="back_to_menu")],
        ]
    )


async def text_only_generate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User pressed: generate without photo (uses stored prompt)."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    telegram_id = query.from_user.id
    data = await get_user_data(telegram_id)
    prompt = (data.get("prompt") or "").strip()
    ar = data.get("aspect_ratio")
    if not prompt:
        await query.edit_message_text(
            "❌ Сначала отправьте текст задания.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # Fake an Update-like object? We'll just enqueue directly.
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


async def gen_again_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    telegram_id = query.from_user.id
    last = await get_last_job(telegram_id)
    prompt = (last.get("prompt") or "").strip()
    ar = last.get("aspect_ratio")
    file_ids = last.get("image_file_ids") or []
    if not prompt:
        await query.edit_message_text("Нет данных для повтора. Нажмите «Новая генерация».", reply_markup=main_menu_keyboard())
        return
    if file_ids:
        # download and enqueue
        await _process_generation_by_file_ids_for_query(query, context, file_ids, prompt, ar)
        return
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


async def gen_new_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    telegram_id = query.from_user.id
    await set_user_state(telegram_id, "waiting_for_generation")
    await update_user_data(telegram_id, tariff=TARIFF_KEY, cost=GENERATION_COST[TARIFF_KEY], prompt="", aspect_ratio=None, image_file_ids=[])
    await query.edit_message_text(
        f"{INSTRUCTION_TEXT}\n\nСтоимость: *{GENERATION_COST[TARIFF_KEY]}* кредитов",
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

    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        if user_message_reply:
            await user_message_reply.reply_text("❌ Ошибка. Нажмите /start")
        elif user_message_edit:
            await user_message_edit.edit_message_text("❌ Ошибка. Нажмите /start", reply_markup=main_menu_keyboard())
        return

    data = await get_user_data(telegram_id)
    tariff = data.get("tariff", TARIFF_KEY)
    cost = data.get("cost", GENERATION_COST.get(tariff, GENERATION_COST[TARIFF_KEY]))

    request_id = new_request_id()

    success = await check_and_charge(user.id, user.is_admin, cost, tariff, request_id)
    if not success:
        msg = f"💰 Недостаточно кредитов (нужно {cost})."
        kb = insufficient_funds_keyboard()
        if user_message_reply:
            await user_message_reply.reply_text(msg, reply_markup=kb)
        elif user_message_edit:
            await user_message_edit.edit_message_text(msg, reply_markup=kb)
        await clear_user_state(telegram_id)
        return

    locked = await acquire_generation_lock(telegram_id, request_id)
    if not locked:
        msg = "⏳ У вас уже есть активная генерация. Дождитесь завершения."
        kb = cancel_keyboard()
        if user_message_reply:
            await user_message_reply.reply_text(msg, reply_markup=kb)
        elif user_message_edit:
            await user_message_edit.edit_message_text(msg, reply_markup=kb)
        await refund_if_needed(user.id, user.is_admin, cost, request_id, tariff)
        return

    gen = await create_generation(user.id, prompt, tariff, cost, request_id)

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
            "generation_id": gen.id,
            "cost": cost,
            "tariff": tariff,
            "request_id": request_id,
            "is_admin": user.is_admin,
            },
        )
    except QueueLimitError as qerr:
        # Queue is full or user exceeded limits — refund and unlock
        await refund_if_needed(user.id, user.is_admin, cost, request_id, tariff)
        from shared.redis_client import release_generation_lock
        await release_generation_lock(telegram_id)
        await clear_user_state(telegram_id)

        msg = (
            "⏳ Сейчас слишком много запросов. Попробуйте чуть позже." if str(qerr) == "global_queue_full" else
            "⏳ У вас уже много задач в очереди. Дождитесь завершения предыдущих." 
        )
        kb = main_menu_keyboard()
        if user_message_reply:
            await user_message_reply.reply_text(msg, reply_markup=kb)
        elif user_message_edit:
            await user_message_edit.edit_message_text(msg, reply_markup=kb)
        return
    except Exception:
        # Unknown enqueue failure — refund and unlock
        await refund_if_needed(user.id, user.is_admin, cost, request_id, tariff)
        from shared.redis_client import release_generation_lock
        await release_generation_lock(telegram_id)
        await clear_user_state(telegram_id)
        raise

    await clear_user_state(telegram_id)

    # If there is no backlog, avoid talking about a queue — start immediately.
    if int(position_ahead) <= 0:
        msg = "✅ Принято. Начинаю обработку — скоро пришлю результат."
    else:
        msg = f"✅ Принято. Перед вами в очереди: {position_ahead}. Ожидайте результат."
    if user_message_reply:
        await user_message_reply.reply_text(msg, reply_markup=cancel_keyboard())
    elif user_message_edit:
        await user_message_edit.edit_message_text(msg, reply_markup=cancel_keyboard())
    else:
        try:
            from bot_api.bot import bot_app
            if bot_app:
                await bot_app.bot.send_message(chat_id=chat_id, text=msg, reply_markup=cancel_keyboard())
        except Exception:
            pass


async def _process_media_group_after_delay(context: ContextTypes.DEFAULT_TYPE, telegram_id: int, media_group_id: str) -> None:
    # Wait a bit to collect all album items
    await asyncio.sleep(1.2)

    locked = await acquire_media_group_process_lock(telegram_id, media_group_id)
    if not locked:
        return

    data = await get_media_group(telegram_id, media_group_id)
    file_ids = data.get("file_ids") or []
    caption = (data.get("caption") or "").strip()
    await delete_media_group(telegram_id, media_group_id)

    if not file_ids:
        return

    # If no caption, ask for it
    if not caption:
        from bot_api.bot import bot_app
        if bot_app:
            await bot_app.bot.send_message(
                chat_id=telegram_id,
                text=(
                    "📸 Альбом получил. Теперь напишите *одно сообщение* — что изменить.\n\n"
                    "Пример: «сделай фон белым, усили свет, сохрани лицо, 1:1»."
                ),
                parse_mode="Markdown",
                reply_markup=cancel_keyboard(),
            )
        await update_user_data(telegram_id, image_file_ids=file_ids[:8])
        return

    prompt, ar = _parse_prompt_and_ar(caption)
    await update_user_data(telegram_id, image_file_ids=file_ids[:8], prompt=prompt, aspect_ratio=ar)

    # Download and enqueue
    images: list[bytes] = []
    for fid in file_ids[:8]:
        f = await context.bot.get_file(fid)
        b = await f.download_as_bytearray()
        images.append(bytes(b))

    await _enqueue_generation(
        telegram_id=telegram_id,
        chat_id=telegram_id,
        context=context,
        prompt=prompt,
        aspect_ratio=ar,
        image_bytes_list=images,
        image_file_ids=file_ids[:8],
        user_message_reply=None,
        user_message_edit=None,
    )


async def edit_model_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: user selected a model for editing."""
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

        # Determine selected model
        callback_data = query.data or ""
        if "nano_banana_pro" in callback_data:
            tariff = "nano_banana_pro"
        elif "riverflow_pro" in callback_data:
            tariff = "riverflow_pro"
        elif "flux_2_pro" in callback_data:
            tariff = "flux_2_pro"
        else:
            tariff = "nano_banana_pro"  # fallback

        cost = GENERATION_COST[tariff]

        # Start the edit flow with selected model
        await set_user_state(telegram_id, "waiting_for_generation")
        await update_user_data(telegram_id, tariff=tariff, cost=cost, mode="edit")

        # Show cost only for non-admins
        from shared.config import settings
        is_admin = telegram_id in settings.ADMIN_IDS
        cost_text = "" if is_admin else f"\n\nСтоимость: *{cost}* кредитов"

        # Model-specific descriptions
        if tariff == "nano_banana_pro":
            model_name = "Nano Banana PRO"
            description = (
                "🔥 *Nano Banana PRO* — премиум-модель от Google DeepMind\n\n"
                "*Что умеет:*\n"
                "• Точный текст на любом языке\n"
                "• До 14 референсов одновременно\n"
                "• Реальные данные через Google Search\n"
                "• Разрешение до 4K\n\n"
                "*Примеры промтов:*\n"
                "• «Создай постер с текстом \"SUMMER 2026\" крупными буквами, винтажный стиль»\n"
                "• «Объедини всех людей с этих фото в одну командную фотографию»\n"
                "• «Измени время суток на закат, теплое освещение»"
            )
        elif tariff == "flux_2_pro":
            model_name = "Flux 2 Pro"
            description = (
                "✨ *Flux 2 Pro* — профессиональная модель от Black Forest Labs\n\n"
                "*Что умеет:*\n"
                "• Идеальный рендеринг текста\n"
                "• Фотореализм и острые детали\n"
                "• До 8 референсов для консистентности\n"
                "• Точные цвета (HEX-коды)\n\n"
                "*Примеры промтов:*\n"
                "• «Профессиональная студийная фотография продукта, мягкое освещение»\n"
                "• «Замени фон на пляж из изображения 3»\n"
                "• «Измени цвет машины на синий, сохрани остальное»"
            )
        elif tariff == "riverflow_pro":
            model_name = "Riverflow 2.0 PRO"
            description = (
                "💎 *Riverflow 2.0 PRO* — агентная модель с максимальной детализацией\n\n"
                "*Что умеет:*\n"
                "• Студийное качество, до 4K\n"
                "• Font Control — загрузка своих шрифтов\n"
                "• Автономная самокоррекция (3 итерации)\n"
                "• Прозрачный фон\n\n"
                "*Примеры промтов:*\n"
                "• «Фотореалистичный портрет, 4K, максимальная детализация»\n"
                "• «Продукт на прозрачном фоне, студийное освещение»\n"
                "• «Создай мокап лендинга, современный дизайн»"
            )
        else:
            model_name = "Nano Banana PRO"
            description = ""

        message_text = f"{description}\n\nСтоимость: *{cost}* кредитов" if not is_admin else description
        message_text += "\n\n📸 *Отправьте фото*, которое хотите отредактировать, а затем напишите промт."

        await query.edit_message_text(
            message_text,
            parse_mode="Markdown",
            reply_markup=cancel_keyboard(),
        )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="edit_model_selection_callback")
