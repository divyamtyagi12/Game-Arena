"""Character artwork — isolated so assets can be swapped without touching game logic.

A character may have a Telegram `image_file_id` (preferred, cached) or an `image_url`.
Artwork is never scraped at runtime; after the first successful send the file_id is cached.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Sequence

from telegram import InputMediaPhoto
from telegram.constants import ParseMode
from telegram.error import TelegramError

from models.character import Character
from services.context import AppContext
from utils.messages import esc

log = logging.getLogger(__name__)


def photo_ref(char: Character) -> str | None:
    return char.image_file_id or char.image_url


async def send_album(ctx: AppContext, chat_id: int, chars: Sequence[Character]) -> bool:
    """Send an album of the offered characters, only if ALL have artwork."""
    refs = [photo_ref(c) for c in chars]
    if len(chars) < 2 or not all(refs):
        return False
    media = [InputMediaPhoto(media=ref, caption=f"{i}. {esc(c.name)}", parse_mode=ParseMode.HTML)
             for i, (c, ref) in enumerate(zip(chars, refs), 1)]
    try:
        msgs = await asyncio.wait_for(
            ctx.bot.send_media_group(chat_id, media, read_timeout=8.0, write_timeout=8.0),
            timeout=10.0,
        )
    except (TelegramError, asyncio.TimeoutError) as exc:
        log.warning("Album send to %s failed (%s); falling back to text", chat_id, exc)
        return False
    for c, m in zip(chars, msgs):
        if m.photo and not c.image_file_id:
            c.image_file_id = m.photo[-1].file_id
            try:
                await ctx.characters.set_file_id(c.id, c.image_file_id)
            except Exception:  # caching is best effort
                log.debug("could not cache file_id for %s", c.id)
    return True
