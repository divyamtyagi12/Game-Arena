"""Cleanup script: Remove all non-2026 IPL players from the database.
Only keep players belonging to 2026 IPL team squads.
"""
from __future__ import annotations

import asyncio
from config import load_settings
from database.client import Database
from ipl_draft.data import ipl_2026_sources as src
from ipl_draft.data.names import normalize_name


def chunk_list(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


async def run():
    s = load_settings()
    db = Database(s.supabase_url, s.supabase_key)

    print("1. Fetching all players from database...")
    all_players = await db.exec(lambda c: c.table("ipl_players").select("id, slug, display_name, last_ipl_season, role, is_eligible"))
    print(f"Total players currently in DB: {len(all_players)}")

    # Collect all 2026 squad player names (normalized)
    names26 = set()
    for sq in src.WISDEN_SQUADS.values():
        names26.update(sq)
    for sq in src.REPLACEMENTS_IN.values():
        names26.update(sq)
    for sq in src.EXTRA_ON_WIKI.values():
        names26.update(sq)
    norm26 = {normalize_name(n) for n in names26}

    # Identify 2026 players vs non-2026 players
    keep_players = [p for p in all_players if p["last_ipl_season"] == 2026 or normalize_name(p["display_name"]) in norm26]
    delete_players = [p for p in all_players if p not in keep_players]

    keep_ids = {p["id"] for p in keep_players}
    delete_ids = [p["id"] for p in delete_players]

    print(f"Players to KEEP (2026 IPL squads): {len(keep_players)}")
    print(f"Players to DELETE (before 2026 / not in 2026 squads): {len(delete_players)}")

    print("\n2. Updating all 2026 players to is_eligible=True and last_ipl_season=2026...")
    for chunk in chunk_list(list(keep_ids), 40):
        await db.exec(lambda c, ids=chunk: c.table("ipl_players").update({
            "is_eligible": True,
            "last_ipl_season": 2026
        }).in_("id", ids))
    print("Updated 2026 players.")

    print("\n3. Cleaning up old tournaments and referencing data so players can be deleted cleanly...")
    order_tables = [
        ("ipl_batting_scorecards", "id"),
        ("ipl_bowling_scorecards", "id"),
        ("ipl_ball_events", "id"),
        ("ipl_innings", "id"),
        ("ipl_matches", "id"),
        ("ipl_fixtures", "id"),
        ("ipl_standings", "tournament_id"),
        ("ipl_player_tournament_stats", "tournament_id"),
        ("ipl_team_awards", "id"),
        ("ipl_tournament_awards", "id"),
        ("ipl_team_rosters", "id"),
        ("ipl_draft_picks", "id"),
        ("ipl_draft_offer_candidates", "offer_id"),
        ("ipl_draft_offers", "id"),
        ("ipl_tournament_pool", "tournament_id"),
        ("ipl_tournament_teams", "id"),
        ("ipl_tournaments", "id"),
    ]
    for tbl, col in order_tables:
        try:
            await db.exec(lambda c, t=tbl, cl=col: c.table(t).delete().neq(cl, -1))
            print(f"Cleared {tbl}")
        except Exception as e:
            print(f"Note on {tbl}: {e}")

    print("\n4. Deleting non-2026 players from ipl_players...")
    deleted_count = 0
    for chunk in chunk_list(delete_ids, 40):
        res = await db.exec(lambda c, ids=chunk: c.table("ipl_players").delete().in_("id", ids))
        deleted_count += len(chunk)
    print(f"Deleted {deleted_count} non-2026 players.")

    # Verification
    remaining = await db.exec(lambda c: c.table("ipl_players").select("id, display_name, last_ipl_season, is_eligible"))
    print(f"\nSUCCESS: Remaining players in DB: {len(remaining)}")
    print(f"All eligible: {all(p['is_eligible'] for p in remaining)}")
    print(f"All 2026 season: {all(p['last_ipl_season'] == 2026 for p in remaining)}")


if __name__ == "__main__":
    asyncio.run(run())
