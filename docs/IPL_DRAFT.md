# IPL DRAFT — design, operations, testing

Third game of the arena. Anime Draft and Number Wars are untouched: no existing table, function, handler or command was changed
(only additive hooks in `bot.py`, `handlers/arena|start|info|lobby|leaderboard.py`, `utils/messages.py`).

## Deploy
1. Supabase SQL editor, in this order (all re-runnable): `database/schema.sql`, `database/number_wars.sql`, `database/ipl_draft.sql`,
   `database/migration_ipl_v2.sql`, then **`database/migration_ipl_v3.sql`** (must be last).
   **Before upgrading an existing deployment run `database/backup_ipl_tables.sql`** (non-destructive: copies every IPL table into a new
   `ipl_backup_<timestamp>` schema and verifies row counts) or `scripts/backup_ipl_tables.sh` (pg_dump). Nothing in v3 drops or deletes data;
   removing an old backup schema is left to the owner.
   Without the third file the bot still runs; IPL is disabled and an error is logged.
2. Load players (see below) — `python -m scripts.import_ipl_players --seed` and/or `--cricsheet ipl_json.zip`.
3. Start the bot as before. No new secrets: IPL uses the existing `SUPABASE_URL`/`SUPABASE_KEY` (service role) and bot token.
   IPL functions are `security definer`, granted to `service_role` only; IPL tables have RLS on with no policies; hidden ratings
   live in schema `ipl_private`, which PostgREST does not expose.

## Player data — honest status
* `--seed`: 356 well-known IPL cricketers (names, role, nationality, batting hand, bowling type) compiled by hand. **No statistics.**
  Stored as `data_quality=SEED`, `ipl_verified=false`; ratings come from an editorial tier (S/A/B/C) + deterministic jitter and are
  tagged `EDITORIAL_TIER`. Roles/nationalities are unverified. This is a bootstrap so the game is playable, not "all IPL players".
* `--cricsheet <zip|dir>` (download https://cricsheet.org/downloads/ipl_json.zip, ODC-By; `--download` fetches it): computes career
  and per-season stats from ball-by-ball data, infers roles, rates players from the stats, marks them `ipl_verified=true`,
  and prints a report (counts, unmatched seed names, bowlers whose pace/spin type is unknown). Idempotent; seed rows never
  overwrite real-stat ratings. **This importer was only tested on synthetic matches — it could not be run on the real archive
  from the build sandbox (no internet).** Run with `--dry-run` first.
* Pool capacity: `required = max((humans+8)*11, 21*humans) + IPL_POOL_MARGIN` (final squads, and the peak of simultaneous
  11-card reservations). `Force start` refuses with a clear message when the pool is smaller.
* Optional: after a real import, retire unmatched seed rows: `update ipl_players set is_eligible=false where data_quality='SEED';`

## Hidden ratings (`ipl-ratings-v1`)
13 attributes 0–100 (`batting_rating … overall_rating`), never sent to Telegram (a test scans every message). From stats with
Bayesian shrinkage: `adj = (observed + prior·K) / (volume + K)` for average, strike rate, economy, bowling strike rate, boundary
share, dot share, death-over splits; batting is capped until ~40 innings so a 3-innings fluke can't outrank a career.
Priors/weights are constants in `ipl_draft/engine/ratings.py`. A per-tournament **snapshot** is frozen at draft start
(`ipl_private.ipl_tournament_ratings`), so mid-tournament re-imports never change a running tournament.

## Draft & fairness
* 11 rounds × 11 random available players; no role or rating filtering of the offer. Exactly one pick.
* Reservations: `ipl_player_reservations unique(tournament_id, player_id)`; offers are generated under a tournament row lock;
  a pick inserts pick+roster row and releases the other 10 in one transaction; double-clicks/races are decided by the DB.
* Deadlines are timestamps in PostgreSQL → survive restarts. Timeout → auto-pick using only public info (roles). After
  `IPL_AUTO_AFTER_TIMEOUTS` consecutive misses (or an undeliverable DM) the team is auto-drafted.
* System teams (MI, CSK, RCB, KKR, RR, PBKS, GT, LSG) draft **after** humans, snake order, from the same kind of random
  11-card offers, with a role-repair safety net so squads are playable. They use the identical match engine, no bonus.

## Tournament
Double round robin (circle method; odd team counts get rotating byes) → N(N−1) matches. Ball-by-ball T20: 20 overs, 10 wickets,
4-over bowler cap, wides/no-balls/free hit/byes/leg-byes, run-outs, Super Over (repeat up to 5, then boundary count, then
seeded coin). Seed per match = sha256(tournament, fixture, draft start, salt) → a crash-retry reproduces the same match.
Win 2, no result 1. Ranking: points → NRR → wins → head-to-head → lowest team number. NRR uses legal balls/6 and counts an
all-out innings as 20 overs. Top 4: Eliminator (3v4) → Qualifier 1 (1v2) → Qualifier 2 → Final; a system team can win.
Awards (from scorecards): Orange Cap, Purple Cap, Player of the Tournament (documented impact points), Most Sixes, Best Innings.
Group UX: one dashboard message edited in place (throttled), compact matchday summaries every `IPL_SUMMARY_EVERY` days.

## State machine
`LOBBY → DRAFTING → SYSTEM_TEAM_GENERATION → FIXTURE_GENERATION → LEAGUE_RUNNING → LEAGUE_COMPLETED → PLAYOFF_ELIMINATOR →
PLAYOFF_QUALIFIER_1 → PLAYOFF_QUALIFIER_2 → PLAYOFF_FINAL → COMPLETED`; `CANCELLED` (terminal) and `FAILED_RECOVERABLE`
(after repeated worker errors; `/iplresume` by host/admin). Enforced by a trigger. One unfinished game per group across all
three games (advisory-locked triggers). Worker: lease per tournament + `SKIP LOCKED` fixture claims (stale after 300 s).

## Commands / buttons
`/ipl /iplrules /iplteam /ipltable /iplfixtures /iplhistory /iplresume /iplstats /stats`; callbacks `ipl:*` (validated server-side:
sender, chat, tournament, offer owner/status). Main menu: Anime Draft · Number Wars · IPL Draft · Leaderboards · My Stats · Help.

## Tests
`python -m tests.run_all` — pure unit tests always run; `test_ipl_flow.py` / `test_ipl_safety.py` need PostgreSQL with the three
SQL files applied (`IPL_TEST_PG="-h host -p port -U user -d db"`) and are skipped otherwise. They run the real services against
real Postgres with a **fake Telegram** and stubbed `telegram`/`supabase` packages when those are not installed.
Load check: `python -m scripts.ipl_load_test --groups 6 --humans 4` (same requirements).

## Manual Telegram checklist (not performed by the author — no Telegram access)
1. `/start` in a group → picker shows 3 games + nav. 2. IPL Draft → lobby; join with a user who never started the bot → prompt +
deep link; start the bot, join. 3. Admin presses Force Start; non-admin is refused. 4. In DM: 11 cards, pick → Confirm/Back;
ignore a round for 45 s → auto-pick. 5. Watch the single group dashboard update; check `/ipltable`, `/iplfixtures`, scorecards.
6. Restart the bot mid-draft and mid-league; it must continue. 7. `/leaderboard` → IPL; `/stats`. 8. Run an Anime Draft and a
Number Wars game afterwards to confirm nothing changed.

## Known limitations
Seed data is unverified and has no stats; real import untested on the real archive; pace/spin is unknown for Cricsheet-only
bowlers (defaults to pace, reported); handlers were verified against fakes, not the real python-telegram-bot or Telegram;
no live load test against Supabase (RPC round-trips ≈ 1 per DB call; a 16-team tournament is ≈ a few thousand calls);
pre-existing leaderboard module has no IPL entry besides the new button; ratings are statistical estimates, not truth.


## v3 rules (cross-team drafting, batting orders)
* A cricketer may be in several squads of one tournament (human or AI). Duplicates are forbidden only inside the same XI
  (`unique (team_id, player_id)`). Statistics, awards and Player-of-the-Match are keyed by tournament + team + player.
* Humans start drafting immediately; the five AI franchises draft independently. DRAFTING ends only through `ipl_finish_drafting`
  (all humans drafted + batting order confirmed + AI squads complete).
* Batting order lives entirely in the database (picks, confirmation, persisted deadline `IPL_BATTING_ORDER_SECONDS`, default 300).
  After a restart the worker re-sends a lost prompt and auto-completes expired orders (keeping the manager's partial picks).
  The match engine uses exactly the confirmed order.
* `python -m scripts.verify_ipl_2026` prints pool coverage against the collected 2026 squad evidence. It never marks anything verified.
