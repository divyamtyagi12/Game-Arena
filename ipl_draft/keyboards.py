"""Inline keyboards. Callback data is namespaced `ipl:` and always < 64 bytes (ids are compact integers)."""
from __future__ import annotations

from typing import Sequence

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup


def lobby(tid: int) -> Markup:
    return Markup([[Btn("✅ JOIN", callback_data=f"ipl:j:{tid}"), Btn("❌ LEAVE", callback_data=f"ipl:lv:{tid}")],
                   [Btn("🚀 FORCE START", callback_data=f"ipl:st:{tid}")],
                   [Btn("📖 RULES", callback_data=f"ipl:ru:{tid}")]])


def open_bot(bot_username: str) -> Markup:
    return Markup([[Btn("🤖 START THE BOT", url=f"https://t.me/{bot_username}?start=ipl")]])


NAT_CODE = {
    "India": "IND",
    "England": "ENG",
    "Australia": "AUS",
    "South Africa": "SA",
    "West Indies": "WI",
    "New Zealand": "NZ",
    "Afghanistan": "AFG",
    "Sri Lanka": "SL",
    "Bangladesh": "BAN",
    "Zimbabwe": "ZIM",
    "Ireland": "IRE",
}

ROLE_ICON = {
    "WK": "🧤",
    "BAT": "🏏",
    "AR": "🔄",
    "BOWL": "🎯",
}


def offer(offer_id: int, cands: Sequence[dict], team_id: int, roster: Sequence[dict] | None = None) -> Markup:
    from .engine.draft_engine import is_pick_legal, remaining_role_needs
    players = roster or []
    needs = remaining_role_needs(players)
    b_need = needs.get("BOWL", 0)
    wk_need = needs.get("WK", 0)
    ar_need = needs.get("AR", 0)
    tot_needed = b_need + wk_need + ar_need
    picks_left = 11 - len(players)

    # Filter strictly to legal candidates — NEVER show any block sign (🚫)
    legal_cands = [c for c in cands if is_pick_legal(players, c.get("player") or {})]

    # If criteria is left and we are in a crunch (picks_left <= tot_needed)
    # or a specific role is mandatory, provide 4 options of that required role
    if picks_left <= tot_needed and tot_needed > 0:
        deficit_cands: list[dict] = []
        if wk_need > 0:
            wk_opts = [c for c in legal_cands if (c.get("player") or {}).get("role") == "WK"]
            deficit_cands.extend(wk_opts[:4])
        if b_need > 0:
            b_opts = [c for c in legal_cands if (c.get("player") or {}).get("role") == "BOWL"]
            deficit_cands.extend(b_opts[:4])
        if ar_need > 0:
            ar_opts = [c for c in legal_cands if (c.get("player") or {}).get("role") == "AR"]
            deficit_cands.extend(ar_opts[:4])

        chosen_cands = deficit_cands if deficit_cands else legal_cands[:4]
    elif tot_needed > 0:
        # Criteria is left, but not in strict crunch yet:
        # Prioritize deficit roles (up to 4 each) plus other legal options, never showing any blocked cards
        chosen_cands = []
        if wk_need > 0:
            chosen_cands.extend([c for c in legal_cands if (c.get("player") or {}).get("role") == "WK"][:4])
        if b_need > 0:
            chosen_cands.extend([c for c in legal_cands if (c.get("player") or {}).get("role") == "BOWL"][:4])
        if ar_need > 0:
            chosen_cands.extend([c for c in legal_cands if (c.get("player") or {}).get("role") == "AR"][:4])
        chosen_ids = {c.get("pos") or c.get("position") for c in chosen_cands}
        for c in legal_cands:
            pos_key = c.get("pos") or c.get("position")
            if pos_key not in chosen_ids:
                chosen_cands.append(c)
                chosen_ids.add(pos_key)
    else:
        chosen_cands = legal_cands

    rows = []
    for c in chosen_cands:
        p = c.get("player") or {}
        role = p.get("role", "BAT")
        icon = ROLE_ICON.get(role, "🏏")
        name = p.get("name") or p.get("display_name") or "Player"
        nat = p.get("nationality", "")
        code = NAT_CODE.get(nat, (nat[:3].upper() if len(nat) >= 3 else ""))
        label = f"{icon} {name} ({code})" if code else f"{icon} {name}"
        pos = c.get("pos") or c.get("position", 1)

        cb_data = f"ipl:pk:{offer_id}:{pos}"
        rows.append([Btn(label, callback_data=cb_data)])

    rows.append([Btn("📋 MY TEAM", callback_data=f"ipl:tm:{team_id}")])
    return Markup(rows)



def draft_done(team_id: int, tid: int) -> Markup:
    """After squad is complete, go straight into batting order selection."""
    return Markup([[Btn("🏏 SET BATTING ORDER", callback_data=f"ipl:bo:{team_id}:0")],
                   [Btn("📋 VIEW MY SQUAD", callback_data=f"ipl:tm:{team_id}")]])


def confirm(offer_id: int, pos: int) -> Markup:
    return Markup([[Btn("✅ CONFIRM", callback_data=f"ipl:cf:{offer_id}:{pos}")]])


def batting_order(team_id: int, players: Sequence[dict]) -> Markup:
    """All player buttons remain visible. Selected players display a ✅ checkmark.
    Zero reset, zero undo, zero confirm buttons."""
    rows = []
    for p in players:
        is_selected = p.get("batting_order") is not None
        p_name = p.get("name") or p.get("display_name") or "Player"
        label = f"✅ {p_name}" if is_selected else p_name
        slot = p.get("slot") or p.get("pos", 1)
        rows.append([Btn(label, callback_data=f"ipl:bop:{team_id}:{slot}")])
    return Markup(rows)


def batting_order_confirm(team_id: int) -> Markup:
    return Markup([[Btn("✅ CONFIRM BATTING ORDER", callback_data=f"ipl:boc:{team_id}")]])


def dm_match_card_buttons(tid: int, match_id: int) -> Markup | None:
    """Requirement: Zero buttons on match scorecards sent to DMs."""
    return None




def group_dashboard(tid: int) -> Markup:
    return Markup([[Btn("🏆 POINTS TABLE", callback_data=f"ipl:tb:{tid}"), Btn("📅 FIXTURES", callback_data=f"ipl:fx:{tid}:0")],
                   [Btn("📋 MATCH HISTORY", callback_data=f"ipl:mh:{tid}:0"), Btn("🔥 PLAYOFF BRACKET", callback_data=f"ipl:br:{tid}")],
                   [Btn("📋 VIEW ALL TEAMS", callback_data=f"ipl:at:{tid}:0")]])


def teams_ready(tid: int) -> Markup:
    return Markup([[Btn("📋 VIEW ALL TEAMS", callback_data=f"ipl:at:{tid}:0")],
                   [Btn("🏆 START TOURNAMENT", callback_data=f"ipl:ts:{tid}")]])


def match_buttons(tid: int, match_id: int) -> Markup:
    return Markup([[Btn("📊 FULL SCORECARD", callback_data=f"ipl:sc:{match_id}:0")],
                   [Btn("🏆 POINTS TABLE", callback_data=f"ipl:tb:{tid}"), Btn("📅 FIXTURES", callback_data=f"ipl:fx:{tid}:0")]])


PAGER = {"fx": "fp", "mh": "mp", "at": "ap"}      # dashboard entry button → in-place paging button


def pager(prefix: str, ident: int, page: int, pages: int, back: str | None = None) -> Markup:
    prefix = PAGER.get(prefix, prefix)
    row = []
    if page > 0:
        row.append(Btn("◀️", callback_data=f"ipl:{prefix}:{ident}:{page - 1}"))
    row.append(Btn(f"{page + 1}/{pages}", callback_data=f"ipl:{prefix}:{ident}:{page}"))
    if page < pages - 1:
        row.append(Btn("▶️", callback_data=f"ipl:{prefix}:{ident}:{page + 1}"))
    rows = [row]
    if back:
        rows.append([Btn("🔙 BACK", callback_data=back)])
    return Markup(rows)


def table_markup(tid: int) -> Markup:
    return Markup([[Btn("🔥 PLAYOFF BRACKET", callback_data=f"ipl:br:{tid}"), Btn("📋 MATCH HISTORY", callback_data=f"ipl:mh:{tid}:0")],
                   [Btn("🔄 REFRESH", callback_data=f"ipl:tr:{tid}")]])


def final_buttons(tid: int, last_match_id: int | None) -> Markup:
    rows = []
    if last_match_id:
        rows.append([Btn("📊 FINAL SCORECARD", callback_data=f"ipl:sc:{last_match_id}:0")])
    rows += [[Btn("🏆 TOURNAMENT SUMMARY", callback_data=f"ipl:sm:{tid}")],
             [Btn("🥇 LEADERBOARD", callback_data="ipl:lb:g:0"), Btn("🔄 PLAY AGAIN", callback_data="ipl:pa")]]
    return Markup(rows)


def leaderboard(scope: str, group_id: int, page: int) -> Markup:
    g = f"ipl:lb:g:{page}"
    c = f"ipl:lb:c:{page}"
    return Markup([[Btn("🌍 GLOBAL" + (" ✅" if scope == "g" else ""), callback_data="ipl:lb:g:0"),
                    Btn("👥 GROUP" + (" ✅" if scope == "c" else ""), callback_data="ipl:lb:c:0")],
                   [Btn("◀️", callback_data=f"ipl:lb:{scope}:{max(page - 1, 0)}"), Btn("▶️", callback_data=f"ipl:lb:{scope}:{page + 1}")],
                   [Btn("🔙 BACK", callback_data="lbd:menu")]])
