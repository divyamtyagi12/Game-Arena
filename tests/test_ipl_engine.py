"""IPL Draft — pure unit tests (no database, no Telegram)."""
import random
import re

from ipl_draft import keyboards as kb, messages as m
from ipl_draft.config import IplSettings
from ipl_draft.data import cricsheet
from ipl_draft.data.names import disambiguate, normalize_name, slugify
from ipl_draft.data.seed_loader import build_seed_payload
from ipl_draft.engine import awards, draft_engine, fixtures as fx, playoffs, standings, team_builder
from ipl_draft.engine.match_engine import simulate_match, to_payload
from ipl_draft.engine.model import ATTRS, Player, Ratings, Squad
from ipl_draft.engine.ratings import CareerStats, rate_from_stats, rate_from_tier


def _pool():
    _, rows = build_seed_payload()
    return [Player(r["slug"], r["display_name"], r["role"], r["bowling_type"], r["batting_style"],
                   Ratings.from_mapping(r["ratings"])) for r in rows]


def _squad(tid, rng, pool=None):
    pool = pool or _pool()
    by = lambda role: [p for p in pool if p.role == role]  # noqa: E731
    xi = rng.sample(by("WK"), 1) + rng.sample(by("BAT"), 3) + rng.sample(by("AR"), 3) + rng.sample(by("BOWL"), 4)
    return Squad(tid, f"Team {tid}", f"T{tid}", tuple(xi))


# ───────────── fixtures ─────────────
def test_double_round_robin_counts_for_all_sizes():
    for n_h in range(2, 9):                     # 10..16 teams, odd sizes get byes
        teams = list(range(1, n_h + 9))
        f = fx.double_round_robin(teams)
        n = len(teams)
        assert len(f) == n * (n - 1)
        fx.validate(f, teams)
        assert max(x["matchday"] for x in f) == (2 * (n - 1) if n % 2 == 0 else 2 * n)
    assert len(fx.double_round_robin(list(range(16)))) == 240      # 8 humans + 8 system teams


def test_each_team_plays_30_matches_in_16_team_league_and_matchdays_are_disjoint():
    f = fx.double_round_robin(list(range(16)))
    for t in range(16):
        assert sum(1 for x in f if t in (x["home"], x["away"])) == 30
        assert sum(1 for x in f if x["home"] == t) == 15
    assert max(x["matchday"] for x in f) == 30


def test_fixture_validator_rejects_bad_schedules():
    f = fx.double_round_robin(list(range(10)))
    for broken in (f[:-1], f + [f[0]], [dict(x, away=x["home"]) if i == 0 else x for i, x in enumerate(f)]):
        try:
            fx.validate(broken, list(range(10)))
        except AssertionError:
            continue
        raise AssertionError("validator accepted a broken schedule")


# ───────────── standings / NRR ─────────────
def test_nrr_formula_uses_overs_not_balls_decimal():
    assert standings.nrr(180, 120, 160, 120) == round(180 / 20 - 160 / 20, 3) == 1.0
    assert standings.nrr(100, 0, 90, 120) == 0.0
    # 19.3 overs = 117 balls → 6 per over arithmetic, not 19.3 decimal overs
    assert standings.nrr(150, 117, 150, 120) == round(150 * 6 / 117 - 150 * 6 / 120, 3)


def test_all_out_counts_full_quota_for_nrr():
    assert standings.nrr_balls(77, True) == 120
    assert standings.nrr_balls(117, False) == 117


def test_points_and_apply_result():
    r = dict(played=0, won=0, lost=0, no_result=0, points=0, runs_for=0, balls_faced=0, runs_against=0, balls_bowled=0, nrr=0.0)
    standings.apply_result(r, won=True, runs_for=180, balls_faced=120, runs_against=170, balls_bowled=120)
    standings.apply_result(r, won=None, runs_for=0, balls_faced=0, runs_against=0, balls_bowled=0)
    standings.apply_result(r, won=False, runs_for=120, balls_faced=100, runs_against=121, balls_bowled=120)
    assert (r["played"], r["won"], r["lost"], r["no_result"], r["points"]) == (3, 1, 1, 1, 3)


def test_tie_break_chain_points_nrr_wins_h2h_then_team_no():
    rows = [dict(team_id=1, team_no=1, points=10, nrr=0.5, won=5), dict(team_id=2, team_no=2, points=10, nrr=0.9, won=5),
            dict(team_id=3, team_no=3, points=12, nrr=-1, won=6), dict(team_id=4, team_no=4, points=10, nrr=0.5, won=4)]
    assert standings.rank_teams(rows) == [3, 2, 1, 4]
    tied = [dict(team_id=7, team_no=2, points=8, nrr=0.1, won=4), dict(team_id=8, team_no=1, points=8, nrr=0.1, won=4)]
    results = [dict(team1=7, team2=8, winner=7), dict(team1=8, team2=7, winner=7)]
    assert standings.rank_teams(tied, results) == [7, 8]          # head-to-head beats team_no
    assert standings.rank_teams(tied) == [8, 7]                   # no h2h → lowest team_no, fully deterministic


# ───────────── playoffs ─────────────
def test_playoff_bracket_paths():
    seen = []

    def play(stage, h, a):
        seen.append((stage, h, a))
        return h
    out = playoffs.run_bracket([10, 20, 30, 40], play)
    assert [s for s, *_ in seen] == ["ELIMINATOR", "QUALIFIER_1", "QUALIFIER_2", "FINAL"]
    assert seen[0][1:] == (30, 40) and seen[1][1:] == (10, 20)
    assert seen[2][1:] == (20, 30)                     # loser Q1 vs winner Eliminator
    assert seen[3][1:] == (10, 20)                     # winner Q1 vs winner Q2
    assert out["champion"] == 10
    # underdog path: rank 4 can win it all
    out = playoffs.run_bracket([1, 2, 3, 4], lambda s, h, a: 4 if 4 in (h, a) else h)
    assert out["champion"] == 4
    try:
        playoffs.initial_pairings([1, 2, 3])
    except ValueError:
        pass
    else:
        raise AssertionError("needs 4 teams")


# ───────────── awards ─────────────
def _p(pid, **k):
    base = dict(player_id=pid, team_id=1, name=pid, runs=0, balls=0, fours=0, sixes=0, bat_innings=0, high=0, high_balls=0,
                wickets=0, bowl_runs=0, bowl_balls=0, maidens=0, best_w=0, catches=0, stumpings=0, run_outs=0, pom=0)
    base.update(k)
    return base


def test_awards_pick_the_right_players_and_ties_are_deterministic():
    stats = [_p("a", runs=500, balls=300, sixes=10, bat_innings=10, high=90, high_balls=50),
             _p("b", runs=500, balls=250, sixes=22, bat_innings=10, high=70, high_balls=30),
             _p("c", wickets=20, bowl_runs=300, bowl_balls=240, best_w=4), _p("d", wickets=20, bowl_runs=250, bowl_balls=240)]
    got = {a["award"]: a for a in awards.compute_awards(stats, {"id": 1, "name": "X"}, {"id": 2, "name": "Y"})}
    assert got["ORANGE_CAP"]["player_id"] == "b"           # equal runs → higher strike rate
    assert got["PURPLE_CAP"]["player_id"] == "d"           # equal wickets → better economy
    assert got["MOST_SIXES"]["player_id"] == "b"
    assert got["BEST_INNINGS"]["player_id"] == "a"
    assert got["CHAMPION"]["team_id"] == 1 and got["RUNNER_UP"]["team_id"] == 2
    assert awards.compute_awards(stats, None, None) == awards.compute_awards(list(reversed(stats)), None, None)
    assert awards.compute_awards([], None, None) == []


# ───────────── draft engine / pool ─────────────
def test_required_pool_formula_for_cross_team_drafting():
    # a cricketer may be in several squads: a team needs its 10 owned + 11 offered cards = 21 distinct players, any team count
    for humans in range(2, 9):
        assert draft_engine.required_pool(humans, 0) == 21
        assert draft_engine.required_pool(humans, 20) == 41


def test_auto_pick_uses_only_public_info_and_builds_balanced_squads():
    rng = random.Random(1)
    pool = _pool()
    roster = []
    for _ in range(11):
        cards = [{"pos": i + 1, "player": {k: p.__dict__[k] for k in ("role", "bowling_type")}} for i, p in enumerate(rng.sample(pool, 11))]
        assert "ratings" not in cards[0]["player"]
        pos = draft_engine.auto_pick(cards, roster, rng)
        roster.append(cards[pos - 1]["player"])
    n = draft_engine.needs(roster)
    assert n == {"wk": 0, "bowl": 0, "bat": 0} or sum(n.values()) <= 1, (n, [r["role"] for r in roster])


def test_force_roles_only_when_unavoidable():
    assert team_builder.force_roles([]) is None
    nine_bats = [{"role": "BAT", "bowling_type": "NONE"}] * 9
    assert team_builder.force_roles(nine_bats)           # 2 picks left, still needs a WK and bowlers


def test_snake_order_alternates():
    assert team_builder.snake_order([1, 2, 3], 1) == [1, 2, 3] and team_builder.snake_order([1, 2, 3], 2) == [3, 2, 1]


# ───────────── ratings ─────────────
def test_ratings_have_13_attributes_in_range_and_shrinkage_blocks_tiny_samples():
    star = rate_from_stats(CareerStats(matches=200, innings_bat=190, runs=6500, balls_faced=4800, outs=170, fours=560, sixes=230,
                                       dots_faced=1500, death_runs=900, death_balls=550, inn_20plus=95, ducks=8), "BAT", "NONE")
    fluke = rate_from_stats(CareerStats(matches=3, innings_bat=3, runs=270, balls_faced=120, outs=1, fours=30, sixes=15,
                                        dots_faced=20, death_runs=40, death_balls=20, inn_20plus=3), "BAT", "NONE")
    assert set(star.as_dict()) == set(ATTRS) and len(ATTRS) == 13
    assert all(0 <= v <= 100 for v in star.as_dict().values())
    assert star.batting_rating > fluke.batting_rating, "3 innings must never outrank a proven career"
    assert fluke.batting_rating <= 45 + 55 * 3 / 40 + 1


def test_missing_rating_record_falls_back_to_neutral():
    r = Ratings.from_mapping(None)
    assert r.overall_rating == 40
    assert Ratings.from_mapping({"batting_rating": 77}).batting_rating == 77


def test_tier_ratings_are_deterministic_and_ordered():
    a1 = rate_from_tier("BAT", "S", "NONE", "x").overall_rating
    assert a1 == rate_from_tier("BAT", "S", "NONE", "x").overall_rating
    assert rate_from_tier("BAT", "S", "NONE", "x").overall_rating > rate_from_tier("BAT", "C", "NONE", "x").overall_rating


# ───────────── seed data honesty / integrity ─────────────
def test_seed_payload_is_labelled_and_unique():
    src, rows = build_seed_payload()
    assert "unverified" in src["name"].lower() and "no statistics" in src["license"].lower()
    assert len(rows) >= 300
    assert len({r["slug"] for r in rows}) == len(rows) == len({r["display_name"].lower() for r in rows})
    assert all(r["data_quality"] == "SEED" and r["ipl_verified"] is False for r in rows)
    assert all(r["ratings"]["rating_source"] == "EDITORIAL_TIER" and "career" not in r for r in rows)
    assert {r["role"] for r in rows} == {"BAT", "BOWL", "AR", "WK"}


def test_name_helpers():
    assert normalize_name("D'Arcy  Short") == "darcy short" and slugify("Rashid Khan") == "rashid-khan"
    d = disambiguate([("a-sharma", "A Sharma"), ("a-sharma-2", "A Sharma")])
    assert sorted(d.values()) == ["A Sharma", "A Sharma (2)"]


# ───────────── match engine ─────────────
def test_match_scorecard_invariants_over_many_matches():
    rng = random.Random(11)
    pool = _pool()
    for i in range(60):
        a, b = _squad(1, rng, pool), _squad(2, rng, pool)
        out = simulate_match(a, b, random.Random(i), seed=str(i))
        assert out.winner in (1, 2) and out.result_type in ("RUNS", "WICKETS", "TIE_SUPER_OVER")
        main = [x for x in out.innings if not x.is_super_over]
        assert len(main) == 2
        for inn in out.innings:
            assert 0 <= inn.wickets <= 10 and inn.legal_balls <= (6 if inn.is_super_over else 120)
            assert sum(l.runs for l in inn.bat_lines) + sum(inn.extras.values()) == inn.runs, "batting + extras == total"
            assert sum(b_.runs for b_ in inn.bowl_lines) == inn.runs - inn.extras.get("byes", 0) - inn.extras.get("legbyes", 0) \
                - inn.extras.get("penalty", 0)
            assert sum(b_.wickets for b_ in inn.bowl_lines) <= inn.wickets
            assert sum(l.balls for l in inn.bat_lines) >= 0
            if not inn.is_super_over:
                assert all(b_.balls <= 24 for b_ in inn.bowl_lines), "4-over bowler cap"
                assert inn.legal_balls == sum(b_.balls for b_ in inn.bowl_lines)
                if inn.wickets < 10 and not (inn.target and inn.runs >= inn.target):
                    assert inn.legal_balls == 120, "an unfinished innings runs the full 20 overs"
        i1, i2 = main
        assert i2.target == i1.runs + 1
        if out.result_type == "RUNS":
            assert i1.runs - i2.runs == out.margin > 0
        elif out.result_type == "WICKETS":
            assert i2.runs > i1.runs and out.margin == 10 - i2.wickets
        else:
            assert i1.runs == i2.runs and out.super_overs >= 1
        assert len({p for p in (l.player.id for l in main[0].bat_lines)}) == 11
        pl = {p.id for p in a.players} | {p.id for p in b.players}
        teams = {a.team_id, b.team_id}
        assert out.pom[0] in teams and out.pom[1] in pl, out.pom           # (team_id, player_id): same cricketer may be on both sides
        assert out.best_bowler[0] in teams and out.best_bowler[1] in pl


def test_match_is_deterministic_for_the_same_seed_and_varies_otherwise():
    rng = random.Random(5)
    a, b = _squad(1, rng), _squad(2, rng)
    x = to_payload(simulate_match(a, b, random.Random(99), seed="s"))
    y = to_payload(simulate_match(a, b, random.Random(99), seed="s"))
    z = to_payload(simulate_match(a, b, random.Random(100), seed="s"))
    assert x == y and x != z


def test_scoring_is_realistic_t20_and_strength_matters_without_being_deterministic():
    rng = random.Random(3)
    pool = _pool()
    totals, wins_top = [], 0
    top = sorted(pool, key=lambda p: -p.ratings.overall_rating)
    ranked = {role: [p for p in top if p.role == role] for role in ("WK", "BAT", "AR", "BOWL")}
    strong = Squad(1, "S", "S", tuple(ranked["WK"][:1] + ranked["BAT"][:3] + ranked["AR"][:3] + ranked["BOWL"][:4]))
    weak = Squad(2, "W", "W", tuple(ranked["WK"][-1:] + ranked["BAT"][-3:] + ranked["AR"][-3:] + ranked["BOWL"][-4:]))
    for i in range(150):
        o = simulate_match(_squad(1, rng, pool), _squad(2, rng, pool), random.Random(i))
        totals += [x.runs for x in o.innings[:2]]
    mean = sum(totals) / len(totals)
    assert 140 <= mean <= 195, mean
    for i in range(200):
        wins_top += simulate_match(strong, weak, random.Random(1000 + i)).winner == 1
    assert 120 <= wins_top <= 196, f"strong side should win clearly but not always: {wins_top}/200"


def test_super_over_loop_is_bounded(monkeypatch=None):
    from ipl_draft.engine import match_engine as me
    assert me.MAX_SUPER_OVERS >= 1
    rng = random.Random(8)
    pool = _pool()
    ties = 0
    for i in range(400):
        o = simulate_match(_squad(1, rng, pool), _squad(2, rng, pool), random.Random(i))
        if o.result_type == "TIE_SUPER_OVER":
            ties += 1
            assert 1 <= o.super_overs <= me.MAX_SUPER_OVERS and o.winner in (1, 2)
            assert len([x for x in o.innings if x.is_super_over]) == 2 * o.super_overs or o.super_overs == me.MAX_SUPER_OVERS
    assert ties >= 1, "a few ties expected in 400 matches"


# ───────────── cricsheet importer (synthetic data; real archive can't be downloaded here) ─────────────
def _delivery(batter, bowler, runs=0, extras=None, wicket=None, non_boundary=False):
    d = {"batter": batter, "bowler": bowler, "non_striker": "S", "runs": {"batter": runs, "extras": sum((extras or {}).values()), "total": runs + sum((extras or {}).values())}}
    if non_boundary:
        d["runs"]["non_boundary"] = True
    if extras:
        d["extras"] = extras
    if wicket:
        d["wickets"] = [wicket]
    return d


def test_cricsheet_aggregation_counts_rules():
    people = {"V Kohli": "id-vk", "J Bumrah": "id-jb", "MS Dhoni": "id-msd", "S": "id-s"}
    match = {"info": {"season": "2007/08", "dates": ["2008-04-18"], "registry": {"people": people},
                      "players": {"A": ["V Kohli", "MS Dhoni"], "B": ["J Bumrah"]}},
             "innings": [{"team": "A", "overs": [
                 {"over": 0, "deliveries": [_delivery("V Kohli", "J Bumrah", 4), _delivery("V Kohli", "J Bumrah", 6),
                                            _delivery("V Kohli", "J Bumrah", 0, {"wides": 1}),
                                            _delivery("V Kohli", "J Bumrah", 1, {"noballs": 1}),
                                            _delivery("V Kohli", "J Bumrah", 4, non_boundary=True),
                                            _delivery("V Kohli", "J Bumrah", 0, wicket={"player_out": "V Kohli", "kind": "caught", "fielders": [{"name": "MS Dhoni"}]}),
                                            _delivery("MS Dhoni", "J Bumrah", 0, wicket={"player_out": "MS Dhoni", "kind": "run out", "fielders": [{"name": "J Bumrah"}]})]},
                 {"over": 15, "deliveries": [_delivery("S", "J Bumrah", 2)]}]},
                 {"team": "B", "super_over": True, "overs": [{"over": 0, "deliveries": [_delivery("J Bumrah", "V Kohli", 6)]}]}]}
    accs = cricsheet.aggregate([match])
    k, b, d = accs["id-vk"], accs["id-jb"], accs["id-msd"]
    assert (k.t["runs"], k.t["balls_faced"], k.t["fours"], k.t["sixes"], k.t["outs"]) == (15, 5, 1, 1, 1)   # wide not faced, overthrow 4 not a boundary
    assert k.t["innings_bat"] == 1 and k.t["innings_bowl"] == 0
    assert b.t["balls_bowled"] == 6, "wide and no-ball are not legal balls"
    assert b.t["runs_conceded"] == 19, "bat runs + wides + no-balls"
    assert b.t["wickets"] == 1, "run outs are not bowler wickets"
    assert d.t["catches"] == 1 and b.t["run_outs"] == 1
    assert k.first == k.last == 2008, "2007/08 is the 2008 season"
    assert accs["id-jb"].t["runs"] == 0, "super over innings are ignored"
    assert k.t["matches"] == 1


def test_cricsheet_payload_is_marked_verified_and_reports_unmatched_seeds():
    people = {"V Kohli": "id-vk", "Z Nobody": "id-zn"}
    match = {"info": {"season": "2019", "dates": ["2019-04-01"], "registry": {"people": people}, "players": {"A": ["V Kohli", "Z Nobody"]}},
             "innings": [{"team": "A", "overs": [{"over": 0, "deliveries": [_delivery("V Kohli", "Z Nobody", 4)]}]}]}
    seeds = {normalize_name(t[0]): t for t in __import__("ipl_draft.data.seed_players", fromlist=["x"]).SEED_PLAYERS}
    src, players, rep = cricsheet.build_payload(cricsheet.aggregate([match]), seeds, min_matches=1)
    kohli = next(p for p in players if p["cricsheet_id"] == "id-vk")
    assert kohli["full_name"] == "Virat Kohli" and kohli["ipl_verified"] and kohli["data_quality"] == "CRICSHEET"
    assert kohli["ratings"]["rating_source"] == "CRICSHEET_STATS" and kohli["career"]["runs"] == 4
    assert "ODC-By" in src["license"] and rep["seed_matched"] == 1 and len(rep["seed_unmatched"]) == rep["seed_total"] - 1
    assert next(p for p in players if p["cricsheet_id"] == "id-zn")["is_eligible"] is True


# ───────────── UI contracts ─────────────
def test_callback_data_namespace_and_length():
    cands = [{"pos": i, "player": {"name": "N" * 40}} for i in range(1, 12)]
    mk = [kb.lobby(10 ** 9), kb.offer(10 ** 12, cands, 10 ** 9), kb.confirm(10 ** 12, 11), kb.draft_done(10 ** 9, 10 ** 9),
          kb.group_dashboard(10 ** 9), kb.teams_ready(10 ** 9), kb.match_buttons(10 ** 9, 10 ** 12), kb.pager("fx", 10 ** 9, 99, 99, "ipl:tr:1"),
          kb.table_markup(10 ** 9), kb.final_buttons(10 ** 9, 10 ** 12), kb.leaderboard("g", 0, 99)]
    for markup in mk:
        for row in markup.inline_keyboard:
            for b in row:
                if b.callback_data:
                    assert b.callback_data.startswith(("ipl:", "lbd:")) and len(b.callback_data.encode()) < 64, b.callback_data


def test_no_hidden_rating_text_leaks_into_messages():
    src, rows = build_seed_payload()
    p = {"id": "x", "name": "Test <Player>", "role": "BAT", "nationality": "India", "bowling_type": "NONE",
         "ratings": rows[0]["ratings"], "overall_rating": 91}
    card = {"pos": 1, "player": p}
    text = m.offer_text({"name": "Mine XI"}, {"round_no": 1, "candidates": [card], "deadline_at": None}, 0, 45)
    low = text.lower()
    for word in ("rating", "overall", "batting_rating", "power_hitting", "tier", "91"):
        assert word not in low, word
    assert "&lt;Player&gt;" in text, "HTML must be escaped"
    assert "rating" not in m.confirm_text(p).lower()


def test_settings_defaults_and_bounds(monkeypatch=None):
    s = IplSettings.from_env()
    assert (s.min_humans, s.max_humans, s.draft_seconds) == (2, 8, 45)
    import os
    os.environ["IPL_MAX_PLAYERS"] = "99"
    os.environ["IPL_DRAFT_SECONDS"] = "abc"
    try:
        s = IplSettings.from_env()
        assert s.max_humans == 8 and s.draft_seconds == 45
    finally:
        del os.environ["IPL_MAX_PLAYERS"], os.environ["IPL_DRAFT_SECONDS"]


def test_rules_text_mentions_key_rules():
    t = m.rules_text(45)
    for s in ("11", "Eliminator", "Qualifier", "double round robin", "5 system franchises"):
        assert s.lower() in t.lower(), s
    assert re.search(r"2.8 human", t)
