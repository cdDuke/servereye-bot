"""Общие помощники для хендлеров."""
from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

log = logging.getLogger(__name__)


async def edit(
    call: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_preview: bool = True,
) -> None:
    """Безопасное редактирование: игнорируем «message is not modified»
    и корректно работаем, если предыдущим сообщением было фото."""
    message = call.message
    if message is None:
        return
    try:
        if message.photo or message.document:
            await message.delete()
            await message.answer(
                text, reply_markup=reply_markup, disable_web_page_preview=disable_preview
            )
            return
        await message.edit_text(
            text, reply_markup=reply_markup, disable_web_page_preview=disable_preview
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        try:
            await message.answer(
                text, reply_markup=reply_markup, disable_web_page_preview=disable_preview
            )
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить сообщение")


async def answer_or_edit(event: Message | CallbackQuery, text: str, reply_markup=None) -> None:
    if isinstance(event, CallbackQuery):
        await edit(event, text, reply_markup)
    else:
        await event.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)


def parse_cb(data: str) -> list[str]:
    return data.split(":")
