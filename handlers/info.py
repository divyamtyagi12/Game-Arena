from __future__ import annotations

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from handlers.common import get_ctx
from handlers.start import GROUP_TYPES
from services import number_wars_service as nw_service, table_service
from utils import messages as msg


async def _game_for(ctx, update):
    chat, user = update.effective_chat, update.effective_user
    if chat.type in GROUP_TYPES:
        return await ctx.games.active_for_group(chat.id) or await ctx.games.latest_for_group(chat.id)
    for gp in await ctx.players.for_user(user.id, 10):
        game = await ctx.games.get(gp["game_id"])
        if game and game["status"] != "CANCELLED":
            return game
    return None


async def team_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = get_ctx(context)
    chat, user, message = update.effective_chat, update.effective_user, update.effective_message
    if chat.type != ChatType.PRIVATE:
        return await message.reply_html("🤫 Teams are secret! Use /team in my DM.")
    for gp in await ctx.players.for_user(user.id, 10):
        game = await ctx.games.get(gp["game_id"])
        if not game or game["status"] == "CANCELLED":
            continue
        picks = await ctx.drafts.picks(gp["id"])
        team = {p["category"]: ctx.catalog.get(p["character_id"]) for p in picks}
        text = msg.team_text(team, locked=gp["draft_done"])
        if not gp["draft_done"]:
            text += "\n\n✍️ Draft in progress — use /draft if you lost the prompt."
        return await message.reply_html(text)
    await message.reply_html("You're not in any game yet. Join one from a group with /start!")


async def table_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = get_ctx(context)
    message = update.effective_message
    chat = update.effective_chat
    if chat.type in GROUP_TYPES and not await ctx.games.active_for_group(chat.id):
        if await ctx.nw.active_for_group(chat.id):
            return await message.reply_html("🔢 Number Wars has no league table. Use /status.")
        rt = context.bot_data.get("ipl")
        if rt is not None and rt.s.enabled and await rt.repo.active_for_group(chat.id):
            from ipl_draft.handlers.commands import ipltable_command
            return await ipltable_command(update, context)
    game = await _game_for(ctx, update)
    if not game:
        return await message.reply_html("No game found.")
    if game["status"] in ("LOBBY", "DRAFTING"):
        return await message.reply_html("🏆 The league hasn't started yet.")
    await message.reply_html(await table_service.table_message(ctx, game["id"]))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = get_ctx(context)
    message = update.effective_message
    chat, user = update.effective_chat, update.effective_user
    if chat.type not in GROUP_TYPES:
        parts = []
        nw_match = await ctx.nw.active_for_user(user.id)
        if nw_match:
            parts.append(await nw_service.status_text(ctx, nw_match))
        rt = context.bot_data.get("ipl")
        if rt is not None and rt.s.enabled:
            ipl_t = await rt.repo.active_for_user(user.id)
            if ipl_t:
                from ipl_draft import messages as ipl_msg
                parts.append(ipl_msg.status_text(ipl_t, len(await rt.repo.lobby_teams(ipl_t["id"]))))
        game = await _game_for(ctx, update)
        if game and game["status"] in ("LOBBY", "DRAFTING", "LEAGUE", "FINAL"):
            parts.append(f"⚔️ <b>ANIME DRAFT</b>: stage <b>{game['status']}</b>")
        if parts:
            return await message.reply_html("\n\n".join(parts))
    else:
        nw_match = None if await ctx.games.active_for_group(chat.id) else await ctx.nw.active_for_group(chat.id)
        if nw_match:
            return await message.reply_html(await nw_service.status_text(ctx, nw_match))
        rt = context.bot_data.get("ipl")
        if rt is not None and rt.s.enabled and not await ctx.games.active_for_group(chat.id):
            ipl_t = await rt.repo.active_for_group(chat.id)
            if ipl_t:
                from ipl_draft import messages as ipl_msg
                teams = await rt.repo.lobby_teams(ipl_t["id"])
                extra = ""
                if ipl_t["state"] == "LEAGUE_RUNNING":
                    prog = await rt.repo.league_progress(ipl_t["id"])
                    extra = f"\nLeague matches: {prog.get('done', 0)}/{prog.get('total', 0)}"
                return await message.reply_html(ipl_msg.status_text(ipl_t, len(teams), extra))
    game = await _game_for(ctx, update)
    if not game:
        return await message.reply_html("No game found. Send /start in a group to create one.")
    players = await ctx.players.list_for_game(game["id"])
    st = game["status"]
    lines = ["📊 <b>ANIME DRAFT — Status</b>", "", f"Stage: <b>{st}</b>", f"Players: {len(players)}"]
    if st == "DRAFTING":
        counts = await ctx.drafts.pick_counts([p["id"] for p in players])
        lines += ["", msg.progress_text(players, counts)]
    elif st in ("LEAGUE", "FINAL", "COMPLETED"):
        ms = await ctx.matches.list_for_game(game["id"], "LEAGUE")
        done = sum(1 for m in ms if m["status"] == "DONE")
        lines.append(f"League matches: {done}/{len(ms)} played")
        if st == "COMPLETED":
            final = await ctx.matches.final_for_game(game["id"])
            names = {p["id"]: p["display_name"] for p in players}
            if final and final.get("winner_id"):
                lines.append(f"👑 Champion: <b>{msg.esc(names.get(final['winner_id'], '?'))}</b>")
    await message.reply_html("\n".join(lines))
