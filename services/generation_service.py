"""Generation-related business logic."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from shared.database import Generation, async_session_factory
from services.ledger_service import deduct_credits_idempotent

logger = logging.getLogger(__name__)


def new_request_id() -> str:
    """Generate a unique request_id for a generation."""
    return uuid.uuid4().hex


async def create_generation(
    user_id: int,
    prompt: Optional[str],
    tariff: str,
    cost: int,
    request_id: str,
) -> Generation:
    """Create a new generation record."""
    async with async_session_factory() as session:
        gen = Generation(
            request_id=request_id,
            user_id=user_id,
            tariff=tariff,
            prompt=prompt,
            cost=cost,
            status="pending",
        )
        session.add(gen)
        await session.commit()
        await session.refresh(gen)
        return gen


async def deduct_for_generation(
    user_id: int,
    cost: int,
    tariff: str,
    request_id: str,
) -> bool:
    """Idempotently deduct credits for a generation.

    Returns True if deduction succeeded (or was already done).
    """
    reason = "pro"  # Only nano_banana_pro tariff exists now
    async with async_session_factory() as session:
        ok = await deduct_credits_idempotent(
            session, user_id, cost, reason, request_id
        )
        await session.commit()
        return ok


async def complete_generation(generation_id: int, status: str = "completed") -> None:
    """Mark a generation as completed or failed."""
    async with async_session_factory() as session:
        stmt = select(Generation).where(Generation.id == generation_id)
        result = await session.execute(stmt)
        gen = result.scalar_one_or_none()
        if gen:
            gen.status = status
            gen.completed_at = datetime.now(timezone.utc)
            await session.commit()


async def refund_generation(user_id: int, cost: int, request_id: str, tariff: str) -> None:
    """Refund credits for a failed generation via ledger."""
    from services.ledger_service import record_credit_change

    async with async_session_factory() as session:
        await record_credit_change(
            session,
            user_id=user_id,
            amount=cost,
            reason="refund",
            reference_id=f"refund_{request_id}",
        )
        await session.commit()
