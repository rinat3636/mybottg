"""Payment processing with YooKassa — credit packages via payment link.

Key properties:
- Webhook processing is transactional and idempotent.
- Webhook is *fail-closed*: we only accrue credits after verifying payment via YooKassa API.
- Strict amount/currency validation via Decimal.
- Optional reconciliation for stuck pending payments.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from yookassa import Configuration, Payment as YooPayment

from shared.config import settings, CREDIT_PACKAGES
from shared.database import Payment, User, CreditLedger, async_session_factory
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
# Helpers
# ---------------------------------------------------------------------------

def _decimal_amount(value: object) -> Optional[Decimal]:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, TypeError):
        return None


def _extract_amount_currency(payment_obj: dict) -> tuple[Optional[Decimal], str]:
    amount_obj = payment_obj.get("amount", {}) or {}
    dec = _decimal_amount(amount_obj.get("value", "0"))
    cur = str(amount_obj.get("currency", "")).strip().upper()
    return dec, cur


async def _process_succeeded_payment(
    *,
    yookassa_id: str,
    payment_obj: dict,
) -> bool:
    """Internal: apply credits for succeeded payment (transactional + idempotent)."""
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

            # Strict amount/currency validation
            webhook_dec, webhook_cur = _extract_amount_currency(payment_obj)
            if webhook_dec is None:
                logger.warning("Payment %s invalid amount value in webhook", yookassa_id)
                return False

            expected = Decimal(f"{payment.amount_rub}.00")
            if webhook_cur != "RUB" or webhook_dec != expected:
                logger.warning(
                    "Payment %s amount/currency mismatch: webhook=%s %s db=%s RUB",
                    yookassa_id, webhook_dec, webhook_cur, expected,
                )
                return False

            # Extra idempotency: if ledger already contains this payment, just mark succeeded.
            existing = await session.execute(
                select(CreditLedger.id).where(
                    CreditLedger.reason == "payment",
                    CreditLedger.reference_id == yookassa_id,
                )
            )
            if existing.scalar_one_or_none() is not None:
                payment.status = "succeeded"
                payment.paid_at = payment.paid_at or datetime.now(timezone.utc)
                logger.info("Payment %s already credited in ledger (idempotent)", yookassa_id)
                return True

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

    logger.info("Payment %s processed successfully", yookassa_id)
    return True


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
                    "amount": {"value": f"{amount_rub}.00", "currency": "RUB"},
                    "confirmation": {
                        "type": "redirect",
                        "return_url": f"https://t.me/{(await _get_bot_username()) or 'bot'}",
                    },
                    "capture": True,
                    "description": f"Пополнение {credits} кредитов — ComfyUI Bot",
                    "metadata": {"telegram_id": str(telegram_id), "amount_rub": str(amount_rub)},
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
    """Process incoming YooKassa webhook notification (fail-closed).

    We only accrue credits after verifying payment via YooKassa API.
    """
    try:
        event_type = data.get("event")
        payment_obj = data.get("object", {}) or {}
        yookassa_id = payment_obj.get("id")
        status = payment_obj.get("status")

        if event_type != "payment.succeeded" or status != "succeeded":
            logger.info("Ignoring YooKassa event: %s status=%s", event_type, status)
            return False

        if not yookassa_id:
            return False

        # Verify payment via API call for extra security (fail-closed)
        try:
            verified = YooPayment.find_one(yookassa_id)
        except Exception:
            logger.exception("Failed to verify payment %s via API", yookassa_id)
            return False

        if getattr(verified, "status", None) != "succeeded":
            logger.warning("Payment %s not confirmed via API (status=%s)", yookassa_id, getattr(verified, "status", None))
            return False

        # Prefer verified amount/currency if present
        try:
            verified_amount = getattr(getattr(verified, "amount", None), "value", None)
            verified_currency = getattr(getattr(verified, "amount", None), "currency", None)
            if verified_amount is not None and verified_currency is not None:
                payment_obj = dict(payment_obj)
                payment_obj["amount"] = {"value": str(verified_amount), "currency": str(verified_currency)}
        except Exception:
            pass

        return await _process_succeeded_payment(yookassa_id=yookassa_id, payment_obj=payment_obj)

    except Exception:
        logger.exception("Error processing YooKassa webhook")
        return False


async def confirm_payment_and_process(telegram_id: int, yookassa_id: str) -> Optional[bool]:
    """User-initiated fallback: verify payment via API and, if succeeded, process it.

    Returns:
      - True if credits were (or already are) accrued
      - False if payment is not succeeded / mismatch / not user's payment
      - None if payments disabled
    """
    if not settings.YOOKASSA_SHOP_ID or not settings.YOOKASSA_SECRET_KEY:
        return None

    owner_tg = await get_payment_user_telegram_id(yookassa_id)
    if owner_tg is None or owner_tg != telegram_id:
        return False

    try:
        verified = YooPayment.find_one(yookassa_id)
    except Exception:
        logger.exception("Failed to verify payment %s via API (user confirm)", yookassa_id)
        return False

    if getattr(verified, "status", None) != "succeeded":
        return False

    verified_amount = getattr(getattr(verified, "amount", None), "value", None)
    verified_currency = getattr(getattr(verified, "amount", None), "currency", None)

    payment_obj = {
        "id": yookassa_id,
        "status": "succeeded",
        "amount": {
            "value": str(verified_amount) if verified_amount is not None else "0",
            "currency": str(verified_currency) if verified_currency is not None else "RUB",
        },
    }
    return await _process_succeeded_payment(yookassa_id=yookassa_id, payment_obj=payment_obj)


async def reconcile_pending_payments(*, older_than_seconds: int = 600, limit: int = 50) -> int:
    """Best-effort reconciliation: verify and process old pending payments.

    Returns number of payments that were processed (succeeded).
    """
    if not settings.YOOKASSA_SHOP_ID or not settings.YOOKASSA_SECRET_KEY:
        return 0

    cutoff_ts = datetime.now(timezone.utc).timestamp() - older_than_seconds

    async with async_session_factory() as session:
        rows = await session.execute(
            select(Payment)
            .where(Payment.status == "pending")
            .order_by(Payment.created_at.asc())
            .limit(limit)
        )
        pending = rows.scalars().all()

    processed = 0
    for pay in pending:
        try:
            if not pay.yookassa_payment_id:
                continue
            if pay.created_at and pay.created_at.timestamp() > cutoff_ts:
                continue

            verified = YooPayment.find_one(pay.yookassa_payment_id)
            if getattr(verified, "status", None) != "succeeded":
                continue

            verified_amount = getattr(getattr(verified, "amount", None), "value", None)
            verified_currency = getattr(getattr(verified, "amount", None), "currency", None)

            payment_obj = {
                "id": pay.yookassa_payment_id,
                "status": "succeeded",
                "amount": {
                    "value": str(verified_amount) if verified_amount is not None else "0",
                    "currency": str(verified_currency) if verified_currency is not None else "RUB",
                },
            }
            ok = await _process_succeeded_payment(yookassa_id=pay.yookassa_payment_id, payment_obj=payment_obj)
            if ok:
                processed += 1
        except Exception:
            logger.exception("Reconcile failed for payment_id=%s", pay.yookassa_payment_id)
            continue

    return processed


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
