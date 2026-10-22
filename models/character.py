from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from game.categories import CATEGORY_KEYS

RARITY_EMOJI = {"LEGENDARY": "🌟", "EPIC": "💎", "RARE": "🔷", "COMMON": "⚪"}
SERIES_TAG = {"Re:ZERO": "Re:ZERO", "Black Clover": "Black Clover", "Death Note": "Death Note",
              "Ben 10": "Ben 10", "Hunter x Hunter": "Hunter x Hunter", "One Punch Man": "One Punch Man"}


@dataclass(eq=False)
class Character:
    id: str
    name: str
    series: str
    rarity: str
    description: str
    abilities: tuple[str, ...]
    stats: dict[str, int]            # category key -> rating (1-100)
    categories: frozenset[str]       # categories this character may be offered in
    image_url: str | None = None
    image_file_id: str | None = None

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Character) and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)

    def rating(self, category: str) -> int:
        return self.stats[category]

    @property
    def peak_rating(self) -> int:
        """Best rating among the categories this character is eligible for."""
        return max(self.stats[c] for c in self.categories) if self.categories else 0

    @property
    def short_name(self) -> str:
        return self.name.split()[0]

    @classmethod
    def from_row(cls, row: dict[str, Any], categories: Iterable[str]) -> "Character":
        abilities = row.get("abilities") or []
        return cls(
            id=row["id"],
            name=row["name"],
            series=row["series"],
            rarity=row["rarity"],
            description=row.get("description") or "",
            abilities=tuple(abilities),
            stats={k: int(row[k.lower()]) for k in CATEGORY_KEYS},
            categories=frozenset(categories),
            image_url=row.get("image_url") or None,
            image_file_id=row.get("image_file_id") or None,
        )
