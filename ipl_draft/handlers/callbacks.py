"""Router for every `ipl:*` inline button.

Every action is validated on the server: the Telegram sender, the chat, the tournament and (for picks) the offer
are re-checked against the database, so forged or stale callback data can never change someone else's team.

callback_data (all < 64 bytes):
    ipl:j|lv|st|ru:<tid>            lobby (group)         ipl:pk:<offer>:<pos>   pick → confirm screen (DM)
    ipl:cf:<offer>:<pos>            confirm pick (DM)     ipl:bk:<offer>         back to the offer (DM)
    ipl:tm:<team>                   my squad (DM)         ipl:ts:<tid>           tournament status
    ipl:tb:<tid>                    points table          ipl:fx|mh|at:<tid>:<page>
    ipl:br:<tid>                    bracket               ipl:sc:<match>:<page>  scorecard
    ipl:sm:<tid>                    final summary         ipl:lb:g|c:<page>      leaderboard
    ipl:pa                          play again
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from handlers.common import get_ctx, is_admin, safe_answer
from services.telegram_io import safe_edit, send_message

from .. import keyboards as kb
from .. import messages as m
from ..services import batting_order_service, draft_service, tournament_service

log = logging.getLogger(__name__)
GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)
PAGE = 10                       # fixtures / history rows per page
TEAMS_PER_PAGE = 4
LB_SIZE = 10
MAX_ID = 2 ** 62
LIVE_STATES = ("FIXTURE_GENERATION", "LEAGUE_RUNNING", "LEAGUE_COMPLETED", "PLAYOFF_ELIMINATOR", "PLAYOFF_QUALIFIER_1",
               "PLAYOFF_QUALIFIER_2", "PLAYOFF_FINAL", "COMPLETED")
SQUADS_VISIBLE = ("SYSTEM_TEAM_GENERATION",) + LIVE_STATES      # squads stay secret until every human has drafted


def get_rt(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data["ipl"]


async def _is_participant(rt, tid: int, user_id: int) -> bool:
    return bool(await rt.repo.team_of_user(tid, user_id))


async def _can_view(rt, t: dict, chat, user_id: int) -> bool:
    """Group buttons work for anyone in that group; elsewhere (DM) only for participants of that tournament."""
    if chat.type in GROUP_TYPES:
        return chat.id == t["group_id"]
    return await _is_participant(rt, t["id"], user_id)


async def _edit(rt, q, text: str, markup=None) -> None:
    await safe_edit(rt.ctx, q.message.chat.id, q.message.message_id, text, reply_markup=markup)


import inspect

async def ipl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = get_rt(context)
    q = update.callback_query
    if not q or not q.message:
        return await safe_answer(q, "⚠️ This button is no longer active.", True)
    parts = (q.data or "").split(":")
    try:
        action = parts[1]
        args = [int(x) for x in parts[2:]] if action not in ("lb",) else [parts[2], int(parts[3])]
    except (IndexError, ValueError):
        return await safe_answer(q, "⚠️ Invalid button.")
    if any(isinstance(a, int) and not 0 <= a <= MAX_ID for a in args):      # forged / absurd ids never reach the database
        return await safe_answer(q, "⚠️ Invalid button.")
    if not rt.s.enabled:
        return await safe_answer(q, "⚠️ IPL Draft is currently disabled.", True)

    fn = ROUTES.get(action)
    if fn is None:
        return await safe_answer(q, "⚠️ Invalid button.")

    # Explicitly validate callback argument count before execution (Bug 9)
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())[3:]  # Skip (rt, context, q)
    has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    min_args = len([p for p in params if p.default is inspect.Parameter.empty and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)])
    max_args = len([p for p in params if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)])

    if len(args) < min_args or (not has_varargs and len(args) > max_args):
        log.warning("Callback argument mismatch for '%s': expected %d..%d, got %d (%s)", action, min_args, max_args, len(args), q.data)
        return await safe_answer(q, "⚠️ Invalid button.")

    try:
        await fn(rt, context, q, *args)
    except Exception:  # noqa: BLE001
        log.exception("IPL callback execution error for data '%s'", q.data)
        await safe_answer(q, "⚠️ Something went wrong. Please try again.")


# ───────────────────────── lobby ─────────────────────────
async def _lobby_t(rt, q, tid: int) -> dict | None:
    t = await rt.repo.get(tid)
    chat = q.message.chat
    if not t or chat.type not in GROUP_TYPES or chat.id != t["group_id"]:
        await safe_answer(q, "⚠️ This lobby is no longer active.", True)
        return None
    return t


async def join(rt, context, q, tid: int) -> None:
    t = await _lobby_t(rt, q, tid)
    if not t:
        return
    user = q.from_user
    name = user.first_name or user.username or f"Player {user.id}"
    res = await tournament_service.join(rt, tid, user, name)
    if res == "NO_DM":
        await safe_answer(q, m.DM_PROMPT, True)
        if rt.bot_username:                               # deep link so they can start the bot in one tap
            await send_message(rt.ctx, q.message.chat.id, f"{m.esc(name)}, {m.DM_PROMPT}",
                               reply_markup=kb.open_bot(rt.bot_username))
        return
    await safe_answer(q, {"OK": "✅ You joined the IPL Draft!", "ALREADY": "You're already in.",
                          "FULL": "❌ The lobby is full.", "CLOSED": "⚠️ This lobby is closed."}.get(res, "⚠️ Could not join."),
                      res != "OK")


async def leave(rt, context, q, tid: int) -> None:
    if not await _lobby_t(rt, q, tid):
        return
    res = await tournament_service.leave(rt, tid, q.from_user.id)
    await safe_answer(q, {"OK": "👋 You left the lobby.", "NOT_IN": "You're not in this lobby.",
                          "CLOSED": "⚠️ The draft has already started."}.get(res, "⚠️ Could not leave."), res != "OK")


async def force_start(rt, context, q, tid: int) -> None:
    t = await _lobby_t(rt, q, tid)
    if not t:
        return
    is_host = bool(t.get("host_id") and q.from_user.id == t.get("host_id"))
    admin = await is_admin(context.bot, q.message.chat.id, q.from_user.id)
    if not (is_host or admin):
        return await safe_answer(q, "❌ Only the tournament host or group admins can force-start.", True)
    ok, text = await tournament_service.force_start(rt, tid)
    await safe_answer(q, text[:190], not ok)
    if not ok and text.startswith("⚠️ Can't DM") and rt.bot_username:
        await send_message(rt.ctx, q.message.chat.id, text, reply_markup=kb.open_bot(rt.bot_username))


async def rules(rt, context, q, tid: int) -> None:
    await safe_answer(q)
    await send_message(rt.ctx, q.message.chat.id, m.rules_text(rt.s.draft_seconds))


# ───────────────────────── draft (DM) ─────────────────────────
_OFFER_ERR = {"STALE": "⚠️ This draft round is no longer active.", "NOT_YOURS": "❌ These cards aren't yours.",
              "ALREADY": "✅ You already picked from this round.", "BAD_POS": "⚠️ Invalid choice.",
              "EXPIRED": "⏱ Time ran out — a player was picked for you.", "CLOSED": "⚠️ This round is closed.",
              "NOT_DUE": "⚠️ Not available."}


async def pick(rt, context, q, offer_id: int, pos: int) -> None:
    res = await draft_service.confirm_pick(rt, q.from_user.id, offer_id, pos, q.message.message_id)
    st = res.get("status")
    if st == "OK":
        pl = res.get("player") or {}
        p_name = pl.get("name") or pl.get("display_name") or "Player"
        return await safe_answer(q, f"✅ {p_name} selected!")
    if st in ("ROLE_REQUIRED", "ILLEGAL_PICK"):
        return await safe_answer(q, res.get("message", "⚠️ Role required to meet squad minimums!"), True)
    if st == "ALREADY_HAVE":
        return await safe_answer(q, res.get("message", "⚠️ This player is already in your squad!"), True)
    if st == "ALREADY":
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await safe_answer(q, "✅ Pick already recorded!")
    await safe_answer(q, _OFFER_ERR.get(st, "❌ This selection is already locked."), True)


async def random_pick(rt, context, q, offer_id: int) -> None:
    st, offer_data = await draft_service._own_open_offer(rt, q.from_user.id, offer_id)
    if st != "OK" or not offer_data:
        return await safe_answer(q, _OFFER_ERR.get(st, "⚠️ Invalid draft offer."), True)

    from ..engine import draft_engine
    team = offer_data["team"]
    roster = await rt.repo.team_roster(team["id"])
    players = roster["players"] if roster else []

    offer_data = await draft_service.ensure_legal_candidates(rt, team, offer_data, players)
    cands = offer_data.get("candidates") or []

    legal_cands = [c for c in cands if draft_engine.is_pick_legal(players, c.get("player") or {})]

    needs = draft_engine.remaining_role_needs(players)
    wk_need = needs.get("WK", 0)
    b_need = needs.get("BOWL", 0)
    ar_need = needs.get("AR", 0)

    chosen = None
    if wk_need > 0:
        wk_opts = [c for c in legal_cands if (c.get("player") or {}).get("role") == "WK"]
        if wk_opts:
            chosen = rt.rng.choice(wk_opts)
    if not chosen and b_need > 0:
        b_opts = [c for c in legal_cands if (c.get("player") or {}).get("role") == "BOWL"]
        if b_opts:
            chosen = rt.rng.choice(b_opts)
    if not chosen and ar_need > 0:
        ar_opts = [c for c in legal_cands if (c.get("player") or {}).get("role") == "AR"]
        if ar_opts:
            chosen = rt.rng.choice(ar_opts)
    if not chosen and legal_cands:
        chosen = rt.rng.choice(legal_cands)

    if not chosen:
        return await safe_answer(q, "⚠️ No legal players available to auto-assign right now.", True)

    pos = chosen.get("pos") or chosen.get("position", 1)
    res = await draft_service.confirm_pick(rt, q.from_user.id, offer_id, pos, q.message.message_id)
    st = res.get("status")
    if st == "OK":
        pl = res.get("player") or {}
        p_name = pl.get("name") or pl.get("display_name") or "Player"
        return await safe_answer(q, f"✅ {p_name} auto-assigned!")
    if st in ("ROLE_REQUIRED", "ILLEGAL_PICK"):
        return await safe_answer(q, res.get("message", "⚠️ Role required to meet squad minimums!"), True)
    if st == "ALREADY_HAVE":
        return await safe_answer(q, res.get("message", "⚠️ This player is already in your squad!"), True)
    if st == "ALREADY":
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await safe_answer(q, "✅ Pick already recorded!")
    await safe_answer(q, _OFFER_ERR.get(st, "❌ This selection is already locked."), True)


async def blocked_pick(rt, context, q, pos: int = 1) -> None:
    await safe_answer(q, "⚠️ You cannot pick this player: you still need required roles (3 bowlers, 1 keeper, 1 all-rounder)!", True)


async def back(rt, context, q, offer_id: int) -> None:
    await safe_answer(q, "❌ This selection is already locked.", True)


async def confirm(rt, context, q, offer_id: int, pos: int) -> None:
    await safe_answer(q, "❌ This selection is already locked.", True)


async def my_team(rt, context, q, team_id: int) -> None:

    roster = await rt.repo.team_roster(team_id)
    if not roster or roster["team"].get("user_id") != q.from_user.id:
        return await safe_answer(q, "❌ That isn't your team.", True)
    await safe_answer(q)
    players = roster["players"]
    text = m.my_team_text(roster["team"], players, len(players) >= 11)
    await send_message(rt.ctx, q.message.chat.id, text)


# ───────────────────────── tournament views ─────────────────────────
async def _viewable(rt, q, tid: int) -> dict | None:
    t = await rt.repo.get(tid)
    if not t or not await _can_view(rt, t, q.message.chat, q.from_user.id):
        await safe_answer(q, "⚠️ This button belongs to another tournament.", True)
        return None
    return t


async def status(rt, context, q, tid: int) -> None:
    t = await _viewable(rt, q, tid)
    if not t:
        return
    teams = await rt.repo.lobby_teams(tid)
    await safe_answer(q)
    await send_message(rt.ctx, q.message.chat.id, m.status_text(t, len(teams)))


async def table(rt, context, q, tid: int, in_place: bool = False) -> None:
    """`ipl:tb` (dashboard button) posts a new message in groups; `ipl:tr` (refresh) edits that message."""
    t = await _viewable(rt, q, tid)
    if not t:
        return
    if t["state"] in ("LOBBY", "DRAFTING", "SYSTEM_TEAM_GENERATION", "FIXTURE_GENERATION"):
        return await safe_answer(q, "🏆 The league hasn't started yet.", True)
    if t["state"] == "LEAGUE_RUNNING":
        return await safe_answer(q, "⏳ Points table will be revealed after the league stage finishes!", True)
    rows = await rt.repo.standings(tid)
    await safe_answer(q, "🔄 Updated" if in_place else None)
    await _show(rt, q, m.points_table(rows), kb.table_markup(tid), in_place)


async def table_refresh(rt, context, q, tid: int) -> None:
    await table(rt, context, q, tid, in_place=True)


async def _show(rt, q, text: str, markup=None, in_place: bool = False) -> None:
    """DMs and pager/refresh presses edit the pressed message; first-level presses in a group reply with a new one
    so the shared dashboard message is never overwritten."""
    if in_place or q.message.chat.type == ChatType.PRIVATE:
        return await _edit(rt, q, text, markup)
    await send_message(rt.ctx, q.message.chat.id, text, reply_markup=markup)


async def fixtures(rt, context, q, tid: int, page: int, in_place: bool = False) -> None:
    t = await _viewable(rt, q, tid)
    if not t:
        return
    if t["state"] in ("LOBBY", "DRAFTING", "SYSTEM_TEAM_GENERATION"):
        return await safe_answer(q, "📅 Fixtures are created after the draft.", True)
    page = max(page, 0)
    data = await rt.repo.fixtures_page(tid, page * PAGE, PAGE)
    pages = max(1, -(-data["total"] // PAGE))
    page = min(page, pages - 1)
    if data["total"] and not data["rows"]:
        data = await rt.repo.fixtures_page(tid, page * PAGE, PAGE)
    await safe_answer(q)
    await _paged(rt, q, m.fixtures_page_text(data, page, PAGE), "fx", tid, page, pages, in_place)


async def history(rt, context, q, tid: int, page: int, in_place: bool = False) -> None:
    t = await _viewable(rt, q, tid)
    if not t:
        return
    page = max(page, 0)
    data = await rt.repo.matches_page(tid, page * PAGE, PAGE)
    pages = max(1, -(-data["total"] // PAGE))
    page = min(page, pages - 1)
    await safe_answer(q)
    await _paged(rt, q, m.history_page_text(data, page, PAGE), "mh", tid, page, pages, in_place)


async def all_teams(rt, context, q, tid: int, page: int, in_place: bool = False) -> None:
    t = await _viewable(rt, q, tid)
    if not t:
        return
    if t["state"] not in SQUADS_VISIBLE:
        return await safe_answer(q, "🔒 Squads are secret until every manager finishes drafting.", True)
    rosters = await rt.repo.all_rosters(tid)
    text, pages = m.all_teams_page(rosters, page, TEAMS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    await safe_answer(q)
    await _paged(rt, q, text, "at", tid, page, pages, in_place)


async def _paged(rt, q, text: str, prefix: str, tid: int, page: int, pages: int, in_place: bool) -> None:
    await _show(rt, q, text, kb.pager(prefix, tid, page, pages, back=f"ipl:tr:{tid}"), in_place)


async def fixtures_p(rt, context, q, tid: int, page: int) -> None:
    await fixtures(rt, context, q, tid, page, True)


async def history_p(rt, context, q, tid: int, page: int) -> None:
    await history(rt, context, q, tid, page, True)


async def all_teams_p(rt, context, q, tid: int, page: int) -> None:
    await all_teams(rt, context, q, tid, page, True)


async def bracket(rt, context, q, tid: int) -> None:
    t = await _viewable(rt, q, tid)
    if not t:
        return
    await safe_answer(q)
    text = m.bracket_text(await rt.repo.bracket(tid), await rt.repo.standings(tid))
    await _reply(rt, q, text)


async def _reply(rt, q, text: str, markup=None) -> None:
    await _show(rt, q, text, markup)


async def scorecard(rt, context, q, match_id: int, page: int, in_place: bool = False) -> None:
    card = await rt.repo.match_card(match_id)
    t = await rt.repo.get(card["tournament_id"]) if card else None
    if not t or not await _can_view(rt, t, q.message.chat, q.from_user.id):
        return await safe_answer(q, "⚠️ This scorecard isn't available here.", True)
    sc = await rt.repo.scorecard(match_id)
    pages = m.scorecard_pages(sc)
    page = max(0, min(page, len(pages) - 1))
    await safe_answer(q)
    from ..keyboards import Btn, Markup
    nav = []
    if page > 0:
        nav.append(Btn("◀️", callback_data=f"ipl:sp:{match_id}:{page - 1}"))
    nav.append(Btn(f"{page + 1}/{len(pages)}", callback_data=f"ipl:sp:{match_id}:{page}"))
    if page < len(pages) - 1:
        nav.append(Btn("▶️", callback_data=f"ipl:sp:{match_id}:{page + 1}"))
    markup = Markup([nav]) if nav else None
    await _show(rt, q, pages[page], markup, in_place)


async def scorecard_p(rt, context, q, match_id: int, page: int) -> None:
    await scorecard(rt, context, q, match_id, page, True)


async def summary(rt, context, q, tid: int) -> None:
    t = await _viewable(rt, q, tid)
    if not t:
        return
    awards = await rt.repo.awards(tid)
    if not awards:
        return await safe_answer(q, "🏅 Awards appear when the tournament ends.", True)
    await safe_answer(q)
    await _reply(rt, q, m.awards_text(awards))


# ───────────────────────── leaderboard / play again ─────────────────────────
async def leaderboard(rt, context, q, scope: str, page: int) -> None:
    chat, user = q.message.chat, q.from_user
    page = max(page, 0)
    if scope == "g":
        data = await rt.repo.global_leaderboard(user.id, LB_SIZE, page * LB_SIZE)
        title, group_id = "Global", 0
    else:
        gid = chat.id if chat.type in GROUP_TYPES else None
        if gid is None:
            groups = await rt.repo.groups_of_user(user.id)
            gid = groups[0]["group_id"] if groups else None
        if gid is None or (chat.type not in GROUP_TYPES and not await rt.repo.user_has_group_stats(user.id, gid)):
            return await safe_answer(q, "⚠️ You haven't finished an IPL Draft in any group yet.", True)
        data = await rt.repo.group_leaderboard(gid, user.id, LB_SIZE, page * LB_SIZE)
        title, group_id = "This Group", gid
    total = int(data.get("total") or 0)
    if page > max(0, -(-total // LB_SIZE) - 1):
        return await safe_answer(q, "That's the last page.")
    await safe_answer(q)
    await _edit(rt, q, m.leaderboard_text(data, title, page, LB_SIZE), kb.leaderboard(scope, group_id, page))


async def play_again(rt, context, q) -> None:
    from handlers import arena
    chat, user = q.message.chat, q.from_user
    if chat.type not in GROUP_TYPES:
        return await safe_answer(q, "🎮 Start new games from a group with /start.", True)
    if await arena.active_game_exists(rt.ctx, chat.id):
        return await safe_answer(q, "⚠️ A game is already in progress here. Use /status.", True)
    await rt.ctx.groups.upsert(chat.id, chat.title)
    await rt.ctx.users.upsert(user.id, user.username, user.first_name)
    await safe_answer(q)
    await arena.show_picker(rt.ctx, chat.id, user)


# ───────────────────────── batting order & next match ─────────────────────────
async def bo_start(rt, context, q, team_id: int, pos: int = 0) -> None:
    state = await batting_order_service.get_batting_order_state(rt, team_id)
    if not state:
        return await safe_answer(q, "⚠️ Team roster not found.", True)
    await safe_answer(q)
    text = m.batting_order_prompt(state["players"], state["ordered"], state["remaining"])
    markup = kb.batting_order(team_id, state["players"])
    await _edit(rt, q, text, markup)


async def bo_pick(rt, context, q, team_id: int, pos: int) -> None:
    user = q.from_user
    msg_id = q.message.message_id if q.message else None
    res = await batting_order_service.pick_batter(rt, user.id, team_id, pos, msg_id)
    if res == "NOT_YOURS":
        return await safe_answer(q, "⚠️ This is not your team.", True)
    if res == "DONE":
        return await safe_answer(q, "✅ Batting order complete! Squad locked.")
    if res == "ALREADY":
        return await safe_answer(q, "⚠️ Already selected in batting order.", True)
    await safe_answer(q)


async def bo_undo(rt, context, q, team_id: int) -> None:
    user = q.from_user
    msg_id = q.message.message_id if q.message else None
    res = await batting_order_service.undo_pick(rt, user.id, team_id, msg_id)
    if res == "NOT_YOURS":
        return await safe_answer(q, "⚠️ This is not your team.", True)
    await safe_answer(q)


async def bo_reset(rt, context, q, team_id: int) -> None:
    user = q.from_user
    msg_id = q.message.message_id if q.message else None
    res = await batting_order_service.reset_order(rt, user.id, team_id, msg_id)
    if res == "NOT_YOURS":
        return await safe_answer(q, "⚠️ This is not your team.", True)
    await safe_answer(q)


async def bo_confirm(rt, context, q, team_id: int) -> None:
    user = q.from_user
    msg_id = q.message.message_id if q.message else None
    res = await batting_order_service.confirm_order(rt, user.id, team_id, msg_id)
    if res == "NOT_YOURS":
        return await safe_answer(q, "⚠️ This is not your team.", True)
    if res == "INCOMPLETE":
        return await safe_answer(q, "⚠️ Assign all 11 players before confirming.", True)
    if res == "ALREADY":
        return await safe_answer(q, "✅ Already confirmed.", True)
    await safe_answer(q, "✅ Lineup confirmed!")


async def next_match(rt, context, q, tid: int) -> None:
    user = q.from_user
    team = await rt.repo.team_of_user(tid, user.id)
    if not team:
        return await safe_answer(q, "⚠️ You are not in this tournament.", True)
    t = await rt.repo.get(tid)
    if not t:
        return await safe_answer(q, "⚠️ Tournament not found.", True)
    if t["state"] in ("COMPLETED", "CANCELLED"):
        return await safe_answer(q, "🏁 Tournament has concluded. Check points table or summary.", True)
    await safe_answer(q, "⚡ Matches simulate automatically! Results appear in your DM.")


ROUTES = {
    "j": join, "lv": leave, "st": force_start, "ru": rules, "pk": pick, "blk": blocked_pick, "bk": back, "cf": confirm,
    "tm": my_team, "ts": status, "tb": table, "fx": fixtures, "mh": history, "at": all_teams,
    "br": bracket, "sc": scorecard, "sp": scorecard_p, "tr": table_refresh, "fp": fixtures_p,
    "mp": history_p, "ap": all_teams_p, "sm": summary, "lb": leaderboard, "pa": play_again,
    "bo": bo_start, "bop": bo_pick, "bou": bo_undo, "bor": bo_reset, "boc": bo_confirm, "nx": next_match,
    "rnd": random_pick,
}

