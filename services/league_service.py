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
                    await asyncio.sleep(1.5 * attempt)
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

    # Limit concurrent live match animations to 2 so Telegram API edits are not flooded
    sem = asyncio.Semaphore(2)

    for rn in rounds:
        game = await ctx.games.get(game_id)
        if not game or game["status"] != "LEAGUE":
            return
        todo = [m for m in matches if m["round_no"] == rn and (m["status"] != "DONE" or not m["notified"])]
        if todo:
            async def _run_with_sem(match_item):
                async with sem:
                    return await match_service.execute_match(ctx, match_item, title=f"⚔️ ROUND {rn}/{len(rounds)}")

            outcomes = await asyncio.gather(*(_run_with_sem(m) for m in todo), return_exceptions=True)
            errors = [o for o in outcomes if isinstance(o, BaseException)]
            if errors:
                log.error("Round %d had %d failed match(es): %s (will retry remaining)", rn, len(errors), errors[0])
                # Small 1-second pause to retry any uncompleted match once before progressing
                await asyncio.sleep(1.0)
                remaining = [m for m in matches if m["round_no"] == rn and m["status"] != "DONE"]
                if remaining:
                    await asyncio.gather(*(_run_with_sem(m) for m in remaining), return_exceptions=True)

            await table_service.post_table(ctx, game_id, f"After round {rn}/{len(rounds)}")
            # Cooldown between rounds to let Telegram rate limits replenish and update handlers breathe
            if rn != rounds[-1]:
                await asyncio.sleep(1.5)

    await final_service.start_final(ctx, game_id)
