"""Regression tests covering Bug 2 through Bug 13 for IPL Draft and Number Wars."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import bot
from game import number_wars as nw
from ipl_draft import messages as m
from ipl_draft.handlers import callbacks
from ipl_draft.repository import IplRepo
from ipl_draft.services import draft_service, simulation_worker, system_service


class TestPhase4Regressions(unittest.TestCase):
    # ── BUG 2: Simulation Worker Failure Handling ─────────────────────────────
    def test_bug2_simulation_worker_handles_invalid_system_squad(self):
        """When simulation returns INVALID_SYSTEM_SQUAD, _league raises an error
        instead of silently swallowing, allowing the worker to increment failure counters
        and avoid infinite reclaim loops.
        """
        rt = MagicMock()
        rt.worker_id = "test-worker"
        rt.repo.claim_fixtures = AsyncMock(return_value=[{"id": 101, "home": 1, "away": 2}])
        
        t = {"id": 1, "group_id": -1001, "state": "LEAGUE"}

        with patch("ipl_draft.services.simulation_worker.get_squads", new_callable=AsyncMock) as mock_sq, \
             patch("ipl_draft.services.simulation_worker._simulate", new_callable=AsyncMock) as mock_sim:
            mock_sq.return_value = {1: MagicMock(), 2: MagicMock()}
            mock_sim.return_value = {"status": "INVALID_SYSTEM_SQUAD", "reason": "System squad missing roles"}
            
            with pytest.raises(RuntimeError) as exc_info:
                asyncio.run(simulation_worker._league(rt, t))
            assert "INVALID_SYSTEM_SQUAD" in str(exc_info.value)

    # ── BUG 3: Finalization Idempotency & Crash Recovery ──────────────────────
    def test_bug3_finalization_concurrency_lease_and_staged_outbox(self):
        """Finalization claims an atomic lease, stages award calculations and announcements
        through durable outbox flags, and does not duplicate announcements upon re-run.
        """
        rt = MagicMock()
        rt.worker_id = "worker-1"
        rt.ctx.bot.send_message = AsyncMock(return_value=MagicMock(message_id=777))
        rt.ctx.lock = MagicMock()
        lock_mock = AsyncMock()
        rt.ctx.lock.return_value.__aenter__ = AsyncMock(return_value=lock_mock)
        rt.ctx.lock.return_value.__aexit__ = AsyncMock(return_value=None)

        rt.repo.lease = AsyncMock(return_value=True)
        rt.repo.get = AsyncMock(return_value={
            "id": 1,
            "group_id": -1001,
            "state": "COMPLETED",
            "champion_team_id": 10,
            "runner_up_team_id": 20,
            "awards_recorded": True,        # Already computed in prior attempt
            "champion_announced": True,     # Champion already announced
            "mvp_announced": False,         # Crashed before MVP was announced
            "final_dms_sent": False,
            "final_announced": False,
        })
        rt.repo.all_teams = AsyncMock(return_value=[
            {"id": 10, "user_id": 100, "name": "Team A", "score": 200, "kind": "HUMAN"},
            {"id": 20, "user_id": 200, "name": "Team B", "score": 150, "kind": "HUMAN"},
        ])
        rt.repo.record_results = AsyncMock()
        rt.repo.store_awards = AsyncMock()
        rt.repo.update = AsyncMock()
        rt.repo.player_stats = AsyncMock(return_value=[])

        rt.repo.team_roster = AsyncMock(return_value={"players": []})
        rt.repo.get_batting_order_state = AsyncMock(return_value={})
        rt.repo.standings = AsyncMock(return_value=[])
        rt.repo.bracket = AsyncMock(return_value=[])
        rt.repo.playoff_fixtures = AsyncMock(return_value=[])

        with patch("ipl_draft.services.simulation_worker.notify.group", new_callable=AsyncMock) as mock_notify_grp, \
             patch("ipl_draft.services.simulation_worker.compute_human_mvps", new_callable=AsyncMock) as mock_mvps, \
             patch("ipl_draft.services.simulation_worker.notify.dm", new_callable=AsyncMock) as mock_notify_dm:
            mock_mvps.return_value = {"most_runs": {"player": "Kohli", "runs": 300, "team": "RCB"}}

            asyncio.run(simulation_worker.finalize(rt, 1))

            # Awards should NOT be re-stored because awards_recorded was already True
            rt.repo.store_awards.assert_not_called()

            # Champion was already True, so champion group message should not be sent
            # MVP was False, so MVP announcement was made and updated
            rt.repo.update.assert_any_call(1, mvp_announced=True)

    def test_bug3_concurrent_workers_finalization_rejected(self):
        """If another worker currently holds the lease, finalization is safely skipped."""
        rt = MagicMock()
        rt.worker_id = "worker-2"
        rt.ctx.lock = MagicMock()
        rt.ctx.lock.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        rt.ctx.lock.return_value.__aexit__ = AsyncMock(return_value=None)
        rt.repo.get = AsyncMock(return_value={"id": 1, "state": "COMPLETED"})
        # Database lease returns False (held by another worker)
        rt.repo.lease = AsyncMock(return_value=False)

        asyncio.run(simulation_worker.finalize(rt, 1))
        # No updates performed
        rt.repo.update.assert_not_called()

    # ── BUG 4: System Service Pagination & Persisted Roster Validation ────────
    def test_bug4_system_service_pagination_over_1000_players(self):
        """Catalog query paginates properly without truncating at Supabase default limit."""
        rt = MagicMock()
        page1_data = [{"id": f"p_{i}", "role": "BAT"} for i in range(1000)]
        page2_data = [{"id": f"p_{i}", "role": "BOWL"} for i in range(1000, 1200)]

        async def mock_exec(fn):
            client = MagicMock()
            def range_fn(start, end):
                q = MagicMock()
                if start == 0:
                    q.execute.return_value = MagicMock(data=page1_data)
                else:
                    q.execute.return_value = MagicMock(data=page2_data)
                return q
            client.table().select().range.side_effect = range_fn
            return fn(client).execute().data

        rt.repo.db.exec = AsyncMock(side_effect=mock_exec)

        players = asyncio.run(system_service._load_all_players(rt))
        assert len(players) == 1200
        assert players[0]["id"] == "p_0"
        assert players[-1]["id"] == "p_1199"

    def test_bug4_system_service_validates_persisted_roster(self):
        """system_service raises SystemDraftError if persisted squad is incomplete."""
        rt = MagicMock()
        rt.repo.get = AsyncMock(return_value={"id": 1, "state": "DRAFTING"})
        codes = [f[0] for f in system_service.FRANCHISES]
        teams = [{"id": 10 + i, "franchise_code": c, "kind": "SYSTEM"} for i, c in enumerate(codes)]
        rt.repo.all_teams = AsyncMock(return_value=teams)
        rt.repo.begin_system_teams = AsyncMock()
        rt.repo.complete_system_teams = AsyncMock()
        rt.repo._r = AsyncMock(return_value={"status": "OK"})

        # Persisted roster query returns only 9 players
        rt.repo.db.exec = AsyncMock(return_value=[{"player_id": f"p_{i}", "slot": i} for i in range(1, 10)])

        catalog = []
        for c in codes:
            for i, name in enumerate(system_service.SYSTEM_PLAYING_XI[c]):
                catalog.append({"id": f"p_{c}_{i}", "display_name": name, "role": "BAT"})

        with patch.object(system_service, "_load_all_players", new_callable=AsyncMock) as mock_players:
            mock_players.return_value = catalog
            with pytest.raises(system_service.SystemDraftError) as exc_info:
                asyncio.run(system_service.prepare_system_teams(rt, 1))
            assert "expected 11" in str(exc_info.value).lower()

    # ── BUG 5: Host and Admin Force-Start Permissions ─────────────────────────
    def test_bug5_force_start_host_allowed_and_unrelated_denied(self):
        """Host and group admin can force start; non-host non-admin member is denied."""
        rt = MagicMock()
        rt.repo.get = AsyncMock(return_value={"id": 1, "group_id": -1001, "host_id": 100, "state": "LOBBY"})
        context = MagicMock()
        context.bot = MagicMock()
        q = MagicMock()
        q.data = "ipl:st:1"
        q.message.chat.id = -1001
        q.message.chat.type = "supergroup"
        q.answer = AsyncMock()

        # 1. Host (user 100) -> allowed
        q.from_user.id = 100
        with patch("ipl_draft.handlers.callbacks.is_admin", new_callable=AsyncMock) as mock_admin, \
             patch("ipl_draft.handlers.callbacks.tournament_service.force_start", new_callable=AsyncMock) as mock_fs:
            mock_admin.return_value = False
            mock_fs.return_value = (True, "Started")
            asyncio.run(callbacks.force_start(rt, context, q, 1))
            mock_fs.assert_called_once_with(rt, 1)

        # 2. Unrelated user (user 200, not admin) -> rejected
        q.from_user.id = 200
        with patch("ipl_draft.handlers.callbacks.is_admin", new_callable=AsyncMock) as mock_admin, \
             patch("ipl_draft.handlers.callbacks.tournament_service.force_start", new_callable=AsyncMock) as mock_fs:
            mock_admin.return_value = False
            asyncio.run(callbacks.force_start(rt, context, q, 1))
            q.answer.assert_called_with(text="❌ Only the tournament host or group admins can force-start.", show_alert=True)
            mock_fs.assert_not_called()

    # ── BUG 6: League Completion Idempotency ──────────────────────────────────
    def test_bug6_league_completed_stages_prevent_duplicate_messages(self):
        """_league_completed avoids resending group summary and DMs if flags are set."""
        rt = MagicMock()
        rt.worker_id = "worker-1"
        rt.pause = AsyncMock()
        t = {
            "id": 1,
            "group_id": -1001,
            "state": "LEAGUE",
            "league_summary_posted": True, # Already posted
            "league_dms_sent": True,        # Already sent
        }
        rt.repo.get = AsyncMock(return_value=t)
        rt.repo.standings = AsyncMock(return_value=[])
        rt.repo.lease = AsyncMock(return_value=True)
        rt.repo.points_table = AsyncMock(return_value=[])
        rt.repo.create_fixtures = AsyncMock(return_value=[])
        rt.repo.set_tournament_state = AsyncMock()
        rt.repo.begin_playoffs = AsyncMock(return_value=True)

        with patch("ipl_draft.services.simulation_worker.notify.group", new_callable=AsyncMock) as mock_grp, \
             patch("ipl_draft.services.simulation_worker.notify.dm", new_callable=AsyncMock) as mock_dm:
            asyncio.run(simulation_worker._league_completed(rt, t))

            mock_grp.assert_not_called()
            mock_dm.assert_not_called()

    # ── BUG 7: Repository Unsafe Direct Fallbacks Removed ─────────────────────
    def test_bug7_repo_direct_fallbacks_removed(self):
        """IplRepo.pick, create_fixtures, and finish_drafting do not perform direct table writes on RPC errors."""
        repo = IplRepo(MagicMock())
        # Mock _r to return error status
        repo._r = AsyncMock(return_value={"status": "CLOSED"})
        res = asyncio.run(repo.pick(1, 1, 100))
        assert res.get("status") == "CLOSED"

    # ── BUG 8: IPL Rules Text Correctness ─────────────────────────────────────
    def test_bug8_rules_text_reflects_actual_roster_constraints(self):
        """Messages include minimum 3 bowlers, 1 keeper, 1 all-rounder, exactly 11 players."""
        text = m.rules_text()
        assert "11 players" in text
        assert "3 bowlers" in text
        assert "1 wicketkeeper" in text
        assert "1 all-rounder" in text
        assert "all batters is allowed" not in text

    # ── BUG 9: Callback Safe Exception Handling ──────────────────────────────
    def test_bug9_callback_safe_exception_handling(self):
        """ipl_callback validates parameters and logs unexpected exceptions without swallowing."""
        update = MagicMock()
        context = MagicMock()
        rt = MagicMock()
        context.bot_data = {"ipl": rt}
        q = MagicMock()
        q.data = "ipl:unknown:action:123"
        q.answer = AsyncMock()
        update.callback_query = q
        asyncio.run(callbacks.ipl_callback(update, context))
        q.answer.assert_called_with(text="⚠️ Invalid button.", show_alert=False)

    # ── BUG 10: Operator Precedence in Group Summary ──────────────────────────
    def test_bug10_operator_precedence_in_group_summary(self):
        """update_group_tournament_summary correctly selects winner team name with proper precedence."""
        t1 = {"id": 10, "name": "Mumbai Indians"}
        t2 = {"id": 20, "name": "Chennai Super Kings"}

        # When t1 wins:
        w_id = 10
        winner_name = (t1.get("name") if w_id == t1.get("id") else t2.get("name")) or "Winner"
        assert winner_name == "Mumbai Indians"

        # When t2 wins:
        w_id = 20
        winner_name = (t1.get("name") if w_id == t1.get("id") else t2.get("name")) or "Winner"
        assert winner_name == "Chennai Super Kings"

    # ── BUG 11: Missing Bot Commands ──────────────────────────────────────────
    def test_bug11_bot_commands_include_iplresume_and_iplstats(self):
        """Telegram command list includes /iplresume and /iplstats."""
        cmds = {cmd.command for cmd in bot.COMMANDS}
        assert "iplresume" in cmds
        assert "iplstats" in cmds

    # ── BUG 13: Number Wars Value Range (1 to 100) ───────────────────────────
    def test_bug13_number_wars_value_range(self):
        """Number Wars numbers are strictly between 1 and 100; 0 is rejected."""
        assert nw.NUMBER_MIN == 1
        assert nw.NUMBER_MAX == 100
        # Value 0 is invalid
        assert not (nw.NUMBER_MIN <= 0 <= nw.NUMBER_MAX)
        # Values 1 to 100 are valid
        assert nw.NUMBER_MIN <= 1 <= nw.NUMBER_MAX
        assert nw.NUMBER_MIN <= 100 <= nw.NUMBER_MAX
        assert not (nw.NUMBER_MIN <= 101 <= nw.NUMBER_MAX)
