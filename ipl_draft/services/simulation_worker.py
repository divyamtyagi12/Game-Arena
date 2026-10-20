"""Tournament progression worker: system teams → fixtures → league → playoffs → awards/leaderboards.

* one driver per tournament at a time (in-process set + PostgreSQL lease);
* every step is idempotent and persisted, so a crash/restart resumes from the database state;
* matches use deterministic per-fixture seeds → a retry after a crash reproduces the same match;
* the group only ever sees an edited dashboard, a compact summary every few matchdays and the playoff cards.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import random
import time

from .. import keyboards as kb
from ..keyboards import Btn, Markup
from .. import messages as m
from ..engine import awards as awards_engine
from ..engine.fixtures import double_round_robin, validate as validate_fixtures
from ..engine.match_engine import simulate_match, to_payload
from ..engine.model import Squad
from ..engine.playoffs import STATE_FOR_STAGE
from ..engine.standings import rank_teams
from ..engine.team_builder import FRANCHISES
from ..runtime import IplRuntime
from . import batting_order_service, draft_service, notification_service as notify, system_service

log = logging.getLogger(__name__)
ADVANCEABLE = ["DRAFTING", "SYSTEM_TEAM_GENERATION", "FIXTURE_GENERATION", "LEAGUE_RUNNING", "LEAGUE_COMPLETED",
               "PLAYOFF_ELIMINATOR", "PLAYOFF_QUALIFIER_1", "PLAYOFF_QUALIFIER_2", "PLAYOFF_FINAL"]
PLAYOFF_STATES = {v for v in STATE_FOR_STAGE.values()}


def match_rng(rt: IplRuntime, tid: int, fixture_id: int, started: str | None) -> tuple[random.Random, str]:
    seed = hashlib.sha256(f"{tid}:{fixture_id}:{started}:{rt.s.seed_salt}".encode()).hexdigest()[:16]
    return random.Random(int(seed, 16)), seed


async def get_squads(rt: IplRuntime, tid: int) -> dict[int, Squad]:
    if tid not in rt.squads:
        rows = await rt.repo.engine_squads(tid)
        rt.squads[tid] = {int(r["team_id"]): Squad.from_rpc(r) for r in rows}
    return rt.squads[tid]


# ───────────────────────── driver ─────────────────────────
async def advance(rt: IplRuntime, tid: int) -> None:
    """Drive one tournament as far as it can go without waiting for humans."""
    if tid in rt.running:
        return
    rt.running.add(tid)
    try:
        if not await rt.repo.lease(tid, rt.worker_id, 180):
            return
        for _ in range(10_000):
            t = await rt.repo.get(tid)
            if not t:
                return
            handler = HANDLERS.get(t["state"])
            if handler is None:
                break
            if not await handler(rt, t):
                break
            await rt.repo.lease(tid, rt.worker_id, 180)
        rt.failures.pop(tid, None)
        rt.retry_after.pop(tid, None)
    except Exception as exc:  # noqa: BLE001
        n = rt.failures.get(tid, 0) + 1
        rt.failures[tid] = n
        log.exception("IPL advance failed for tournament %s (%d/%d)", tid, n, rt.s.max_failures)
        await rt.repo.release_claims(tid, rt.worker_id)

        # Exponential backoff for automatic retry
        backoff = min(60.0, 1.0 * (2 ** min(n - 1, 6)))
        rt.retry_after[tid] = time.monotonic() + backoff
        log.info("Tournament %s scheduled for automatic retry in %.1fs (attempt %d/%d)", tid, backoff, n, rt.s.max_failures)

        if n >= rt.s.max_failures:
            t = await rt.repo.get(tid)
            err_msg = f"{type(exc).__name__}: {exc}"
            if t and await rt.repo.set_failed(tid, err_msg):
                await notify.group(
                    rt,
                    t["group_id"],
                    f"⚠️ <b>IPL Tournament Paused</b>\n\n"
                    f"The tournament encountered an unexpected error and paused automatically:\n"
                    f"<code>{m.esc(err_msg)}</code>\n\n"
                    f"Tournament progress is saved in the database."
                )
    finally:
        rt.running.discard(tid)
        try:
            await rt.repo.release_lease(tid, rt.worker_id)
        except Exception:  # noqa: BLE001
            pass


# ───────────────────────── state handlers (return True = made progress, loop again) ─────────────────────────
async def _drafting(rt: IplRuntime, t: dict) -> bool:
    """Waits (returns False) until every human has drafted their 11-player squad,
    verifies squad legality, autofills any unconfirmed batting orders, prepares system teams,
    and automatically finishes drafting.
    """
    tid = t["id"]
    prog = await rt.repo.draft_progress(tid)
    if not prog or not all(p["done"] for p in prog):
        return False

    # 1. Verify every human squad contains exactly 11 valid players with all required roles
    from ..engine.draft_engine import validate_squad_legality
    all_teams = await rt.repo.all_teams(tid)
    human_teams = [tm for tm in all_teams if tm.get("kind") == "HUMAN"]
    for tm in human_teams:
        roster = await rt.repo.team_roster(tm["id"])
        players = roster.get("players", []) if roster else []
        if len(players) != 11:
            log.warning("Human team %s has %d/11 players; waiting for completion", tm["id"], len(players))
            return False
        is_legal, reason = validate_squad_legality(players)
        if not is_legal:
            raise RuntimeError(f"Human squad validation failed for team {tm['id']} ({tm.get('name')}): {reason}")

    # 2. Wait for human managers to complete and confirm their batting orders.
    # batting_order_tick handles deadline timeouts automatically via rt.s.batting_order_seconds.
    if await rt.repo.teams_pending_batting_order(tid):
        return False

    # 3. Ensure all system teams are prepared with valid 11-player squads
    await system_service.prepare_system_teams(rt, tid)

    # 4. Finish drafting atomically
    res = await rt.repo.finish_drafting(tid, len(FRANCHISES))
    st = res.get("status")
    if st in ("OK", "ALREADY"):
        return True
    if st != "NOT_READY":
        raise RuntimeError(f"finish_drafting: {res}")
    return False


async def _system(rt: IplRuntime, t: dict) -> bool:
    tid = t["id"]
    await system_service.prepare_system_teams(rt, tid)
    res = await rt.repo.complete_system_teams(tid)
    if res.get("status") not in ("OK", "ALREADY"):
        raise RuntimeError(f"complete_system_teams: {res}")
    if not t.get("system_announced"):
        teams = [x for x in await rt.repo.all_teams(tid) if x["kind"] == "SYSTEM"]
        await notify.set_dashboard(rt, t, m.system_draft_text(teams), kb.teams_ready(tid))
        await rt.repo.update(tid, system_announced=True)
    return True


async def _fixtures(rt: IplRuntime, t: dict) -> bool:
    tid = t["id"]
    teams = sorted(await rt.repo.all_teams(tid), key=lambda x: x["team_no"])
    ids = [x["id"] for x in teams]
    fx = double_round_robin(ids)
    validate_fixtures(fx, ids)
    res = await rt.repo.create_fixtures(tid, fx)
    if res.get("status") != "OK":
        raise RuntimeError(f"create_fixtures: {res}")

    # Automatic tournament start announcement
    start_msg = (
        "✅ <b>All squads completed!</b>\n\n"
        "🏏 <b>IPL Tournament is starting!</b>\n\n"
        "⚔️ League matches will begin automatically.\n"
        "🏆 Good luck to all teams!"
    )
    await notify.group(rt, t["group_id"], start_msg)
    return True


async def _simulate(rt: IplRuntime, t: dict, fx: dict, squads: dict[int, Squad]) -> dict:
    tid = t["id"]
    teams_list = await rt.repo.all_teams(tid)
    teams_map = {x["id"]: x for x in teams_list}
    h_team = teams_map.get(fx["home"])
    a_team = teams_map.get(fx["away"])
    
    # Identify participating human managers
    humans = [tm for tm in (h_team, a_team) if tm and tm["kind"] == "HUMAN" and tm.get("user_id")]

    dm_msgs: dict[int, int] = {}  # user_id -> message_id
    if humans:
        stage_label = m.STAGE_LABEL.get(fx.get("stage", "LEAGUE"), "League").upper()
        h_name = h_team["name"] if h_team else "Home"
        a_name = a_team["name"] if a_team else "Away"
        init_text = (f"🏏 <b>IPL DRAFT — {stage_label}</b>\n\n"
                     f"<b>{m.esc(h_name)}</b> vs <b>{m.esc(a_name)}</b>\n\n"
                     "🪙 <i>Toss underway &amp; players taking the field…</i>")
        for h in humans:
            try:
                sent = await notify.dm(rt, h["user_id"], init_text)
                if sent:
                    dm_msgs[h["user_id"]] = sent.message_id
            except Exception:  # noqa: BLE001
                pass

    # ── Strict validation: verify system teams before match simulation ──
    from ..engine.team_builder import validate_system_team_squad
    for tm, side in [(h_team, fx["home"]), (a_team, fx["away"])]:
        if tm and tm.get("kind") == "SYSTEM":
            code = tm.get("franchise_code")
            sq = squads.get(side)
            if not sq or not sq.players:
                log.error("Missing squad players for system team %s (%s)", tm["id"], code)
                return {"status": "INVALID_SYSTEM_SQUAD", "team_id": tm["id"]}
            ok, reason = validate_system_team_squad(tm["id"], sq.players, code)
            if not ok:
                log.warning("SYSTEM SQUAD VALIDATION FAILED for team %s (%s): %s — attempting repair", tm["id"], code, reason)
                try:
                    await system_service.prepare_system_teams(rt, tid)
                    rt.squads.pop(tid, None)
                    fresh_squads = await get_squads(rt, tid)
                    sq = fresh_squads.get(side)
                    if sq and sq.players:
                        ok, reason = validate_system_team_squad(tm["id"], sq.players, code)
                except Exception:
                    log.exception("Failed to repair system team %s (%s)", tm["id"], code)
                if not ok:
                    log.error("SYSTEM SQUAD VALIDATION FAILED for team %s (%s): %s", tm["id"], code, reason)
                    return {"status": "INVALID_SYSTEM_SQUAD", "reason": reason, "team_id": tm["id"]}

    rng, seed = match_rng(rt, tid, fx["id"], t.get("draft_started_at"))
    out = simulate_match(squads[fx["home"]], squads[fx["away"]], rng, seed=seed)

    # Live simulated progress update in DM
    if humans and dm_msgs:
        try:
            inn1 = out.innings[0]
            progress_text = (f"🏏 <b>IPL DRAFT — {fx.get('stage', 'LEAGUE').upper()}</b>\n\n"
                             f"<b>{m.esc(out.team1.name)}</b>: {inn1.runs}/{inn1.wickets} ({m.overs_str(inn1.legal_balls)} ov)\n"
                             f"🎯 <b>{m.esc(out.team2.name)}</b> chasing {inn1.runs + 1}…")
            for h in humans:
                mid = dm_msgs.get(h["user_id"])
                if mid:
                    await notify.edit_dm(rt, h["user_id"], mid, progress_text)
            await rt.pause(0.8)
        except Exception:  # noqa: BLE001
            pass

    res = await rt.repo.settle(fx["id"], to_payload(out, store_balls=rt.s.store_ball_events))
    if res.get("status") not in ("OK", "ALREADY"):
        raise RuntimeError(f"settle {fx['id']}: {res}")

    # Deliver final compact scorecard automatically to each human user's DM — no buttons needed
    if humans and res.get("match_id"):
        try:
            card = await rt.repo.scorecard(res["match_id"])
            if card:
                for h in humans:
                    uid = h["user_id"]
                    your_team_id = h["id"]
                    mid = dm_msgs.get(uid)
                    text = m.format_compact_scorecard(card, fx.get("stage", "LEAGUE"), your_team_id)
                    if mid:
                        if not await notify.edit_dm(rt, uid, mid, text):
                            await notify.dm(rt, uid, text)
                    else:
                        await notify.dm(rt, uid, text)
        except Exception:  # noqa: BLE001
            log.exception("Failed to deliver compact scorecard DM for match %s", res.get("match_id"))

    return res


async def _league(rt: IplRuntime, t: dict) -> bool:
    tid = t["id"]
    squads = await get_squads(rt, tid)
    batch = max(1, len(squads) // 2)
    claimed = await rt.repo.claim_fixtures(tid, rt.worker_id, batch)
    if not claimed:
        prog = await rt.repo.league_progress(tid)
        if prog["done"] < prog["total"]:
            return False                                           # claimed elsewhere; try again next tick
        standings = await rt.repo.standings(tid)
        ranking = rank_teams(standings, await rt.repo.league_results(tid))
        res = await rt.repo.complete_league(tid, ranking)
        if res.get("status") != "OK":
            raise RuntimeError(f"complete_league: {res}")
        return True
    for fx in claimed:
        res = await _simulate(rt, t, fx, squads)
        if res.get("status") not in ("OK", "ALREADY"):
            log.error("League fixture %s failed: %s", fx.get("id"), res)
            raise RuntimeError(f"Simulation failed for league fixture {fx.get('id')}: {res.get('status')} ({res.get('reason', '')})")
    # Fix 1 & 2: Do NOT post intermediate matchday summaries or points tables to group
    await rt.pause(rt.s.matchday_delay)
    return True


async def compute_human_mvps(rt: IplRuntime, tid: int) -> dict:
    stats = await rt.repo.player_stats(tid)
    teams = {x["id"]: x for x in await rt.repo.all_teams(tid)}
    human_team_ids = {tm["id"] for tm in teams.values() if tm.get("kind") == "HUMAN"}
    human_stats = [s for s in stats if s.get("team_id") in human_team_ids]

    mvps: dict[str, dict] = {}
    if human_stats:
        best_run = max(human_stats, key=lambda s: (int(s.get("runs") or 0), -int(s.get("balls") or 0)))
        if int(best_run.get("runs") or 0) > 0:
            tm = teams.get(best_run.get("team_id"), {})
            mvps["most_runs"] = {
                "player": best_run.get("name", "Player"),
                "runs": int(best_run.get("runs") or 0),
                "team": tm.get("name", "Human Team"),
            }
        best_wkt = max(human_stats, key=lambda s: (int(s.get("wickets") or 0), -int(s.get("bowl_runs") or 0)))
        if int(best_wkt.get("wickets") or 0) > 0:
            tm = teams.get(best_wkt.get("team_id"), {})
            mvps["most_wickets"] = {
                "player": best_wkt.get("name", "Player"),
                "wickets": int(best_wkt.get("wickets") or 0),
                "team": tm.get("name", "Human Team"),
            }
    return mvps


async def update_group_tournament_summary(rt: IplRuntime, t: dict) -> None:
    tid = t["id"]
    fresh_t = await rt.repo.get(tid) or t
    standings = await rt.repo.standings(tid)
    bracket = await rt.repo.bracket(tid)
    teams = {x["id"]: x for x in await rt.repo.all_teams(tid)}

    STAGE_ORDER = ["QUALIFIER_1", "ELIMINATOR", "QUALIFIER_2", "FINAL"]
    sorted_bracket = sorted(
        [b for b in bracket if b.get("status") == "COMPLETED" and b.get("match_id")],
        key=lambda b: STAGE_ORDER.index(b["stage"]) if b["stage"] in STAGE_ORDER else 99
    )

    playoff_results = []
    for b in sorted_bracket:
        card = await rt.repo.match_card(b["match_id"])
        if not card:
            continue
        w_id = card.get("winner_team_id")
        t1 = card.get("team1") or {}
        t2 = card.get("team2") or {}
        winner_name = (t1.get("name") if w_id == t1.get("id") else t2.get("name")) or "Winner"
        inn = card.get("innings") or []
        inn1_score = f"{inn[0]['runs']}/{inn[0]['wickets']}" if len(inn) > 0 else "0/0"
        inn2_score = f"{inn[1]['runs']}/{inn[1]['wickets']}" if len(inn) > 1 else "0/0"
        playoff_results.append({
            "stage": b["stage"],
            "winner_name": winner_name,
            "inn1_score": inn1_score,
            "inn2_score": inn2_score,
        })

    champ = None
    mvps = None
    if fresh_t.get("champion_team_id"):
        champ = teams.get(fresh_t["champion_team_id"])
        mvps = await compute_human_mvps(rt, tid)
    elif any(p["stage"] == "FINAL" for p in playoff_results):
        final_b = next((b for b in sorted_bracket if b["stage"] == "FINAL"), None)
        if final_b and final_b.get("winner_team_id"):
            champ = teams.get(final_b["winner_team_id"])
            mvps = await compute_human_mvps(rt, tid)

    summary_text = m.format_group_tournament_summary(
        standings=standings,
        playoff_results=playoff_results,
        champ=champ,
        mvps=mvps,
    )
    await notify.set_group_summary(rt, fresh_t, summary_text)


async def _league_completed(rt: IplRuntime, t: dict) -> bool:
    tid = t["id"]
    fresh_t = await rt.repo.get(tid) or t
    standings = await rt.repo.standings(tid)

    if not fresh_t.get("league_summary_posted"):
        # Message 1 pops: Points Table
        await notify.group(rt, t["group_id"], m.format_group_points_table(standings))
        # Message 2 pops: Playoff structure
        await notify.group(rt, t["group_id"], m.playoff_structure_text())
        await rt.repo.update(tid, league_summary_posted=True)

    if not fresh_t.get("league_dms_sent"):
        rank = {r["team_id"]: r["rank"] for r in standings}
        for tm in await rt.repo.all_teams(tid):
            if tm["kind"] == "HUMAN":
                r = rank.get(tm["id"])
                tail = "🔥 You're in the playoffs!" if r and r <= 4 else "Better luck next time!"
                await notify.dm(rt, tm["user_id"], f"🏟 <b>League stage finished</b>\n\n{m.esc(tm['name'])} finished <b>#{r}</b>. {tail}")
        await rt.repo.update(tid, league_dms_sent=True)

    await rt.pause(rt.s.playoff_delay)
    return await rt.repo.begin_playoffs(tid)


async def _playoff(rt: IplRuntime, t: dict) -> bool:
    tid = t["id"]
    squads = await get_squads(rt, tid)
    claimed = await rt.repo.claim_fixtures(tid, rt.worker_id, 1)
    if not claimed:
        return False
    fx = claimed[0]
    teams = {x["id"]: x for x in await rt.repo.all_teams(tid)}
    home_tm = teams.get(fx["home"])
    away_tm = teams.get(fx["away"])
    if not home_tm or not away_tm:
        log.error("Invalid playoff fixture teams: home=%s away=%s", fx.get("home"), fx.get("away"))
        return False

    res = await _simulate(rt, t, fx, squads)
    if res.get("status") not in ("OK", "ALREADY"):
        log.error("Playoff fixture %s failed: %s", fx.get("id"), res)
        raise RuntimeError(f"Simulation failed for playoff fixture {fx.get('id')}: {res.get('status')} ({res.get('reason', '')})")

    if res.get("status") == "OK" and res.get("match_id"):
        # Match result message pops into group one by one (Image 1 style)
        card = await rt.repo.scorecard(res["match_id"])
        if card:
            line = m.format_playoff_result_line(fx.get("stage", ""), card)
            await notify.group(rt, t["group_id"], line)

    await rt.pause(rt.s.playoff_delay)
    return True


async def _completed(rt: IplRuntime, t: dict) -> bool:
    await finalize(rt, t["id"])
    return False


async def finalize(rt: IplRuntime, tid: int) -> None:
    """Awards + leaderboard + champion announcement.
    Concurrency-safe and retry-durable. State transitions are tracked step-by-step
    so Telegram deliveries are not repeated on restarts or worker retries.
    """
    async with rt.ctx.lock(("ipl-finalize", tid)):
        t = await rt.repo.get(tid)
        if not t or t["state"] != "COMPLETED":
            return
        if t.get("final_announced"):
            return

        # Atomic database lease to ensure only one worker across processes finalizes
        if t.get("lease_owner") != rt.worker_id and not await rt.repo.lease(tid, rt.worker_id, 120):
            return

        try:
            teams = {x["id"]: x for x in await rt.repo.all_teams(tid)}
            champ = teams.get(t.get("champion_team_id"))
            runner = teams.get(t.get("runner_up_team_id"))

            # Step 1: Record results (idempotent in SQL)
            try:
                await rt.repo.record_results(tid)
            except Exception:
                log.exception("Failed to record results for tournament %s", tid)

            # Step 2: Calculate and persist awards before announcements
            stats = await rt.repo.player_stats(tid)
            computed_awards = []
            if not t.get("awards_recorded"):
                try:
                    computed_awards = awards_engine.compute_awards(stats, champ, runner)
                    await rt.repo.store_awards(tid, computed_awards)
                    await rt.repo.update(tid, awards_recorded=True)
                except Exception:
                    log.exception("Failed to compute/store awards for tournament %s", tid)
            if not computed_awards:
                try:
                    computed_awards = awards_engine.compute_awards(stats, champ, runner)
                except Exception:
                    pass

            # Step 3: Champion announcement (deliver once)
            if champ and not t.get("champion_announced"):
                try:
                    await notify.group(rt, t["group_id"], f"👑 <b>{m.esc(champ['name'])} are CHAMPIONS! 🏆🎉</b>")
                    await rt.repo.update(tid, champion_announced=True)
                except Exception:
                    log.exception("Failed to announce champion for tournament %s", tid)

            # Step 4: Tournament Awards and MVPs announcement in group (deliver once)
            if not t.get("mvp_announced"):
                try:
                    awards = None
                    if hasattr(rt.repo, "awards"):
                        res_aw = rt.repo.awards(tid)
                        awards = await res_aw if inspect.isawaitable(res_aw) else res_aw
                    if not awards and computed_awards:
                        # Fallback: if DB hasn't populated or returned empty, populate names from stats and teams
                        stats_by_id = {p.get("player_id"): p for p in stats if p.get("player_id")}
                        for ca in computed_awards:
                            if not ca.get("player") and ca.get("player_id") in stats_by_id:
                                ca["player"] = stats_by_id[ca["player_id"]].get("name", "")
                            if not ca.get("team") and ca.get("team_id") in teams:
                                ca["team"] = teams[ca["team_id"]].get("name", "")
                        awards = computed_awards
                    if awards:
                        await notify.group(rt, t["group_id"], m.awards_text(awards))
                    mvps = await compute_human_mvps(rt, tid)
                    if mvps and (mvps.get("most_runs") or mvps.get("most_wickets")):
                        await notify.group(rt, t["group_id"], m.format_mvps_text(mvps))
                    await rt.repo.update(tid, mvp_announced=True)
                except Exception:
                    log.exception("Failed to announce MVPs for tournament %s", tid)

            # Step 5: Human manager final squad summary and performance DMs (deliver once, idempotent per user)
            if not t.get("final_dms_sent"):
                all_squad_summaries_sent = True
                for tm in teams.values():
                    if tm.get("kind") == "HUMAN" and tm.get("user_id"):
                        won = champ and tm["id"] == champ["id"]
                        try:
                            await notify.dm(rt, tm["user_id"], ("🏆 <b>YOU ARE THE IPL DRAFT CHAMPION!</b>" if won else
                                                                f"🏁 IPL Draft finished — champion: <b>{m.esc(champ['name']) if champ else '?'}</b>."))
                        except Exception:
                            log.exception("Failed to send champion/finished DM to user %s (team %s)", tm.get("user_id"), tm.get("id"))

                        # 1. Final drafted squad summary DM
                        already_sent = tm.get("squad_summary_sent") or ((tid, tm["id"]) in getattr(rt, "sent_squad_summaries", set()))
                        roster_data = None
                        squad_players = []
                        try:
                            roster_data = await rt.repo.team_roster(tm["id"])
                            squad_players = roster_data.get("players", []) if roster_data else []
                        except Exception:
                            log.exception("Failed to fetch roster for team %s", tm.get("id"))

                        if not already_sent:
                            try:
                                squad_text = m.final_squad_text(tm, squad_players)
                                sent_res = await notify.dm(rt, tm["user_id"], squad_text)
                                if sent_res:
                                    tm["squad_summary_sent"] = True
                                    if hasattr(rt, "sent_squad_summaries"):
                                        rt.sent_squad_summaries.add((tid, tm["id"]))
                                    if hasattr(rt.repo, "mark_squad_summary_sent"):
                                        res_m = rt.repo.mark_squad_summary_sent(tm["id"])
                                        if inspect.isawaitable(res_m):
                                            await res_m
                                else:
                                    all_squad_summaries_sent = False
                                    log.warning("Telegram reported delivery failure for squad summary to user %s (team %s)", tm.get("user_id"), tm.get("id"))
                            except Exception:
                                all_squad_summaries_sent = False
                                log.exception("Exception sending final squad summary to user %s (team %s)", tm.get("user_id"), tm.get("id"))

                            # 2. Tournament performance DM
                            try:
                                bo_state = await rt.repo.get_batting_order_state(tm["id"])
                                perf_players = list(squad_players)
                                if bo_state and bo_state.get("ordered") and len(bo_state["ordered"]) == 11:
                                    perf_players = bo_state["ordered"]
                                elif perf_players:
                                    perf_players = sorted(
                                        perf_players,
                                        key=lambda p: (p.get("batting_order") is None, p.get("batting_order", 99), p.get("slot", 99))
                                    )
                                if not perf_players and bo_state and bo_state.get("players"):
                                    perf_players = bo_state["players"]

                                if perf_players and stats:
                                    perf_text = m.squad_performance_text(perf_players, stats)
                                    await notify.dm(rt, tm["user_id"], perf_text)
                            except Exception:
                                log.exception("Failed to send squad performance DM to user %s (team %s)", tm.get("user_id"), tm.get("id"))

                if all_squad_summaries_sent:
                    await rt.repo.update(tid, final_dms_sent=True)

            # Step 6: Mark finalization complete
            await rt.repo.update(tid, final_announced=True)
            try:
                await update_group_tournament_summary(rt, t)
            except Exception:
                log.exception("Failed to update group summary for tournament %s", tid)

        finally:
            try:
                await rt.repo.release_lease(tid, rt.worker_id)
            except Exception:
                pass


HANDLERS = {
    "DRAFTING": _drafting, "SYSTEM_TEAM_GENERATION": _system, "FIXTURE_GENERATION": _fixtures,
    "LEAGUE_RUNNING": _league, "LEAGUE_COMPLETED": _league_completed,
    "PLAYOFF_ELIMINATOR": _playoff, "PLAYOFF_QUALIFIER_1": _playoff, "PLAYOFF_QUALIFIER_2": _playoff,
    "PLAYOFF_FINAL": _playoff, "COMPLETED": _completed,
}


# ───────────────────────── loop / recovery ─────────────────────────
async def tick(rt: IplRuntime) -> None:
    await draft_service.draft_tick(rt)
    await batting_order_service.batting_order_tick(rt)
    now = time.monotonic()

    # Active advanceable tournaments
    for t in await rt.repo.by_states(ADVANCEABLE):
        tid = t["id"]
        if tid in rt.running:
            continue
        if now < rt.retry_after.get(tid, 0.0):
            continue
        rt.ctx.spawn(advance(rt, tid), name=f"ipl-advance-{tid}")

    # Recoverable paused tournaments: automatically resume without manual intervention
    for t in await rt.repo.by_states(["FAILED_RECOVERABLE"]):
        tid = t["id"]
        if tid in rt.running:
            continue
        if now < rt.retry_after.get(tid, 0.0):
            continue
        log.info("IPL auto-recovery: resuming tournament %s (resume_state=%s)", tid, t.get("resume_state"))
        resumed_state = await rt.repo.resume(tid)
        if resumed_state:
            rt.failures.pop(tid, None)
            rt.retry_after.pop(tid, None)
            rt.ctx.spawn(advance(rt, tid), name=f"ipl-advance-{tid}")
        else:
            rt.retry_after[tid] = now + 60.0

    for t in await rt.repo.unannounced_completed():
        rt.ctx.spawn(finalize(rt, t["id"]), name=f"ipl-finalize-{t['id']}")


async def run_forever(rt: IplRuntime) -> None:
    while True:
        try:
            await tick(rt)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("IPL worker tick failed (will retry)")
        await asyncio.sleep(rt.s.tick_seconds)


async def recover(rt: IplRuntime) -> None:
    """Called once on startup: stale claims are re-claimable after 5 minutes, leases expire by themselves, so the normal
    tick resumes everything (drafting offers, system teams, league, playoffs, unannounced results)."""
    await rt.repo.release_stale_reservations()
    recoverable = await rt.repo.by_states(["FAILED_RECOVERABLE"])
    for t in recoverable:
        log.info("IPL recovery: auto-resuming paused tournament %s", t["id"])
        await rt.repo.resume(t["id"])
    active = await rt.repo.by_states(ADVANCEABLE)
    log.info("IPL recovery: %d tournament(s) active (+ %d auto-resumed)", len(active), len(recoverable))
