"""Build IPL 2026 player ratings — ONLY players who actually played IPL 2026.

Sources: WISDEN_SQUADS + REPLACEMENTS_IN from ipl_draft/data/ipl_2026_sources.py
Ratings: editorial-tier fallback (rate_from_tier) matched against SEED_PLAYERS for role/tier.
Players not found in SEED_PLAYERS get a default tier based on name heuristics.
"""
from __future__ import annotations

import csv
import hashlib
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ipl_draft.data.ipl_2026_sources import WISDEN_SQUADS, REPLACEMENTS_IN
from ipl_draft.data.seed_players import SEED_PLAYERS
from ipl_draft.data.names import slugify, disambiguate, normalize_name
from ipl_draft.engine.model import ATTRS

# ── Rating helpers (same formulas as ratings.py but salt-aware) ───────────────
TIER_BASE = {"S": 80, "A": 69, "B": 58, "C": 46}


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _i(x: float) -> int:
    return int(round(_clip(x, 0, 100)))


def _jit(slug: str, key: str, salt: str) -> float:
    h = hashlib.sha256(f"{salt}:{slug}:{key}".encode()).digest()
    return int.from_bytes(h[:4], "big") / 0xFFFFFFFF * 2 - 1


def rate(role: str, tier: str, bowling_type: str, slug: str, salt: str) -> dict[str, int]:
    base = TIER_BASE.get(tier, TIER_BASE["C"])
    j = lambda k, amp: _jit(slug, k, salt) * amp
    bowls = bowling_type != "NONE"
    if role == "BAT":
        bat, bowl = base + j("b", 5), (22 + (base - 46) * 0.2 if bowls else 12)
    elif role == "WK":
        bat, bowl = base - 3 + j("b", 5), 10
    elif role == "BOWL":
        bat, bowl = 14 + (base - 46) * 0.12 + j("b", 4), base + j("w", 5)
    else:
        lean = j("lean", 9)
        bat, bowl = base - 6 + lean, base - 6 - lean
    power     = bat + j("p", 10) + (4 if role == "AR" else 0) - (8 if role == "BOWL" else 0)
    rot       = 50 + (bat - 50) * 0.5 + j("r", 10)
    cons      = bat * 0.8 + 10 + j("c", 8)
    bowl_cons = bowl * 0.8 + 10 + j("bc", 8)
    kind = bowling_type
    spin = bowl * (1.0 if kind == "SPIN" else 0.3 if kind == "PACE" else 0.6)
    pace = bowl * (1.0 if kind == "PACE" else 0.3 if kind == "SPIN" else 0.6)
    fielding = 52 + (base - 46) * 0.25 + j("f", 12)
    keep = (58 + (base - 46) * 0.5 + j("k", 6)) if role == "WK" else (12 if role != "AR" else 14)
    b_i = _i(bat); w_i = _i(bowl); f_i = _i(fielding); k_i = _i(keep)
    if role == "BAT":
        o = 0.85 * b_i + 0.10 * f_i + 0.05 * w_i
    elif role == "WK":
        o = 0.78 * b_i + 0.12 * k_i + 0.10 * f_i
    elif role == "BOWL":
        o = 0.85 * w_i + 0.10 * f_i + 0.05 * b_i
    else:
        o = 0.62 * max(b_i, w_i) + 0.38 * min(b_i, w_i)
    return {
        "batting_rating":       b_i,
        "bowling_rating":       w_i,
        "fielding_rating":      f_i,
        "wicketkeeping_rating": k_i,
        "batting_consistency":  _i(cons),
        "bowling_consistency":  _i(bowl_cons),
        "power_hitting":        _i(power),
        "strike_rotation":      _i(rot),
        "death_overs_batting":  _i(bat + j("d", 8) + (_i(power) - bat) * 0.3),
        "death_overs_bowling":  _i(bowl + j("db", 8)),
        "spin_effectiveness":   _i(spin),
        "pace_effectiveness":   _i(pace),
        "overall_rating":       _i(o),
    }


# ── Build lookup from seed data: slug -> (role, nat, bat, bowl, tier) ─────────
SEED_LOOKUP: dict[str, tuple] = {}
for _name, _role, _nat, _bat, _bowl, _tier in SEED_PLAYERS:
    SEED_LOOKUP[slugify(_name)] = (_role, _nat, _bat, _bowl, _tier)


def _guess_entry(name: str) -> tuple[str, str, str, str, str]:
    """Best-effort defaults for players not in the seed list."""
    # Default: Indian AR, tier C (conservative)
    return ("AR", "Unknown", "R", "-", "C")


# ── Collect all unique 2026 players with their team(s) ────────────────────────
player_teams: dict[str, list[str]] = {}   # canonical name -> [team codes]

for team, players in WISDEN_SQUADS.items():
    for p in players:
        player_teams.setdefault(p, []).append(team)

for team, players in REPLACEMENTS_IN.items():
    for p in players:
        entry = player_teams.setdefault(p, [])
        label = f"{team}*"          # * marks replacement
        if label not in entry:
            entry.append(label)

print(f"Unique IPL 2026 players: {len(player_teams)}")


# ── Rate every player ─────────────────────────────────────────────────────────
SALT = f"ipl2026-reshuffle-{int(time.time() * 1000)}"

# Build display names for disambiguation
all_name_slugs = [(slugify(n), n) for n in player_teams]
display_map = disambiguate(all_name_slugs)

rows = []
not_in_seed = []

for name, teams in player_teams.items():
    slug = slugify(name)
    if slug in SEED_LOOKUP:
        role, nat, bat, bowl, tier = SEED_LOOKUP[slug]
    else:
        role, nat, bat, bowl, tier = _guess_entry(name)
        not_in_seed.append(name)

    btype = {"P": "PACE", "S": "SPIN", "-": "NONE"}[bowl]
    ratings = rate(role, tier, btype, slug, SALT)
    rows.append({
        "name":          display_map.get(slug, name),
        "slug":          slug,
        "teams":         "/".join(teams),
        "role":          role,
        "nationality":   nat,
        "batting_style": {"R": "RHB", "L": "LHB"}[bat],
        "bowling_type":  btype,
        "tier":          tier,
        **ratings,
    })

# Sort by team then overall desc
rows.sort(key=lambda r: (r["teams"].split("/")[0], -int(r["overall_rating"])))

print(f"Players not in seed (using default tier C): {len(not_in_seed)}")
for n in sorted(not_in_seed):
    print(f"  - {n}")


# ── Write CSV ─────────────────────────────────────────────────────────────────
COLS = ["name", "slug", "teams", "role", "nationality", "batting_style", "bowling_type", "tier", *ATTRS]
OUT_CSV = os.path.join(ROOT, "data", "ipl_2026_player_ratings.csv")
os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=COLS)
    w.writeheader()
    w.writerows(rows)

print(f"\n[OK] Salt       : {SALT}")
print(f"[OK] Total rows : {len(rows)}")
print(f"[OK] Output CSV : {OUT_CSV}")

