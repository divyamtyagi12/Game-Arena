"""Run one match: claim → compute → persist (atomic) → deliver to both DMs."""
from __future__ import annotations

import asyncio
import logging

from game.battle import ClashResult, MatchResult, Tiebreak, play_match
from game.categories import CATEGORY_KEYS
from services.context import AppContext
from services.telegram_io import safe_edit, send_message
from utils import messages as msg

log = logging.getLogger(__name__)


def result_from_rows(ctx: AppContext, rows: list[dict], match: dict) -> MatchResult:
    clashes = [ClashResult(
        clash_no=r["clash_no"], category=r["category"],
        p1_char=ctx.catalog.get(r["p1_character_id"]), p2_char=ctx.catalog.get(r["p2_character_id"]),
        p1_rating=r["p1_rating"], p2_rating=r["p2_rating"],
        p1_score=float(r["p1_score"]), p2_score=float(r["p2_score"]), winner_side=r["winner_side"])
        for r in sorted(rows, key=lambda r: r["clash_no"])]
    tb = None
    if match.get("tiebreak_used"):
        tb = Tiebreak(float(match["tiebreak_p1"]), float(match["tiebreak_p2"]),
                      1 if match["winner_id"] == match["p1_id"] else 2)
    return MatchResult(clashes, tb)


async def execute_match(ctx: AppContext, match: dict, *, title: str, final: bool = False) -> MatchResult | None:
    p1, p2 = await ctx.players.get(match["p1_id"]), await ctx.players.get(match["p2_id"])
    if match["status"] == "PENDING":
        game = await ctx.games.get(match["game_id"])
        if not game or game["status"] not in ("LEAGUE", "FINAL"):
            return None  # cancelled
        if not await ctx.matches.claim(match["id"]):
            return None  # another task owns it → no double execution
        t1, t2 = await ctx.load_team(p1["id"]), await ctx.load_team(p2["id"])
        result = play_match(t1, t2, ctx.rng, final=final)
        await ctx.matches.finish(match["id"], result)  # atomic; awards points exactly once
        animated = True
    elif match["status"] == "DONE" and not match["notified"]:
        fresh = await ctx.matches.get(match["id"])
        result = result_from_rows(ctx, await ctx.matches.clashes(match["id"]), fresh)
        animated = False  # recovery: only deliver the static scorecard
    else:
        return None
    try:
        await _deliver(ctx, p1, p2, result, title=title, final=final, animated=animated)
    except Exception as exc:
        log.warning("Match %s delivery encountered error: %s", match["id"], exc)
    try:
        await ctx.matches.mark_notified(match["id"])
    except Exception as exc:
        log.warning("Match %s mark_notified error: %s", match["id"], exc)
    return result


async def _deliver(ctx: AppContext, p1: dict, p2: dict, result: MatchResult, *,
                   title: str, final: bool, animated: bool) -> None:
    n1, n2 = p1["display_name"], p2["display_name"]
    delay = ctx.settings.clash_delay

    async def to_player(uid: int) -> None:
        live = await send_message(ctx, uid, msg.battle_intro(title, n1, n2)) if animated else None
        if live:
            for i in range(1, len(result.clashes) + 1):
                await asyncio.sleep(delay)
                await safe_edit(ctx, uid, live.message_id,
                                msg.battle_progress_text(title, n1, n2, result.clashes, i))
            if result.tiebreak:
                await asyncio.sleep(delay)
                await safe_edit(ctx, uid, live.message_id, msg.tiebreak_text(title, n1, n2, result, False))
                await asyncio.sleep(delay + 1)
                await safe_edit(ctx, uid, live.message_id, msg.tiebreak_text(title, n1, n2, result, True))
            await asyncio.sleep(delay / 2)
        await send_message(ctx, uid, msg.scorecard_text(n1, n2, result, final))

    await asyncio.gather(to_player(p1["user_id"]), to_player(p2["user_id"]), return_exceptions=True)
