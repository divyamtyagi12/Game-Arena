"""Shared IPL runtime object (kept in application.bot_data['ipl']): settings, repository, rng, in-memory helpers.

Everything authoritative lives in PostgreSQL; the dicts here are only caches / re-entrancy guards, so losing them
(process restart) loses nothing.
"""
from __future__ import annotations

import os
import random
import socket
from dataclasses import dataclass, field
from typing import Any

from .config import IplSettings
from .engine.model import Squad
from .repository import IplRepo


@dataclass
class IplRuntime:
    ctx: Any                                  # services.context.AppContext (bot, users, spawn, lock)
    repo: IplRepo
    s: IplSettings
    worker_id: str = field(default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}:{random.randrange(10**6)}")
    rng: random.Random = field(default_factory=random.SystemRandom)
    squads: dict[int, dict[int, Squad]] = field(default_factory=dict)
    running: set[int] = field(default_factory=set)
    failures: dict[int, int] = field(default_factory=dict)
    kicked: set[int] = field(default_factory=set)
    progress_pending: set[int] = field(default_factory=set)
    progress_last: dict[int, float] = field(default_factory=dict)
    retry_after: dict[int, float] = field(default_factory=dict)
    sleep: Any = None                          # injectable (tests set a no-op)

    @classmethod
    def create(cls, ctx: Any, settings: IplSettings | None = None) -> "IplRuntime":
        return cls(ctx=ctx, repo=IplRepo(ctx.db), s=settings or IplSettings.from_env())

    @property
    def bot_username(self) -> str:
        return getattr(self.ctx, "bot_username", "")

    async def pause(self, seconds: float) -> None:
        import asyncio
        if seconds <= 0:
            return
        await (self.sleep or asyncio.sleep)(seconds)
