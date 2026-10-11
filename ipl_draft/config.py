"""IPL Draft settings (env vars, all optional). Kept out of the shared config.py so the other games are untouched."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        v = int(raw) if raw else default
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def _float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        v = float(raw) if raw else default
    except ValueError:
        v = default
    return max(lo, min(hi, v))


@dataclass(frozen=True)
class IplSettings:
    min_humans: int = 2
    max_humans: int = 8
    draft_seconds: int = 45                 # time to choose each cricketer
    auto_after_timeouts: int = 2            # consecutive missed deadlines before the rest of the draft is automatic
    system_announce_delay: float = 6.0      # pause after "SYSTEM DRAFT COMPLETE" before the league starts
    matchday_delay: float = 4.0             # pause between simulated matchdays (keeps Telegram edits well under limits)
    summary_every: int = 5                  # send a fresh compact summary message every N matchdays (edits otherwise)
    playoff_delay: float = 8.0              # pause between playoff matches
    tick_seconds: float = 2.5               # worker poll interval
    store_ball_events: bool = True          # persist every delivery (ipl_ball_events)
    pool_margin: int = 20                   # spare players required on top of the capacity formula
    seed_salt: str = ""                     # optional extra entropy for match seeds
    max_failures: int = 5                   # consecutive worker errors before FAILED_RECOVERABLE
    batting_order_seconds: int = 300        # time a manager has to confirm the batting order (persisted deadline)
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "IplSettings":
        mn = _int("IPL_MIN_PLAYERS", 2, 2, 8)
        return cls(
            min_humans=mn, max_humans=_int("IPL_MAX_PLAYERS", 8, mn, 8),
            draft_seconds=_int("IPL_DRAFT_SECONDS", 45, 5, 600),
            auto_after_timeouts=_int("IPL_AUTO_AFTER_TIMEOUTS", 2, 1, 11),
            system_announce_delay=_float("IPL_SYSTEM_ANNOUNCE_DELAY", 6.0, 0, 120),
            matchday_delay=_float("IPL_MATCHDAY_DELAY", 4.0, 0, 120),
            summary_every=_int("IPL_SUMMARY_EVERY", 5, 1, 100),
            playoff_delay=_float("IPL_PLAYOFF_DELAY", 8.0, 0, 300),
            tick_seconds=_float("IPL_TICK_SECONDS", 2.5, 0.2, 60),
            store_ball_events=os.getenv("IPL_STORE_BALL_EVENTS", "true").strip().lower() not in ("0", "false", "no"),
            pool_margin=_int("IPL_POOL_MARGIN", 20, 0, 200),
            seed_salt=os.getenv("IPL_SEED_SALT", "").strip(),
            max_failures=_int("IPL_MAX_FAILURES", 5, 1, 50),
            batting_order_seconds=_int("IPL_BATTING_ORDER_SECONDS", 300, 10, 3600),
            enabled=os.getenv("IPL_ENABLED", "true").strip().lower() not in ("0", "false", "no"),
        )
