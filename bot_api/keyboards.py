"""Inline keyboards for the Telegram bot."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from shared.config import settings


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

def main_menu_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    """Main menu — 3 core features + admin pod control."""
    buttons = [
        [InlineKeyboardButton("🖼️ Редактировать фото", callback_data="menu_edit_photo")],
        [InlineKeyboardButton("🎬 Оживить фото (видео 10 сек)", callback_data="menu_animate_photo")],
        [InlineKeyboardButton("🧙 Создать изображение", callback_data="menu_generate")],
        [InlineKeyboardButton("💬 Поддержка", callback_data="menu_support")],
    ]
    if is_admin:
        buttons.append([
            InlineKeyboardButton("🖥 Управление подом RunPod", callback_data="menu_pod_control"),
        ])
    return InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# Pod control keyboard (admin only)
# ---------------------------------------------------------------------------

def pod_control_keyboard(is_running: bool = False) -> InlineKeyboardMarkup:
    """Keyboard for RunPod pod management."""
    buttons = []
    if is_running:
        buttons.append([InlineKeyboardButton("⏹ Остановить под", callback_data="pod_stop")])
    else:
        buttons.append([InlineKeyboardButton("▶️ Запустить под", callback_data="pod_start")])
    buttons.append([InlineKeyboardButton("🔄 Обновить статус", callback_data="pod_status")])
    buttons.append([InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# Support
# ---------------------------------------------------------------------------

def support_link_keyboard() -> InlineKeyboardMarkup:
    """Open a direct Telegram support link if configured."""
    url = (settings.SUPPORT_TG_URL or "").strip()
    if not url:
        return back_to_menu_keyboard()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Написать в поддержку", url=url)],
        [InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")],
    ])


# ---------------------------------------------------------------------------
# Back to menu / Cancel
# ---------------------------------------------------------------------------

def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")]]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")]]
    )


# ---------------------------------------------------------------------------
# Generation done
# ---------------------------------------------------------------------------

def generation_done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Ещё раз", callback_data="gen_again")],
        [InlineKeyboardButton("◀️ Главное меню", callback_data="back_to_menu")],
    ])


# ---------------------------------------------------------------------------
# Admin: ban / unban
# ---------------------------------------------------------------------------

def admin_user_keyboard(telegram_id: int, is_banned: bool) -> InlineKeyboardMarkup:
    action = "unban" if is_banned else "ban"
    label = "🔓 Разбанить" if is_banned else "🔒 Забанить"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"admin_{action}_{telegram_id}")]]
    )


# ---------------------------------------------------------------------------
# Support reply (for admins)
# ---------------------------------------------------------------------------

def support_reply_keyboard(ticket_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Ответить", callback_data=f"support_reply_{ticket_id}")],
    ])
