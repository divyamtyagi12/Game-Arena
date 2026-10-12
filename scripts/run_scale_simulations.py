"""Scale Draft Simulation Runner.
Executes:
- 20,000 random-picker draft simulations
- 20,000 batter-heavy draft simulations
- 20,000 adversarial draft simulations

Collects:
- Total completed squads
- Dead-end count
- Illegal auto-pick count
- Invalid completed squad count
- Exact execution time
"""
from __future__ import annotations

import sys
import time
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_ipl_draft_scale import (
    create_sample_pool,
    run_single_draft,
    validate_squad_legality,
)


def run_batch(strategy: str, count: int, seed: int):
    print(f"\n==========================================")
    print(f"Starting {count:,} {strategy.upper()} drafts (seed={seed})...")
    print(f"==========================================")
    sys.stdout.flush()

    pool, by_role = create_sample_pool()
    rng = random.Random(seed)

    total_dead_ends = 0
    total_illegal_autopicks = 0
    invalid_squads = 0
    squad_size_mismatches = 0

    t0 = time.perf_counter()
    for i in range(1, count + 1):
        roster, dead_ends, illegal_aps = run_single_draft(strategy, pool, by_role, rng)
        total_dead_ends += dead_ends
        total_illegal_autopicks += illegal_aps

        if len(roster) != 11:
            squad_size_mismatches += 1

        valid, _ = validate_squad_legality(roster)
        if not valid:
            invalid_squads += 1

        if i % 5000 == 0:
            elapsed = time.perf_counter() - t0
            print(f"  Progress: {i:,} / {count:,} drafts completed ({elapsed:.1f}s, {i/elapsed:.1f} drafts/sec)")
            sys.stdout.flush()

    total_time = time.perf_counter() - t0
    print(f"\nResults for {strategy.upper()} ({count:,} drafts):")
    print(f"  Total Drafts Completed:      {count:,}")
    print(f"  Dead-end Offers:             {total_dead_ends}")
    print(f"  Illegal Auto-Picks:          {total_illegal_autopicks}")
    print(f"  Invalid Squads:              {invalid_squads}")
    print(f"  Squad Size Mismatches (!=11):{squad_size_mismatches}")
    print(f"  Total Duration:              {total_time:.2f}s ({count/total_time:.1f} drafts/s)")
    sys.stdout.flush()

    assert total_dead_ends == 0, f"{strategy}: Found {total_dead_ends} dead ends"
    assert total_illegal_autopicks == 0, f"{strategy}: Found {total_illegal_autopicks} illegal auto picks"
    assert invalid_squads == 0, f"{strategy}: Found {invalid_squads} invalid squads"
    assert squad_size_mismatches == 0, f"{strategy}: Found {squad_size_mismatches} size mismatches"

    return {
        "strategy": strategy,
        "count": count,
        "dead_ends": total_dead_ends,
        "illegal_autopicks": total_illegal_autopicks,
        "invalid_squads": invalid_squads,
        "size_mismatches": squad_size_mismatches,
        "duration_sec": round(total_time, 2),
        "rate": round(count / total_time, 1),
    }


def main():
    print("=" * 60)
    print("IPL DRAFT SCALE FEASIBILITY VERIFICATION (60,000 DRAFTS)")
    print("=" * 60)

    res_random = run_batch("random", 20_000, seed=42)
    res_batter = run_batch("batter_heavy", 20_000, seed=1337)
    res_adversarial = run_batch("adversarial", 20_000, seed=9999)

    print("\n" + "=" * 60)
    print("ALL 60,000 DRAFT SIMULATIONS PASSED SUCCESSFULLY!")
    print("=" * 60)


if __name__ == "__main__":
    main()
