"""Fakes for the Telegram/app layer used by the IPL integration tests.

The database is REAL PostgreSQL (tests.support.pgdb); only Telegram and the pre-existing repositories (users/groups)
are faked. This verifies our services/handlers and the SQL, not python-telegram-bot itself.
"""
from __future__ import annotations

import asyncio
import itertools
import random
from dataclasses import dataclass, field
from typing import Any

from telegram.error import Forbidden

from tests.support import pgdb


@dataclass
class Msg:
    message_id: int
    chat_id: int
    text: str
    markup: Any = None


class FakeBot:
    def __init__(self) -> None:
        self._ids = itertools.count(1000)
        self.messages: dict[tuple[int, int], Msg] = {}
        self.sent: list[Msg] = []
        self.edits = 0
        self.blocked: set[int] = set()
        self.admins: set[int] = set()
        self.dm_fail_after: dict[int, int] = {}           # user → allow N more DMs then act blocked

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if chat_id in self.blocked:
            raise Forbidden("bot was blocked by the user")
        if chat_id in self.dm_fail_after:
            if self.dm_fail_after[chat_id] <= 0:
                raise Forbidden("bot was blocked by the user")
            self.dm_fail_after[chat_id] -= 1
        m = Msg(next(self._ids), chat_id, text, reply_markup)
        self.messages[(chat_id, m.message_id)] = m
        self.sent.append(m)
        return m

    async def edit_message_text(self, text, chat_id=None, message_id=None, parse_mode=None, reply_markup=None):
        if (chat_id, message_id) not in self.messages:
            from telegram.error import BadRequest
            raise BadRequest("message to edit not found")
        self.messages[(chat_id, message_id)] = Msg(message_id, chat_id, text, reply_markup)
        self.edits += 1

    async def delete_message(self, chat_id, message_id):
        self.messages.pop((chat_id, message_id), None)

    async def send_chat_action(self, chat_id, action):
        if chat_id in self.blocked:
            raise Forbidden("blocked")

    async def get_chat_member(self, chat_id, user_id, **kwargs):
        class M: status = "administrator" if user_id in self.admins else "member"
        return M()

    def last(self, chat_id: int) -> Msg | None:
        for m in reversed(self.sent):
            if m.chat_id == chat_id:
                return self.messages.get((m.chat_id, m.message_id), m)
        return None

    def texts(self, chat_id: int) -> list[str]:
        return [m.text for m in self.sent if m.chat_id == chat_id]


class FakeUsers:
    """Writes to the real `users` table (FKs need the rows) — psql, not PostgREST."""
    def __init__(self) -> None:
        self.dm: dict[int, bool] = {}

    async def upsert(self, user_id, username, first_name, dm_started=None):
        if dm_started is not None:
            self.dm[user_id] = dm_started
        await asyncio.to_thread(pgdb.run_sql,
            f"insert into users (id, username, first_name) values ({int(user_id)}, null, {pgdb._q(first_name or '')}) "
            "on conflict (id) do update set first_name = excluded.first_name;")

    async def get(self, user_id):
        return {"id": user_id, "dm_started": self.dm.get(user_id, False)}

    async def set_dm(self, user_id, started):
        self.dm[user_id] = started


class FakeGroups:
    async def upsert(self, group_id, title):
        await asyncio.to_thread(pgdb.run_sql,
            f"insert into groups (id, title) values ({int(group_id)}, {pgdb._q(title or '')}) "
            "on conflict (id) do update set title = excluded.title;")


class _None:
    async def active_for_group(self, *_a): return None
    async def active_for_user(self, *_a): return None


@dataclass
class FakeCtx:
    db: Any
    bot: FakeBot = field(default_factory=FakeBot)
    users: FakeUsers = field(default_factory=FakeUsers)
    groups: FakeGroups = field(default_factory=FakeGroups)
    games: _None = field(default_factory=_None)
    nw: _None = field(default_factory=_None)
    bot_username: str = "ipl_test_bot"
    rng: random.Random = field(default_factory=lambda: random.Random(7))
    _locks: dict = field(default_factory=dict)
    tasks: set = field(default_factory=set)

    def lock(self, key):
        return self._locks.setdefault(key, asyncio.Lock())

    def spawn(self, coro, name=None):
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def drain(self):
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=False)


class FakeUser:
    def __init__(self, uid: int, name: str | None = None) -> None:
        self.id, self.first_name, self.username = uid, name or f"User{uid}", None


class FakeChat:
    def __init__(self, cid: int, kind: str = "supergroup", title: str = "Test Group") -> None:
        self.id, self.type, self.title = cid, kind, title


class FakeMessage:
    def __init__(self, chat, message_id: int, data: str | None = None) -> None:
        self.chat, self.message_id = chat, message_id


class FakeQuery:
    """Stands in for telegram.CallbackQuery. `answers` records every toast so tests can assert on them."""
    def __init__(self, data: str, user: FakeUser, chat: FakeChat, message_id: int) -> None:
        self.data, self.from_user, self.message = data, user, FakeMessage(chat, message_id)
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
