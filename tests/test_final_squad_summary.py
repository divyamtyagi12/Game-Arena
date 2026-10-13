"""Comprehensive test suite for final squad summary post-tournament feature.

Tests all required scenarios:
1. Tournament completes with multiple human players.
2. Every player receives only their own squad.
3. Squads with 8/11 and 11/11 players display correctly (format exact match).
4. Player suffixes such as '(2)' and role emojis are preserved.
5. A player blocks the bot (Forbidden / None return) - handles gracefully without crashing or interrupting others.
6. The tournament completion handler executes twice - delivery is idempotent, no duplicates.
7. The bot restarts during notification delivery - persistent state prevents duplicates.
8. A tournament contains AI/system-controlled teams - AI teams are excluded.
9. Multiple tournaments complete concurrently - each delivers independently without crosstalk.
10. Group chat displays championship and MVPs announcement matching Image format.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from ipl_draft import messages as m
from ipl_draft.config import IplSettings
from ipl_draft.runtime import IplRuntime
from ipl_draft.services import simulation_worker


class TestFinalSquadSummary(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.lock = lambda *a, **k: asyncio.Lock()
        self.repo = MagicMock()
        self.repo.lease = AsyncMock(return_value=True)
        self.repo.release_lease = AsyncMock(return_value=None)
        self.repo.update = AsyncMock()
        self.repo.record_results = AsyncMock()
        self.repo.store_awards = AsyncMock()
        self.repo.mark_squad_summary_sent = AsyncMock()
        self.repo.standings = AsyncMock(return_value=[])
        self.repo.bracket = AsyncMock(return_value=[])
        self.repo.player_stats = AsyncMock(return_value=[])
        self.repo.get_batting_order_state = AsyncMock(return_value=None)
        self.s = IplSettings()
        self.rt = IplRuntime(ctx=self.ctx, repo=self.repo, s=self.s)

    # ── 1. Message Format Test (Exact Style) ─────────────────────────────────
    def test_exact_squad_summary_format_8_of_11(self):
        """Verify 8/11 drafted squad matches exact requested format."""
        team = {"name": "Dre XI"}
        players = [
            {"name": "Hardik Pandya", "role": "AR", "nationality": "India"},
            {"name": "Azmatullah Omarzai", "role": "AR", "nationality": "Afghanistan"},
            {"name": "Sunil Narine", "role": "AR", "nationality": "West Indies"},
            {"name": "Jasprit Bumrah", "role": "BOWL", "nationality": "India"},
            {"name": "Virat Kohli", "role": "BAT", "nationality": "India"},
            {"name": "Sam Curran", "role": "AR", "nationality": "England"},
            {"name": "Cameron Green (2)", "role": "AR", "nationality": "Australia"},
            {"name": "MS Dhoni", "role": "WK", "nationality": "India"},
        ]
        text = m.final_squad_text(team, players)
        expected = (
            "📋 <b>Dre XI</b> — 8/11\n\n"
            "1. Hardik Pandya — ⚡ All-rounder · India\n"
            "2. Azmatullah Omarzai — ⚡ All-rounder · Afghanistan\n"
            "3. Sunil Narine — ⚡ All-rounder · West Indies\n"
            "4. Jasprit Bumrah — 🎯 Bowler · India\n"
            "5. Virat Kohli — 🏏 Batter · India\n"
            "6. Sam Curran — ⚡ All-rounder · England\n"
            "7. Cameron Green (2) — ⚡ All-rounder · Australia\n"
            "8. MS Dhoni — 🧤 Wicketkeeper · India"
        )
        assert text == expected

    def test_exact_squad_summary_format_11_of_11(self):
        """Verify 11/11 full squad displays correctly."""
        team = {"name": "Mumbai Indians"}
        players = [
            {"name": f"Player {i}", "role": "BAT", "nationality": "India"} for i in range(1, 12)
        ]
        text = m.final_squad_text(team, players)
        assert "📋 <b>Mumbai Indians</b> — 11/11" in text
        assert "1. Player 1 — 🏏 Batter · India" in text
        assert "11. Player 11 — 🏏 Batter · India" in text

    # ── 2. Multiple Human Players: Each Receives Only Their Own Squad ────────
    async def test_multiple_human_players_receive_own_squad_only(self):
        tid = 101
        t = {
            "id": tid, "group_id": -1101, "state": "COMPLETED",
            "champion_team_id": 10, "runner_up_team_id": 20,
            "final_announced": False, "final_dms_sent": False,
            "awards_recorded": True, "champion_announced": True, "mvp_announced": True,
        }
        self.repo.get = AsyncMock(return_value=t)

        user1_team = {"id": 10, "name": "Dre XI", "kind": "HUMAN", "user_id": 1001}
        user2_team = {"id": 20, "name": "Rhyme XI", "kind": "HUMAN", "user_id": 2002}
        ai_team = {"id": 30, "name": "Chennai Super Kings", "kind": "SYSTEM", "user_id": None}
        self.repo.all_teams = AsyncMock(return_value=[user1_team, user2_team, ai_team])

        squad1 = [
            {"name": "Virat Kohli", "role": "BAT", "nationality": "India"},
            {"name": "Jasprit Bumrah", "role": "BOWL", "nationality": "India"},
        ]
        squad2 = [
            {"name": "Heinrich Klaasen", "role": "WK", "nationality": "South Africa"},
            {"name": "Arshdeep Singh", "role": "BOWL", "nationality": "India"},
        ]

        async def get_roster(team_id):
            if team_id == 10:
                return {"team": user1_team, "players": squad1}
            elif team_id == 20:
                return {"team": user2_team, "players": squad2}
            return {"team": ai_team, "players": []}

        self.repo.team_roster = AsyncMock(side_effect=get_roster)

        with patch("ipl_draft.services.notification_service.dm", new=AsyncMock(return_value=True)) as mock_dm, \
             patch("ipl_draft.services.simulation_worker.update_group_tournament_summary", new=AsyncMock()):
            await simulation_worker.finalize(self.rt, tid)

            # Check calls to user 1001
            u1_calls = [c for c in mock_dm.call_args_list if c.args[1] == 1001]
            u1_squad_calls = [c for c in u1_calls if "📋 <b>Dre XI</b> — 2/11" in c.args[2]]
            assert len(u1_squad_calls) == 1
            assert "Virat Kohli" in u1_squad_calls[0].args[2]
            assert "Heinrich Klaasen" not in u1_squad_calls[0].args[2]

            # Check calls to user 2002
            u2_calls = [c for c in mock_dm.call_args_list if c.args[1] == 2002]
            u2_squad_calls = [c for c in u2_calls if "📋 <b>Rhyme XI</b> — 2/11" in c.args[2]]
            assert len(u2_squad_calls) == 1
            assert "Heinrich Klaasen" in u2_squad_calls[0].args[2]
            assert "Virat Kohli" not in u2_squad_calls[0].args[2]

            # AI team should have received no DM
            ai_calls = [c for c in mock_dm.call_args_list if c.args[1] is None]
            assert len(ai_calls) == 0

    # ── 3. Player Blocks Bot (Graceful Handling) ──────────────────────────────
    async def test_player_blocks_bot_does_not_break_other_players(self):
        tid = 102
        t = {
            "id": tid, "group_id": -1102, "state": "COMPLETED",
            "champion_team_id": 11, "runner_up_team_id": 22,
            "final_announced": False, "final_dms_sent": False,
            "awards_recorded": True, "champion_announced": True, "mvp_announced": True,
        }
        self.repo.get = AsyncMock(return_value=t)

        user1_team = {"id": 11, "name": "Active XI", "kind": "HUMAN", "user_id": 1111}
        user2_team = {"id": 22, "name": "Blocked XI", "kind": "HUMAN", "user_id": 2222}
        self.repo.all_teams = AsyncMock(return_value=[user1_team, user2_team])

        self.repo.team_roster = AsyncMock(return_value={"team": user1_team, "players": [{"name": "P1", "role": "BAT", "nationality": "India"}]})

        # Mock dm returning None for user 2222 (blocked / failure) and object for 1111
        async def mock_dm_fn(rt, uid, text, markup=None):
            if uid == 2222:
                return None  # Telegram reported Forbidden / failed
            return True

        with patch("ipl_draft.services.notification_service.dm", side_effect=mock_dm_fn) as mock_dm, \
             patch("ipl_draft.services.simulation_worker.update_group_tournament_summary", new=AsyncMock()):
            await simulation_worker.finalize(self.rt, tid)

            # Active user received squad DM
            assert user1_team.get("squad_summary_sent") is True
            # Blocked user was not marked as delivered
            assert user2_team.get("squad_summary_sent") is not True

            # Finalize completed without crashing
            self.repo.update.assert_any_call(tid, final_announced=True)

    # ── 4. Duplicate Prevention (Idempotent Execution) ────────────────────────
    async def test_completion_handler_executes_twice_no_duplicates(self):
        tid = 103
        t = {
            "id": tid, "group_id": -1103, "state": "COMPLETED",
            "champion_team_id": 33, "runner_up_team_id": 44,
            "final_announced": False, "final_dms_sent": False,
            "awards_recorded": True, "champion_announced": True, "mvp_announced": True,
        }
        self.repo.get = AsyncMock(return_value=t)
        team = {"id": 33, "name": "Solo XI", "kind": "HUMAN", "user_id": 3333}
        self.repo.all_teams = AsyncMock(return_value=[team])
        self.repo.team_roster = AsyncMock(return_value={"team": team, "players": [{"name": "P1", "role": "BAT", "nationality": "India"}]})

        dm_call_count = 0
        async def mock_dm_counter(rt, uid, text, markup=None):
            nonlocal dm_call_count
            if "📋 <b>Solo XI</b>" in text:
                dm_call_count += 1
            return True

        with patch("ipl_draft.services.notification_service.dm", side_effect=mock_dm_counter), \
             patch("ipl_draft.services.simulation_worker.update_group_tournament_summary", new=AsyncMock()):
            # Execution 1
            await simulation_worker.finalize(self.rt, tid)
            assert dm_call_count == 1

            # Execution 2 (e.g. retry or race before final_announced checked)
            # t may still have final_announced False if restarted mid-step
            await simulation_worker.finalize(self.rt, tid)
            # Must NOT have sent squad summary a second time!
            assert dm_call_count == 1

    # ── 5. Bot Restart Recovery ──────────────────────────────────────────────
    async def test_bot_restarts_during_notification_delivery_no_duplicates(self):
        """If the bot restarts after user 1 receives DM, user 1 is not re-sent upon restart."""
        tid = 104
        t = {
            "id": tid, "group_id": -1104, "state": "COMPLETED",
            "champion_team_id": 51, "runner_up_team_id": 52,
            "final_announced": False, "final_dms_sent": False,
            "awards_recorded": True, "champion_announced": True, "mvp_announced": True,
        }
        self.repo.get = AsyncMock(return_value=t)

        # User 1 already has squad_summary_sent=True persisted in DB from before the restart!
        user1_team = {"id": 51, "name": "Persisted XI", "kind": "HUMAN", "user_id": 5001, "squad_summary_sent": True}
        user2_team = {"id": 52, "name": "Waiting XI", "kind": "HUMAN", "user_id": 5002, "squad_summary_sent": False}
        self.repo.all_teams = AsyncMock(return_value=[user1_team, user2_team])
        self.repo.team_roster = AsyncMock(return_value={"team": user2_team, "players": [{"name": "P1", "role": "BAT", "nationality": "India"}]})

        # Fresh runtime instance simulates bot restart (in-memory sets are empty)
        fresh_rt = IplRuntime(ctx=self.ctx, repo=self.repo, s=self.s)

        sent_to = []
        async def mock_dm_fn(rt, uid, text, markup=None):
            if "📋 <b>" in text:
                sent_to.append(uid)
            return True

        with patch("ipl_draft.services.notification_service.dm", side_effect=mock_dm_fn), \
             patch("ipl_draft.services.simulation_worker.update_group_tournament_summary", new=AsyncMock()):
            await simulation_worker.finalize(fresh_rt, tid)

            # User 1 was skipped because of DB state! User 2 received squad DM.
            assert 5001 not in sent_to
            assert 5002 in sent_to

    # ── 6. Group Chat Image Format (Champion + MVPs) ─────────────────────────
    async def test_group_chat_displays_image_format_champion_and_mvps(self):
        tid = 105
        t = {
            "id": tid, "group_id": -1105, "state": "COMPLETED",
            "champion_team_id": 61, "runner_up_team_id": 62,
            "final_announced": False, "final_dms_sent": True,
            "awards_recorded": False, "champion_announced": False, "mvp_announced": False,
            "lease_owner": self.rt.worker_id,
        }
        self.repo.get = AsyncMock(return_value=t)
        champ = {"id": 61, "name": "Rhyme", "kind": "HUMAN", "user_id": 6001}
        runner = {"id": 62, "name": "Mumbai Indians", "kind": "SYSTEM", "user_id": None}
        self.repo.all_teams = AsyncMock(return_value=[champ, runner])
        self.repo.team_roster = AsyncMock(return_value={"team": champ, "players": []})

        stats = [
            {"player_id": "p1", "name": "Heinrich Klaasen", "team_id": 61, "runs": 581, "balls": 300, "wickets": 0, "bat_innings": 10},
            {"player_id": "p2", "name": "Arshdeep Singh", "team_id": 61, "runs": 10, "balls": 15, "wickets": 29, "bowl_runs": 280, "bat_innings": 5},
        ]
        self.repo.player_stats = AsyncMock(return_value=stats)

        group_msgs = []
        async def mock_group(rt, gid, text, markup=None):
            group_msgs.append(text)
            return True

        with patch("ipl_draft.services.notification_service.group", side_effect=mock_group), \
             patch("ipl_draft.services.simulation_worker.update_group_tournament_summary", new=AsyncMock()):
            await simulation_worker.finalize(self.rt, tid)

            # Check champion announcement
            champ_msg = next((m for m in group_msgs if "are CHAMPIONS! 🏆🎉" in m), None)
            assert champ_msg is not None
            assert "👑 <b>Rhyme are CHAMPIONS! 🏆🎉</b>" in champ_msg

            # Check MVPs announcement matching Image format
            mvp_msg = next((m for m in group_msgs if "Tournament MVPs (among players' teams)" in m), None)
            assert mvp_msg is not None
            assert "🏏 <b>Most Runs:</b> Heinrich Klaasen — 581 runs (in Rhyme)" in mvp_msg
            assert "🎯 <b>Most Wickets:</b> Arshdeep Singh — 29 wickets (in Rhyme)" in mvp_msg

    # ── 7. Concurrent Tournament Finalization ────────────────────────────────
    async def test_concurrent_tournament_finalizations(self):
        """Two tournaments finalize concurrently without conflicts or cross-talk."""
        t1 = {"id": 201, "group_id": -201, "state": "COMPLETED", "champion_team_id": 1, "runner_up_team_id": 2, "final_announced": False, "final_dms_sent": False, "awards_recorded": True, "champion_announced": True, "mvp_announced": True}
        t2 = {"id": 202, "group_id": -202, "state": "COMPLETED", "champion_team_id": 3, "runner_up_team_id": 4, "final_announced": False, "final_dms_sent": False, "awards_recorded": True, "champion_announced": True, "mvp_announced": True}

        team1 = {"id": 1, "name": "Team A", "kind": "HUMAN", "user_id": 10}
        team2 = {"id": 3, "name": "Team B", "kind": "HUMAN", "user_id": 20}

        async def get_tourney(tid):
            return t1 if tid == 201 else t2

        async def get_all_teams(tid):
            return [team1] if tid == 201 else [team2]

        self.repo.get = AsyncMock(side_effect=get_tourney)
        self.repo.all_teams = AsyncMock(side_effect=get_all_teams)
        self.repo.team_roster = AsyncMock(return_value={"team": team1, "players": [{"name": "Player 1", "role": "BAT", "nationality": "India"}]})

        received_by_user = {}
        async def mock_dm_fn(rt, uid, text, markup=None):
            if "📋 <b>" in text:
                received_by_user.setdefault(uid, []).append(text)
            return True

        with patch("ipl_draft.services.notification_service.dm", side_effect=mock_dm_fn), \
             patch("ipl_draft.services.simulation_worker.update_group_tournament_summary", new=AsyncMock()):
            await asyncio.gather(
                simulation_worker.finalize(self.rt, 201),
                simulation_worker.finalize(self.rt, 202),
            )

            assert len(received_by_user[10]) == 1
            assert "Team A" in received_by_user[10][0]
            assert len(received_by_user[20]) == 1
            assert "Team B" in received_by_user[20][0]
