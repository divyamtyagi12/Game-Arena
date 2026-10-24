"""Draft offer generation — pure logic."""
from __future__ import annotations

from collections import Counter
import random
import re
from typing import Collection

from game.categories import OFFER_SIZE
from models.catalog import CharacterCatalog
from models.character import Character

MAX_PER_ANIME = 2

ANIME_ALIASES: dict[str, str] = {
    "opm": "one punch man",
    "onepunchman": "one punch man",
    "one punch man": "one punch man",
    "hxh": "hunter x hunter",
    "hunterxhunter": "hunter x hunter",
    "hunter x hunter": "hunter x hunter",
    "aot": "attack on titan",
    "snk": "attack on titan",
    "attackontitan": "attack on titan",
    "shingekinokyojin": "attack on titan",
    "attack on titan": "attack on titan",
    "rezero": "re:zero",
    "re zero": "re:zero",
    "re:zero": "re:zero",
    "bc": "black clover",
    "blackclover": "black clover",
    "black clover": "black clover",
    "dn": "death note",
    "deathnote": "death note",
    "death note": "death note",
    "b10": "ben 10",
    "ben10": "ben 10",
    "ben 10": "ben 10",
    "jjk": "jujutsu kaisen",
    "jujutsukaisen": "jujutsu kaisen",
    "jujutsu kaisen": "jujutsu kaisen",
    "mha": "my hero academia",
    "myheroacademia": "my hero academia",
    "bnha": "my hero academia",
    "bokunoheroacademia": "my hero academia",
    "dbz": "dragon ball",
    "dragonball": "dragon ball",
    "dragonballz": "dragon ball",
    "naruto": "naruto",
    "narutoshippuden": "naruto",
    "bleach": "bleach",
    "onepiece": "one piece",
}


def normalize_anime(series: str | None) -> str:
    """Normalize anime title to prevent inconsistent names from bypassing the max-per-anime limit."""
    if not series:
        return ""
    cleaned = series.strip().lower().replace("×", "x").replace("✕", "x")
    if cleaned in ANIME_ALIASES:
        return ANIME_ALIASES[cleaned]
    compact = re.sub(r"[^a-z0-9]", "", cleaned)
    if compact in ANIME_ALIASES:
        return ANIME_ALIASES[compact]
    return compact or cleaned


def build_offers(
    catalog: CharacterCatalog,
    category: str,
    exclude: Collection[str],
    rng: random.Random,
    size: int = OFFER_SIZE,
    max_per_anime: int = MAX_PER_ANIME,
) -> list[Character]:
    """Pick up to `size` random eligible characters for `category`, strictly enforcing:
    1. No characters already in `exclude` (previously selected characters).
    2. All returned characters have unique IDs.
    3. At most `max_per_anime` (default 2) characters belong to the same anime series.
    4. Fair randomized selection across valid anime distributions.
    5. Safe fallback to the wider catalog when category pool is depleted, still respecting all rules.
    6. Safe graceful termination if the total pool cannot satisfy all constraints (never infinite loops).
    """
    chosen: list[Character] = []
    chosen_ids: set[str] = set(exclude)
    series_counts: Counter[str] = Counter()

    # 1. Primary candidate pool: characters eligible for this category, not in exclude
    primary_pool = [c for c in catalog.pool(category) if c.id not in chosen_ids]
    # Deduplicate by c.id while preserving unique entries
    unique_primary: list[Character] = []
    seen_primary_ids: set[str] = set()
    for c in primary_pool:
        if c.id not in seen_primary_ids:
            seen_primary_ids.add(c.id)
            unique_primary.append(c)

    # Fair random shuffle of primary pool
    rng.shuffle(unique_primary)

    for c in unique_primary:
        if c.id in chosen_ids:
            continue
        anime = normalize_anime(c.series)
        if series_counts[anime] < max_per_anime:
            chosen.append(c)
            chosen_ids.add(c.id)
            series_counts[anime] += 1
            if len(chosen) >= size:
                break

    # 2. Fallback: if category pool did not yield `size` characters without violating rules,
    # supplement from remaining catalog characters, prioritized by category rating, still respecting rules.
    if len(chosen) < size:
        extras = sorted(
            (c for c in catalog.all() if c.id not in chosen_ids),
            key=lambda c: (-c.rating(category), c.id),
        )
        for c in extras:
            if c.id in chosen_ids:
                continue
            anime = normalize_anime(c.series)
            if series_counts[anime] < max_per_anime:
                chosen.append(c)
                chosen_ids.add(c.id)
                series_counts[anime] += 1
                if len(chosen) >= size:
                    break

    # 3. Final shuffle of the selected options so their positions are randomized
    rng.shuffle(chosen)
    return chosen
