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


def needs(roster: Sequence[Mapping]) -> dict[str, int]:
    wk = sum(1 for p in roster if p["role"] == "WK")
    bowlers = sum(1 for p in roster if can_bowl(p))
    batters = sum(1 for p in roster if p["role"] in ("BAT", "WK", "AR"))
    return {"wk": max(0, 1 - wk), "bowl": max(0, 5 - bowlers), "bat": max(0, 5 - batters)}


def _score(p: Mapping, n: Mapping[str, int], rem: int) -> float:
    urgent = 1.0 + (1.5 if sum(n.values()) >= rem else 0.0)
    s = 0.0
    r = p["role"]
    if r == "WK":
        s += 3.0 * n["wk"] + 1.0 * n["bat"]
    elif r == "BOWL":
        s += 2.0 * n["bowl"]
    elif r == "AR":
        s += (1.4 if n["bowl"] else 0.3) + (1.0 if n["bat"] else 0.3) + 0.5
    elif r == "BAT":
        s += 1.5 * n["bat"]
    return s * urgent


def auto_pick(candidates: Sequence[Mapping], roster: Sequence[Mapping], rng: random.Random) -> int:
    """candidates: [{'pos':int,'player':{role,bowling_type,...}}]. Returns the chosen `pos` (balanced squad, ties random)."""
    n = needs(roster)
    rem = SQUAD - len(roster)
    best, best_s = [], -1.0
    for c in candidates:
        s = _score(c["player"], n, rem)
        if s > best_s + 1e-9:
            best, best_s = [c["pos"]], s
        elif abs(s - best_s) <= 1e-9:
            best.append(c["pos"])
    return rng.choice(best)
