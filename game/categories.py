"""The five draft / clash categories (order = draft order = clash order)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CategoryInfo:
    key: str
    emoji: str
    label: str

    @property
    def column(self) -> str:
        """Column name in the characters / teams tables."""
        return self.key.lower()

    @property
    def display(self) -> str:
        return f"{self.emoji} {self.label}"


CATEGORIES: tuple[CategoryInfo, ...] = (
    CategoryInfo("ATTACK", "⚔️", "Attack"),
    CategoryInfo("TANKING", "🏰", "Tanking"),
    CategoryInfo("SPEED", "⚡", "Speed"),
    CategoryInfo("HEALING", "💚", "Healing"),
    CategoryInfo("INTELLIGENCE", "🧠", "Intelligence"),
)
CATEGORY_KEYS: tuple[str, ...] = tuple(c.key for c in CATEGORIES)
CATEGORY_BY_KEY: dict[str, CategoryInfo] = {c.key: c for c in CATEGORIES}
TEAM_SIZE = len(CATEGORIES)
OFFER_SIZE = 6
