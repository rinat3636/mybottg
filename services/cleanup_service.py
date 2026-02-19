"""Cleanup service for ComfyUI output files.

Runs periodically to delete old generated files from RunPod storage
to prevent disk space issues.

Should be scheduled to run every hour via background task.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

from shared.config import settings

logger = logging.getLogger(__name__)

# Cleanup configuration
CLEANUP_INTERVAL = 3600  # 1 hour in seconds
FILE_MAX_AGE = 86400  # 24 hours in seconds
CLEANUP_BATCH_SIZE = 100  # Files to delete per batch

_cleanup_task: Optional[asyncio.Task] = None
_shutdown_event = asyncio.Event()


def _get_base_url() -> str:
    """Construct base URL for ComfyUI API."""
    url = settings.COMFYUI_API_URL.rstrip("/")
    port = settings.COMFYUI_API_PORT
    
    if ":" in url.split("//")[-1]:
        return url
    
    return f"{url}:{port}"


def _get_headers() -> dict[str, str]:
    """Get headers for ComfyUI API requests."""
    headers = {
        "Content-Type": "application/json",
    }
    
    if settings.COMFYUI_API_KEY:
        headers["Authorization"] = f"Bearer {settings.COMFYUI_API_KEY}"
    
    return headers


async def cleanup_old_files() -> tuple[int, int]:
    """Clean up old output files from ComfyUI.
    
    Returns:
        Tuple of (files_checked, files_deleted)
    """
    base_url = _get_base_url()
    
    # ComfyUI doesn't have a built-in cleanup API
    # We need to use the file system API or custom endpoint
    
    # This is a placeholder implementation
    # You'll need to implement this based on your ComfyUI setup
    
    logger.info("Starting cleanup of old ComfyUI output files (max age: %d hours)", FILE_MAX_AGE // 3600)
    
    files_checked = 0
    files_deleted = 0
    
    try:
        # Option 1: If you have SSH access to RunPod, use subprocess
        # This requires SSH keys to be set up
        
        # Option 2: If ComfyUI has a custom cleanup endpoint
        # url = f"{base_url}/cleanup"
        # params = {"max_age_seconds": FILE_MAX_AGE}
        # async with httpx.AsyncClient(timeout=30) as client:
        #     response = await client.post(url, json=params, headers=_get_headers())
        #     result = response.json()
        #     files_deleted = result.get("deleted", 0)
        
        # Option 3: Manual cleanup via file listing API (if available)
        # This would require ComfyUI to expose file listing
        
        # For now, log that cleanup should be done manually or via SSH
        logger.warning(
            "Automatic cleanup not implemented - please set up manual cleanup on RunPod. "
            "Files older than %d hours should be deleted from /workspace/ComfyUI/output/",
            FILE_MAX_AGE // 3600
        )
        
        # You can implement SSH-based cleanup like this:
        # import subprocess
        # cmd = [
        #     "ssh", "root@your-runpod-ip",
        #     f"find /workspace/ComfyUI/output/ -type f -mtime +1 -delete"
        # ]
        # result = subprocess.run(cmd, capture_output=True, text=True)
        
    except Exception as exc:
        logger.error("Cleanup failed: %s", exc)
    
    logger.info("Cleanup completed: checked=%d, deleted=%d", files_checked, files_deleted)
    return files_checked, files_deleted


async def cleanup_stale_gpu_jobs() -> int:
    """Clean up stale GPU job tracking.
    
    Returns:
        Number of stale jobs cleaned up
    """
    try:
        from shared.redis_client_gpu import cleanup_stale_gpu_jobs as cleanup_gpu
        cleaned = await cleanup_gpu()
        if cleaned > 0:
            logger.info("Cleaned up %d stale GPU job(s)", cleaned)
        return cleaned
    except Exception as exc:
        logger.error("Failed to cleanup stale GPU jobs: %s", exc)
        return 0


async def _cleanup_loop() -> None:
    """Background loop that runs cleanup periodically."""
    logger.info("Cleanup service started (interval: %d seconds)", CLEANUP_INTERVAL)
    
    while not _shutdown_event.is_set():
        try:
            # Run file cleanup
            await cleanup_old_files()
            
            # Run GPU job cleanup
            await cleanup_stale_gpu_jobs()
            
            # Wait for next interval
            await asyncio.sleep(CLEANUP_INTERVAL)
            
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Cleanup loop error: %s", exc)
            await asyncio.sleep(60)  # Wait 1 minute before retry on error
    
    logger.info("Cleanup service stopped")


async def start_cleanup_service() -> None:
    """Start the background cleanup service."""
    global _cleanup_task
    _shutdown_event.clear()
    _cleanup_task = asyncio.create_task(_cleanup_loop())
    logger.info("Cleanup service scheduled")


async def stop_cleanup_service() -> None:
    """Stop the background cleanup service."""
    global _cleanup_task
    _shutdown_event.set()
    if _cleanup_task:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        _cleanup_task = None
    logger.info("Cleanup service stopped")


# Manual cleanup script for RunPod (to be run via SSH or cron)
RUNPOD_CLEANUP_SCRIPT = """#!/bin/bash
# ComfyUI Output Cleanup Script
# Run this via cron every hour on your RunPod instance

OUTPUT_DIR="/workspace/ComfyUI/output"
MAX_AGE_HOURS=24

echo "Starting cleanup of ComfyUI output files older than ${MAX_AGE_HOURS} hours..."

# Find and delete files older than MAX_AGE_HOURS
find "$OUTPUT_DIR" -type f -mmin +$((MAX_AGE_HOURS * 60)) -print -delete | wc -l

echo "Cleanup completed"

# Optional: Log disk usage
df -h "$OUTPUT_DIR"
"""
