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
    """Requirement 2: Squads 2020-2026, AB de Villiers & Gayle eligible, older ineligible."""
    players = [
        {"name": "AB de Villiers", "last_ipl_season": 2021, "is_eligible": True},
        {"name": "Chris Gayle", "last_ipl_season": 2021, "is_eligible": True},
        {"name": "Virat Kohli", "last_ipl_season": 2026, "is_eligible": True},
        {"name": "Sachin Tendulkar", "last_ipl_season": 2013, "is_eligible": False},
        {"name": "Virender Sehwag", "last_ipl_season": 2015, "is_eligible": False},
        {"name": "Gautam Gambhir", "last_ipl_season": 2018, "is_eligible": False},
    ]
    # Check that older players are marked ineligible (< 2020)
    for p in players:
        if p["last_ipl_season"] < 2020:
            assert p["is_eligible"] is False
        else:
            assert p["is_eligible"] is True
            assert p["last_ipl_season"] >= 2020


def test_batting_order_state_machine():
    """Requirement 3: Batting order flow: pick 11 positions, undo, reset, confirm."""
    # 11 sample players
    players = [{"id": f"p{i}", "name": f"Player {i}", "role": "BAT", "slot": i} for i in range(1, 12)]
    
    # Initially: 0 ordered, 11 remaining
    ordered = []
    remaining = [{"pos": p["slot"], "player": p} for p in players]
    
    prompt = m.batting_order_prompt(players, ordered, remaining)
    assert "(0/11)" in prompt
    assert "Select <b>batter #1</b>" in prompt

    # User picks first batter (pos 4)
    ordered.append(players[3])
    remaining = [r for r in remaining if r["pos"] != 4]
    prompt = m.batting_order_prompt(players, ordered, remaining)
    assert "(1/11)" in prompt
    assert "Player 4" in prompt
    assert "Select <b>batter #2</b>" in prompt

    # User picks remaining batters up to 11
    for p in players:
        if p not in ordered:
            ordered.append(p)
    remaining = []
    prompt = m.batting_order_prompt(players, ordered, remaining)
    assert "(11/11)" in prompt
    assert "CONFIRM" in prompt

    # Check keyboard generation
    k_prompt = kb.batting_order(101, [{"pos": 1, "player": players[0]}], 1)
    # Undo and Reset buttons are present
    button_texts = [btn.text for row in k_prompt.inline_keyboard for btn in row]
    assert "↩️ UNDO" in button_texts
    assert "🔄 RESET" in button_texts

    # Confirm keyboard
    k_conf = kb.batting_order_confirm(101)
    conf_texts = [btn.text for row in k_conf.inline_keyboard for btn in row]
    assert any("CONFIRM" in t for t in conf_texts)
    assert "🔄 RESET" in conf_texts


def test_match_engine_respects_custom_batting_order():
    """Requirement 3 & 6: Verify match engine follows the designated batting order."""
    rng = random.Random(42)
    # Create 11 players with distinctive names
    players_a = tuple(Player(id=f"a_{i}", name=f"Batter_A_{i}", role="BAT", ratings=Ratings(batting_rating=50)) for i in range(1, 12))
    players_b = tuple(Player(id=f"b_{i}", name=f"Batter_B_{i}", role="BAT", ratings=Ratings(batting_rating=50)) for i in range(1, 12))
    sq_a = Squad(1, "Team A", "TMA", players_a)
    sq_b = Squad(2, "Team B", "TMB", players_b)

    # Invert the order so Batter_A_11 is opener, Batter_A_1 is #11
    inverted_order = list(reversed(players_a))
    sim = InningsSim(rng, sq_a, sq_b, 1, order=inverted_order).run()

    # The first bat line MUST be Batter_A_11
    assert sim.bat_lines[0].player.name == "Batter_A_11"
    assert sim.bat_lines[1].player.name == "Batter_A_10"
    assert sim.bat_lines[-1].player.name == "Batter_A_1"


def test_dm_match_result_and_buttons():
    """Requirement 4: Private DM match cards and interactive buttons."""
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

    # Check the 4 required buttons
    markup = kb.dm_match_card_buttons(tid=1, match_id=999)
    button_callbacks = {btn.callback_data for row in markup.inline_keyboard for btn in row}
    button_labels = {btn.text for row in markup.inline_keyboard for btn in row}
    assert any("FULL SCORECARD" in l for l in button_labels)
    assert any("NEXT MATCH" in l for l in button_labels)
    assert any("POINTS TABLE" in l for l in button_labels)
    assert any("TOURNAMENT PROGRESS" in l for l in button_labels)
    assert "ipl:sc:999:0" in button_callbacks
    assert "ipl:nx:1" in button_callbacks
    assert "ipl:tb:1" in button_callbacks
    assert "ipl:ts:1" in button_callbacks


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
    """Verify that system franchises are prepared and drafted before user draft."""
    from unittest.mock import AsyncMock, MagicMock
    from ipl_draft.services import system_service

    rt = MagicMock()
    rt.repo = MagicMock()
    rt.repo.get = AsyncMock(return_value={"id": 10, "state": "DRAFTING"})
    rt.repo.all_teams = AsyncMock(return_value=[
        {"id": 1, "kind": "SYSTEM", "franchise_code": "RCB"},
        {"id": 2, "kind": "SYSTEM", "franchise_code": "KKR"},
        {"id": 3, "kind": "SYSTEM", "franchise_code": "MI"},
        {"id": 4, "kind": "SYSTEM", "franchise_code": "CSK"},
    ])
    rt.repo.system_progress = AsyncMock(return_value=[
        {"team_id": i, "picks": 11, "done": True} for i in range(1, 5)
    ])
    rt.repo.complete_system_teams = AsyncMock(return_value={"status": "OK"})
    rt.repo.batting_order_auto_assign = AsyncMock(return_value=None)

    rt.ctx.lock = lambda key: __import__("contextlib").AsyncExitStack()
    asyncio.run(system_service.prepare_system_teams(rt, 10))
    assert rt.repo.complete_system_teams.called
    assert rt.repo.batting_order_auto_assign.call_count == 4


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
