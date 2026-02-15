"""Payment processing with YooKassa — credit packages via payment link.

Idempotent webhook processing with transactional credit accrual:
BEGIN → check payment_id → accrue credits → COMMIT.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from yookassa import Configuration, Payment as YooPayment

from shared.config import settings, CREDIT_PACKAGES
from shared.database import Payment, User, async_session_factory
from services.ledger_service import record_credit_change

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# YooKassa SDK configuration
# ---------------------------------------------------------------------------

if settings.YOOKASSA_SHOP_ID and settings.YOOKASSA_SECRET_KEY:
    Configuration.account_id = settings.YOOKASSA_SHOP_ID
    Configuration.secret_key = settings.YOOKASSA_SECRET_KEY
else:
    logger.warning("YooKassa credentials not configured — payments disabled")


# ---------------------------------------------------------------------------
# Service functions
# ---------------------------------------------------------------------------

async def create_payment(telegram_id: int, amount_rub: int) -> Optional[dict]:
    """Create a YooKassa payment for a credit package.

    Returns dict with payment_url, payment_id, amount, credits on success.
    """
    if not settings.YOOKASSA_SHOP_ID or not settings.YOOKASSA_SECRET_KEY:
        logger.error("YooKassa not configured, cannot create payment")
        return None

    credits = CREDIT_PACKAGES.get(amount_rub)
    if credits is None:
        return None

    async with async_session_factory() as session:
        # Find user
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        if not user:
            return None

        idempotency_key = str(uuid.uuid4())

        try:
            yoo_payment = YooPayment.create(
                {
                    "amount": {
                        "value": f"{amount_rub}.00",
                        "currency": "RUB",
                    },
                    "confirmation": {
                        "type": "redirect",
                        "return_url": f"https://t.me/{(await _get_bot_username()) or 'bot'}",
                    },
                    "capture": True,
                    "description": f"Пополнение {credits} кредитов — Nano Banana Bot",
                    "metadata": {
                        "telegram_id": str(telegram_id),
                        "amount_rub": str(amount_rub),
                    },
                },
                idempotency_key,
            )
        except Exception:
            logger.exception("YooKassa payment creation failed")
            return None

        # Save to DB
        payment = Payment(
            user_id=user.id,
            amount_rub=amount_rub,
            credits=credits,
            status="pending",
            yookassa_payment_id=yoo_payment.id,
        )
        session.add(payment)
        await session.commit()

        return {
            "payment_url": yoo_payment.confirmation.confirmation_url,
            "payment_id": yoo_payment.id,
            "amount": amount_rub,
            "credits": credits,
        }


async def process_yookassa_webhook(data: dict) -> bool:
    """Process incoming YooKassa webhook notification.

    Fully transactional and idempotent:
    BEGIN → SELECT payment FOR UPDATE → check status → accrue → COMMIT.
    If payment_id already processed, returns 200 OK without re-accrual.
    """
    try:
        event_type = data.get("event")
        payment_obj = data.get("object", {})
        yookassa_id = payment_obj.get("id")
        status = payment_obj.get("status")

        if event_type != "payment.succeeded" or status != "succeeded":
            logger.info("Ignoring YooKassa event: %s status=%s", event_type, status)
            return False

        if not yookassa_id:
            return False

        # Verify payment via API call for extra security
        try:
            verified = YooPayment.find_one(yookassa_id)
            if verified.status != "succeeded":
                logger.warning(
                    "Payment %s not confirmed via API (status=%s)",
                    yookassa_id, verified.status,
                )
                return False
        except Exception:
            logger.exception("Failed to verify payment %s via API", yookassa_id)
            # Continue with webhook data if API check fails

        # Transactional processing with row-level lock
        async with async_session_factory() as session:
            async with session.begin():
                stmt = (
                    select(Payment)
                    .where(Payment.yookassa_payment_id == yookassa_id)
                    .with_for_update()
                )
                result = await session.execute(stmt)
                payment = result.scalar_one_or_none()

                if not payment:
                    logger.warning("Payment %s not found in DB", yookassa_id)
                    return False

                # Idempotency: already processed?
                if payment.status == "succeeded":
                    logger.info("Payment %s already processed (idempotent)", yookassa_id)
                    return True

                # Validate amount matches
                webhook_amount = payment_obj.get("amount", {}).get("value", "0")
                try:
                    webhook_rub = int(float(webhook_amount))
                except (ValueError, TypeError):
                    webhook_rub = 0
                if webhook_rub != payment.amount_rub:
                    logger.warning(
                        "Payment %s amount mismatch: webhook=%d db=%d",
                        yookassa_id, webhook_rub, payment.amount_rub,
                    )
                    return False

                # Update payment status
                payment.status = "succeeded"
                payment.paid_at = datetime.now(timezone.utc)

                # Add credits via ledger (inside the same transaction)
                await record_credit_change(
                    session,
                    user_id=payment.user_id,
                    amount=payment.credits,
                    reason="payment",
                    reference_id=yookassa_id,
                )

            # session.begin() auto-commits on exit

            logger.info(
                "Payment %s processed: +%d credits for user_id=%d",
                yookassa_id, payment.credits, payment.user_id,
            )
            return True

    except Exception:
        logger.exception("Error processing YooKassa webhook")
        return False


async def get_payment_user_telegram_id(yookassa_id: str) -> Optional[int]:
    """Get the telegram_id of the user who made the payment."""
    async with async_session_factory() as session:
        stmt = (
            select(User.telegram_id)
            .join(Payment, Payment.user_id == User.id)
            .where(Payment.yookassa_payment_id == yookassa_id)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def get_payment_credits(yookassa_id: str) -> Optional[int]:
    """Get the number of credits for a payment."""
    async with async_session_factory() as session:
        stmt = select(Payment.credits).where(Payment.yookassa_payment_id == yookassa_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def _get_bot_username() -> Optional[str]:
    """Try to get bot username for return URL (best-effort)."""
    try:
        from bot_api.bot import bot_app
        if bot_app and bot_app.bot:
            me = await bot_app.bot.get_me()
            return me.username
    except Exception:
        pass
    return None
