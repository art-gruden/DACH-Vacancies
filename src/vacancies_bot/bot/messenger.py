"""aiogram adapter for the sender + conversion of plain-data keyboards."""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from ..notify.sender import MessengerBlocked, MessengerRetryAfter, MessengerTransient
from ..questionnaire.engine import Button

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def markup(rows: list[list[Button]] | None) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    out = []
    for row in rows:
        btns = []
        for b in row:
            if b.url:
                btns.append(InlineKeyboardButton(text=b.text[:64], url=b.url))
            else:
                btns.append(InlineKeyboardButton(text=b.text[:64], callback_data=(b.data or "noop")[:64]))
        if btns:
            out.append(btns)
    return InlineKeyboardMarkup(inline_keyboard=out) if out else None


class AiogramMessenger:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send(self, chat_id: int, text: str, rows: list[list[Button]] | None = None) -> int:
        try:
            msg = await self.bot.send_message(chat_id, text, reply_markup=markup(rows), link_preview_options=NO_PREVIEW)
            return msg.message_id
        except TelegramForbiddenError as exc:
            raise MessengerBlocked() from exc
        except TelegramRetryAfter as exc:
            raise MessengerRetryAfter(exc.retry_after) from exc
        except TelegramBadRequest as exc:
            if "chat not found" in str(exc).lower():
                raise MessengerBlocked() from exc
            raise
        except TelegramServerError as exc:
            # 5xx: Telegram did not accept the message
            raise MessengerTransient(str(exc)) from exc
        except TelegramNetworkError:
            # the request may or may not have reached Telegram → ambiguous, the sender will not resend
            raise
