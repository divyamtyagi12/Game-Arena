"""System franchises use official predefined IPL 2026 player squads — never participating in human drafts."""
from __future__ import annotations

import logging
from ..data.names import normalize_name
from ..engine.team_builder import FRANCHISES, SYSTEM_PLAYING_XI, franchise_payload
from ..runtime import IplRuntime

log = logging.getLogger(__name__)


class SystemDraftError(RuntimeError):
    pass


async def prepare_system_teams(rt: IplRuntime, tid: int) -> None:
    """Idempotent: Ensure the 4 AI franchises exist, are populated with their official
    predefined IPL 2026 squads, and have confirmed batting orders."""
    t = await rt.repo.get(tid)
    if not t or t["state"] in ("COMPLETED", "CANCELLED", "LOBBY"):
        return
    teams = await rt.repo.all_teams(tid)
    if sum(1 for x in teams if x["kind"] == "SYSTEM") < len(FRANCHISES):
        await rt.repo.begin_system_teams(tid, franchise_payload())
        teams = await rt.repo.all_teams(tid)

    async def _db_call(fn):
        if not hasattr(rt.repo, "db") or not hasattr(rt.repo.db, "exec"):
            return []
        try:
            res = rt.repo.db.exec(fn)
            return (await res) if hasattr(res, "__await__") else res
        except Exception:
            return []

    # Cache players from DB for fast lookup
    all_players = (await _db_call(
        lambda c: c.table("ipl_players").select("id, display_name, full_name, role, bowling_type")
    )) or []
    by_name = {normalize_name(p["display_name"]): p for p in all_players}
    for p in all_players:
        by_name[normalize_name(p["full_name"])] = p

    for s_team in teams:
        if s_team["kind"] != "SYSTEM":
            continue
        code = s_team.get("franchise_code")
        if not code or code not in SYSTEM_PLAYING_XI:
            continue
        team_id = s_team["id"]

        # Check existing roster
        roster = (await _db_call(
            lambda c: c.table("ipl_team_rosters").select("id, slot, player_id").eq("team_id", team_id)
        )) or []
        if len(roster) < 11 or not s_team.get("squad_complete") or not s_team.get("batting_order_confirmed"):
            names = SYSTEM_PLAYING_XI[code]
            roster_pids = {r["player_id"] for r in roster}
            for idx, nm in enumerate(names, 1):
                p = by_name.get(normalize_name(nm))
                if not p:
                    log.warning("System team player not found: %s", nm)
                    continue
                pid = p["id"]
                if pid not in roster_pids:
                    await _db_call(
                        lambda c: c.table("ipl_team_rosters").insert({
                            "tournament_id": tid,
                            "team_id": team_id,
                            "player_id": pid,
                            "slot": idx,
                            "batting_order": idx,
                            "source": "SYSTEM",
                        })
                    )
                    roster_pids.add(pid)
                else:
                    await _db_call(
                        lambda c: c.table("ipl_team_rosters").update({
                            "batting_order": idx,
                            "slot": idx,
                        }).eq("team_id", team_id).eq("player_id", pid)
                    )
            await _db_call(
                lambda c: c.table("ipl_tournament_teams").update({
                    "squad_complete": True,
                    "batting_order_confirmed": True,
                }).eq("id", team_id)
            )

    await rt.repo.complete_system_teams(tid)
