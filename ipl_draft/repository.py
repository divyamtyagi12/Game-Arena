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

    async def mark_squad_summary_sent(self, team_id: int) -> None:
        try:
            await self._r("ipl_mark_squad_summary_sent", p_team_id=team_id)
        except Exception:
            pass
        try:
            await self.db.exec(
                lambda c: c.table("ipl_tournament_teams")
                .update({"squad_summary_sent": True})
                .eq("id", team_id)
            )
        except Exception:
            pass

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
        try:
            res = await self._r("ipl_lease", p_tid=tid, p_owner=owner, p_seconds=seconds)
            if res:
                return True
        except Exception:
            pass

        # Fallback when RPC fails, returns false (e.g. outdated stored proc rejecting COMPLETED unannounced), or is unavailable: atomic conditional update
        try:
            from datetime import datetime, timezone, timedelta
            now_iso = datetime.now(timezone.utc).isoformat()
            until_iso = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
            rows = await self.db.exec(
                lambda c: c.table("ipl_tournaments")
                .update({"lease_owner": owner, "lease_until": until_iso})
                .eq("id", tid)
                .neq("state", "CANCELLED")
                .or_(f"lease_owner.is.null,lease_owner.eq.{owner},lease_until.lt.{now_iso}")
            )
            return bool(rows)
        except Exception:
            return False

    async def release_lease(self, tid: int, owner: str) -> None:
        try:
            await self._r("ipl_release_lease", p_tid=tid, p_owner=owner)
        except Exception:
            pass
        try:
            await self.db.exec(
                lambda c: c.table("ipl_tournaments")
                .update({"lease_owner": None, "lease_until": None})
                .eq("id", tid)
                .eq("lease_owner", owner)
            )
        except Exception:
            pass

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
        if isinstance(res, dict) and res.get("status") == "OK":
            if "done" not in res and "squad_complete" in res:
                res["done"] = res["squad_complete"]
            if "squad_complete" not in res and "done" in res:
                res["squad_complete"] = res["done"]
            if "all_humans_done" not in res and "humans_drafting" in res:
                res["all_humans_done"] = (res["humans_drafting"] == 0)
            if "humans_drafting" not in res and "all_humans_done" in res:
                res["humans_drafting"] = 0 if res["all_humans_done"] else 1
            if "auto" not in res:
                res["auto"] = mode in ("TIMEOUT", "AUTO")
            if "user_id" not in res:
                res["user_id"] = user_id
            if "team_id" not in res or "tournament_id" not in res:
                try:
                    offer = await self.get_offer(offer_id)
                    if offer and isinstance(offer, dict):
                        res.setdefault("team_id", offer.get("team_id"))
                        res.setdefault("tournament_id", offer.get("tournament_id"))
                        res.setdefault("round_no", offer.get("round_no"))
                except Exception:
                    pass
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

    async def get_legal_pool_players(self, tid: int, team_id: int, roles: list[str], limit: int = 4) -> list[dict]:
        """Fetch available players from tournament pool (or catalog) matching roles, excluding team roster."""
        try:
            # 1. Get drafted IDs for this team
            roster_res = await self.db.exec(lambda c: c.table("ipl_team_rosters").select("player_id").eq("team_id", team_id))
            drafted_pids = {str(r["player_id"]) for r in (roster_res or []) if r.get("player_id")}

            # 2. Get pool players for this tournament
            pool_res = await self.db.exec(lambda c: c.table("ipl_tournament_pool").select("player_id").eq("tournament_id", tid))
            pool_pids = [str(p["player_id"]) for p in (pool_res or []) if str(p.get("player_id")) not in drafted_pids]

            players = []
            if pool_pids:
                pls = await self.db.exec(
                    lambda c: c.table("ipl_players")
                    .select("id, slug, full_name, display_name, role, bowling_type, nationality, batting_style, bowling_style")
                    .in_("id", pool_pids)
                    .in_("role", roles)
                    .limit(limit)
                )
                if pls:
                    players.extend(pls)

            # 3. Fallback if pool is exhausted or missing roles
            if len(players) < limit:
                needed_cnt = limit - len(players)
                existing_ids = drafted_pids.union({str(p["id"]) for p in players})
                cat_pls = await self.db.exec(
                    lambda c: c.table("ipl_players")
                    .select("id, slug, full_name, display_name, role, bowling_type, nationality, batting_style, bowling_style")
                    .eq("is_eligible", True)
                    .in_("role", roles)
                    .limit(needed_cnt * 4)
                )
                for cp in (cat_pls or []):
                    if str(cp["id"]) not in existing_ids:
                        players.append(cp)
                        existing_ids.add(str(cp["id"]))
                        if len(players) >= limit:
                            break
            return players
        except Exception:
            log.exception("IPL get_legal_pool_players failed for tid=%s, team=%s, roles=%s", tid, team_id, roles)
            return []

    async def replace_offer_candidates(self, offer_id: int, player_ids: list[str]) -> list[dict]:
        """Replace the candidates of an open offer with the given player_ids, renumbering positions 1..N."""
        try:
            await self.db.exec(lambda c: c.table("ipl_draft_offer_candidates").delete().eq("offer_id", offer_id))
            rows = [{"offer_id": offer_id, "position": idx + 1, "player_id": pid} for idx, pid in enumerate(player_ids)]
            if rows:
                await self.db.exec(lambda c: c.table("ipl_draft_offer_candidates").insert(rows))
            try:
                await self.db.exec(lambda c: c.table("ipl_player_reservations").delete().eq("offer_id", offer_id))
            except Exception:
                pass
            res = await self.get_offer(offer_id)
            return (res.get("candidates") if isinstance(res, dict) else []) or []
        except Exception:
            log.exception("IPL replace_offer_candidates failed for offer_id=%s", offer_id)
            return []

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

    async def create_fixtures(self, tid: int, fixtures: list[dict]) -> dict:
        """Validated, idempotent creation of tournament fixtures (ipl_create_fixtures)."""
        res = await self._r("ipl_create_fixtures", p_tid=tid, p_fixtures=fixtures)
        if res and (res.get("status") in ("OK", "ALREADY") or res.get("already")):
            cur_t = await self.get(tid)
            if cur_t and cur_t.get("state") in ("FIXTURE_GENERATION", "FAILED_RECOVERABLE"):
                if cur_t.get("state") == "FAILED_RECOVERABLE":
                    await self.resume(tid)
                await self.db.exec(lambda c: c.table("ipl_tournaments").update({
                    "state": "LEAGUE_RUNNING", "league_started_at": "now()", "resume_state": None, "failure_reason": None
                }).eq("id", tid))
            return res

        # Resilient handling if live DB stored proc has an outdated team count check (e.g. n < 8 or n < 10)
        # or if tournament is recovering from FAILED_RECOVERABLE (BAD_STATE)
        if res and (res.get("status") == "BAD_TEAM_COUNT" or (res.get("status") == "BAD_STATE" and res.get("state") == "FAILED_RECOVERABLE")):
            teams = await self.all_teams(tid)
            if 6 <= len(teams) <= 16:
                existing_check = await self.db.exec(lambda c: c.table("ipl_fixtures").select("id").eq("tournament_id", tid).limit(1))
                if existing_check:
                    return {"status": "OK", "already": True}
                rows = [
                    {
                        "tournament_id": tid,
                        "stage": "LEAGUE",
                        "match_no": int(f["match_no"]),
                        "matchday": int(f["matchday"]),
                        "leg": int(f["leg"]),
                        "home_team_id": int(f["home"]),
                        "away_team_id": int(f["away"]),
                    }
                    for f in fixtures
                ]
                await self.db.exec(lambda c: c.table("ipl_fixtures").insert(rows))
                standings_rows = [{"tournament_id": tid, "team_id": tm["id"]} for tm in teams]
                await self.db.exec(lambda c: c.table("ipl_standings").upsert(standings_rows))

                # If currently in FAILED_RECOVERABLE, resume to resume_state first per DB trigger
                cur_t = await self.get(tid)
                if cur_t and cur_t.get("state") == "FAILED_RECOVERABLE":
                    await self.resume(tid)

                await self.db.exec(lambda c: c.table("ipl_tournaments").update({
                    "state": "LEAGUE_RUNNING", "league_started_at": "now()", "resume_state": None, "failure_reason": None
                }).eq("id", tid))
                return {"status": "OK", "fixtures": len(fixtures), "teams": len(teams)}

        return res

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
        if res and res.get("status") in ("OK", "ALREADY"):
            return res
        if res and res.get("status") == "NOT_READY" and res.get("why") == "system_teams":
            teams = await self.db.exec(lambda c: c.table("ipl_tournament_teams").select("squad_complete").eq("tournament_id", tid).eq("kind", "SYSTEM"))
            if teams and len(teams) >= expected_system and all(tm.get("squad_complete") for tm in teams):
                await self.db.exec(lambda c: c.table("ipl_tournaments").update({"state": "SYSTEM_TEAM_GENERATION"}).eq("id", tid))
                return {"status": "OK"}
        return res
