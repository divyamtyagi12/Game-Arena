"""Plain data classes shared by the IPL engines (no I/O, no Telegram, no database)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

ATTRS = (
    "batting_rating", "bowling_rating", "fielding_rating", "wicketkeeping_rating",
    "batting_consistency", "bowling_consistency", "power_hitting", "strike_rotation",
    "death_overs_batting", "death_overs_bowling", "spin_effectiveness", "pace_effectiveness",
    "overall_rating",
)
ROLES = ("BAT", "BOWL", "AR", "WK")


@dataclass(frozen=True)
class Ratings:
    """HIDDEN attributes (0-100). Never rendered to Telegram, never returned by a public API."""
    batting_rating: int = 40
    bowling_rating: int = 15
    fielding_rating: int = 50
    wicketkeeping_rating: int = 15
    batting_consistency: int = 45
    bowling_consistency: int = 40
    power_hitting: int = 40
    strike_rotation: int = 50
    death_overs_batting: int = 40
    death_overs_bowling: int = 20
    spin_effectiveness: int = 15
    pace_effectiveness: int = 15
    overall_rating: int = 40

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any] | None) -> "Ratings":
        """Tolerates a missing rating record (edge case): unknown attributes fall back to neutral defaults."""
        if not m:
            return cls()
        return cls(**{a: int(m[a]) for a in ATTRS if m.get(a) is not None})

    def as_dict(self) -> dict[str, int]:
        return {a: getattr(self, a) for a in ATTRS}


@dataclass(frozen=True)
class Player:
    id: str
    name: str
    role: str                      # BAT | BOWL | AR | WK
    bowling_type: str = "NONE"     # PACE | SPIN | NONE
    batting_style: str | None = None
    ratings: Ratings = field(default_factory=Ratings)


@dataclass(frozen=True)
class Squad:
    team_id: int
    name: str
    short_name: str
    players: tuple[Player, ...]    # exactly 11 (in confirmed batting order when `ordered`)
    ordered: bool = False          # True = the manager's confirmed batting order is in `players`; engine must not re-sort

    def __post_init__(self) -> None:
        if len(self.players) != 11:
            raise ValueError(f"a squad has exactly 11 players, got {len(self.players)}")
        if len({p.id for p in self.players}) != 11:
            raise ValueError("duplicate player in squad")

    @classmethod
    def from_rpc(cls, row: Mapping[str, Any]) -> "Squad":
        rows = list(row["players"])
        ordered = bool(row.get("batting_order_confirmed")) and all(p.get("batting_order") for p in rows)
        if ordered:
            rows.sort(key=lambda p: int(p["batting_order"]))
        players = tuple(Player(id=str(p["id"]), name=p["name"], role=p["role"],
                               bowling_type=p.get("bowling_type") or "NONE",
                               batting_style=p.get("batting_style"),
                               ratings=Ratings.from_mapping(p.get("ratings")))
                        for p in rows)
        return cls(team_id=int(row["team_id"]), name=row["name"], short_name=row["short_name"], players=players,
                   ordered=ordered)
