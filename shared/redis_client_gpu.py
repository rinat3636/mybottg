"""GPU job limit tracking for ComfyUI.

This module adds GPU-level concurrency control on top of the existing
per-user generation locks. It ensures that only a limited number of
tasks are actively processing on the GPU at any given time.

For RTX A5000 with 20GB VRAM:
- MAX_GPU_JOBS = 1 (safe default, one task at a time)
- Can be increased to 2 for smaller models/resolutions
"""

from __future__ import annotations

import logging
from typing import Optional

from shared.redis_client import get_redis

logger = logging.getLogger(__name__)

# GPU job tracking
_GPU_JOBS_KEY = "gpu:active_jobs"
_GPU_JOB_PREFIX = "gpu:job:"
_GPU_JOB_TTL = 900  # 15 minutes max per job

# Maximum concurrent GPU jobs
# For A5000 (20GB VRAM): 1 is safe, 2 may work for smaller tasks
MAX_GPU_JOBS = 1


async def get_active_gpu_jobs() -> int:
    """Get the current number of active GPU jobs.
    
    Returns:
        Number of active GPU jobs
    """
    r = await get_redis()
    try:
        count = await r.get(_GPU_JOBS_KEY)
        return int(count or 0)
    except Exception as exc:
        logger.warning("Failed to get active GPU jobs count: %s", exc)
        return 0


async def acquire_gpu_slot(task_id: str) -> bool:
    """Try to acquire a GPU slot for processing.
    
    Args:
        task_id: Unique task identifier
        
    Returns:
        True if slot acquired, False if GPU is at capacity
    """
    r = await get_redis()
    
    try:
        # Use Lua script for atomic check-and-increment
        lua_script = """
        local jobs_key = KEYS[1]
        local job_key = KEYS[2]
        local max_jobs = tonumber(ARGV[1])
        local ttl = tonumber(ARGV[2])
        
        local current = tonumber(redis.call('GET', jobs_key) or 0)
        
        if current >= max_jobs then
            return 0
        end
        
        redis.call('INCR', jobs_key)
        redis.call('SETEX', job_key, ttl, '1')
        
        return 1
        """
        
        result = await r.eval(
            lua_script,
            2,
            _GPU_JOBS_KEY,
            f"{_GPU_JOB_PREFIX}{task_id}",
            MAX_GPU_JOBS,
            _GPU_JOB_TTL,
        )
        
        if result == 1:
            logger.info("GPU slot acquired for task %s (active jobs: %d/%d)", task_id, await get_active_gpu_jobs(), MAX_GPU_JOBS)
            return True
        else:
            logger.info("GPU at capacity, cannot acquire slot for task %s", task_id)
            return False
            
    except Exception as exc:
        logger.error("Failed to acquire GPU slot: %s", exc)
        # On error, allow the task to proceed (fail-open)
        return True


async def release_gpu_slot(task_id: str) -> None:
    """Release a GPU slot after processing completes.
    
    Args:
        task_id: Task identifier that held the slot
    """
    r = await get_redis()
    
    try:
        job_key = f"{_GPU_JOB_PREFIX}{task_id}"
        
        # Check if this task actually held a slot
        exists = await r.exists(job_key)
        
        if exists:
            # Use Lua script for atomic decrement
            lua_script = """
            local jobs_key = KEYS[1]
            local job_key = KEYS[2]
            
            local current = tonumber(redis.call('GET', jobs_key) or 0)
            
            if current > 0 then
                redis.call('DECR', jobs_key)
            end
            
            redis.call('DEL', job_key)
            
            return redis.call('GET', jobs_key) or 0
            """
            
            remaining = await r.eval(
                lua_script,
                2,
                _GPU_JOBS_KEY,
                job_key,
            )
            
            logger.info("GPU slot released for task %s (remaining active jobs: %d)", task_id, int(remaining or 0))
        else:
            logger.debug("Task %s did not hold a GPU slot", task_id)
            
    except Exception as exc:
        logger.error("Failed to release GPU slot for task %s: %s", task_id, exc)


async def cleanup_stale_gpu_jobs() -> int:
    """Clean up stale GPU job tracking (jobs that crashed without releasing).
    
    This should be called periodically (e.g., every 5 minutes) to ensure
    the counter doesn't get stuck if a worker crashes.
    
    Returns:
        Number of stale jobs cleaned up
    """
    r = await get_redis()
    
    try:
        # Get all GPU job keys
        pattern = f"{_GPU_JOB_PREFIX}*"
        cursor = 0
        job_keys = []
        
        while True:
            cursor, keys = await r.scan(cursor, match=pattern, count=100)
            job_keys.extend(keys)
            if cursor == 0:
                break
        
        # Count active jobs
        actual_count = len(job_keys)
        
        # Get reported count
        reported_count = await get_active_gpu_jobs()
        
        if actual_count != reported_count:
            logger.warning(
                "GPU job count mismatch: reported=%d, actual=%d - fixing",
                reported_count, actual_count
            )
            
            # Reset to actual count
            if actual_count > 0:
                await r.set(_GPU_JOBS_KEY, actual_count)
            else:
                await r.delete(_GPU_JOBS_KEY)
            
            return abs(reported_count - actual_count)
        
        return 0
        
    except Exception as exc:
        logger.error("Failed to cleanup stale GPU jobs: %s", exc)
        return 0


async def get_gpu_queue_position(task_id: str) -> Optional[int]:
    """Get the approximate queue position for a task waiting for GPU.
    
    Args:
        task_id: Task identifier
        
    Returns:
        Approximate position in queue (0 = next, None = unknown)
    """
    # This is a simple implementation - could be enhanced with a proper waiting queue
    active_jobs = await get_active_gpu_jobs()
    
    if active_jobs >= MAX_GPU_JOBS:
        # GPU is at capacity, task is waiting
        return active_jobs - MAX_GPU_JOBS + 1
    else:
        # GPU has capacity, task can start immediately
        return 0
