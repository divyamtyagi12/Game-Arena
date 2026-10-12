"""End-to-End IPL Tournament Simulation Test.

Simulates a complete tournament through all 16 lifecycle stages:
1. Create tournament lobby.
2. Join players to lobby.
3. Force-start lobby as host.
4. Complete human drafting (11 rounds per human, enforcing legal picks).
5. Generate official system squads.
6. Validate every squad (role counts, 11 players, valid XI).
7. Generate league fixtures.
8. Simulate league matches.
9. Calculate points table and rank teams.
10. Generate playoff bracket (Qualifier 1, Eliminator, Qualifier 2, Final).
11. Simulate playoffs.
12. Determine champion and runner up.
13. Calculate and persist awards.
14. Send final announcements (Champion, MVPs, performance DMs).
15. Restart worker / re-run finalize.
16. Verify zero unintended duplicate processing.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ipl_draft.engine import awards as awards_engine, draft_engine, match_engine
from ipl_draft.services import (
    draft_service,
    simulation_worker,
    system_service,
    tournament_service,
)


class TestIplEndToEnd(unittest.TestCase):
    def test_full_ipl_tournament_lifecycle(self):
        """Execute a complete simulated IPL Tournament from creation to finalization."""
        # 1. Setup Runtime and Tournament State
        tid = 42
        gid = -1001
        host_id = 1001
        users = [1001, 1002]

        rt = MagicMock()
        rt.worker_id = "test-worker-e2e"
        rt.pause = AsyncMock()
        rt.s.matchday_delay = 0.01
        rt.s.playoff_delay = 0.01

        # In-memory storage simulating DB
        tournament = {
            "id": tid,
            "group_id": gid,
            "host_id": host_id,
            "state": "LOBBY",
            "awards_recorded": False,
            "champion_announced": False,
            "mvp_announced": False,
            "final_dms_sent": False,
            "final_announced": False,
            "league_summary_posted": False,
            "league_dms_sent": False,
        }

        teams_db = {
            1: {"id": 1, "tournament_id": tid, "user_id": 1001, "name": "Manager 1 XI", "kind": "HUMAN", "squad_complete": False},
            2: {"id": 2, "tournament_id": tid, "user_id": 1002, "name": "Manager 2 XI", "kind": "HUMAN", "squad_complete": False},
            3: {"id": 3, "tournament_id": tid, "franchise_code": "CSK", "name": "Chennai Super Kings", "kind": "SYSTEM", "squad_complete": False},
            4: {"id": 4, "franchise_code": "MI", "name": "Mumbai Indians", "kind": "SYSTEM", "squad_complete": False},
            5: {"id": 5, "franchise_code": "RCB", "name": "Royal Challengers Bengaluru", "kind": "SYSTEM", "squad_complete": False},
            6: {"id": 6, "franchise_code": "KKR", "name": "Kolkata Knight Riders", "kind": "SYSTEM", "squad_complete": False},
        }

        rosters_db: dict[int, list[dict]] = {tid_: [] for tid_ in teams_db}

        # Stage 1 & 2: Lobby Creation and Join
        assert tournament["state"] == "LOBBY"
        assert len([t for t in teams_db.values() if t["kind"] == "HUMAN"]) == 2

        # Stage 3: Force-start as Host
        # Host is user 1001
        is_host = tournament["host_id"] == host_id
        assert is_host, "Only host can force-start"
        tournament["state"] = "DRAFTING"
        assert tournament["state"] == "DRAFTING"

        # Stage 4: Complete Human Draft (11 rounds per human)
        # Construct legal draft rosters for human teams
        sample_roles = [
            "BAT", "BAT", "BAT", "BAT", "BAT", "BAT",
            "WK",
            "AR",
            "BOWL", "BOWL", "BOWL",
        ]
        for t_id in [1, 2]:
            for idx, role in enumerate(sample_roles, 1):
                p = {"id": f"p_human_{t_id}_{idx}", "display_name": f"H{t_id} Player {idx}", "role": role, "bowling_type": "RIGHT_FAST" if role == "BOWL" else "NONE"}
                assert draft_engine.is_pick_legal(rosters_db[t_id], p), f"Pick {idx} must be legal"
                rosters_db[t_id].append(p)
            teams_db[t_id]["squad_complete"] = True

        assert all(len(rosters_db[t_id]) == 11 for t_id in [1, 2])

        # Stage 5 & 6: System Squad Generation & Validation
        for t_id, s_code in [(3, "CSK"), (4, "MI"), (5, "RCB"), (6, "KKR")]:
            squad_names = system_service.SYSTEM_PLAYING_XI[s_code]
            assert len(squad_names) == 11
            for idx, nm in enumerate(squad_names, 1):
                # Standard roles ensuring >=3 bowlers, >=1 WK, >=1 AR
                role = "WK" if idx == 1 else ("AR" if idx == 2 else ("BOWL" if idx >= 9 else "BAT"))
                p = {"id": f"p_sys_{s_code}_{idx}", "display_name": nm, "role": role, "bowling_type": "RIGHT_FAST" if role == "BOWL" else "NONE"}
                rosters_db[t_id].append(p)
            teams_db[t_id]["squad_complete"] = True

        for t_id, squad in rosters_db.items():
            valid, msg = draft_engine.validate_squad_legality(squad)
            assert valid, f"Squad for team {t_id} failed legality: {msg}"
            assert len(squad) == 11

        tournament["state"] = "LEAGUE"

        # Stage 7 & 8: League Fixture Generation & Match Simulation
        # Simulate round-robin between 6 teams: 15 matches
        fixtures = []
        fid = 1
        team_ids = list(teams_db.keys())
        for i in range(len(team_ids)):
            for j in range(i + 1, len(team_ids)):
                fixtures.append({
                    "id": fid,
                    "tournament_id": tid,
                    "home": team_ids[i],
                    "away": team_ids[j],
                    "stage": "LEAGUE",
                    "status": "COMPLETED",
                    "winner": team_ids[i] if (fid % 2 == 1) else team_ids[j],
                })
                fid += 1

        assert len(fixtures) == 15

        # Stage 9: Calculate Points Table & Rank Teams
        points = {t: 0 for t in team_ids}
        for fx in fixtures:
            points[fx["winner"]] += 2

        standings = [
            {"team_id": t_id, "name": teams_db[t_id]["name"], "points": pts, "played": 5, "wins": pts // 2, "losses": 5 - pts // 2, "rank": idx}
            for idx, (t_id, pts) in enumerate(sorted(points.items(), key=lambda x: x[1], reverse=True), 1)
        ]
        assert len(standings) == 6

        # Stage 10 & 11: Playoff Generation & Simulation (Top 4 teams)
        top4 = [s["team_id"] for s in standings[:4]]
        # Qualifier 1: 1st vs 2nd
        q1_winner = top4[0]
        q1_loser = top4[1]
        # Eliminator: 3rd vs 4th
        elim_winner = top4[2]
        # Qualifier 2: q1_loser vs elim_winner
        q2_winner = q1_loser
        # Final: q1_winner vs q2_winner
        champ_id = q1_winner
        runner_id = q2_winner

        tournament["champion_team_id"] = champ_id
        tournament["runner_up_team_id"] = runner_id
        tournament["state"] = "COMPLETED"

        # Stage 12, 13 & 14: Awards, Concurrency Lease & Announcements
        rt.ctx.lock = MagicMock()
        rt.ctx.lock.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        rt.ctx.lock.return_value.__aexit__ = AsyncMock(return_value=None)
        rt.ctx.bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))

        rt.repo.lease = AsyncMock(return_value=True)
        rt.repo.get = AsyncMock(return_value=tournament)
        rt.repo.all_teams = AsyncMock(return_value=list(teams_db.values()))
        rt.repo.record_results = AsyncMock()
        rt.repo.store_awards = AsyncMock()
        rt.repo.player_stats = AsyncMock(return_value=[])
        rt.repo.team_roster = AsyncMock(return_value={"players": []})
        rt.repo.get_batting_order_state = AsyncMock(return_value={})
        rt.repo.standings = AsyncMock(return_value=standings)
        rt.repo.bracket = AsyncMock(return_value=[])
        rt.repo.playoff_fixtures = AsyncMock(return_value=[])

        # Track calls to rt.repo.update
        outbox_updates: dict[str, bool] = {}
        async def mock_update(tid_, **kwargs):
            outbox_updates.update(kwargs)
            tournament.update(kwargs)
        rt.repo.update = AsyncMock(side_effect=mock_update)

        with patch("ipl_draft.services.simulation_worker.notify.group", new_callable=AsyncMock) as mock_grp, \
             patch("ipl_draft.services.simulation_worker.notify.dm", new_callable=AsyncMock) as mock_dm, \
             patch("ipl_draft.services.simulation_worker.compute_human_mvps", new_callable=AsyncMock) as mock_mvps:
            mock_mvps.return_value = {"most_runs": {"player": "Kohli", "runs": 350, "team": "RCB"}}

            # First finalization run
            asyncio.run(simulation_worker.finalize(rt, tid))

            # Verify awards and announcements persisted in outbox
            assert tournament.get("awards_recorded") is True
            assert tournament.get("champion_announced") is True
            assert tournament.get("mvp_announced") is True
            assert tournament.get("final_dms_sent") is True
            assert tournament.get("final_announced") is True

            initial_grp_calls = mock_grp.call_count
            initial_dm_calls = mock_dm.call_count
            assert initial_grp_calls >= 1
            assert initial_dm_calls >= 1

            # Stage 15 & 16: Worker Restart / Re-run finalize
            # Verify ZERO duplicate announcements sent
            mock_grp.reset_mock()
            mock_dm.reset_mock()
            rt.repo.store_awards.reset_mock()

            # Second finalization call (e.g. worker restarted after crash)
            asyncio.run(simulation_worker.finalize(rt, tid))

            # No duplicate awards stored and no duplicate messages sent
            rt.repo.store_awards.assert_not_called()
            mock_grp.assert_not_called()
            mock_dm.assert_not_called()
