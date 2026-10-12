# Comprehensive Codebase Reliability Audit Report

## Executive Summary
This reliability audit examined the entire Game-Arena multi-game Telegram bot codebase, encompassing:
1. **IPL Draft** (Drafting engine, Match engine, Ball model, Standings, Playoffs, Workers, Repositories, Database functions)
2. **Anime Draft** (Lobby, character drafting, team confirmation, league, battles, points table, grand final)
3. **Number Wars** (Lobby, number submissions, round validation, scoring, elimination, progression, messaging)
4. **PostgreSQL / Supabase Database Architecture** (Migrations v1 through v5, RPC functions, transaction atomicity, lease safety)
5. **Telegram Bot API Integration** (Callback routing, error handling, rate limiting, inline keyboards, message rendering)

All confirmed issues were categorized by severity:
- **P0 (Critical Blocker)**: Crashes the game loop, causes irrecoverable state, or breaks progression.
- **P1 (High)**: Concurrency race condition, contract mismatch, or data integrity violation.
- **P2 (Medium)**: Broken UI resilience, notification failures under network/DM block, or double-tap vulnerabilities.
- **P3 (Low)**: Minor unhandled warning or transient inefficiency.

---

## Confirmed Issues Breakdown

### Issue 1: SQL/Python Response Contract Mismatch in `ipl_pick` (P0)
- **File**: `database/migration_ipl_v4.sql`, `ipl_draft/services/draft_service.py:108-142`, `ipl_draft/repository.py:165-174`
- **Root Cause**:
  In PostgreSQL function `ipl_pick()` (defined in `migration_ipl_v4.sql`), the returned JSON structure upon a successful pick was:
  ```json
  {"status": "OK", "player": ..., "picks_done": N, "squad_complete": bool, "humans_drafting": N}
  ```
  However, `draft_service.after_pick()` immediately dereferenced fields:
  ```python
  tid = int(res["tournament_id"])
  team_id = int(res["team_id"])
  done = bool(res["done"])
  all_humans_done = bool(res.get("all_humans_done", False))
  ```
- **Reproduction**:
  1. Start an IPL tournament draft.
  2. Pick any player from the Telegram inline keyboard (`d:pick:{offer_id}:{pos}`).
  3. Handler invokes `draft_service.confirm_pick()` which calls `after_pick()`.
  4. Execution raises `KeyError: 'tournament_id'` or `'team_id'`.
- **Impact**:
  Every single draft pick crashed before post-pick processing could run. The previous keyboard remained active, the next offer was never generated/sent, the 11th pick never transitioned to batting order, and tournament progression completely stalled.
- **Fix**:
  - Authored `database/migration_ipl_v5.sql` with updated `ipl_pick()` returning `{status, player, picks_done, squad_complete, humans_drafting, team_id, tournament_id, done, all_humans_done, round_no, auto, user_id}`.
  - In `ipl_draft/repository.py`, enhanced `IplRepo.pick()` with client-side normalization to backfill `team_id`, `tournament_id`, `done`, and `all_humans_done` from active offer records if missing.
  - In `ipl_draft/services/draft_service.py`, made `after_pick()` defensively query `rt.repo.get_offer()` and fallback safely to avoid any KeyError.

---

### Issue 2: Duplicate Player Selection Inside Same Squad (P1)
- **File**: `ipl_draft/engine/draft_engine.py:27-46`, `database/migration_ipl_v4.sql`
- **Root Cause**:
  `is_pick_legal()` verified role counts (bowlers, keepers, all-rounders against roster size) but did not check if the candidate player ID or candidate name already existed in `squad`. Similarly, `ipl_pick` in SQL checked `SELECT count(*) FROM ipl_team_players WHERE team_id = ... AND player_id = ...` but if player metadata varied or candidate IDs clashed, duplicate insertions were possible.
- **Reproduction**:
  1. Present an offer containing a player already picked in an earlier round (e.g. if candidate pool refreshed with overlapping IDs).
  2. Selecting the player allowed drafting the same player multiple times into one XI.
- **Impact**:
  Teams could draft identical players multiple times, violating cricket squad invariants and breaking match simulation line-ups.
- **Fix**:
  - In `ipl_draft/engine/draft_engine.py:is_pick_legal()`, added explicit duplicate validation:
    ```python
    if any(p.get("id") == cand_id or p.get("name") == cand_name for p in squad):
        return False
    ```
  - In `ipl_draft/keyboards.py`, integrated `is_pick_legal(squad, cand)` so buttons for illegal/duplicate players are disabled.
  - In `database/migration_ipl_v5.sql`, enforced SQL validation returning `{"status": "ALREADY_HAVE"}`.

---

### Issue 3: Inconsistent Unified Draft Legality Enforcement (P1)
- **File**: `ipl_draft/keyboards.py`, `ipl_draft/engine/draft_engine.py`, `ipl_draft/services/draft_service.py`
- **Root Cause**:
  Keyboard rendering permitted buttons to be clickable even when picking that player would mathematically make it impossible to fulfill minimum requirements (3 bowlers, 1 wicketkeeper, 1 all-rounder) within remaining picks. Python service had disparate logic from keyboard display.
- **Reproduction**:
  1. A user reaches Round 10 with 0 bowlers drafted.
  2. The offer displays a batter and a bowler.
  3. The user clicks the batter.
- **Impact**:
  The user could finalize an illegal squad missing mandatory roles.
- **Fix**:
  - Unified `is_pick_legal` across keyboards, draft service, auto-pick selection, and SQL.
  - Maintained single-tap pick UX (strictly adhering to prohibition of confirm/reset buttons).

---

### Issue 4: Telegram Edit Error Breaking Next Offer Delivery & Progression (P1)
- **File**: `ipl_draft/services/draft_service.py:108-142`
- **Root Cause**:
  In `draft_service.after_pick()`, updating the previous Telegram message via `_edit_dm()` was placed in the main execution path before delivering the next offer (`send_next_offer`) or checking completion (`advance`). If Telegram returned `BadRequest("Message is not modified")` or `TelegramError("Network error")`, the exception aborted `after_pick()` before the next offer was dispatched.
- **Reproduction**:
  1. Pick a card.
  2. A transient Telegram API latency or duplicate edit error occurs.
  3. Exception terminates `after_pick()`.
- **Impact**:
  The pick is saved in PostgreSQL, but the user never receives their next offer, stalling the tournament indefinitely.
- **Fix**:
  - Wrapped `_edit_dm()` in an isolated `try...except Exception` block in `after_pick()`, logging warnings without stopping next-offer delivery or tournament advancement.

---

### Issue 5: Callback Multi-Click & Idempotency Vulnerability (P2)
- **File**: `ipl_draft/handlers/callbacks.py`, `ipl_draft/repository.py`
- **Root Cause**:
  When a user rapidly double-tapped an inline pick button, the second callback encountered `{"status": "ALREADY"}` or `{"status": "ALREADY_HAVE"}`. The callback handler treated non-`OK` responses with generic errors or failed callbacks without answering Telegram query.
- **Reproduction**:
  1. Double click a card quickly on mobile.
- **Impact**:
  The second tap produced a Telegram spinning spinner or error popup.
- **Fix**:
  - Added explicit handling in `ipl_draft/handlers/callbacks.py` for `"ALREADY"`, `"ALREADY_HAVE"`, and `"ILLEGAL_PICK"`, answering callback query cleanly and informing the player.

---

### Issue 6: Insecure Tournament Database Lease Acquisition & Release (P0)
- **File**: `ipl_draft/repository.py:278-305`, `database/migration_ipl_v4.sql`
- **Root Cause**:
  In `IplRepo.lease()`, when RPC `ipl_acquire_lease` was not found, the fallback Python logic performed:
  ```python
  q = self.client.table("ipl_tournaments").select("id, lease_owner, lease_until, state").eq("id", tid)
  ...
  # Did not check if tournament was COMPLETED, allowing already finalized tournaments to be leased!
  ```
  Furthermore, `release_lease()` fell back to:
  ```python
  self.client.table("ipl_tournaments").update({"lease_owner": None, "lease_until": None}).eq("id", tid).execute()
  ```
  This unconditionally wiped leases regardless of whether the calling worker actually owned the lease, enabling one worker to steal or clear another active worker's lease.
- **Reproduction**:
  1. Worker A acquires lease for tournament 1.
  2. Worker B executes `release_lease(1, "worker_b")`.
  3. Worker A's active lease is cleared mid-flight, allowing Worker C to simulate concurrently.
- **Impact**:
  Multiple background workers could concurrently simulate playoff fixtures, produce duplicate match scorecards, and finalize completed tournaments twice.
- **Fix**:
  - In `database/migration_ipl_v5.sql`, defined atomic functions `ipl_acquire_lease_v2` and `ipl_release_lease_v2` enforcing:
    - Lease acquisition requires: `state != 'COMPLETED'` AND `(lease_owner IS NULL OR lease_owner = p_owner OR lease_until < now())`.
    - Lease release requires: `lease_owner = p_owner`.
  - In `ipl_draft/repository.py`, updated fallback methods to enforce strict state checks and ownership matching.

---

### Issue 7: Fixture Creation State Transition Deadlock (P1)
- **File**: `database/migration_ipl_v4.sql:ipl_create_fixtures`
- **Root Cause**:
  In `ipl_create_fixtures`, if fixtures were already present, the function returned `{"status": "OK", "already": true}` but failed to ensure `state` was updated to `'LEAGUE_RUNNING'` if it was still in `'FIXTURES_PENDING'`.
- **Reproduction**:
  1. A worker generates fixtures and crashes before state write.
  2. A recovering worker calls `ipl_create_fixtures`.
- **Impact**:
  The tournament remained trapped in `FIXTURES_PENDING` forever.
- **Fix**:
  - In `database/migration_ipl_v5.sql`, updated `ipl_create_fixtures` to always transition `state = 'LEAGUE_RUNNING'` even when `already = true`.

---

### Summary of Audit Classification
| Issue ID | Component | Severity | Description | Status |
|---|---|---|---|---|
| ISSUE-A | IPL Draft / DB | **P0** | Response contract mismatch in `ipl_pick` / `after_pick()` | **FIXED** |
| ISSUE-B | IPL Draft / Keyboards | **P1** | Squad duplicate player check & unified draft legality | **FIXED** |
| ISSUE-C | IPL Draft / Telegram | **P1** | DM edit failure blocking offer delivery; double-clicks | **FIXED** |
| ISSUE-D | IPL Draft / Simulation | **P0** | Database lease safety & worker concurrency races | **FIXED** |
| ISSUE-E | IPL Draft / SQL | **P1** | Fixture creation idempotent state transition deadlock | **FIXED** |
