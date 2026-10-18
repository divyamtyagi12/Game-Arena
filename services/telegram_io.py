"""Resilient Telegram I/O: retries, rate limits, blocked users, deleted messages."""
from __future__ import annotations

import asyncio
import logging
import time

from telegram import InlineKeyboardMarkup, Message
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut

from services.context import AppContext

log = logging.getLogger(__name__)

DEFAULT_IO_TIMEOUT = 10.0


async def send_message(ctx: AppContext, chat_id: int, text: str, *,
                       reply_markup: InlineKeyboardMarkup | None = None) -> Message | None:
    """Returns None on failure; a blocked user is flagged so the lobby refuses them."""
    for attempt in range(1, 3):
        t0 = time.perf_counter()
        try:
            try:
                msg = await asyncio.wait_for(
                    ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                         reply_markup=reply_markup,
                                         read_timeout=DEFAULT_IO_TIMEOUT,
                                         write_timeout=DEFAULT_IO_TIMEOUT,
                                         connect_timeout=5.0),
                    timeout=DEFAULT_IO_TIMEOUT + 2.0,
                )
            except TypeError:
                msg = await asyncio.wait_for(
                    ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=reply_markup),
                    timeout=DEFAULT_IO_TIMEOUT + 2.0,
                )
            elapsed = time.perf_counter() - t0
            if elapsed > 1.5:
                log.warning("[TG_SLOW] send_message to %s took %.3fs", chat_id, elapsed)
            else:
                log.debug("[TG_API] send_message to %s took %.3fs", chat_id, elapsed)
            return msg
        except RetryAfter as exc:
            elapsed = time.perf_counter() - t0
            log.warning("[TG_429] send_message to %s hit RetryAfter(%.1fs) after %.3fs", chat_id, exc.retry_after, elapsed)
            if exc.retry_after > 5.0 or attempt == 2:
                return None
            await asyncio.sleep(exc.retry_after + 0.2)
        except Forbidden:
            log.warning("Cannot message chat %s (blocked / removed)", chat_id)
            if chat_id > 0:
                await ctx.users.set_dm(chat_id, False)
            return None
        except (TimedOut, NetworkError, asyncio.TimeoutError) as exc:
            elapsed = time.perf_counter() - t0
            log.warning("[TG_NET] send_message to %s timed out (%s) after %.3fs (attempt %d/2)",
                        chat_id, type(exc).__name__, elapsed, attempt)
            if attempt < 2:
                await asyncio.sleep(0.5)
        except TelegramError as exc:
            log.warning("send_message to %s failed: %s", chat_id, exc)
            return None
    return None


async def safe_edit(ctx: AppContext, chat_id: int, message_id: int, text: str, *,
                    reply_markup: InlineKeyboardMarkup | None = None) -> str:
    """Returns 'ok' | 'gone' (message deleted/uneditable) | 'error' (transient)."""
    for attempt in range(1, 3):
        t0 = time.perf_counter()
        try:
            try:
                await asyncio.wait_for(
                    ctx.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                              parse_mode=ParseMode.HTML, reply_markup=reply_markup,
                                              read_timeout=DEFAULT_IO_TIMEOUT,
                                              write_timeout=DEFAULT_IO_TIMEOUT,
                                              connect_timeout=5.0),
                    timeout=DEFAULT_IO_TIMEOUT + 2.0,
                )
            except TypeError:
                await asyncio.wait_for(
                    ctx.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                              parse_mode=ParseMode.HTML, reply_markup=reply_markup),
                    timeout=DEFAULT_IO_TIMEOUT + 2.0,
                )
            elapsed = time.perf_counter() - t0
            if elapsed > 1.5:
                log.warning("[TG_SLOW] edit_message in %s mid=%s took %.3fs", chat_id, message_id, elapsed)
            else:
                log.debug("[TG_API] edit_message in %s mid=%s took %.3fs", chat_id, message_id, elapsed)
            return "ok"
        except RetryAfter as exc:
            elapsed = time.perf_counter() - t0
            log.warning("[TG_429] edit in %s mid=%s hit RetryAfter(%.1fs) after %.3fs", chat_id, message_id, exc.retry_after, elapsed)
            if exc.retry_after > 5.0 or attempt == 2:
                return "error"
            await asyncio.sleep(exc.retry_after + 0.2)
        except BadRequest as exc:
            msg_str = str(exc).lower()
            if "not modified" in msg_str:
                return "ok"
            if "not found" in msg_str or "can't be edited" in msg_str or "message_id_invalid" in msg_str:
                return "gone"
            log.warning("edit in %s failed: %s", chat_id, exc)
            return "error"
        except Forbidden:
            return "gone"
        except (TimedOut, NetworkError, asyncio.TimeoutError) as exc:
            elapsed = time.perf_counter() - t0
            log.warning("[TG_NET] edit in %s mid=%s timed out (%s) after %.3fs (attempt %d/2)",
                        chat_id, message_id, type(exc).__name__, elapsed, attempt)
            if attempt < 2:
                await asyncio.sleep(0.5)
        except TelegramError as exc:
            log.warning("edit in %s failed: %s", chat_id, exc)
            return "error"
    return "error"


async def safe_delete(ctx: AppContext, chat_id: int, message_id: int) -> None:
    try:
        try:
            await asyncio.wait_for(
                ctx.bot.delete_message(chat_id, message_id, read_timeout=5.0, write_timeout=5.0),
                timeout=6.0,
            )
        except TypeError:
            await asyncio.wait_for(
                ctx.bot.delete_message(chat_id, message_id),
                timeout=6.0,
            )
    except (TelegramError, asyncio.TimeoutError):
        pass


async def delete_later(ctx: AppContext, chat_id: int, message_id: int, seconds: float) -> None:
    await asyncio.sleep(seconds)
    await safe_delete(ctx, chat_id, message_id)


async def check_dm(ctx: AppContext, user_id: int) -> bool:
    """Can the bot DM this user right now? (typing action is invisible & harmless)"""
    try:
        try:
            await asyncio.wait_for(
                ctx.bot.send_chat_action(user_id, ChatAction.TYPING, read_timeout=5.0, write_timeout=5.0),
                timeout=6.0,
            )
        except TypeError:
            await asyncio.wait_for(
                ctx.bot.send_chat_action(user_id, ChatAction.TYPING),
                timeout=6.0,
            )
        return True
    except (Forbidden, BadRequest):
        await ctx.users.set_dm(user_id, False)
        return False
    except (TelegramError, asyncio.TimeoutError):
        return True  # transient problem: don't punish the player
