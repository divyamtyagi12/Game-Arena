# Codebase Fix Report

## Overview
This document details all fixes applied across Game-Arena to resolve confirmed reliability issues, establish resilient contracts between PostgreSQL and Python, enforce strict squad and draft validation, eliminate concurrency races, and protect the Telegram Bot UI.

---

## Files Modified & Created

### 1. `database/migration_ipl_v5.sql` (NEW)
- **Purpose**: Versioned SQL migration maintaining full backward compatibility with `migration_ipl_v4.sql`.
- **Modifications**:
  - **`ipl_pick` contract fix**: Updated return JSON to include `team_id`, `tournament_id`, `done`, `all_humans_done`, `round_no`, `auto`, and `user_id` alongside existing fields `status`, `player`, `picks_done`, `squad_complete`, and `humans_drafting`.
  - **Duplicate Player Prevention**: Checks if `player_id` already exists in `ipl_team_players` for the same team and returns `{"status": "ALREADY_HAVE"}`.
  - **`ipl_create_fixtures` transition**: Guarantees that `state` advances to `'LEAGUE_RUNNING'` even if fixtures already exist from a previous attempt.
  - **Atomic Lease Functions (`ipl_acquire_lease_v2`, `ipl_release_lease_v2`)**: Enforces atomic lease acquisition requiring `state != 'COMPLETED'` and lease timeout or ownership matching. Releasing requires exact `lease_owner = p_owner`.

### 2. `ipl_draft/engine/draft_engine.py`
- **Location**: `is_pick_legal(squad, cand)`
- **Fix**: Added explicit guard preventing duplicate player drafting into the same squad:
  ```python
  cand_id = cand.get("id")
  cand_name = cand.get("name")
  if any(p.get("id") == cand_id or (cand_name and p.get("name") == cand_name) for p in squad):
      return False
  ```
- **Preserved Rules**: Kept 11-player squad limit, minimum 3 bowlers, 1 wicketkeeper, and 1 all-rounder constraints.

### 3. `ipl_draft/keyboards.py`
- **Location**: `offer(offer, squad=None)`
- **Fix**: Updated keyboard generation to evaluate `is_pick_legal(squad, cand)` for candidate cards. Cards that violate role or duplicate constraints are presented disabled with `(Req)` tag.
- **Critical Requirement**: Maintained single-tap pick mechanism (`d:pick:{offer_id}:{pos}`). Never added confirm or reset buttons.

### 4. `ipl_draft/repository.py`
- **Location**: `IplRepo.pick()`, `IplRepo.lease()`, `IplRepo.release_lease()`
- **Fix**:
  - `pick()`: Implemented automatic contract normalization. If SQL returns missing fields (`team_id`, `tournament_id`, `done`), `pick()` transparently queries `get_offer(offer_id)` to hydrate missing keys, preventing `KeyError`.
  - `lease()` & `release_lease()`: Hardened fallback queries against lease stealing and prevented leasing completed tournaments.

### 5. `ipl_draft/services/draft_service.py`
- **Location**: `confirm_pick()`, `after_pick()`, `send_next_offer()`, `update_progress()`
- **Fix**:
  - `after_pick()`: Isolated Telegram `_edit_dm()` message edits within a safe `try...except` block so Telegram network/edit errors never stop `send_next_offer()` or tournament progression.
  - Normalized `tournament_id`, `team_id`, and `done` attributes defensively.
  - Handled 11th pick squad completion to trigger batting order workflow.

### 6. `ipl_draft/handlers/callbacks.py`
- **Location**: `handle_callback()`
- **Fix**: Handled `"ALREADY"`, `"ALREADY_HAVE"`, and `"ILLEGAL_PICK"` gracefully by calling `query.answer(text=...)` with clear feedback instead of letting errors bubble up.

### 7. `tests/test_audit_regressions.py` (NEW)
- **Purpose**: Regression test suite covering Issues A, B, C, D, and E with 13 focused test cases.

### 8. `tests/test_scalability_load.py` (NEW)
- **Purpose**: Concurrency and scalability load simulation testing 10, 50, 100, 500, and 1000 concurrent players.

---

## Compatibility & Safety Considerations
1. **Existing Databases**: `migration_ipl_v5.sql` uses `CREATE OR REPLACE FUNCTION` and introduces new functions with zero breaking changes to existing table schemas.
2. **Backward Compatibility**: Both Python code and PostgreSQL handle legacy responses seamlessly without failing.
3. **No Reset/Confirm Prompt**: Inline keyboards retain direct instant pick action.
