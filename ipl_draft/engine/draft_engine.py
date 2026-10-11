"""Draft helpers: pool capacity, role needs, automatic pick heuristic for humans. Pure.

Auto-picks use ONLY public information (roles / bowling type of the 11 offered cards and the roster so far) — never the
hidden ratings — so an auto-drafted human team gets no hidden help.
"""
from __future__ import annotations

import random
from typing import Mapping, Sequence

SQUAD = 11
SYSTEM_TEAMS = 4


def required_pool(humans: int, margin: int = 20) -> int:
    """Mirror of SQL ipl_required_pool (v3). Cricketers may sit in several squads, so a team only needs 10 owned players
    plus 11 fresh offer cards = 21 distinct cricketers in the pool, whatever the number of teams."""
    return 21 + margin


def can_bowl(p: Mapping) -> bool:
    return p["role"] == "BOWL" or (p["role"] == "AR" and p.get("bowling_type", "NONE") != "NONE")


def still_required_text(roster: Sequence[Mapping]) -> str:
    """Format required roles remaining: at least 3 bowlers, 1 keeper, 1 all-rounder."""
    b_count = sum(1 for p in roster if p.get("role") == "BOWL")
    wk_count = sum(1 for p in roster if p.get("role") == "WK")
    ar_count = sum(1 for p in roster if p.get("role") == "AR")
    
    b_need = max(0, 3 - b_count)
    wk_need = max(0, 1 - wk_count)
    ar_need = max(0, 1 - ar_count)
    
    parts = []
    if b_need > 0:
        parts.append(f"{b_need} bowler(s)")
    if wk_need > 0:
        parts.append(f"{wk_need} keeper")
    if ar_need > 0:
        parts.append(f"{ar_need} all-rounder")
    
    if not parts:
        return "None (all minimums met)"
    return ", ".join(parts)


def is_role_pick_allowed(roster: Sequence[Mapping], role: str) -> bool:
    """Determine whether picking a player of `role` leaves enough remaining picks to fulfill
    the mandatory requirement of 3 bowlers, 1 keeper, and 1 all-rounder."""
    b_count = sum(1 for p in roster if p.get("role") == "BOWL")
    wk_count = sum(1 for p in roster if p.get("role") == "WK")
    ar_count = sum(1 for p in roster if p.get("role") == "AR")
    
    b_need = max(0, 3 - b_count)
    wk_need = max(0, 1 - wk_count)
    ar_need = max(0, 1 - ar_count)
    total_needed = b_need + wk_need + ar_need
    
    if total_needed == 0:
        return True
    
    picks_left = SQUAD - len(roster)
    if picks_left <= 0:
        return False
    
    new_b_need = max(0, b_need - (1 if role == "BOWL" else 0))
    new_wk_need = max(0, wk_need - (1 if role == "WK" else 0))
    new_ar_need = max(0, ar_need - (1 if role == "AR" else 0))
    new_total_needed = new_b_need + new_wk_need + new_ar_need
    
    picks_left_after = picks_left - 1
    if picks_left_after >= new_total_needed:
        return True
    
    # If picks left are tight or behind, must pick one of the required roles
    if role == "BOWL" and b_need > 0:
        return True
    if role == "WK" and wk_need > 0:
        return True
    if role == "AR" and ar_need > 0:
        return True
    
    return False


def needs(roster: Sequence[Mapping]) -> dict[str, int]:
    wk = sum(1 for p in roster if p["role"] == "WK")
    bowlers = sum(1 for p in roster if p["role"] == "BOWL")
    all_rounders = sum(1 for p in roster if p["role"] == "AR")
    batters = sum(1 for p in roster if p["role"] in ("BAT", "WK", "AR"))
    return {
        "wk": max(0, 1 - wk),
        "bowl": max(0, 3 - bowlers),
        "ar": max(0, 1 - all_rounders),
        "bat": max(0, 4 - batters),
    }


def _score(p: Mapping, n: Mapping[str, int], rem: int) -> float:
    urgent = 1.0 + (3.0 if sum(n.values()) >= rem else 0.0)
    s = 1.0
    r = p["role"]
    if r == "WK":
        s += 5.0 * n["wk"] + 1.0 * n.get("bat", 0)
    elif r == "BOWL":
        s += 4.0 * n["bowl"]
    elif r == "AR":
        s += 4.0 * n.get("ar", 0) + 1.5 * n.get("bowl", 0) + 1.0
    elif r == "BAT":
        s += 2.0 * n.get("bat", 0)
    return s * urgent


def auto_pick(candidates: Sequence[Mapping], roster: Sequence[Mapping], rng: random.Random) -> int:
    """candidates: [{'pos':int,'player':{role,bowling_type,...}}]. Returns the chosen `pos` (balanced squad, ties random)."""
    allowed_cands = [c for c in candidates if is_role_pick_allowed(roster, c["player"]["role"])]
    pool = allowed_cands if allowed_cands else candidates
    n = needs(roster)
    rem = SQUAD - len(roster)
    best, best_s = [], -1.0
    for c in pool:
        s = _score(c["player"], n, rem)
        if s > best_s + 1e-9:
            best, best_s = [c["pos"]], s
        elif abs(s - best_s) <= 1e-9:
            best.append(c["pos"])
    return rng.choice(best)

