"""IPL Draft v3 — cross-team drafting, per-team statistics, restart-safe batting orders (real PostgreSQL, fake Telegram)."""
import asyncio
import random
import unittest

from ipl_draft.engine.model import Squad
from ipl_draft.services import batting_order_service, simulation_worker
from tests.support import fakes, pgdb
from tests.support.harness import Harness


def _need_db():
    if not pgdb.available():
        raise unittest.SkipTest("no PostgreSQL test database (set IPL_TEST_PG; apply database/*.sql)")


def sql(q):
    return pgdb.run_sql(q)


def run(c):
    return asyncio.run(c)


async def _started(n=2):
    h = Harness()
    Harness.reset_db()
    await h.ensure_players()
    humans = list(range(1, n + 1))
    tid = await h.lobby(humans)
    await h.force_start(tid)
    await h.ctx.drain()
    return h, tid, humans


def test_human_can_draft_a_player_already_on_an_ai_team_but_not_twice_in_one_xi():
    _need_db()

    async def go():
        h, tid, humans = await _started(2)
        for _ in range(60):                                                     # let the AI franchises finish drafting
            await h.ctx.drain()
            if sql(f"select count(*) from ipl_tournament_teams where tournament_id={tid} and kind='SYSTEM' and squad_complete") == "4":
                break
            await asyncio.sleep(0.2)
        assert sql(f"select count(*) from ipl_tournament_teams where tournament_id={tid} and kind='SYSTEM' and squad_complete") == "4"
        ai_pid = sql(f"""select r.player_id from ipl_team_rosters r join ipl_tournament_teams t on t.id=r.team_id
                          where r.tournament_id={tid} and t.kind='SYSTEM' order by r.player_id limit 1""")
        ai_team = sql(f"select team_id from ipl_team_rosters where tournament_id={tid} and player_id='{ai_pid}' limit 1")
        offer = sql(f"select o.id from ipl_draft_offers o join ipl_tournament_teams t on t.id=o.team_id where t.user_id=1 and o.status='OPEN'")
        # rig ONE card of user 1's open offer to be the AI-owned cricketer (cards are normally random)
        sql(f"delete from ipl_draft_offer_candidates where offer_id={offer} and player_id='{ai_pid}' and position<>1")   # keep (offer, player) unique
        sql(f"update ipl_draft_offer_candidates set player_id='{ai_pid}' where offer_id={offer} and position=1")
        dm = fakes.FakeChat(1, "private")
        m = h.open_offer_for(1)
        q = await h.click(f"ipl:pk:{offer}:1", 1, chat=dm, message_id=m.message_id)      # single tap: picking a card drafts him
        assert q.answers[-1][0].startswith("✅") and q.answers[-1][0].endswith("selected!"), q.answers
        owners = sql(f"select string_agg(team_id::text, ',' order by team_id) from ipl_team_rosters where tournament_id={tid} and player_id='{ai_pid}'")
        team1_id = sql(f"select id from ipl_tournament_teams where tournament_id={tid} and user_id=1")
        # AI franchises draft independently, so he may sit in several AI squads; what matters is that the human got him too
        assert team1_id in owners.split(",") and ai_team in owners.split(",") and len(owners.split(",")) >= 2, owners
        # the SAME cricketer can never be offered to (or inserted for) that human's XI again
        team1 = sql(f"select id from ipl_tournament_teams where tournament_id={tid} and user_id=1")
        assert sql(f"""select count(*) from ipl_draft_offer_candidates c join ipl_draft_offers o on o.id=c.offer_id
                       where o.team_id={team1} and o.status='OPEN' and c.player_id='{ai_pid}'""") == "0"
        try:
            sql(f"insert into ipl_team_rosters (tournament_id, team_id, player_id, slot, source) values ({tid},{team1},'{ai_pid}',11,'DRAFT')")
        except pgdb.PgError as e:
            assert e.code == "23505", e.code
        else:
            raise AssertionError("duplicate inside one XI must be rejected by the database")
    run(go())


def test_stats_are_separate_per_tournament_team_and_player():
    """A cricketer in two squads of one tournament produces two independent stat rows; team totals still reconcile exactly."""
    _need_db()

    async def go():
        h, tid, humans = await _started(2)
        for _ in range(60):
            await h.ctx.drain()
            if sql(f"select count(*) from ipl_tournament_teams where tournament_id={tid} and kind='SYSTEM' and squad_complete") == "4":
                break
            await asyncio.sleep(0.2)
        # share one AI cricketer with BOTH humans
        ai_pid = sql(f"""select r.player_id from ipl_team_rosters r join ipl_tournament_teams t on t.id=r.team_id
                          where r.tournament_id={tid} and t.kind='SYSTEM' order by r.player_id limit 1""")
        for u in humans:
            offer = sql(f"select o.id from ipl_draft_offers o join ipl_tournament_teams t on t.id=o.team_id where t.user_id={u} and o.status='OPEN'")
            sql(f"delete from ipl_draft_offer_candidates where offer_id={offer} and player_id='{ai_pid}' and position<>1")   # keep (offer, player) unique
            sql(f"update ipl_draft_offer_candidates set player_id='{ai_pid}' where offer_id={offer} and position=1")
            m = h.open_offer_for(u)
            dm = fakes.FakeChat(u, "private")
            q = await h.click(f"ipl:pk:{offer}:1", u, chat=dm, message_id=m.message_id)      # single tap: picking a card drafts him
            assert q.answers[-1][0].startswith("✅") and q.answers[-1][0].endswith("selected!"), q.answers
        # AI franchises draft independently, so he may already be in several AI squads; both humans now own him too
        assert int(sql(f"select count(*) from ipl_team_rosters where tournament_id={tid} and player_id='{ai_pid}'")) >= 3
        await h.draft_all(humans, 7, tid)
        await h.set_batting_orders(tid, humans)
        t = await h.run_to_end(tid)
        assert t["state"] == "COMPLETED"
        stats = await h.rt.repo.player_stats(tid)
        keys = [(r["player_id"], r["team_id"]) for r in stats]
        assert len(keys) == len(set(keys)), "one stat row per (player, team)"
        shared = [r for r in stats if r["player_id"] == ai_pid]
        assert len({r["team_id"] for r in shared}) >= 2, "the shared cricketer has separate rows for each team he played for"
        # exact reconciliation: runs/wickets per team in the stats == scorecard totals of that team
        for tm in sql(f"select id from ipl_tournament_teams where tournament_id={tid}").split("\n"):
            runs = sum(r["runs"] for r in stats if r["team_id"] == int(tm))
            wk = sum(r["wickets"] for r in stats if r["team_id"] == int(tm))
            db_runs = int(sql(f"""select coalesce(sum(b.runs),0) from ipl_batting_scorecards b join ipl_innings i on i.id=b.innings_id
                                  join ipl_matches m on m.id=i.match_id where m.tournament_id={tid} and i.batting_team_id={tm} and not i.is_super_over"""))
            db_wk = int(sql(f"""select coalesce(sum(w.wickets),0) from ipl_bowling_scorecards w join ipl_innings i on i.id=w.innings_id
                                join ipl_matches m on m.id=i.match_id where m.tournament_id={tid} and i.bowling_team_id={tm} and not i.is_super_over"""))
            assert (runs, wk) == (db_runs, db_wk), (tm, runs, db_runs, wk, db_wk)
        # awards name the team
        assert sql(f"select count(*) from ipl_tournament_awards where tournament_id={tid} and award='ORANGE_CAP' and team_id is not null") == "1"
    run(go())


def test_batting_order_survives_restart_deadline_autofill_and_drives_the_match():
    _need_db()

    async def go():
        h, tid, humans = await _started(2)
        await h.draft_all(humans, 5, tid)
        t1 = await h.rt.repo.team_of_user(tid, 1)
        dm1 = fakes.FakeChat(1, "private")
        # user 1 picks the batting order for 3 players only (never confirms); user 2 confirms fully
        for pos in (11, 5, 2):
            await h.click(f"ipl:bop:{t1['id']}:{pos}", 1, chat=dm1)
        await h.set_batting_orders(tid, [2])
        first3 = [r for r in sql(f"select player_id from ipl_team_rosters where team_id={t1['id']} and batting_order is not null order by batting_order").split("\n")]
        assert len(first3) == 3
        # ── bot restart: brand-new runtime, nothing in memory ──
        h.restart()
        assert sql(f"select batting_order_confirmed from ipl_tournament_teams where id={t1['id']}") == "f"
        assert sql(f"select batting_order_deadline_at is not null from ipl_tournament_teams where id={t1['id']}") == "t"
        await simulation_worker.tick(h.rt)
        await h.ctx.drain()
        assert sql(f"select state from ipl_tournaments where id={tid}") == "DRAFTING", "must keep waiting for user 1"
        assert sql(f"select count(*) from ipl_team_rosters where team_id={t1['id']} and batting_order is not null") == "3", "partial picks survive"
        # deadline passes while 'the bot' is running → autofill keeps the 3 picks, completes and confirms
        sql(f"update ipl_tournament_teams set batting_order_deadline_at = now() - interval '1 second' where id={t1['id']}")
        h.restart()                                                              # and another restart for good measure
        await simulation_worker.tick(h.rt)
        await h.ctx.drain()
        assert sql(f"select batting_order_confirmed from ipl_tournament_teams where id={t1['id']}") == "t"
        got = sql(f"select player_id from ipl_team_rosters where team_id={t1['id']} order by batting_order").split("\n")
        assert got[:3] == first3 and len(set(got)) == 11 and sql(f"select count(distinct batting_order) from ipl_team_rosters where team_id={t1['id']}") == "11"
        # engine view after restart carries the confirmed order
        rows = await h.rt.repo.engine_squads(tid)
        sq = {int(r["team_id"]): Squad.from_rpc(r) for r in rows}
        assert sq[t1["id"]].ordered and [p.id for p in sq[t1["id"]].players] == got
        # whole tournament: every human innings uses exactly the confirmed order
        t = await h.run_to_end(tid)
        assert t["state"] == "COMPLETED"
        bad = sql(f"""select count(*) from ipl_batting_scorecards b join ipl_innings i on i.id=b.innings_id
                       join ipl_matches m on m.id=i.match_id
                       join ipl_team_rosters r on r.team_id=i.batting_team_id and r.player_id=b.player_id
                       join ipl_tournament_teams tm on tm.id=i.batting_team_id
                      where m.tournament_id={tid} and not i.is_super_over and tm.kind='HUMAN' and b.batting_pos <> r.batting_order""")
        assert bad == "0", f"{bad} scorecard rows deviate from the confirmed batting order"
        assert int(sql(f"""select count(*) from ipl_innings i join ipl_matches m on m.id=i.match_id
                           where m.tournament_id={tid} and i.batting_team_id={t1['id']}""")) >= 10   # 6 teams → 10 league matches each
    run(go())


def test_confirmed_batting_order_cannot_be_reopened_and_double_taps_are_safe():
    _need_db()

    async def go():
        h, tid, humans = await _started(2)
        await h.draft_all(humans, 3, tid)
        await h.set_batting_orders(tid, [1])
        t1 = await h.rt.repo.team_of_user(tid, 1)
        before = sql(f"select string_agg(player_id::text, ',' order by batting_order) from ipl_team_rosters where team_id={t1['id']}")
        dm = fakes.FakeChat(1, "private")
        for data in (f"ipl:bor:{t1['id']}", f"ipl:bou:{t1['id']}", f"ipl:bop:{t1['id']}:3", f"ipl:boc:{t1['id']}"):
            await h.click(data, 1, chat=dm)
        assert sql(f"select string_agg(player_id::text, ',' order by batting_order) from ipl_team_rosters where team_id={t1['id']}") == before
        # another manager cannot touch it
        q = await h.click(f"ipl:bor:{t1['id']}", 2, chat=fakes.FakeChat(2, "private"))
        assert q.answers and "not your team" in q.answers[-1][0].lower()
        # incomplete confirm is rejected
        t2 = await h.rt.repo.team_of_user(tid, 2)
        q = await h.click(f"ipl:boc:{t2['id']}", 2, chat=fakes.FakeChat(2, "private"))
        assert "Assign all 11" in q.answers[-1][0]
    run(go())
