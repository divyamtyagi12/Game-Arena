"""Human drafting through DMs: offers, confirmation, picks, timeouts/auto-draft, progress display."""
from __future__ import annotations

import asyncio
import logging
import time

from .. import keyboards as kb
from .. import messages as m
from ..engine.draft_engine import auto_pick
from ..runtime import IplRuntime
from . import notification_service as notify, system_service

log = logging.getLogger(__name__)
PROGRESS_MIN_GAP = 8.0       # seconds between edits of the group progress message (group rate limits)


# ───────────────────────── offers ─────────────────────────
async def kickoff(rt: IplRuntime, tid: int) -> None:
    """Humans get their first offers immediately. The AI franchises draft from the same pool independently (a cricketer
    may sit in several squads; only duplicates inside one XI are forbidden), so they never delay or block a human."""
    await update_progress(rt, tid, force=True)
    await offers_for_teams(rt, tid)
    rt.ctx.spawn(_prepare_system(rt, tid), name=f"ipl-system-{tid}")


async def _prepare_system(rt: IplRuntime, tid: int) -> None:
    try:
        await system_service.prepare_system_teams(rt, tid)
    except Exception:
        # not swallowed silently: the worker retries every tick and escalates through its failure counter
        log.exception("IPL system-team preparation failed for tournament %s (will retry)", tid)


async def offers_for_teams(rt: IplRuntime, tid: int) -> None:
    for team in await rt.repo.teams_needing_offer(tid):
        await deliver_offer(rt, team)


async def deliver_offer(rt: IplRuntime, team: dict) -> None:
    """Open (or re-read) this team's current offer and DM it once. An undeliverable DM switches the team to auto-draft."""
    res = await rt.repo.open_offer(team["id"])
    if res.get("status") != "OPEN":
        return
    if res.get("dm_message_id") or team.get("auto_draft"):
        return
    roster = await rt.repo.team_roster(team["id"])
    players = roster["players"] if roster else []
    text = m.offer_text(team, res, res["picks_done"], rt.s.draft_seconds, roster=players)
    sent = await notify.dm(rt, team["user_id"], text, kb.offer(res["offer_id"], res["candidates"], team["id"], roster=players))
    if sent:
        await rt.repo.set_offer_message(res["offer_id"], sent.message_id)
    else:
        log.warning("IPL: cannot DM user %s — switching team %s to auto-draft", team["user_id"], team["id"])
        await rt.repo.set_auto_draft(team["id"])


# ───────────────────────── callbacks (validated against the Telegram sender) ─────────────────────────
async def _own_open_offer(rt: IplRuntime, user_id: int, offer_id: int) -> tuple[str, dict | None]:
    offer = await rt.repo.get_offer(offer_id)
    if not offer:
        return "STALE", None
    team = await rt.repo.team_of_user(offer["tournament_id"], user_id)
    if not team or team["id"] != offer["team_id"]:
        return "NOT_YOURS", None
    if offer["status"] != "OPEN":
        return "ALREADY", offer
    return "OK", {**offer, "team": team}


async def confirm_pick(rt: IplRuntime, user_id: int, offer_id: int, pos: int, message_id: int | None) -> dict:
    from ..engine.draft_engine import is_pick_legal, still_required_text
    offer = None
    if hasattr(rt.repo, "get_offer") and callable(rt.repo.get_offer):
        res_o = rt.repo.get_offer(offer_id)
        offer = await res_o if asyncio.iscoroutine(res_o) else res_o
    if offer and isinstance(offer, dict) and offer.get("team_id"):
        res_r = rt.repo.team_roster(offer["team_id"])
        roster = await res_r if asyncio.iscoroutine(res_r) else res_r
        players = roster["players"] if isinstance(roster, dict) and roster.get("players") else []
        cands = offer.get("candidates") or []
        cand = next((c for c in cands if c.get("pos") == pos or c.get("position") == pos), None)
        if cand:
            p = cand.get("player") or {}
            if not is_pick_legal(players, p):
                req = still_required_text(players)
                role = p.get("role", "this player")
                return {"status": "ROLE_REQUIRED", "message": f"⚠️ Cannot pick {role}! Still required: {req}."}

    res = await rt.repo.pick(offer_id, pos, user_id, "USER", rt.s.auto_after_timeouts)
    if res.get("status") == "OK":
        await after_pick(rt, res, message_id, auto=False)
    return res




async def after_pick(rt: IplRuntime, res: dict, message_id: int | None, *, auto: bool) -> None:
    try:
        roster = await rt.repo.team_roster(res["team_id"])
        team, players = roster["team"], roster["players"]
        user_id = team["user_id"]
        if not team.get("auto_draft") or not auto:
            text = (m.draft_complete_text(team, players) if res["done"]
                    else m.drafted_text(res["player"], players, auto=auto))
            # Remove previous selection buttons immediately to prevent duplicate clicks
            await notify.edit_dm(rt, user_id, message_id, text, markup=None)
        if res["done"]:
            from . import batting_order_service
            await batting_order_service.start_batting_order_flow(rt, team, players)
        else:
            await deliver_offer(rt, team)
        await update_progress(rt, res["tournament_id"], force=bool(res["done"]))
        if res.get("all_humans_done"):
            from . import simulation_worker
            rt.ctx.spawn(simulation_worker.advance(rt, res["tournament_id"]), name=f"ipl-advance-{res['tournament_id']}")
    except Exception:  # noqa: BLE001  — a display problem must never undo a committed pick
        log.exception("IPL after_pick failed (pick is already saved)")


# ───────────────────────── worker side ─────────────────────────
async def draft_tick(rt: IplRuntime) -> None:
    """Auto-picks for expired deadlines / auto-draft teams, and (re)opens offers after restarts."""
    for due in await rt.repo.due_offers():
        try:
            pos = auto_pick(due["candidates"], due["roster"], rt.rng)
            mode = "TIMEOUT" if due["timed_out"] and not due["auto_draft"] else "AUTO"
            res = await rt.repo.pick(due["offer_id"], pos, due.get("user_id") or 0, mode, rt.s.auto_after_timeouts)
            if res.get("status") == "OK":
                await after_pick(rt, res, due.get("dm_message_id"), auto=True)
            else:
                log.error("IPL auto-pick returned non-OK status %s for offer %s", res, due.get("offer_id"))
        except Exception:  # noqa: BLE001
            log.exception("IPL auto-pick failed for offer %s", due.get("offer_id"))
    for t in await rt.repo.by_states(["DRAFTING"]):
        await offers_for_teams(rt, t["id"])
    await rt.repo.release_stale_reservations()


async def update_progress(rt: IplRuntime, tid: int, *, force: bool = False) -> None:
    now = time.monotonic()
    last = rt.progress_last.get(tid, 0.0)
    if not force and now - last < PROGRESS_MIN_GAP:
        if tid not in rt.progress_pending:                       # one trailing update is enough
            rt.progress_pending.add(tid)

            async def later() -> None:
                await asyncio.sleep(PROGRESS_MIN_GAP - (time.monotonic() - last))
                rt.progress_pending.discard(tid)
                await update_progress(rt, tid, force=True)
            rt.ctx.spawn(later(), name=f"ipl-progress-{tid}")
        return
    rt.progress_last[tid] = now
    t = await rt.repo.get(tid)
    if not t or t["state"] != "DRAFTING":
        return
    await notify.set_dashboard(rt, t, m.draft_progress_text(await rt.repo.draft_progress(tid)))
