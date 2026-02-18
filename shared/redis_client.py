"""Async Redis client for FSM state, rate limiting, task queue, and caching.

Supports ``rediss://`` URLs and ``REDIS_SSL=true`` ENV for TLS connections.
"""

from __future__ import annotations

import json
import logging
import ssl
from typing import Any, Optional

import redis.asyncio as aioredis

from shared.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

_redis: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    """Return (and lazily create) the global Redis connection."""
    global _redis
    if _redis is None:
        url = settings.REDIS_URL
        use_ssl = settings.redis_ssl_enabled

        kwargs: dict[str, Any] = {
            "decode_responses": True,
            "socket_connect_timeout": 5,
        }

        if use_ssl:
            # Accept self-signed certs on Railway / managed Redis
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            kwargs["ssl"] = ssl_ctx
            logger.info("Redis: connecting with TLS enabled")
        else:
            logger.info("Redis: connecting without TLS")

        try:
            _redis = aioredis.from_url(url, **kwargs)
            # Test connection
            await _redis.ping()
            logger.info("Redis: connection established")
        except Exception:
            logger.exception("Redis: failed to connect")
            raise

    return _redis


async def close_redis() -> None:
    """Gracefully close the Redis connection."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


# ---------------------------------------------------------------------------
# FSM helpers  (user state machine)
# ---------------------------------------------------------------------------

_STATE_PREFIX = "fsm:state:"
_DATA_PREFIX = "fsm:data:"


async def get_user_state(telegram_id: int) -> Optional[str]:
    r = await get_redis()
    return await r.get(f"{_STATE_PREFIX}{telegram_id}")


async def set_user_state(telegram_id: int, state: str, ttl: int = 3600) -> None:
    r = await get_redis()
    await r.set(f"{_STATE_PREFIX}{telegram_id}", state, ex=ttl)


async def clear_user_state(telegram_id: int) -> None:
    r = await get_redis()
    await r.delete(f"{_STATE_PREFIX}{telegram_id}")
    await r.delete(f"{_DATA_PREFIX}{telegram_id}")


async def get_user_data(telegram_id: int) -> dict[str, Any]:
    r = await get_redis()
    raw = await r.get(f"{_DATA_PREFIX}{telegram_id}")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


async def set_user_data(telegram_id: int, data: dict[str, Any], ttl: int = 3600) -> None:
    r = await get_redis()
    await r.set(f"{_DATA_PREFIX}{telegram_id}", json.dumps(data), ex=ttl)


async def update_user_data(telegram_id: int, **kwargs: Any) -> dict[str, Any]:
    """Merge kwargs into existing user data and persist."""
    data = await get_user_data(telegram_id)
    data.update(kwargs)
    await set_user_data(telegram_id, data)
    return data


# ---------------------------------------------------------------------------
# Rate limiting  (sliding window counter)
# ---------------------------------------------------------------------------

_RATE_PREFIX = "rate:"


async def check_rate_limit(
    telegram_id: int,
    action: str = "cmd",
    max_requests: int = 5,
    window_seconds: int = 60,
) -> bool:
    """Return True if the action is allowed, False if rate-limited."""
    r = await get_redis()
    key = f"{_RATE_PREFIX}{action}:{telegram_id}"
    current = await r.get(key)
    if current is not None and int(current) >= max_requests:
        return False
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, window_seconds)
    await pipe.execute()
    return True


# ---------------------------------------------------------------------------
# Active generation lock  (1 concurrent generation per user)
# ---------------------------------------------------------------------------

_ACTIVE_GEN_PREFIX = "active_gen:"


async def acquire_generation_lock(telegram_id: int, task_id: str, ttl: int = settings.GENERATION_LOCK_TTL) -> bool:
    """Try to acquire a per-user generation lock.  Returns True on success."""
    r = await get_redis()
    key = f"{_ACTIVE_GEN_PREFIX}{telegram_id}"
    result = await r.set(key, task_id, nx=True, ex=ttl)
    return result is not None


async def release_generation_lock(telegram_id: int) -> None:
    """Release the per-user generation lock."""
    r = await get_redis()
    await r.delete(f"{_ACTIVE_GEN_PREFIX}{telegram_id}")


async def get_active_generation(telegram_id: int) -> Optional[str]:
    """Return the task_id of the active generation, or None."""
    r = await get_redis()
    return await r.get(f"{_ACTIVE_GEN_PREFIX}{telegram_id}")


# ---------------------------------------------------------------------------
# Task queue  (Redis-based simple queue with status tracking)
# ---------------------------------------------------------------------------

class QueueLimitError(RuntimeError):
    """Raised when the queue is full or user exceeded per-user queue limits."""


# Per-user queued task counter (queued only; processing is handled by the generation lock)
_USER_QUEUE_PREFIX = "user_queue_count:"

# Task TTL and status constants
_TASK_TTL = 3600  # 1 hour
TASK_STATUS_QUEUED = "queued"
TASK_STATUS_PROCESSING = "processing"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"


async def _get_user_queue_count(telegram_id: int) -> int:
    r = await get_redis()
    v = await r.get(f"{_USER_QUEUE_PREFIX}{telegram_id}")
    try:
        return int(v or 0)
    except Exception:
        return 0


async def _incr_user_queue_count(telegram_id: int, ttl: int = _TASK_TTL) -> int:
    r = await get_redis()
    key = f"{_USER_QUEUE_PREFIX}{telegram_id}"
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, ttl)
    res = await pipe.execute()
    try:
        return int(res[0])
    except Exception:
        return 0


async def _decr_user_queue_count(telegram_id: int) -> None:
    r = await get_redis()
    key = f"{_USER_QUEUE_PREFIX}{telegram_id}"
    try:
        # Ensure we don't go negative
        pipe = r.pipeline()
        pipe.decr(key)
        pipe.get(key)
        out = await pipe.execute()
        cur = int(out[1] or 0)
        if cur <= 0:
            await r.delete(key)
    except Exception:
        # Best-effort; counter will self-heal over TTL
        return



_TASK_PREFIX = "task:"
_TASK_QUEUE = "task_queue"


# ---------------------------------------------------------------------------
# Media-group (album) buffer
# ---------------------------------------------------------------------------

_MEDIA_GROUP_PREFIX = "media_group:"


async def add_media_group_item(
    telegram_id: int,
    media_group_id: str,
    file_id: str,
    caption: str | None = None,
    ttl: int = 120,
) -> dict[str, Any]:
    """Append an item to a Telegram media group buffer.

    We buffer file_ids for a short time so we can process albums (up to 8 images)
    as a single generation request.
    """
    r = await get_redis()
    key = f"{_MEDIA_GROUP_PREFIX}{telegram_id}:{media_group_id}"
    raw = await r.get(key)
    data: dict[str, Any]
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}

    items = data.get("file_ids") or []
    if file_id not in items:
        items.append(file_id)
    data["file_ids"] = items[:8]
    if caption and caption.strip():
        data["caption"] = caption.strip()

    await r.set(key, json.dumps(data), ex=ttl)
    return data


async def get_media_group(telegram_id: int, media_group_id: str) -> dict[str, Any]:
    r = await get_redis()
    raw = await r.get(f"{_MEDIA_GROUP_PREFIX}{telegram_id}:{media_group_id}")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


async def delete_media_group(telegram_id: int, media_group_id: str) -> None:
    r = await get_redis()
    await r.delete(f"{_MEDIA_GROUP_PREFIX}{telegram_id}:{media_group_id}")


async def acquire_media_group_process_lock(telegram_id: int, media_group_id: str, ttl: int = 120) -> bool:
    """Ensure album is processed only once."""
    r = await get_redis()
    key = f"{_MEDIA_GROUP_PREFIX}lock:{telegram_id}:{media_group_id}"
    result = await r.set(key, "1", nx=True, ex=ttl)
    return result is not None


# ---------------------------------------------------------------------------
# Last job cache (for "Ещё раз")
# ---------------------------------------------------------------------------

_LAST_JOB_PREFIX = "last_job:"


async def set_last_job(telegram_id: int, payload: dict[str, Any], ttl: int = 24 * 3600) -> None:
    r = await get_redis()
    await r.set(f"{_LAST_JOB_PREFIX}{telegram_id}", json.dumps(payload), ex=ttl)


async def get_last_job(telegram_id: int) -> dict[str, Any]:
    r = await get_redis()
    raw = await r.get(f"{_LAST_JOB_PREFIX}{telegram_id}")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


async def enqueue_task(task_id: str, payload: dict[str, Any]) -> int:
    """Add a task to the queue and set its status to 'queued'.

    Enforces:
      - Global queue limit (settings.MAX_GLOBAL_QUEUE_SIZE)
      - Per-user queued limit (settings.MAX_QUEUED_TASKS_PER_USER)

    Returns:
      An integer count of how many tasks were already in the queue *before* this task
      was added (i.e., an approximate "position ahead").

    Raises:
      QueueLimitError if the task cannot be enqueued.
    """
    r = await get_redis()

    # Approximate position: tasks already waiting in the Redis list.
    # (Does not include a currently-processing task, if your worker pops immediately.)
    position_ahead: int = 0

    # Global hard limit
    try:
        qlen = await r.llen(_TASK_QUEUE)
        if qlen is not None:
            position_ahead = int(qlen)
        if qlen is not None and int(qlen) >= int(settings.MAX_GLOBAL_QUEUE_SIZE):
            raise QueueLimitError("global_queue_full")
    except QueueLimitError:
        raise
    except Exception:
        # If LLEN fails, proceed (best-effort)
        pass

    telegram_id = int(payload.get("telegram_id") or 0)
    if telegram_id:
        current = await _get_user_queue_count(telegram_id)
        if current >= int(settings.MAX_QUEUED_TASKS_PER_USER):
            raise QueueLimitError("user_queue_limit")

        # Increment *before* push so burst traffic is throttled deterministically
        new_count = await _incr_user_queue_count(telegram_id)
        if new_count > int(settings.MAX_QUEUED_TASKS_PER_USER):
            # Roll back increment best-effort and fail
            await _decr_user_queue_count(telegram_id)
            raise QueueLimitError("user_queue_limit")

    key = f"{_TASK_PREFIX}{task_id}"
    payload["status"] = TASK_STATUS_QUEUED
    await r.set(key, json.dumps(payload), ex=_TASK_TTL)
    await r.rpush(_TASK_QUEUE, task_id)

    return position_ahead



async def dequeue_task() -> Optional[tuple[str, dict[str, Any]]]:
    """Pop the next task from the queue.  Returns (task_id, payload) or None.

    Decrements per-user queued counter (best-effort) because the task leaves the queue
    and becomes "processing".
    """
    r = await get_redis()
    task_id = await r.lpop(_TASK_QUEUE)
    if task_id is None:
        return None
    key = f"{_TASK_PREFIX}{task_id}"
    raw = await r.get(key)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    try:
        telegram_id = int(payload.get("telegram_id") or 0)
        if telegram_id:
            await _decr_user_queue_count(telegram_id)
    except Exception:
        pass

    return task_id, payload



async def get_task_payload(task_id: str) -> Optional[dict[str, Any]]:
    """Return the full payload of a task, or None if not found."""
    r = await get_redis()
    raw = await r.get(f"{_TASK_PREFIX}{task_id}")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return None


async def set_task_status(task_id: str, status: str) -> None:
    """Update the status field of a task."""
    r = await get_redis()
    key = f"{_TASK_PREFIX}{task_id}"
    raw = await r.get(key)
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        payload["status"] = status
        await r.set(key, json.dumps(payload), ex=_TASK_TTL)


async def get_task_status(task_id: str) -> Optional[str]:
    """Return the current status of a task, or None if expired / not found."""
    r = await get_redis()
    raw = await r.get(f"{_TASK_PREFIX}{task_id}")
    if raw:
        try:
            return json.loads(raw).get("status")
        except json.JSONDecodeError:
            pass
    return None


async def cancel_task(task_id: str) -> bool:
    """Cancel a queued task.  Returns True on success."""
    r = await get_redis()
    key = f"{_TASK_PREFIX}{task_id}"
    raw = await r.get(key)
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if payload.get("status") != TASK_STATUS_QUEUED:
        return False
    payload["status"] = TASK_STATUS_CANCELLED
    await r.set(key, json.dumps(payload), ex=_TASK_TTL)
    await r.lrem(_TASK_QUEUE, 1, task_id)
    try:
        telegram_id = int(payload.get("telegram_id") or 0)
        if telegram_id:
            await _decr_user_queue_count(telegram_id)
    except Exception:
        pass
    return True


async def cancel_processing_task(task_id: str) -> bool:
    """Mark a processing task as cancelled so the worker skips sending the result.

    Returns True if the task was in 'processing' state and was marked cancelled.
    """
    r = await get_redis()
    key = f"{_TASK_PREFIX}{task_id}"
    raw = await r.get(key)
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if payload.get("status") != TASK_STATUS_PROCESSING:
        return False
    payload["status"] = TASK_STATUS_CANCELLED
    await r.set(key, json.dumps(payload), ex=_TASK_TTL)
    return True


# ---------------------------------------------------------------------------
# Generic cache helpers
# ---------------------------------------------------------------------------

_CACHE_PREFIX = "cache:"


async def cache_get(key: str) -> Optional[str]:
    r = await get_redis()
    return await r.get(f"{_CACHE_PREFIX}{key}")


async def cache_set(key: str, value: str, ttl: int = 300) -> None:
    r = await get_redis()
    await r.set(f"{_CACHE_PREFIX}{key}", value, ex=ttl)
