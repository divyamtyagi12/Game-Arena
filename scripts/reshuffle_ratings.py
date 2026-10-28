"""Reshuffle IPL draft player ratings using a fresh random salt and export to CSV.

Usage:
    python scripts/reshuffle_ratings.py [--salt SALT] [--out OUTPUT_CSV]

The salt changes the deterministic per-player jitter inside rate_from_tier(),
which produces a fresh (but still reproducible) set of ratings.
If no salt is given, a random one is generated and printed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sys
import time

# ── Make sure the project root is on PYTHONPATH ──────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ipl_draft.data.seed_players import SEED_PLAYERS
from ipl_draft.data.names import disambiguate, normalize_name, slugify
from ipl_draft.engine.model import ATTRS

# ── Inline re-implementation of rate_from_tier with a custom salt ─────────────
TIER_BASE = {"S": 80, "A": 69, "B": 58, "C": 46}


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _i(x: float) -> int:
    return int(round(_clip(x, 0, 100)))


def _jit(slug: str, key: str, salt: str) -> float:
    """Deterministic value in [-1, 1] from (slug, key, salt)."""
    h = hashlib.sha256(f"{salt}:{slug}:{key}".encode()).digest()
    return int.from_bytes(h[:4], "big") / 0xFFFFFFFF * 2 - 1


def rate_from_tier_salted(role: str, tier: str, bowling_type: str, slug: str, salt: str) -> dict[str, int]:
    base = TIER_BASE.get(tier, TIER_BASE["C"])
    j = lambda k, amp: _jit(slug, k, salt) * amp  # noqa: E731
    bowls = bowling_type != "NONE"

    if role == "BAT":
        bat, bowl = base + j("b", 5), (22 + (base - 46) * 0.2 if bowls else 12)
    elif role == "WK":
        bat, bowl = base - 3 + j("b", 5), 10
    elif role == "BOWL":
        bat, bowl = 14 + (base - 46) * 0.12 + j("b", 4), base + j("w", 5)
    else:  # AR
        lean = j("lean", 9)
        bat, bowl = base - 6 + lean, base - 6 - lean

    power    = bat + j("p", 10) + (4 if role == "AR" else 0) - (8 if role == "BOWL" else 0)
    rot      = 50 + (bat - 50) * 0.5 + j("r", 10)
    cons     = bat * 0.8 + 10 + j("c", 8)
    bowl_cons = bowl * 0.8 + 10 + j("bc", 8)

    kind = bowling_type
    spin = bowl * (1.0 if kind == "SPIN" else 0.3 if kind == "PACE" else 0.6)
    pace = bowl * (1.0 if kind == "PACE" else 0.3 if kind == "SPIN" else 0.6)

    fielding = 52 + (base - 46) * 0.25 + j("f", 12)
    keep = (58 + (base - 46) * 0.5 + j("k", 6)) if role == "WK" else (12 if role != "AR" else 14)

    # overall
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


# ── CSV columns ───────────────────────────────────────────────────────────────
COLUMNS = [
    "name", "slug", "role", "nationality", "batting_style", "bowling_type", "tier",
    *ATTRS,
]


def generate(salt: str) -> list[dict]:
    rows_for_disambig = [(slugify(n), n) for n, *_ in SEED_PLAYERS]
    display = disambiguate(rows_for_disambig)
    out = []
    for name, role, nat, bat, bowl, tier in SEED_PLAYERS:
        slug = slugify(name)
        btype = {"P": "PACE", "S": "SPIN", "-": "NONE"}[bowl]
        ratings = rate_from_tier_salted(role, tier, btype, slug, salt)
        out.append({
            "name":          display[slug],
            "slug":          slug,
            "role":          role,
            "nationality":   nat,
            "batting_style": {"R": "RHB", "L": "LHB"}[bat],
            "bowling_type":  btype,
            "tier":          tier,
            **ratings,
        })
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Reshuffle IPL draft player ratings")
    parser.add_argument("--salt", default=None,
                        help="Deterministic salt string (default: auto-generated from timestamp)")
    parser.add_argument("--out", default=None,
                        help="Output CSV path (default: data/ipl_player_ratings.csv in project root)")
    args = parser.parse_args()

    salt = args.salt or f"reshuffle-{int(time.time() * 1000)}"
    out_path = args.out or os.path.join(ROOT, "data", "ipl_player_ratings.csv")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    rows = generate(salt)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[OK] Salt used   : {salt}")
    print(f"[OK] Players     : {len(rows)}")
    print(f"[OK] Output file : {out_path}")


if __name__ == "__main__":
    main()
