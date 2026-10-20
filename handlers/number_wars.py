"""NUMBER WARS Telegram handlers.

callback_data (all < 64 bytes, stateless):
    nw:j:<match>  join        nw:lv:<match>  leave       nw:st:<match>  start (host/admin)
    nw:x              dismiss a confirmation prompt
    nwl:g | nwl:c | nwl:me    leaderboard (global / this group) and personal stats
"""
from __future__ import annotations

import logging
import re
import time

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from game.number_wars import NUMBER_MAX, NUMBER_MIN
from handlers.common import get_ctx, is_admin, safe_answer
from handlers.lobby import _display_name
from services import lobby_service, number_wars_service as svc
from services.context import AppContext
from services.telegram_io import delete_later, safe_edit, send_message
from utils import messages as msg
from utils import nw_messages as nm

log = logging.getLogger(__name__)
GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)


def _throttled(ctx: AppContext, user_id: int, key: str, gap: float) -> bool:
    """Tiny per-user rate limit for button spam (in-memory is fine: it only protects the bot)."""
    now = time.monotonic()
    last = ctx.nw_throttle.get((user_id, key), 0.0)
    if now - last < gap:
        return True
    if len(ctx.nw_throttle) > 5000:
        ctx.nw_throttle.clear()
    ctx.nw_throttle[(user_id, key)] = now
    return False


# ───────────────────────── callbacks ─────────────────────────
async def nw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = get_ctx(context)
    q = update.callback_query
    parts = (q.data or "").split(":")
    try:
        action = parts[1]
        if action in ("j", "lv", "st"):
            return await _lobby_action(ctx, context, q, action, int(parts[2]))
        if action == "x":
            await safe_answer(q)
            try:
                await q.message.delete()
            except Exception:  # noqa: BLE001 - message may already be gone
                pass
            return
    except (IndexError, ValueError):
        pass
    await safe_answer(q, "⚠️ Invalid button.")


async def _lobby_action(ctx: AppContext, context, q, action: str, match_id: int) -> None:
    match = await ctx.nw.get_match(match_id)
    if not match or not q.message or q.message.chat.id != match["group_id"]:
        return await safe_answer(q, "⚠️ This lobby is no longer active.", True)
    if match["status"] != "LOBBY":
        return await safe_answer(q, "⚠️ This lobby is closed.", True)
    user = q.from_user

    if action == "j":
        await ctx.users.upsert(user.id, user.username, user.first_name)
        row = await ctx.users.get(user.id)
        if not row or not row["dm_started"]:
            await safe_answer(q, msg.dm_prompt_text(), True)
            sent = await send_message(
                ctx, match["group_id"],
                f'<a href="tg://user?id={user.id}">{msg.esc(user.first_name)}</a>, {msg.dm_prompt_text()}',
                reply_markup=lobby_service.open_bot_markup(ctx))
            if sent:
                ctx.spawn(delete_later(ctx, match["group_id"], sent.message_id, 45))
            return
        players = await ctx.nw.players(match_id)
        res = await ctx.nw.join(match_id, user.id, _display_name(user, [p["display_name"] for p in players]))
        texts = {"OK": "🔢 You joined Number Wars!", "ALREADY": "✅ You've already joined.",
                 "FULL": "🚫 The lobby is full.", "CLOSED": "⚠️ This lobby is closed.",
                 "OTHER_MATCH": "⚠️ You're already in an active Number Wars match in another group!"}
        await safe_answer(q, texts.get(res, "⚠️ Could not join."), res != "OK")
        if res == "OK":
            await svc.refresh_lobby(ctx, match_id)
            if len(players) + 1 >= match["min_players"]:
                svc.arm_countdown(ctx, match_id)

    elif action == "lv":
        res = await ctx.nw.leave(match_id, user.id)
        texts = {"OK": "🚪 You left the lobby.", "NOT_IN": "You're not in this game.",
                 "CLOSED": "⚠️ This lobby is closed."}
        await safe_answer(q, texts.get(res, "⚠️ Could not leave."))
        if res == "OK":
            await svc.refresh_lobby(ctx, match_id)

    else:  # start
        if user.id != match["host_id"] and not await is_admin(context.bot, match["group_id"], user.id):
            return await safe_answer(q, "❌ Only the host/admin can start the game.", True)
        if len(await ctx.nw.players(match_id)) < match["min_players"]:
            return await safe_answer(q, f"❌ Need at least {match['min_players']} players.", True)
        await safe_answer(q, "🔥 Starting…")
        await svc.start_match(ctx, match_id)



# ───────────────────────── typed numbers (DM) ─────────────────────────
async def number_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A private text that is a whole number during an open round.
    Locks in the player's choice directly."""
    ctx = get_ctx(context)
    message, user = update.effective_message, update.effective_user
    text = (message.text or "").strip()
    if not user or not re.fullmatch(r"-?\d{1,6}", text):
        return
    if _throttled(ctx, user.id, "t", 0.5):
        return
    round_id = await ctx.nw.open_round_for_user(user.id)
    if not round_id:
        return
    n = int(text)
    if not NUMBER_MIN <= n <= NUMBER_MAX:
        return await message.reply_html(f"⚠️ Pick a whole number from <b>{NUMBER_MIN}</b> to <b>{NUMBER_MAX}</b>.")
    res = await ctx.nw.submit(round_id, user.id, n)
    status = res.get("status")
    if status == "OK":
        text_locked = nm.locked_text(n)
        await message.reply_html(text_locked)
        prompt_id = ctx.nw_prompts.get(round_id, {}).get(user.id)
        if prompt_id:
            await safe_edit(ctx, message.chat.id, prompt_id, text_locked)
        if res.get("all_in"):
            ev = ctx.nw_events.get(res.get("match_id"))
            if ev:
                ev.set()
    elif status == "DUPLICATE":
        await message.reply_html("🔒 You've already locked a number this round.")
    elif status == "NOT_IN":
        await message.reply_html("❌ You're not an active player in this round.")
    else:
        await message.reply_html("⏰ <b>This round is closed.</b>")


# ───────────────────────── commands ─────────────────────────
async def nwrules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_ctx(context).settings
    await update.effective_message.reply_html(nm.rules_text(s.nw_round_seconds, s.nw_max_rounds))


async def nwstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = get_ctx(context)
    user = update.effective_user
    await update.effective_message.reply_html(await _stats_text(ctx, user))


async def _stats_text(ctx: AppContext, user) -> str:
    glob = await ctx.nw.stats_for(user.id, 0)
    rank = await ctx.nw.rank_of(user.id, 0, glob["rating"]) if glob else None
    return nm.stats_text(user.first_name or user.username or "Player", glob, rank)


# ───────────────────────── leaderboard ─────────────────────────
def _lb_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [Btn("🌍 GLOBAL", callback_data="nwl:g"), Btn("👥 THIS GROUP", callback_data="nwl:c")],
        [Btn("👤 MY STATS", callback_data="nwl:me")],
        [Btn("🔙 BACK", callback_data="lbd:menu")],
    ])


async def nw_leaderboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = get_ctx(context)
    q = update.callback_query
    if not q.message:
        return await safe_answer(q, "⚠️ This button is no longer active.", True)
    action = (q.data or "").split(":")[1] if ":" in (q.data or "") else ""
    chat = q.message.chat
    if action == "g":
        text = nm.leaderboard_text(await ctx.nw.leaderboard(0), "Global rating")
    elif action == "c":
        if chat.type not in GROUP_TYPES:
            return await safe_answer(q, "👥 Open this inside a group to see its board.", True)
        text = nm.leaderboard_text(await ctx.nw.leaderboard(chat.id), chat.title or "This group")
    elif action == "me":
        text = await _stats_text(ctx, q.from_user)
    else:
        return await safe_answer(q, "⚠️ Invalid button.")
    await safe_answer(q)
    await safe_edit(ctx, chat.id, q.message.message_id, text, reply_markup=_lb_markup())
