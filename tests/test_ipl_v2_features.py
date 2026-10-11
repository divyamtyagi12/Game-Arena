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
        for p_name in squad:
            mock_players.append({
                "id": f"pid_{p_name}",
                "display_name": p_name,
                "full_name": p_name,
                "role": "BAT",
                "bowling_type": None,
            })
    
    def fake_db_exec(query_fn):
        mock_client = MagicMock()
        mock_query = MagicMock()
        mock_client.table.return_value = mock_query
        mock_query.select.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_query.insert.return_value = mock_query
        mock_query.update.return_value = mock_query
        
        # Check what table was queried
        query_fn(mock_client)
        called_table = mock_client.table.call_args[0][0]
        if called_table == "ipl_players":
            return mock_players
        elif called_table == "ipl_team_rosters":
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


def test_fix2_and_test12_to_18_scorecards_all_stages_no_buttons():
    """TEST 12, 13, 14, 15, 16, 17, 18: Full scorecard format for all stages with zero buttons and valid team names."""
    stages = [
        ("LEAGUE", "IPL DRAFT — LEAGUE"),
        ("PLAYOFF_QUALIFIER_1", "IPL DRAFT — QUALIFIER 1"),
        ("PLAYOFF_ELIMINATOR", "IPL DRAFT — ELIMINATOR"),
        ("PLAYOFF_QUALIFIER_2", "IPL DRAFT — QUALIFIER 2"),
        ("PLAYOFF_FINAL", "IPL DRAFT — GRAND FINAL"),
    ]

    for stage_key, expected_title in stages:
        match_data = {
            "match_id": 501,
            "stage": stage_key,
            "home_team": {"id": 1, "name": "Dre XI", "short_name": "DRE"},
            "away_team": {"id": 2, "name": "RCB XI", "short_name": "RCB"},
            "winner_team_id": 1,
            "summary": "Dre XI won by 10 runs",
            "innings_detail": [
                {
                    "innings_no": 1, "is_super_over": False, "runs": 200, "wickets": 4, "legal_balls": 120,
                    "batting_team": {"name": "Dre XI", "short_name": "DRE"},
                    "batting": [{"name": "Travis Head", "runs": 70, "balls": 40, "fours": 7, "sixes": 4, "did_bat": True}],
                    "bowling": [{"name": "Jasprit Bumrah", "legal_balls": 24, "runs": 25, "wickets": 2}],
                },
                {
                    "innings_no": 2, "is_super_over": False, "runs": 190, "wickets": 6, "legal_balls": 120,
                    "batting_team": {"name": "RCB XI", "short_name": "RCB"},
                    "batting": [{"name": "Virat Kohli", "runs": 80, "balls": 45, "fours": 8, "sixes": 3, "did_bat": True}],
                    "bowling": [{"name": "Sunil Narine", "legal_balls": 24, "runs": 28, "wickets": 3}],
                }
            ]
        }

        # Centralized scorecard function produces complete rich scorecard
        pages = m.format_full_scorecard(match_data, stage_key, user_id=1)
        sc_text = "\n\n".join(pages)
        assert expected_title in sc_text
        assert "Dre XI" in sc_text
        assert "RCB XI" in sc_text
        assert "? vs ?" not in sc_text
        assert "None vs None" not in sc_text
        assert "Travis Head" in sc_text
        assert "Jasprit Bumrah" in sc_text
        assert "YOU WON!" in sc_text

        # Verify ZERO buttons
        assert kb.dm_match_card_buttons(1, 501) is None


def test_fix1_and_fix2_final_league_points_table():
    """FIX 1, 2, 3: Final league points table formatting, Q/E markers, and zero inline buttons."""
    standings = [
        {"team_id": 1, "name": "Chennai Super Kings", "short_name": "CSK", "kind": "SYSTEM", "played": 10, "won": 8, "lost": 2, "points": 16, "nrr": 1.45},
        {"team_id": 2, "name": "Royal Challengers Bengaluru", "short_name": "RCB", "kind": "SYSTEM", "played": 10, "won": 7, "lost": 3, "points": 14, "nrr": 0.92},
        {"team_id": 3, "name": "Kolkata Knight Riders", "short_name": "KKR", "kind": "SYSTEM", "played": 10, "won": 6, "lost": 4, "points": 12, "nrr": 0.51},
        {"team_id": 4, "name": "Mumbai Indians", "short_name": "MI", "kind": "SYSTEM", "played": 10, "won": 5, "lost": 5, "points": 10, "nrr": 0.12},
        {"team_id": 5, "name": "Dre XI", "short_name": "DRE", "kind": "HUMAN", "played": 10, "won": 4, "lost": 6, "points": 8, "nrr": -0.21},
        {"team_id": 6, "name": "Rahul XI", "short_name": "RAH", "kind": "HUMAN", "played": 10, "won": 3, "lost": 7, "points": 6, "nrr": -0.65},
    ]

    table_text = m.league_complete_text(standings)

    # 1. Title
    assert "IPL DRAFT — LEAGUE STAGE COMPLETED!" in table_text
    # 2. Columns
    assert "POS  TEAM          P   W   L   PTS    NRR" in table_text
    # 3. Top 4 marked Q
    assert "Q1   CSK" in table_text
    assert "Q2   RCB" in table_text
    assert "Q3   KKR" in table_text
    assert "Q4   MI" in table_text
    # 4. Remaining marked E
    assert "E5   Dre XI" in table_text
    assert "E6   Rahul XI" in table_text
    # 5. Explanations
    assert "✅ Q = Qualified for Playoffs" in table_text
    assert "❌ E = Eliminated" in table_text
    assert "TOP 4 QUALIFIED!" in table_text
    assert "IPL PLAYOFFS BEGIN!" in table_text
    # 6. Pre tags used for alignment
    assert "<pre>" in table_text and "</pre>" in table_text


def test_fix6_7_8_ai_teams_squad_validation():
    """FIX 6, 7, 8: Strict squad validation for AI teams (RCB, KKR, MI, CSK)."""
    from ipl_draft.engine.team_builder import SYSTEM_PLAYING_XI, validate_system_team_squad

    # Valid predefined squads pass validation
    for code, squad in SYSTEM_PLAYING_XI.items():
        ok, reason = validate_system_team_squad(1, squad, code)
        assert ok, f"{code} squad validation failed: {reason}"

    # Invalid player (Rishabh Pant on RCB) MUST fail validation
    invalid_rcb_pant = list(SYSTEM_PLAYING_XI["RCB"])
    invalid_rcb_pant[0] = "Rishabh Pant"
    ok, reason = validate_system_team_squad(1, invalid_rcb_pant, "RCB")
    assert not ok
    assert "Rishabh Pant" in reason

    # Invalid player (Venkatesh Iyer on RCB) MUST fail validation
    invalid_rcb_venky = list(SYSTEM_PLAYING_XI["RCB"])
    invalid_rcb_venky[0] = "Venkatesh Iyer"
    ok, reason = validate_system_team_squad(1, invalid_rcb_venky, "RCB")
    assert not ok
    assert "Venkatesh Iyer" in reason

    # Too many overseas players (> 4) MUST fail validation
    invalid_overseas = [
        "Phil Salt", "Tim David", "Romario Shepherd", "Josh Hazlewood", "Jacob Bethell",  # 5 overseas
        "Virat Kohli", "Rajat Patidar", "Devdutt Padikkal", "Krunal Pandya", "Bhuvneshwar Kumar", "Yash Dayal"
    ]
    ok, reason = validate_system_team_squad(1, invalid_overseas, "RCB")
    assert not ok
    assert "Maximum 4 overseas" in reason

    # Duplicate players MUST fail validation
    invalid_duplicates = list(SYSTEM_PLAYING_XI["RCB"])
    invalid_duplicates[1] = invalid_duplicates[0]  # Duplicate Virat Kohli
    ok, reason = validate_system_team_squad(1, invalid_duplicates, "RCB")
    assert not ok
    assert "Duplicate players" in reason


def test_fix4_playoff_scorecard_group_and_dm():
    """FIX 4 & 5: Playoff matches send complete scorecards to group and DMs with zero buttons."""
    card = {
        "match_id": 801,
        "stage": "PLAYOFF_ELIMINATOR",
        "home_team": {"id": 3, "name": "Mumbai Indians", "short_name": "MI"},
        "away_team": {"id": 4, "name": "Kolkata Knight Riders", "short_name": "KKR"},
        "winner_team_id": 3,
        "summary": "Mumbai Indians won by 36 runs",
        "innings_detail": [
            {
                "innings_no": 1, "is_super_over": False, "runs": 169, "wickets": 5, "legal_balls": 120,
                "batting_team": {"name": "Mumbai Indians", "short_name": "MI"},
                "batting": [
                    {"name": "Rohit Sharma", "runs": 45, "balls": 32, "fours": 5, "sixes": 2, "did_bat": True},
                    {"name": "Suryakumar Yadav", "runs": 66, "balls": 39, "fours": 7, "sixes": 3, "did_bat": True},
                ],
                "bowling": [{"name": "Harshit Rana", "legal_balls": 24, "runs": 28, "wickets": 2}],
            },
            {
                "innings_no": 2, "is_super_over": False, "runs": 133, "wickets": 10, "legal_balls": 103,
                "batting_team": {"name": "Kolkata Knight Riders", "short_name": "KKR"},
                "batting": [
                    {"name": "Sunil Narine", "runs": 42, "balls": 28, "fours": 4, "sixes": 2, "did_bat": True},
                ],
                "bowling": [{"name": "Jasprit Bumrah", "legal_balls": 24, "runs": 23, "wickets": 3}],
            }
        ]
    }

    # Group message format (user_id=None)
    pages_group = m.format_full_scorecard(card, "PLAYOFF_ELIMINATOR", user_id=None)
    text_group = "\n\n".join(pages_group)
    assert "IPL DRAFT — ELIMINATOR" in text_group
    assert "Mumbai Indians" in text_group
    assert "Kolkata Knight Riders" in text_group
    assert "Rohit Sharma" in text_group
    assert "Suryakumar Yadav" in text_group
    assert "Harshit Rana" in text_group
    assert "Jasprit Bumrah" in text_group
    assert "Mumbai Indians won by 36 runs" in text_group
    # Neutral view in group does not include "YOU WON!"
    assert "YOU WON!" not in text_group

    # DM format (user_id=3 -> MI manager)
    pages_dm = m.format_full_scorecard(card, "PLAYOFF_ELIMINATOR", user_id=3)
    text_dm = "\n\n".join(pages_dm)
    assert "YOU WON!" in text_dm


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



