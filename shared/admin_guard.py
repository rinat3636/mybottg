"""Global admin-free guard — centralised admin check and credit bypass.

Usage in handlers:

    from shared.admin_guard import is_admin_user, check_and_charge

    # Simple admin check
    if not await is_admin_user(telegram_id):
        ...

    # Charge credits (admins are never charged)
    ok = await check_and_charge(user, cost, tariff, request_id)
"""

from __future__ import annotations

import logging
from typing import Optional

from shared.config import settings

logger = logging.getLogger(__name__)


def is_admin_id(telegram_id: int) -> bool:
    """Check if a telegram_id belongs to an admin (config-level check)."""
    return telegram_id in settings.ADMIN_IDS


async def is_admin_user(telegram_id: int) -> bool:
    """Check if a telegram_id belongs to an admin (DB-level check).

    Falls back to config-level check if user is not in DB.
    """
    from services.user_service import get_user_by_telegram_id

    user = await get_user_by_telegram_id(telegram_id)
    if user is not None:
        return user.is_admin
    return is_admin_id(telegram_id)


async def check_and_charge(
    user_id: int,
    is_admin: bool,
    cost: int,
    tariff: str,
    request_id: str,
) -> bool:
    """Deduct credits for a generation.  Admins are NEVER charged.

    Returns True if the operation succeeded (admin bypass or successful deduction).
    Returns False if insufficient balance.
    """
    if is_admin:
        logger.info(
            "Admin bypass: user_id=%d skipping charge of %d credits (tariff=%s, req=%s)",
            user_id, cost, tariff, request_id,
        )
        return True

    from services.generation_service import deduct_for_generation
    return await deduct_for_generation(user_id, cost, tariff, request_id)


async def refund_if_needed(
    user_id: int,
    is_admin: bool,
    cost: int,
    request_id: str,
    tariff: str,
) -> None:
    """Refund credits for a cancelled/failed generation.  Admins are skipped."""
    if is_admin:
        return

    from services.generation_service import refund_generation
    await refund_generation(user_id, cost, request_id, tariff)
