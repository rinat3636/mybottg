"""Inline keyboards for the Telegram bot."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from shared.config import CREDIT_PACKAGES
from shared.config import settings


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🖼️ Редактировать", callback_data="menu_edit"),
                InlineKeyboardButton("🪄 Сгенерировать", callback_data="menu_generate"),
            ],
            [
                InlineKeyboardButton("💎 Баланс", callback_data="menu_balance"),
                InlineKeyboardButton("💰 Пополнить", callback_data="menu_topup"),
            ],
            [InlineKeyboardButton("📚 Примеры промтов", callback_data="menu_examples")],
            [
                InlineKeyboardButton("👥 Рефералка", callback_data="menu_referral"),
            ],
            [InlineKeyboardButton("🆘 Поддержка", callback_data="menu_support")],
        ]
    )


def support_link_keyboard() -> InlineKeyboardMarkup:
    """Open a direct Telegram support link if configured."""
    url = (settings.SUPPORT_TG_URL or "").strip()
    if not url:
        # no-op: return back-to-menu only
        return back_to_menu_keyboard()
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💬 Написать в поддержку", url=url)],
            [InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")],
        ]
    )


def generation_done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔁 Ещё раз", callback_data="gen_again")],
            [InlineKeyboardButton("🆕 Новая генерация", callback_data="gen_new")],
            [InlineKeyboardButton("◀️ В меню", callback_data="back_to_menu")],
        ]
    )


# ---------------------------------------------------------------------------
# Top-up packages
# ---------------------------------------------------------------------------

def topup_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for rub, credits in sorted(CREDIT_PACKAGES.items()):
        buttons.append(
            [
                InlineKeyboardButton(
                    f"💳 {rub}₽ → {credits} кредитов",
                    callback_data=f"topup_{rub}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# Tariff selection for generation
# ---------------------------------------------------------------------------

def tariff_select_keyboard() -> InlineKeyboardMarkup:
    """Deprecated: left for backward compatibility."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🍌 Nano Banana Pro (11 кредитов)",
                    callback_data="gen_tariff_nano_banana_pro",
                ),
            ],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")],
        ]
    )


# ---------------------------------------------------------------------------
# Insufficient funds
# ---------------------------------------------------------------------------

def insufficient_funds_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💰 Пополнить", callback_data="menu_topup")],
            [InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")],
        ]
    )


# ---------------------------------------------------------------------------
# Back to menu
# ---------------------------------------------------------------------------

def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")]]
    )


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")]]
    )


# ---------------------------------------------------------------------------
# Support reply (for admins) — now uses ticket_id
# ---------------------------------------------------------------------------

def support_reply_keyboard(ticket_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✏️ Ответить",
                    callback_data=f"support_reply_{ticket_id}",
                ),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Admin: ban / unban
# ---------------------------------------------------------------------------

def admin_user_keyboard(telegram_id: int, is_banned: bool) -> InlineKeyboardMarkup:
    action = "unban" if is_banned else "ban"
    label = "🔓 Разбанить" if is_banned else "🔒 Забанить"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"admin_{action}_{telegram_id}")]]
    )
