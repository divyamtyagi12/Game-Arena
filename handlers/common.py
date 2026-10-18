from __future__ import annotations

import asyncio
import logging

from telegram import Bot
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from services.context import AppContext

log = logging.getLogger(__name__)


def get_ctx(context: ContextTypes.DEFAULT_TYPE) -> AppContext:
    return context.bot_data["ctx"]


async def safe_answer(query, text: str | None = None, alert: bool = False) -> None:
    """Every callback query must be acknowledged; never let that fail a handler."""
    try:
        await asyncio.wait_for(
            query.answer(text=text, show_alert=alert),
            timeout=4.0,
        )
    except (TelegramError, asyncio.TimeoutError) as exc:
        log.debug("answer_callback_query failed: %s", exc)


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await asyncio.wait_for(
            bot.get_chat_member(chat_id, user_id, read_timeout=5.0),
            timeout=6.0,
        )
        return member.status in ("administrator", "creator")
    except (TelegramError, asyncio.TimeoutError):
        return False
