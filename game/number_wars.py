"""NUMBER WARS rules engine — pure functions, no I/O (unit-tested).

Targets are computed with exact rational arithmetic (Fraction), so "exact match" means the
number equals the mathematically exact target, not merely its rounded display value.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping, Sequence

MAX_HP = 5
NUMBER_MIN, NUMBER_MAX = 1, 100
LOSE_DAMAGE = 1          # valid submission, not closest
MISS_DAMAGE = 2          # no submission
EXACT_BONUS = 2          # exact target match (capped at MAX_HP)
BASE_MULTIPLIER = Fraction(4, 5)
SUDDEN_DEATH_AFTER = 3   # consecutive zero-damage rounds before a random-multiplier round

END_LAST_STANDING = "LAST_STANDING"
END_ROUND_LIMIT = "ROUND_LIMIT"
END_ALL_ELIMINATED = "ALL_ELIMINATED"


@dataclass(frozen=True)
class PlayerResult:
    user_id: int
    number: int | None          # None = missed the round
    distance: Fraction | None
    hp_before: int
    hp_delta: int
    is_winner: bool
    exact: bool

    @property
    def hp_after(self) -> int:
        return max(0, min(MAX_HP, self.hp_before + self.hp_delta))

    @property
    def eliminated(self) -> bool:
        return self.hp_after <= 0


@dataclass(frozen=True)
class RoundOutcome:
    multiplier: Fraction
    average: Fraction | None
    target: Fraction | None
    results: tuple[PlayerResult, ...]

    @property
    def submitted(self) -> int:
        return sum(1 for r in self.results if r.number is not None)

    @property
    def zero_damage(self) -> bool:
        """Nobody lost HP — feeds the sudden-death streak."""
        return all(r.hp_delta >= 0 for r in self.results)


@dataclass(frozen=True)
class Standing:
    user_id: int
    hp: int
    round_wins: int
    total_distance: float
    alive: bool = True


def resolve_round(hp: Mapping[int, int], numbers: Mapping[int, int],
                  multiplier: Fraction = BASE_MULTIPLIER) -> RoundOutcome:
    """`hp` = every player alive at round start; `numbers` = locked submissions (others ignored)."""
    subs = {u: int(n) for u, n in numbers.items()
            if u in hp and NUMBER_MIN <= int(n) <= NUMBER_MAX}
    results: list[PlayerResult] = []

    if not subs:                                   # everyone missed: no target, all −2
        for u, h in hp.items():
            results.append(PlayerResult(u, None, None, h, -MISS_DAMAGE, False, False))
        return RoundOutcome(multiplier, None, None, tuple(results))

    if len(subs) == 1:                             # lone submitter wins, no bonus, others −2
        (solo,) = subs
        for u, h in hp.items():
            if u == solo:
                results.append(PlayerResult(u, subs[u], None, h, 0, True, False))
            else:
                results.append(PlayerResult(u, None, None, h, -MISS_DAMAGE, False, False))
        return RoundOutcome(multiplier, None, None, tuple(results))

    average = Fraction(sum(subs.values()), len(subs))
    target = multiplier * average
    dist = {u: abs(Fraction(n) - target) for u, n in subs.items()}
    best = min(dist.values())
    for u, h in hp.items():
        if u not in subs:
            results.append(PlayerResult(u, None, None, h, -MISS_DAMAGE, False, False))
        elif dist[u] == best:                      # all tied players avoid damage
            exact = dist[u] == 0
            delta = min(EXACT_BONUS, MAX_HP - h) if exact else 0
            results.append(PlayerResult(u, subs[u], dist[u], h, delta, True, exact))
        else:
            results.append(PlayerResult(u, subs[u], dist[u], h, -LOSE_DAMAGE, False, False))
    return RoundOutcome(multiplier, average, target, tuple(results))


def apply_outcome(before: Sequence[Standing], outcome: RoundOutcome) -> list[Standing]:
    by_user = {r.user_id: r for r in outcome.results}
    after = []
    for s in before:
        r = by_user[s.user_id]
        hp = r.hp_after
        after.append(Standing(
            s.user_id, hp, s.round_wins + (1 if r.is_winner else 0),
            s.total_distance + (float(r.distance) if r.distance is not None else 0.0), hp > 0))
    return after


def _rank_key(s: Standing) -> tuple[int, int, float]:
    # higher HP, then more round wins, then LOWER cumulative distance
    return (-s.hp, -s.round_wins, round(s.total_distance, 4))


def top_ranked(standings: Sequence[Standing]) -> list[int]:
    """Everyone tied for first under HP → round wins → distance (several = joint champions)."""
    best = min(_rank_key(s) for s in standings)
    return [s.user_id for s in standings if _rank_key(s) == best]


def decide_end(before: Sequence[Standing], after: Sequence[Standing],
               round_number: int, max_rounds: int) -> tuple[list[int], str] | None:
    """None = keep playing, else (champion_ids, reason).

    before: players alive at the START of the round (pre-round HP / wins / distance)
    after : the same players after the round was applied
    """
    survivors = [s for s in after if s.alive]
    if len(survivors) == 1:
        return [survivors[0].user_id], END_LAST_STANDING
    if not survivors:                              # simultaneous knock-out: judge the pre-round state
        return top_ranked(before), END_ALL_ELIMINATED
    if round_number >= max_rounds:
        return top_ranked(survivors), END_ROUND_LIMIT
    return None
