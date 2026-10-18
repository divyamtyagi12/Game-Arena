"""Drafting: start, offers, picks, finalisation, recovery, auto-pick."""
from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database.client import is_unique_violation
from game.categories import CATEGORY_KEYS, TEAM_SIZE
from game.draft import build_offers
from models.character import Character
from postgrest.exceptions import APIError
from services import images, league_service, lobby_service
from services.context import AppContext
from services.telegram_io import check_dm, safe_edit, send_message
from utils import messages as msg
from utils.timeutil import in_minutes_iso, now_iso

log = logging.getLogger(__name__)


# ───────────────────────── start ─────────────────────────
async def start_game(ctx: AppContext, game_id: int) -> None:
    async with ctx.lock(("start", game_id)):  # cheap in-process guard; DB RPC is the real lock
        game = await ctx.games.get(game_id)
        if not game or game["status"] != "LOBBY":
            return
        players = await ctx.players.list_for_game(game_id)
        gid = game["group_id"]

        # Every player must be reachable by DM before the tournament may begin.
        results = await asyncio.gather(*(check_dm(ctx, p["user_id"]) for p in players))
        bad = [p for p, ok in zip(players, results) if not ok]
        if bad:
            for p in bad:
                await ctx.games.leave(game_id, p["user_id"])
            names = ", ".join(msg.esc(p["display_name"]) for p in bad)
            await send_message(ctx, gid,
                               f"⚠️ Removed (can't DM): <b>{names}</b>\nThey must open the bot and press Start, then JOIN again.",
                               reply_markup=lobby_service.open_bot_markup(ctx))
            players = [p for p in players if p not in bad]
            await lobby_service.refresh_lobby(ctx, game_id)
        if len(players) < game["min_players"]:
            await send_message(ctx, gid, f"❌ Need at least {game['min_players']} players to start.")
            return

        timeout = ctx.settings.draft_timeout_minutes
        deadline = in_minutes_iso(timeout) if timeout > 0 else None
        if not await ctx.games.begin_draft(game_id, deadline):
            return  # someone else already started it (or players left)
        await lobby_service.refresh_lobby(ctx, game_id)
        prog = await send_message(ctx, gid, msg.progress_text(
            [{**p, "draft_done": False} for p in players], {}))
        if prog:
            await ctx.games.update(game_id, progress_message_id=prog.message_id)
        group = await ctx.groups.get(gid)
        title = (group or {}).get("title", "")
        await asyncio.gather(*(_begin_player(ctx, p, title) for p in players), return_exceptions=True)


async def _begin_player(ctx: AppContext, gp: dict, title: str) -> None:
    await send_message(ctx, gp["user_id"], msg.draft_intro(title))
    await send_prompt(ctx, gp)


# ───────────────────────── prompts ─────────────────────────
async def ensure_offer(ctx: AppContext, gp_id: int, category: str, picked: set[str]) -> list[Character]:
    ids = await ctx.drafts.get_offer(gp_id, category)
    if not ids:
        chars = build_offers(ctx.catalog, category, picked, ctx.rng)
        ids = await ctx.drafts.create_offer(gp_id, category, [c.id for c in chars])
    return [ctx.catalog.get(i) for i in ids]


def offer_markup(gp_id: int, idx: int, chars: list[Character]) -> InlineKeyboardMarkup:
    labels = msg.button_labels(chars)
    rows = [[InlineKeyboardButton(f"{pos}. {label}", callback_data=f"dp:{gp_id}:{idx}:{pos}")]
            for pos, label in enumerate(labels, 1)]
    return InlineKeyboardMarkup(rows)


async def send_prompt(ctx: AppContext, gp: dict) -> bool:
    picks = await ctx.drafts.picks(gp["id"])
    idx = len(picks)
    if idx >= TEAM_SIZE:
        return False
    cat = CATEGORY_KEYS[idx]
    chars = await ensure_offer(ctx, gp["id"], cat, {p["character_id"] for p in picks})
    await images.send_album(ctx, gp["user_id"], chars)
    sent = await send_message(ctx, gp["user_id"], msg.offer_text(cat, idx, chars),
                              reply_markup=offer_markup(gp["id"], idx, chars))
    return sent is not None


# ───────────────────────── picks ─────────────────────────
async def handle_pick(ctx: AppContext, query, gp_id: int, cat_idx: int, pos: int) -> None:
    async def answer(text: str, alert: bool = False) -> None:
        try:
            await asyncio.wait_for(query.answer(text, show_alert=alert), timeout=4.0)
        except Exception:  # noqa: BLE001 - answering is best effort
            pass

    user = query.from_user
    gp = await ctx.players.get(gp_id)
    if not gp or gp["user_id"] != user.id:
        return await answer("❌ This isn't your draft.", True)
    game = await ctx.games.get(gp["game_id"])
    if not game or game["status"] != "DRAFTING":
        return await answer("⚠️ This draft is no longer active.", True)
    picks = await ctx.drafts.picks(gp_id)
    if cat_idx != len(picks) or not 0 <= cat_idx < TEAM_SIZE:
        return await answer("⚠️ You already chose for this category.")
    cat = CATEGORY_KEYS[cat_idx]
    ids = await ctx.drafts.get_offer(gp_id, cat)
    if not ids or not 1 <= pos <= len(ids):
        return await answer("⚠️ That button is outdated.", True)
    char = ctx.catalog.get(ids[pos - 1])
    try:
        await ctx.drafts.add_pick(gp_id, cat, char.id)
    except APIError as exc:
        if is_unique_violation(exc):  # double-click / race: the other click already won
            return await answer("⚠️ Already selected.")
        raise
    await answer(f"✅ {char.name} selected!")
    if query.message:
        try:
            await safe_edit(ctx, query.message.chat.id, query.message.message_id,
                            msg.pick_locked_text(cat, char))
        except Exception as exc:
            log.warning("safe_edit pick locked text error: %s", exc)
    if len(picks) + 1 >= TEAM_SIZE:
        await finalize_player(ctx, gp_id)
    else:
        await send_prompt(ctx, gp)


async def finalize_player(ctx: AppContext, gp_id: int) -> None:
    gp = await ctx.players.get(gp_id)
    picks = await ctx.drafts.picks(gp_id)
    if not gp or len(picks) < TEAM_SIZE:
        return
    await ctx.drafts.create_team(gp_id, {p["category"]: p["character_id"] for p in picks})
    if not await ctx.players.mark_done(gp_id):
        return  # another task already finalised this player
    team = {p["category"]: ctx.catalog.get(p["character_id"]) for p in picks}
    await send_message(ctx, gp["user_id"], msg.team_text(team, locked=True)
                       + "\n\n⏳ Waiting for the other players to finish drafting…")
    await refresh_progress(ctx, gp["game_id"])
    await try_begin_league(ctx, gp["game_id"])


async def refresh_progress(ctx: AppContext, game_id: int) -> None:
    async with ctx.lock(("progress", game_id)):
        game = await ctx.games.get(game_id)
        if not game:
            return
        players = await ctx.players.list_for_game(game_id)
        counts = await ctx.drafts.pick_counts([p["id"] for p in players])
        text = msg.progress_text(players, counts)
        mid = game.get("progress_message_id")
        if mid and await safe_edit(ctx, game["group_id"], mid, text) != "gone":
            return
        sent = await send_message(ctx, game["group_id"], text)
        if sent:
            await ctx.games.update(game_id, progress_message_id=sent.message_id)


async def try_begin_league(ctx: AppContext, game_id: int) -> None:
    if await ctx.games.begin_league(game_id):  # exactly one caller wins
        await refresh_progress(ctx, game_id)
        league_service.launch(ctx, game_id)


# ───────────────────────── recovery / timeout ─────────────────────────
async def resume_draft(ctx: AppContext, game_id: int) -> None:
    for p in await ctx.players.list_for_game(game_id):
        if p["draft_done"]:
            continue
        picks = await ctx.drafts.picks(p["id"])
        if len(picks) >= TEAM_SIZE:
            await finalize_player(ctx, p["id"])
        elif not await ctx.drafts.get_offer(p["id"], CATEGORY_KEYS[len(picks)]):
            await send_prompt(ctx, p)  # prompt was never delivered before the restart
    await try_begin_league(ctx, game_id)


async def autopick_player(ctx: AppContext, gp: dict) -> None:
    while True:
        picks = await ctx.drafts.picks(gp["id"])
        if len(picks) >= TEAM_SIZE:
            break
        cat = CATEGORY_KEYS[len(picks)]
        chars = await ensure_offer(ctx, gp["id"], cat, {p["character_id"] for p in picks})
        try:
            await ctx.drafts.add_pick(gp["id"], cat, ctx.rng.choice(chars).id, auto=True)
        except APIError as exc:
            if not is_unique_violation(exc):
                raise
    await send_message(ctx, gp["user_id"], "⏱️ Draft time ran out — your remaining picks were chosen automatically.")
    await finalize_player(ctx, gp["id"])


async def autopick_expired(ctx: AppContext) -> None:
    for game in await ctx.games.expired_drafts(now_iso()):
        slow = [p for p in await ctx.players.list_for_game(game["id"]) if not p["draft_done"]]
        if slow:
            await send_message(ctx, game["group_id"], "⏱️ Draft time is up — auto-picking for slow drafters.")
        for p in slow:
            try:
                await autopick_player(ctx, p)
            except Exception as exc:
                log.exception("Autopick failed for player %s in game %s: %s", p.get("id"), game["id"], exc)
        try:
            await try_begin_league(ctx, game["id"])
        except Exception as exc:
            log.exception("try_begin_league after autopick failed for game %s: %s", game["id"], exc)


async def autopick_loop(ctx: AppContext) -> None:
    while True:
        try:
            await asyncio.sleep(60)
            await autopick_expired(ctx)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("autopick loop error (recovering)")
