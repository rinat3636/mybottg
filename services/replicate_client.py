"""Replicate API client for image editing (google/nano-banana-pro).

This replaces the old Gemini integration.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Optional, Union, List, Any

import httpx
import replicate  # type: ignore

from shared.config import settings

logger = logging.getLogger(__name__)

MODEL_NANO_BANANA = "google/nano-banana-pro"
MODEL_RIVERFLOW = "sourceful/riverflow-2.0-pro"

_MAX_WAIT_TIME = 600  # 10 minutes max wait for prediction
_POLL_INTERVAL = 2  # Check status every 2 seconds
_DOWNLOAD_TIMEOUT = 60  # 60 seconds for downloading result
_MAX_RETRIES = 3
_RETRY_BACKOFF = 2  # seconds base


def _ensure_token() -> None:
    # Replicate's SDK reads REPLICATE_API_TOKEN from env.
    token = settings.REPLICATE_API_TOKEN
    if token:
        os.environ.setdefault("REPLICATE_API_TOKEN", token)


async def _download_bytes(url: str) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.content
    except Exception as exc:
        logger.warning("Failed to download Replicate output url=%s err=%s", url, exc)
        return None


def _pick_first_url(output: Any) -> Optional[str]:
    # Replicate outputs vary: could be a URL string, list of URLs, dict with urls, or FileOutput object.
    if output is None:
        return None
    
    # Handle FileOutput object (replicate.helpers.FileOutput)
    if hasattr(output, 'url'):
        url = getattr(output, 'url', None)
        if isinstance(url, str):
            logger.info("Extracted URL from FileOutput object: %s", url)
            return url
    
    # Handle plain string URL
    if isinstance(output, str):
        return output
    
    # Handle list of outputs
    if isinstance(output, list) and output:
        first = output[0]
        # Check if first item is FileOutput object
        if hasattr(first, 'url'):
            url = getattr(first, 'url', None)
            if isinstance(url, str):
                logger.info("Extracted URL from FileOutput object in list: %s", url)
                return url
        # Check if first item is string
        if isinstance(first, str):
            return first
        # Check if first item is dict
        if isinstance(first, dict):
            return first.get("url") or first.get("href")
    
    # Handle dict with nested structure
    if isinstance(output, dict):
        # try common keys
        for k in ("output", "image", "image_url", "url"):
            v = output.get(k)
            # Check if value is FileOutput object
            if hasattr(v, 'url'):
                url = getattr(v, 'url', None)
                if isinstance(url, str):
                    logger.info("Extracted URL from FileOutput object in dict: %s", url)
                    return url
            # Check if value is string
            if isinstance(v, str):
                return v
            # Check if value is list
            if isinstance(v, list) and v:
                first_item = v[0]
                if hasattr(first_item, 'url'):
                    url = getattr(first_item, 'url', None)
                    if isinstance(url, str):
                        return url
                if isinstance(first_item, str):
                    return first_item
    
    # Log unexpected format for debugging
    logger.warning("Could not extract URL from output. Type: %s, Value: %r", type(output).__name__, output)
    return None


async def _wait_for_prediction(prediction_id: str) -> Any:
    """Wait for a Replicate prediction to complete and return the output.
    
    Uses manual polling with configurable interval and timeout.
    """
    _ensure_token()
    
    elapsed = 0
    poll_count = 0
    
    logger.info("Waiting for prediction %s (max %ds, poll every %ds)", prediction_id, _MAX_WAIT_TIME, _POLL_INTERVAL)
    
    while elapsed < _MAX_WAIT_TIME:
        poll_count += 1
        
        # Get prediction status
        prediction = await asyncio.to_thread(
            replicate.predictions.get,
            prediction_id
        )
        
        status = prediction.status
        
        if status == "succeeded":
            logger.info("Prediction %s succeeded after %ds (%d polls)", prediction_id, elapsed, poll_count)
            return prediction.output
        
        elif status == "failed":
            error = getattr(prediction, 'error', 'Unknown error')
            logger.error("Prediction %s failed: %s", prediction_id, error)
            raise Exception(f"Prediction failed: {error}")
        
        elif status == "canceled":
            logger.error("Prediction %s was canceled", prediction_id)
            raise Exception("Prediction was canceled")
        
        elif status in ("starting", "processing"):
            # Still processing, continue polling
            if poll_count % 10 == 0:  # Log every 20 seconds
                logger.info("Prediction %s still %s... (elapsed: %ds)", prediction_id, status, elapsed)
        
        else:
            # Unknown status
            logger.warning("Prediction %s has unknown status: %s", prediction_id, status)
        
        # Wait before next poll
        await asyncio.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL
    
    # Timeout reached
    logger.error("Prediction %s timed out after %ds (%d polls)", prediction_id, _MAX_WAIT_TIME, poll_count)
    raise asyncio.TimeoutError(f"Prediction timed out after {_MAX_WAIT_TIME}s")


async def run_nano_banana(
    *,
    prompt: str,
    images: Optional[list[bytes]] = None,
    aspect_ratio: Optional[str] = None,
    model: str = MODEL_NANO_BANANA,
) -> Optional[bytes]:
    """Run Nano Banana Pro.

    - If ``images`` is provided (1..8), the model will perform image editing.
    - If ``images`` is empty/None, the model will generate an image from text.

    ``aspect_ratio`` is best-effort: if the model rejects the parameter, we retry
    without it.
    """
    _ensure_token()

    last_exc: Optional[Exception] = None

    images = images or []

    def _build_inputs(include_aspect_ratio: bool) -> dict:
        # Match official Replicate API example format
        inputs: dict = {
            "prompt": prompt,
            "resolution": "2K",
            "image_input": [],  # Always include, even if empty
            "output_format": "png",
            "safety_filter_level": "block_only_high",
        }
        if images:
            # Convert bytes to data URI format for Replicate API
            data_uris = []
            for img_bytes in images[:8]:
                b64 = base64.b64encode(img_bytes).decode('utf-8')
                data_uri = f"data:image/png;base64,{b64}"
                data_uris.append(data_uri)
            inputs["image_input"] = data_uris
        if include_aspect_ratio and aspect_ratio:
            # Many Replicate models use 'aspect_ratio'. If it fails, we'll retry.
            inputs["aspect_ratio"] = aspect_ratio
        return inputs

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            # Create prediction
            logger.info("Creating Replicate prediction (attempt %d/%d)", attempt, _MAX_RETRIES)
            
            prediction = await asyncio.to_thread(
                replicate.predictions.create,
                model=model,
                input=_build_inputs(include_aspect_ratio=True)
            )
            
            prediction_id = prediction.id
            logger.info("Prediction created: %s", prediction_id)
            
            # Wait for completion with manual polling
            output = await _wait_for_prediction(prediction_id)
            
            # Extract URL from output
            url = _pick_first_url(output)
            if not url:
                logger.warning("Replicate returned no URL (attempt %d): %r", attempt, output)
                return None

            # Download the result
            logger.info("Downloading result from %s", url)
            data = await _download_bytes(url)
            if data:
                logger.info("Successfully downloaded result (%d bytes)", len(data))
                return data
            else:
                logger.warning("Failed to download result from %s", url)
                return None

        except asyncio.TimeoutError:
            last_exc = asyncio.TimeoutError()
            logger.warning("Replicate request timed out (attempt %d/%d)", attempt, _MAX_RETRIES)
            
        except Exception as exc:
            last_exc = exc
            # If aspect_ratio parameter was rejected, retry once without it.
            msg = str(exc).lower()
            if aspect_ratio and any(k in msg for k in ("aspect_ratio", "unexpected", "invalid")):
                logger.info("Retrying without aspect_ratio parameter")
                try:
                    prediction = await asyncio.to_thread(
                        replicate.predictions.create,
                        model=model,
                        input=_build_inputs(include_aspect_ratio=False)
                    )
                    output = await _wait_for_prediction(prediction.id)
                    url = _pick_first_url(output)
                    if not url:
                        return None
                    return await _download_bytes(url)
                except Exception as retry_exc:
                    logger.warning("Retry without aspect_ratio also failed: %s", retry_exc)
                    
            wait = _RETRY_BACKOFF * attempt
            logger.warning("Replicate error (attempt %d/%d), waiting %ds: %s", attempt, _MAX_RETRIES, wait, exc)
            await asyncio.sleep(wait)

    logger.error("Replicate failed after %d attempts: %s", _MAX_RETRIES, last_exc)
    return None


async def run_riverflow(
    *,
    instruction: str,
    images: Optional[list[bytes]] = None,
    aspect_ratio: Optional[str] = None,
) -> Optional[bytes]:
    """Run Riverflow 2.0 PRO.
    
    Riverflow uses 'instruction' parameter instead of 'prompt'.
    Supports image editing (i2i) with up to 10 init_images.
    """
    _ensure_token()

    last_exc: Optional[Exception] = None
    images = images or []

    def _build_inputs() -> dict:
        inputs: dict = {
            "instruction": instruction,  # Riverflow uses 'instruction' not 'prompt'
            "resolution": "2K",
            "output_format": "webp",
            "enhance_prompt": True,
            "max_iterations": 3,
            "safety_checker": True,
        }
        if images:
            # Convert bytes to data URI format for init_images
            data_uris = []
            for img_bytes in images[:10]:  # Riverflow Pro supports up to 10 images
                b64 = base64.b64encode(img_bytes).decode('utf-8')
                data_uri = f"data:image/png;base64,{b64}"
                data_uris.append(data_uri)
            inputs["init_images"] = data_uris
        if aspect_ratio:
            inputs["aspect_ratio"] = aspect_ratio
        return inputs

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            logger.info("Creating Replicate prediction (attempt %d/%d)", attempt, _MAX_RETRIES)
            
            prediction = await asyncio.to_thread(
                replicate.predictions.create,
                model=MODEL_RIVERFLOW,
                input=_build_inputs()
            )
            
            prediction_id = prediction.id
            logger.info("Prediction created: %s", prediction_id)
            
            # Wait for completion with manual polling
            output = await _wait_for_prediction(prediction_id)
            
            # Extract URL from output
            url = _pick_first_url(output)
            if not url:
                logger.warning("Replicate returned no URL (attempt %d): %r", attempt, output)
                return None

            # Download the result
            logger.info("Downloading result from %s", url)
            data = await _download_bytes(url)
            if data:
                logger.info("Successfully downloaded result (%d bytes)", len(data))
                return data
            else:
                logger.warning("Failed to download result from %s", url)
                return None

        except asyncio.TimeoutError:
            last_exc = asyncio.TimeoutError()
            logger.warning("Replicate request timed out (attempt %d/%d)", attempt, _MAX_RETRIES)
            
        except Exception as exc:
            last_exc = exc
            wait = _RETRY_BACKOFF * attempt
            logger.warning("Replicate error (attempt %d/%d), waiting %ds: %s", attempt, _MAX_RETRIES, wait, exc)
            await asyncio.sleep(wait)

    logger.error("Replicate failed after %d attempts: %s", _MAX_RETRIES, last_exc)
    return None


async def edit_image(images: list[bytes], prompt: str, aspect_ratio: Optional[str] = None, model: str = MODEL_NANO_BANANA) -> Optional[bytes]:
    if model == MODEL_RIVERFLOW:
        return await run_riverflow(instruction=prompt, images=images, aspect_ratio=aspect_ratio)
    else:
        return await run_nano_banana(prompt=prompt, images=images, aspect_ratio=aspect_ratio, model=model)


async def generate_image(prompt: str, aspect_ratio: Optional[str] = None, model: str = MODEL_NANO_BANANA) -> Optional[bytes]:
    if model == MODEL_RIVERFLOW:
        return await run_riverflow(instruction=prompt, images=[], aspect_ratio=aspect_ratio)
    else:
        return await run_nano_banana(prompt=prompt, images=[], aspect_ratio=aspect_ratio, model=model)
