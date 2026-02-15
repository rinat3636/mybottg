"""Google Gemini API client for image editing with timeouts and retries."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from google import genai
from google.genai import types

from shared.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Client initialization
# ---------------------------------------------------------------------------

_client: Optional[genai.Client] = None

_REQUEST_TIMEOUT = 120  # seconds
_MAX_RETRIES = 3
_RETRY_BACKOFF = 2  # seconds base


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


def _detect_mime_type(image_bytes: bytes) -> str:
    """Detect MIME type from image bytes by checking magic numbers."""
    if len(image_bytes) < 12:
        return "image/jpeg"  # fallback
    
    # PNG: starts with 89 50 4E 47
    if image_bytes[:4] == b'\x89PNG':
        return "image/png"
    
    # JPEG: starts with FF D8 FF
    if image_bytes[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    
    # WebP: starts with RIFF and contains WEBP
    if image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
        return "image/webp"
    
    # GIF: starts with GIF87a or GIF89a
    if image_bytes[:6] in (b'GIF87a', b'GIF89a'):
        return "image/gif"
    
    # Default fallback
    logger.warning("Could not detect image MIME type, defaulting to image/jpeg")
    return "image/jpeg"


# ---------------------------------------------------------------------------
# Image editing with retries
# ---------------------------------------------------------------------------

async def edit_image(image_bytes: bytes, prompt: str) -> Optional[bytes]:
    """Send image + prompt to Gemini and return the edited image bytes.

    Retries on 429 (rate limit) and 5xx errors.
    Returns PNG image bytes on success, None on failure.
    """
    client = _get_client()
    
    # Detect actual MIME type
    mime_type = _detect_mime_type(image_bytes)
    logger.info("Detected image MIME type: %s", mime_type)

    image_part = types.Part.from_bytes(
        data=image_bytes,
        mime_type=mime_type,
    )

    last_exc: Optional[Exception] = None

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    client.models.generate_content,
                    model="gemini-2.5-flash-image",
                    contents=[
                        types.Content(
                            parts=[
                                image_part,
                                types.Part.from_text(text=prompt),
                            ],
                        ),
                    ],
                    config=types.GenerateContentConfig(
                        response_modalities=["TEXT", "IMAGE"],
                    ),
                ),
                timeout=_REQUEST_TIMEOUT,
            )

            # Extract image from response
            if response.candidates:
                for candidate in response.candidates:
                    if candidate.content and candidate.content.parts:
                        for part in candidate.content.parts:
                            if part.inline_data and part.inline_data.data:
                                logger.info("Successfully received image from Gemini")
                                return part.inline_data.data

            logger.warning("No image found in Gemini response (attempt %d)", attempt)
            return None

        except asyncio.TimeoutError:
            logger.warning("Gemini request timed out (attempt %d/%d)", attempt, _MAX_RETRIES)
            last_exc = asyncio.TimeoutError()
        except Exception as exc:
            last_exc = exc
            exc_str = str(exc).lower()
            # Retry on 429 or 5xx
            if "429" in exc_str or "500" in exc_str or "502" in exc_str or "503" in exc_str or "504" in exc_str:
                wait = _RETRY_BACKOFF * attempt
                logger.warning(
                    "Gemini retryable error (attempt %d/%d), waiting %ds: %s",
                    attempt, _MAX_RETRIES, wait, exc,
                )
                await asyncio.sleep(wait)
                continue
            else:
                logger.exception("Gemini non-retryable error")
                return None

    logger.error("Gemini failed after %d attempts: %s", _MAX_RETRIES, last_exc)
    return None
