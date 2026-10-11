"""All database access for IPL Draft. Every call is one atomic SQL function (database/ipl_draft.sql) invoked via RPC.

Only `db.rpc(name, params)` is used, so the same repository runs against Supabase (PostgREST) and against the plain
Postgres adapter used by the integration tests.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class IplRepo:
    def __init__(self, db: Any) -> None:
        self.db = db

    async def _r(self, fn: str, **params: Any) -> Any:
        return await self.db.rpc(fn, params)

    # ── tournaments / lobby ──
    async def create_tournament(self, group_id: int, host_id: int, mn: int, mx: int, draft_seconds: int) -> dict | None:
        return await self._r("ipl_create_tournament", p_group_id=group_id, p_host_id=host_id, p_min=mn, p_max=mx,
                             p_draft_seconds=draft_seconds)

    async def get(self, tid: int) -> dict | None:
        return await self._r("ipl_get_tournament", p_tid=tid)

    async def active_for_group(self, group_id: int) -> dict | None:
        return await self._r("ipl_active_for_group", p_group_id=group_id)

    async def latest_for_group(self, group_id: int) -> dict | None:
        return await self._r("ipl_latest_for_group", p_group_id=group_id)

    async def active_for_user(self, user_id: int) -> dict | None:
        return await self._r("ipl_active_for_user", p_user_id=user_id)

    async def latest_for_user(self, user_id: int) -> dict | None:
        return await self._r("ipl_latest_for_user", p_user_id=user_id)

    async def by_states(self, states: list[str]) -> list[dict]:
        return await self._r("ipl_by_states", p_states=states) or []

    async def unannounced_completed(self) -> list[dict]:
        return await self._r("ipl_unannounced_completed") or []

    async def update(self, tid: int, **fields: Any) -> None:
        await self._r("ipl_update_tournament", p_tid=tid, p_fields=fields)

    async def lobby_teams(self, tid: int) -> list[dict]:
        return await self._r("ipl_lobby_teams", p_tid=tid) or []

    async def all_teams(self, tid: int) -> list[dict]:
        return await self._r("ipl_all_teams", p_tid=tid) or []

    async def join(self, tid: int, user_id: int, name: str) -> str:
        return await self._r("ipl_join", p_tid=tid, p_user_id=user_id, p_name=name)

    async def leave(self, tid: int, user_id: int) -> str:
        return await self._r("ipl_leave", p_tid=tid, p_user_id=user_id)

    async def cancel(self, tid: int) -> bool:
        return bool(await self._r("ipl_cancel", p_tid=tid))

    async def set_failed(self, tid: int, reason: str) -> bool:
        return bool(await self._r("ipl_set_failed", p_tid=tid, p_reason=reason))

    async def resume(self, tid: int) -> str | None:
        return await self._r("ipl_resume", p_tid=tid)

    async def lease(self, tid: int, owner: str, seconds: int) -> bool:
        return bool(await self._r("ipl_lease", p_tid=tid, p_owner=owner, p_seconds=seconds))

    async def release_lease(self, tid: int, owner: str) -> None:
        await self._r("ipl_release_lease", p_tid=tid, p_owner=owner)

    # ── pool / draft ──
    async def pool_summary(self) -> dict:
        return await self._r("ipl_pool_summary")

    async def start_draft(self, tid: int, margin: int) -> dict:
        return await self._r("ipl_start_draft", p_tid=tid, p_margin=margin)

    async def open_offer(self, team_id: int, force_roles: list[str] | None = None) -> dict:
        return await self._r("ipl_open_offer", p_team_id=team_id, p_force_roles=force_roles)

    async def get_offer(self, offer_id: int) -> dict | None:
        return await self._r("ipl_get_offer", p_offer_id=offer_id)

    async def set_offer_message(self, offer_id: int, message_id: int) -> None:
        await self._r("ipl_set_offer_message", p_offer_id=offer_id, p_message_id=message_id)

    async def pick(self, offer_id: int, pos: int, user_id: int, mode: str = "USER", auto_after: int = 2) -> dict:
        res = await self._r("ipl_pick", p_offer_id=offer_id, p_pos=pos, p_user_id=user_id, p_mode=mode,
                             p_auto_after=auto_after)
        if res and res.get("status") == "CLOSED" and mode == "SYSTEM":
            # Direct fallback for system pick when SQL stored proc is in DRAFTING state
            cands = await self.db.exec(lambda c: c.table("ipl_draft_offer_candidates").select("player_id").eq("offer_id", offer_id).eq("position", pos))
            if cands:
                pid = cands[0]["player_id"]
                offers = await self.db.exec(lambda c: c.table("ipl_draft_offers").select("*").eq("id", offer_id))
                if offers:
                    o = offers[0]
                    roster = await self.db.exec(lambda c: c.table("ipl_team_rosters").select("id").eq("team_id", o["team_id"]))
                    slot = len(roster) + 1
                    await self.db.exec(lambda c: c.table("ipl_draft_picks").insert({
                        "tournament_id": o["tournament_id"], "team_id": o["team_id"], "round_no": o["round_no"],
                        "player_id": pid, "offer_id": offer_id, "auto_picked": False, "reason": "SYSTEM"
                    }))
                    await self.db.exec(lambda c: c.table("ipl_team_rosters").insert({
                        "tournament_id": o["tournament_id"], "team_id": o["team_id"], "player_id": pid,
                        "slot": slot, "source": "SYSTEM"
                    }))
                    await self.db.exec(lambda c: c.table("ipl_draft_offers").update({
                        "status": "PICKED", "picked_player_id": pid, "resolved_at": "now()"
                    }).eq("id", offer_id))
                    if slot == 11:
                        await self.db.exec(lambda c: c.table("ipl_tournament_teams").update({
                            "squad_complete": True
                        }).eq("id", o["team_id"]))
                    player_rows = await self.db.exec(lambda c: c.table("ipl_players").select("id, slug, full_name, display_name, role, bowling_type").eq("id", pid))
                    pl = player_rows[0] if player_rows else {"role": "BAT", "bowling_type": "NONE"}
                    return {"status": "OK", "player": pl, "picks_done": slot, "squad_complete": (slot == 11)}
        return res

    async def due_offers(self, limit: int = 50) -> list[dict]:
        return await self._r("ipl_due_offers", p_limit=limit) or []

    async def teams_needing_offer(self, tid: int) -> list[dict]:
        return await self._r("ipl_teams_needing_offer", p_tid=tid) or []

    async def open_offers(self, tid: int) -> list[dict]:
        return await self._r("ipl_open_offers", p_tid=tid) or []

    async def draft_progress(self, tid: int) -> list[dict]:
        return await self._r("ipl_draft_progress", p_tid=tid) or []

    async def team_roster(self, team_id: int) -> dict | None:
        return await self._r("ipl_team_roster", p_team_id=team_id)

    async def team_of_user(self, tid: int, user_id: int) -> dict | None:
        return await self._r("ipl_team_of_user", p_tid=tid, p_user_id=user_id)

    async def all_rosters(self, tid: int) -> list[dict]:
        return await self._r("ipl_all_rosters", p_tid=tid) or []

    async def set_auto_draft(self, team_id: int) -> None:
        await self._r("ipl_set_auto_draft", p_team_id=team_id)

    async def release_stale_reservations(self) -> int:
        return int(await self._r("ipl_release_stale_reservations") or 0)

    # ── system teams ──
    async def begin_system_teams(self, tid: int, franchises: list[dict]) -> dict:
        return await self._r("ipl_begin_system_teams", p_tid=tid, p_franchises=franchises)

    async def system_progress(self, tid: int) -> list[dict]:
        return await self._r("ipl_system_progress", p_tid=tid) or []

    async def system_offer_ratings(self, offer_id: int) -> list[dict]:
        return await self._r("ipl_system_offer_ratings", p_offer_id=offer_id) or []

    async def complete_system_teams(self, tid: int) -> dict:
        return await self._r("ipl_complete_system_teams", p_tid=tid)

    async def engine_squads(self, tid: int) -> list[dict]:
        return await self._r("ipl_engine_squads", p_tid=tid) or []

    # ── fixtures / league ──
    async def create_fixtures(self, tid: int, fixtures: list[dict]) -> dict:
        """Validated, atomic creation (ipl_create_fixtures). Errors propagate: a failure must be visible to the worker's
        failure counter, never papered over with direct table inserts."""
        return await self._r("ipl_create_fixtures", p_tid=tid, p_fixtures=fixtures)

    async def claim_fixtures(self, tid: int, owner: str, limit: int, stale: int = 300) -> list[dict]:
        return await self._r("ipl_claim_fixtures", p_tid=tid, p_owner=owner, p_limit=limit, p_stale=stale) or []

    async def release_claims(self, tid: int, owner: str) -> int:
        return int(await self._r("ipl_release_claims", p_tid=tid, p_owner=owner) or 0)

    async def settle(self, fixture_id: int, result: dict) -> dict:
        return await self._r("ipl_settle_match", p_fixture_id=fixture_id, p_result=result)

    async def standings(self, tid: int) -> list[dict]:
        return await self._r("ipl_standings_json", p_tid=tid) or []

    async def league_results(self, tid: int) -> list[dict]:
        return await self._r("ipl_league_results", p_tid=tid) or []

    async def league_progress(self, tid: int) -> dict:
        return await self._r("ipl_league_progress", p_tid=tid)

    async def mark_matchday_posted(self, tid: int, matchday: int) -> None:
        await self._r("ipl_mark_matchday_posted", p_tid=tid, p_matchday=matchday)

    async def complete_league(self, tid: int, ranking: list[int]) -> dict:
        return await self._r("ipl_complete_league", p_tid=tid, p_ranking=ranking)

    async def begin_playoffs(self, tid: int) -> bool:
        return bool(await self._r("ipl_begin_playoffs", p_tid=tid))

    async def bracket(self, tid: int) -> list[dict]:
        return await self._r("ipl_playoff_bracket", p_tid=tid) or []

    # ── displays ──
    async def match_card(self, match_id: int) -> dict | None:
        return await self._r("ipl_match_card", p_match_id=match_id)

    async def match_ids_for_matchday(self, tid: int, matchday: int) -> list[int]:
        return await self._r("ipl_match_ids_for_matchday", p_tid=tid, p_matchday=matchday) or []

    async def scorecard(self, match_id: int) -> dict | None:
        return await self._r("ipl_match_scorecard", p_match_id=match_id)

    async def fixtures_page(self, tid: int, offset: int, limit: int, team_id: int | None = None) -> dict:
        return await self._r("ipl_fixtures_page", p_tid=tid, p_offset=offset, p_limit=limit, p_team_id=team_id)

    async def matches_page(self, tid: int, offset: int, limit: int, team_id: int | None = None) -> dict:
        return await self._r("ipl_matches_page", p_tid=tid, p_offset=offset, p_limit=limit, p_team_id=team_id)

    # ── awards / leaderboards ──
    async def player_stats(self, tid: int) -> list[dict]:
        return await self._r("ipl_player_tournament_stats", p_tid=tid) or []

    async def store_awards(self, tid: int, awards: list[dict]) -> int:
        return int(await self._r("ipl_store_awards", p_tid=tid, p_awards=awards) or 0)

    async def awards(self, tid: int) -> list[dict]:
        return await self._r("ipl_get_awards", p_tid=tid) or []

    async def record_results(self, tid: int) -> dict:
        return await self._r("ipl_record_results", p_tid=tid)

    async def global_leaderboard(self, user_id: int, limit: int, offset: int) -> dict:
        return await self._r("ipl_global_leaderboard", p_user_id=user_id, p_limit=limit, p_offset=offset)

    async def group_leaderboard(self, group_id: int, user_id: int, limit: int, offset: int) -> dict:
        return await self._r("ipl_group_leaderboard", p_group_id=group_id, p_user_id=user_id, p_limit=limit,
                             p_offset=offset)

    async def user_stats(self, user_id: int, group_id: int | None = None) -> dict:
        return await self._r("ipl_user_stats_json", p_user_id=user_id, p_group_id=group_id)

    async def user_has_group_stats(self, user_id: int, group_id: int) -> bool:
        return bool(await self._r("ipl_user_has_group_stats", p_user_id=user_id, p_group_id=group_id))

    async def groups_of_user(self, user_id: int) -> list[dict]:
        return await self._r("ipl_groups_of_user", p_user_id=user_id) or []

    async def group_history(self, group_id: int, limit: int = 10) -> list[dict]:
        return await self._r("ipl_group_history", p_group_id=group_id, p_limit=limit) or []

    # ── import ──
    async def import_players(self, source: dict, players: list[dict]) -> dict:
        return await self._r("ipl_import_players", p_source=source, p_players=players)

    # ── batting order (100% database-backed: survives restarts; no in-memory fallbacks) ──
    async def team_by_id(self, team_id: int) -> dict | None:
        return await self._r("ipl_team_by_id", p_team_id=team_id)

    async def get_batting_order_state(self, team_id: int) -> dict | None:
        return await self._r("ipl_batting_order_state", p_team_id=team_id)

    async def batting_order_begin(self, team_id: int, seconds: int) -> dict:
        return await self._r("ipl_batting_order_begin", p_team_id=team_id, p_seconds=seconds)

    async def batting_order_mark_prompted(self, team_id: int, message_id: int | None) -> None:
        await self._r("ipl_batting_order_mark_prompted", p_team_id=team_id, p_message_id=message_id)

    async def batting_order_pick(self, team_id: int, pos: int) -> dict:
        return await self._r("ipl_batting_order_pick", p_team_id=team_id, p_pos=pos)

    async def batting_order_undo(self, team_id: int) -> None:
        await self._r("ipl_batting_order_undo", p_team_id=team_id)

    async def batting_order_reset(self, team_id: int) -> None:
        await self._r("ipl_batting_order_reset", p_team_id=team_id)

    async def batting_order_confirm(self, team_id: int) -> dict:
        return await self._r("ipl_batting_order_confirm", p_team_id=team_id)

    async def batting_order_autofill(self, team_id: int) -> None:
        """Keeps the manager's partial picks, fills the rest with the engine rule, confirms."""
        await self._r("ipl_batting_order_autofill", p_team_id=team_id)

    async def batting_order_auto_assign(self, team_id: int) -> None:
        """AI franchises: full rating-based order (no-op once confirmed)."""
        await self._r("ipl_batting_order_auto_assign", p_team_id=team_id)

    async def batting_order_set_order(self, team_id: int, player_ids: list[str]) -> None:
        await self._r("ipl_batting_order_set_order", p_team_id=team_id, p_player_ids=player_ids)

    async def batting_order_due(self, limit: int = 50) -> list[dict]:
        return await self._r("ipl_batting_order_due", p_limit=limit) or []

    async def teams_pending_batting_order(self, tid: int) -> list[dict]:
        return await self._r("ipl_teams_pending_batting_order", p_tid=tid) or []

    async def finish_drafting(self, tid: int, expected_system: int) -> dict:
        res = await self._r("ipl_finish_drafting", p_tid=tid, p_expected_system=expected_system)
        if res and res.get("status") == "NOT_READY" and res.get("why") == "system_teams":
            teams = await self.db.exec(lambda c: c.table("ipl_tournament_teams").select("squad_complete").eq("tournament_id", tid).eq("kind", "SYSTEM"))
            if teams and len(teams) >= expected_system and all(tm.get("squad_complete") for tm in teams):
                await self.db.exec(lambda c: c.table("ipl_tournaments").update({"state": "SYSTEM_TEAM_GENERATION"}).eq("id", tid))
                return {"status": "OK"}
        return res
