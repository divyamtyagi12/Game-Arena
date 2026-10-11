# IPL Draft — fix & completion report

## 1. Why the 2-human tournament stuck in DRAFTING
1. `simulation_worker._drafting` called `ipl_begin_system_teams`, which (after migration v2) no longer changes the tournament state, then returned "progress made". The worker looped forever; no code ever moved DRAFTING → SYSTEM_TEAM_GENERATION. **Fix:** new SQL `ipl_finish_drafting` (checks humans drafted + batting orders confirmed + 5 AI squads complete) is the only exit; the handler waits (returns False) until ready.
2. Batting-order deadline lived in process memory; a restart or failed DM waited forever. **Fix:** persisted `batting_order_deadline_at`, worker tick re-sends lost prompts and auto-completes expired orders.
3. Test harness gave up after ~3 s of drafting and never confirmed batting orders.

## 2. Bugs fixed
| # | Bug | Fix |
|---|-----|-----|
| 1 | DRAFTING never advanced | `ipl_finish_drafting` |
| 2 | In-memory batting-order state / `_local_orders` fallbacks hid a missing migration | database-only, fallbacks removed |
| 3 | Confirmed batting order ignored by the match engine (engine re-sorted XI by rating) | `Squad.ordered` + `batting_order()` honours it; engine SQL returns confirmed flag |
| 4 | `ipl_draft.sql` failed on a fresh DB (`batting_order` column only in v2) | columns added to base file |
| 5 | Humans waited for all AI drafts before first offer | kickoff offers humans first, AI drafts in background |
| 6 | Direct-table fixture "fallback" and system-draft fallback (invalid on PostgREST, skipped validation) | removed; errors reach the failure counter |
| 7 | State machine had been loosened (LOBBY→SYSTEM_TEAM_GENERATION, DRAFTING→FIXTURE_GENERATION) | strict again |
| 8 | DM match cards showed "? vs ?" (read `home_team/away_team`, card has `team1/team2`) | fixed |
| 9 | "Draft complete" squad card was never sent | sent on 11th pick |
| 10 | Batting-order pick/confirm/reset races; reset could reopen a confirmed order | row-locked, confirmed orders immutable, distinct-order check |
| 11 | Awards/POM keyed by player only (breaks when one cricketer is in both squads) | keyed by (team, player); `pom_team_id`, `best_bowler_team_id` |

## 3. Cross-team drafting (items 3–4)
Offers exclude only cricketers already in the manager's own XI; cross-team reservations are gone. DB uniqueness is `(team_id, player_id)` in rosters and picks. `ipl_player_tournament_stats` groups by (tournament, team, player); tests reconcile every team's runs/wickets exactly against scorecards. Pool requirement is now 21 + margin.

## 4. Tests (final full-suite run: **110/110 passed**)
* Full tournaments, real PostgreSQL, scripted humans, batting orders via real callbacks, league + 4 playoff matches + awards + leaderboards: **2 humans, 3 humans, 8 humans — all pass** (`tests/test_ipl_flow.py`).
* New `tests/test_ipl_v3.py`: human drafts an AI-owned cricketer; duplicate in one XI rejected by DB; per-team stats reconcile; batting order survives two restarts, expired deadline autofills keeping partial picks, and every human innings in a whole tournament uses exactly the confirmed order; confirmed order cannot be reopened.
* Existing Anime Draft, Number Wars and Telegram-bot tests untouched and passing.
* Upgrade path tested: old-schema DB with a live tournament → backup (26 tables, row counts verified) → v3 → data intact, offers continue.

## 5. Squad verification status — **NOT VERIFIED**
* Evidence collected into `ipl_draft/data/ipl_2026_sources.py` from Wisden (post-auction, 10 teams), Wikipedia team pages / personnel-changes list, CricTracker (LSG). **iplt20.com could not be used** (shell blocked; fetched page truncated) and Cricsheet could not be reached, so no squad is marked verified and `ipl_verified` stays false everywhere.
* Source conflicts are listed in that file (e.g. David Miller DC vs LSG, Lungi Ngidi, Pant/Kuldeep trade headline).
* **Coverage gap (important):** of 274 distinct 2026 players found, only 147 are in the current 356-player pool; 127 are missing, and 209 pool entries are not in any 2026 squad (legends/historic). `python -m scripts.verify_ipl_2026` prints the per-team list.
* **AI playing XIs cannot be verified against 2026 match records:** the five AI teams are random-draft franchises (not real XIs), and no 2026 ball-by-ball records were reachable. Not claimed.

## 6. Remaining issues / decisions for you
1. Add the 127 missing 2026 players to the seed (needs role/hand/rating-tier data; I did not invent it). Then restrict the pool to 2026 squads if "official 2026 only" is wanted.
2. Provide `ipl_json.zip` (Cricsheet) or iplt20.com squad exports to complete verification.
3. Real-world check on Supabase: SQL verified on PostgreSQL 16 only; the PostgREST path was not exercised. `pytest` could not be installed here; tests ran with the project's own runner.
4. No destructive cleanup was run; old `ipl_backup_*` schemas (if you create them) are left for you to drop.
