"""Background reconciliation for pending YooKassa payments.

Purpose: if a webhook is missed/delayed, periodically verify old pending payments
via YooKassa API and accrue credits.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from services.payment_service import reconcile_pending_payments

logger = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None
_stop = asyncio.Event()


async def start_reconciler(interval_seconds: int = 300) -> None:
    global _task
    _stop.clear()
    _task = asyncio.create_task(_loop(interval_seconds))
    logger.info("Payment reconciler started (interval=%ss)", interval_seconds)


async def stop_reconciler() -> None:
    global _task
    _stop.set()
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    logger.info("Payment reconciler stopped")


async def _loop(interval_seconds: int) -> None:
    while not _stop.is_set():
        try:
            processed = await reconcile_pending_payments()
            if processed:
                logger.info("Reconciled %d pending payments", processed)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Payment reconcile loop error")
        await asyncio.sleep(interval_seconds)
