"""NUMBER WARS orchestration: lobby, countdown, round loop, resolution, final, recovery.

Design rules (mirrors the anime-draft services):
  * the DATABASE is the source of truth — deadlines use the database clock and persist, so a
    restart resumes the round that was open (see recover());
  * a round is closed, then resolved exactly once by an atomic SQL function (nw_apply_round);
  * one asyncio task per MATCH (never per player); in-memory dicts are only caches/wake-ups.
"""
from __future__ import annotations

import asyncio
import logging
from fractions import Fraction

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup

from game import number_wars as engine
from services.context import AppContext
from services.telegram_io import check_dm, safe_delete, safe_edit, send_message
from utils import nw_messages as nm

log = logging.getLogger(__name__)


# ───────────────────────── keyboards ─────────────────────────
def lobby_markup(match_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [Btn("🔢 JOIN", callback_data=f"nw:j:{match_id}"),
         Btn("🚪 LEAVE", callback_data=f"nw:lv:{match_id}")],
        [Btn("▶️ START", callback_data=f"nw:st:{match_id}")],
    ])


def new_game_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn("🎮 NEW GAME", callback_data="ga:new:0")]])


# ───────────────────────── lobby ─────────────────────────
async def create_lobby(ctx: AppContext, chat_id: int, host_id: int) -> bool:
    match = await ctx.nw.create_match(chat_id, host_id, ctx.settings.nw_min_players, ctx.settings.nw_max_players)
    if match is None:
        return False
    await refresh_lobby(ctx, match["id"], repost=True)
    return True


async def refresh_lobby(ctx: AppContext, match_id: int, *, repost: bool = False) -> None:
    """Edit the existing lobby message (never spam); re-post only if it was deleted."""
    async with ctx.lock(("nw-lobby", match_id)):
        match = await ctx.nw.get_match(match_id)
        if not match:
            return
        players = await ctx.nw.players(match_id)
        text = nm.lobby_text(match, players, ctx.settings.nw_round_seconds, ctx.settings.nw_lobby_countdown)
        markup = lobby_markup(match_id) if match["status"] == "LOBBY" else None
        old = match.get("lobby_message_id")
        if old and not repost:
            if await safe_edit(ctx, match["group_id"], old, text, reply_markup=markup) != "gone":
                return
        sent = await send_message(ctx, match["group_id"], text, reply_markup=markup)
        if sent:
            await ctx.nw.update_match(match_id, lobby_message_id=sent.message_id)
            if old and repost:
                await safe_delete(ctx, match["group_id"], old)


def arm_countdown(ctx: AppContext, match_id: int) -> None:
    """Auto-start N seconds after the minimum player count is reached (once per lobby)."""
    secs = ctx.settings.nw_lobby_countdown
    if secs <= 0 or match_id in ctx.nw_countdowns:
        return
    ctx.nw_countdowns.add(match_id)
    ctx.spawn(_countdown(ctx, match_id, secs), name=f"nw-countdown-{match_id}")


async def _countdown(ctx: AppContext, match_id: int, secs: int) -> None:
    try:
        await asyncio.sleep(secs)
        match = await ctx.nw.get_match(match_id)
        if not match or match["status"] != "LOBBY":
            return
        if len(await ctx.nw.players(match_id)) >= match["min_players"]:
            await start_match(ctx, match_id)
    finally:
        ctx.nw_countdowns.discard(match_id)


async def start_match(ctx: AppContext, match_id: int) -> bool:
    async with ctx.lock(("nw-start", match_id)):
        match = await ctx.nw.get_match(match_id)
        if not match or match["status"] != "LOBBY":
            return False
        group = match["group_id"]
        players = await ctx.nw.players(match_id)

        # Telegram bots can't open a DM — drop anyone we can't reach before the match locks.
        reachable = await asyncio.gather(*(check_dm(ctx, p["user_id"]) for p in players))
        dropped = [p for p, ok in zip(players, reachable) if not ok]
        for p in dropped:
            await ctx.nw.leave(match_id, p["user_id"])
        if dropped:
            await send_message(ctx, group, "⚠️ Removed (can't DM them): "
                               + ", ".join(nm.esc(p["display_name"]) for p in dropped)
                               + "\nThey need to press <b>Start</b> in the bot's DM, then JOIN again.")
        if len(players) - len(dropped) < match["min_players"]:
            await refresh_lobby(ctx, match_id)
            await send_message(ctx, group, f"❌ Not enough eligible players — need at least "
                                           f"{match['min_players']}. The lobby stays open.")
            return False

        if not await ctx.nw.start(match_id):
            await refresh_lobby(ctx, match_id)
            return False
        await refresh_lobby(ctx, match_id)
        await send_message(ctx, group, f"🔢 <b>NUMBER WARS begins!</b> {len(players) - len(dropped)} players, "
                                       f"{engine.MAX_HP} HP each. Round 1 starts in a moment — watch your DMs!")
    ctx.spawn(run_match(ctx, match_id, initial_delay=3.0), name=f"nw-match-{match_id}")
    return True


async def cancel_match(ctx: AppContext, match: dict, by_name: str) -> bool:
    if not await ctx.nw.cancel(match["id"]):
        return False
    ev = ctx.nw_events.get(match["id"])
    if ev:
        ev.set()                                   # wake a sleeping round loop so it can stop
    await refresh_lobby(ctx, match["id"])
    await send_message(ctx, match["group_id"], f"❌ Match cancelled by {nm.esc(by_name)}.")
    players = await ctx.nw.players(match["id"])
    await asyncio.gather(*(send_message(ctx, p["user_id"], "❌ The NUMBER WARS match in your group was cancelled.")
                           for p in players), return_exceptions=True)
    return True


async def status_text(ctx: AppContext, match: dict) -> str:
    return nm.status_text(match, await ctx.nw.players(match["id"]))


# ───────────────────────── match loop ─────────────────────────
async def run_match(ctx: AppContext, match_id: int, *, initial_delay: float = 0.0) -> None:
    """Resumable: every step re-reads the database, so a crash just retries from the open round."""
    if match_id in ctx.running_nw:
        return
    ctx.running_nw.add(match_id)
    try:
        if initial_delay:
            await asyncio.sleep(initial_delay)
        for attempt in range(1, 4):
            try:
                await _match_loop(ctx, match_id)
                break
            except Exception:
                log.exception("Number Wars match %s failed (attempt %d/3)", match_id, attempt)
                await asyncio.sleep(2 + 3 * attempt)
        else:
            log.error("Number Wars match %s runner failed all 3 attempts", match_id)
            match = await ctx.nw.get_match(match_id)
            if match and match.get("group_id"):
                await send_message(ctx, match["group_id"],
                                   "⚠️ <b>Number Wars match paused</b>\n\n"
                                   "The match runner encountered an error and paused. "
                                   "Progress is saved in the database.")
    finally:
        ctx.running_nw.discard(match_id)
        ctx.nw_events.pop(match_id, None)


async def _match_loop(ctx: AppContext, match_id: int) -> None:
    ev = ctx.nw_events.setdefault(match_id, asyncio.Event())
    while True:
        match = await ctx.nw.get_match(match_id)
        if not match or match["status"] != "ACTIVE":
            return
        opened = await ctx.nw.open_round(match_id, ctx.settings.nw_round_seconds)
        if not opened:
            return
        rnd, created, left = opened["round"], opened["created"], float(opened["seconds_left"])
        alive = [p for p in await ctx.nw.players(match_id) if p["alive"]]

        if rnd["status"] == "OPEN":
            ev.clear()
            if created:
                await _announce_round(ctx, match, rnd, alive)
            else:
                await _resend_prompts(ctx, rnd, alive)
            state = await ctx.nw.close_round(rnd["id"])          # closes early if everyone locked in
            if state == "OPEN":
                try:
                    await asyncio.wait_for(ev.wait(), timeout=max(left, 0.0))
                except asyncio.TimeoutError:
                    pass
                while True:                                   # the DB clock has the last word
                    current = await ctx.nw.get_match(match_id)
                    if not current or current["status"] != "ACTIVE":   # cancelled while waiting
                        return
                    state = await ctx.nw.close_round(rnd["id"])
                    if state != "OPEN":
                        break
                    await asyncio.sleep(0.25)

        finished = await _resolve(ctx, match_id, rnd, alive)
        if finished:
            return
        await asyncio.sleep(ctx.settings.nw_between_rounds)


async def _announce_round(ctx: AppContext, match: dict, rnd: dict, alive: list[dict]) -> None:
    ctx.nw_prompts[rnd["id"]] = {}
    sent = await send_message(ctx, match["group_id"],
                              nm.round_start_group(rnd, len(alive), ctx.settings.nw_round_seconds))
    if sent:
        ctx.nw_round_msgs[rnd["id"]] = sent.message_id
    await _send_prompts(ctx, rnd, alive)


async def _resend_prompts(ctx: AppContext, rnd: dict, alive: list[dict]) -> None:
    """After a restart: re-prompt only the players who haven't locked a number yet."""
    locked = await ctx.nw.submissions(rnd["id"])
    await _send_prompts(ctx, rnd, [p for p in alive if p["user_id"] not in locked], total_alive=len(alive))


async def _send_prompts(ctx: AppContext, rnd: dict, players: list[dict], total_alive: int | None = None) -> None:
    prompts = ctx.nw_prompts.setdefault(rnd["id"], {})
    n_alive = total_alive or len(players)

    async def one(p: dict) -> None:
        text = nm.dm_prompt(rnd, p["hp"], n_alive, ctx.settings.nw_round_seconds)
        sent = await send_message(ctx, p["user_id"], text)
        if sent:
            prompts[p["user_id"]] = sent.message_id

    await asyncio.gather(*(one(p) for p in players), return_exceptions=True)


async def _resolve(ctx: AppContext, match_id: int, rnd: dict, alive: list[dict]) -> bool:
    """Compute → apply atomically → report. Returns True when the match is over."""
    subs = await ctx.nw.submissions(rnd["id"])
    ctx.spawn(_close_prompts(ctx, rnd, set(subs)), name=f"nw-close-prompts-{rnd['id']}")

    before = [engine.Standing(p["user_id"], p["hp"], p["round_wins"], float(p["total_distance"])) for p in alive]
    outcome = engine.resolve_round({s.user_id: s.hp for s in before}, subs, Fraction(str(rnd["multiplier"])))
    after = engine.apply_outcome(before, outcome)
    end = engine.decide_end(before, after, rnd["round_number"], ctx.settings.nw_max_rounds)

    if not await ctx.nw.apply_round(rnd["id"], outcome, end):
        match = await ctx.nw.get_match(match_id)       # already applied, or cancelled
        return not match or match["status"] != "ACTIVE"

    names = {p["user_id"]: p["display_name"] for p in alive}
    await _post_round_result(ctx, match_id, rnd, outcome, names)
    winners = set(end[0]) if end else set()
    for r in outcome.results:
        if r.eliminated and r.user_id not in winners:
            ctx.spawn(send_message(ctx, r.user_id, nm.eliminated_dm(rnd["round_number"])), name="nw-elim-dm")
    if end:
        await announce_final(ctx, match_id)
        return True
    return False


async def _close_prompts(ctx: AppContext, rnd: dict, locked: set[int]) -> None:
    prompts = ctx.nw_prompts.pop(rnd["id"], {})
    await asyncio.gather(*(safe_edit(ctx, uid, mid, nm.missed_text(rnd["round_number"]))
                           for uid, mid in prompts.items() if uid not in locked), return_exceptions=True)


async def _post_round_result(ctx: AppContext, match_id: int, rnd: dict, outcome, names: dict[int, str]) -> None:
    match = await ctx.nw.get_match(match_id)
    if not match:
        return
    text = nm.round_result(rnd, outcome, names)
    mid = ctx.nw_round_msgs.pop(rnd["id"], None)
    if mid and await safe_edit(ctx, match["group_id"], mid, text) == "ok":   # round banner → results
        return
    await send_message(ctx, match["group_id"], text)


async def announce_final(ctx: AppContext, match_id: int) -> None:
    if not await ctx.nw.claim_announce(match_id):
        return
    match = await ctx.nw.get_match(match_id)
    if not match:
        return
    players = await ctx.nw.players(match_id)
    sent = await send_message(ctx, match["group_id"], nm.final_text(match, players),
                              reply_markup=new_game_markup())
    if not sent:
        await ctx.nw.release_announce(match_id)    # retried on the next restart
        return
    await refresh_lobby(ctx, match_id)             # lobby card → "finished"


# ───────────────────────── recovery ─────────────────────────
async def recover(ctx: AppContext) -> None:
    active = await ctx.nw.by_status(["ACTIVE"])
    pending = await ctx.nw.unannounced_finished()
    lobbies = await ctx.nw.by_status(["LOBBY"])
    for m in active:
        ctx.spawn(run_match(ctx, m["id"]), name=f"nw-resume-{m['id']}")
    for m in pending:
        ctx.spawn(announce_final(ctx, m["id"]), name=f"nw-announce-{m['id']}")
    for m in lobbies:
        if len(await ctx.nw.players(m["id"])) >= m["min_players"]:
            arm_countdown(ctx, m["id"])
    log.info("Number Wars recovery: %d active, %d to announce, %d lobbies", len(active), len(pending), len(lobbies))
