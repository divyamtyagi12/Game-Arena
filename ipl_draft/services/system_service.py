"""System franchises draft through the SAME offer mechanism as humans (11 random cards per pick)."""
from __future__ import annotations

import logging

from ..engine.team_builder import FRANCHISES, choose, force_roles, franchise_payload, snake_order
from ..runtime import IplRuntime

log = logging.getLogger(__name__)


class SystemDraftError(RuntimeError):
    pass


async def run_system_draft(rt: IplRuntime, tid: int) -> None:
    """Resumable: every pick is committed individually, so a restart simply continues where it stopped."""
    prog = await rt.repo.system_progress(tid)
    rosters = {p["team_id"]: list(p["roster"]) for p in prog}
    picks = {p["team_id"]: p["picks"] for p in prog}
    order_ids = [p["team_id"] for p in sorted(prog, key=lambda p: p["team_no"])]
    guard = 0
    while any(picks[t] < 11 for t in order_ids):
        guard += 1
        if guard > 11 * len(order_ids) + 50:
            raise SystemDraftError("system draft made no progress")
        rnd = min(picks[t] for t in order_ids if picks[t] < 11) + 1
        for team_id in snake_order(order_ids, rnd):
            if picks[team_id] != rnd - 1:
                continue
            offer = await rt.repo.open_offer(team_id, force_roles(rosters[team_id]))
            st = offer.get("status")
            if st == "DONE":
                picks[team_id] = 11
                continue
            if st != "OPEN":
                raise SystemDraftError(f"cannot open offer for team {team_id}: {st}")
            ratings = {r["pos"]: r for r in await rt.repo.system_offer_ratings(offer["offer_id"])}
            cards = [{"pos": c["pos"], "role": c["player"]["role"], "bowling_type": c["player"]["bowling_type"],
                      "ratings": ratings[c["pos"]]["ratings"]} for c in offer["candidates"] if c["pos"] in ratings]
            if not cards:
                raise SystemDraftError("offer without rating snapshot")
            pos = choose(cards, rosters[team_id], rt.rng)
            res = await rt.repo.pick(offer["offer_id"], pos, 0, "SYSTEM")
            if res.get("status") not in ("OK", "ALREADY"):
                raise SystemDraftError(f"system pick rejected: {res}")
            if res["status"] == "OK":
                rosters[team_id].append({"role": res["player"]["role"], "bowling_type": res["player"]["bowling_type"]})
            picks[team_id] += 1


async def prepare_system_teams(rt: IplRuntime, tid: int) -> None:
    """Idempotent: make sure the AI franchises exist, are fully drafted and have a batting order.
    Errors propagate (no hidden fallback) so the worker's failure counter and logs show the real problem."""
    t = await rt.repo.get(tid)
    if not t or t["state"] in ("COMPLETED", "CANCELLED", "LOBBY"):
        return
    teams = await rt.repo.all_teams(tid)
    if sum(1 for x in teams if x["kind"] == "SYSTEM") < len(FRANCHISES):
        await rt.repo.begin_system_teams(tid, franchise_payload())
    prog = await rt.repo.system_progress(tid)
    if not prog or any(p.get("picks", 0) < 11 or not p.get("done") for p in prog):
        async with rt.ctx.lock(("ipl-system-draft", tid)):          # one system draft at a time per tournament
            await run_system_draft(rt, tid)
    await rt.repo.complete_system_teams(tid)
    for s_team in await rt.repo.all_teams(tid):
        if s_team["kind"] == "SYSTEM" and not s_team.get("batting_order_confirmed"):
            await rt.repo.batting_order_auto_assign(s_team["id"])
