"""Admin commands: /stats, /addadmin, /removeadmin, /ban, /unban, /broadcast.

All admin commands use shared.admin_guard.is_admin_user for authorization.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, func
from telegram import Update
from telegram.ext import ContextTypes

from bot_api.keyboards import main_menu_keyboard
from services.user_service import (
    get_stats,
    set_admin,
    set_banned,
    get_user_by_telegram_id,
)
from shared.admin_guard import is_admin_user
from shared.database import User, async_session_factory
from shared.errors import log_exception, generate_trace_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show bot statistics (admin only)."""
    if not update.message or not update.effective_user:
        return

    trace_id = generate_trace_id()

    try:
        if not await is_admin_user(update.effective_user.id):
            await update.message.reply_text("🚫 У вас нет прав администратора.")
            return

        stats = await get_stats()

        await update.message.reply_text(
            "📊 *Статистика бота*\n\n"
            f"👥 Пользователей: {stats['total_users']}\n"
            f"🎨 Генераций: {stats['total_generations']}\n"
            f"💰 Выручка: {stats['total_revenue']}₽",
            parse_mode="Markdown",
        )
    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="stats_command")
        await update.message.reply_text("Произошла ошибка.")


# ---------------------------------------------------------------------------
# /addadmin <telegram_id>
# ---------------------------------------------------------------------------

async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add admin by telegram_id."""
    if not update.message or not update.effective_user:
        return

    if not await is_admin_user(update.effective_user.id):
        await update.message.reply_text("🚫 У вас нет прав администратора.")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Формат: /addadmin <telegram_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный Telegram ID.")
        return

    ok = await set_admin(target_id, True)
    if ok:
        await update.message.reply_text(f"✅ Пользователь {target_id} назначен администратором.")
    else:
        await update.message.reply_text(f"❌ Пользователь {target_id} не найден.")


# ---------------------------------------------------------------------------
# /removeadmin <telegram_id>
# ---------------------------------------------------------------------------

async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove admin by telegram_id."""
    if not update.message or not update.effective_user:
        return

    if not await is_admin_user(update.effective_user.id):
        await update.message.reply_text("🚫 У вас нет прав администратора.")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Формат: /removeadmin <telegram_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный Telegram ID.")
        return

    ok = await set_admin(target_id, False)
    if ok:
        await update.message.reply_text(f"✅ Пользователь {target_id} снят с роли администратора.")
    else:
        await update.message.reply_text(f"❌ Пользователь {target_id} не найден.")


# ---------------------------------------------------------------------------
# /ban <telegram_id>
# ---------------------------------------------------------------------------

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ban a user by telegram_id."""
    if not update.message or not update.effective_user:
        return

    if not await is_admin_user(update.effective_user.id):
        await update.message.reply_text("🚫 У вас нет прав администратора.")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Формат: /ban <telegram_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный Telegram ID.")
        return

    ok = await set_banned(target_id, True)
    if ok:
        await update.message.reply_text(f"🔒 Пользователь {target_id} заблокирован.")
    else:
        await update.message.reply_text(f"❌ Пользователь {target_id} не найден.")


# ---------------------------------------------------------------------------
# /unban <telegram_id>
# ---------------------------------------------------------------------------

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unban a user by telegram_id."""
    if not update.message or not update.effective_user:
        return

    if not await is_admin_user(update.effective_user.id):
        await update.message.reply_text("🚫 У вас нет прав администратора.")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Формат: /unban <telegram_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный Telegram ID.")
        return

    ok = await set_banned(target_id, False)
    if ok:
        await update.message.reply_text(f"🔓 Пользователь {target_id} разблокирован.")
    else:
        await update.message.reply_text(f"❌ Пользователь {target_id} не найден.")


# ---------------------------------------------------------------------------
# /broadcast <text> — send message to all users
# ---------------------------------------------------------------------------

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Broadcast a message to all users (admin only)."""
    if not update.message or not update.effective_user:
        return

    trace_id = generate_trace_id()

    try:
        if not await is_admin_user(update.effective_user.id):
            await update.message.reply_text("🚫 У вас нет прав администратора.")
            return

        raw_text = update.message.text or ""
        # /broadcast some text
        parts = raw_text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await update.message.reply_text("Формат: /broadcast <текст сообщения>")
            return

        broadcast_text = parts[1].strip()

        # Get all non-banned users
        async with async_session_factory() as session:
            stmt = select(User.telegram_id).where(User.is_banned == False)  # noqa: E712
            result = await session.execute(stmt)
            user_ids = [row[0] for row in result.all()]

        sent = 0
        failed = 0
        for uid in user_ids:
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"📢 *Объявление:*\n\n{broadcast_text}",
                    parse_mode="Markdown",
                )
                sent += 1
            except Exception:
                failed += 1

        await update.message.reply_text(
            f"📢 Рассылка завершена.\n"
            f"✅ Отправлено: {sent}\n"
            f"❌ Не доставлено: {failed}",
        )

    except Exception as exc:
        log_exception(exc, trace_id=trace_id, context="broadcast_command")
        await update.message.reply_text("Произошла ошибка при рассылке.")
