"""All IPL Draft user-facing text (Telegram HTML). No ratings are ever rendered here — only names, roles, nationality,
real scorecard numbers and standings."""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .engine.playoffs import STAGE_LABEL
from .engine.standings import overs_str
from .engine.team_builder import FRANCHISE_EMOJI

LINE = "━━━━━━━━━━━━━━━━"
ROLE_EMOJI = {"BAT": "🏏", "BOWL": "🎯", "AR": "⚡", "WK": "🧤"}
ROLE_NAME = {"BAT": "Batter", "BOWL": "Bowler", "AR": "All-rounder", "WK": "Wicketkeeper"}
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉", 4: "🏅"}


def esc(x: Any) -> str:
    return html.escape(str(x), quote=False)


def player_line(p: Mapping, with_nat: bool = True) -> str:
    extra = f" · {esc(p['nationality'])}" if with_nat and p.get("nationality") else ""
    return f"{esc(p['name'])} — {ROLE_EMOJI.get(p['role'], '🏏')} {ROLE_NAME.get(p['role'], p['role'])}{extra}"


def team_badge(t: Mapping) -> str:
    if t.get("kind") == "SYSTEM":
        return FRANCHISE_EMOJI.get(t.get("franchise_code") or t.get("short_name"), "🤖")
    return "🔴"


def fmt_score(inn: Mapping) -> str:
    return f"{inn['runs']}/{inn['wickets']} ({overs_str(inn['legal_balls'])} ov)"


def clock(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
        return d.strftime("%H:%M:%S UTC")
    except ValueError:
        return ""


# ───────────────────────── lobby ─────────────────────────
def lobby_text(t: Mapping, teams: Sequence[Mapping]) -> str:
    n, mx = len(teams), t["max_humans"]
    if teams:
        rows = [f"{i + 1}. {esc(teams[i]['owner_name'])}" for i in range(n)]
    else:
        rows = ["<i>No players yet.</i>"]
    status = {"LOBBY": "", "CANCELLED": "\n\n❌ <b>Cancelled</b>"}.get(t["state"], "\n\n🔒 <b>Lobby closed — the draft has started!</b>")
    return (
        "🏏 <b>IPL DRAFT</b>\n\n🏆 <b>MULTIPLAYER CRICKET TOURNAMENT</b>\n\n"
        f"👥 Human Players: <b>{n}/{mx}</b>\n🤖 System Teams: <b>4</b>\n\n"
        "🏏 Squad Size: <b>11 Players</b>\n\n🎲 Draft System:\n<b>11 Random Choices Per Pick</b>\n\n"
        "🏆 Tournament:\n<b>Double Round Robin + IPL Playoffs</b>\n\n"
        f"{LINE}\n\n👥 <b>JOINED PLAYERS</b>\n\n" + "\n".join(rows) + f"\n\n{LINE}"
        f"\n<i>Minimum {t['min_humans']} players · only group admins can force-start</i>" + status)


def rules_text(draft_seconds: int = 45) -> str:
    return (
        "📖 <b>IPL DRAFT — RULES</b>\n\n"
        "👥 2–8 human managers + <b>4 system franchises</b> (RCB 🔴, KKR 🟣, MI 🔵, CSK 🟡).\n\n"
        f"🎲 <b>Draft (in your DM):</b> 11 rounds. Each round you get <b>11 random IPL cricketers</b> — pick exactly "
        f"<b>one</b> ({draft_seconds}s; if you're slow one is picked for you). No role restrictions: all batters is allowed… "
        "but the match engine will notice.\n\n"
        "🔒 Nobody else can be offered the cricketers you're choosing from, and nobody can draft the same player twice.\n\n"
        "🏏 <b>Batting Order:</b> After your squad is complete, you'll set your batting lineup (1-11) via DM.\n"
        "If you time out, a smart order is auto-assigned.\n\n"
        "🏟 <b>League:</b> double round robin — every team plays every other team twice. Win = 2 pts, no result = 1. "
        "Table: points → net run rate → wins.\n\n"
        "🔥 <b>Playoffs (top 4):</b> Eliminator (3v4) → Qualifier 1 (1v2) → Qualifier 2 → 🏆 Final. "
        "A system franchise can win it too!\n\n"
        "📱 Match results appear in your <b>private DM</b>. The group only sees milestones and the final champion.\n\n"
        "🧠 Every match is a real ball-by-ball T20 simulation of the two squads — ratings stay hidden, scorecards are real.\n\n"
        "🏅 Awards: Orange Cap, Purple Cap, Player of the Tournament, Most Sixes, Best Innings.")



DM_PROMPT = "⚠️ Please start the bot privately before joining IPL Draft."


# ───────────────────────── draft ─────────────────────────
def offer_text(team: Mapping, offer: Mapping, picks_done: int, seconds: int) -> str:
    cands = offer["candidates"]
    lines = [f"{c['pos']}. {player_line(c['player'])}" for c in cands]
    dl = clock(offer.get("deadline_at"))
    return (
        "🏏 <b>IPL DRAFT</b>\n\n"
        f"🎲 <b>DRAFT ROUND {offer['round_no']}/11</b>\n\n"
        f"👤 Team: <b>{esc(team['name'])}</b>\n\n🏏 Players Selected: <b>{picks_done}/11</b>\n\n{LINE}\n\n"
        "🎯 <b>CHOOSE ONE PLAYER</b>\n\n" + "\n".join(lines) + f"\n\n{LINE}\n"
        f"⏱ <b>{seconds} seconds</b> — deadline {dl}\n<i>If time runs out, one is picked for you.</i>")


def confirm_text(player: Mapping) -> str:
    return f"🏏 <b>PLAYER SELECTED</b>\n\n<b>{esc(player['name'])}</b>\n{ROLE_EMOJI.get(player['role'], '')} {ROLE_NAME.get(player['role'], '')} · {esc(player.get('nationality', ''))}\n\nAdd to your squad?"


def drafted_text(player: Mapping, roster: Sequence[Mapping], auto: bool = False) -> str:
    name = esc(player.get("name") or player.get("display_name") or "?")
    count = len(roster)
    head = f"⏱ <b>TIME UP — {name} auto-selected!</b>" if auto else f"✅ <b>{name} selected!</b>"
    return (f"{head}\n\n"
            f"🏏 <b>Your Squad:</b> {count}/11\n\n"
            "Proceeding to the next pick...")


def my_team_text(team: Mapping, players: Sequence[Mapping], complete: bool) -> str:
    rows = "\n".join(f"{i + 1}. {player_line(p)}" for i, p in enumerate(players)) or "<i>No players yet.</i>"
    return f"📋 <b>{esc(team['name'])}</b> — {len(players)}/11\n\n{rows}" + ("" if complete else "\n\n✍️ Draft in progress…")


def draft_complete_text(team: Mapping, players: Sequence[Mapping]) -> str:
    name = esc(players[-1].get("name") or players[-1].get("display_name") or "?") if players else "Player"
    return (f"✅ <b>{name} selected!</b>\n\n"
            "🏏 <b>Your Squad:</b> 11/11\n\n"
            "🔒 <b>Squad complete!</b> Proceeding to batting order...")




def draft_progress_text(rows: Sequence[Mapping]) -> str:
    out = ["🏏 <b>IPL DRAFT — DRAFTING IN PROGRESS</b>", "", "Everyone is picking in their DMs (squads stay secret until the draft ends).", ""]
    for r in rows:
        mark = "✅" if r["done"] else "⏳"
        auto = " 🤖" if r.get("auto") and not r["done"] else ""
        out.append(f"{mark} {esc(r['name'])} — {r['picks']}/11{auto}")
    return "\n".join(out)


def system_draft_text(system_teams: Sequence[Mapping]) -> str:
    rows = "\n".join(f"✅ {t['short_name']} — READY" for t in system_teams)
    return f"🤖 <b>SYSTEM DRAFT COMPLETE!</b>\n\n{rows}\n\n🏏 <b>ALL TEAMS READY!</b>"


def all_teams_page(rosters: Sequence[Mapping], page: int, per_page: int = 4) -> tuple[str, int]:
    pages = max(1, -(-len(rosters) // per_page))
    page = max(0, min(page, pages - 1))
    chunk = rosters[page * per_page:(page + 1) * per_page]
    blocks = []
    for r in chunk:
        t = r["team"]
        pl = "\n".join(f"  {i + 1}. {esc(p['name'])} <i>({ROLE_NAME[p['role']][:3]})</i>" for i, p in enumerate(r["players"]))
        blocks.append(f"{team_badge(t)} <b>{esc(t['name'])}</b>\n{pl}")
    return f"📋 <b>ALL TEAMS</b> — page {page + 1}/{pages}\n\n" + "\n\n".join(blocks), pages


# ───────────────────────── tables / dashboard ─────────────────────────
def _tname(r: Mapping, w: int = 9) -> str:
    return (r["short_name"] if r["kind"] == "SYSTEM" else r["name"])[:w]


def points_table(rows: Sequence[Mapping], title: str = "IPL DRAFT POINTS TABLE", qualified: bool = True) -> str:
    out = [f"🏆 <b>{title}</b>", "", "<pre>POS TEAM       P  W  L PTS   NRR"]
    for i, r in enumerate(rows, 1):
        out.append(f"{i:>2}. {_tname(r, 9):<9} {r['played']:>2} {r['won']:>2} {r['lost']:>2} {r['points']:>3} {float(r['nrr']):>+6.2f}")
        if i == 4 and len(rows) > 4:
            out.append("─" * 31)
    out.append("</pre>")
    if qualified and rows and rows[0]["played"] >= 0:
        out.append("✅ Top 4 qualify for the playoffs")
    return "\n".join(out)


def progress_bar(done: int, total: int, width: int = 12) -> str:
    n = 0 if total == 0 else round(width * done / total)
    return "█" * n + "░" * (width - n)


def dashboard_text(t: Mapping, prog: Mapping, standings: Sequence[Mapping], human_lines: Sequence[str]) -> str:
    total, done = prog["total"], prog["done"]
    md = prog["completed_matchdays"]
    head = ("🏏 <b>IPL DRAFT — LEAGUE STAGE</b>\n\n"
            f"📅 Matchday <b>{md}/{prog['matchdays']}</b>   ·   Matches <b>{done}/{total}</b>\n"
            f"{progress_bar(done, total)} {0 if not total else round(100 * done / total)}%\n\n")
    top = standings[:4]
    tbl = "\n".join(f"{MEDALS.get(i + 1, '')} {_tname(r, 14)} — {r['points']} pts · NRR {float(r['nrr']):+.2f}"
                    for i, r in enumerate(top))
    hl = ("\n\n🔴 <b>Latest results — human teams</b>\n" + "\n".join(human_lines)) if human_lines else ""
    return head + "<b>Top 4 right now</b>\n" + tbl + hl


def matchday_summary(md: int, cards: Sequence[Mapping]) -> str:
    out = [f"📅 <b>MATCHDAY {md} — RESULTS</b>", ""]
    for c in cards:
        out.append(result_line(c))
    return "\n".join(out)


def result_line(c: Mapping) -> str:
    t1, t2 = c["team1"], c["team2"]
    inn = [i for i in c["innings"] if not i["is_super_over"]]
    s1 = f"{inn[0]['runs']}/{inn[0]['wickets']}" if inn else "-"
    s2 = f"{inn[1]['runs']}/{inn[1]['wickets']}" if len(inn) > 1 else "-"
    win = t1 if c["winner_team_id"] == t1["id"] else t2 if c["winner_team_id"] == t2["id"] else None
    w = f" → <b>{esc(_tname_t(win))}</b>" if win else " → no result"
    return f"#{c['match_no']} {esc(_tname_t(t1))} {s1} v {esc(_tname_t(t2))} {s2}{w}"


def _tname_t(t: Mapping, w: int = 12) -> str:
    return (t["short_name"] if t["kind"] == "SYSTEM" else t["name"])[:w]


def human_result_line(c: Mapping, human_ids: set[int]) -> str | None:
    t1, t2 = c["team1"], c["team2"]
    if t1["id"] not in human_ids and t2["id"] not in human_ids:
        return None
    return result_line(c)


# ───────────────────────── match cards ─────────────────────────
def result_sentence(c: Mapping) -> str:
    if c.get("summary"):
        return esc(c["summary"])
    t1, t2 = c.get("team1") or {}, c.get("team2") or {}
    win = t1 if c.get("winner_team_id") == t1.get("id") else t2 if c.get("winner_team_id") == t2.get("id") else None
    if not win:
        return "NO RESULT"
    n = esc(win.get("name", "?").upper())
    res_type = c.get("result_type")
    margin = c.get("margin", 0)
    if res_type == "RUNS":
        return f"{n} WINS BY {margin} RUN{'S' if margin != 1 else ''}!"
    if res_type == "WICKETS":
        return f"{n} WINS BY {margin} WICKET{'S' if margin != 1 else ''}!"
    return f"{n} WINS THE SUPER OVER!"


def match_card_text(c: Mapping, header: str | None = None) -> str:
    t1, t2 = c["team1"], c["team2"]
    inn = [i for i in c["innings"] if not i["is_super_over"]]
    s = {c["team1"]["id"]: fmt_score(inn[0]) if inn else "", c["team2"]["id"]: fmt_score(inn[1]) if len(inn) > 1 else ""}
    pom, bb = c.get("pom"), c.get("best_bowler")
    head = header or f"🏏 <b>IPL DRAFT — {'MATCH ' + str(c['match_no']) if c['stage'] == 'LEAGUE' else esc(STAGE_LABEL[c['stage']].upper())}</b>"
    text = (f"{head}\n\n{team_badge(t1)} <b>{esc(t1['name'].upper())}</b>\n{s[t1['id']]}\n\nVS\n\n"
            f"{team_badge(t2)} <b>{esc(t2['name'].upper())}</b>\n{s[t2['id']]}\n\n{LINE}\n\n🏆 <b>{result_sentence(c)}</b>")
    if c.get("super_overs"):
        so = [i for i in c["innings"] if i["is_super_over"]]
        if len(so) >= 2:
            text += f"\n<i>Super Over: {so[-2]['runs']}/{so[-2]['wickets']} v {so[-1]['runs']}/{so[-1]['wickets']}</i>"
    if pom:
        text += f"\n\n⭐ <b>PLAYER OF THE MATCH</b>\n{esc(pom['name'])}"
        if pom["balls"]:
            text += f"\n{pom['runs']} Runs ({pom['balls']} Balls)"
        if pom["wickets"]:
            text += f"\n{pom['wickets']}/{pom['bowl_runs']}"
    if bb and bb["wickets"]:
        text += f"\n\n🎯 <b>BEST BOWLER</b>\n{esc(bb['name'])}\n{bb['wickets']}/{bb['runs']}"
    return text + f"\n\n{LINE}"


def scorecard_pages(sc: Mapping) -> list[str]:
    pages = [match_card_text(sc)]
    for inn in sc["innings_detail"]:
        tag = "SUPER OVER " if inn["is_super_over"] else ""
        bt = inn["batting_team"]
        lines = [f"📊 <b>{tag}INNINGS {inn['innings_no'] if not inn['is_super_over'] else ''} — {esc(bt['name'].upper())}</b>",
                 f"<b>{inn['runs']}/{inn['wickets']}</b> ({overs_str(inn['legal_balls'])} ov)", "", "<b>BATTING</b>", "<pre>Player          R   B  4s 6s   SR"]
        for b in inn["batting"]:
            if not b["did_bat"]:
                continue
            sr = f"{b['runs'] * 100 / b['balls']:.0f}" if b["balls"] else "-"
            mark = "" if b["out"] else "*"
            lines.append(f"{b['name'][:14]:<14} {str(b['runs']) + mark:>4} {b['balls']:>3} {b['fours']:>3}{b['sixes']:>3} {sr:>4}")
        lines.append("</pre>")
        ex = inn["extras"]
        total_ex = sum(ex.values())
        lines.append(f"Extras: <b>{total_ex}</b> (w {ex.get('wides', 0)}, nb {ex.get('noballs', 0)}, b {ex.get('byes', 0)}, lb {ex.get('legbyes', 0)})")
        dnb = [b["name"] for b in inn["batting"] if not b["did_bat"]]
        if dnb:
            lines.append("Did not bat: " + esc(", ".join(dnb)))
        fow = inn.get("fall_of_wickets") or []
        if fow:
            lines.append("\n<b>FALL OF WICKETS</b>\n" + esc(", ".join(f"{f['score']}-{f['wkt']} ({f['player']}, {f['over']})" for f in fow)))
        lines += ["", "<b>BOWLING</b>", "<pre>Player          O   R   W  Econ"]
        for w in inn["bowling"]:
            ov = overs_str(w["legal_balls"])
            econ = f"{w['runs'] * 6 / w['legal_balls']:.2f}" if w["legal_balls"] else "-"
            lines.append(f"{w['name'][:14]:<14} {ov:>4} {w['runs']:>3} {w['wickets']:>3} {econ:>5}")
        lines.append("</pre>")
        pages.append("\n".join(lines))
    return pages


def resolve_team_name(team: Mapping | None, fallback: str = "Team") -> str:
    """Safely extracts a valid team name, never returning ?, None, null, or undefined."""
    if not team or not isinstance(team, Mapping):
        return fallback
    for k in ("name", "short_name", "team_name"):
        v = team.get(k)
        if v and str(v).strip() not in ("?", "None", "null", "undefined", ""):
            return str(v).strip()
    return fallback


def full_scorecard_pages(c: Mapping) -> list[str]:
    """Rich automatic DM scorecard — no buttons.
    Format matches:  header / innings 1 / innings 2 / result+winner
    Split into Telegram-safe pages (<= 4096 chars each)."""
    DIVIDER = "━━━━━━━━━━━━━━━━━━━━"
    TG_LIMIT = 4000   # leave headroom

    # Resolve team names robustly (team1/team2 or home_team/away_team)
    t1 = c.get("team1") or c.get("home_team") or {}
    t2 = c.get("team2") or c.get("away_team") or {}
    t1_name = esc(resolve_team_name(t1, "Team 1"))
    t2_name = esc(resolve_team_name(t2, "Team 2"))

    raw_stage = str(c.get("stage", "LEAGUE"))
    stage = STAGE_LABEL.get(raw_stage.removeprefix("PLAYOFF_"), STAGE_LABEL.get(raw_stage, raw_stage.title()))
    is_playoff = raw_stage.startswith("PLAYOFF_") or raw_stage in ("QUALIFIER_1", "ELIMINATOR", "QUALIFIER_2", "FINAL")
    stage_icon = "🏆" if is_playoff else "🏏"
    header = (f"{stage_icon} <b>IPL DRAFT — {esc(stage.upper())}</b>\n\n"
              f"🔥 <b>{t1_name}</b> vs <b>{t2_name}</b>")

    def _innings_block(inn: Mapping) -> str:
        bt = inn.get("batting_team") or {}
        default_bat = t1_name if inn.get("innings_no") == 1 else t2_name
        bat_name = esc(resolve_team_name(bt, default_bat).upper())
        total = f"{inn['runs']}/{inn['wickets']} ({overs_str(inn['legal_balls'])} ov)"
        block = [f"\n{DIVIDER}\n", f"🏏 <b>{bat_name} — {total}</b>\n"]

        # batting table
        batters = [b for b in inn.get("batting", []) if b.get("did_bat")]
        if batters:
            block.append("<pre>BATSMAN              R    B   4s  6s")
            for b in batters:
                mark = "" if b.get("out") else "*"
                nm = b.get("name", "?")[:19]
                block.append(
                    f"{nm:<19} {str(b['runs']) + mark:>4} {b['balls']:>4} "
                    f"{b['fours']:>4} {b['sixes']:>3}"
                )
            block.append("</pre>")

        # bowling table
        bowlers = [w for w in inn.get("bowling", []) if w.get("legal_balls", 0) > 0]
        if bowlers:
            block.append("\n🎯 <b>BOWLING</b>")
            block.append("<pre>BOWLER               O    R   W")
            for w in bowlers:
                ov = overs_str(w["legal_balls"])
                nm = w.get("name", "?")[:19]
                block.append(f"{nm:<19} {ov:>4} {w['runs']:>4} {w['wickets']:>3}")
            block.append("</pre>")

        # extras
        ex = inn.get("extras") or {}
        total_ex = sum(ex.values()) if isinstance(ex, dict) else 0
        if total_ex:
            block.append(f"<i>Extras: {total_ex} (w {ex.get('wides', 0)}, nb {ex.get('noballs', 0)}, b {ex.get('byes', 0)}, lb {ex.get('legbyes', 0)})</i>")

        return "\n".join(block)

    # Build all innings blocks
    main_innings = [i for i in (c.get("innings_detail") or []) if not i.get("is_super_over")]
    super_over = [i for i in (c.get("innings_detail") or []) if i.get("is_super_over")]

    inn_blocks = [_innings_block(i) for i in main_innings]

    # Result
    pom = c.get("pom")
    bb = c.get("best_bowler")
    result_block = f"\n{DIVIDER}\n\n🏆 <b>{result_sentence(c)}</b>"
    if pom:
        details = []
        if pom.get("balls"):
            details.append(f"{pom['runs']} runs, {pom['balls']} balls")
        elif pom.get("runs"):
            details.append(f"{pom['runs']} runs")
        if pom.get("wickets"):
            details.append(f"{pom['wickets']}/{pom.get('bowl_runs', 0)}")
        det_str = f" ({', '.join(details)})" if details else ""
        result_block += f"\n\n⭐ <b>PLAYER OF THE MATCH</b>: {esc(pom['name'])}{det_str}"
    if bb and bb.get("wickets"):
        result_block += f"\n🎯 <b>BEST BOWLER</b>: {esc(bb['name'])} ({bb['wickets']}/{bb['runs']})"
    if super_over:
        so_txt = " | ".join(
            f"{resolve_team_name(i.get('batting_team'), '?')}: {i['runs']}/{i['wickets']}"
            for i in super_over
        )
        result_block += f"\n<i>Super Over: {so_txt}</i>"

    # Pack into Telegram-safe pages
    pages: list[str] = []
    current = header
    for block in inn_blocks + [result_block]:
        if len(current) + len(block) > TG_LIMIT:
            pages.append(current)
            current = block.lstrip("\n")
        else:
            current += block
    pages.append(current)

    return pages


def format_full_scorecard(match_data: Mapping, stage: str = "LEAGUE", user_id: int | None = None) -> list[str]:
    """Single centralized function to format match scorecards for all stages (LEAGUE, QUALIFIER_1,
    ELIMINATOR, QUALIFIER_2, FINAL). Reused for every match, sent to DM with zero inline buttons."""
    c = dict(match_data)
    if stage:
        c["stage"] = stage
    pages = full_scorecard_pages(c)
    if user_id is not None and pages:
        winner_id = c.get("winner_team_id")
        if winner_id == user_id:
            pages[-1] += "\n\n🎉 <b>YOU WON!</b>"
        elif winner_id is not None:
            pages[-1] += "\n\n😔 <b>You lost.</b>"
    return pages



def fixtures_page_text(data: Mapping, page: int, per_page: int) -> str:
    pages = max(1, -(-data["total"] // per_page))
    out = [f"📅 <b>FIXTURES</b> — page {page + 1}/{pages}", ""]
    for f in data["rows"]:
        tag = "" if f["stage"] == "LEAGUE" else f" [{STAGE_LABEL[f['stage']]}]"
        st = {"COMPLETED": f"✅ {esc(f['winner_short'] or 'NR')}", "CLAIMED": "⏳", "SCHEDULED": "·"}[f["status"]]
        out.append(f"#{f['match_no']} {esc(f['home'])} v {esc(f['away'])}{tag}  {st}")
    return "\n".join(out)


def history_page_text(data: Mapping, page: int, per_page: int) -> str:
    pages = max(1, -(-data["total"] // per_page))
    out = [f"📋 <b>MATCH HISTORY</b> — page {page + 1}/{pages}", ""]
    out += [result_line(c) for c in data["rows"]] or ["<i>No matches played yet.</i>"]
    return "\n".join(out)


def bracket_text(bracket: Sequence[Mapping], standings: Sequence[Mapping]) -> str:
    out = ["🔥 <b>IPL PLAYOFF BRACKET</b>", ""]
    names = {"ELIMINATOR": "🔥 ELIMINATOR", "QUALIFIER_1": "🏆 QUALIFIER 1", "QUALIFIER_2": "⚔️ QUALIFIER 2", "FINAL": "👑 GRAND FINAL"}
    src = {"RANK1": "Rank 1", "RANK2": "Rank 2", "RANK3": "Rank 3", "RANK4": "Rank 4", "LOSER_Q1": "Loser Q1",
           "WINNER_ELIMINATOR": "Winner Eliminator", "WINNER_Q1": "Winner Q1", "WINNER_Q2": "Winner Q2"}
    for b in bracket:
        h = esc(b["home"]["name"]) if b.get("home") else src[b["home_source"]]
        a = esc(b["away"]["name"]) if b.get("away") else src[b["away_source"]]
        status = "✅" if b.get("status") == "COMPLETED" else ""
        out.append(f"{names[b['stage']]} {status}\n   {h}\n   vs {a}")
        if b.get("winner_team_id") and b.get("home") and b.get("away"):
            w = b["home"] if b["winner_team_id"] == b["home"]["id"] else b["away"]
            out.append(f"   🏆 {esc(w['name'])}")
        out.append("")
    return "\n".join(out)


def league_complete_text(rows: Sequence[Mapping]) -> str:
    out = [
        "🏆 <b>IPL DRAFT — LEAGUE STAGE COMPLETED!</b>",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "<pre>POS  TEAM          P   W   L   PTS    NRR",
    ]
    for i, r in enumerate(rows, 1):
        tag = f"Q{i}" if i <= 4 else f"E{i}"
        name = _tname(r, 12)
        p = r.get("played", 0)
        w = r.get("won", 0)
        l = r.get("lost", 0)
        pts = r.get("points", 0)
        nrr = float(r.get("nrr", 0.0))
        out.append(f"{tag:<4} {name:<12} {p:>3} {w:>3} {l:>3} {pts:>5} {nrr:>+7.2f}")
    out.append("</pre>")
    out.append("")
    out.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    out.append("")
    out.append("✅ Q = Qualified for Playoffs")
    out.append("❌ E = Eliminated")
    out.append("")
    out.append("🔥 <b>TOP 4 QUALIFIED!</b>")
    out.append("")
    out.append("🏆 <b>IPL PLAYOFFS BEGIN!</b>")
    return "\n".join(out)


def playoff_intro(stage: str, home: Mapping, away: Mapping) -> str:
    adv = {"ELIMINATOR": "Winner → Qualifier 2\nLoser → Eliminated", "QUALIFIER_1": "Winner → FINAL\nLoser → Qualifier 2",
           "QUALIFIER_2": "Winner → FINAL\nLoser → Eliminated", "FINAL": "Winner → IPL DRAFT CHAMPION"}[stage]
    title = {"ELIMINATOR": "🔥 ELIMINATOR", "QUALIFIER_1": "🏆 QUALIFIER 1", "QUALIFIER_2": "⚔️ QUALIFIER 2", "FINAL": "🏆 IPL DRAFT GRAND FINAL!"}[stage]
    return f"<b>{title}</b>\n\n{team_badge(home)} {esc(home['name'].upper())}\nVS\n{team_badge(away)} {esc(away['name'].upper())}\n\n{adv}\n\n<i>Simulating…</i>"


def champion_text(card: Mapping, champ: Mapping, owner: str | None) -> str:
    own = f"\n\n👤 OWNER: <b>{esc(owner.upper())}</b>" if owner else "\n\n🤖 <i>System franchise</i>"
    return (match_card_text(card, "🏆 <b>IPL DRAFT GRAND FINAL!</b>") + f"\n\n👑 <b>IPL DRAFT CHAMPION!</b>\n\n🏆 <b>{esc(champ['name'].upper())}</b>{own}\n\n{LINE}")


AWARD_TITLES = {"CHAMPION": "👑 CHAMPION", "RUNNER_UP": "🥈 RUNNER-UP", "ORANGE_CAP": "🏏 ORANGE CAP",
                "PURPLE_CAP": "🎯 PURPLE CAP", "PLAYER_OF_TOURNAMENT": "⭐ PLAYER OF THE TOURNAMENT",
                "MOST_SIXES": "💥 MOST SIXES", "BEST_INNINGS": "🔥 BEST INDIVIDUAL INNINGS"}


def awards_text(awards: Sequence[Mapping]) -> str:
    by = {a["award"]: a for a in awards}
    out = ["🏆 <b>IPL DRAFT AWARDS</b>", ""]
    for k, title in AWARD_TITLES.items():
        a = by.get(k)
        if not a:
            continue
        who = a.get("player") or a.get("team") or ""
        detail = f"\n{esc(a['value_text'])}" if a.get("player") and a.get("value_text") else ""
        team = f" <i>({esc(a['team'])})</i>" if a.get("player") and a.get("team") else ""
        out.append(f"{title}\n<b>{esc(who)}</b>{team}{detail}\n")
    return "\n".join(out)


# ───────────────────────── leaderboards / stats ─────────────────────────
def _name(r: Mapping) -> str:
    return esc(r.get("first_name") or r.get("username") or f"User {r['user_id']}")


def leaderboard_menu_text() -> str:
    return "🏏 <b>IPL DRAFT LEADERBOARD</b>\n\nRanked by championships, then runner-ups, then league wins.\nOnly human managers appear here."


def leaderboard_text(data: Mapping, title: str, page: int, size: int) -> str:
    rows = data.get("rows") or []
    out = [f"🏏 <b>IPL DRAFT — {esc(title)}</b>", ""]
    if not rows:
        out.append("<i>No finished tournaments yet.</i>")
    for r in rows:
        medal = MEDALS.get(r["rnk"], f"{r['rnk']}.")
        wr = f"{100 * r['championships'] / r['tournaments_played']:.0f}%" if r["tournaments_played"] else "0%"
        out.append(f"{medal} <b>{_name(r)}</b> — 🏆 {r['championships']} · 🥈 {r['runner_ups']} · {r['tournaments_played']} played ({wr} titles)")
    me = data.get("me")
    if me and not any(r["user_id"] == me["user_id"] for r in rows):
        out += ["", f"You: #{me['rnk']} — 🏆 {me['championships']} · 🥈 {me['runner_ups']}"]
    out.append(f"\n<i>Page {page + 1}/{max(1, -(-int(data.get('total') or 0) // size))}</i>")
    return "\n".join(out)


def user_stats_text(name: str, st: Mapping | None, scope: str = "Global") -> str:
    if not st:
        return f"📊 <b>IPL DRAFT — {esc(scope)} stats for {esc(name)}</b>\n\n<i>No finished tournaments yet. Join one with /start in a group!</i>"
    played = st["league_played"]
    wpct = f"{100 * st['league_wins'] / played:.0f}%" if played else "0%"
    tpct = f"{100 * st['championships'] / st['tournaments_played']:.0f}%" if st["tournaments_played"] else "0%"
    return (f"📊 <b>IPL DRAFT — {esc(scope)} stats for {esc(name)}</b>\n\n"
            f"🏟 Tournaments played: <b>{st['tournaments_played']}</b>\n🏆 Championships: <b>{st['championships']}</b>\n"
            f"🥈 Runner-up finishes: <b>{st['runner_ups']}</b>\n🔥 Playoff appearances: <b>{st['playoff_appearances']}</b>\n"
            f"✅ League wins: <b>{st['league_wins']}</b>  ❌ losses: <b>{st['league_losses']}</b> ({wpct})\n"
            f"📈 Tournament win rate: <b>{tpct}</b>\n🏏 Runs by your teams: <b>{st['runs_scored']}</b>\n🎯 Wickets by your teams: <b>{st['wickets_taken']}</b>")


def history_text(rows: Sequence[Mapping]) -> str:
    out = ["🏏 <b>IPL DRAFT — GROUP HISTORY</b>", ""]
    for r in rows:
        who = esc(r["champion"] or "?") + ("" if r["champion_kind"] == "HUMAN" else " 🤖")
        out.append(f"#{r['tournament_id']} 👑 <b>{who}</b> · 🥈 {esc(r['runner_up'] or '?')} · {r['humans']} managers")
    if not rows:
        out.append("<i>No completed tournaments in this group yet.</i>")
    return "\n".join(out)


def status_text(t: Mapping, humans: int, extra: str = "") -> str:
    labels = {"LOBBY": "Lobby", "DRAFTING": "Drafting", "SYSTEM_TEAM_GENERATION": "System teams drafting",
              "FIXTURE_GENERATION": "Building fixtures", "LEAGUE_RUNNING": "League running",
              "LEAGUE_COMPLETED": "League completed", "PLAYOFF_ELIMINATOR": "Playoffs — Eliminator",
              "PLAYOFF_QUALIFIER_1": "Playoffs — Qualifier 1", "PLAYOFF_QUALIFIER_2": "Playoffs — Qualifier 2",
              "PLAYOFF_FINAL": "Playoffs — Final", "COMPLETED": "Completed", "CANCELLED": "Cancelled",
              "FAILED_RECOVERABLE": "⚠️ Paused (an admin can /iplresume)"}
    return f"📊 <b>IPL DRAFT — Status</b>\n\nStage: <b>{labels.get(t['state'], t['state'])}</b>\nHuman managers: {humans}{extra}"


# ───────────────────────── batting order ─────────────────────────
def batting_order_prompt(players: Sequence[Mapping], ordered: Sequence[Mapping], remaining: Sequence[Mapping] = None) -> str:
    """DM screen for the batting order selection flow."""
    done = len(ordered)
    head = f"🏏 <b>SET YOUR BATTING ORDER ({done}/11)</b>\n\nSelected: <b>{done}/11</b>\n\n"
    if ordered:
        lines = "\n".join(f"{i + 1}. {esc(p['name'])}" for i, p in enumerate(ordered))
        head += f"<b>Batting Order:</b>\n{lines}\n\n"
    if remaining is None:
        ordered_ids = {p.get("id") or p.get("name") for p in ordered}
        remaining = [p for p in players if (p.get("id") or p.get("name")) not in ordered_ids]
    if remaining:
        head += f"Select <b>batter #{done + 1}</b> from the buttons below:"
    return head.strip()


def batting_order_confirmed_text(players: Sequence[Mapping]) -> str:
    rows = "\n".join(f"{i + 1}. {esc(p['name'])}" for i, p in enumerate(players))
    return (f"✅ <b>BATTING ORDER COMPLETE</b>\n\n"
            f"{rows}\n\n"
            "🔒 <b>Squad Locked!</b>")


def batting_order_timeout_text(players: Sequence[Mapping]) -> str:
    rows = "\n".join(f"  {i + 1}. <b>{esc(p['name'])}</b> {ROLE_EMOJI.get(p['role'], '')}" for i, p in enumerate(players))
    return (f"⏱ <b>TIME UP — BATTING ORDER AUTO-ASSIGNED</b>\n\n{rows}\n\n"
            "⏳ Waiting for the league to start…")


# ───────────────────────── DM match results ─────────────────────────
def dm_match_result(card: Mapping, your_team_id: int) -> str:
    """Short DM card shown to a human manager after their team's match."""
    # ipl_match_card returns team1 (batted first) / team2; "home_team"/"away_team" are accepted for older callers.
    # (Regression: only home/away were read, so every DM card showed "? vs ?" with "?" score lines.)
    h = card.get("team1") or card.get("home_team") or {}
    a = card.get("team2") or card.get("away_team") or {}
    you = h if h.get("id") == your_team_id else a
    opp = a if h.get("id") == your_team_id else h
    inn = card.get("innings") or []
    score_lines = []
    for i in inn:
        if i.get("is_super_over"):
            continue
        t = h if i.get("batting_team_id") == h.get("id") else a
        score_lines.append(f"  {esc(t.get('short_name', t.get('name', '?')))}: {i['runs']}/{i['wickets']} ({overs_str(i['legal_balls'])} ov)")
    winner_id = card.get("winner_team_id")
    if winner_id is None:
        result = "🤝 No result"
    elif winner_id == your_team_id:
        result = "🎉 <b>YOU WON!</b>"
    else:
        result = "😔 <b>You lost.</b>"
    stage = STAGE_LABEL.get(card.get("stage", "LEAGUE"), "League")
    return (f"🏏 <b>IPL DRAFT — {stage.upper()}</b>\n\n"
            f"{esc(you.get('name', '?'))} vs {esc(opp.get('name', '?'))}\n\n"
            + "\n".join(score_lines)
            + f"\n\n{esc(card.get('summary', ''))}\n\n{result}")


def dm_playoff_intro(stage: str, your_team_id: int, home: Mapping, away: Mapping) -> str:
    """DM message previewing an upcoming playoff match for a human participant."""
    you = home if home.get("id") == your_team_id else away
    opp = away if home.get("id") == your_team_id else home
    stage_name = STAGE_LABEL.get(stage, stage)
    return (f"🔥 <b>{stage_name.upper()}</b>\n\n"
            f"Your team: <b>{esc(you.get('name', '?'))}</b>\n"
            f"Opponent: <b>{esc(opp.get('name', '?'))}</b>\n\n"
            "⚡ Match simulation starting…")


def squad_performance_text(players: Sequence[Mapping], stats: Sequence[Mapping] | Mapping[Any, Mapping]) -> str:
    """Format the XI squad tournament performance sent to user DM at tournament conclusion.

    Example:
    📋 Your XI — Tournament Performance

    1. Sunil Narine — 288 runs, 21 wkts
    2. Travis Head — 293 runs
    3. Nicholas Pooran — 385 runs
    4. Hardik Pandya — 293 runs, 22 wkts
    ...
    Team totals: 2369 runs, 129 wickets
    """
    stats_by_id: dict[str, Mapping] = {}
    stats_by_name: dict[str, Mapping] = {}
    if isinstance(stats, Mapping):
        for k, v in stats.items():
            if isinstance(v, Mapping):
                stats_by_id[str(k)] = v
                if "name" in v:
                    stats_by_name[str(v["name"]).strip().lower()] = v
    else:
        for s in stats:
            if isinstance(s, Mapping):
                pid = str(s.get("player_id") or s.get("id") or "")
                if pid:
                    stats_by_id[pid] = s
                name = str(s.get("name") or s.get("display_name") or "").strip().lower()
                if name:
                    stats_by_name[name] = s

    lines: list[str] = []
    tot_runs = 0
    tot_wkts = 0

    for i, p in enumerate(players, 1):
        pid = str(p.get("id") or p.get("player_id") or "")
        pname = str(p.get("name") or p.get("display_name") or "Player").strip()
        st = stats_by_id.get(pid) or stats_by_name.get(pname.lower()) or {}

        runs = int(st.get("runs") or 0)
        wkts = int(st.get("wickets") or 0)
        tot_runs += runs
        tot_wkts += wkts

        if runs > 0 and wkts > 0:
            w_str = "wkts" if wkts != 1 else "wkt"
            perf = f"{runs} runs, {wkts} {w_str}"
        elif wkts > 0:
            w_str = "wkts" if wkts != 1 else "wkt"
            perf = f"{wkts} {w_str}"
        else:
            perf = f"{runs} runs"

        lines.append(f"{i}. {esc(pname)} — {perf}")

    w_total_str = "wickets" if tot_wkts != 1 else "wicket"
    totals_line = f"Team totals: {tot_runs} runs, {tot_wkts} {w_total_str}"

    return (
        "📋 <b>Your XI — Tournament Performance</b>\n\n"
        + "\n".join(lines)
        + f"\n\n{totals_line}"
    )


