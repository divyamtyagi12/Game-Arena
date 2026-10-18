from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from services.context import AppContext
from services.telegram_io import safe_delete, safe_edit, send_message
from utils import messages as msg

log = logging.getLogger(__name__)


def lobby_markup(game_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚔️ JOIN", callback_data=f"lb:join:{game_id}"),
         InlineKeyboardButton("🚪 LEAVE", callback_data=f"lb:leave:{game_id}")],
        [InlineKeyboardButton("🔥 FORCE START", callback_data=f"lb:start:{game_id}")],
    ])


def open_bot_markup(ctx: AppContext) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "🤖 OPEN BOT", url=f"https://t.me/{ctx.bot_username}?start=ready")]])


_last_lobby_refresh: dict[int, float] = {}


async def refresh_lobby(ctx: AppContext, game_id: int, *, repost: bool = False) -> None:
    """Edit the existing lobby message (never spam). Re-posts only if it was deleted."""
    async with ctx.lock(("lobby", game_id)):
        now = asyncio.get_running_loop().time()
        last = _last_lobby_refresh.get(game_id, 0.0)
        # If an edit just happened less than 0.8s ago, pause slightly to let other concurrent joins settle
        if now - last < 0.8 and not repost:
            await asyncio.sleep(0.5)

        game = await ctx.games.get(game_id)
        if not game:
            return
        players = await ctx.players.list_for_game(game_id)
        text = msg.lobby_text(game, players)
        markup = lobby_markup(game_id) if game["status"] == "LOBBY" else None
        old = game.get("lobby_message_id")
        _last_lobby_refresh[game_id] = asyncio.get_running_loop().time()
        if old and not repost:
            if await safe_edit(ctx, game["group_id"], old, text, reply_markup=markup) != "gone":
                return
        sent = await send_message(ctx, game["group_id"], text, reply_markup=markup)
        if sent:
            await ctx.games.update(game_id, lobby_message_id=sent.message_id)
            if old and repost:
                await safe_delete(ctx, game["group_id"], old)


async def cancel_game(ctx: AppContext, game: dict, by_name: str) -> bool:
    if not await ctx.games.cancel(game["id"]):
        return False
    await refresh_lobby(ctx, game["id"])
    await send_message(ctx, game["group_id"], f"❌ Game cancelled by {msg.esc(by_name)}.")
    players = await ctx.players.list_for_game(game["id"])
    await asyncio.gather(*(send_message(
        ctx, p["user_id"], "❌ The ANIME DRAFT game in your group was cancelled.") for p in players),
        return_exceptions=True)
    return True
