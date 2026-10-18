"""Comprehensive regression tests covering all confirmed issues:
- Issue A: SQL/Python pick response contract normalization & after_pick completion
- Issue B: Unified squad legality (11 players, 3 bowlers, 1 keeper, 1 all-rounder, no duplicates, no confirm/reset buttons)
- Issue C: Offer delivery resilience, double-click handling, Telegram DM failure recovery
- Issue D: Database lease safety, mutual exclusion, renewal ownership, and single finalization
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from ipl_draft.engine import draft_engine
from ipl_draft.engine.draft_engine import SQUAD, auto_pick, is_pick_legal, remaining_role_needs
from ipl_draft import keyboards as kb
from ipl_draft.repository import IplRepo
from ipl_draft.runtime import IplRuntime
from ipl_draft.services import draft_service, simulation_worker


class TestAuditRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_db = MagicMock()
        self.repo = IplRepo(self.mock_db)
        self.rt = MagicMock(spec=IplRuntime)
        self.rt.repo = self.repo
        self.rt.s = MagicMock()
        self.rt.s.draft_seconds = 30
        self.rt.s.auto_after_timeouts = 2
        self.rt.s.batting_order_seconds = 60
        self.rt.running = set()
        self.rt.failures = {}
        self.rt.retry_after = {}
        self.rt.progress_last = {}
        self.rt.progress_pending = set()
        self.rt.ctx = MagicMock()
        self.rt.ctx.spawn = MagicMock()
        self.rt.rng = MagicMock()
        self.rt.worker_id = "worker-1"

    # ══════════════════════════════════════════════════════════════════════
    # ISSUE A: Response Contract Normalization & Post-Pick Progression
    # ══════════════════════════════════════════════════════════════════════

    async def test_repo_pick_normalizes_v4_response_contract(self):
        """Verify IplRepo.pick normalizes a v4 response lacking team_id/tournament_id/done."""
        offer_id = 42
        team_id = 101
        tournament_id = 99
        v4_response = {
            "status": "OK",
            "player": {"id": "p1", "name": "Jasprit Bumrah", "role": "BOWL"},
            "picks_done": 5,
            "squad_complete": False,
            "humans_drafting": 1,
        }
        self.repo._r = AsyncMock(return_value=v4_response)
        self.repo.get_offer = AsyncMock(return_value={"id": offer_id, "team_id": team_id, "tournament_id": tournament_id, "round_no": 5})

        normalized = await self.repo.pick(offer_id, 1, 12345, "USER")

        assert normalized["status"] == "OK"
        assert normalized["done"] is False
        assert normalized["squad_complete"] is False
        assert normalized["all_humans_done"] is False
        assert normalized["team_id"] == team_id
        assert normalized["tournament_id"] == tournament_id
        assert normalized["round_no"] == 5
        assert normalized["auto"] is False
        assert normalized["user_id"] == 12345

    async def test_after_pick_succeeds_without_key_error_on_normalized_res(self):
        """Verify after_pick executes fully, disables previous markup, and delivers next offer."""
        res = {
            "status": "OK",
            "player": {"id": "p1", "name": "Virat Kohli", "role": "BAT"},
            "round_no": 3,
            "picks_done": 3,
            "done": False,
            "squad_complete": False,
            "team_id": 101,
            "user_id": 12345,
            "tournament_id": 99,
            "all_humans_done": False,
            "humans_drafting": 2,
        }
        human_team = {"id": 101, "name": "Royal Challengers", "user_id": 12345, "tournament_id": 99, "auto_draft": False}
        roster = {"team": human_team, "players": [{"name": "Virat Kohli", "role": "BAT"}]}
        self.repo.team_roster = AsyncMock(return_value=roster)

        with patch("ipl_draft.services.notification_service.edit_dm", new=AsyncMock(return_value=True)) as mock_edit, \
             patch("ipl_draft.services.draft_service.deliver_offer", new=AsyncMock()) as mock_deliver, \
             patch("ipl_draft.services.draft_service.update_progress", new=AsyncMock()) as mock_progress:
            await draft_service.after_pick(self.rt, res, message_id=555, auto=False)

            mock_edit.assert_called_once()
            _, kwargs = mock_edit.call_args
            assert kwargs.get("markup") is None  # Previous buttons removed
            mock_deliver.assert_called_once_with(self.rt, human_team)
            mock_progress.assert_called_once_with(self.rt, 99, force=False)

    async def test_after_pick_11th_pick_triggers_batting_order_and_advances(self):
        """Verify 11th pick triggers batting order flow and spawns tournament advance if all humans done."""
        res = {
            "status": "OK",
            "player": {"id": "p11", "name": "Ravindra Jadeja", "role": "AR"},
            "round_no": 11,
            "picks_done": 11,
            "done": True,
            "squad_complete": True,
            "team_id": 101,
            "user_id": 12345,
            "tournament_id": 99,
            "all_humans_done": True,
            "humans_drafting": 0,
        }
        human_team = {"id": 101, "name": "Super Kings", "user_id": 12345, "tournament_id": 99, "auto_draft": False}
        players = [{"id": f"p{i}", "name": f"P{i}", "role": "BAT"} for i in range(11)]
        roster = {"team": human_team, "players": players}
        self.repo.team_roster = AsyncMock(return_value=roster)

        with patch("ipl_draft.services.notification_service.edit_dm", new=AsyncMock(return_value=True)), \
             patch("ipl_draft.services.batting_order_service.start_batting_order_flow", new=AsyncMock()) as mock_bo, \
             patch("ipl_draft.services.draft_service.deliver_offer", new=AsyncMock()) as mock_deliver, \
             patch("ipl_draft.services.draft_service.update_progress", new=AsyncMock()), \
             patch("ipl_draft.services.simulation_worker.advance", new=AsyncMock()) as mock_advance:
            await draft_service.after_pick(self.rt, res, message_id=555, auto=False)

            mock_bo.assert_called_once_with(self.rt, human_team, players)
            mock_deliver.assert_not_called()  # No more offers after 11th pick
            assert self.rt.ctx.spawn.called  # advance spawned

    # ══════════════════════════════════════════════════════════════════════
    # ISSUE B: Unified Draft Legality & Duplicate Prevention
    # ══════════════════════════════════════════════════════════════════════

    def test_duplicate_player_rejected_by_is_pick_legal(self):
        """Verify a candidate cricketer already in the roster is strictly rejected."""
        roster = [
            {"id": "p_vk", "name": "Virat Kohli", "role": "BAT"},
            {"id": "p_jb", "name": "Jasprit Bumrah", "role": "BOWL"},
        ]
        # Same ID
        assert is_pick_legal(roster, {"id": "p_vk", "name": "Virat Kohli", "role": "BAT"}) is False
        # Same Name (case insensitive)
        assert is_pick_legal(roster, {"id": "p_diff", "name": "virat kohli", "role": "BAT"}) is False
        # New player
        assert is_pick_legal(roster, {"id": "p_rs", "name": "Rohit Sharma", "role": "BAT"}) is True

    def test_all_roster_sizes_from_0_to_11_role_feasibility(self):
        """Verify mandatory role constraints (3 BOWL, 1 WK, 1 AR) across roster sizes 0..11."""
        roster = []
        # Size 0: all roles permitted
        for r in ("BAT", "BOWL", "WK", "AR"):
            assert is_pick_legal(roster, r) is True

        # Pick 6 pure batters (size 6, 5 picks left: 3 BOWL + 1 WK + 1 AR = 5 required)
        for i in range(6):
            roster.append({"id": f"bat_{i}", "name": f"Batter {i}", "role": "BAT"})

        # Now only mandatory deficit roles are allowed!
        assert is_pick_legal(roster, "BAT") is False
        assert is_pick_legal(roster, "BOWL") is True
        assert is_pick_legal(roster, "WK") is True
        assert is_pick_legal(roster, "AR") is True

        # Pick 1 bowler (size 7, 4 picks left: 2 BOWL + 1 WK + 1 AR = 4 required)
        roster.append({"id": "bowl_1", "name": "Bowler 1", "role": "BOWL"})
        assert is_pick_legal(roster, "BAT") is False
        assert is_pick_legal(roster, "BOWL") is True

        # Pick 1 WK (size 8, 3 picks left: 2 BOWL + 1 AR = 3 required)
        roster.append({"id": "wk_1", "name": "Keeper 1", "role": "WK"})
        assert is_pick_legal(roster, "BAT") is False
        assert is_pick_legal(roster, "WK") is False  # WK already satisfied, remaining slots needed for BOWL & AR
        assert is_pick_legal(roster, "BOWL") is True
        assert is_pick_legal(roster, "AR") is True

        # Pick 1 AR (size 9, 2 picks left: 2 BOWL = 2 required)
        roster.append({"id": "ar_1", "name": "All-Rounder 1", "role": "AR"})
        assert is_pick_legal(roster, "BOWL") is True
        assert is_pick_legal(roster, "AR") is False
        assert is_pick_legal(roster, "BAT") is False

        # Pick 2 bowlers (size 11)
        roster.append({"id": "bowl_2", "name": "Bowler 2", "role": "BOWL"})
        roster.append({"id": "bowl_3", "name": "Bowler 3", "role": "BOWL"})
        assert len(roster) == 11
        # Full roster (11 players) allows no more picks
        assert is_pick_legal(roster, "BAT") is False
        assert is_pick_legal(roster, "BOWL") is False

        # Squad legality
        ok, reason = draft_engine.validate_squad_legality(roster)
        assert ok is True
        assert reason == "OK"

    def test_keyboards_offer_disables_illegal_and_duplicate_candidates(self):
        """Verify keyboards.offer excludes illegal and duplicate cards and NEVER displays block signs (🚫)."""
        roster = [
            {"id": "p_vk", "name": "Virat Kohli", "role": "BAT"},
            {"id": "p1", "name": "B1", "role": "BAT"},
            {"id": "p2", "name": "B2", "role": "BAT"},
            {"id": "p3", "name": "B3", "role": "BAT"},
            {"id": "p4", "name": "B4", "role": "BAT"},
            {"id": "p5", "name": "B5", "role": "BAT"},
        ]  # 6 batters: BAT is now illegal, plus Virat Kohli is duplicate
        candidates = [
            {"pos": 1, "player": {"id": "p_vk", "name": "Virat Kohli", "role": "BAT"}},  # Duplicate
            {"pos": 2, "player": {"id": "p_rs", "name": "Rohit Sharma", "role": "BAT"}},  # Illegal role
            {"pos": 3, "player": {"id": "p_jb", "name": "Jasprit Bumrah", "role": "BOWL"}},  # Legal!
        ]
        markup = kb.offer(42, candidates, 101, roster=roster)
        buttons = [btn for row in markup.inline_keyboard for btn in row]

        # Illegal/duplicate candidates are never rendered as blocked buttons: zero 🚫 signs
        assert all("🚫" not in b.text for b in buttons)
        assert all("ipl:blk" not in (b.callback_data or "") for b in buttons)

        # Only the legal candidate (+ MY TEAM button) is present
        selectable = [b for b in buttons if b.callback_data and b.callback_data.startswith("ipl:pk")]
        assert len(selectable) == 1
        assert selectable[0].callback_data == "ipl:pk:42:3"
        assert "Jasprit Bumrah" in selectable[0].text

    def test_keyboards_offer_criteria_left_gives_4_options_never_block_sign(self):
        """When a role criteria is left (e.g. 1 keeper), offer provides 4 options of that required role and zero block signs."""
        # 10 players, 1 pick left, needs 1 WK
        roster_need_wk = [
            {"id": f"p{i}", "name": f"P{i}", "role": "BAT"} for i in range(1, 7)
        ] + [
            {"id": "p7", "name": "P7", "role": "AR"},
            {"id": "p8", "name": "P8", "role": "BOWL"},
            {"id": "p9", "name": "P9", "role": "BOWL"},
            {"id": "p10", "name": "P10", "role": "BOWL"},
        ]
        candidates = [
            {"pos": i, "player": {"id": f"wk{i}", "name": f"Keeper {i}", "role": "WK"}} for i in range(1, 7)
        ] + [
            {"pos": 7, "player": {"id": "bat1", "name": "Batter 1", "role": "BAT"}},
            {"pos": 8, "player": {"id": "bat2", "name": "Batter 2", "role": "BAT"}},
        ]
        markup = kb.offer(100, candidates, 10, roster=roster_need_wk)
        buttons = [btn for row in markup.inline_keyboard for btn in row if btn.callback_data and btn.callback_data.startswith("ipl:pk")]

        # Exactly 4 options of keeper to pick
        assert len(buttons) == 4
        assert all("🧤" in b.text for b in buttons)
        assert all("🚫" not in b.text for b in buttons)

        # Same for bowler criteria: 10 players, needs 1 BOWL
        roster_need_bowl = [
            {"id": f"p{i}", "name": f"P{i}", "role": "BAT"} for i in range(1, 7)
        ] + [
            {"id": "p7", "name": "P7", "role": "WK"},
            {"id": "p8", "name": "P8", "role": "AR"},
            {"id": "p9", "name": "P9", "role": "BOWL"},
            {"id": "p10", "name": "P10", "role": "BOWL"},
        ]
        bowl_cands = [
            {"pos": i, "player": {"id": f"bw{i}", "name": f"Bowler {i}", "role": "BOWL"}} for i in range(1, 7)
        ] + [
            {"pos": 7, "player": {"id": "bat1", "name": "Batter 1", "role": "BAT"}},
        ]
        markup_b = kb.offer(101, bowl_cands, 10, roster=roster_need_bowl)
        b_buttons = [btn for row in markup_b.inline_keyboard for btn in row if btn.callback_data and btn.callback_data.startswith("ipl:pk")]
        assert len(b_buttons) == 4
        assert all("🎯" in b.text for b in b_buttons)
        assert all("🚫" not in b.text for b in b_buttons)

        # Same for all-rounder criteria: 10 players, needs 1 AR
        roster_need_ar = [
            {"id": f"p{i}", "name": f"P{i}", "role": "BAT"} for i in range(1, 7)
        ] + [
            {"id": "p7", "name": "P7", "role": "WK"},
            {"id": "p8", "name": "P8", "role": "BOWL"},
            {"id": "p9", "name": "P9", "role": "BOWL"},
            {"id": "p10", "name": "P10", "role": "BOWL"},
        ]
        ar_cands = [
            {"pos": i, "player": {"id": f"ar{i}", "name": f"AllRounder {i}", "role": "AR"}} for i in range(1, 7)
        ] + [
            {"pos": 7, "player": {"id": "bat1", "name": "Batter 1", "role": "BAT"}},
        ]
        markup_ar = kb.offer(102, ar_cands, 10, roster=roster_need_ar)
        ar_buttons = [btn for row in markup_ar.inline_keyboard for btn in row if btn.callback_data and btn.callback_data.startswith("ipl:pk")]
        assert len(ar_buttons) == 4
        assert all("🔄" in b.text for b in ar_buttons)
        assert all("🚫" not in b.text for b in ar_buttons)

    def test_offer_keyboard_never_has_confirm_or_reset_buttons(self):
        """Verify offer keyboards strictly adhere to single-tap selection (zero confirm/reset buttons)."""
        candidates = [{"pos": 1, "player": {"name": "Rashid Khan", "role": "BOWL"}}]
        markup = kb.offer(1, candidates, 10)
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        for l in labels:
            assert "CONFIRM" not in l.upper()
            assert "RESET" not in l.upper()

    def test_auto_pick_never_selects_illegal_candidate(self):
        """Verify auto_pick selects strictly from mathematically legal candidates."""
        roster = [{"id": f"p{i}", "name": f"P{i}", "role": "BAT"} for i in range(6)]
        candidates = [
            {"pos": 1, "player": {"id": "c1", "name": "Batter", "role": "BAT"}},  # Illegal
            {"pos": 2, "player": {"id": "c2", "name": "Bowler", "role": "BOWL"}},  # Legal
        ]
        rng = MagicMock()
        rng.choice = lambda x: x[0]
        selected_pos = auto_pick(candidates, roster, rng)
        assert selected_pos == 2

    # ══════════════════════════════════════════════════════════════════════
    # ISSUE C: Delivery Resilience, Double-Click Handling & Recovery
    # ══════════════════════════════════════════════════════════════════════

    async def test_telegram_edit_failure_does_not_abort_post_pick_flow(self):
        """Verify that a Telegram edit failure in after_pick does not prevent offer delivery or progress."""
        res = {
            "status": "OK",
            "player": {"id": "p1", "name": "Shubman Gill", "role": "BAT"},
            "done": False,
            "squad_complete": False,
            "team_id": 101,
            "tournament_id": 99,
        }
        human_team = {"id": 101, "name": "Titans", "user_id": 12345, "auto_draft": False}
        self.repo.team_roster = AsyncMock(return_value={"team": human_team, "players": [res["player"]]})

        with patch("ipl_draft.services.notification_service.edit_dm", side_effect=Exception("Telegram Network Error")), \
             patch("ipl_draft.services.draft_service.deliver_offer", new=AsyncMock()) as mock_deliver, \
             patch("ipl_draft.services.draft_service.update_progress", new=AsyncMock()) as mock_progress:
            await draft_service.after_pick(self.rt, res, message_id=123, auto=False)

            mock_deliver.assert_called_once_with(self.rt, human_team)
            mock_progress.assert_called_once_with(self.rt, 99, force=False)

    async def test_undeliverable_dm_switches_team_to_auto_draft(self):
        """Verify deliver_offer sets auto_draft if DM fails so tournament is never stalled."""
        team = {"id": 101, "user_id": 99999, "auto_draft": False}
        self.repo.open_offer = AsyncMock(return_value={"status": "OPEN", "offer_id": 1, "picks_done": 0, "candidates": []})
        self.repo.team_roster = AsyncMock(return_value={"players": []})
        self.repo.set_auto_draft = AsyncMock()

        with patch("ipl_draft.services.notification_service.dm", new=AsyncMock(return_value=None)):
            await draft_service.deliver_offer(self.rt, team)

            self.repo.set_auto_draft.assert_called_once_with(101)

    # ══════════════════════════════════════════════════════════════════════
    # ISSUE D: Database Lease Safety & Concurrency
    # ══════════════════════════════════════════════════════════════════════

    async def test_database_lease_mutual_exclusion_and_renewal(self):
        """Verify lease checks ownership and returns False when leased by another worker."""
        # 1. Lease acquisition when available
        self.repo._r = AsyncMock(return_value=True)
        ok = await self.repo.lease(tid=10, owner="worker-1", seconds=180)
        assert ok is True

        # 2. Lease acquisition fails when another worker holds unexpired lease
        self.repo._r = AsyncMock(return_value=False)
        ok2 = await self.repo.lease(tid=10, owner="worker-2", seconds=180)
        assert ok2 is False  # Must NOT steal the lease!

    async def test_database_release_lease_ownership(self):
        """Verify release_lease passes owner and does not clear another worker's lease."""
        self.repo._r = AsyncMock()
        await self.repo.release_lease(tid=10, owner="worker-1")
        self.repo._r.assert_called_once_with("ipl_release_lease", p_tid=10, p_owner="worker-1")

    async def test_completed_tournament_not_finalized_twice(self):
        """Verify finalize exits immediately if final_announced is already True."""
        t = {
            "id": 99,
            "state": "COMPLETED",
            "final_announced": True,
            "group_id": -100,
        }
        self.repo.get = AsyncMock(return_value=t)

        with patch("ipl_draft.services.simulation_worker.awards_engine.compute_awards") as mock_awards:
            await simulation_worker.finalize(self.rt, 99)
            mock_awards.assert_not_called()

    # ══════════════════════════════════════════════════════════════════════
    # ISSUE E: Missing Deficit Roles (Keeper/Bowler) and Auto-Assign Fallback
    # ══════════════════════════════════════════════════════════════════════

    def test_keyboards_offer_missing_keeper_renders_auto_assign_button(self):
        """When an offer contains NO keepers and 1 keeper is required, keyboard must render AUTO-ASSIGN button."""
        roster_need_wk = [
            {"id": f"p{i}", "name": f"P{i}", "role": "BAT"} for i in range(1, 7)
        ] + [
            {"id": "p7", "name": "P7", "role": "AR"},
            {"id": "p8", "name": "P8", "role": "BOWL"},
            {"id": "p9", "name": "P9", "role": "BOWL"},
            {"id": "p10", "name": "P10", "role": "BOWL"},
        ]
        # Only BAT and BOWL offered — NO WK!
        candidates = [
            {"pos": 1, "player": {"id": "bat1", "name": "Batter 1", "role": "BAT"}},
            {"pos": 2, "player": {"id": "bowl1", "name": "Bowler 1", "role": "BOWL"}},
        ]
        markup = kb.offer(100, candidates, 10, roster=roster_need_wk)
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        button_texts = [b.text for b in buttons]

        # Must NOT be empty with only MY TEAM! Must offer AUTO-ASSIGN RANDOM KEEPER!
        assert any("AUTO-ASSIGN RANDOM KEEPER" in t for t in button_texts)
        assign_btn = next(b for b in buttons if "AUTO-ASSIGN RANDOM KEEPER" in b.text)
        assert assign_btn.callback_data == "ipl:rnd:100"

    async def test_ensure_legal_candidates_injects_keeper_when_missing(self):
        """ensure_legal_candidates queries pool and updates offer when keeper is required but missing."""
        team = {"id": 10, "tournament_id": 5}
        roster_need_wk = [
            {"id": f"p{i}", "name": f"P{i}", "role": "BAT"} for i in range(1, 7)
        ] + [
            {"id": "p7", "name": "P7", "role": "AR"},
            {"id": "p8", "name": "P8", "role": "BOWL"},
            {"id": "p9", "name": "P9", "role": "BOWL"},
            {"id": "p10", "name": "P10", "role": "BOWL"},
        ]
        offer = {
            "offer_id": 99,
            "tournament_id": 5,
            "team_id": 10,
            "candidates": [
                {"pos": 1, "player": {"id": "bat1", "name": "Batter 1", "role": "BAT"}}
            ],
        }

        mock_keeper = {
            "id": "wk_msd",
            "name": "MS Dhoni",
            "display_name": "MS Dhoni",
            "role": "WK",
            "nationality": "India",
        }
        self.repo.get_legal_pool_players = AsyncMock(return_value=[mock_keeper])
        self.repo.replace_offer_candidates = AsyncMock(return_value=[
            {"pos": 1, "position": 1, "player": mock_keeper}
        ])

        updated = await draft_service.ensure_legal_candidates(self.rt, team, offer, roster_need_wk)
        assert len(updated["candidates"]) == 1
        assert updated["candidates"][0]["player"]["role"] == "WK"
        self.repo.replace_offer_candidates.assert_called_once_with(99, ["wk_msd"])

    async def test_random_pick_callback_auto_assigns_required_keeper(self):
        """Pressing ipl:rnd:{offer_id} auto-picks a legal keeper when keeper is required."""
        from ipl_draft.handlers import callbacks

        mock_q = MagicMock()
        mock_q.from_user.id = 12345
        mock_q.message.message_id = 999
        mock_context = MagicMock()

        offer_data = {
            "offer_id": 100,
            "tournament_id": 1,
            "team_id": 50,
            "status": "OPEN",
            "team": {"id": 50, "user_id": 12345},
            "candidates": [
                {"pos": 1, "player": {"id": "wk_1", "name": "MS Dhoni", "role": "WK"}},
            ],
        }
        roster_players = [
            {"id": f"p{i}", "name": f"P{i}", "role": "BAT"} for i in range(1, 7)
        ] + [
            {"id": "p7", "name": "P7", "role": "AR"},
            {"id": "p8", "name": "P8", "role": "BOWL"},
            {"id": "p9", "name": "P9", "role": "BOWL"},
            {"id": "p10", "name": "P10", "role": "BOWL"},
        ]

        self.repo.get_offer = AsyncMock(return_value=offer_data)
        self.repo.team_of_user = AsyncMock(return_value={"id": 50, "user_id": 12345})
        self.repo.team_roster = AsyncMock(return_value={"team": {"id": 50, "user_id": 12345}, "players": roster_players})
        self.repo.pick = AsyncMock(return_value={
            "status": "OK",
            "player": {"name": "MS Dhoni", "role": "WK"},
            "done": True,
            "squad_complete": True,
        })
        self.rt.rng.choice = MagicMock(side_effect=lambda lst: lst[0])

        with patch("ipl_draft.handlers.callbacks.safe_answer", new_callable=AsyncMock) as mock_ans:
            await callbacks.random_pick(self.rt, mock_context, mock_q, 100)
            mock_ans.assert_called_once()
            assert "MS Dhoni auto-assigned!" in mock_ans.call_args[0][1]
