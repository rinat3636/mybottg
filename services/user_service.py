"""User-related business logic."""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import User, Generation, Payment, async_session_factory
from shared.config import settings
from services.ledger_service import record_credit_change

logger = logging.getLogger(__name__)

WELCOME_CREDITS = 11
REFERRAL_CREDITS = 11


async def get_or_create_user(
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    referrer_telegram_id: Optional[int] = None,
) -> tuple[User, bool]:
    """Get existing user or create a new one. Returns (user, created)."""
    async with async_session_factory() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if user is not None:
            changed = False

            if username and user.username != username:
                user.username = username
                changed = True
            if first_name and user.first_name != first_name:
                user.first_name = first_name
                changed = True

            # Keep admin status in sync with ENV ADMIN_IDS (supports multiple admins)
            should_be_admin = telegram_id in settings.ADMIN_IDS
            if user.is_admin != should_be_admin:
                user.is_admin = should_be_admin
                changed = True

            if changed:
                await session.commit()

            return user, False

        # Determine admin status
        is_admin = telegram_id in settings.ADMIN_IDS

        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            is_admin=is_admin,
            balance=0,
            referral_code=User.generate_referral_code(),
            referred_by=referrer_telegram_id,
        )
        session.add(user)
        await session.flush()  # get user.id

        # Welcome credits for every new user (admins can still have it, but they are never charged)
        await record_credit_change(
            session,
            user_id=user.id,
            amount=WELCOME_CREDITS,
            reason="welcome",
            reference_id=f"welcome_{telegram_id}",
        )

        # Referral bonuses (optional, on top of welcome credits)
        if referrer_telegram_id and referrer_telegram_id != telegram_id:
            await _apply_referral_bonus(session, user, referrer_telegram_id)

        await session.commit()
        await session.refresh(user)
        return user, True


async def _apply_referral_bonus(
    session: AsyncSession,
    new_user: User,
    referrer_telegram_id: int,
) -> None:
    """Give REFERRAL_CREDITS credits to both the new user and the referrer via ledger."""
    try:
        # Credit new user
        await record_credit_change(
            session,
            user_id=new_user.id,
            amount=REFERRAL_CREDITS,
            reason="referral",
            reference_id=f"ref_new_{new_user.telegram_id}",
        )

        # Credit referrer
        referrer_stmt = select(User).where(User.telegram_id == referrer_telegram_id)
        result = await session.execute(referrer_stmt)
        referrer = result.scalar_one_or_none()
        if referrer:
            await record_credit_change(
                session,
                user_id=referrer.id,
                amount=REFERRAL_CREDITS,
                reason="referral",
                reference_id=f"ref_invite_{new_user.telegram_id}",
            )

        logger.info(
            "Referral bonus applied: new_user=%s referrer=%s",
            new_user.telegram_id,
            referrer_telegram_id,
        )
    except Exception:
        logger.exception("Failed to apply referral bonus")
        raise


async def get_user_by_telegram_id(telegram_id: int) -> Optional[User]:
    async with async_session_factory() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def get_user_by_referral_code(code: str) -> Optional[User]:
    async with async_session_factory() as session:
        stmt = select(User).where(User.referral_code == code)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def add_credits(
    telegram_id: int,
    amount: int,
    reason: str = "payment",
    reference_id: Optional[str] = None,
) -> Optional[User]:
    """Add credits to user balance via ledger."""
    async with async_session_factory() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        if not user:
            return None
        await record_credit_change(session, user.id, amount, reason, reference_id)
        await session.commit()
        await session.refresh(user)
        return user


async def set_admin(telegram_id: int, is_admin: bool) -> bool:
    """Set admin status for a user. Returns True if user found."""
    async with async_session_factory() as session:
        stmt = update(User).where(User.telegram_id == telegram_id).values(is_admin=is_admin)
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount > 0  # type: ignore[union-attr]


async def set_banned(telegram_id: int, is_banned: bool) -> bool:
    """Ban or unban a user. Returns True if user found."""
    async with async_session_factory() as session:
        stmt = update(User).where(User.telegram_id == telegram_id).values(is_banned=is_banned)
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount > 0  # type: ignore[union-attr]


async def get_stats() -> dict:
    """Return aggregate statistics."""
    async with async_session_factory() as session:
        total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0
        total_generations = (await session.execute(select(func.count(Generation.id)))).scalar() or 0
        total_revenue = (
            await session.execute(
                select(func.coalesce(func.sum(Payment.amount_rub), 0)).where(Payment.status == "succeeded")
            )
        ).scalar() or 0

        return {
            "total_users": total_users,
            "total_generations": total_generations,
            "total_revenue": total_revenue,
        }


async def get_all_admins() -> list[User]:
    """Return all admin users."""
    async with async_session_factory() as session:
        stmt = select(User).where(User.is_admin == True)  # noqa: E712
        result = await session.execute(stmt)
        return list(result.scalars().all())
