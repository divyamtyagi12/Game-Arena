"""Telegram output for IPL Draft: edit-in-place group dashboard, DMs, compact summaries. Never raises."""
from __future__ import annotations

import logging
from typing import Any

from services.telegram_io import safe_delete, safe_edit, send_message

from .. import keyboards as kb
from .. import messages as m
from ..runtime import IplRuntime

log = logging.getLogger(__name__)


async def dm(rt: IplRuntime, user_id: int, text: str, markup: Any = None):
    return await send_message(rt.ctx, user_id, text, reply_markup=markup)


async def edit_dm(rt: IplRuntime, user_id: int, message_id: int | None, text: str, markup: Any = None) -> bool:
    if not message_id:
        return False
    return await safe_edit(rt.ctx, user_id, message_id, text, reply_markup=markup) == "ok"


async def group(rt: IplRuntime, group_id: int, text: str, markup: Any = None):
    return await send_message(rt.ctx, group_id, text, reply_markup=markup)


async def refresh_lobby(rt: IplRuntime, tid: int, *, repost: bool = False) -> None:
    async with rt.ctx.lock(("ipl-lobby", tid)):
        t = await rt.repo.get(tid)
        if not t:
            return
        teams = await rt.repo.lobby_teams(tid)
        text = m.lobby_text(t, teams)
        markup = kb.lobby(tid) if t["state"] == "LOBBY" else None
        old = t.get("lobby_message_id")
        if old and not repost:
            if await safe_edit(rt.ctx, t["group_id"], old, text, reply_markup=markup) != "gone":
                return
        sent = await send_message(rt.ctx, t["group_id"], text, reply_markup=markup)
        if sent:
            await rt.repo.update(tid, lobby_message_id=sent.message_id)
            if old and repost:
                await safe_delete(rt.ctx, t["group_id"], old)


async def set_dashboard(rt: IplRuntime, t: dict, text: str, markup: Any = None) -> None:
    """Edit the tournament's single dashboard message; re-post only if it was deleted."""
    async with rt.ctx.lock(("ipl-dash", t["id"])):
        fresh = await rt.repo.get(t["id"])
        mid = (fresh or t).get("dashboard_message_id")
        if mid and await safe_edit(rt.ctx, t["group_id"], mid, text, reply_markup=markup) != "gone":
            return
        sent = await send_message(rt.ctx, t["group_id"], text, reply_markup=markup)
        if sent:
            await rt.repo.update(t["id"], dashboard_message_id=sent.message_id)


async def set_group_summary(rt: IplRuntime, t: dict, text: str) -> None:
    """Edit the tournament's single post-league summary message; re-post only if it was deleted."""
    tid = t["id"]
    gid = t["group_id"]
    async with rt.ctx.lock(("ipl-summary", tid)):
        fresh = await rt.repo.get(tid)
        mid = (fresh or t).get("summary_message_id")
        if mid and await safe_edit(rt.ctx, gid, mid, text, reply_markup=None) != "gone":
            return
        sent = await send_message(rt.ctx, gid, text, reply_markup=None)
        if sent:
            await rt.repo.update(tid, summary_message_id=sent.message_id)
