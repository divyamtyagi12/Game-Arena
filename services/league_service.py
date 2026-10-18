"""League runner: round-robin rounds, standings updates, then hand-off to the final."""
from __future__ import annotations

import asyncio
import logging

from services import final_service, match_service, table_service
from services.context import AppContext
from services.telegram_io import send_message

log = logging.getLogger(__name__)


def launch(ctx: AppContext, game_id: int) -> None:
    """Start the runner once per game (idempotent)."""
    if game_id in ctx.running_games:
        return
    ctx.running_games.add(game_id)

    async def _wrap() -> None:
        try:
            for attempt in range(1, 4):
                try:
                    await run_league(ctx, game_id)
                    return
                except Exception:  # noqa: BLE001
                    log.exception("League runner for game %s failed (attempt %d/3)", game_id, attempt)
                    await asyncio.sleep(10 * attempt)
        finally:
            ctx.running_games.discard(game_id)

    ctx.spawn(_wrap(), name=f"league-{game_id}")


async def run_league(ctx: AppContext, game_id: int) -> None:
    game = await ctx.games.get(game_id)
    if not game:
        return
    if game["status"] == "FINAL":
        await final_service.start_final(ctx, game_id)
        return
    if game["status"] != "LEAGUE":
        return

    await ctx.matches.reset_running(game_id)  # we are the only runner: RUNNING = orphaned
    players = await ctx.players.list_for_game(game_id)
    created = await ctx.matches.ensure_fixtures(game_id, [p["id"] for p in players])
    matches = await ctx.matches.list_for_game(game_id, "LEAGUE")
    rounds = sorted({m["round_no"] for m in matches})
    if created:
        await send_message(ctx, game["group_id"],
                           f"⚔️ <b>LEAGUE STAGE BEGINS</b>\n\n📋 {len(matches)} matches · {len(rounds)} rounds\n"
                           "Battles are fought in your DMs. I'll update the table after every round (/table).")

    for rn in rounds:
        game = await ctx.games.get(game_id)
        if not game or game["status"] != "LEAGUE":
            return
        todo = [m for m in matches if m["round_no"] == rn and (m["status"] != "DONE" or not m["notified"])]
        if todo:
            tasks = []
            for i, m in enumerate(todo):
                async def _staggered(match_item=m, offset=i * 0.35):
                    if offset > 0:
                        await asyncio.sleep(offset)
                    return await match_service.execute_match(ctx, match_item, title=f"⚔️ ROUND {rn}/{len(rounds)}")
                tasks.append(_staggered())
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            errors = [o for o in outcomes if isinstance(o, BaseException)]
            if errors:
                raise RuntimeError(f"{len(errors)} match(es) failed in round {rn}: {errors[0]!r}")
            await table_service.post_table(ctx, game_id, f"After round {rn}/{len(rounds)}")
            # Cooldown between rounds to let Telegram rate limits replenish and update handlers breathe
            if rn != rounds[-1]:
                await asyncio.sleep(2.0)

    await final_service.start_final(ctx, game_id)
