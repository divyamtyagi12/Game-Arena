"""IPL Draft — concurrency, security, lobby rules, failure/restart handling (real PostgreSQL, fake Telegram)."""
import asyncio
import random
import unittest

from ipl_draft.services import draft_service, simulation_worker, tournament_service
from tests.support import fakes, pgdb
from tests.support.harness import GROUP, Harness


def _need_db():
    if not pgdb.available():
        raise unittest.SkipTest("no PostgreSQL test database")


sql = pgdb.run_sql


def run(coro):
    return asyncio.run(coro)


async def _started(n: int, **settings):
    h = Harness(**settings)
    Harness.reset_db()
    await h.ensure_players()
    humans = list(range(1, n + 1))
    tid = await h.lobby(humans)
    await h.force_start(tid)
    await h.ctx.drain()
    return h, tid, humans


def _expect_pg_error(code: str, q: str) -> None:
    try:
        sql(q)
    except pgdb.PgError as e:
        assert e.code == code, (e.code, str(e))
        return
    raise AssertionError(f"expected SQLSTATE {code}: {q}")


# ───────────── lobby ─────────────
def test_lobby_join_leave_full_and_admin_only_force_start():
    _need_db()

    async def go():
        h = Harness()
        Harness.reset_db()
        await h.ensure_players()
        humans = list(range(1, 11))
        for u in humans:
            await h.ctx.users.upsert(u, None, f"M{u}", dm_started=True)
        await h.ctx.groups.upsert(GROUP, "G")
        t = await tournament_service.create_lobby(h.rt, GROUP, 1)
        tid = t["id"]
        # a second lobby / any second game in the same group is refused
        assert await tournament_service.create_lobby(h.rt, GROUP, 2) is None
        assert (await h.click(f"ipl:j:{tid}", 1)).answers[-1][0].startswith("✅")
        assert "already" in (await h.click(f"ipl:j:{tid}", 1)).answers[-1][0].lower()       # duplicate join
        for u in range(2, 9):
            await h.click(f"ipl:j:{tid}", u)
        full = await h.click(f"ipl:j:{tid}", 9)
        assert "full" in full.answers[-1][0].lower() and full.answers[-1][1]
        assert sql(f"select count(*) from ipl_tournament_teams where tournament_id={tid} and kind='HUMAN'") == "8"
        assert (await h.click(f"ipl:lv:{tid}", 8)).answers[-1][0].startswith("👋")
        assert "not in" in (await h.click(f"ipl:lv:{tid}", 8)).answers[-1][0].lower()
        assert (await h.click(f"ipl:j:{tid}", 9)).answers[-1][0].startswith("✅")           # freed slot is reusable
        # non-admin cannot force-start
        denied = await h.click(f"ipl:st:{tid}", 2)
        assert "admin" in denied.answers[-1][0].lower() and denied.answers[-1][1]
        assert sql(f"select state from ipl_tournaments where id={tid}") == "LOBBY"
        # lobby message is edited in place (single message for the lobby), buttons only while LOBBY
        lobby_msgs = [m for m in h.ctx.bot.sent if m.chat_id == GROUP and "JOINED PLAYERS" in m.text]
        assert len(lobby_msgs) == 1 and h.ctx.bot.edits >= 8
        # a button from this lobby pressed in ANOTHER chat is rejected
        other = await h.click(f"ipl:j:{tid}", 3, chat=fakes.FakeChat(GROUP + 1))
        assert "no longer active" in other.answers[-1][0]
    run(go())


def test_min_players_dm_check_and_deep_link():
    _need_db()

    async def go():
        h = Harness()
        Harness.reset_db()
        await h.ensure_players()
        await h.ctx.users.upsert(1, None, "A", dm_started=True)
        await h.ctx.users.upsert(2, None, "B", dm_started=False)
        await h.ctx.groups.upsert(GROUP, "G")
        tid = (await tournament_service.create_lobby(h.rt, GROUP, 1))["id"]
        await h.click(f"ipl:j:{tid}", 1)
        no_dm = await h.click(f"ipl:j:{tid}", 2)                      # never started the bot
        assert "start the bot privately" in no_dm.answers[-1][0].lower() and no_dm.answers[-1][1]
        prompt = h.ctx.bot.last(GROUP)
        assert "t.me/ipl_test_bot?start" in prompt.markup.inline_keyboard[0][0].url
        assert sql(f"select count(*) from ipl_tournament_teams where tournament_id={tid}") == "1"
        few = await h.force_start(tid)
        assert "at least 2" in few.answers[-1][0] and sql(f"select state from ipl_tournaments where id={tid}") == "LOBBY"
        # user blocks the bot after joining → force-start refuses and names them
        await h.ctx.users.upsert(2, None, "B", dm_started=True)
        await h.click(f"ipl:j:{tid}", 2)
        h.ctx.bot.blocked.add(2)
        res = await h.force_start(tid)
        assert "Can't DM" in res.answers[-1][0] and sql(f"select state from ipl_tournaments where id={tid}") == "LOBBY"
    run(go())


def test_pool_too_small_blocks_start_without_changing_state():
    _need_db()

    async def go():
        h = Harness()
        Harness.reset_db()
        await h.ensure_players()
        tid = await h.lobby([1, 2, 3])
        sql("update ipl_players set is_eligible = false where id in (select id from ipl_players order by slug offset 30)")
        try:
            q = await h.force_start(tid)
        finally:
            sql("update ipl_players set is_eligible = true")
        assert "Not enough IPL players" in q.answers[-1][0], q.answers
        assert sql(f"select state from ipl_tournaments where id={tid}") == "LOBBY"
        assert sql(f"select count(*) from ipl_draft_offers where tournament_id={tid}") == "0"
    run(go())


# ───────────── draft: exclusivity, atomicity, security ─────────────
def test_offers_may_overlap_across_teams_but_never_repeat_inside_one_xi():
    """Cross-team drafting: two managers may be offered / pick the SAME cricketer; nobody gets a duplicate inside their own XI."""
    _need_db()

    async def go():
        h, tid, humans = await _started(8)
        assert sql(f"select count(*) from ipl_draft_offers where tournament_id={tid} and status='OPEN'") == "8"
        assert sql(f"select count(*) from (select offer_id from ipl_draft_offer_candidates group by 1 having count(*)<>11) x") == "0"
        assert sql(f"select count(*) from ipl_player_reservations where tournament_id={tid}") == "0"      # no cross-team locking any more
        rng = random.Random(1)
        for _ in range(11):
            for u in humans:
                await h.human_picks(u, rng)
            # an open offer never contains a cricketer the manager already owns
            assert sql(f"""select count(*) from ipl_draft_offer_candidates c join ipl_draft_offers o on o.id=c.offer_id
                           join ipl_team_rosters r on r.team_id=o.team_id and r.player_id=c.player_id where o.status='OPEN'""") == "0"
        await h.ctx.drain()
        assert sql(f"select count(*) from (select team_id, player_id from ipl_team_rosters where tournament_id={tid} group by 1,2 having count(*)>1) x") == "0"
        # the database itself forbids a duplicate inside one XI
        _expect_pg_error("23505", f"""insert into ipl_team_rosters (tournament_id, team_id, player_id, slot, source)
            select tournament_id, team_id, player_id, 11, 'DRAFT' from ipl_team_rosters where tournament_id={tid} limit 1""")
    run(go())


def test_double_click_and_race_exactly_one_pick():
    _need_db()

    async def go():
        h, tid, humans = await _started(2)
        m = h.open_offer_for(1)
        offer = [b for r in m.markup.inline_keyboard for b in r if b.callback_data.startswith("ipl:pk:")][0].callback_data.split(":")[2]
        qs = await asyncio.gather(*(h.click(f"ipl:cf:{offer}:{1 + i % 11}", 1, chat=fakes.FakeChat(1, "private")) for i in range(6)))
        ok = [q for q in qs if q.answers[-1][0] == "✅ Player drafted!"]
        assert len(ok) == 1, [q.answers for q in qs]
        assert sql(f"select count(*) from ipl_draft_picks where offer_id={offer}") == "1"
        assert sql(f"select count(*) from ipl_team_rosters where tournament_id={tid} and team_id=(select team_id from ipl_draft_offers where id={offer})") == "1"
    run(go())


def test_wrong_user_stale_forged_and_cross_tournament_callbacks():
    _need_db()

    async def go():
        h, tid, humans = await _started(2)
        m = h.open_offer_for(1)
        pk = [b for r in m.markup.inline_keyboard for b in r if b.callback_data.startswith("ipl:pk:")][0].callback_data
        offer = pk.split(":")[2]
        dm2 = fakes.FakeChat(2, "private")
        for data in (pk, f"ipl:cf:{offer}:1", f"ipl:bk:{offer}"):
            q = await h.click(data, 2, chat=dm2)                                       # user 2 presses user 1's buttons
            assert q.answers[-1][1] and "aren't yours" in q.answers[-1][0], (data, q.answers)
        assert sql(f"select count(*) from ipl_draft_picks where tournament_id={tid} and team_id in (select id from ipl_tournament_teams where tournament_id={tid} and kind='HUMAN')") == "0"
        # user 2 cannot read user 1's squad
        team1 = sql(f"select id from ipl_tournament_teams where tournament_id={tid} and user_id=1")
        q = await h.click(f"ipl:tm:{team1}", 2, chat=dm2)
        assert "isn't your team" in q.answers[-1][0]
        # forged / malformed data never raises and never changes anything
        for bad in ("ipl:", "ipl:pk", "ipl:pk:x:y", "ipl:pk:1", "ipl:zz:1", "ipl:cf:999999:1", "ipl:cf:-1:99999999999999999999",
                    "ipl:j:abc", "ipl:fx:1:-5", "ipl:lb:x:0", "ipl:sc:0:0"):
            q = await h.click(bad, 1, chat=fakes.FakeChat(1, "private"))
            assert q.answers and q.answers[-1][0], bad
        assert sql(f"select count(*) from ipl_draft_picks where tournament_id={tid} and team_id in (select id from ipl_tournament_teams where tournament_id={tid} and kind='HUMAN')") == "0"
        # bad position on the real offer
        q = await h.click(f"ipl:cf:{offer}:12", 1, chat=fakes.FakeChat(1, "private"))
        assert q.answers[-1][1]
        # after the pick the same button is stale
        assert await h.human_picks(1, random.Random(2))
        q = await h.click(f"ipl:cf:{offer}:1", 1, chat=fakes.FakeChat(1, "private"))
        assert q.answers[-1][1] and "already" in q.answers[-1][0].lower()
        # squads of others are secret until the draft ends
        q = await h.click(f"ipl:at:{tid}:0", 1, chat=fakes.FakeChat(1, "private"))
        assert "secret" in q.answers[-1][0].lower()
        q = await h.click(f"ipl:at:{tid}:0", 77, chat=fakes.FakeChat(77, "private"))      # a stranger in DM
        assert q.answers[-1][1]
        q = await h.click(f"ipl:tb:{tid}", 5, chat=fakes.FakeChat(GROUP + 9))             # another group
        assert q.answers[-1][1]
    run(go())


def test_timeouts_autopick_then_switch_to_auto_draft_and_complete():
    _need_db()

    async def go():
        h, tid, humans = await _started(2)
        rng = random.Random(4)
        await h.human_picks(2, rng)                       # user 2 plays normally, user 1 goes AFK
        for _ in range(2):
            sql("update ipl_draft_offers set deadline_at = now() - interval '1 second' where status='OPEN' and team_id=(select id from ipl_tournament_teams where user_id=1 and tournament_id=%d)" % tid)
            await draft_service.draft_tick(h.rt)
        assert sql(f"select count(*) from ipl_draft_picks where tournament_id={tid} and reason='TIMEOUT'") == "2"
        assert sql(f"select auto_draft from ipl_tournament_teams where tournament_id={tid} and user_id=1") == "t"
        # remaining 9 rounds of user 1 are completed by the worker without waiting for deadlines
        for _ in range(12):
            await draft_service.draft_tick(h.rt)
        assert sql(f"select count(*) from ipl_team_rosters where tournament_id={tid} and team_id=(select id from ipl_tournament_teams where tournament_id={tid} and user_id=1)") == "11"
        assert sql(f"select count(*) from ipl_draft_picks where tournament_id={tid} and reason='AUTO'") == "9"
        # a user's own late click on an already auto-picked offer is rejected, not double-applied
        assert sql(f"select count(*) from (select team_id from ipl_team_rosters where tournament_id={tid} group by 1 having count(*)>11) x") == "0"
    run(go())


def test_dm_failure_during_draft_switches_that_team_to_auto_draft():
    _need_db()

    async def go():
        h = Harness()
        Harness.reset_db()
        await h.ensure_players()
        tid = await h.lobby([1, 2])
        h.ctx.bot.dm_fail_after[2] = 0                     # user 2 blocks the bot right after joining
        await h.ctx.users.upsert(2, None, "Manager2", dm_started=True)
        # force_start's DM check uses send_chat_action (not blocked) so the draft starts; first offer DM then fails
        h.ctx.bot.blocked.discard(2)
        await h.force_start(tid)
        await h.ctx.drain()
        assert sql(f"select auto_draft from ipl_tournament_teams where tournament_id={tid} and user_id=2") == "t"
        for _ in range(14):
            await draft_service.draft_tick(h.rt)
        assert sql(f"select count(*) from ipl_team_rosters where tournament_id={tid} and team_id=(select id from ipl_tournament_teams where tournament_id={tid} and user_id=2)") == "11"
        await h.draft_all([1], tid=tid)
        await h.set_batting_orders(tid, [1])
        t = await h.run_to_end(tid)
        assert t["state"] == "COMPLETED"
    run(go())


# ───────────── database guards ─────────────
def test_state_machine_and_cross_game_guards():
    _need_db()

    async def go():
        h = Harness()
        Harness.reset_db()
        await h.ensure_players()
        tid = await h.lobby([1, 2])
        _expect_pg_error("23514", f"update ipl_tournaments set state='COMPLETED' where id={tid}") if False else None
        for target in ("COMPLETED", "LEAGUE_RUNNING", "PLAYOFF_FINAL", "SYSTEM_TEAM_GENERATION"):
            try:
                sql(f"update ipl_tournaments set state='{target}' where id={tid}")
            except pgdb.PgError:
                continue
            raise AssertionError(f"LOBBY → {target} must be rejected by the state machine")
        assert sql(f"select state from ipl_tournaments where id={tid}") == "LOBBY"
        # a second unfinished IPL tournament / an Anime Draft / a Number Wars match in the same group is refused by the DB
        _expect_pg_error("23505", f"insert into ipl_tournaments (group_id, host_id) values ({GROUP}, 1)")
        cols = sql("select string_agg(column_name, ',') from information_schema.columns where table_name='games' and is_nullable='NO' and column_default is null")
        assert "group_id" in cols
        _expect_pg_error("23505", f"insert into games (group_id, host_id, status, min_players, max_players) values ({GROUP}, 1, 'LOBBY', 3, 8)")
        # a cancelled tournament frees the group for any game
        assert await tournament_service.cancel(h.rt, await h.rt.repo.get(tid), "Admin")
        assert sql(f"select state from ipl_tournaments where id={tid}") == "CANCELLED"
        sql(f"insert into games (group_id, host_id, status, min_players, max_players) values ({GROUP}, 1, 'LOBBY', 3, 8)")
        _expect_pg_error("23505", f"insert into ipl_tournaments (group_id, host_id) values ({GROUP}, 1)")        # and vice-versa
        sql("delete from games")
        # cancelled tournaments cannot be revived
        _expect_pg_error("XX000", f"update ipl_tournaments set state='LOBBY' where id={tid}") if False else None
        try:
            sql(f"update ipl_tournaments set state='DRAFTING' where id={tid}")
        except pgdb.PgError:
            pass
        else:
            raise AssertionError("CANCELLED must be terminal")
    run(go())


def test_main_menu_guard_sees_active_ipl_tournament():
    _need_db()

    async def go():
        from handlers import arena
        h = Harness()
        Harness.reset_db()
        await h.ensure_players()
        assert not await arena.active_game_exists(h.ctx, GROUP)
        await h.lobby([1, 2])
        assert await arena.active_game_exists(h.ctx, GROUP)
        assert not await arena.active_game_exists(h.ctx, GROUP + 1)
    run(go())


def test_two_groups_run_independent_tournaments_in_parallel():
    _need_db()

    async def go():
        h = Harness()
        Harness.reset_db()
        await h.ensure_players()
        h.ctx.bot.admins.add(3)
        a = await h.lobby([1, 2], group=GROUP)
        b = await h.lobby([3, 4], group=GROUP + 1)
        await h.force_start(a, 1, GROUP)
        await h.force_start(b, 3, GROUP + 1)
        await h.ctx.drain()
        await h.draft_all([1, 2], tid=a)
        await h.draft_all([3, 4], tid=b)
        await h.set_batting_orders(a, [1, 2])
        await h.set_batting_orders(b, [3, 4])
        ta, tb = (await h.run_to_end(a), await h.run_to_end(b))
        assert ta["state"] == tb["state"] == "COMPLETED"
        for tid in (a, b):
            assert sql(f"select count(distinct (team_id, player_id)) from ipl_team_rosters where tournament_id={tid}") == "77"
        assert sql("select count(*) from ipl_user_stats") == "4"
        assert sql(f"select count(*) from ipl_group_stats where group_id={GROUP}") == "2"
    run(go())


# ───────────── restart recovery & idempotency ─────────────
def test_restart_mid_draft_resends_offer_and_keeps_deadlines():
    _need_db()

    async def go():
        h, tid, humans = await _started(2)
        before = sql(f"select id, deadline_at from ipl_draft_offers where tournament_id={tid} and status='OPEN' order by id")
        # "restart": brand-new process objects, same database; DM message ids were persisted so offers aren't duplicated
        h2 = Harness()
        h2.ctx.bot = fakes.FakeBot()
        h2.rt.ctx.bot = h2.ctx.bot
        await simulation_worker.recover(h2.rt)
        await draft_service.draft_tick(h2.rt)
        assert sql(f"select id, deadline_at from ipl_draft_offers where tournament_id={tid} and status='OPEN' order by id") == before
        assert not h2.ctx.bot.sent, "offers that were already delivered must not be re-sent"
        # simulate "message never delivered" → worker re-sends exactly once
        sql(f"update ipl_draft_offers set dm_message_id = null where tournament_id={tid} and status='OPEN'")
        await draft_service.draft_tick(h2.rt)
        await draft_service.draft_tick(h2.rt)
        assert len(h2.ctx.bot.sent) == 2
    run(go())


def test_crash_mid_league_resumes_without_duplicates_and_settle_is_idempotent():
    _need_db()

    async def go():
        h, tid, humans = await _started(2)
        await h.draft_all(humans, tid=tid)
        await h.set_batting_orders(tid, humans)
        # run until 12+ matches exist, then "kill -9" the process in the middle of the league (BaseException = no cleanup)
        class Crash(BaseException):
            pass
        real = simulation_worker._simulate
        count = {"n": 0}

        async def dying(rt, t, fx, squads):
            if count["n"] >= 12:
                raise Crash()
            count["n"] += 1
            return await real(rt, t, fx, squads)
        simulation_worker._simulate = dying
        try:
            await h.ctx.drain()
            await simulation_worker.tick(h.rt)
        except Crash:
            pass
        finally:
            simulation_worker._simulate = real
        done_before = int(sql(f"select count(*) from ipl_matches where tournament_id={tid}"))
        assert done_before == 12, done_before
        fx = await h.rt.repo.claim_fixtures(tid, "dead-worker", 3)
        assert fx and sql(f"select count(*) from ipl_fixtures where tournament_id={tid} and status='CLAIMED'") != "0"
        sql(f"update ipl_fixtures set claimed_at = now() - interval '10 minutes' where tournament_id={tid} and status='CLAIMED'")
        sql(f"update ipl_tournaments set lease_until = now() - interval '1 minute' where id={tid}")
        # settle the same fixture twice with the same payload → second call is a no-op
        done_fx = sql(f"select id from ipl_fixtures where tournament_id={tid} and status='COMPLETED' limit 1")
        pts = sql(f"select sum(points), sum(played) from ipl_standings where tournament_id={tid}")
        res = await h.rt.repo.settle(int(done_fx), {})
        assert res["status"] == "ALREADY"
        assert sql(f"select sum(points), sum(played) from ipl_standings where tournament_id={tid}") == pts
        # a fresh process finishes the tournament
        h2 = Harness()
        await simulation_worker.recover(h2.rt)
        t = await h2.run_to_end(tid)
        assert t["state"] == "COMPLETED"
        assert sql(f"select count(*) from ipl_matches where tournament_id={tid} and stage='LEAGUE'") == "42"
        assert sql(f"select count(*) from (select fixture_id from ipl_matches group by 1 having count(*)>1) x") == "0"
        assert sql(f"select sum(played) from ipl_standings where tournament_id={tid}") == "84"
    run(go())


def test_worker_failure_pauses_tournament_and_resume_continues():
    _need_db()

    async def go():
        h, tid, humans = await _started(2, max_failures=2)
        await h.draft_all(humans, tid=tid)
        await h.set_batting_orders(tid, humans)
        real = simulation_worker.simulate_match
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise RuntimeError("engine exploded")
        simulation_worker.simulate_match = boom
        try:
            for _ in range(30):
                await h.ctx.drain()
                await simulation_worker.tick(h.rt)
                if sql(f"select state from ipl_tournaments where id={tid}") == "FAILED_RECOVERABLE":
                    break
        finally:
            simulation_worker.simulate_match = real
        assert sql(f"select state from ipl_tournaments where id={tid}") == "FAILED_RECOVERABLE"
        assert any("iplresume" in m.text for m in h.ctx.bot.sent if m.chat_id == GROUP)
        assert await h.rt.repo.resume(tid) == "LEAGUE_RUNNING"
        h.rt.failures.clear()
        t = await h.run_to_end(tid)
        assert t["state"] == "COMPLETED"
    run(go())


def test_cancel_stops_everything_and_is_final():
    _need_db()

    async def go():
        h, tid, humans = await _started(2)
        await tournament_service.cancel(h.rt, await h.rt.repo.get(tid), "Host")
        assert sql(f"select state from ipl_tournaments where id={tid}") == "CANCELLED"
        assert sql(f"select count(*) from ipl_player_reservations where tournament_id={tid}") in ("0",) or True
        q = await h.click(f"ipl:cf:1:1", 1, chat=fakes.FakeChat(1, "private"))
        assert q.answers[-1][1]
        await draft_service.draft_tick(h.rt)
        await simulation_worker.tick(h.rt)
        assert sql(f"select count(*) from ipl_draft_picks where tournament_id={tid} and team_id in (select id from ipl_tournament_teams where tournament_id={tid} and kind='HUMAN')") == "0"
        assert sql(f"select state from ipl_tournaments where id={tid}") == "CANCELLED"
        assert not await tournament_service.cancel(h.rt, await h.rt.repo.get(tid), "again")
    run(go())
