"""System-franchise drafting AI. Pure.

Fairness rule: franchises draft through the SAME mechanism as humans — each pick they are offered 11 random available
cricketers (never the full pool) and choose one. The only differences are (a) they see hidden ratings, exactly like the
match engine does, and (b) a role-repair safety net: when a hard need (a keeper, enough bowlers) could otherwise not be met
in the remaining rounds, the offer is guaranteed to contain a card of that role. The repair never adds *strength* —
it only guarantees a playable shape, and humans may still pick 11 batters if they want.
"""
from __future__ import annotations

import random
from typing import Mapping, Sequence

from .draft_engine import SQUAD, can_bowl, needs

FRANCHISES = [
    ("MI",  "Mumbai Indians",             "🔵"),
    ("CSK", "Chennai Super Kings",        "🟡"),
    ("RCB", "Royal Challengers Bengaluru", "🔴"),
    ("KKR", "Kolkata Knight Riders",      "🟣"),
]
FRANCHISE_EMOJI = {c: e for c, _, e in FRANCHISES}


def franchise_payload() -> list[dict]:
    return [{"code": c, "name": n} for c, n, _ in FRANCHISES]


def force_roles(roster: Sequence[Mapping]) -> list[str] | None:
    """Roles the next offer must contain when the remaining picks are only just enough to meet hard needs."""
    n = needs(roster)
    rem = SQUAD - len(roster)
    roles: list[str] = []
    if n["wk"] and rem <= n["wk"] + n["bowl"] + 1:
        roles.append("WK")
    if n["bowl"] and rem <= n["bowl"] + n["wk"] + 1:
        roles += ["BOWL", "AR"]
    return roles or None


def choose(cards: Sequence[Mapping], roster: Sequence[Mapping], rng: random.Random) -> int:
    """cards: [{'pos','role','bowling_type','ratings':{...}}]. Value = overall + shape bonus (+ small noise so the
    eight franchises differ). Returns the chosen `pos`."""
    n = needs(roster)
    rem = SQUAD - len(roster)
    pressure = 1.0 + (1.0 if sum(n.values()) >= rem else 0.0)
    best_pos, best = cards[0]["pos"], -1e9
    for c in cards:
        r = c["ratings"]
        v = float(r["overall_rating"])
        role = c["role"]
        shape = 0.0
        if role == "WK":
            shape += 14 * n["wk"] + 4 * n["bat"]
        elif role == "BOWL":
            shape += 9 * n["bowl"]
        elif role == "AR":
            shape += (6 if n["bowl"] else 1) + (4 if n["bat"] else 1)
        elif role == "BAT":
            shape += 6 * n["bat"]
        shape += 0.06 * (r["fielding_rating"] - 50)
        v += shape * pressure + rng.gauss(0, 3.0)
        if v > best:
            best, best_pos = v, c["pos"]
    return best_pos


def snake_order(team_ids: Sequence[int], round_no: int) -> list[int]:
    return list(team_ids) if round_no % 2 == 1 else list(reversed(team_ids))
