"""IPL Draft — end-to-end flows against REAL PostgreSQL with fake Telegram (see tests/support).

Skipped automatically when no test database is reachable (IPL_TEST_PG, default: local PG16 used during development).
These exercise the actual repository, services, worker and callback router; they do NOT exercise python-telegram-bot.
"""
import asyncio
import unittest

from ipl_draft.engine import standings as st
from ipl_draft.services import simulation_worker
from tests.support import pgdb
from tests.support.harness import GROUP, Harness


def _need_db():
    if not pgdb.available():
        raise unittest.SkipTest("no PostgreSQL test database (set IPL_TEST_PG; apply database/*.sql)")


def sql(q: str) -> str:
    return pgdb.run_sql(q)


def run(coro):
    return asyncio.run(coro)


async def _play(n_humans: int, seed: int = 3):
    h = Harness()
    Harness.reset_db()
    await h.ensure_players()
    humans = list(range(1, n_humans + 1))
    tid = await h.lobby(humans)
    q = await h.force_start(tid)
    assert q.answers[-1][0].startswith("🚀"), q.answers
    await h.draft_all(humans, seed, tid)
    orders = await h.set_batting_orders(tid, humans, seed)
    t = await h.run_to_end(tid)
    h.orders = orders
    return h, tid, t, humans


def _check_tournament(h, tid: int, n_humans: int) -> None:
    teams = n_humans + 4
    n_fix = teams * (teams - 1)
    assert sql(f"select state from ipl_tournaments where id={tid}") == "COMPLETED"
    # squads: 11 each, no duplicate players anywhere, humans + 4 franchises; a cricketer may sit in several squads but never twice in the SAME XI
    assert sql(f"select count(*) from ipl_tournament_teams where tournament_id={tid}") == str(teams)
    assert sql(f"select count(*) from ipl_tournament_teams where tournament_id={tid} and kind='SYSTEM'") == "4"
    assert sql(f"select count(*) from ipl_team_rosters where tournament_id={tid}") == str(teams * 11)
    assert sql(f"select count(distinct (team_id, player_id)) from ipl_team_rosters where tournament_id={tid}") == str(teams * 11)
    assert sql(f"select count(*) from (select team_id from ipl_team_rosters where tournament_id={tid} group by 1 having count(*)<>11) x") == "0"
    # fixtures / matches
    assert sql(f"select count(*) from ipl_fixtures where tournament_id={tid} and stage='LEAGUE'") == str(n_fix)
    assert sql(f"select count(*) from ipl_matches where tournament_id={tid} and stage='LEAGUE'") == str(n_fix)
    assert sql(f"select count(*) from ipl_fixtures where tournament_id={tid} and status<>'COMPLETED'") == "0"
    assert sql(f"""select count(*) from (select least(home_team_id,away_team_id) a, greatest(home_team_id,away_team_id) b, count(*)
                   from ipl_fixtures where tournament_id={tid} and stage='LEAGUE' group by 1,2 having count(*)<>2) x""") == "0"
    assert sql(f"select count(distinct (least(home_team_id,away_team_id), greatest(home_team_id,away_team_id))) from ipl_fixtures where tournament_id={tid} and stage='LEAGUE'") == str(teams * (teams - 1) // 2)
    # points table cross-checked against an independent recomputation with the pure engine
    rows = {int(r.split("|")[0]): dict(played=0, won=0, lost=0, no_result=0, points=0, runs_for=0, balls_faced=0, runs_against=0, balls_bowled=0, nrr=0.0)
            for r in sql(f"select id from ipl_tournament_teams where tournament_id={tid}").split("\n")}
    out = sql(f"""select m.team1_id, m.team2_id, m.winner_team_id,
                    (select i.runs from ipl_innings i where i.match_id=m.id and i.innings_no=1), (select i.runs from ipl_innings i where i.match_id=m.id and i.innings_no=2),
                    (select case when i.wickets=10 then 120 else i.legal_balls end from ipl_innings i where i.match_id=m.id and i.innings_no=1),
                    (select case when i.wickets=10 then 120 else i.legal_balls end from ipl_innings i where i.match_id=m.id and i.innings_no=2)
                  from ipl_matches m where m.tournament_id={tid} and m.stage='LEAGUE'""")
    for line in out.split("\n"):
        t1, t2, w, r1, r2, b1, b2 = line.split("|")
        t1, t2, r1, r2, b1, b2 = int(t1), int(t2), int(r1), int(r2), int(b1), int(b2)
        w = int(w) if w else None
        st.apply_result(rows[t1], won=None if w is None else w == t1, runs_for=r1, balls_faced=b1, runs_against=r2, balls_bowled=b2)
        st.apply_result(rows[t2], won=None if w is None else w == t2, runs_for=r2, balls_faced=b2, runs_against=r1, balls_bowled=b1)
    db_rows = sql(f"select team_id, played, won, lost, points, runs_for, balls_faced, runs_against, balls_bowled, round(nrr::numeric,3) from ipl_standings where tournament_id={tid}")
    assert len(db_rows.split("\n")) == teams
    for line in db_rows.split("\n"):
        tm, played, won, lost, pts, rf, bf, ra, bb, nrr = line.split("|")
        exp = rows[int(tm)]
        assert (int(played), int(won), int(lost), int(pts), int(rf), int(bf), int(ra), int(bb)) == \
               (exp["played"], exp["won"], exp["lost"], exp["points"], exp["runs_for"], exp["balls_faced"], exp["runs_against"], exp["balls_bowled"]), (tm, line, exp)
        assert abs(float(nrr) - exp["nrr"]) < 0.002, (nrr, exp["nrr"])
        assert int(played) == 2 * (teams - 1)
    assert sql(f"select sum(points) from ipl_standings where tournament_id={tid}") == str(2 * n_fix)       # no ties/NR here: 2 pts per match
    # playoffs: exactly four, right order, champion consistent
    stages = sql(f"select stage from ipl_matches where tournament_id={tid} and stage<>'LEAGUE' order by match_no").split("\n")
    assert stages == ["ELIMINATOR", "QUALIFIER_1", "QUALIFIER_2", "FINAL"], stages
    assert sql(f"select count(*) from ipl_playoff_matches where tournament_id={tid}") == "4"
    champ = sql(f"select winner_team_id from ipl_matches where tournament_id={tid} and stage='FINAL'")
    champ_human = sql(f"select count(*) from ipl_tournament_teams where id={champ} and kind='HUMAN'")
    assert champ and sql(f"select count(*) from ipl_tournament_results where tournament_id={tid} and champion") == champ_human
    top4 = sql(f"""select team_id from (select team_id, row_number() over (order by points desc, nrr desc) rn from ipl_standings where tournament_id={tid}) x where rn<=4""").split("\n")
    qual = sql(f"select string_agg(team_id::text, ',') from (select home_team_id team_id from ipl_matches m join ipl_fixtures f on f.id=m.fixture_id where m.tournament_id={tid} and m.stage<>'LEAGUE' union select away_team_id from ipl_matches m join ipl_fixtures f on f.id=m.fixture_id where m.tournament_id={tid} and m.stage<>'LEAGUE') z").split(",")
    assert len(set(qual)) == 4
    # scorecard consistency in the database
    assert sql(f"""select count(*) from ipl_innings i join ipl_matches m on m.id=i.match_id where m.tournament_id={tid}
                   and i.runs <> (select coalesce(sum(b.runs),0) from ipl_batting_scorecards b where b.innings_id=i.id)
                                 + coalesce((i.extras->>'wides')::int,0)+coalesce((i.extras->>'noballs')::int,0)+coalesce((i.extras->>'byes')::int,0)
                                 + coalesce((i.extras->>'legbyes')::int,0)+coalesce((i.extras->>'penalty')::int,0)""") == "0"
    assert sql(f"select count(*) from ipl_bowling_scorecards w join ipl_innings i on i.id=w.innings_id join ipl_matches m on m.id=i.match_id where m.tournament_id={tid} and not i.is_super_over and w.legal_balls>24") == "0"
    # awards
    got = set(sql(f"select award from ipl_tournament_awards where tournament_id={tid}").split("\n"))
    assert {"CHAMPION", "RUNNER_UP", "ORANGE_CAP", "PURPLE_CAP", "PLAYER_OF_TOURNAMENT", "MOST_SIXES", "BEST_INNINGS"} <= got, got
    # leaderboards: humans only, one row per human, SYSTEM teams never appear
    assert sql(f"select count(*) from ipl_user_stats") == str(n_humans)
    assert sql("select coalesce(sum(tournaments_played),0) from ipl_user_stats") == str(n_humans)
    assert sql("select count(*) from ipl_user_stats where user_id > 1000000") == "0"
    assert sql("select coalesce(sum(championships),0) from ipl_user_stats") in ("0", "1")
    # existing games untouched
    assert sql("select count(*) from games") == "0" and sql("select count(*) from nw_matches") == "0"
    assert sql("select count(*) from rating_history") == "0"
    # hidden ratings are never present in any text the bot sent
    blob = "\n".join(m.text.lower() for m in h.ctx.bot.sent)
    for word in ("overall_rating", "batting_rating", "power_hitting", "hidden rating", "editorial", "tier "):
        assert word not in blob, word
    assert "rating" not in blob


def test_full_12_team_tournament_8_humans():
    _need_db()
    h, tid, t, humans = run(_play(8))
    _check_tournament(h, tid, 8)
    assert sql(f"select count(*) from ipl_fixtures where tournament_id={tid} and stage='LEAGUE'") == "132"
    # group experience: exactly one dashboard message edited in place; compact matchday summaries only
    dash = sql(f"select dashboard_message_id from ipl_tournaments where id={tid}")
    assert dash
    group_msgs = [m for m in h.ctx.bot.sent if m.chat_id == GROUP]
    assert len(group_msgs) < 80, f"group spam: {len(group_msgs)} messages for 132 matches"
    assert h.ctx.bot.edits > 20
    # every human got their squad DM and a final result DM
    for u in humans:
        # the squad card is delivered by EDITING the last offer message, so read the current message contents
        current = [m.text for (chat, _), m in h.ctx.bot.messages.items() if chat == u]
        assert any("11/11" in x for x in current), current[-3:]      # final squad summary DM


def test_odd_team_count_three_humans_has_byes_and_completes():
    _need_db()
    h, tid, t, humans = run(_play(3))
    _check_tournament(h, tid, 3)                        # 7 teams → 42 fixtures
    assert sql(f"select count(*) from ipl_fixtures where tournament_id={tid} and stage='LEAGUE'") == "42"


def test_two_humans_minimum_and_recorded_group_history():
    _need_db()
    h, tid, t, humans = run(_play(2))
    _check_tournament(h, tid, 2)
    hist = run(h.rt.repo.group_history(GROUP))
    assert len(hist) == 1 and hist[0]["humans"] == 2
    g = run(h.rt.repo.group_leaderboard(GROUP, 1, 10, 0))
    assert g["total"] == 2
    glob = run(h.rt.repo.global_leaderboard(1, 10, 0))
    assert glob["total"] == 2 and all(r["user_id"] in (1, 2) for r in glob["rows"])
