"""Credit Ledger service — every credit change is recorded here.

Reasons: payment, nano, pro, refund, referral, welcome
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import CreditLedger, User

logger = logging.getLogger(__name__)


async def record_credit_change(
    session: AsyncSession,
    user_id: int,
    amount: int,
    reason: str,
    reference_id: Optional[str] = None,
) -> CreditLedger:
    """Atomically update user balance and write a ledger entry.

    ``amount`` is positive for credits, negative for debits.
    Must be called inside an active session/transaction.
    """
    # Atomic balance update
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(balance=User.balance + amount)
        .returning(User.balance)
    )
    result = await session.execute(stmt)
    new_balance = result.scalar_one()

    entry = CreditLedger(
        user_id=user_id,
        amount=amount,
        reason=reason,
        reference_id=reference_id,
        balance_after=new_balance,
    )
    session.add(entry)

    logger.info(
        "Ledger: user_id=%d amount=%+d reason=%s ref=%s balance_after=%d",
        user_id, amount, reason, reference_id, new_balance,
    )
    return entry


async def deduct_credits_idempotent(
    session: AsyncSession,
    user_id: int,
    amount: int,
    reason: str,
    reference_id: str,
) -> bool:
    """Deduct credits only if not already deducted for this reference_id.

    Uses ``reference_id`` (generation request_id) for idempotency.
    Returns True if deduction was performed, False if already done or
    insufficient balance.
    """
    from sqlalchemy import select

    # Check idempotency — already deducted?
    existing = await session.execute(
        select(CreditLedger.id).where(
            CreditLedger.reference_id == reference_id,
            CreditLedger.amount < 0,
        )
    )
    if existing.scalar_one_or_none() is not None:
        logger.info("Deduction already recorded for ref=%s, skipping", reference_id)
        return True  # already deducted — treat as success

    # Check balance with row-level lock
    user_row = await session.execute(
        select(User).where(User.id == user_id).with_for_update()
    )
    user = user_row.scalar_one_or_none()
    if user is None or user.balance < amount:
        return False

    await record_credit_change(
        session, user_id, -amount, reason, reference_id
    )
    return True
