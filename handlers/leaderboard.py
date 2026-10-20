"""/leaderboard and the `lbd:` inline-keyboard callbacks.

callback_data (all < 64 bytes, stateless → survives restarts):
    lbd:menu                       main menu
    lbd:home                       back to the welcome screen (DM) / close (group)
    lbd:g:<page>                   global board
    lbd:pick                       group board for this chat, or the group picker in DM
    lbd:grp:<group_id>:<crit>:<page>   crit = champs | wins | winrate
    lbd:again                      new lobby from the championship message
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from game.rating import MIN_MATCHES_FOR_WIN_RATE, PAGE_SIZE
from handlers import arena
from handlers.common import get_ctx, safe_answer
from handlers.start import GROUP_TYPES, dm_welcome_markup
from services import lobby_service
from services.telegram_io import safe_delete, safe_edit
from utils import messages as msg

log = logging.getLogger(__name__)
CRITERIA = ("champs", "wins", "winrate")


def menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [Btn("⚔️ ANIME DRAFT", callback_data="lbd:ad")],
        [Btn("🔢 NUMBER WARS", callback_data="nwl:g")],
        [Btn("🏏 IPL DRAFT", callback_data="ipl:lb:g:0")],
        [Btn("🏠 BACK", callback_data="lbd:home")],
    ])


def anime_draft_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [Btn("🌍 GLOBAL LEADERBOARD", callback_data="lbd:g:0")],
        [Btn("👥 GROUP LEADERBOARD", callback_data="lbd:pick")],
        [Btn("🔙 BACK", callback_data="lbd:menu")],
    ])


def global_markup(page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [Btn("◀️ PREVIOUS", callback_data=f"lbd:g:{max(page - 1, 0)}"),
         Btn("NEXT ▶️", callback_data=f"lbd:g:{page + 1}")],
        [Btn("👥 GROUP", callback_data="lbd:pick"), Btn("🔄 REFRESH", callback_data=f"lbd:g:{page}")],
        [Btn("🔙 BACK", callback_data="lbd:ad")],
    ])


def group_markup(group_id: int, crit: str, page: int) -> InlineKeyboardMarkup:
    def label(key: str, text: str) -> str:
        return f"✅ {text}" if key == crit else text
    return InlineKeyboardMarkup([
        [Btn(label("champs", "🏆 CHAMPIONSHIPS"), callback_data=f"lbd:grp:{group_id}:champs:0")],
        [Btn(label("wins", "⚔️ TOTAL WINS"), callback_data=f"lbd:grp:{group_id}:wins:0")],
        [Btn(label("winrate", "📊 WIN RATE"), callback_data=f"lbd:grp:{group_id}:winrate:0")],
        [Btn("◀️", callback_data=f"lbd:grp:{group_id}:{crit}:{max(page - 1, 0)}"),
         Btn("▶️", callback_data=f"lbd:grp:{group_id}:{crit}:{page + 1}")],
        [Btn("🌍 GLOBAL", callback_data="lbd:g:0"), Btn("🔙 BACK", callback_data="lbd:ad")],
    ])


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message and update.effective_chat.type in (ChatType.PRIVATE, *GROUP_TYPES):
        await update.effective_message.reply_html(msg.leaderboard_menu_text(), reply_markup=menu_markup())


async def _show(ctx, q, text: str, markup: InlineKeyboardMarkup) -> None:
    await safe_edit(ctx, q.message.chat.id, q.message.message_id, text, reply_markup=markup)


async def _show_global(ctx, q, page: int) -> None:
    page = max(page, 0)
    data = await ctx.leaderboard.global_page(q.from_user.id, PAGE_SIZE, page * PAGE_SIZE)
    total = int(data.get("total") or 0)
    last = max(0, -(-total // PAGE_SIZE) - 1)
    if page > last:  # e.g. NEXT on the last page
        await safe_answer(q, "That's the last page.")
        return
    await safe_answer(q)
    await _show(ctx, q, msg.global_leaderboard_text(data, page, PAGE_SIZE), global_markup(page))


async def _show_group(ctx, q, group_id: int, crit: str, page: int) -> None:
    chat = q.message.chat
    if chat.type in GROUP_TYPES:
        if chat.id != group_id:  # a group chat can only show its own board
            return await safe_answer(q, "⚠️ This button belongs to another group.", True)
    elif not await ctx.leaderboard.has_group_stats(q.from_user.id, group_id):
        return await safe_answer(q, "⚠️ You haven't played in that group.", True)
    crit = crit if crit in CRITERIA else "champs"
    page = max(page, 0)
    data = await ctx.leaderboard.group_page(group_id, crit, q.from_user.id, PAGE_SIZE,
                                            page * PAGE_SIZE, MIN_MATCHES_FOR_WIN_RATE)
    total = int(data.get("total") or 0)
    if page > max(0, -(-total // PAGE_SIZE) - 1):
        return await safe_answer(q, "That's the last page.")
    await safe_answer(q)
    await _show(ctx, q, msg.group_leaderboard_text(data, crit, page, PAGE_SIZE, MIN_MATCHES_FOR_WIN_RATE),
                group_markup(group_id, crit, page))


async def _pick(ctx, q) -> None:
    chat = q.message.chat
    if chat.type in GROUP_TYPES:
        return await _show_group(ctx, q, chat.id, "champs", 0)
    groups = await ctx.leaderboard.groups_of_user(q.from_user.id)
    await safe_answer(q)
    rows = [[Btn(f"📍 {(g['title'] or 'Group')[:40]}", callback_data=f"lbd:grp:{g['group_id']}:champs:0")]
            for g in groups]
    rows.append([Btn("🔙 BACK", callback_data="lbd:ad")])
    await _show(ctx, q, msg.group_picker_text(bool(groups)), InlineKeyboardMarkup(rows))


async def _play_again(ctx, q) -> None:
    chat, user = q.message.chat, q.from_user
    if chat.type not in GROUP_TYPES:
        return await safe_answer(q, "🎮 Start new games from a group with /start.", True)
    if await arena.active_game_exists(ctx, chat.id):
        return await safe_answer(q, "⚠️ A game is already in progress here. Use /status.", True)
    await ctx.groups.upsert(chat.id, chat.title)
    await ctx.users.upsert(user.id, user.username, user.first_name)
    await safe_answer(q)
    await arena.show_picker(ctx, chat.id, user)


async def leaderboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = get_ctx(context)
    q = update.callback_query
    if not q.message:
        return await safe_answer(q, "⚠️ This button is no longer active.", True)
    parts = (q.data or "").split(":")
    try:
        action = parts[1]
        if action == "menu":
            await safe_answer(q)
            await _show(ctx, q, msg.leaderboard_menu_text(), menu_markup())
        elif action == "ad":
            await safe_answer(q)
            await _show(ctx, q, msg.anime_draft_leaderboard_menu_text(), anime_draft_menu_markup())
        elif action == "home":
            await safe_answer(q)
            if q.message.chat.type == ChatType.PRIVATE:
                await _show(ctx, q, msg.welcome_dm(), dm_welcome_markup(ctx))
            else:
                await safe_delete(ctx, q.message.chat.id, q.message.message_id)
        elif action == "g":
            await _show_global(ctx, q, int(parts[2]))
        elif action == "pick":
            await _pick(ctx, q)
        elif action == "grp":
            await _show_group(ctx, q, int(parts[2]), parts[3], int(parts[4]))
        elif action == "again":
            await _play_again(ctx, q)
        else:
            await safe_answer(q, "⚠️ Invalid button.")
    except (IndexError, ValueError):
        await safe_answer(q, "⚠️ Invalid button.")
