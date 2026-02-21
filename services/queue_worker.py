"""Background queue worker that processes generation tasks.

Runs as an asyncio background task inside the FastAPI process.
Polls Redis queue and processes one task at a time.

Supports real cancellation:
- If task is still queued → removed from queue, credits refunded.
- If task is processing → marked as cancelled, result not sent.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Optional

from shared.config import settings

from shared.redis_client import (
    dequeue_task,
    set_task_status,
    get_task_status,
    release_generation_lock,
    TASK_STATUS_PROCESSING,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_CANCELLED,
)
from shared.redis_client_gpu import (
    acquire_gpu_slot,
    release_gpu_slot,
    get_active_gpu_jobs,
    MAX_GPU_JOBS,
)
from services.comfy_client import (
    generate_image,
    generate_video,
    edit_image,
    ComfyUINoFaceError,
    ComfyUITimeoutError,
    ComfyUIConnectionError,
    ComfyUIGenerationError,
)
from services.generation_service import complete_generation
from shared.errors import log_exception, generate_trace_id

logger = logging.getLogger(__name__)

_worker_task: Optional[asyncio.Task] = None
_shutdown_event = asyncio.Event()


async def start_worker() -> None:
    """Start the background queue worker."""
    global _worker_task
    _shutdown_event.clear()
    _worker_task = asyncio.create_task(_worker_loop())
    logger.info("Queue worker started")


async def stop_worker() -> None:
    """Stop the background queue worker gracefully."""
    global _worker_task
    _shutdown_event.set()
    if _worker_task:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None
    logger.info("Queue worker stopped")


async def _worker_loop() -> None:
    """Main worker loop — polls queue every second."""
    while not _shutdown_event.is_set():
        try:
            result = await dequeue_task()
            if result is None:
                await asyncio.sleep(1)
                continue

            task_id, payload = result

            # Check if cancelled before processing
            status = await get_task_status(task_id)
            if status == TASK_STATUS_CANCELLED:
                logger.info("Task %s was cancelled before processing, skipping", task_id)
                await _handle_refund(payload, task_id)
                telegram_id = payload.get("telegram_id", 0)
                await release_generation_lock(telegram_id)
                continue

            # Try to acquire GPU slot
            gpu_acquired = await acquire_gpu_slot(task_id)
            if not gpu_acquired:
                # GPU is at capacity, put task back in queue
                active_jobs = await get_active_gpu_jobs()
                logger.info(
                    "GPU at capacity (%d/%d jobs), task %s waiting",
                    active_jobs, MAX_GPU_JOBS, task_id
                )
                
                # Notify user that they're in queue
                telegram_id = payload.get("telegram_id", 0)
                chat_id = payload.get("chat_id", telegram_id)
                await _notify_user(
                    chat_id,
                    f"⏳ Сервер загружен ({active_jobs}/{MAX_GPU_JOBS} задач). "
                    f"Ваша генерация начнется через несколько секунд..."
                )
                
                # Wait a bit and try again
                await asyncio.sleep(5)
                continue

            try:
                await set_task_status(task_id, TASK_STATUS_PROCESSING)
                await _process_task(task_id, payload)
            finally:
                # Always release GPU slot when done
                await release_gpu_slot(task_id)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            log_exception(exc, context="queue_worker_loop")
            await asyncio.sleep(2)


async def _process_task(task_id: str, payload: dict) -> None:
    """Process a single generation task."""
    task_type = payload.get("task_type", "image")
    
    if task_type == "video":
        await _process_video_task(task_id, payload)
    elif task_type == "edit_photo":
        await _process_edit_photo_task(task_id, payload)
    elif task_type == "animate_photo":
        await _process_animate_photo_task(task_id, payload)
    else:
        await _process_image_task(task_id, payload)


async def _process_image_task(task_id: str, payload: dict) -> None:
    """Process an image generation/editing task."""
    telegram_id = payload.get("telegram_id", 0)
    user_id = payload.get("user_id", 0)
    images_hex = payload.get("images_hex")
    prompt = payload.get("prompt", "")
    aspect_ratio = payload.get("aspect_ratio")
    generation_id = payload.get("generation_id", 0)
    cost = payload.get("cost", 11)
    tariff = payload.get("tariff", "comfyui")
    request_id = payload.get("request_id", task_id)
    chat_id = payload.get("chat_id", telegram_id)
    is_admin = payload.get("is_admin", False)

    trace_id = generate_trace_id()

    try:
        images: list[bytes] = []
        if isinstance(images_hex, list):
            for x in images_hex:
                if isinstance(x, str) and x:
                    try:
                        images.append(bytes.fromhex(x))
                    except (ValueError, TypeError) as exc:
                        logger.warning("Failed to decode hex image data: %s", exc)
                        continue

        # Send "processing" notification
        await _notify_user(chat_id, "⏳ Обрабатываю изображение... Это может занять несколько секунд.")

        # --- Check cancellation before calling ComfyUI ---
        status = await get_task_status(task_id)
        if status == TASK_STATUS_CANCELLED:
            logger.info("Task %s cancelled before ComfyUI call", task_id)
            await complete_generation(generation_id, "cancelled")
            await _handle_refund(payload, task_id)
            await _notify_user(chat_id, "❌ Генерация отменена.")
            return

        # All tariffs now use the same SDXL model via ComfyUI
        # The difference is only in cost/credits
        if images:
            result_bytes = await asyncio.wait_for(
                edit_image(images, prompt, aspect_ratio=aspect_ratio),
                timeout=settings.GENERATION_TIMEOUT,
            )
        else:
            result_bytes = await asyncio.wait_for(
                generate_image(prompt, aspect_ratio=aspect_ratio),
                timeout=settings.GENERATION_TIMEOUT,
            )

        # --- Check cancellation AFTER ComfyUI call ---
        status = await get_task_status(task_id)
        if status == TASK_STATUS_CANCELLED:
            logger.info("Task %s cancelled during ComfyUI processing, discarding result", task_id)
            await complete_generation(generation_id, "cancelled")
            await _handle_refund(payload, task_id)
            await _notify_user(chat_id, "❌ Генерация отменена.")
            return

        if result_bytes:
            await complete_generation(generation_id, "completed")
            await set_task_status(task_id, TASK_STATUS_COMPLETED)

            # Send result
            await _send_result(chat_id, result_bytes)

            # Store "last job" so the user can press "Ещё раз"
            try:
                from shared.redis_client import set_last_job
                await set_last_job(
                    telegram_id,
                    {
                        "prompt": prompt,
                        "aspect_ratio": aspect_ratio,
                        "image_file_ids": payload.get("image_file_ids") or [],
                    },
                )
            except Exception:
                pass
        else:
            await complete_generation(generation_id, "failed")
            await set_task_status(task_id, TASK_STATUS_FAILED)

            # Refund
            # credits refund skipped (no credits system)
            await _notify_user(
                chat_id,
                "❌ Не удалось обработать изображение.\n"
                "Попробуйте другой промт или фото.",
            )

    except ComfyUIConnectionError as exc:
        logger.error("ComfyUI connection error for task %s: %s", task_id, exc)
        await set_task_status(task_id, TASK_STATUS_FAILED)
        await complete_generation(generation_id, "failed")
        await _handle_refund(payload, task_id)
        await _notify_user(
            chat_id,
            "❌ Сервер генерации недоступен.\n"
            "Попробуйте позже."
        )

    except ComfyUITimeoutError as exc:
        logger.error("ComfyUI timeout for task %s: %s", task_id, exc)
        await set_task_status(task_id, TASK_STATUS_FAILED)
        await complete_generation(generation_id, "failed")
        await _handle_refund(payload, task_id)
        await _notify_user(
            chat_id,
            "❌ Генерация заняла слишком много времени.\n"
            "Попробуйте упростить промт."
        )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context=f"process_task:{task_id}")
        await set_task_status(task_id, TASK_STATUS_FAILED)
        try:
            await complete_generation(generation_id, "failed")
        except Exception:
            pass
        await _handle_refund(payload, task_id)
        await _notify_user(chat_id, "Произошла ошибка, попробуйте позже.")
        logger.error("Task %s failed with trace_id=%s", task_id, trace_id)

    finally:
        await release_generation_lock(telegram_id)


async def _handle_refund(payload: dict, task_id: str) -> None:
    """No-op: credits system is disabled."""
    pass  # credits refund skipped (no credits system)


async def _notify_user(chat_id: int, text: str) -> None:
    """Send a text message to the user."""
    try:
        from bot_api.bot import bot_app
        from bot_api.keyboards import main_menu_keyboard
        if bot_app:
            await bot_app.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=main_menu_keyboard(),
            )
    except Exception as exc:
        logger.warning("Failed to notify user %d: %s", chat_id, exc)


async def _send_result(chat_id: int, result_bytes: bytes) -> None:
    """Send the generated image to the user as photo preview + document."""
    try:
        from bot_api.bot import bot_app
        from bot_api.keyboards import main_menu_keyboard, generation_done_keyboard

        if not bot_app:
            return

        # Send compressed photo for quick preview
        photo_bio = io.BytesIO(result_bytes)
        photo_bio.name = "preview.png"
        await bot_app.bot.send_photo(
            chat_id=chat_id,
            photo=photo_bio,
            caption="✅ Готово! Вот ваше изображение.",
        )

        # Send original quality as document
        doc_bio = io.BytesIO(result_bytes)
        doc_bio.name = "result.png"
        await bot_app.bot.send_document(
            chat_id=chat_id,
            document=doc_bio,
            caption="💾 Оригинальное качество (без сжатия)",
            reply_markup=main_menu_keyboard(),
        )

        await bot_app.bot.send_message(
            chat_id=chat_id,
            text="Хотите сделать ещё?",
            reply_markup=generation_done_keyboard(),
        )
    except Exception as exc:
        logger.warning("Failed to send result to user %d: %s", chat_id, exc)


async def _send_video_result(chat_id: int, video_bytes: bytes, duration: int) -> None:
    """Send the generated video to the user."""
    try:
        from bot_api.bot import bot_app
        from bot_api.keyboards import main_menu_keyboard

        if not bot_app:
            return

        # Send video
        video_bio = io.BytesIO(video_bytes)
        video_bio.name = "video.mp4"
        await bot_app.bot.send_video(
            chat_id=chat_id,
            video=video_bio,
            caption=f"✅ Ваше видео готово! ({duration} сек)",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as exc:
        logger.warning("Failed to send video to user %d: %s", chat_id, exc)


async def _process_video_task(task_id: str, payload: dict) -> None:
    """Process a video generation task using LivePortrait."""
    telegram_id = payload.get("telegram_id", 0)
    user_id = payload.get("user_id", 0)
    image_hex = payload.get("image_hex")
    prompt = payload.get("prompt", "")
    duration = payload.get("duration", 5)
    generation_id = payload.get("generation_id", 0)
    cost = payload.get("cost", 70)
    tariff = payload.get("tariff", "kling_video_5s")
    request_id = payload.get("request_id", task_id)
    chat_id = payload.get("chat_id", telegram_id)
    is_admin = payload.get("is_admin", False)

    trace_id = generate_trace_id()

    try:
        # Decode image
        if not image_hex:
            raise ValueError("No image provided for video generation")
        
        image_bytes = bytes.fromhex(image_hex)

        # Send "processing" notification
        await _notify_user(chat_id, f"⏳ Генерирую видео ({duration} сек)... Это может занять несколько минут.")

        # Check cancellation before calling ComfyUI
        status = await get_task_status(task_id)
        if status == TASK_STATUS_CANCELLED:
            logger.info("Video task %s cancelled before ComfyUI call", task_id)
            await complete_generation(generation_id, "cancelled")
            await _handle_refund(payload, task_id)
            await _notify_user(chat_id, "❌ Генерация отменена.")
            return

        # Generate video using LivePortrait
        result_bytes = await asyncio.wait_for(
            generate_video(
                image_bytes=image_bytes,
                prompt=prompt,
                duration_seconds=duration,
            ),
            timeout=settings.GENERATION_TIMEOUT * 2,  # Video takes longer
        )

        # Check cancellation AFTER ComfyUI call
        status = await get_task_status(task_id)
        if status == TASK_STATUS_CANCELLED:
            logger.info("Video task %s cancelled during processing, discarding result", task_id)
            await complete_generation(generation_id, "cancelled")
            await _handle_refund(payload, task_id)
            await _notify_user(chat_id, "❌ Генерация отменена.")
            return

        if result_bytes:
            await complete_generation(generation_id, "completed")
            await set_task_status(task_id, TASK_STATUS_COMPLETED)

            # Send video result
            await _send_video_result(chat_id, result_bytes, duration)
        else:
            await complete_generation(generation_id, "failed")
            await set_task_status(task_id, TASK_STATUS_FAILED)

            # Refund
            # credits refund skipped (no credits system)
            await _notify_user(
                chat_id,
                "❌ Не удалось сгенерировать видео.\n"
                "Попробуйте другой промт или фото.",
            )

    except ComfyUINoFaceError as exc:
        logger.warning("No face detected in video task %s: %s", task_id, exc)
        await set_task_status(task_id, TASK_STATUS_FAILED)
        await complete_generation(generation_id, "failed")
        await _handle_refund(payload, task_id)
        await _notify_user(
            chat_id,
            "❌ На фото не обнаружено лицо.\n"
            "Загрузите фото с четким изображением лица."
        )

    except ComfyUIConnectionError as exc:
        logger.error("ComfyUI connection error for video task %s: %s", task_id, exc)
        await set_task_status(task_id, TASK_STATUS_FAILED)
        await complete_generation(generation_id, "failed")
        await _handle_refund(payload, task_id)
        await _notify_user(
            chat_id,
            "❌ Сервер генерации недоступен.\n"
            "Попробуйте позже."
        )

    except ComfyUITimeoutError as exc:
        logger.error("ComfyUI timeout for video task %s: %s", task_id, exc)
        await set_task_status(task_id, TASK_STATUS_FAILED)
        await complete_generation(generation_id, "failed")
        await _handle_refund(payload, task_id)
        await _notify_user(
            chat_id,
            "❌ Генерация видео заняла слишком много времени. Кредиты возвращены.\n"
            "Попробуйте позже."
        )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context=f"process_video_task:{task_id}")
        await set_task_status(task_id, TASK_STATUS_FAILED)
        try:
            await complete_generation(generation_id, "failed")
        except Exception:
            pass
        await _handle_refund(payload, task_id)
        await _notify_user(chat_id, "Произошла ошибка, попробуйте позже.")
        logger.error("Video task %s failed with trace_id=%s", task_id, trace_id)

    finally:
        await release_generation_lock(telegram_id)
"""New task handlers for edit_photo and animate_photo.

This file contains the processing logic for the new task types.
Append this to queue_worker.py
"""

import io
import logging
from shared.redis_client import (
    set_task_status,
    get_task_status,
    release_generation_lock,
    TASK_STATUS_PROCESSING,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_CANCELLED,
)
from services.comfy_client import (
    generate_image,
    generate_video,
    edit_image,
    ComfyUINoFaceError,
    ComfyUITimeoutError,
    ComfyUIConnectionError,
    ComfyUIGenerationError,
)
from shared.errors import log_exception, generate_trace_id

logger = logging.getLogger(__name__)


async def _process_edit_photo_task(task_id: str, payload: dict) -> None:
    """Process a photo editing task with face preservation."""
    telegram_id = payload.get("telegram_id", 0)
    user_id = payload.get("user_id", 0)
    chat_id = payload.get("chat_id", telegram_id)
    prompt = payload.get("prompt", "")
    photo_bytes = payload.get("photo_bytes")
    cost = payload.get("credits_cost", 25)
    
    trace_id = generate_trace_id()
    
    try:
        # Send "processing" notification
        await _notify_user(chat_id, "🎨 Редактируем ваше фото с сохранением лица...")
        
        # Check cancellation
        status = await get_task_status(task_id)
        if status == TASK_STATUS_CANCELLED:
            logger.info("Task %s cancelled before processing", task_id)
            await _handle_refund(payload, task_id)
            await _notify_user(chat_id, "❌ Редактирование отменено. Кредиты возвращены.")
            return
        
        # Call ComfyUI with IP-Adapter workflow
        result_bytes = await edit_image(
            images=[photo_bytes],
            prompt=prompt,
            aspect_ratio="1:1",  # Keep original aspect ratio
        )
        
        # Check cancellation again
        status = await get_task_status(task_id)
        if status == TASK_STATUS_CANCELLED:
            logger.info("Task %s cancelled after generation", task_id)
            await _handle_refund(payload, task_id)
            await _notify_user(chat_id, "❌ Редактирование отменено. Кредиты возвращены.")
            return
        
        if not result_bytes:
            logger.error("Edit photo failed: no result for task %s", task_id)
            await set_task_status(task_id, TASK_STATUS_FAILED)
            await _handle_refund(payload, task_id)
            await _notify_user(
                chat_id,
                "❌ Не удалось обработать изображение.\n"
                "Попробуйте другой промт или фото."
            )
            return
        
        # Send result
        await _send_result(chat_id, result_bytes)
        await set_task_status(task_id, TASK_STATUS_COMPLETED)
        logger.info("Edit photo task %s completed successfully", task_id)
    
    except ComfyUINoFaceError as exc:
        logger.error("No face detected for task %s: %s", task_id, exc)
        await set_task_status(task_id, TASK_STATUS_FAILED)
        await _handle_refund(payload, task_id)
        await _notify_user(
            chat_id,
            "❌ На фото не обнаружено лицо.\n\n"
            "Загрузите фото с четким изображением лица:\n"
            "• Лицо хорошо освещено\n"
            "• Лицо не закрыто\n"
            "• Лицо занимает достаточную часть фото"
        )
    
    except ComfyUIConnectionError as exc:
        logger.error("ComfyUI connection error for task %s: %s", task_id, exc)
        await set_task_status(task_id, TASK_STATUS_FAILED)
        await _handle_refund(payload, task_id)
        await _notify_user(
            chat_id,
            "❌ Сервер генерации недоступен.\n"
            "Попробуйте позже."
        )
    
    except ComfyUITimeoutError as exc:
        logger.error("ComfyUI timeout for task %s: %s", task_id, exc)
        await set_task_status(task_id, TASK_STATUS_FAILED)
        await _handle_refund(payload, task_id)
        await _notify_user(
            chat_id,
            "❌ Редактирование заняло слишком много времени. Кредиты возвращены.\n"
            "Попробуйте упростить промт."
        )
    
    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context=f"edit_photo_task:{task_id}")
        await set_task_status(task_id, TASK_STATUS_FAILED)
        await _handle_refund(payload, task_id)
        await _notify_user(chat_id, "❌ Произошла ошибка. Кредиты возвращены.")
        logger.error("Edit photo task %s failed with trace_id=%s", task_id, trace_id)
    
    finally:
        await release_generation_lock(telegram_id)


async def _process_animate_photo_task(task_id: str, payload: dict) -> None:
    """Process a photo animation task with LivePortrait."""
    telegram_id = payload.get("telegram_id", 0)
    user_id = payload.get("user_id", 0)
    chat_id = payload.get("chat_id", telegram_id)
    photo_bytes = payload.get("photo_bytes")
    duration_seconds = payload.get("duration_seconds", 10)
    cost = payload.get("credits_cost", 50)
    
    trace_id = generate_trace_id()
    
    try:
        # Send "processing" notification
        await _notify_user(
            chat_id,
            f"🎬 Оживляем ваше фото ({duration_seconds} секунд)...\n"
            f"⏱️ Это может занять до 2 минут."
        )
        
        # Check cancellation
        status = await get_task_status(task_id)
        if status == TASK_STATUS_CANCELLED:
            logger.info("Task %s cancelled before processing", task_id)
            await _handle_refund(payload, task_id)
            await _notify_user(chat_id, "❌ Оживление отменено. Кредиты возвращены.")
            return
        
        # Call ComfyUI with LivePortrait workflow
        result_bytes = await generate_video(
            image_bytes=photo_bytes,
            prompt="",  # LivePortrait doesn't need prompt
            duration_seconds=duration_seconds,
        )
        
        # Check cancellation again
        status = await get_task_status(task_id)
        if status == TASK_STATUS_CANCELLED:
            logger.info("Task %s cancelled after generation", task_id)
            await _handle_refund(payload, task_id)
            await _notify_user(chat_id, "❌ Оживление отменено. Кредиты возвращены.")
            return
        
        if not result_bytes:
            logger.error("Animate photo failed: no result for task %s", task_id)
            await set_task_status(task_id, TASK_STATUS_FAILED)
            await _handle_refund(payload, task_id)
            await _notify_user(
                chat_id,
                "❌ Не удалось создать видео. Кредиты возвращены.\n"
                "Попробуйте другое фото."
            )
            return
        
        # Send video result
        await _send_video_result(chat_id, result_bytes, duration_seconds)
        await set_task_status(task_id, TASK_STATUS_COMPLETED)
        logger.info("Animate photo task %s completed successfully", task_id)
    
    except ComfyUINoFaceError as exc:
        logger.error("No face detected for task %s: %s", task_id, exc)
        await set_task_status(task_id, TASK_STATUS_FAILED)
        await _handle_refund(payload, task_id)
        await _notify_user(
            chat_id,
            "❌ На фото не обнаружено лицо.\n\n"
            "Требования к фото:\n"
            "• Четкое изображение лица\n"
            "• Хорошее освещение\n"
            "• Лицо не закрыто (очками, маской и т.д.)\n"
            "• Лицо смотрит прямо в камеру"
        )
    
    except ComfyUIConnectionError as exc:
        logger.error("ComfyUI connection error for task %s: %s", task_id, exc)
        await set_task_status(task_id, TASK_STATUS_FAILED)
        await _handle_refund(payload, task_id)
        await _notify_user(
            chat_id,
            "❌ Сервер генерации недоступен.\n"
            "Попробуйте позже."
        )
    
    except ComfyUITimeoutError as exc:
        logger.error("ComfyUI timeout for task %s: %s", task_id, exc)
        await set_task_status(task_id, TASK_STATUS_FAILED)
        await _handle_refund(payload, task_id)
        await _notify_user(
            chat_id,
            "❌ Генерация видео заняла слишком много времени. Кредиты возвращены.\n"
            "Попробуйте уменьшить длительность."
        )
    
    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context=f"animate_photo_task:{task_id}")
        await set_task_status(task_id, TASK_STATUS_FAILED)
        await _handle_refund(payload, task_id)
        await _notify_user(chat_id, "❌ Произошла ошибка. Кредиты возвращены.")
        logger.error("Animate photo task %s failed with trace_id=%s", task_id, trace_id)
    
    finally:
        await release_generation_lock(telegram_id)


async def _send_video_result(chat_id: int, result_bytes: bytes, duration: int) -> None:
    """Send the generated video to the user."""
    try:
        from bot_api.bot import bot_app
        from bot_api.keyboards import main_menu_keyboard, generation_done_keyboard
        
        if not bot_app:
            return
        
        # Send video
        video_bio = io.BytesIO(result_bytes)
        video_bio.name = f"animated_{duration}s.mp4"
        await bot_app.bot.send_video(
            chat_id=chat_id,
            video=video_bio,
            caption=f"✅ Готово! Ваше фото оживлено ({duration} секунд).",
            supports_streaming=True,
        )
        
        await bot_app.bot.send_message(
            chat_id=chat_id,
            text="Хотите сделать ещё?",
            reply_markup=generation_done_keyboard(),
        )
    except Exception as exc:
        logger.error("Failed to send video result: %s", exc)
