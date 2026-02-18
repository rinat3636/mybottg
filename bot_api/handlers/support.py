"""Support system handlers with ticket_id.

Each support message gets a unique ticket_id.
Admins reply via /reply_TICKET_ID or inline button.
Ticket_id ensures the reply goes to the correct user.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from bot_api.keyboards import (
    cancel_keyboard,
    back_to_menu_keyboard,
    support_reply_keyboard,
    main_menu_keyboard,
    support_link_keyboard,
)
from services.user_service import get_all_admins, get_user_by_telegram_id
from shared.database import SupportMessage, User, async_session_factory
from shared.redis_client import (
    get_user_state,
    set_user_state,
    clear_user_state,
)
from shared.errors import log_exception, generate_trace_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# User: start support flow
# ---------------------------------------------------------------------------

async def support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User pressed 'Поддержка' button."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    # If SUPPORT_TG_URL is configured, show direct link.
    from shared.config import settings
    if (settings.SUPPORT_TG_URL or "").strip():
        await clear_user_state(query.from_user.id)
        await query.edit_message_text(
            "🆘 *Поддержка*\n\nНажмите кнопку ниже, чтобы написать в поддержку.",
            parse_mode="Markdown",
            reply_markup=support_link_keyboard(),
        )
        return

    # Fallback: ticket-based support
    telegram_id = query.from_user.id
    await set_user_state(telegram_id, "waiting_for_support_message")

    await query.edit_message_text(
        "🆘 *Поддержка*\n\n"
        "Напишите ваше сообщение, и мы ответим как можно скорее.",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard(),
    )


# ---------------------------------------------------------------------------
# User: send support message
# ---------------------------------------------------------------------------

async def support_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User sends a support message."""
    if not update.message or not update.effective_user:
        return

    trace_id = generate_trace_id()

    try:
        telegram_id = update.effective_user.id
        text = update.message.text.strip() if update.message.text else ""

        if not text:
            await update.message.reply_text("Пожалуйста, отправьте текстовое сообщение.")
            return

        user = await get_user_by_telegram_id(telegram_id)
        if not user:
            await update.message.reply_text("Нажмите /start для начала работы.")
            return

        # Save to DB (ticket_id is auto-generated)
        async with async_session_factory() as session:
            msg = SupportMessage(
                user_id=user.id,
                message_text=text,
            )
            session.add(msg)
            await session.commit()
            await session.refresh(msg)
            ticket_id = msg.ticket_id

        await clear_user_state(telegram_id)

        await update.message.reply_text(
            f"✅ Ваше обращение #{ticket_id} отправлено в поддержку.\n"
            "Мы ответим вам в ближайшее время!",
            reply_markup=main_menu_keyboard(),
        )

        # Forward to all admins
        admins = await get_all_admins()
        user_display = f"@{user.username}" if user.username else f"id:{telegram_id}"
        for admin in admins:
            try:
                await context.bot.send_message(
                    chat_id=admin.telegram_id,
                    text=(
                        f"🆘 *Новое обращение в поддержку*\n\n"
                        f"🎫 Тикет: `#{ticket_id}`\n"
                        f"От: {user_display} ({user.first_name or '—'})\n"
                        f"Telegram ID: `{telegram_id}`\n\n"
                        f"Сообщение:\n{text}\n\n"
                        f"Ответить: `/reply_{ticket_id} текст ответа`"
                    ),
                    parse_mode="Markdown",
                    reply_markup=support_reply_keyboard(ticket_id),
                )
            except Exception:
                logger.exception("Failed to notify admin %s", admin.telegram_id)

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="support_message_handler")
        await update.message.reply_text("Произошла ошибка, попробуйте позже.")


# ---------------------------------------------------------------------------
# Admin: press reply button (inline)
# ---------------------------------------------------------------------------

async def support_reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin presses 'Ответить' on a support message."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    admin_tg_id = query.from_user.id
    admin = await get_user_by_telegram_id(admin_tg_id)
    if not admin or not admin.is_admin:
        await query.answer("У вас нет прав администратора.", show_alert=True)
        return

    # support_reply_ABCD1234
    ticket_id = query.data.replace("support_reply_", "", 1)

    await set_user_state(admin_tg_id, f"support_reply_{ticket_id}")
    await query.edit_message_text(
        f"✏️ Введите ответ на обращение #{ticket_id}:",
        reply_markup=cancel_keyboard(),
    )


# ---------------------------------------------------------------------------
# Admin: /reply_TICKET_ID text — command-based reply
# ---------------------------------------------------------------------------

async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reply_TICKET_ID <text> command from admin."""
    if not update.message or not update.effective_user:
        return

    trace_id = generate_trace_id()

    try:
        admin_tg_id = update.effective_user.id
        admin = await get_user_by_telegram_id(admin_tg_id)
        if not admin or not admin.is_admin:
            await update.message.reply_text("🚫 У вас нет прав администратора.")
            return

        # Parse: /reply_ABCD1234 some reply text
        raw_text = update.message.text or ""
        match = re.match(r"^/reply_([A-Za-z0-9]+)\s+(.+)$", raw_text, re.DOTALL)
        if not match:
            await update.message.reply_text(
                "Формат: `/reply_TICKET_ID текст ответа`",
                parse_mode="Markdown",
            )
            return

        ticket_id = match.group(1).upper()
        reply_text = match.group(2).strip()

        if not reply_text:
            await update.message.reply_text("Пожалуйста, введите текст ответа.")
            return

        success = await _send_reply_by_ticket(ticket_id, reply_text, context)

        if success:
            await update.message.reply_text(f"✅ Ответ на тикет #{ticket_id} отправлен пользователю.")
        else:
            await update.message.reply_text(f"❌ Тикет #{ticket_id} не найден.")

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="reply_command")
        await update.message.reply_text("Произошла ошибка, попробуйте позже.")


# ---------------------------------------------------------------------------
# Admin: type reply text (inline button flow)
# ---------------------------------------------------------------------------

async def support_reply_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin sends reply text for a support message (inline button flow)."""
    if not update.message or not update.effective_user:
        return

    trace_id = generate_trace_id()

    try:
        admin_tg_id = update.effective_user.id
        state = await get_user_state(admin_tg_id)

        if not state or not state.startswith("support_reply_"):
            return

        # support_reply_ABCD1234
        ticket_id = state.replace("support_reply_", "", 1)
        reply_text = update.message.text.strip() if update.message.text else ""

        if not reply_text:
            await update.message.reply_text("Пожалуйста, введите текст ответа.")
            return

        await clear_user_state(admin_tg_id)

        success = await _send_reply_by_ticket(ticket_id, reply_text, context)

        if success:
            await update.message.reply_text(f"✅ Ответ на тикет #{ticket_id} отправлен пользователю.")
        else:
            await update.message.reply_text(f"❌ Тикет #{ticket_id} не найден.")

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="support_reply_text_handler")
        await update.message.reply_text("Произошла ошибка, попробуйте позже.")


# ---------------------------------------------------------------------------
# Internal: send reply by ticket_id
# ---------------------------------------------------------------------------

async def _send_reply_by_ticket(
    ticket_id: str,
    reply_text: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Find support message by ticket_id, save reply, notify user.

    Returns True on success, False if ticket not found.
    """
    async with async_session_factory() as session:
        stmt = select(SupportMessage).where(SupportMessage.ticket_id == ticket_id)
        result = await session.execute(stmt)
        msg = result.scalar_one_or_none()

        if not msg:
            return False

        msg.admin_reply = reply_text
        msg.replied_at = datetime.now(timezone.utc)
        await session.commit()

        # Get user telegram_id
        user_stmt = select(User).where(User.id == msg.user_id)
        user_result = await session.execute(user_stmt)
        user = user_result.scalar_one_or_none()

    if user:
        try:
            await context.bot.send_message(
                chat_id=user.telegram_id,
                text=(
                    f"💬 *Ответ от поддержки* (тикет #{ticket_id}):\n\n{reply_text}"
                ),
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
        except Exception:
            logger.exception("Failed to send support reply to user %s", user.telegram_id)

    return True
