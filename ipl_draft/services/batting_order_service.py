"""Batting order selection flow (post-draft DM interaction).

Everything authoritative is in PostgreSQL (migration_ipl_v3.sql):
  * ipl_team_rosters.batting_order           the picks so far (1..11)
  * ipl_tournament_teams.batting_order_confirmed
  * ipl_tournament_teams.batting_order_deadline_at   the persisted clock (started when the 11th card is picked)
  * ipl_tournament_teams.batting_order_prompted_at / _message_id   whether (and where) the DM was delivered

There is NO in-memory state: after a restart the worker tick simply re-reads `ipl_batting_order_due`, re-sends a prompt that
never arrived, and auto-completes any order whose deadline has passed (keeping the manager's partial picks and filling the
rest with the engine's own rule). The match engine then simulates with exactly the confirmed order.
"""
from __future__ import annotations

import logging

from .. import keyboards as kb
from .. import messages as m
from ..runtime import IplRuntime
from . import notification_service as notify

log = logging.getLogger(__name__)


# ─────────────────────────── public API ────────────────────────────
async def start_batting_order_flow(rt: IplRuntime, team: dict, players: list[dict] | None = None) -> None:
    """Called when a human squad reaches 11: starts the persisted clock, then DMs the first prompt.
    Idempotent — the worker tick calls the same pieces again after a restart."""
    team_id = team["id"]
    await rt.repo.batting_order_begin(team_id, rt.s.batting_order_seconds)
    if team.get("auto_draft"):
        await auto_assign_order(rt, team, announce=False)       # unreachable / absent manager: do not wait for them
        return
    await send_prompt(rt, team_id)


async def send_prompt(rt: IplRuntime, team_id: int) -> None:
    team = await rt.repo.team_by_id(team_id)
    if not team or team.get("batting_order_confirmed"):
        return
    state = await rt.repo.get_batting_order_state(team_id)
    if not state:
        return
    text = m.batting_order_prompt(state["players"], state["ordered"], state["remaining"])
    markup = kb.batting_order(team_id, state["players"])
    sent = await notify.dm(rt, team["user_id"], text, markup)
    if sent:
        await rt.repo.batting_order_mark_prompted(team_id, sent.message_id)
    else:
        log.warning("IPL: cannot DM batting-order prompt to user %s — auto-assigning team %s", team["user_id"], team_id)
        await rt.repo.set_auto_draft(team_id)
        await auto_assign_order(rt, team, announce=False)


async def get_batting_order_state(rt: IplRuntime, team_id: int) -> dict | None:
    return await rt.repo.get_batting_order_state(team_id)


async def _guard(rt: IplRuntime, user_id: int, team_id: int) -> tuple[str, dict | None]:
    team = await rt.repo.team_by_id(team_id)
    if not team or team.get("user_id") != user_id:
        return "NOT_YOURS", None
    if team.get("batting_order_confirmed"):
        return "DONE", team
    return "OK", team


async def pick_batter(rt: IplRuntime, user_id: int, team_id: int, pos: int, dm_msg_id: int | None) -> str:
    """User pressed a player button. Returns: OK | NOT_YOURS | ALREADY | DONE | <other status from the database>."""
    st, team = await _guard(rt, user_id, team_id)
    if st != "OK":
        return st
    res = await rt.repo.batting_order_pick(team_id, pos)
    status = res.get("status")
    if status == "LAST":
        # Automatically complete ordering after all 11 players are selected
        await confirm_order(rt, user_id, team_id, dm_msg_id)
        return "DONE"
    if status == "ALREADY":
        return "ALREADY"
    if status != "OK":
        return status
    return await _refresh_dm(rt, user_id, team, dm_msg_id)


async def undo_pick(rt: IplRuntime, user_id: int, team_id: int, dm_msg_id: int | None) -> str:
    st, team = await _guard(rt, user_id, team_id)
    if st != "OK":
        return st
    await rt.repo.batting_order_undo(team_id)
    return await _refresh_dm(rt, user_id, team, dm_msg_id)


async def reset_order(rt: IplRuntime, user_id: int, team_id: int, dm_msg_id: int | None) -> str:
    st, team = await _guard(rt, user_id, team_id)
    if st != "OK":
        return st
    await rt.repo.batting_order_reset(team_id)
    return await _refresh_dm(rt, user_id, team, dm_msg_id)


async def confirm_order(rt: IplRuntime, user_id: int, team_id: int, dm_msg_id: int | None) -> str:
    """Returns OK | NOT_YOURS | ALREADY | INCOMPLETE."""
    st, team = await _guard(rt, user_id, team_id)
    if st == "DONE":
        return "ALREADY"
    if st != "OK":
        return st
    res = await rt.repo.batting_order_confirm(team_id)
    if res.get("status") != "OK":
        return res.get("status", "ERROR")
    state = await rt.repo.get_batting_order_state(team_id)
    ordered = state["ordered"] if state else []
    text = m.batting_order_confirmed_text(ordered)
    mid = dm_msg_id or team.get("batting_order_message_id")
    # Clear markup so buttons are removed when complete
    if not await notify.edit_dm(rt, user_id, mid, text, markup=None):
        await notify.dm(rt, user_id, text)
    await _maybe_advance(rt, team["tournament_id"])
    return "OK"


async def auto_assign_order(rt: IplRuntime, team: dict, *, announce: bool = True) -> None:
    """Deadline passed (or the manager is unreachable): keep their picks, fill the rest, confirm, tell them, move on."""
    team_id = team["id"]
    await rt.repo.batting_order_autofill(team_id)
    if announce:
        state = await rt.repo.get_batting_order_state(team_id)
        ordered = state["ordered"] if state else []
        text = m.batting_order_timeout_text(ordered)
        fresh = await rt.repo.team_by_id(team_id) or team
        if not await notify.edit_dm(rt, team["user_id"], fresh.get("batting_order_message_id"), text, markup=None):
            await notify.dm(rt, team["user_id"], text)
    await _maybe_advance(rt, team["tournament_id"])


# ─────────────────────────── helpers ───────────────────────────────
async def _refresh_dm(rt: IplRuntime, user_id: int, team: dict, dm_msg_id: int | None) -> str:
    state = await rt.repo.get_batting_order_state(team["id"])
    if not state:
        return "ERROR"
    text = m.batting_order_prompt(state["players"], state["ordered"], state["remaining"])
    markup = kb.batting_order(team["id"], state["players"])
    mid = dm_msg_id or team.get("batting_order_message_id")
    if mid:
        await notify.edit_dm(rt, user_id, mid, text, markup)
    else:
        sent = await notify.dm(rt, user_id, text, markup)
        if sent:
            await rt.repo.batting_order_mark_prompted(team["id"], sent.message_id)
    return "OK"


async def _maybe_advance(rt: IplRuntime, tid: int) -> None:
    """If every human team has confirmed, wake the tournament worker (it also polls, so this is only a latency optimisation)."""
    if not await rt.repo.teams_pending_batting_order(tid):
        from . import simulation_worker
        rt.ctx.spawn(simulation_worker.advance(rt, tid), name=f"ipl-advance-{tid}")


# ─────────────────────── worker tick (restart-safe) ───────────────────────
async def batting_order_tick(rt: IplRuntime) -> None:
    """Driven purely by database state, so a restart changes nothing."""
    for team in await rt.repo.batting_order_due():
        try:
            if team.get("needs_clock"):
                await rt.repo.batting_order_begin(team["id"], rt.s.batting_order_seconds)
            if team.get("auto_draft") or team.get("expired"):
                await auto_assign_order(rt, team, announce=not team.get("auto_draft"))
            elif team.get("needs_prompt"):
                await send_prompt(rt, team["id"])
        except Exception:  # noqa: BLE001
            log.exception("batting_order_tick failed for team %s", team.get("id"))
