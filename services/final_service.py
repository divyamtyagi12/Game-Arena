"""Grand final + championship announcement (duplicate-proof)."""
from __future__ import annotations

import logging

from services import leaderboard_service, match_service, table_service
from services.context import AppContext
from services.telegram_io import send_message
from utils import messages as msg

log = logging.getLogger(__name__)


async def start_final(ctx: AppContext, game_id: int) -> None:
    game = await ctx.games.get(game_id)
    if not game or game["status"] not in ("LEAGUE", "FINAL"):
        return
    ranked, names, _ = await table_service.build_table(ctx, game_id)
    if len(ranked) < 2:
        return
    top1, top2 = ranked[0], ranked[1]
    info = await ctx.games.begin_final(game_id, top1["game_player_id"], top2["game_player_id"])
    if not info:  # league not fully finished (or cancelled)
        game = await ctx.games.get(game_id)
        if game and game["status"] == "LEAGUE":
            all_m = await ctx.matches.list_for_game(game_id, "LEAGUE")
            unfinished = [m for m in all_m if m["status"] != "DONE"]
            if unfinished:
                raise RuntimeError(f"begin_final refused to start: {len(unfinished)} league matches are unfinished")
        return
    match = await ctx.matches.get(info["match_id"])
    n1, n2 = names[match["p1_id"]], names[match["p2_id"]]
    if info["created"]:
        by_id = {r["game_player_id"]: r for r in ranked}
        await send_message(ctx, game["group_id"], msg.league_complete_text(
            n1, by_id[match["p1_id"]]["points"], n2, by_id[match["p2_id"]]["points"]))
    await match_service.execute_match(ctx, match, title="🔥 GRAND FINAL", final=True)
    await announce_final(ctx, game_id)


async def announce_final(ctx: AppContext, game_id: int) -> None:
    game = await ctx.games.get(game_id)
    if not game or game["status"] != "COMPLETED":
        return
    await leaderboard_service.record_game(ctx, game_id)  # idempotent; never blocks the announcement
    if not await ctx.games.claim_announcement(game_id):  # only one announcement ever
        return
    try:
        match = await ctx.matches.final_for_game(game_id)
        players = {p["id"]: p for p in await ctx.players.list_for_game(game_id)}
        result = match_service.result_from_rows(ctx, await ctx.matches.clashes(match["id"]), match)
        text = msg.final_announcement(players[match["p1_id"]]["display_name"],
                                      players[match["p2_id"]]["display_name"], result)
        block, markup = await leaderboard_service.final_block(ctx, game, players, match)
        if block:
            text += "\n\n" + block
        if await send_message(ctx, game["group_id"], text, reply_markup=markup) is None:
            raise RuntimeError("could not post the final announcement")
    except Exception:
        await ctx.games.release_announcement(game_id)  # allow a retry (recovery)
        raise
    await table_service.post_table(ctx, game_id, "Final league standings")
