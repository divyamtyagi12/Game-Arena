import random
from fractions import Fraction as F

from game import number_wars as nw


def _by(outcome):
    return {r.user_id: r for r in outcome.results}


def test_design_doc_example():
    # A20 B40 C60 D80 E50 → average 50, target 40 → B wins (exact, already at MAX_HP)
    hp = {u: nw.MAX_HP for u in "ABCDE"}
    out = nw.resolve_round(hp, dict(A=20, B=40, C=60, D=80, E=50))
    r = _by(out)
    assert out.average == 50 and out.target == 40
    assert r["B"].is_winner and r["B"].exact and r["B"].hp_after == nw.MAX_HP
    assert [r[u].hp_after for u in "ACDE"] == [nw.MAX_HP - 1] * 4
    assert [r[u].distance for u in "ACDE"] == [20, 20, 40, 10]


def test_exact_bonus_and_cap():
    out = nw.resolve_round({"A": 3, "B": 5}, dict(A=50, B=50), F(1, 1))
    r = _by(out)
    assert out.target == 50 and r["A"].exact and r["A"].hp_after == 5    # 3 + 2
    assert r["B"].hp_after == 5                                         # capped


def test_exact_means_mathematically_exact_not_rounded():
    # 1 and 2 → avg 1.5 → target 1.2. Closest is 1 (distance 0.2) — NOT exact even though round(1.2)==1
    out = nw.resolve_round({"A": 5, "B": 5}, dict(A=1, B=2))
    r = _by(out)
    assert out.target == F(6, 5)
    assert r["A"].is_winner and not r["A"].exact and r["A"].hp_after == 5
    assert r["B"].hp_after == 4


def test_ties_all_avoid_damage():
    out = nw.resolve_round({"A": 5, "B": 5, "C": 5}, dict(A=1, B=100, C=50))
    # avg 151/3 → target 120.8/3; distances ~39.27, ~59.73, ~9.73 → C wins alone
    assert [r.is_winner for r in out.results] == [False, False, True]
    out = nw.resolve_round({"A": 5, "B": 5, "C": 5}, dict(A=20, B=20, C=80))
    # avg 40 → target 32; A and B tie at 12 → both safe
    r = _by(out)
    assert r["A"].is_winner and r["B"].is_winner and not r["C"].is_winner
    assert r["C"].hp_after == 4


def test_missing_submission_costs_two_and_is_excluded_from_average():
    out = nw.resolve_round({"A": 5, "B": 5, "C": 5}, dict(A=50, B=50))
    r = _by(out)
    assert out.average == 50 and out.submitted == 2
    assert r["C"].number is None and r["C"].hp_after == 3


def test_nobody_submits():
    out = nw.resolve_round({"A": 5, "B": 2}, {})
    assert out.target is None and out.average is None
    assert [r.hp_after for r in out.results] == [3, 0]
    assert out.results[1].eliminated and not out.zero_damage


def test_single_submitter_wins_no_bonus():
    out = nw.resolve_round({"A": 3, "B": 5, "C": 5}, dict(A=50))
    r = _by(out)
    assert r["A"].is_winner and not r["A"].exact and r["A"].hp_after == 3   # no +2 bonus
    assert r["B"].hp_after == 3 and r["C"].hp_after == 3
    assert out.target is None


def test_out_of_range_and_foreign_submissions_ignored():
    out = nw.resolve_round({"A": 8, "B": 8}, dict(A=50, B=0, C=101, Z=3))
    assert out.submitted == 1 and _by(out)["A"].is_winner


def test_everyone_picks_same_number_is_zero_damage():
    out = nw.resolve_round({u: 8 for u in "ABC"}, dict(A=50, B=50, C=50))
    assert out.zero_damage and all(r.is_winner for r in out.results)


def test_fractional_multipliers():
    out = nw.resolve_round({"A": 8, "B": 8}, dict(A=20, B=40), F(3, 2))   # avg 30 → target 45
    assert out.target == 45 and _by(out)["B"].is_winner


def _st(uid, hp, wins=0, dist=0.0, alive=True):
    return nw.Standing(uid, hp, wins, dist, alive)


def test_last_standing_wins():
    before = [_st("A", 1), _st("B", 5)]
    after = nw.apply_outcome(before, nw.resolve_round({"A": 1, "B": 5}, dict(A=50, B=40)))
    assert nw.decide_end(before, after, 4, 30) == (["B"], nw.END_LAST_STANDING)


def test_all_eliminated_uses_pre_round_hp_then_wins_then_distance():
    before = [_st("A", 2, wins=1, dist=9), _st("B", 2, wins=1, dist=3), _st("C", 1)]
    after = [_st(s.user_id, 0, alive=False) for s in before]
    assert nw.decide_end(before, after, 7, 30) == (["B"], nw.END_ALL_ELIMINATED)   # distance breaks tie
    before = [_st("A", 2, wins=2), _st("B", 2, wins=1)]
    assert nw.decide_end(before, [_st("A", 0, alive=False), _st("B", 0, alive=False)], 7, 30) == (
        ["A"], nw.END_ALL_ELIMINATED)
    before = [_st("A", 2), _st("B", 2)]
    ids, why = nw.decide_end(before, [_st("A", 0, alive=False), _st("B", 0, alive=False)], 7, 30)
    assert sorted(ids) == ["A", "B"] and why == nw.END_ALL_ELIMINATED               # joint champions


def test_round_limit_ranking_and_continue():
    after = [_st("A", 6, 3, 40), _st("B", 6, 3, 25), _st("C", 8, 0, 90)]
    assert nw.decide_end(after, after, 29, 30) is None
    assert nw.decide_end(after, after, 30, 30) == (["C"], nw.END_ROUND_LIMIT)       # HP first
    after = [_st("A", 6, 3, 40), _st("B", 6, 3, 25)]
    assert nw.decide_end(after, after, 30, 30) == (["B"], nw.END_ROUND_LIMIT)       # lower distance
    after = [_st("A", 6, 3, 25), _st("B", 6, 3, 25)]
    assert sorted(nw.decide_end(after, after, 30, 30)[0]) == ["A", "B"]


def test_random_matches_always_terminate_and_conserve_invariants():
    rng = random.Random(42)
    for _ in range(300):
        players = list(range(rng.randint(3, 20)))
        st = [_st(u, nw.MAX_HP) for u in players]
        for rnd in range(1, 31):
            alive = [s for s in st if s.alive]
            hp = {s.user_id: s.hp for s in alive}
            nums = {u: rng.randint(1, 100) for u in hp if rng.random() > 0.1}
            out = nw.resolve_round(hp, nums, rng.choice([F(1, 2), F(4, 5), F(6, 5), F(3, 2)]))
            assert all(0 <= r.hp_after <= nw.MAX_HP for r in out.results)
            assert out.submitted == 0 or any(r.is_winner for r in out.results)
            after = nw.apply_outcome(alive, out)
            end = nw.decide_end(alive, after, rnd, 30)
            st = after
            if end:
                ids, _why = end
                assert ids and set(ids) <= set(players)
                break
        else:
            raise AssertionError("match did not end within 30 rounds")
