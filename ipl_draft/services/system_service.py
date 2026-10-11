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

        # Verify existing roster matches target 2026 squad
        target_names = SYSTEM_PLAYING_XI[code]
        target_pids = []
        for nm in target_names:
            p = by_name.get(normalize_name(nm))
            if p:
                target_pids.append(p["id"])
            else:
                log.warning("System team player not found in DB: %s", nm)

        roster = (await _db_call(
            lambda c: c.table("ipl_team_rosters").select("id, slot, player_id, batting_order").eq("team_id", team_id)
        )) or []
        current_pids = [r["player_id"] for r in sorted(roster, key=lambda x: x.get("batting_order") or x.get("slot") or 0)]

        # If roster is incomplete, mismatched, or contains non-2026 franchise players, repair it
        if current_pids != target_pids or not s_team.get("squad_complete") or not s_team.get("batting_order_confirmed"):
            if roster:
                await _db_call(lambda c: c.table("ipl_team_rosters").delete().eq("team_id", team_id))

            for idx, nm in enumerate(target_names, 1):
                p = by_name.get(normalize_name(nm))
                if not p:
                    continue
                pid = p["id"]
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

            await _db_call(
                lambda c: c.table("ipl_tournament_teams").update({
                    "squad_complete": True,
                    "batting_order_confirmed": True,
                }).eq("id", team_id)
            )

        # Validate Playing XI strictly against IPL rules
        from ..engine.team_builder import validate_system_team_squad
        squad_objs = [by_name.get(normalize_name(nm)) for nm in target_names if normalize_name(nm) in by_name]
        is_valid, reason = validate_system_team_squad(team_id, squad_objs, code)
        if not is_valid:
            log.error("SYSTEM SQUAD VALIDATION FAILED for %s: %s", code, reason)
            raise RuntimeError(f"System squad validation failed for {code}: {reason}")

    await rt.repo.complete_system_teams(tid)
