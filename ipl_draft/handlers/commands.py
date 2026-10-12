"""IPL slash commands: /ipl /iplrules /iplteam /ipltable /iplfixtures /iplhistory /iplstats."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from handlers.common import get_ctx, is_admin

from .. import keyboards as kb
from .. import messages as m
from ..services import simulation_worker, tournament_service
from .callbacks import GROUP_TYPES, PAGE, get_rt

log = logging.getLogger(__name__)


async def create_ipl_lobby(rt, chat, user) -> str:
    """Shared by the main-menu button and /ipl. Returns CREATED | BUSY | DISABLED."""
    if not rt.s.enabled:
        return "DISABLED"
    await rt.ctx.groups.upsert(chat.id, chat.title)
    await rt.ctx.users.upsert(user.id, user.username, user.first_name)
    from handlers import arena                               # local import: arena imports this module's callers
    if await arena.active_game_exists(rt.ctx, chat.id):
        return "BUSY"
    t = await tournament_service.create_lobby(rt, chat.id, user.id)
    return "CREATED" if t else "BUSY"


async def _tournament(rt, update: Update, *, prefer_active: bool = True) -> dict | None:
    chat, user = update.effective_chat, update.effective_user
    if chat.type in GROUP_TYPES:
        return await rt.repo.active_for_group(chat.id) or await rt.repo.latest_for_group(chat.id)
    return await rt.repo.active_for_user(user.id) or await rt.repo.latest_for_user(user.id)


async def ipl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = get_rt(context)
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type == ChatType.PRIVATE:
        await rt.ctx.users.upsert(user.id, user.username, user.first_name, dm_started=True)
        t = await rt.repo.active_for_user(user.id)
        if t:
            return await msg.reply_html(m.status_text(t, len(await rt.repo.lobby_teams(t["id"]))))
        return await msg.reply_html("🏏 <b>IPL DRAFT</b>\n\nAdd me to a group and send /ipl there to open a lobby.\n" + "Rules: /iplrules")
    if chat.type not in GROUP_TYPES:
        return
    existing = await rt.repo.active_for_group(chat.id)
    if existing and existing["state"] == "LOBBY":                 # bring the lobby back to the bottom of the chat
        await rt.ctx.users.upsert(user.id, user.username, user.first_name)
        from ..services import notification_service as notify
        return await notify.refresh_lobby(rt, existing["id"], repost=True)
    res = await create_ipl_lobby(rt, chat, user)
    if res == "BUSY":
        await msg.reply_html("⚠️ A game is already in progress here. Use /status.")
    elif res == "DISABLED":
        await msg.reply_html("⚠️ IPL Draft is currently disabled.")


async def iplrules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = get_rt(context)
    await update.effective_message.reply_html(m.rules_text(rt.s.draft_seconds))


async def iplteam_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = get_rt(context)
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type != ChatType.PRIVATE:
        return await msg.reply_html("🤫 Squads are secret while drafting! Use /iplteam in my DM.")
    t = await rt.repo.active_for_user(user.id) or await rt.repo.latest_for_user(user.id)
    team = await rt.repo.team_of_user(t["id"], user.id) if t else None
    if not team:
        return await msg.reply_html("You're not in an IPL Draft yet. Send /ipl in a group to start one!")
    roster = await rt.repo.team_roster(team["id"])
    players = roster["players"]
    text = m.my_team_text(roster["team"], players, len(players) >= 11)
    if t["state"] == "DRAFTING" and len(players) < 11:
        text += "\n\nYour current draft round is in the message above (scroll up)."
    await msg.reply_html(text)


async def ipltable_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = get_rt(context)
    msg = update.effective_message
    t = await _tournament(rt, update)
    if not t:
        return await msg.reply_html("No IPL Draft found. Send /ipl in a group to start one.")
    if t["state"] in ("LOBBY", "DRAFTING", "SYSTEM_TEAM_GENERATION", "FIXTURE_GENERATION", "CANCELLED"):
        return await msg.reply_html("🏆 The league hasn't started yet.")
    if t["state"] == "LEAGUE_RUNNING":
        return await msg.reply_html("⏳ Points table will be dropped after the league stage finishes!")
    await msg.reply_html(m.points_table(await rt.repo.standings(t["id"])), reply_markup=kb.table_markup(t["id"]))


async def iplfixtures_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = get_rt(context)
    msg = update.effective_message
    t = await _tournament(rt, update)
    if not t or t["state"] in ("LOBBY", "DRAFTING", "SYSTEM_TEAM_GENERATION", "CANCELLED"):
        return await msg.reply_html("📅 No fixtures yet — they're created once the draft finishes.")
    data = await rt.repo.fixtures_page(t["id"], 0, PAGE)
    pages = max(1, -(-data["total"] // PAGE))
    await msg.reply_html(m.fixtures_page_text(data, 0, PAGE), reply_markup=kb.pager("fx", t["id"], 0, pages, back=f"ipl:tr:{t['id']}"))


async def iplhistory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = get_rt(context)
    chat, msg = update.effective_chat, update.effective_message
    t = await _tournament(rt, update)
    if t and t["state"] not in ("LOBBY", "DRAFTING", "SYSTEM_TEAM_GENERATION", "FIXTURE_GENERATION", "CANCELLED"):
        data = await rt.repo.matches_page(t["id"], 0, PAGE)
        pages = max(1, -(-data["total"] // PAGE))
        return await msg.reply_html(m.history_page_text(data, 0, PAGE), reply_markup=kb.pager("mh", t["id"], 0, pages, back=f"ipl:tr:{t['id']}"))
    if chat.type in GROUP_TYPES:                                  # no tournament running → past champions of this group
        return await msg.reply_html(m.history_text(await rt.repo.group_history(chat.id)))
    await msg.reply_html("No IPL matches yet.")




async def iplstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = get_rt(context)
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    st = await rt.repo.user_stats(user.id, chat.id if chat.type in GROUP_TYPES else None)
    name = user.first_name or user.username or "Player"
    text = m.user_stats_text(name, st.get("global"))
    if chat.type in GROUP_TYPES and st.get("group"):
        text += "\n\n" + m.user_stats_text(name, st["group"], scope="This group")
    await msg.reply_html(text)
