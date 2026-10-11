"""Tournament progression worker: system teams → fixtures → league → playoffs → awards/leaderboards.

* one driver per tournament at a time (in-process set + PostgreSQL lease);
* every step is idempotent and persisted, so a crash/restart resumes from the database state;
* matches use deterministic per-fixture seeds → a retry after a crash reproduces the same match;
* the group only ever sees an edited dashboard, a compact summary every few matchdays and the playoff cards.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random

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
    except Exception as exc:  # noqa: BLE001
        n = rt.failures.get(tid, 0) + 1
        rt.failures[tid] = n
        log.exception("IPL advance failed for tournament %s (%d/%d)", tid, n, rt.s.max_failures)
        await rt.repo.release_claims(tid, rt.worker_id)
        if n >= rt.s.max_failures:
            t = await rt.repo.get(tid)
            if t and await rt.repo.set_failed(tid, f"{type(exc).__name__}: {exc}"):
                await notify.group(rt, t["group_id"], "⚠️ <b>IPL Draft paused</b> after repeated errors. "
                                   "Your progress is saved — a group admin can send /iplresume to continue.")
    finally:
        rt.running.discard(tid)
        try:
            await rt.repo.release_lease(tid, rt.worker_id)
        except Exception:  # noqa: BLE001
            pass


# ───────────────────────── state handlers (return True = made progress, loop again) ─────────────────────────
async def _drafting(rt: IplRuntime, t: dict) -> bool:
    """Waits (returns False) until every human has drafted AND confirmed a batting order and the AI franchises are
    complete; then makes the one explicit DRAFTING -> SYSTEM_TEAM_GENERATION step in the database.
    (Regression: this handler used to return True forever without changing the state, so tournaments sat in DRAFTING.)"""
    tid = t["id"]
    prog = await rt.repo.draft_progress(tid)
    if not prog or not all(p["done"] for p in prog):
        return False
    if await rt.repo.teams_pending_batting_order(tid):
        return False
    await system_service.prepare_system_teams(rt, tid)
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
        raise RuntimeError(f"complete_system_teams: {res}")           # visible to the failure counter, never forced through
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
    await notify.group(rt, t["group_id"], f"🏟 <b>IPL DRAFT — LEAGUE BEGINS!</b>\n\n{len(ids)} teams · {len(fx)} matches · every team plays every other team twice.")
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
                     "🪙 <i>Toss underway & players taking the field…</i>")
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

    # Deliver final scorecard pages automatically to each human user's DM — no buttons needed
    if humans and res.get("match_id"):
        try:
            card = await rt.repo.scorecard(res["match_id"])
            if card:
                for h in humans:
                    uid = h["user_id"]
                    your_team_id = h["id"]
                    mid = dm_msgs.get(uid)
                    pages = m.format_full_scorecard(card, fx.get("stage", "LEAGUE"), your_team_id)
                    for page_no, page_text in enumerate(pages):
                        if page_no == 0 and mid:
                            if not await notify.edit_dm(rt, uid, mid, page_text):
                                await notify.dm(rt, uid, page_text)
                        else:
                            await notify.dm(rt, uid, page_text)
        except Exception:  # noqa: BLE001
            log.exception("Failed to deliver full scorecard DM for match %s", res.get("match_id"))

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
        await _simulate(rt, t, fx, squads)
    # Fix 1: Do NOT post intermediate matchday summaries or points tables to group
    await rt.pause(rt.s.matchday_delay)
    return True


async def _league_completed(rt: IplRuntime, t: dict) -> bool:
    tid = t["id"]
    standings = await rt.repo.standings(tid)
    # Fix 2 & 3: Exactly ONE final points table sent directly to group with ZERO inline buttons
    await notify.group(rt, t["group_id"], m.league_complete_text(standings), markup=None)
    rank = {r["team_id"]: r["rank"] for r in standings}
    for tm in await rt.repo.all_teams(tid):
        if tm["kind"] == "HUMAN":
            r = rank.get(tm["id"])
            tail = "🔥 You're in the playoffs!" if r and r <= 4 else "Better luck next time!"
            await notify.dm(rt, tm["user_id"], f"🏟 <b>League stage finished</b>\n\n{m.esc(tm['name'])} finished <b>#{r}</b>. {tail}")
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

    # Fix 5: Do NOT send separate 'Simulating...' or temporary intro announcements to group
    res = await _simulate(rt, t, fx, squads)
    if res.get("status") == "OK":
        # Fix 4: Send the COMPLETE playoff scorecard to the group chat (no buttons)
        card = await rt.repo.scorecard(res["match_id"])
        if card:
            group_pages = m.format_full_scorecard(card, fx["stage"], user_id=None)
            for page in group_pages:
                await notify.group(rt, t["group_id"], page, markup=None)
    await rt.pause(rt.s.playoff_delay)
    return True


async def _completed(rt: IplRuntime, t: dict) -> bool:
    await finalize(rt, t["id"])
    return False


async def finalize(rt: IplRuntime, tid: int) -> None:
    """Awards + leaderboard + champion announcement. Safe to call repeatedly (stats are recorded exactly once)."""
    t = await rt.repo.get(tid)
    if not t or t["state"] != "COMPLETED":
        return
    teams = {x["id"]: x for x in await rt.repo.all_teams(tid)}
    champ, runner = teams.get(t["champion_team_id"]), teams.get(t["runner_up_team_id"])
    await rt.repo.record_results(tid)
    if not t["final_announced"]:
        stats = await rt.repo.player_stats(tid)
        await rt.repo.store_awards(tid, awards_engine.compute_awards(stats, champ, runner))
        bracket = await rt.repo.bracket(tid)
        final_match = next((b["match_id"] for b in bracket if b["stage"] == "FINAL"), None)
        card = await rt.repo.scorecard(final_match) if final_match else None
        owner = champ["owner_name"] if champ and champ["kind"] == "HUMAN" else None
        if card and champ:
            # Fix 11: Championship announcement without inline buttons
            await notify.group(rt, t["group_id"], m.champion_text(card, champ, owner), markup=None)
        await notify.group(rt, t["group_id"], m.awards_text(await rt.repo.awards(tid)), markup=None)
        for tm in teams.values():
            if tm["kind"] == "HUMAN":
                won = champ and tm["id"] == champ["id"]
                await notify.dm(rt, tm["user_id"], ("🏆 <b>YOU ARE THE IPL DRAFT CHAMPION!</b>" if won else
                                                    f"🏁 IPL Draft finished — champion: <b>{m.esc(champ['name']) if champ else '?'}</b>."))
                try:
                    roster_data = await rt.repo.team_roster(tm["id"])
                    squad_players = roster_data.get("players", []) if roster_data else []
                    bo_state = await rt.repo.get_batting_order_state(tm["id"])
                    if bo_state and bo_state.get("ordered") and len(bo_state["ordered"]) == 11:
                        squad_players = bo_state["ordered"]
                    elif squad_players:
                        squad_players = sorted(
                            squad_players,
                            key=lambda p: (p.get("batting_order") is None, p.get("batting_order", 99), p.get("slot", 99))
                        )
                    if squad_players:
                        perf_text = m.squad_performance_text(squad_players, stats)
                        await notify.dm(rt, tm["user_id"], perf_text)
                except Exception:
                    log.exception("Failed to send squad performance DM to user %s (team %s)", tm.get("user_id"), tm.get("id"))
        await rt.repo.update(tid, final_announced=True)


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
    for t in await rt.repo.by_states(ADVANCEABLE):
        if t["id"] not in rt.running:
            rt.ctx.spawn(advance(rt, t["id"]), name=f"ipl-advance-{t['id']}")
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
    active = await rt.repo.by_states(ADVANCEABLE)
    log.info("IPL recovery: %d tournament(s) to resume", len(active))
