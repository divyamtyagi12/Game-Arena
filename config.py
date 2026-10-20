"""Configuration loaded from environment variables (never hardcode secrets)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if lo is not None and value < lo:
        raise ConfigError(f"{name} must be >= {lo}")
    if hi is not None and value > hi:
        raise ConfigError(f"{name} must be <= {hi}")
    return value


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    supabase_url: str
    supabase_key: str
    run_mode: str = "polling"
    webhook_url: str = ""
    webhook_path: str = "anime-draft-webhook"
    webhook_secret: str = ""
    webhook_listen: str = "0.0.0.0"
    webhook_port: int = 8443
    min_players: int = 3
    max_players: int = 8
    clash_delay: float = 2.0
    draft_timeout_minutes: int = 20
    auto_seed: bool = True
    owner_ids: frozenset[int] = frozenset()
    log_level: str = "INFO"
    # ── Number Wars ──
    nw_min_players: int = 3
    nw_max_players: int = 20
    nw_round_seconds: int = 30
    nw_lobby_countdown: int = 60      # auto-start this many seconds after the minimum is reached (0 = off)
    nw_max_rounds: int = 30
    nw_between_rounds: float = 4.0    # pause between a result and the next round

    @property
    def secrets(self) -> list[str]:
        """Values that must never appear in logs."""
        return [s for s in (self.telegram_token, self.supabase_key, self.webhook_secret) if s]


def load_settings() -> Settings:
    load_dotenv()
    required = ("TELEGRAM_BOT_TOKEN", "SUPABASE_URL", "SUPABASE_KEY")
    missing = [n for n in required if not os.getenv(n, "").strip()]
    if missing:
        raise ConfigError(
            "Missing required environment variables: " + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )

    run_mode = os.getenv("RUN_MODE", "polling").strip().lower()
    if run_mode not in {"polling", "webhook"}:
        raise ConfigError("RUN_MODE must be 'polling' or 'webhook'")
    webhook_url = os.getenv("WEBHOOK_URL", "").strip()
    if run_mode == "webhook" and not webhook_url:
        raise ConfigError("WEBHOOK_URL is required when RUN_MODE=webhook")
    webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if run_mode == "webhook" and not webhook_secret:
        raise ConfigError("WEBHOOK_SECRET is required when RUN_MODE=webhook")

    min_players = _int("MIN_PLAYERS", 3, lo=2, hi=16)
    max_players = _int("MAX_PLAYERS", 8, lo=min_players, hi=16)

    nw_min = _int("NW_MIN_PLAYERS", 3, lo=2, hi=20)

    owners: set[int] = set()
    for part in os.getenv("OWNER_IDS", "").split(","):
        part = part.strip()
        if part:
            try:
                owners.add(int(part))
            except ValueError as exc:
                raise ConfigError("OWNER_IDS must be comma-separated integers") from exc

    return Settings(
        telegram_token=os.environ["TELEGRAM_BOT_TOKEN"].strip(),
        supabase_url=os.environ["SUPABASE_URL"].strip().rstrip("/"),
        supabase_key=os.environ["SUPABASE_KEY"].strip(),
        run_mode=run_mode,
        webhook_url=webhook_url,
        webhook_path=os.getenv("WEBHOOK_PATH", "anime-draft-webhook").strip().strip("/") or "anime-draft-webhook",
        webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
        webhook_listen=os.getenv("WEBHOOK_LISTEN", "0.0.0.0").strip(),
        webhook_port=_int("WEBHOOK_PORT", 8443, lo=1, hi=65535),
        min_players=min_players,
        max_players=max_players,
        clash_delay=_float("CLASH_DELAY", 2.0),
        draft_timeout_minutes=_int("DRAFT_TIMEOUT_MINUTES", 20, lo=0),
        auto_seed=_bool("AUTO_SEED", True),
        owner_ids=frozenset(owners),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        nw_min_players=nw_min,
        nw_max_players=_int("NW_MAX_PLAYERS", 20, lo=nw_min, hi=50),
        nw_round_seconds=_int("NW_ROUND_SECONDS", 30, lo=10, hi=300),
        nw_lobby_countdown=_int("NW_LOBBY_COUNTDOWN", 60, lo=0, hi=3600),
        nw_max_rounds=_int("NW_MAX_ROUNDS", 30, lo=1, hi=200),
        nw_between_rounds=_float("NW_BETWEEN_ROUNDS", 4.0),
    )
