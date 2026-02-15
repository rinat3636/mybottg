"""Prompt examples screen.

Goal: make the bot feel "modern" — short, guided, and copy‑paste friendly.
We store the selected example prompt into Redis so the user can immediately
press "Generate" without manually retyping.
"""

from __future__ import annotations

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot_api.keyboards import back_to_menu_keyboard
from shared.redis_client import update_user_data, set_user_state


EXAMPLES: dict[str, dict[str, str]] = {
    "portrait": {
        "title": "👤 Портрет",
        "prompt": "Сделай портрет в стиле кинопостера, мягкий свет, чёткое лицо, натуральная кожа, лёгкая глубина резкости, 3:4",
    },
    "product": {
        "title": "🛍️ Товарка",
        "prompt": "Сделай предметную фотосъёмку продукта на чистом фоне, мягкие тени, детализация, коммерческий стиль, 1:1",
    },
    "style": {
        "title": "🎨 Стиль",
        "prompt": "Перерисуй в стиле аниме, сохрани лицо, чистые линии, яркие цвета, аккуратные детали, 1:1",
    },
    "bg": {
        "title": "🌆 Фон",
        "prompt": "Замени фон на ночной город с неоном, сохрани человека и лицо, реалистично, 9:16",
    },
}


def _examples_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, item in EXAMPLES.items():
        rows.append([InlineKeyboardButton(item["title"], callback_data=f"ex_{key}")])
    rows.append([InlineKeyboardButton("◀️ В меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(rows)


def _use_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Использовать этот промт", callback_data=f"ex_use_{key}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="menu_examples")],
        ]
    )


async def examples_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    await query.edit_message_text(
        "📚 *Примеры промтов*\n\n"
        "Выберите пример → я положу промт в буфер.\n"
        "Потом можно сразу нажать «Сгенерировать» (без фото) или отправить фото для редактирования.",
        parse_mode="Markdown",
        reply_markup=_examples_keyboard(),
    )


async def example_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    key = query.data.replace("ex_", "", 1)
    item = EXAMPLES.get(key)
    if not item:
        await query.edit_message_text("Не нашёл пример 🙃", reply_markup=back_to_menu_keyboard())
        return

    await query.edit_message_text(
        f"{item['title']}\n\n"
        f"`{item['prompt']}`\n\n"
        "Нажмите «Использовать», и промт будет готов к генерации.",
        parse_mode="Markdown",
        reply_markup=_use_keyboard(key),
    )


async def example_use_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    key = query.data.replace("ex_use_", "", 1)
    item = EXAMPLES.get(key)
    if not item:
        await query.edit_message_text("Не нашёл пример 🙃", reply_markup=back_to_menu_keyboard())
        return

    telegram_id = query.from_user.id

    # Put prompt into the user's buffer and move to "generation" flow.
    # User can now press "Generate" button or send a photo.
    await update_user_data(telegram_id, prompt=item["prompt"], aspect_ratio=None)
    await set_user_state(telegram_id, "waiting_for_generation")

    await query.edit_message_text(
        "✅ Готово! Промт сохранён.\n\n"
        "Дальше так:\n"
        "• хотите *сгенерировать без фото* → нажмите «Сгенерировать» в меню\n"
        "• хотите *редактировать* → отправьте фото (можно альбом до 8).",
        parse_mode="Markdown",
        reply_markup=back_to_menu_keyboard(),
    )
