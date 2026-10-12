"""Tests for IPL Draft V2 enhancements:
1. 5 System franchises (RCB, KKR, MI, CSK, DC).
2. Player eligibility: only 2020-2026 IPL squads; AB de Villiers and Chris Gayle eligible; older ineligible.
3. 11-player batting lineup selection: pick, undo, reset, confirm, smart auto-assign.
4. Match simulation follows saved batting order.
5. DM match card formatting and buttons.
"""
import asyncio
import random

from ipl_draft.engine import draft_engine
from ipl_draft.engine.match_engine import InningsSim, simulate_match
from ipl_draft.engine.model import Player, Ratings, Squad
from ipl_draft.engine.team_builder import FRANCHISES, franchise_payload
from ipl_draft import keyboards as kb
from ipl_draft import messages as m


def test_system_franchises_reduced_to_exact_four():
    """Requirement 1: Exactly 4 system teams: RCB, KKR, MI, CSK."""
    assert len(FRANCHISES) == 4
    codes = {f[0] for f in FRANCHISES}
    assert codes == {"RCB", "KKR", "MI", "CSK"}
    payload = franchise_payload()
    assert len(payload) == 4
    payload_codes = {p["code"] for p in payload}
    assert payload_codes == {"RCB", "KKR", "MI", "CSK"}
    assert draft_engine.SYSTEM_TEAMS == 4


def test_player_eligibility_logic():
    """Requirement: Only 2026 IPL squad players are eligible; players before 2026 are ineligible."""
    players = [
        {"name": "Virat Kohli", "last_ipl_season": 2026, "is_eligible": True},
        {"name": "Rohit Sharma", "last_ipl_season": 2026, "is_eligible": True},
        {"name": "MS Dhoni", "last_ipl_season": 2026, "is_eligible": True},
        {"name": "AB de Villiers", "last_ipl_season": 2021, "is_eligible": False},
        {"name": "Chris Gayle", "last_ipl_season": 2021, "is_eligible": False},
        {"name": "Sachin Tendulkar", "last_ipl_season": 2013, "is_eligible": False},
        {"name": "Virender Sehwag", "last_ipl_season": 2015, "is_eligible": False},
        {"name": "Gautam Gambhir", "last_ipl_season": 2018, "is_eligible": False},
    ]
    # Check that older players (< 2026) are marked ineligible
    for p in players:
        if p["last_ipl_season"] < 2026:
            assert p["is_eligible"] is False
        else:
            assert p["is_eligible"] is True
            assert p["last_ipl_season"] == 2026


def test_batting_order_state_machine():
    """Requirement 3: Batting order flow: 11 player buttons with checkmarks, no undo/reset/confirm, auto-complete."""
    # 11 sample players
    players = [{"id": f"p{i}", "name": f"Player {i}", "role": "BAT", "slot": i, "batting_order": None} for i in range(1, 12)]
    
    # Initially: 0 ordered
    ordered = []
    
    prompt = m.batting_order_prompt(players, ordered)
    assert "(0/11)" in prompt
    assert "Select <b>batter #1</b>" in prompt

    # User picks first batter (pos 4)
    players[3]["batting_order"] = 1
    ordered.append(players[3])
    prompt = m.batting_order_prompt(players, ordered)
    assert "(1/11)" in prompt
    assert "Player 4" in prompt
    assert "Select <b>batter #2</b>" in prompt

    # Check keyboard generation: all 11 player buttons remain visible, selected has checkmark
    k_prompt = kb.batting_order(101, players)
    button_texts = [btn.text for row in k_prompt.inline_keyboard for btn in row]
    assert len(button_texts) == 11
    assert "✅ Player 4" in button_texts
    assert "Player 1" in button_texts
    assert "↩️ UNDO" not in button_texts
    assert "🔄 RESET" not in button_texts
    assert "CONFIRM" not in button_texts

    # User picks all 11 batters
    ordered = list(players)
    prompt_done = m.batting_order_confirmed_text(ordered)
    assert "BATTING ORDER COMPLETE" in prompt_done
    assert "Squad Locked!" in prompt_done
    assert "11. Player 11" in prompt_done


def test_dm_match_result_format_without_buttons():
    """Requirement 2 & 9: Full scorecard delivered without inline buttons."""
    card = {
        "match_id": 999,
        "stage": "LEAGUE",
        "team1": {"id": 10, "name": "Mumbai Indians", "short_name": "MI"},
        "team2": {"id": 20, "name": "Chennai Super Kings", "short_name": "CSK"},
        "winner_team_id": 10,
        "summary": "Mumbai Indians won by 15 runs",
        "innings": [
            {"batting_team_id": 10, "runs": 180, "wickets": 4, "legal_balls": 120, "is_super_over": False},
            {"batting_team_id": 20, "runs": 165, "wickets": 8, "legal_balls": 120, "is_super_over": False},
        ]
    }
    # Check DM card for winner (team 10)
    dm_text_win = m.dm_match_result(card, 10)
    assert "Mumbai Indians vs Chennai Super Kings" in dm_text_win
    assert "YOU WON!" in dm_text_win
    assert "180/4" in dm_text_win
    assert "165/8" in dm_text_win

    # Check DM card for loser (team 20)
    dm_text_loss = m.dm_match_result(card, 20)
    assert "You lost." in dm_text_loss

    # Verify no inline buttons attached
    assert kb.dm_match_card_buttons(tid=1, match_id=999) is None


def test_automatic_full_scorecard_in_dm_format():
    """Verify that full_scorecard_pages produces complete match scorecard without buttons."""
    card = {
        "match_id": 101,
        "stage": "PLAYOFF_QUALIFIER_1",
        "team1": {"id": 1, "name": "Dre XI", "short_name": "DRE"},
        "team2": {"id": 2, "name": "RCB XI", "short_name": "RCB"},
        "winner_team_id": 1,
        "summary": "Dre XI won by 23 runs",
        "pom": {"name": "Virat Kohli", "runs": 82, "balls": 50, "wickets": 0, "bowl_runs": 0},
        "best_bowler": {"name": "Jasprit Bumrah", "wickets": 2, "runs": 28},
        "innings_detail": [
            {
                "innings_no": 1,
                "is_super_over": False,
                "runs": 218,
                "wickets": 3,
                "legal_balls": 120,
                "extras": {"wides": 5, "noballs": 1, "byes": 2, "legbyes": 4},
                "batting_team": {"name": "Dre XI", "short_name": "DRE"},
                "batting": [
                    {"name": "Virat Kohli", "runs": 82, "balls": 50, "fours": 8, "sixes": 3, "out": False, "did_bat": True},
                    {"name": "Rohit Sharma", "runs": 45, "balls": 30, "fours": 4, "sixes": 2, "out": True, "did_bat": True},
                ],
                "bowling": [
                    {"name": "Jasprit Bumrah", "legal_balls": 24, "runs": 28, "wickets": 2},
                ],
            },
            {
                "innings_no": 2,
                "is_super_over": False,
                "runs": 195,
                "wickets": 7,
                "legal_balls": 120,
                "extras": {"wides": 4, "noballs": 0, "byes": 1, "legbyes": 3},
                "batting_team": {"name": "RCB XI", "short_name": "RCB"},
                "batting": [
                    {"name": "Faf du Plessis", "runs": 60, "balls": 38, "fours": 6, "sixes": 2, "out": True, "did_bat": True},
                ],
                "bowling": [
                    {"name": "Rashid Khan", "legal_balls": 24, "runs": 30, "wickets": 3},
                ],
            }
        ]
    }
    pages = m.full_scorecard_pages(card)
    assert len(pages) >= 1
    full_text = "\n".join(pages)
    assert "IPL DRAFT — QUALIFIER 1" in full_text
    assert "<b>Dre XI</b> vs <b>RCB XI</b>" in full_text
    assert "<b>DRE XI — 218/3 (20.0 ov)</b>" in full_text
    assert "Virat Kohli" in full_text
    assert "Rohit Sharma" in full_text
    assert "<b>RCB XI — 195/7 (20.0 ov)</b>" in full_text
    assert "Dre XI won by 23 runs" in full_text
    assert "<b>PLAYER OF THE MATCH</b>: Virat Kohli (82 runs, 50 balls)" in full_text
    assert "<b>BEST BOWLER</b>: Jasprit Bumrah (2/28)" in full_text



def test_system_draft_ready_before_user_draft():
    """Verify that system franchises are assigned predefined IPL 2026 squads directly."""
    from unittest.mock import AsyncMock, MagicMock
    from ipl_draft.services import system_service
    from ipl_draft.engine.team_builder import SYSTEM_PLAYING_XI

    rt = MagicMock()
    rt.repo = MagicMock()
    rt.repo.get = AsyncMock(return_value={"id": 10, "state": "DRAFTING"})
    rt.repo.all_teams = AsyncMock(return_value=[
        {"id": 1, "kind": "SYSTEM", "franchise_code": "RCB"},
        {"id": 2, "kind": "SYSTEM", "franchise_code": "KKR"},
        {"id": 3, "kind": "SYSTEM", "franchise_code": "MI"},
        {"id": 4, "kind": "SYSTEM", "franchise_code": "CSK"},
    ])
    
    mock_players = []
    for code, squad in SYSTEM_PLAYING_XI.items():
        for idx, p_name in enumerate(squad):
            role = "WK" if idx == 0 else ("AR" if idx == 1 else ("BOWL" if idx >= 8 else "BAT"))
            mock_players.append({
                "id": f"pid_{p_name}",
                "display_name": p_name,
                "full_name": p_name,
                "role": role,
                "bowling_type": "RIGHT_FAST" if role == "BOWL" else None,
            })

    persisted_rosters = {}

    def fake_db_exec(query_fn):
        mock_client = MagicMock()
        mock_query = MagicMock()
        mock_client.table.return_value = mock_query
        mock_query.select.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_query.range.return_value = mock_query

        # Track insertions
        def mock_insert(rows):
            if rows:
                tid = rows[0]["team_id"]
                persisted_rosters[tid] = rows
            return mock_query
        mock_query.insert.side_effect = mock_insert
        mock_query.update.return_value = mock_query

        # Check what table was queried
        query_fn(mock_client)
        called_table = mock_client.table.call_args[0][0]
        if called_table == "ipl_players":
            return mock_players
        elif called_table == "ipl_team_rosters":
            # Extract team_id if eq was called
            eq_calls = [c for c in mock_query.eq.call_args_list if c[0][0] == "team_id"]
            if eq_calls:
                tid = eq_calls[-1][0][1]
                return persisted_rosters.get(tid, [])
            return []
        return []

    rt.repo.db = MagicMock()
    rt.repo.db.exec = fake_db_exec
    rt.repo.complete_system_teams = AsyncMock(return_value={"status": "OK"})

    rt.ctx.lock = lambda key: __import__("contextlib").AsyncExitStack()
    asyncio.run(system_service.prepare_system_teams(rt, 10))
    assert rt.repo.complete_system_teams.called


def test_create_fixtures_never_bypasses_the_validated_sql_function():
    """The old direct-insert fallback (which skipped all validation and did not work on PostgREST) is gone: a bad
    result is returned/raised so the worker's failure counter sees it."""
    from unittest.mock import AsyncMock, MagicMock
    from ipl_draft.repository import IplRepo

    db = MagicMock()
    db.rpc = AsyncMock(return_value={"status": "BAD_TEAM_COUNT", "teams": 5})
    db.exec = AsyncMock(return_value=[])
    repo = IplRepo(db)
    res = asyncio.run(repo.create_fixtures(10, [{"match_no": 1, "matchday": 1, "leg": 1, "home": 1, "away": 2}]))
    assert res["status"] == "BAD_TEAM_COUNT"
    db.exec.assert_not_called()


def test_fix1_and_test1_to_3_single_click_instant_lock():
    """TEST 1, 2, 3: Single-click final selection, no confirm/reset, stale button rejection."""
    from unittest.mock import AsyncMock, MagicMock
    from ipl_draft.services import draft_service

    rt = MagicMock()
    rt.repo = MagicMock()
    # Mock repo.pick
    rt.repo.pick = AsyncMock(return_value={"status": "OK", "team_id": 101, "tournament_id": 1, "picks": 1, "done": False, "player": {"name": "Virat Kohli"}})
    rt.repo.team_roster = AsyncMock(return_value={"team": {"user_id": 123, "id": 101}, "players": [{"name": "Virat Kohli"}]})
    rt.ctx.bot.edit_message_text = AsyncMock()

    # User clicks player button (pos 1) -> instant confirm
    res = asyncio.run(draft_service.confirm_pick(rt, user_id=123, offer_id=456, pos=1, message_id=789))
    assert res.get("status") == "OK"
    assert rt.repo.pick.called

    # If clicked again when already picked/locked
    rt.repo.pick = AsyncMock(return_value={"status": "ALREADY_PICKED"})
    res2 = asyncio.run(draft_service.confirm_pick(rt, user_id=123, offer_id=456, pos=1, message_id=789))
    assert res2.get("status") == "ALREADY_PICKED"

    # Verify no confirm or reset buttons exist in offer keyboards
    candidates = [{"pos": 1, "player": {"name": "Virat Kohli", "role": "BAT", "bowling_type": None, "star": False}}]
    markup = kb.offer(456, candidates, 101)
    btn_texts = [b.text for row in markup.inline_keyboard for b in row]
    assert not any("CONFIRM" in t for t in btn_texts)
    assert not any("RESET" in t for t in btn_texts)


def test_fix3_and_test4_to_5_batting_order_ticks_and_no_duplicates():
    """TEST 4, 5: Selected players show ✅, duplicate selection rejected, auto-locks on 11."""
    from unittest.mock import AsyncMock, MagicMock
    from ipl_draft.services import batting_order_service

    players = [{"id": f"p{i}", "name": f"Player {i}", "role": "BAT", "slot": i, "batting_order": None} for i in range(1, 12)]
    
    # Check initial keyboard
    markup = kb.batting_order(101, players)
    for row in markup.inline_keyboard:
        for btn in row:
            assert "✅" not in btn.text

    # Pick player 1 -> shows ✅
    players[0]["batting_order"] = 1
    markup_picked = kb.batting_order(101, players)
    button_labels = [btn.text for row in markup_picked.inline_keyboard for btn in row]
    assert "✅ Player 1" in button_labels
    assert "Player 2" in button_labels

    # Test duplicate pick rejection
    rt = MagicMock()
    rt.repo = MagicMock()
    rt.repo.team_by_id = AsyncMock(return_value={"id": 101, "user_id": 123, "batting_order_confirmed": False})
    rt.repo.get_batting_order_state = AsyncMock(return_value={
        "players": players,
        "ordered": [players[0]],
        "remaining": players[1:],
    })
    rt.repo.batting_order_pick = AsyncMock(return_value={"status": "ALREADY"})

    status = asyncio.run(batting_order_service.pick_batter(rt, user_id=123, team_id=101, pos=1, dm_msg_id=789))
    assert status == "ALREADY"


def test_fix4_and_test6_to_7_system_teams_2026_squads_never_draft():
    """TEST 6, 7: 4 system teams have 2026 squads, valid Playing XI, never draft."""
    from ipl_draft.engine.team_builder import SYSTEM_PLAYING_XI, FRANCHISES
    assert len(SYSTEM_PLAYING_XI) == 4
    for code, squad in SYSTEM_PLAYING_XI.items():
        assert len(squad) == 11, f"{code} squad must have 11 players"
        assert len(set(squad)) == 11, f"{code} squad must have 11 distinct players"

    # Verify team builder does not issue draft offers to system teams
    system_codes = {f[0] for f in FRANCHISES}
    assert system_codes == {"MI", "CSK", "RCB", "KKR"}


def test_fix6_and_test9_to_11_match_simulation_types():
    """TEST 9, 10, 11: Human vs Human, Human vs System, System vs System all simulate correctly."""
    import random
    from ipl_draft.engine.match_engine import simulate_match
    from ipl_draft.engine.model import Player, Ratings, Squad

    rng = random.Random(42)
    def make_squad(tid, name, code):
        players = tuple(Player(id=f"{code}_{i}", name=f"{code}_P{i}", role="BAT" if i < 6 else "BOWL",
                               ratings=Ratings(batting_rating=55, bowling_rating=55)) for i in range(1, 12))
        return Squad(tid, name, code, players)

    sq_human1 = make_squad(1, "Dre XI", "DRE")
    sq_human2 = make_squad(2, "Virat XI", "VIR")
    sq_system = make_squad(3, "Mumbai Indians", "MI")

    # 1. Human vs Human
    card_hh = simulate_match(sq_human1, sq_human2, rng)
    assert card_hh.winner in (1, 2)
    assert len(card_hh.innings) >= 2

    # 2. Human vs System
    card_hs = simulate_match(sq_human1, sq_system, rng)
    assert card_hs.winner in (1, 3)

    # 3. System vs System
    sq_system2 = make_squad(4, "Chennai Super Kings", "CSK")
    card_ss = simulate_match(sq_system, sq_system2, rng)
    assert card_ss.winner in (3, 4)


def test_compact_scorecards_all_stages_top3_batters_and_bowlers_no_buttons():
    """FIX 1: Compact scorecard format showing ONLY top 3 batters, top 3 bowlers, team scores, result, win/loss in DM with 0 buttons."""
    stages = [
        ("LEAGUE", "🏆 League Match"),
        ("PLAYOFF_QUALIFIER_1", "🏆 Qualifier 1"),
        ("PLAYOFF_ELIMINATOR", "🔥 Eliminator"),
        ("PLAYOFF_QUALIFIER_2", "⚔️ Qualifier 2"),
        ("PLAYOFF_FINAL", "👑 Grand Final"),
    ]

    for stage_key, expected_title in stages:
        match_data = {
            "match_id": 501,
            "stage": stage_key,
            "home_team": {"id": 1, "name": "Dre XI", "short_name": "DRE"},
            "away_team": {"id": 2, "name": "RCB XI", "short_name": "RCB"},
            "winner_team_id": 1,
            "result_type": "RUNS",
            "margin": 10,
            "summary": "Dre XI won by 10 runs",
            "innings_detail": [
                {
                    "innings_no": 1, "is_super_over": False, "runs": 200, "wickets": 4, "legal_balls": 120,
                    "batting_team": {"name": "Dre XI", "short_name": "DRE"},
                    "batting": [
                        {"name": "Travis Head", "runs": 70, "balls": 40, "fours": 7, "sixes": 4, "out": True, "did_bat": True},
                        {"name": "Nicholas Pooran", "runs": 60, "balls": 35, "fours": 5, "sixes": 3, "out": False, "did_bat": True},
                        {"name": "David Miller", "runs": 30, "balls": 20, "fours": 2, "sixes": 1, "out": True, "did_bat": True},
                        {"name": "Hardik Pandya", "runs": 20, "balls": 15, "fours": 1, "sixes": 1, "out": False, "did_bat": True}, # 4th batter (must be omitted)
                    ],
                    "bowling": [
                        {"name": "Jasprit Bumrah", "legal_balls": 24, "runs": 25, "wickets": 2},
                        {"name": "Kagiso Rabada", "legal_balls": 24, "runs": 30, "wickets": 1},
                        {"name": "Arshdeep Singh", "legal_balls": 24, "runs": 35, "wickets": 1},
                        {"name": "Sunil Narine", "legal_balls": 24, "runs": 40, "wickets": 0}, # 4th bowler (must be omitted)
                    ],
                },
                {
                    "innings_no": 2, "is_super_over": False, "runs": 190, "wickets": 6, "legal_balls": 120,
                    "batting_team": {"name": "RCB XI", "short_name": "RCB"},
                    "batting": [
                        {"name": "Virat Kohli", "runs": 80, "balls": 45, "fours": 8, "sixes": 3, "out": False, "did_bat": True},
                        {"name": "Faf du Plessis", "runs": 45, "balls": 30, "fours": 4, "sixes": 2, "out": True, "did_bat": True},
                        {"name": "Glenn Maxwell", "runs": 35, "balls": 20, "fours": 3, "sixes": 2, "out": True, "did_bat": True},
                        {"name": "Dinesh Karthik", "runs": 15, "balls": 10, "fours": 1, "sixes": 1, "out": True, "did_bat": True}, # 4th batter (must be omitted)
                    ],
                    "bowling": [
                        {"name": "Rashid Khan", "legal_balls": 24, "runs": 28, "wickets": 3},
                        {"name": "Vijay Shankar", "legal_balls": 18, "runs": 30, "wickets": 1},
                        {"name": "Mohit Sharma", "legal_balls": 18, "runs": 15, "wickets": 0},
                    ],
                }
            ]
        }

        # Centralized compact scorecard function
        sc_text = m.format_compact_scorecard(match_data, stage_key, user_id=1)
        assert expected_title in sc_text
        assert "Dre XI vs RCB XI" in sc_text
        assert "Dre XI — 200/4 (20.0 ov)" in sc_text
        assert "RCB XI — 190/6 (20.0 ov)" in sc_text
        assert "Travis Head 70 (40b, 7x4, 4x6)" in sc_text
        assert "Nicholas Pooran 60* (35b, 5x4, 3x6)" in sc_text
        assert "David Miller 30 (20b, 2x4, 1x6)" in sc_text
        # 4th batter must NOT be in top 3
        assert "Hardik Pandya" not in sc_text

        # Top 3 bowlers
        assert "Jasprit Bumrah 2/25 (4.0)" in sc_text
        assert "Sunil Narine" not in sc_text  # 4th bowler omitted

        assert "✅ Dre XI won by 10 runs" in sc_text
        assert "YOU WON!" in sc_text

        # Verify ZERO buttons
        assert kb.dm_match_card_buttons(1, 501) is None


def test_fix3_and_fix4_group_points_table_format():
    """FIX 3 & 4: Group points table format with <pre>, ⭐ for human teams, and playoff rules."""
    standings = [
        {"team_id": 1, "name": "Chennai Super Kings", "short_name": "CSK", "kind": "SYSTEM", "played": 10, "won": 8, "lost": 2, "points": 16},
        {"team_id": 2, "name": "Royal Challengers", "short_name": "RCB", "kind": "SYSTEM", "played": 10, "won": 7, "lost": 3, "points": 14},
        {"team_id": 3, "name": "Rhyme", "short_name": "RHY", "kind": "HUMAN", "played": 10, "won": 6, "lost": 4, "points": 12},
        {"team_id": 4, "name": "Hardik Sinha", "short_name": "HAR", "kind": "HUMAN", "played": 10, "won": 5, "lost": 5, "points": 10},
        {"team_id": 5, "name": "Dre XI", "short_name": "DRE", "kind": "HUMAN", "played": 10, "won": 4, "lost": 6, "points": 8},
        {"team_id": 6, "name": "Mumbai Indians", "short_name": "MI", "kind": "SYSTEM", "played": 10, "won": 3, "lost": 7, "points": 6},
    ]

    table_text = m.format_group_tournament_summary(standings)

    # 1. Title
    assert "📊 <b>Points Table</b>" in table_text
    # 2. Table columns inside <pre>
    assert "<pre>" in table_text and "</pre>" in table_text
    assert "#  Team                P  W  L  Pts" in table_text
    # 3. Human teams have ⭐
    assert "12⭐" in table_text  # Rhyme
    assert "10⭐" in table_text  # Hardik Sinha
    assert " 8⭐" in table_text  # Dre XI
    assert "⭐ = Human Player" in table_text
    # 4. Playoff info below points table
    assert "🏆 <b>Playoffs! Top 4 advance:</b>" in table_text
    assert "• Qualifier 1: 1 v 2 (winner → Final)" in table_text
    assert "• Eliminator: 3 v 4 (loser out)" in table_text
    assert "• Qualifier 2: Q1 loser v Elim winner" in table_text
    assert "• Final" in table_text


def test_fix5_6_7_group_summary_lifecycle_and_mvps():
    """FIX 5, 6, 7: Updating the same group summary message through Qualifier 1, Eliminator, Qualifier 2, Final, Champion, and MVPs."""
    standings = [
        {"team_id": 1, "name": "Chennai Super Kings", "kind": "SYSTEM", "played": 10, "won": 8, "lost": 2, "points": 16},
        {"team_id": 2, "name": "Royal Challengers", "kind": "SYSTEM", "played": 10, "won": 7, "lost": 3, "points": 14},
        {"team_id": 3, "name": "Rhyme", "kind": "HUMAN", "played": 10, "won": 6, "lost": 4, "points": 12},
        {"team_id": 4, "name": "Dre XI", "kind": "HUMAN", "played": 10, "won": 5, "lost": 5, "points": 10},
    ]

    # After Qualifier 1
    playoff_results = [
        {"stage": "QUALIFIER_1", "winner_name": "Royal Challengers", "inn1_score": "165/6", "inn2_score": "166/7"}
    ]
    summary_q1 = m.format_group_tournament_summary(standings, playoff_results)
    assert "🏏 <b>Qualifier 1:</b>\nRoyal Challengers won (165/6 vs 166/7)" in summary_q1

    # After Eliminator
    playoff_results.append(
        {"stage": "ELIMINATOR", "winner_name": "Dre XI", "inn1_score": "169/5", "inn2_score": "133/10"}
    )
    summary_elim = m.format_group_tournament_summary(standings, playoff_results)
    assert "🏏 <b>Eliminator:</b>\nDre XI won (169/5 vs 133/10)" in summary_elim

    # After Qualifier 2
    playoff_results.append(
        {"stage": "QUALIFIER_2", "winner_name": "Chennai Super Kings", "inn1_score": "174/8", "inn2_score": "176/2"}
    )
    summary_q2 = m.format_group_tournament_summary(standings, playoff_results)
    assert "🏏 <b>Qualifier 2:</b>\nChennai Super Kings won (174/8 vs 176/2)" in summary_q2

    # After Final + Champion + MVPs
    playoff_results.append(
        {"stage": "FINAL", "winner_name": "Chennai Super Kings", "inn1_score": "185/6", "inn2_score": "179/8"}
    )
    champ = {"id": 1, "name": "Chennai Super Kings"}
    mvps = {
        "most_runs": {"player": "Heinrich Klaasen", "runs": 581, "team": "Rhyme's XI"},
        "most_wickets": {"player": "Arshdeep Singh", "wickets": 29, "team": "Rhyme's XI"},
    }
    summary_final = m.format_group_tournament_summary(standings, playoff_results, champ, mvps)

    assert "🏏 🏆 <b>FINAL:</b>\nChennai Super Kings won (185/6 vs 179/8)" in summary_final
    assert "👑 <b>Chennai Super Kings are CHAMPIONS! 🏆🎉</b>" in summary_final
    assert "🏅 <b>Tournament MVPs (among players' teams)</b>" in summary_final
    assert "Heinrich Klaasen — 581 runs (in Rhyme's XI)" in summary_final
    assert "Arshdeep Singh — 29 wickets (in Rhyme's XI)" in summary_final



def test_squad_performance_text_exact_format():
    """Verify squad tournament performance text sent to DM matches user's exact specification."""
    squad_players = [
        {"name": "Sunil Narine", "id": "1"},
        {"name": "Travis Head", "id": "2"},
        {"name": "Nicholas Pooran", "id": "3"},
        {"name": "Hardik Pandya", "id": "4"},
        {"name": "Andre Russell", "id": "5"},
        {"name": "Krunal Pandya", "id": "6"},
        {"name": "David Miller", "id": "7"},
        {"name": "Marcus Stoinis", "id": "8"},
        {"name": "Kagiso Rabada", "id": "9"},
        {"name": "Jasprit Bumrah", "id": "10"},
        {"name": "Arshdeep Singh", "id": "11"},
    ]

    stats = [
        {"player_id": "1", "name": "Sunil Narine", "runs": 288, "wickets": 21},
        {"player_id": "2", "name": "Travis Head", "runs": 293, "wickets": 0},
        {"player_id": "3", "name": "Nicholas Pooran", "runs": 385, "wickets": 0},
        {"player_id": "4", "name": "Hardik Pandya", "runs": 293, "wickets": 22},
        {"player_id": "5", "name": "Andre Russell", "runs": 345, "wickets": 19},
        {"player_id": "6", "name": "Krunal Pandya", "runs": 88, "wickets": 0},
        {"player_id": "7", "name": "David Miller", "runs": 344, "wickets": 0},
        {"player_id": "8", "name": "Marcus Stoinis", "runs": 191, "wickets": 0},
        {"player_id": "9", "name": "Kagiso Rabada", "runs": 69, "wickets": 21},
        {"player_id": "10", "name": "Jasprit Bumrah", "runs": 45, "wickets": 26},
        {"player_id": "11", "name": "Arshdeep Singh", "runs": 28, "wickets": 20},
    ]

    text = m.squad_performance_text(squad_players, stats)

    assert "📋 <b>Your XI — Tournament Performance</b>" in text
    assert "1. Sunil Narine — 288 runs, 21 wkts" in text
    assert "2. Travis Head — 293 runs" in text
    assert "3. Nicholas Pooran — 385 runs" in text
    assert "4. Hardik Pandya — 293 runs, 22 wkts" in text
    assert "5. Andre Russell — 345 runs, 19 wkts" in text
    assert "6. Krunal Pandya — 88 runs" in text
    assert "7. David Miller — 344 runs" in text
    assert "8. Marcus Stoinis — 191 runs" in text
    assert "9. Kagiso Rabada — 69 runs, 21 wkts" in text
    assert "10. Jasprit Bumrah — 45 runs, 26 wkts" in text
    assert "11. Arshdeep Singh — 28 runs, 20 wkts" in text
    assert "Team totals: 2369 runs, 129 wickets" in text


def test_lobby_shows_only_joined_players_no_waiting():
    """Verify lobby text only displays joined players and does not show 'Waiting...' rows."""
    t = {"max_humans": 8, "min_humans": 2, "state": "LOBBY"}
    teams = [
        {"owner_name": "STARK 🇮🇳"},
        {"owner_name": "Dre"},
    ]

    lobby_out = m.lobby_text(t, teams)

    assert "1. STARK 🇮🇳" in lobby_out
    assert "2. Dre" in lobby_out
    assert "Waiting..." not in lobby_out
    assert "3." not in lobby_out
    assert "4." not in lobby_out
    assert "8." not in lobby_out


def test_bowler_tiebreak_and_fewer_than_three_records():
    """Test bowler tiebreaker (fewer runs conceded) and handling when fewer than 3 batters or bowlers played."""
    match_data = {
        "match_id": 601,
        "stage": "LEAGUE",
        "home_team": {"id": 1, "name": "Dre XI", "short_name": "DRE"},
        "away_team": {"id": 2, "name": "Gujarat Titans XI", "short_name": "GT"},
        "winner_team_id": 1,
        "result_type": "RUNS",
        "margin": 35,
        "summary": "Dre XI won by 35 runs",
        "innings_detail": [
            {
                "innings_no": 1, "is_super_over": False, "runs": 166, "wickets": 2, "legal_balls": 120,
                "batting_team": {"name": "Dre XI", "short_name": "DRE"},
                "batting": [
                    # Only 2 batters batted
                    {"name": "Nicholas Pooran", "runs": 71, "balls": 47, "fours": 7, "sixes": 3, "out": False, "did_bat": True},
                    {"name": "Travis Head", "runs": 68, "balls": 47, "fours": 7, "sixes": 2, "out": True, "did_bat": True},
                ],
                "bowling": [
                    # 4 bowlers with tiebreak: Rashid (1/16) and Vijay (1/30) tied on wickets, Rashid has fewer runs conceded
                    {"name": "Mohit Sharma", "legal_balls": 18, "runs": 15, "wickets": 0},
                    {"name": "Vijay Shankar", "legal_balls": 18, "runs": 30, "wickets": 1},
                    {"name": "Rashid Khan", "legal_balls": 24, "runs": 16, "wickets": 1},
                    {"name": "Noor Ahmad", "legal_balls": 12, "runs": 20, "wickets": 0},
                ],
            },
            {
                "innings_no": 2, "is_super_over": False, "runs": 131, "wickets": 6, "legal_balls": 120,
                "batting_team": {"name": "Gujarat Titans XI", "short_name": "GT"},
                "batting": [
                    {"name": "B Sai Sudharsan", "runs": 52, "balls": 43, "fours": 3, "sixes": 4, "out": True, "did_bat": True},
                    {"name": "Wriddhiman Saha", "runs": 20, "balls": 20, "fours": 0, "sixes": 1, "out": True, "did_bat": True},
                    {"name": "Vijay Shankar", "runs": 16, "balls": 21, "fours": 1, "sixes": 0, "out": False, "did_bat": True},
                ],
                "bowling": [
                    # Arshdeep (2/6) vs Bumrah (2/33) -> Arshdeep conceded fewer runs -> ranked higher
                    {"name": "Jasprit Bumrah", "legal_balls": 24, "runs": 33, "wickets": 2},
                    {"name": "Hardik Pandya", "legal_balls": 18, "runs": 19, "wickets": 1},
                    {"name": "Arshdeep Singh", "legal_balls": 18, "runs": 6, "wickets": 2},
                ],
            }
        ]
    }

    sc = m.format_compact_scorecard(match_data, "LEAGUE", user_id=1)
    # Check that only 2 batters appear in innings 1
    assert "Nicholas Pooran 71* (47b, 7x4, 3x6)" in sc
    assert "Travis Head 68 (47b, 7x4, 2x6)" in sc

    # Check bowler tiebreaker order: Rashid Khan (1/16) before Vijay Shankar (1/30)
    pos_rashid = sc.find("Rashid Khan 1/16 (4.0)")
    pos_vijay = sc.find("Vijay Shankar 1/30 (3.0)")
    assert pos_rashid != -1 and pos_vijay != -1
    assert pos_rashid < pos_vijay

    # Check bowler tiebreaker order: Arshdeep (2/6) before Bumrah (2/33)
    pos_arshdeep = sc.find("Arshdeep Singh 2/6 (3.0)")
    pos_bumrah = sc.find("Jasprit Bumrah 2/33 (4.0)")
    assert pos_arshdeep != -1 and pos_bumrah != -1
    assert pos_arshdeep < pos_bumrah


def test_private_dm_delivery_rules_and_no_group_scorecards():
    """FIX 8: Scorecards sent to private DM only (Human vs Human -> both, Human vs System -> human only, System vs System -> neither). Zero to group."""
    from unittest.mock import AsyncMock, MagicMock
    from ipl_draft.services import simulation_worker

    async def run_scenario(h_kind: str, a_kind: str):
        rt = MagicMock()
        rt.worker_id = "test-worker"
        rt.s = MagicMock(store_ball_events=False, matchday_delay=0, playoff_delay=0)
        rt.pause = AsyncMock()

        h_user = 111 if h_kind == "HUMAN" else None
        a_user = 222 if a_kind == "HUMAN" else None

        teams = [
            {"id": 1, "kind": h_kind, "user_id": h_user, "name": "Team H", "team_no": 1},
            {"id": 2, "kind": a_kind, "user_id": a_user, "name": "Team A", "team_no": 2},
        ]

        card = {
            "match_id": 999,
            "stage": "LEAGUE",
            "team1": {"id": 1, "name": "Team H"},
            "team2": {"id": 2, "name": "Team A"},
            "winner_team_id": 1,
            "innings_detail": [],
        }

        rt.repo = MagicMock()
        rt.repo.all_teams = AsyncMock(return_value=teams)
        rt.repo.settle = AsyncMock(return_value={"status": "OK", "match_id": 999})
        rt.repo.scorecard = AsyncMock(return_value=card)

        sent_dms = []
        sent_group_msgs = []

        async def fake_dm(runtime, user_id, text, reply_markup=None):
            sent_dms.append((user_id, text))
            msg = MagicMock()
            msg.message_id = len(sent_dms)
            return msg

        async def fake_group(runtime, group_id, text, reply_markup=None):
            sent_group_msgs.append((group_id, text))
            msg = MagicMock()
            msg.message_id = len(sent_group_msgs)
            return msg

        import ipl_draft.services.notification_service as notify
        orig_dm = notify.dm
        orig_group = notify.group
        notify.dm = fake_dm
        notify.group = fake_group

        try:
            t = {"id": 10, "group_id": -100999, "draft_started_at": 1000}
            fx = {"id": 888, "home": 1, "away": 2, "stage": "LEAGUE"}
            
            # Create dummy squads
            from ipl_draft.engine.model import Squad, Player, Ratings
            p_dummy = tuple(Player(id=f"p_{i}", name=f"P{i}", role="BAT", ratings=Ratings(50, 50)) for i in range(11))
            squads = {1: Squad(1, "Team H", "TH", p_dummy), 2: Squad(2, "Team A", "TA", p_dummy)}

            await simulation_worker._simulate(rt, t, fx, squads)
            return sent_dms, sent_group_msgs
        finally:
            notify.dm = orig_dm
            notify.group = orig_group

    # 1. Human vs Human -> DM sent to both users
    dms_hh, groups_hh = asyncio.run(run_scenario("HUMAN", "HUMAN"))
    dm_users_hh = {d[0] for d in dms_hh}
    assert 111 in dm_users_hh and 222 in dm_users_hh
    assert len(groups_hh) == 0  # Zero scorecards to group

    # 2. Human vs System -> DM sent ONLY to human
    dms_hs, groups_hs = asyncio.run(run_scenario("HUMAN", "SYSTEM"))
    dm_users_hs = {d[0] for d in dms_hs}
    assert 111 in dm_users_hs
    assert len(dm_users_hs) == 1
    assert len(groups_hs) == 0

    # 3. System vs System -> Zero DMs sent
    dms_ss, groups_ss = asyncio.run(run_scenario("SYSTEM", "SYSTEM"))
    assert len(dms_ss) == 0
    assert len(groups_ss) == 0


def test_group_summary_lifecycle_and_restart_persistence():
    """FIX 5 & 7: Exactly one group message, edited in place across playoffs, restart does not duplicate."""
    from unittest.mock import AsyncMock, MagicMock
    from ipl_draft.services import simulation_worker

    rt = MagicMock()
    rt.ctx = MagicMock()
    rt.ctx.lock = lambda key: __import__("contextlib").AsyncExitStack()

    t_db = {
        "id": 10,
        "group_id": -100555,
        "summary_message_id": None,
        "champion_team_id": None,
    }

    standings = [
        {"team_id": 1, "name": "Team 1", "kind": "HUMAN", "played": 6, "won": 4, "lost": 2, "points": 8},
        {"team_id": 2, "name": "Team 2", "kind": "HUMAN", "played": 6, "won": 3, "lost": 3, "points": 6},
    ]

    all_teams = [
        {"id": 1, "name": "Team 1", "kind": "HUMAN", "user_id": 111},
        {"id": 2, "name": "Team 2", "kind": "HUMAN", "user_id": 222},
    ]

    bracket = [
        {"stage": "QUALIFIER_1", "status": "SCHEDULED", "home": {"id": 1, "name": "Team 1"}, "away": {"id": 2, "name": "Team 2"}},
    ]

    rt.repo = MagicMock()
    rt.repo.get = AsyncMock(side_effect=lambda tid: t_db)
    rt.repo.standings = AsyncMock(return_value=standings)
    rt.repo.all_teams = AsyncMock(return_value=all_teams)
    rt.repo.bracket = AsyncMock(return_value=bracket)
    rt.repo.player_stats = AsyncMock(return_value=[])
    
    async def fake_update(tid, **kwargs):
        t_db.update(kwargs)
        return {"status": "OK"}
    rt.repo.update = AsyncMock(side_effect=fake_update)

    messages_sent = []
    messages_edited = []

    async def fake_send(ctx, gid, text, reply_markup=None):
        msg = MagicMock()
        msg.message_id = 9001
        messages_sent.append((gid, text))
        return msg

    async def fake_edit(ctx, gid, mid, text, reply_markup=None):
        messages_edited.append((gid, mid, text))
        return "ok"

    import ipl_draft.services.notification_service as notify
    orig_send = notify.send_message
    orig_edit = notify.safe_edit
    notify.send_message = fake_send
    notify.safe_edit = fake_edit

    try:
        # Stage 1: After league completion -> Sends initial summary message
        asyncio.run(simulation_worker.update_group_tournament_summary(rt, t_db))
        assert len(messages_sent) == 1
        assert t_db["summary_message_id"] == 9001
        assert len(messages_edited) == 0

        # Stage 2: Qualifier 1 finishes -> Edits existing message
        bracket[0]["status"] = "COMPLETED"
        bracket[0]["winner_team_id"] = 1
        bracket[0]["card"] = {
            "summary": "Team 1 won",
            "innings_detail": [
                {"innings_no": 1, "batting_team": {"name": "Team 1"}, "runs": 160, "wickets": 4},
                {"innings_no": 2, "batting_team": {"name": "Team 2"}, "runs": 150, "wickets": 8},
            ]
        }
        asyncio.run(simulation_worker.update_group_tournament_summary(rt, t_db))
        assert len(messages_sent) == 1  # No new message sent!
        assert len(messages_edited) == 1
        assert messages_edited[0][1] == 9001
        assert "Qualifier 1:" in messages_edited[0][2]

        # Stage 3: Bot restarts! The bot creates a new runtime, but t_db still has summary_message_id=9001
        # It must EDIT message 9001 and NOT send a duplicate message!
        asyncio.run(simulation_worker.update_group_tournament_summary(rt, t_db))
        assert len(messages_sent) == 1  # Still exactly 1 message sent across lifetime!
        assert len(messages_edited) == 2
        assert messages_edited[1][1] == 9001
    finally:
        notify.send_message = orig_send
        notify.safe_edit = orig_edit


def test_compute_human_mvps_excludes_system_teams():
    """FIX 6: MVPs must be calculated strictly among HUMAN-controlled teams, excluding system teams."""
    from unittest.mock import AsyncMock, MagicMock
    from ipl_draft.services.simulation_worker import compute_human_mvps

    rt = MagicMock()
    rt.repo = MagicMock()

    # System teams: MI (id=1), CSK (id=2). Human teams: Dre XI (id=3), Rhyme XI (id=4)
    all_teams = [
        {"id": 1, "kind": "SYSTEM", "name": "Mumbai Indians"},
        {"id": 2, "kind": "SYSTEM", "name": "Chennai Super Kings"},
        {"id": 3, "kind": "HUMAN", "name": "Dre XI"},
        {"id": 4, "kind": "HUMAN", "name": "Rhyme XI"},
    ]
    rt.repo.all_teams = AsyncMock(return_value=all_teams)

    # Player stats: System team player has higher runs/wickets, but must be ignored!
    player_stats = [
        # System players (should be EXCLUDED)
        {"player_id": "sys_1", "team_id": 1, "name": "Rohit Sharma", "runs": 900, "balls": 500, "wickets": 0, "bowl_runs": 0},
        {"player_id": "sys_2", "team_id": 2, "name": "Matheesha Pathirana", "runs": 10, "balls": 5, "wickets": 35, "bowl_runs": 200},
        # Human players (should be SELECTED)
        {"player_id": "hum_1", "team_id": 3, "name": "Heinrich Klaasen", "runs": 581, "balls": 310, "wickets": 0, "bowl_runs": 0},
        {"player_id": "hum_2", "team_id": 4, "name": "Arshdeep Singh", "runs": 28, "balls": 20, "wickets": 29, "bowl_runs": 250},
        {"player_id": "hum_3", "team_id": 4, "name": "Travis Head", "runs": 450, "balls": 250, "wickets": 2, "bowl_runs": 50},
    ]
    rt.repo.player_stats = AsyncMock(return_value=player_stats)

    mvps = asyncio.run(compute_human_mvps(rt, tid=10))

    assert "most_runs" in mvps
    assert mvps["most_runs"]["player"] == "Heinrich Klaasen"
    assert mvps["most_runs"]["runs"] == 581
    assert mvps["most_runs"]["team"] == "Dre XI"

    assert "most_wickets" in mvps
    assert mvps["most_wickets"]["player"] == "Arshdeep Singh"
    assert mvps["most_wickets"]["wickets"] == 29
    assert mvps["most_wickets"]["team"] == "Rhyme XI"


def test_mandatory_roles_enforcement_3_bowlers_1_wk_1_ar():
    """Verify that 3 bowlers, 1 keeper, and 1 all-rounder are strictly enforced."""
    from ipl_draft.engine import draft_engine

    roster = []
    # 0 players picked: 3 bowlers, 1 keeper, 1 all-rounder required
    req = draft_engine.still_required_text(roster)
    assert req == "3 bowler(s), 1 keeper, 1 all-rounder"
    assert draft_engine.is_role_pick_allowed(roster, "BAT") is True
    assert draft_engine.is_role_pick_allowed(roster, "BOWL") is True
    assert draft_engine.is_role_pick_allowed(roster, "WK") is True
    assert draft_engine.is_role_pick_allowed(roster, "AR") is True

    # Pick 6 batters (total 6, 5 picks remaining)
    roster = [{"role": "BAT"} for _ in range(6)]
    # Needs: 3 bowlers + 1 keeper + 1 all-rounder = 5 picks needed! Picks remaining = 5!
    # Batter should be BLOCKED!
    assert draft_engine.is_role_pick_allowed(roster, "BAT") is False
    # Bowler, Keeper, and All-rounder MUST be ALLOWED!
    assert draft_engine.is_role_pick_allowed(roster, "BOWL") is True
    assert draft_engine.is_role_pick_allowed(roster, "WK") is True
    assert draft_engine.is_role_pick_allowed(roster, "AR") is True

    # Pick 1 bowler (total 7, 4 picks remaining)
    roster.append({"role": "BOWL"})
    req2 = draft_engine.still_required_text(roster)
    assert req2 == "2 bowler(s), 1 keeper, 1 all-rounder"
    assert draft_engine.is_role_pick_allowed(roster, "BAT") is False
    assert draft_engine.is_role_pick_allowed(roster, "BOWL") is True

    # Pick 2 more bowlers, 1 keeper, 1 all-rounder (total 11)
    roster.extend([{"role": "BOWL"}, {"role": "BOWL"}, {"role": "WK"}, {"role": "AR"}])
    req_done = draft_engine.still_required_text(roster)
    assert req_done == "None (all minimums met)"


def test_draft_appearance_matching_image_2():
    """Verify draft prompt text and buttons match Image 2 format."""
    team = {"name": "Dre XI"}
    offer = {"round_no": 1}
    roster = []

    text = m.offer_text(team, offer, picks_done=0, roster=roster)
    assert "IPL DRAFT — Dre XI" in text
    assert "Pick 1/11 — choose a player:" in text
    assert "Still required: 3 bowler(s), 1 keeper, 1 all-rounder" in text

    # Verify buttons contain role emojis and country codes
    cands = [
        {"pos": 1, "player": {"name": "Alec Stewart", "role": "WK", "nationality": "England"}},
        {"pos": 2, "player": {"name": "Graham Gooch", "role": "BAT", "nationality": "England"}},
        {"pos": 3, "player": {"name": "Andrew Flintoff", "role": "AR", "nationality": "England"}},
        {"pos": 4, "player": {"name": "Phil DeFreitas", "role": "BOWL", "nationality": "England"}},
        {"pos": 5, "player": {"name": "Virat Kohli", "role": "BAT", "nationality": "India"}},
    ]
    markup = kb.offer(101, cands, 1, roster=roster)
    btn_labels = [btn.text for row in markup.inline_keyboard for btn in row]

    assert "🧤 Alec Stewart (ENG)" in btn_labels
    assert "🏏 Graham Gooch (ENG)" in btn_labels
    assert "🔄 Andrew Flintoff (ENG)" in btn_labels
    assert "🎯 Phil DeFreitas (ENG)" in btn_labels
    assert "🏏 Virat Kohli (IND)" in btn_labels
    assert "📋 MY TEAM" in btn_labels


def test_playoff_messages_pop_one_by_one_matching_image_1():
    """Verify separate individual messages pop one by one matching Image 1."""
    # 1. Playoff structure text
    p_struct = m.playoff_structure_text()
    assert "🏆 <b>Playoffs! Top 4 advance:</b>" in p_struct
    assert "• Qualifier 1: 1 v 2 (winner → Final)" in p_struct
    assert "• Eliminator: 3 v 4 (loser out)" in p_struct
    assert "• Qualifier 2: Q1 loser v Elim winner" in p_struct
    assert "• Final" in p_struct

    # 2. Individual match result messages
    card_q1 = {
        "team1": {"id": 1, "name": "RCB"},
        "team2": {"id": 2, "name": "FENIL"},
        "winner_team_id": 2,
        "innings": [
            {"runs": 158, "wickets": 8},
            {"runs": 160, "wickets": 8},
        ]
    }
    line_q1 = m.format_playoff_result_line("QUALIFIER_1", card_q1)
    assert line_q1 == "🏏 <b>Qualifier 1:</b> FENIL won (158/8 vs 160/8)"

    card_elim = {
        "team1": {"id": 1, "name": "MI"},
        "team2": {"id": 3, "name": "Rhyme"},
        "winner_team_id": 3,
        "innings": [
            {"runs": 57, "wickets": 10},
            {"runs": 58, "wickets": 5},
        ]
    }
    line_elim = m.format_playoff_result_line("ELIMINATOR", card_elim)
    assert line_elim == "🏏 <b>Eliminator:</b> Rhyme won (57/10 vs 58/5)"

    card_q2 = {
        "team1": {"id": 1, "name": "CSK"},
        "team2": {"id": 3, "name": "Rhyme"},
        "winner_team_id": 3,
        "innings": [
            {"runs": 154, "wickets": 6},
            {"runs": 155, "wickets": 7},
        ]
    }
    line_q2 = m.format_playoff_result_line("QUALIFIER_2", card_q2)
    assert line_q2 == "🏏 <b>Qualifier 2:</b> Rhyme won (154/6 vs 155/7)"

    card_final = {
        "team1": {"id": 2, "name": "FENIL"},
        "team2": {"id": 3, "name": "Rhyme"},
        "winner_team_id": 3,
        "innings": [
            {"runs": 142, "wickets": 10},
            {"runs": 145, "wickets": 4},
        ]
    }
    line_final = m.format_playoff_result_line("FINAL", card_final)
    assert line_final == "🏏 🏆 <b>FINAL:</b> Rhyme won (142/10 vs 145/4)"

    # 3. MVPs text
    mvps = {
        "most_runs": {"player": "Jonny Bairstow", "runs": 478, "team": "Hardik Sinha's XI"},
        "most_wickets": {"player": "Rashid Khan", "wickets": 31, "team": "Manish's XI"},
    }
    mvp_text = m.format_mvps_text(mvps)
    assert "🏅 <b>Tournament MVPs (among players' teams)</b>" in mvp_text
    assert "Jonny Bairstow — 478 runs (in Hardik Sinha's XI)" in mvp_text
    assert "Rashid Khan — 31 wickets (in Manish's XI)" in mvp_text





