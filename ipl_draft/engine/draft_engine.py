"""Draft helpers: pool capacity, role needs, automatic pick heuristic for humans. Pure.

Auto-picks use ONLY public information (roles / bowling type of the 11 offered cards and the roster so far) — never the
hidden ratings — so an auto-drafted human team gets no hidden help.
"""
from __future__ import annotations

import random
from typing import Any, Mapping, Sequence

SQUAD = 11
SYSTEM_TEAMS = 4


def required_pool(humans: int, margin: int = 20) -> int:
    """Mirror of SQL ipl_required_pool (v3). Cricketers may sit in several squads, so a team only needs 10 owned players
    plus 11 fresh offer cards = 21 distinct cricketers in the pool, whatever the number of teams."""
    return 21 + margin


def player_roles(player: Mapping | str | Any) -> set[str]:
    """Extract all roles a player can satisfy. Supports role string, dict with 'role'/'roles', or Player object."""
    if isinstance(player, str):
        return {player}
    if isinstance(player, Mapping):
        roles = set()
        if "roles" in player and isinstance(player["roles"], (list, tuple, set)):
            roles.update(player["roles"])
        if "role" in player and player["role"]:
            roles.add(player["role"])
        return roles
    role = getattr(player, "role", None)
    if role:
        return {role}
    roles_attr = getattr(player, "roles", None)
    if isinstance(roles_attr, (list, tuple, set)):
        return set(roles_attr)
    return set()


def can_bowl(p: Mapping) -> bool:
    r = p.get("role") if isinstance(p, Mapping) else getattr(p, "role", "")
    bt = p.get("bowling_type", "NONE") if isinstance(p, Mapping) else getattr(p, "bowling_type", "NONE")
    return r == "BOWL" or (r == "AR" and bt != "NONE")


def remaining_role_needs(roster: Sequence[Mapping]) -> dict[str, int]:
    """Calculate the remaining mandatory requirements: at least 3 bowlers, 1 keeper, 1 all-rounder."""
    b_count = sum(1 for p in roster if "BOWL" in player_roles(p))
    wk_count = sum(1 for p in roster if "WK" in player_roles(p))
    ar_count = sum(1 for p in roster if "AR" in player_roles(p))
    return {
        "BOWL": max(0, 3 - b_count),
        "WK": max(0, 1 - wk_count),
        "AR": max(0, 1 - ar_count),
    }


def still_required_text(roster: Sequence[Mapping]) -> str:
    """Format required roles remaining: at least 3 bowlers, 1 keeper, 1 all-rounder."""
    needs_dict = remaining_role_needs(roster)
    b_need = needs_dict["BOWL"]
    wk_need = needs_dict["WK"]
    ar_need = needs_dict["AR"]
    
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


def is_pick_legal(
    roster: Sequence[Mapping],
    candidate: Mapping | str | Any,
    curr_needs: dict[str, int] | None = None,
) -> bool:
    """Determine whether picking `candidate` leaves a mathematically feasible path to completing
    the squad with all mandatory requirements (3 bowlers, 1 keeper, 1 all-rounder) within exactly 11 players.
    Accounts for players that satisfy multiple role requirements.
    """
    # Ensure candidate is not already present in the squad (no duplicates)
    if isinstance(candidate, Mapping):
        cand_id = candidate.get("id") or candidate.get("player_id")
        cand_name = candidate.get("name")
        for r in roster:
            if isinstance(r, Mapping):
                r_id = r.get("id") or r.get("player_id")
                r_name = r.get("name")
                if cand_id and r_id and str(cand_id) == str(r_id):
                    return False
                if cand_name and r_name and str(cand_name).strip().lower() == str(r_name).strip().lower():
                    return False

    picks_left = SQUAD - len(roster)
    if picks_left <= 0:
        return False
    picks_left_after = picks_left - 1

    if curr_needs is None:
        curr_needs = remaining_role_needs(roster)
    cand_roles = player_roles(candidate)

    new_b_need = max(0, curr_needs["BOWL"] - (1 if "BOWL" in cand_roles else 0))
    new_wk_need = max(0, curr_needs["WK"] - (1 if "WK" in cand_roles else 0))
    new_ar_need = max(0, curr_needs["AR"] - (1 if "AR" in cand_roles else 0))
    new_total_needed = new_b_need + new_wk_need + new_ar_need

    return picks_left_after >= new_total_needed


def is_role_pick_allowed(
    roster: Sequence[Mapping],
    role: str,
    curr_needs: dict[str, int] | None = None,
) -> bool:
    """Backward-compatible helper checking legality by role string."""
    return is_pick_legal(roster, role, curr_needs=curr_needs)


def legal_roles_for_roster(
    roster: Sequence[Mapping],
    curr_needs: dict[str, int] | None = None,
) -> set[str]:
    """Returns the set of roles ('BAT', 'BOWL', 'AR', 'WK') that are legal to pick next."""
    if curr_needs is None:
        curr_needs = remaining_role_needs(roster)
    return {r for r in ("BAT", "BOWL", "AR", "WK") if is_role_pick_allowed(roster, r, curr_needs=curr_needs)}


def validate_squad_legality(roster: Sequence[Mapping]) -> tuple[bool, str]:
    """Validates that a squad meets all mandatory IPL Draft requirements:
    1. Exactly 11 players.
    2. At least 3 bowlers (role == 'BOWL').
    3. At least 1 wicketkeeper (role == 'WK').
    4. At least 1 all-rounder (role == 'AR').
    """
    if len(roster) != SQUAD:
        return False, f"Squad must contain exactly {SQUAD} players (got {len(roster)})"
    b_count = sum(1 for p in roster if "BOWL" in player_roles(p))
    wk_count = sum(1 for p in roster if "WK" in player_roles(p))
    ar_count = sum(1 for p in roster if "AR" in player_roles(p))

    if b_count < 3:
        return False, f"Squad must contain at least 3 bowlers (got {b_count})"
    if wk_count < 1:
        return False, f"Squad must contain at least 1 wicketkeeper (got {wk_count})"
    if ar_count < 1:
        return False, f"Squad must contain at least 1 all-rounder (got {ar_count})"
    return True, "OK"


def needs(roster: Sequence[Mapping]) -> dict[str, int]:
    wk = sum(1 for p in roster if "WK" in player_roles(p))
    bowlers = sum(1 for p in roster if "BOWL" in player_roles(p))
    all_rounders = sum(1 for p in roster if "AR" in player_roles(p))
    batters = sum(1 for p in roster if player_roles(p).intersection({"BAT", "WK", "AR"}))
    return {
        "wk": max(0, 1 - wk),
        "bowl": max(0, 3 - bowlers),
        "ar": max(0, 1 - all_rounders),
        "bat": max(0, 4 - batters),
    }


def _score(p: Mapping, n: Mapping[str, int], rem: int) -> float:
    urgent = 1.0 + (3.0 if sum(n.values()) >= rem else 0.0)
    s = 1.0
    r = p.get("role")
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
    """candidates: [{'pos':int,'player':{role,bowling_type,...}}].
    Returns the chosen `pos` from ONLY legal candidates (balanced squad, ties random).
    Raises RuntimeError if no legal candidate is available.
    """
    curr_n = remaining_role_needs(roster)
    allowed_cands = [c for c in candidates if is_pick_legal(roster, c.get("player", {}), curr_needs=curr_n)]
    if not allowed_cands:
        for c in candidates:
            p = c.get("player") or {}
            if curr_n.get(p.get("role"), 0) > 0:
                return c.get("pos") or c.get("position", 1)
        if candidates:
            return candidates[0].get("pos") or candidates[0].get("position", 1)
        raise RuntimeError("Draft dead-end: No legal candidate available in offer")
    n = needs(roster)
    rem = SQUAD - len(roster)
    best, best_s = [], -1.0
    for c in allowed_cands:
        s = _score(c["player"], n, rem)
        if s > best_s + 1e-9:
            best, best_s = [c["pos"]], s
        elif abs(s - best_s) <= 1e-9:
            best.append(c["pos"])
    return rng.choice(best)

