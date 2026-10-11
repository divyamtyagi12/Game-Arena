"""Compare the IPL player pool against the collected IPL 2026 squad evidence and print a coverage report.

    python -m scripts.verify_ipl_2026            # report only; never writes to the database, never marks anything verified

`ipl_verified` stays false everywhere: it can only be set by the Cricsheet importer from ball-by-ball records, and
the evidence here is secondary (see ipl_draft/data/ipl_2026_sources.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ipl_draft.data import ipl_2026_sources as src                    # noqa: E402
from ipl_draft.data.names import normalize_name                      # noqa: E402
from ipl_draft.data.seed_players import SEED_PLAYERS                 # noqa: E402

# spelling variants seen across sources -> canonical seed spelling
ALIASES = {"vaibhav sooryavanshi": "vaibhav suryavanshi", "shahbaz ahmad": "shahbaz ahmed", "prithvi raj": "prithvi raj yarra",
           "rasikh salam": "rasikh dar", "sakib hussain": "sakib hassan", "vijaykumar vyshak": "vyshak vijaykumar",
           "abishek porel": "abhishek porel", "mamimaran siddharth": "manimaran siddharth", "aman hakim khan": "aman khan"}


def key(name: str) -> str:
    k = normalize_name(name)
    return ALIASES.get(k, k)


def collect() -> dict[str, set[str]]:
    teams: dict[str, set[str]] = {}
    for t, names in src.WISDEN_SQUADS.items():
        teams.setdefault(t, set()).update(names)
    for d in (src.REPLACEMENTS_IN, src.EXTRA_ON_WIKI):
        for t, names in d.items():
            teams.setdefault(t, set()).update(names)
    return teams


def main() -> int:
    teams = collect()
    seed = {key(n): n for n, *_ in SEED_PLAYERS}
    all26 = {key(n): n for names in teams.values() for n in names}
    missing = {k: n for k, n in all26.items() if k not in seed}
    extra = [n for k, n in seed.items() if k not in all26]
    print(f"IPL 2026 evidence: {len(all26)} distinct players across {len(teams)} teams (SECONDARY sources, official site not verified)")
    print(f"Seed pool: {len(seed)} players")
    print(f"2026 players PRESENT in the pool: {len(all26) - len(missing)}  |  MISSING from the pool: {len(missing)}")
    for t, names in sorted(teams.items()):
        miss = sorted(n for n in names if key(n) not in seed)
        print(f"  {t:5} {len(names):3} listed, {len(names) - len(miss):3} in pool, missing: {', '.join(miss) or '-'}")
    print(f"Pool players NOT in any 2026 squad source (historic/legend entries): {len(extra)}")
    print("Source conflicts:")
    for c in src.SOURCE_CONFLICTS:
        print("  -", c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
