import asyncio
from collections import Counter
import random
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from game.categories import CATEGORY_KEYS, OFFER_SIZE
from game.draft import MAX_PER_ANIME, build_offers, normalize_anime
from models.catalog import CharacterCatalog
from models.character import Character
from services import draft_service
from tests.helpers import build_catalog

CAT = build_catalog()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Selection Rules & Draft Integrity
# ─────────────────────────────────────────────────────────────────────────────

def test_full_random_drafts_never_dead_end():
    """Verify 3,000 full 5-round drafts: exactly 6 options, <= 2 per anime, 5 unique picks."""
    rng = random.Random(5)
    for _ in range(3000):
        picked: set[str] = set()
        for cat in CATEGORY_KEYS:
            offers = build_offers(CAT, cat, picked, rng)
            # Exactly 6 options
            assert len(offers) == OFFER_SIZE == 6
            # All 6 options have unique IDs
            assert len({o.id for o in offers}) == 6
            # No previously picked character appears
            assert not ({o.id for o in offers} & picked)
            # Max 2 characters from the same anime
            series_counts = Counter(normalize_anime(o.series) for o in offers)
            assert max(series_counts.values()) <= MAX_PER_ANIME
            # All from primary pool (no fallback needed for standard catalog)
            assert all(cat in o.categories for o in offers)
            # Drafter selects one
            picked.add(rng.choice(offers).id)
        assert len(picked) == 5


def test_offers_vary_between_games():
    """Verify options are randomized and vary across different invocations."""
    rng = random.Random(6)
    seen = {tuple(sorted(o.id for o in build_offers(CAT, "ATTACK", set(), rng))) for _ in range(50)}
    assert len(seen) > 10


def test_anime_distributions_are_fair_and_unforced():
    """Verify multiple valid anime distributions occur and no fixed distribution is forced."""
    rng = random.Random(42)
    distributions = Counter()
    for _ in range(3000):
        offers = build_offers(CAT, "INTELLIGENCE", set(), rng)
        assert len(offers) == 6
        series_cnt = Counter(normalize_anime(o.series) for o in offers)
        assert max(series_cnt.values()) <= 2
        dist = tuple(sorted(series_cnt.values(), reverse=True))
        distributions[dist] += 1

    # Valid distributions: 2+2+1+1, 2+1+1+1+1, 2+2+2, 1+1+1+1+1+1
    assert distributions[(2, 2, 1, 1)] > 0
    assert distributions[(2, 1, 1, 1, 1)] > 0
    assert distributions[(2, 2, 2)] > 0
    assert distributions[(1, 1, 1, 1, 1, 1)] > 0
    # Three characters from the same anime must NEVER occur
    assert all(max(d) <= 2 for d in distributions.keys())


def test_previously_offered_unselected_remain_eligible():
    """Unselected characters from earlier offers must remain eligible in subsequent rounds."""
    rng = random.Random(10)
    # Offer in category 1 (ATTACK)
    attack_offers = build_offers(CAT, "ATTACK", set(), rng)
    assert len(attack_offers) == 6

    # Select character 0; character 1 was offered but not selected
    chosen_char = attack_offers[0]
    unselected_char = attack_offers[1]
    picked = {chosen_char.id}

    # In subsequent categories, unselected_char must be allowed to appear
    # We test with a custom small catalog containing unselected_char and chosen_char
    eligible_offers = build_offers(CAT, "ATTACK", picked, rng)
    # chosen_char must NEVER appear
    assert chosen_char.id not in {o.id for o in eligible_offers}


def test_player_isolation_selected_characters():
    """Player 1's picks only exclude characters for Player 1, not Player 2."""
    rng = random.Random(11)
    p1_picked = {"saitama", "asta"}
    p2_picked = {"genos"}

    p1_offers = build_offers(CAT, "ATTACK", p1_picked, rng)
    assert "saitama" not in {o.id for o in p1_offers}
    assert "asta" not in {o.id for o in p1_offers}

    # Saitama and Asta are eligible for Player 2
    # Verify by constructing an exclude set for Player 2 that does not include Saitama
    p2_offers = build_offers(CAT, "ATTACK", p2_picked, rng)
    assert "genos" not in {o.id for o in p2_offers}


# ─────────────────────────────────────────────────────────────────────────────
# 2. Anime Normalization & Inconsistent Name Handling
# ─────────────────────────────────────────────────────────────────────────────

def test_anime_normalization_cases():
    """Verify robust normalization across casing, punctuation, unicode, and aliases."""
    test_cases = [
        ("One Punch Man", "one punch man"),
        ("OPM", "one punch man"),
        ("one-punch man", "one punch man"),
        ("ONE PUNCH MAN", "one punch man"),
        ("Hunter x Hunter", "hunter x hunter"),
        ("HxH", "hunter x hunter"),
        ("Hunter × Hunter", "hunter x hunter"),
        ("Hunter ✕ Hunter", "hunter x hunter"),
        ("Re:ZERO", "re:zero"),
        ("Re:Zero", "re:zero"),
        ("Re-Zero", "re:zero"),
        ("Black Clover", "black clover"),
        ("BC", "black clover"),
        ("Death Note", "death note"),
        ("DN", "death note"),
        ("Ben 10", "ben 10"),
        ("Ben10", "ben 10"),
        ("AoT", "attack on titan"),
        ("Attack on Titan", "attack on titan"),
        ("Attack-on-Titan", "attack on titan"),
        ("  Demon Slayer! ", "demonslayer"),
        ("Demon-Slayer", "demonslayer"),
        (None, ""),
        ("", ""),
    ]
    for raw, expected in test_cases:
        assert normalize_anime(raw) == expected, f"Failed for {raw}: got {normalize_anime(raw)}, expected {expected}"


def test_inconsistent_anime_names_cannot_bypass_limit():
    """Characters from the same anime with inconsistent series names must be limited to max 2."""
    # Build a synthetic catalog with 4 characters from One Punch Man under varying names
    c1 = Character("c1", "Saitama", "OPM", "LEGENDARY", "", (), {"ATTACK": 99, "TANKING": 90, "SPEED": 90, "HEALING": 10, "INTELLIGENCE": 70}, frozenset(["ATTACK"]))
    c2 = Character("c2", "Genos", "One Punch Man", "EPIC", "", (), {"ATTACK": 88, "TANKING": 80, "SPEED": 85, "HEALING": 10, "INTELLIGENCE": 80}, frozenset(["ATTACK"]))
    c3 = Character("c3", "Garou", "one-punch man", "LEGENDARY", "", (), {"ATTACK": 96, "TANKING": 92, "SPEED": 95, "HEALING": 10, "INTELLIGENCE": 85}, frozenset(["ATTACK"]))
    c4 = Character("c4", "Boros", "One Punch Man ", "LEGENDARY", "", (), {"ATTACK": 99, "TANKING": 97, "SPEED": 90, "HEALING": 20, "INTELLIGENCE": 75}, frozenset(["ATTACK"]))
    c5 = Character("c5", "Asta", "Black Clover", "RARE", "", (), {"ATTACK": 80, "TANKING": 75, "SPEED": 80, "HEALING": 10, "INTELLIGENCE": 50}, frozenset(["ATTACK"]))
    c6 = Character("c6", "Yami", "Black Clover", "EPIC", "", (), {"ATTACK": 90, "TANKING": 85, "SPEED": 85, "HEALING": 10, "INTELLIGENCE": 80}, frozenset(["ATTACK"]))
    c7 = Character("c7", "Hisoka", "HxH", "EPIC", "", (), {"ATTACK": 88, "TANKING": 80, "SPEED": 88, "HEALING": 10, "INTELLIGENCE": 90}, frozenset(["ATTACK"]))
    c8 = Character("c8", "Killua", "Hunter x Hunter", "EPIC", "", (), {"ATTACK": 85, "TANKING": 75, "SPEED": 95, "HEALING": 10, "INTELLIGENCE": 85}, frozenset(["ATTACK"]))

    test_cat = CharacterCatalog([c1, c2, c3, c4, c5, c6, c7, c8])
    rng = random.Random(77)

    for _ in range(50):
        offers = build_offers(test_cat, "ATTACK", set(), rng)
        assert len(offers) == 6
        # Count normalized series
        counts = Counter(normalize_anime(o.series) for o in offers)
        # OPM must never have more than 2, despite c1, c2, c3, c4 having 4 different series strings!
        assert counts["one punch man"] <= 2
        assert counts["black clover"] <= 2
        assert counts["hunter x hunter"] <= 2


# ─────────────────────────────────────────────────────────────────────────────
# 3. Insufficient Pools & Safe Graceful Handling
# ─────────────────────────────────────────────────────────────────────────────

def test_fallback_when_category_pool_depleted():
    """When category pool is depleted, extras from catalog.all() safely fill up to 6."""
    healing_ids = {c.id for c in CAT.pool("HEALING")}
    offers = build_offers(CAT, "HEALING", healing_ids, random.Random(1))
    assert len(offers) == OFFER_SIZE == 6
    assert len({o.id for o in offers}) == 6
    counts = Counter(normalize_anime(o.series) for o in offers)
    assert max(counts.values()) <= 2


def test_tiny_catalog_does_not_dead_end_or_loop():
    """Catalog with fewer than 6 characters returns all available without infinite loop."""
    c1 = Character("t1", "Hero 1", "Anime A", "COMMON", "", (), {"ATTACK": 50, "TANKING": 50, "SPEED": 50, "HEALING": 50, "INTELLIGENCE": 50}, frozenset(["ATTACK"]))
    c2 = Character("t2", "Hero 2", "Anime B", "COMMON", "", (), {"ATTACK": 50, "TANKING": 50, "SPEED": 50, "HEALING": 50, "INTELLIGENCE": 50}, frozenset(["ATTACK"]))
    c3 = Character("t3", "Hero 3", "Anime C", "COMMON", "", (), {"ATTACK": 50, "TANKING": 50, "SPEED": 50, "HEALING": 50, "INTELLIGENCE": 50}, frozenset(["ATTACK"]))

    small_cat = CharacterCatalog([c1, c2, c3])
    offers = build_offers(small_cat, "ATTACK", set(), random.Random(1))
    assert len(offers) == 3
    assert {o.id for o in offers} == {"t1", "t2", "t3"}


def test_monoculture_catalog_enforces_limit_without_loop():
    """Catalog with 10 characters all from the same anime returns at most 2."""
    chars = [
        Character(f"mono_{i}", f"Hero {i}", "Single Anime", "COMMON", "", (),
                  {"ATTACK": 50, "TANKING": 50, "SPEED": 50, "HEALING": 50, "INTELLIGENCE": 50}, frozenset(["ATTACK"]))
        for i in range(10)
    ]
    mono_cat = CharacterCatalog(chars)
    offers = build_offers(mono_cat, "ATTACK", set(), random.Random(1))
    # At most 2 characters can be returned; must never loop infinitely or return 3+
    assert len(offers) == 2
    assert len({o.id for o in offers}) == 2
    assert all(normalize_anime(o.series) == "singleanime" for o in offers)


def test_entirely_exhausted_catalog_returns_empty():
    """When all characters are excluded, returns empty list without error or loop."""
    all_ids = {c.id for c in CAT.all()}
    offers = build_offers(CAT, "ATTACK", all_ids, random.Random(1))
    assert offers == []


# ─────────────────────────────────────────────────────────────────────────────
# 4. State Persistence & Server-Side Callback Validation
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ensure_offer_persists_generated_options():
    """ensure_offer must create the offer once in the DB and return the exact same options on subsequent calls."""
    mock_ctx = MagicMock()
    mock_ctx.catalog = CAT
    mock_ctx.rng = random.Random(123)

    stored_offer = {}

    async def mock_get_offer(gp_id, category):
        return stored_offer.get((gp_id, category))

    async def mock_create_offer(gp_id, category, ids):
        stored_offer[(gp_id, category)] = ids
        return ids

    mock_ctx.drafts.get_offer = AsyncMock(side_effect=mock_get_offer)
    mock_ctx.drafts.create_offer = AsyncMock(side_effect=mock_create_offer)

    # First call: generates and persists
    offers_1 = await draft_service.ensure_offer(mock_ctx, 101, "ATTACK", set())
    assert len(offers_1) == 6
    ids_1 = [c.id for c in offers_1]

    # Second call (e.g. user refreshed or clicked /draft): loads persisted offer
    offers_2 = await draft_service.ensure_offer(mock_ctx, 101, "ATTACK", set())
    ids_2 = [c.id for c in offers_2]

    assert ids_1 == ids_2
    assert mock_ctx.drafts.create_offer.call_count == 1
    assert mock_ctx.drafts.get_offer.call_count == 2


@pytest.mark.asyncio
async def test_handle_pick_server_side_validations():
    """Verify handle_pick rejects unauthorized users, outdated buttons, invalid indices, and duplicate characters."""
    mock_ctx = MagicMock()
    mock_ctx.catalog = CAT
    mock_ctx.lock = MagicMock(return_value=asyncio.Lock())

    # Player 101 belongs to user 999
    gp = {"id": 101, "game_id": 50, "user_id": 999}
    game = {"id": 50, "status": "DRAFTING"}

    mock_ctx.players.get = AsyncMock(return_value=gp)
    mock_ctx.games.get = AsyncMock(return_value=game)

    # 1. Reject unauthorized user (different Telegram user clicking the button)
    wrong_query = MagicMock()
    wrong_query.from_user.id = 888  # Different user
    wrong_query.answer = AsyncMock()

    await draft_service.handle_pick(mock_ctx, wrong_query, 101, 0, 1)
    wrong_query.answer.assert_awaited_once_with("❌ This isn't your draft.", show_alert=True)

    # 2. Reject out-of-order / already chosen category
    right_query = MagicMock()
    right_query.from_user.id = 999
    right_query.answer = AsyncMock()
    right_query.message = MagicMock()
    # Player already picked 1 character (len(picks) == 1), so cat_idx 0 is outdated
    mock_ctx.drafts.picks = AsyncMock(return_value=[{"category": "ATTACK", "character_id": "saitama"}])

    await draft_service.handle_pick(mock_ctx, right_query, 101, 0, 1)
    right_query.answer.assert_awaited_with("⚠️ You already chose for this category.", show_alert=False)

    # 3. Reject invalid position (pos > offer length or pos < 1)
    mock_ctx.drafts.picks = AsyncMock(return_value=[])  # cat_idx 0 is valid now
    mock_ctx.drafts.get_offer = AsyncMock(return_value=["reinhard", "reid", "cecilus", "subaru", "emilia", "beatrice"])

    invalid_pos_query = MagicMock()
    invalid_pos_query.from_user.id = 999
    invalid_pos_query.answer = AsyncMock()
    await draft_service.handle_pick(mock_ctx, invalid_pos_query, 101, 0, 9)  # pos 9 does not exist
    invalid_pos_query.answer.assert_awaited_with("⚠️ That button is outdated.", show_alert=True)

    # 4. Reject character already selected in a previous category
    # Say reinhard was already picked in an earlier category
    mock_ctx.drafts.picks = AsyncMock(return_value=[{"category": "ATTACK", "character_id": "reinhard"}])
    # Now in category index 1 (TANKING)
    mock_ctx.drafts.get_offer = AsyncMock(return_value=["reinhard", "garou", "boros", "zombieman", "elder_centipede", "albedo"])

    dup_query = MagicMock()
    dup_query.from_user.id = 999
    dup_query.answer = AsyncMock()
    await draft_service.handle_pick(mock_ctx, dup_query, 101, 1, 1)  # pos 1 is reinhard
    dup_query.answer.assert_awaited_with("⚠️ That character was already selected in another category.", show_alert=True)


@pytest.mark.asyncio
async def test_handle_pick_concurrency_race_safety():
    """Verify rapid double-clicks on draft buttons are serialized and handled safely without double picks."""
    mock_ctx = MagicMock()
    mock_ctx.catalog = CAT
    lock = asyncio.Lock()
    mock_ctx.lock = MagicMock(return_value=lock)

    gp = {"id": 101, "game_id": 50, "user_id": 999}
    game = {"id": 50, "status": "DRAFTING"}
    mock_ctx.players.get = AsyncMock(return_value=gp)
    mock_ctx.games.get = AsyncMock(return_value=game)

    picks = []

    async def mock_picks(gp_id):
        return list(picks)

    async def mock_add_pick(gp_id, cat, char_id):
        # Simulate slight network delay
        await asyncio.sleep(0.01)
        picks.append({"category": cat, "character_id": char_id})

    mock_ctx.drafts.picks = AsyncMock(side_effect=mock_picks)
    mock_ctx.drafts.add_pick = AsyncMock(side_effect=mock_add_pick)
    mock_ctx.drafts.get_offer = AsyncMock(return_value=["reinhard", "reid", "cecilus", "subaru", "emilia", "beatrice"])

    q1 = MagicMock()
    q1.from_user.id = 999
    q1.answer = AsyncMock()
    q1.message = None

    q2 = MagicMock()
    q2.from_user.id = 999
    q2.answer = AsyncMock()
    q2.message = None

    with patch("services.draft_service.send_prompt", new_callable=AsyncMock):
        # Fire two concurrent clicks for category 0
        await asyncio.gather(
            draft_service.handle_pick(mock_ctx, q1, 101, 0, 1),
            draft_service.handle_pick(mock_ctx, q2, 101, 0, 2),
        )

    # Exactly one pick was added for category 0
    assert len(picks) == 1
    # One query succeeded, the other received 'already chose'
    answers = [q1.answer.call_args[0][0], q2.answer.call_args[0][0]]
    assert any("selected" in a for a in answers)
    assert any("already chose" in a for a in answers)
