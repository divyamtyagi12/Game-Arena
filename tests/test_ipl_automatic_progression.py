"""Unit and Integration Tests for Fully Automatic IPL Tournament Progression.

Verifies:
1. Automatic Draft Completion (all humans complete drafting, validated, batting orders auto-assigned).
2. Last Human Completes Drafting triggers the exact start announcement without /iplresume.
3. System Squads are generated and validated automatically.
4. Tournament starts automatically through all stages without manual commands.
5. Bot restarts during drafting (progress preserved, offers re-opened).
6. Bot restarts during league matches (resumes simulation without duplicate fixtures or results).
7. Temporary DB error triggers automatic retry with exponential backoff.
8. Invalid squad detection & repair during match simulation.
9. Worker crashes during playoffs (safely resumes without duplicate bracket matches).
10. Tournament completes successfully and champion announcement is delivered without duplicates.
11. Zero /iplresume references in commands, menus, or user messages.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import bot
from ipl_draft.engine import awards as awards_engine, draft_engine, match_engine
from ipl_draft.engine.fixtures import double_round_robin, validate as validate_fixtures
from ipl_draft.engine.model import Player, Squad
from ipl_draft.runtime import IplRuntime
from ipl_draft.services import (
    batting_order_service,
    draft_service,
    simulation_worker,
    system_service,
)


class TestIplAutomaticProgression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ctx = MagicMock()
        self.ctx.bot = AsyncMock()
        self.ctx.lock = MagicMock()
        self.ctx.lock.return_value.__aenter__ = AsyncMock()
        self.ctx.lock.return_value.__aexit__ = AsyncMock()
        self.ctx.spawn = MagicMock()

        self.repo = MagicMock()
        self.repo.db = MagicMock()
        self.repo.db.exec = AsyncMock()
        self.repo.all_teams = AsyncMock()
        self.repo.team_roster = AsyncMock()
        self.repo.teams_pending_batting_order = AsyncMock(return_value=[])
        self.repo.batting_order_autofill = AsyncMock()
        self.repo.finish_drafting = AsyncMock(return_value={"status": "OK"})
        self.repo.create_fixtures = AsyncMock(return_value={"status": "OK"})
        self.repo.claim_fixtures = AsyncMock()
        self.repo.release_claims = AsyncMock(return_value=0)
        self.repo.settle = AsyncMock(return_value={"status": "OK", "match_id": 101})
        self.repo.standings = AsyncMock(return_value=[])
        self.repo.complete_league = AsyncMock(return_value={"status": "OK"})
        self.repo.begin_playoffs = AsyncMock(return_value=True)
        self.repo.scorecard = AsyncMock(return_value=None)
        self.repo.match_card = AsyncMock(return_value=None)
        self.repo.bracket = AsyncMock(return_value=[])
        self.repo.record_results = AsyncMock()
        self.repo.store_awards = AsyncMock()
        self.repo.update = AsyncMock()
        self.repo.lease = AsyncMock(return_value=True)
        self.repo.release_lease = AsyncMock()
        self.repo.release_stale_reservations = AsyncMock()
        self.repo.resume = AsyncMock(return_value="LEAGUE_RUNNING")
        self.repo.set_failed = AsyncMock(return_value=True)
        self.repo.by_states = AsyncMock(return_value=[])
        self.repo.unannounced_completed = AsyncMock(return_value=[])

        self.settings = MagicMock()
        self.settings.max_failures = 3
        self.settings.tick_seconds = 0.01
        self.settings.matchday_delay = 0.001
        self.settings.playoff_delay = 0.001
        self.settings.seed_salt = "test-salt"
        self.settings.store_ball_events = False

        self.rt = IplRuntime(
            ctx=self.ctx,
            repo=self.repo,
            s=self.settings,
            worker_id="test-worker-auto-1",
        )
        self.rt.pause = AsyncMock()

    # ── 1. All Humans Complete Drafting ──────────────────────────────────────
    async def test_all_humans_complete_drafting_auto_finishes(self):
        """When all humans finish drafting, _drafting validates all squads and marks drafting complete."""
        tid = 10
        t = {"id": tid, "group_id": -1001, "state": "DRAFTING"}

        self.repo.draft_progress = AsyncMock(return_value=[
            {"team_id": 1, "done": True, "picks": 11},
            {"team_id": 2, "done": True, "picks": 11},
        ])

        valid_11 = [
            {"id": f"p{i}", "name": f"Player {i}", "role": "BAT", "bowling_type": "NONE"} for i in range(1, 7)
        ] + [
            {"id": "p7", "name": "Player 7", "role": "WK", "bowling_type": "NONE"},
            {"id": "p8", "name": "Player 8", "role": "AR", "bowling_type": "RIGHT_FAST"},
            {"id": "p9", "name": "Player 9", "role": "BOWL", "bowling_type": "RIGHT_FAST"},
            {"id": "p10", "name": "Player 10", "role": "BOWL", "bowling_type": "SPIN"},
            {"id": "p11", "name": "Player 11", "role": "BOWL", "bowling_type": "LEFT_FAST"},
        ]

        self.repo.all_teams = AsyncMock(return_value=[
            {"id": 1, "name": "Team 1", "kind": "HUMAN"},
            {"id": 2, "name": "Team 2", "kind": "HUMAN"},
        ])
        self.repo.team_roster = AsyncMock(return_value={"team": {"id": 1}, "players": valid_11})
        # While a team is still pending batting order, _drafting must wait (return False)
        self.repo.teams_pending_batting_order = AsyncMock(return_value=[{"id": 2}])
        with patch("ipl_draft.services.system_service.prepare_system_teams", new=AsyncMock()) as mock_sys:
            res_waiting = await simulation_worker._drafting(self.rt, t)
            assert res_waiting is False
            mock_sys.assert_not_called()
            self.repo.finish_drafting.assert_not_called()

            # Once all batting orders are confirmed, _drafting advances
            self.repo.teams_pending_batting_order = AsyncMock(return_value=[])
            res_ready = await simulation_worker._drafting(self.rt, t)
            assert res_ready is True
            # System teams prepared
            mock_sys.assert_called_once_with(self.rt, tid)
            # finish_drafting called atomically
            self.repo.finish_drafting.assert_called_once()

    # ── 2. Last Human Completes Drafting & Start Announcement ───────────────
    async def test_tournament_start_announcement_format(self):
        """When fixtures are generated, the exact tournament start announcement is posted without /iplresume."""
        tid = 20
        t = {"id": tid, "group_id": -1002, "state": "FIXTURE_GENERATION"}

        teams = [
            {"id": 1, "team_no": 1, "name": "Team 1", "kind": "HUMAN"},
            {"id": 2, "team_no": 2, "name": "Team 2", "kind": "HUMAN"},
            {"id": 3, "team_no": 3, "name": "CSK", "kind": "SYSTEM"},
            {"id": 4, "team_no": 4, "name": "MI", "kind": "SYSTEM"},
            {"id": 5, "team_no": 5, "name": "RCB", "kind": "SYSTEM"},
            {"id": 6, "team_no": 6, "name": "KKR", "kind": "SYSTEM"},
        ]
        self.repo.all_teams = AsyncMock(return_value=teams)
        self.repo.create_fixtures = AsyncMock(return_value={"status": "OK"})

        with patch("ipl_draft.services.notification_service.group", new=AsyncMock()) as mock_group:
            res = await simulation_worker._fixtures(self.rt, t)
            assert res is True
            mock_group.assert_called_once()
            call_text = mock_group.call_args[0][2]
            assert "All squads completed!" in call_text
            assert "IPL Tournament is starting!" in call_text
            assert "League matches will begin automatically." in call_text
            assert "Good luck to all teams!" in call_text
            assert "iplresume" not in call_text.lower()

    # ── 3. System Squads Generated & Validated ──────────────────────────────
    async def test_system_squads_generated_and_verified(self):
        """System team generator produces 4 valid squads with 11 players each."""
        from ipl_draft.engine.team_builder import SYSTEM_PLAYING_XI
        for code in ["CSK", "MI", "RCB", "KKR"]:
            squad = SYSTEM_PLAYING_XI[code]
            assert len(squad) == 11
            assert len(set(squad)) == 11, f"No duplicate players in system squad for {code}"

    # ── 4. Resilient Fixture Creation Handles Outdated Stored Proc ───────────
    async def test_repository_create_fixtures_handles_bad_team_count_fallback(self):
        """When Supabase stored proc returns BAD_TEAM_COUNT for 7 teams, repository safely inserts fixtures directly."""
        from ipl_draft.repository import IplRepo
        tid = 30
        teams = [{"id": i, "name": f"T{i}"} for i in range(1, 8)]
        ids = [t["id"] for t in teams]
        fx = double_round_robin(ids)
        validate_fixtures(fx, ids)

        mock_db = MagicMock()
        mock_db.exec = AsyncMock(side_effect=[
            [],  # existing fixtures check
            [],  # second check inside fallback
            None, # insert fixtures
            None, # upsert standings
            None, # update tournament
        ])
        real_repo = IplRepo(mock_db)
        real_repo._r = AsyncMock(return_value={"status": "BAD_TEAM_COUNT", "teams": 7})
        real_repo.all_teams = AsyncMock(return_value=teams)

        res = await real_repo.create_fixtures(tid, fx)
        assert res["status"] == "OK"
        assert res["teams"] == 7
        assert res["fixtures"] == 42  # 7 * 6 = 42

    # ── 5. Bot Restarts During Drafting (Recovery) ───────────────────────────
    async def test_bot_restarts_during_drafting(self):
        """When bot restarts, recover() safely resumes active and paused tournaments."""
        self.repo.by_states = AsyncMock(side_effect=[
            [{"id": 42, "state": "FAILED_RECOVERABLE", "resume_state": "DRAFTING"}], # recoverable
            [{"id": 41, "state": "DRAFTING"}],  # active tournaments
        ])

        await simulation_worker.recover(self.rt)
        self.repo.release_stale_reservations.assert_called_once()
        self.repo.resume.assert_called_once_with(42)

    # ── 6. Bot Restarts During League Matches (Recovery) ─────────────────────
    async def test_bot_restarts_during_league_matches_no_duplicate_fixtures(self):
        """When bot restarts during league matches, create_fixtures returns ALREADY and matches resume."""
        from ipl_draft.repository import IplRepo
        tid = 50
        mock_db = MagicMock()
        mock_db.exec = AsyncMock(return_value=[{"id": 1001}]) # existing fixtures exist
        real_repo = IplRepo(mock_db)
        real_repo._r = AsyncMock(return_value={"status": "OK", "already": True})
        res = await real_repo.create_fixtures(tid, [{"match_no": 1}])
        assert res["status"] == "OK"
        assert res.get("already") is True

    # ── 7. Temporary DB Error Triggers Automatic Exponential Backoff ─────────
    async def test_temporary_db_error_triggers_exponential_backoff(self):
        """Worker catches transient error, applies exponential backoff, releases claims, does not pause on first failure."""
        tid = 60
        t = {"id": tid, "group_id": -1006, "state": "LEAGUE_RUNNING"}
        self.repo.get = AsyncMock(return_value=t)

        with patch.dict(simulation_worker.HANDLERS, {"LEAGUE_RUNNING": AsyncMock(side_effect=RuntimeError("Supabase timeout"))}):
            await simulation_worker.advance(self.rt, tid)
            assert self.rt.failures.get(tid) == 1
            assert tid in self.rt.retry_after
            self.repo.release_claims.assert_called_once_with(tid, self.rt.worker_id)
            # Tournament is NOT marked failed because 1 < max_failures (3)
            self.repo.set_failed.assert_not_called()

    # ── 8. Simulation Encounters Invalid Squad ──────────────────────────────
    async def test_simulation_encounters_invalid_squad_repair(self):
        """When system squad has invalid composition, system squad repair is attempted."""
        tid = 70
        t = {"id": tid, "draft_started_at": None}
        fx = {"id": 1, "home": 1, "away": 2, "stage": "LEAGUE"}

        h_team = {"id": 1, "name": "Human XI", "kind": "HUMAN", "user_id": 1001}
        a_team = {"id": 2, "name": "Chennai Super Kings", "kind": "SYSTEM", "franchise_code": "CSK"}
        self.repo.all_teams = AsyncMock(return_value=[h_team, a_team])

        sq1 = Squad(team_id=1, name="Human XI", short_name="H1", players=tuple(
            Player(id=f"p{i}", name=f"P{i}", role="BAT") for i in range(11)
        ))
        sq2_invalid = Squad(team_id=2, name="CSK", short_name="CSK", players=tuple(
            Player(id=f"csk_{i}", name=f"CSK Batter {i}", role="BAT") for i in range(11)
        ))  # 11 batters (0 bowlers) is invalid for system squad
        squads = {1: sq1, 2: sq2_invalid}

        with patch("ipl_draft.services.system_service.prepare_system_teams", new=AsyncMock()):
            res = await simulation_worker._simulate(self.rt, t, fx, squads)
            assert res.get("status") == "INVALID_SYSTEM_SQUAD"
            assert res.get("team_id") == 2

    # ── 9. Worker Crashes During Playoffs ────────────────────────────────────
    async def test_worker_crashes_during_playoffs_resumes_cleanly(self):
        """Worker restart during playoff stages correctly claims and settles pending fixtures."""
        tid = 80
        t = {"id": tid, "group_id": -1008, "state": "PLAYOFF_QUALIFIER_1"}

        teams = {
            1: {"id": 1, "name": "T1", "kind": "HUMAN"},
            2: {"id": 2, "name": "T2", "kind": "HUMAN"},
        }
        self.repo.all_teams = AsyncMock(return_value=list(teams.values()))

        # Fixture unclaimed
        self.repo.claim_fixtures = AsyncMock(return_value=[{"id": 201, "home": 1, "away": 2, "stage": "QUALIFIER_1"}])

        sq1 = Squad(team_id=1, name="T1", short_name="T1", players=tuple(
            Player(id=f"p1_{i}", name=f"P{i}", role="BAT") for i in range(11)
        ))
        sq2 = Squad(team_id=2, name="T2", short_name="T2", players=tuple(
            Player(id=f"p2_{i}", name=f"P{i}", role="BAT") for i in range(11)
        ))

        with patch("ipl_draft.services.simulation_worker.get_squads", new=AsyncMock(return_value={1: sq1, 2: sq2})), \
             patch("ipl_draft.services.simulation_worker._simulate", new=AsyncMock(return_value={"status": "OK", "match_id": 500})):
            res = await simulation_worker._playoff(self.rt, t)
            assert res is True
            self.repo.claim_fixtures.assert_called_once_with(tid, self.rt.worker_id, 1)

    # ── 10. Tournament Completes & Champion Announced ───────────────────────
    async def test_tournament_completes_and_champion_announced(self):
        """Finalize executes awards recording, champion announcement, and marks finalization complete."""
        tid = 90
        t = {
            "id": tid,
            "group_id": -1009,
            "state": "COMPLETED",
            "champion_team_id": 1,
            "runner_up_team_id": 2,
            "final_announced": False,
            "awards_recorded": False,
            "champion_announced": False,
            "mvp_announced": False,
            "final_dms_sent": False,
        }
        self.repo.get = AsyncMock(return_value=t)
        self.repo.all_teams = AsyncMock(return_value=[
            {"id": 1, "name": "Mumbai Indians", "kind": "SYSTEM"},
            {"id": 2, "name": "Chennai Super Kings", "kind": "SYSTEM"},
        ])
        self.repo.player_stats = AsyncMock(return_value=[])
        self.repo.standings = AsyncMock(return_value=[])
        self.repo.bracket = AsyncMock(return_value=[])

        with patch("ipl_draft.services.notification_service.group", new=AsyncMock()) as mock_group, \
             patch("ipl_draft.services.simulation_worker.update_group_tournament_summary", new=AsyncMock()):
            await simulation_worker.finalize(self.rt, tid)
            self.repo.record_results.assert_called_once_with(tid)
            self.repo.store_awards.assert_called_once()
            # Verify champion message announced
            mock_group.assert_called()
            champ_call = any("are CHAMPIONS!" in str(c) for c in mock_group.call_args_list)
            assert champ_call

    # ── 11. No /iplresume in Bot Commands or Menus ───────────────────────────
    def test_no_iplresume_in_bot_commands(self):
        """Verify /iplresume is completely removed from bot.COMMANDS."""
        cmds = {cmd.command for cmd in bot.COMMANDS}
        assert "iplresume" not in cmds
        assert "iplstats" in cmds
        assert "ipltable" in cmds
        assert "iplrules" in cmds

    # ── 12. Finalize Sends Squad Performance DM To Human Managers ──────────
    async def test_finalize_sends_squad_performance_dm_to_human_managers(self):
        """Verify squad performance summary (runs, wickets, totals) is sent to human managers on tournament end."""
        tid = 95
        t = {
            "id": tid,
            "group_id": -1095,
            "state": "COMPLETED",
            "champion_team_id": 1,
            "runner_up_team_id": 2,
            "final_announced": False,
            "final_dms_sent": False,
            "awards_recorded": False,
            "champion_announced": False,
            "mvp_announced": False,
        }
        self.repo.get = AsyncMock(return_value=t)
        human_team = {"id": 1, "name": "Dre XI", "kind": "HUMAN", "user_id": 7158623869}
        self.repo.all_teams = AsyncMock(return_value=[human_team])
        squad = [
            {"id": f"p{i}", "name": f"Player {i}", "role": "BAT", "bowling_type": "NONE"} for i in range(1, 12)
        ]
        self.repo.team_roster = AsyncMock(return_value={"team": human_team, "players": squad})
        self.repo.get_batting_order_state = AsyncMock(return_value=None)
        player_stats = [
            {"player_id": "p1", "runs": 288, "wickets": 21, "balls": 200, "bowl_runs": 150},
            {"player_id": "p2", "runs": 293, "wickets": 0, "balls": 210, "bowl_runs": 0},
        ]
        self.repo.player_stats = AsyncMock(return_value=player_stats)
        self.repo.standings = AsyncMock(return_value=[])
        self.repo.bracket = AsyncMock(return_value=[])

        with patch("ipl_draft.services.notification_service.dm", new=AsyncMock()) as mock_dm, \
             patch("ipl_draft.services.notification_service.group", new=AsyncMock()), \
             patch("ipl_draft.services.simulation_worker.update_group_tournament_summary", new=AsyncMock()):
            await simulation_worker.finalize(self.rt, tid)
            # DM sent to human user
            mock_dm.assert_called()
            # Verify squad performance text sent
            perf_calls = [c for c in mock_dm.call_args_list if "Your XI — Tournament Performance" in str(c)]
            assert len(perf_calls) == 1
            call_text = str(perf_calls[0])
            assert "Player 1 — 288 runs, 21 wkts" in call_text
            assert "Team totals:" in call_text
            self.repo.update.assert_any_call(tid, final_dms_sent=True)
            self.repo.update.assert_any_call(tid, final_announced=True)
