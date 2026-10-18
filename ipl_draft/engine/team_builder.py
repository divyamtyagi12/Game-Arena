"""System-franchise drafting AI. Pure.

Fairness rule: franchises draft through the SAME mechanism as humans — each pick they are offered 11 random available
cricketers (never the full pool) and choose one. The only differences are (a) they see hidden ratings, exactly like the
match engine does, and (b) a role-repair safety net: when a hard need (a keeper, enough bowlers) could otherwise not be met
in the remaining rounds, the offer is guaranteed to contain a card of that role. The repair never adds *strength* —
it only guarantees a playable shape, and humans may still pick 11 batters if they want.
"""
from __future__ import annotations

import random
from typing import Any, Mapping, Sequence

from .draft_engine import SQUAD, can_bowl, needs

FRANCHISES = [
    ("MI",  "Mumbai Indians",             "🔵"),
    ("CSK", "Chennai Super Kings",        "🟡"),
    ("RCB", "Royal Challengers Bengaluru", "🔴"),
    ("KKR", "Kolkata Knight Riders",      "🟣"),
]
FRANCHISE_EMOJI = {c: e for c, _, e in FRANCHISES}

# Official predefined 2026 IPL Playing XI squads for the 4 computer-controlled franchises
SYSTEM_PLAYING_XI: dict[str, list[str]] = {
    "MI": [
        "Rohit Sharma", "Ryan Rickelton", "Suryakumar Yadav", "Tilak Varma",
        "Hardik Pandya", "Will Jacks", "Shardul Thakur", "Trent Boult",
        "Deepak Chahar", "Jasprit Bumrah", "Mayank Markande",
    ],
    "CSK": [
        "Ruturaj Gaikwad", "Ayush Mhatre", "Sanju Samson", "Shivam Dube",
        "Dewald Brevis", "MS Dhoni", "Shreyas Gopal", "Akeal Hosein",
        "Noor Ahmad", "Khaleel Ahmed", "Rahul Chahar",
    ],
    "RCB": [
        "Virat Kohli", "Phil Salt", "Rajat Patidar", "Devdutt Padikkal",
        "Krunal Pandya", "Tim David", "Romario Shepherd", "Bhuvneshwar Kumar",
        "Suyash Sharma", "Josh Hazlewood", "Yash Dayal",
    ],
    "KKR": [
        "Sunil Narine", "Ajinkya Rahane", "Angkrish Raghuvanshi", "Rahul Tripathi",
        "Rinku Singh", "Finn Allen", "Ramandeep Singh", "Harshit Rana",
        "Matheesha Pathirana", "Varun Chakravarthy", "Umran Malik",
    ],
}

OFFICIAL_IPL_2026_SQUADS: dict[str, set[str]] = {
    "RCB": {
        "Virat Kohli", "Rajat Patidar", "Phil Salt", "Devdutt Padikkal", "Krunal Pandya",
        "Tim David", "Romario Shepherd", "Bhuvneshwar Kumar", "Suyash Sharma", "Josh Hazlewood",
        "Yash Dayal", "Jitesh Sharma", "Swapnil Singh", "Jacob Bethell", "Nuwan Thushara",
        "Rasikh Dar", "Jordan Cox", "Abhinandan Singh", "Kanishk Chouhan", "Manoj Bhandage",
        "Swastik Chikara", "Mohit Rathee",
    },
    "KKR": {
        "Sunil Narine", "Ajinkya Rahane", "Angkrish Raghuvanshi", "Rahul Tripathi", "Rinku Singh",
        "Finn Allen", "Ramandeep Singh", "Harshit Rana", "Matheesha Pathirana", "Varun Chakravarthy",
        "Umran Malik", "Cameron Green", "Vaibhav Arora", "Anukul Roy", "Rovman Powell",
        "Manish Pandey", "Kartik Tyagi", "Tejasvi Dahiya", "Luvnith Sisodia", "Blessing Muzarabani",
        "Navdeep Saini", "Venkatesh Iyer",
    },
    "MI": {
        "Rohit Sharma", "Ryan Rickelton", "Suryakumar Yadav", "Tilak Varma", "Hardik Pandya",
        "Will Jacks", "Shardul Thakur", "Trent Boult", "Deepak Chahar", "Jasprit Bumrah",
        "Mayank Markande", "Quinton de Kock", "Mitchell Santner", "Naman Dhir", "Robin Minz",
        "Corbin Bosch", "Raj Angad Bawa", "Ashwani Kumar", "Allah Ghazanfar", "Sherfane Rutherford",
        "Danish Malewar", "Avesh Khan", "Arjun Tendulkar",
    },
    "CSK": {
        "Ruturaj Gaikwad", "Ayush Mhatre", "Sanju Samson", "Shivam Dube", "Dewald Brevis",
        "MS Dhoni", "Shreyas Gopal", "Akeal Hosein", "Noor Ahmad", "Khaleel Ahmed",
        "Rahul Chahar", "Urvil Patel", "Kartik Sharma", "Matthew Short", "Sarfaraz Khan",
        "Anshul Kamboj", "Jamie Overton", "Mukesh Choudhary", "Matt Henry", "Nathan Ellis",
        "Spencer Johnson", "Gurjapneet Singh", "Prashant Veer", "Zak Foulkes", "Akash Madhwal",
    },
}

OVERSEAS_PLAYERS_2026: set[str] = {
    "Phil Salt", "Tim David", "Romario Shepherd", "Josh Hazlewood", "Jacob Bethell", "Nuwan Thushara", "Jordan Cox",
    "Sunil Narine", "Finn Allen", "Matheesha Pathirana", "Cameron Green", "Rovman Powell", "Blessing Muzarabani",
    "Ryan Rickelton", "Will Jacks", "Trent Boult", "Quinton de Kock", "Mitchell Santner", "Corbin Bosch", "Allah Ghazanfar", "Sherfane Rutherford",
    "Dewald Brevis", "Akeal Hosein", "Noor Ahmad", "Matthew Short", "Jamie Overton", "Matt Henry", "Nathan Ellis", "Spencer Johnson", "Zak Foulkes",
}


def validate_system_team_squad(team_id: int, playing_xi: Sequence[Any], franchise_code: str) -> tuple[bool, str]:
    """Validates that a computer-controlled system franchise Playing XI strictly conforms to IPL rules:
    1. Exactly 11 players.
    2. Every player is an officially verified member of that franchise's IPL 2026 squad.
    3. No cross-team player assignments (e.g. Rishabh Pant or Venkatesh Iyer on RCB).
    4. No duplicates in the Playing XI.
    5. Maximum 4 overseas players.
    6. Contains at least one wicketkeeper (role == 'WK').
    """
    if franchise_code not in OFFICIAL_IPL_2026_SQUADS:
        return False, f"Unknown system franchise code: {franchise_code}"

    if len(playing_xi) != 11:
        return False, f"System team {franchise_code} must have exactly 11 players (got {len(playing_xi)})"

    official_squad = OFFICIAL_IPL_2026_SQUADS[franchise_code]

    names: list[str] = []
    wk_count = 0
    overseas_count = 0

    for p in playing_xi:
        if hasattr(p, "name"):
            name = p.name
            role = getattr(p, "role", "")
            nationality = getattr(p, "nationality", None)
        elif isinstance(p, dict):
            name = p.get("name") or p.get("display_name") or ""
            role = p.get("role", "")
            nationality = p.get("nationality")
        else:
            name = str(p)
            role = ""
            nationality = None

        names.append(name)

        if name not in official_squad:
            return False, f"Player '{name}' does not belong to {franchise_code} verified IPL 2026 squad"

        if (nationality and nationality not in ("India", "Indian")) or name in OVERSEAS_PLAYERS_2026:
            overseas_count += 1

        if role == "WK" or name in ("Phil Salt", "Finn Allen", "Ryan Rickelton", "MS Dhoni", "Sanju Samson"):
            wk_count += 1

    if len(set(names)) != 11:
        return False, f"Duplicate players detected in {franchise_code} Playing XI: {names}"

    if overseas_count > 4:
        return False, f"Maximum 4 overseas players allowed in Playing XI, found {overseas_count} in {franchise_code}"

    if wk_count < 1:
        return False, f"Playing XI for {franchise_code} must contain at least 1 wicketkeeper"

    return True, "OK"


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
