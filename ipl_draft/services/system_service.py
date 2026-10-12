"""System franchises use official predefined IPL 2026 player squads — never participating in human drafts."""
from __future__ import annotations

import inspect
import logging
from typing import Any, Sequence

from ..data.names import normalize_name
from ..engine.team_builder import FRANCHISES, SYSTEM_PLAYING_XI, franchise_payload, validate_system_team_squad
from ..runtime import IplRuntime

log = logging.getLogger(__name__)


class SystemDraftError(RuntimeError):
    """Raised when system team preparation or persistence validation fails."""
    pass


async def _exec_db(rt: IplRuntime, fn) -> Any:
    """Helper to support both async database execution and synchronous test mock functions."""
    res = rt.repo.db.exec(fn)
    return await res if inspect.isawaitable(res) else res


async def _load_all_players(rt: IplRuntime) -> list[dict]:
    """Fetch players with explicit pagination to safely support player pools exceeding 1,000 rows."""
    all_players: list[dict] = []
    page_size = 1000
    start = 0

    while True:
        try:
            page = await _exec_db(
                rt,
                lambda c: c.table("ipl_players")
                .select("id, display_name, full_name, role, bowling_type, nationality")
                .range(start, start + page_size - 1)
            )
        except Exception as exc:
            log.exception("Database error loading ipl_players page (range %d-%d): %s", start, start + page_size - 1, exc)
            raise SystemDraftError(f"Database error loading ipl_players: {exc}") from exc

        if not page:
            break
        all_players.extend(page)
        if len(page) < page_size:
            break
        start += page_size

    return all_players


async def prepare_system_teams(rt: IplRuntime, tid: int) -> None:
    """Idempotent: Ensure the 4 AI franchises exist, are populated with their official
    predefined IPL 2026 squads, and have confirmed batting orders.
    Validates actual database-persisted rosters with rollback on failure.
    """
    t = await rt.repo.get(tid)
    if not t or t["state"] in ("COMPLETED", "CANCELLED", "LOBBY"):
        return

    teams = await rt.repo.all_teams(tid)
    if sum(1 for x in teams if x["kind"] == "SYSTEM") < len(FRANCHISES):
        await rt.repo.begin_system_teams(tid, franchise_payload())
        teams = await rt.repo.all_teams(tid)

    # Load players from DB with explicit pagination (never assume <= 1,000 rows)
    all_players = await _load_all_players(rt)
    if not all_players:
        raise SystemDraftError("ipl_players table is empty or unreachable")

    by_name: dict[str, dict] = {}
    by_id: dict[str, dict] = {}
    for p in all_players:
        by_id[str(p["id"])] = p
        by_name[normalize_name(p["display_name"])] = p
        if p.get("full_name"):
            by_name[normalize_name(p["full_name"])] = p

    for s_team in teams:
        if s_team["kind"] != "SYSTEM":
            continue
        code = s_team.get("franchise_code")
        if not code or code not in SYSTEM_PLAYING_XI:
            continue
        team_id = s_team["id"]

        target_names = SYSTEM_PLAYING_XI[code]
        target_pids: list[str] = []
        missing_names: list[str] = []
        for nm in target_names:
            p = by_name.get(normalize_name(nm))
            if p:
                target_pids.append(str(p["id"]))
            else:
                missing_names.append(nm)

        if missing_names or len(target_pids) != 11:
            err_msg = f"System team {code} missing required players in DB: {missing_names}"
            log.error(err_msg)
            raise SystemDraftError(err_msg)

        # Check existing persisted roster
        try:
            roster = await _exec_db(
                rt,
                lambda c: c.table("ipl_team_rosters")
                .select("id, slot, player_id, batting_order")
                .eq("team_id", team_id)
            ) or []
        except Exception as exc:
            log.exception("Failed to query ipl_team_rosters for team %s: %s", team_id, exc)
            raise SystemDraftError(f"Database error reading roster for team {team_id}: {exc}") from exc

        current_pids = [str(r["player_id"]) for r in sorted(roster, key=lambda x: x.get("batting_order") or x.get("slot") or 0)]

        # If roster is incomplete, mismatched, or not confirmed, rebuild it atomically
        needs_rebuild = (
            current_pids != target_pids
            or not s_team.get("squad_complete")
            or not s_team.get("batting_order_confirmed")
        )

        if needs_rebuild:
            log.info("Rebuilding system roster for %s (team %s)", code, team_id)
            # Try atomic RPC first if available
            rpc_worked = False
            if hasattr(rt.repo, "_r"):
                try:
                    res = await rt.repo._r("ipl_set_system_squad", p_tid=tid, p_team_id=team_id, p_player_ids=target_pids)
                    if res and res.get("status") == "OK":
                        rpc_worked = True
                except Exception:
                    rpc_worked = False

            if not rpc_worked:
                # Perform atomic-style replacement with cleanup on error
                try:
                    if roster:
                        await _exec_db(rt, lambda c: c.table("ipl_team_rosters").delete().eq("team_id", team_id))

                    rows = [
                        {
                            "tournament_id": tid,
                            "team_id": team_id,
                            "player_id": pid,
                            "slot": idx,
                            "batting_order": idx,
                            "source": "SYSTEM",
                        }
                        for idx, pid in enumerate(target_pids, 1)
                    ]
                    await _exec_db(rt, lambda c: c.table("ipl_team_rosters").insert(rows))

                    await _exec_db(
                        rt,
                        lambda c: c.table("ipl_tournament_teams").update({
                            "squad_complete": True,
                            "batting_order_confirmed": True,
                        }).eq("id", team_id)
                    )
                except Exception as exc:
                    log.exception("Failed to insert system roster for %s: %s", code, exc)
                    # Attempt cleanup of partial roster
                    try:
                        await _exec_db(rt, lambda c: c.table("ipl_team_rosters").delete().eq("team_id", team_id))
                        await _exec_db(rt, lambda c: c.table("ipl_tournament_teams").update({
                            "squad_complete": False,
                            "batting_order_confirmed": False,
                        }).eq("id", team_id))
                    except Exception:
                        pass
                    raise SystemDraftError(f"Database error writing system squad for {code}: {exc}") from exc

        # ── Verify the ACTUAL PERSISTED database roster, NOT in-memory objects ──
        try:
            persisted_roster = await _exec_db(
                rt,
                lambda c: c.table("ipl_team_rosters")
                .select("id, slot, player_id, batting_order")
                .eq("team_id", team_id)
            ) or []
        except Exception as exc:
            raise SystemDraftError(f"Database error verifying persisted roster: {exc}") from exc

        if len(persisted_roster) != 11:
            err = f"Persisted roster for {code} has {len(persisted_roster)} players (expected 11)"
            log.error(err)
            raise SystemDraftError(err)

        persisted_pids = [str(r["player_id"]) for r in persisted_roster]
        if len(set(persisted_pids)) != 11:
            err = f"Persisted roster for {code} contains duplicate player IDs: {persisted_pids}"
            log.error(err)
            raise SystemDraftError(err)

        persisted_players = [by_id.get(pid) for pid in persisted_pids]
        if any(p is None for p in persisted_players):
            err = f"Persisted player ID not found in catalog for {code}"
            log.error(err)
            raise SystemDraftError(err)

        # Validate strictly against official Playing XI and IPL rules
        is_valid, reason = validate_system_team_squad(team_id, persisted_players, code)
        if not is_valid:
            log.error("SYSTEM SQUAD VALIDATION FAILED for %s on persisted data: %s", code, reason)
            raise SystemDraftError(f"System squad validation failed for {code}: {reason}")

        # Verify role constraints: minimum 3 bowlers, 1 keeper, 1 all-rounder
        b_count = sum(1 for p in persisted_players if p.get("role") == "BOWL")
        wk_count = sum(1 for p in persisted_players if p.get("role") == "WK" or p.get("display_name") in ("MS Dhoni", "Sanju Samson", "Phil Salt", "Ryan Rickelton", "Finn Allen"))
        ar_count = sum(1 for p in persisted_players if p.get("role") == "AR")

        if b_count < 3 or wk_count < 1 or ar_count < 1:
            err = f"Persisted system squad for {code} does not meet role minimums: {b_count} bowlers, {wk_count} keepers, {ar_count} all-rounders"
            log.error(err)
            raise SystemDraftError(err)

    await rt.repo.complete_system_teams(tid)
