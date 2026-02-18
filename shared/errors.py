"""Unified error handling utilities.

Every user-facing error is short and safe (no stack traces, no keys).
Full details go to the application log with a ``trace_id`` for correlation.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from typing import Optional

try:
    import sentry_sdk  # type: ignore
except Exception:  # pragma: no cover
    sentry_sdk = None

logger = logging.getLogger(__name__)

USER_FACING_ERROR = "Произошла ошибка, попробуйте позже."


def generate_trace_id() -> str:
    """Return a short unique trace identifier."""
    return uuid.uuid4().hex[:12]


def log_exception(
    exc: BaseException,
    *,
    trace_id: Optional[str] = None,
    context: str = "",
) -> str:
    """Log full traceback with trace_id.  Returns the trace_id used."""
    if trace_id is None:
        trace_id = generate_trace_id()
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    logger.error(
        "trace_id=%s | %s | %s\n%s",
        trace_id,
        context,
        str(exc),
        "".join(tb),
    )

    # Send to Sentry if configured
    try:
        if sentry_sdk is not None:
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass

    return trace_id


def safe_user_message(trace_id: Optional[str] = None) -> str:
    """Return a user-safe error message.
    
    trace_id is logged but not shown to user to avoid confusion.
    """
    return USER_FACING_ERROR
