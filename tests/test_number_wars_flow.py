"""End-to-end test of the Number Wars service (round loop, early close, deadline close, resolution,
elimination, finish, announcement, cancel, crash-resume) with an in-memory stand-in for the database
and a fake Telegram bot. The SQL functions themselves are exercised separately against Postgres."""
import asyncio
import random
import time
from types import SimpleNamespace

from game import number_wars as engine
from services import number_wars_service as svc


# ───────────────────────── fakes ─────────────────────────
class FakeBot:
    def __init__(self):
        self.sent, self.edits, self._id = [], [], 1000

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self._id += 1
        self.sent.append((chat_id, text, self._id))
        return SimpleNamespace(message_id=self._id)

    async def edit_message_text(self, text, chat_id=None, message_id=None, parse_mode=None, reply_markup=None):
        self.edits.append((chat_id, message_id, text))

    async def send_chat_action(self, chat_id, action):
        return True

    async def delete_message(self, chat_id, message_id):
        return True


class FakeNW:
    """Mirrors the semantics of database/number_wars.sql (state machine + idempotency)."""

    def __init__(self, uids, round_seconds):
        self.match = {"id": 1, "group_id": -1, "host_id": uids[0], "status": "ACTIVE", "min_players": 3,
                      "max_players": 20, "current_round": 0, "zero_streak": 0, "winner_ids": [],
                      "end_reason": None, "announced": False, "lobby_message_id": None}
        self.pl = {u: {"user_id": u, "display_name": f"P{u}", "hp": engine.MAX_HP, "alive": True, "round_wins": 0,
                       "total_distance": 0.0, "eliminated_round": None} for u in uids}
        self.rounds, self.subs, self.secs = {}, {}, round_seconds
        self.applied = 0

    async def get_match(self, _):
        return dict(self.match)

    async def players(self, _):
        return [dict(p) for p in self.pl.values()]

    async def open_round(self, _m, seconds):
        if self.match["status"] != "ACTIVE":
            return None
        live = [r for r in self.rounds.values() if r["status"] != "RESOLVED"]
        if live:
            r = live[-1]
            return {"created": False, "round": dict(r), "seconds_left": max(0, r["_dl"] - time.monotonic())}
        n = self.match["current_round"] + 1
        sd = self.match["zero_streak"] >= 3
        mult = random.choice([0.5, 0.8, 1.2, 1.5]) if sd else 0.8
        r = {"id": n, "match_id": 1, "round_number": n, "status": "OPEN", "multiplier": mult,
             "sudden_death": sd, "_dl": time.monotonic() + seconds}
        self.rounds[n] = r
        self.subs[n] = {}
        self.match["current_round"] = n
        return {"created": True, "round": dict(r), "seconds_left": float(seconds)}

    def _alive(self):
        return [p for p in self.pl.values() if p["alive"]]

    async def close_round(self, rid):
        r = self.rounds[rid]
        if r["status"] != "OPEN":
            return r["status"]
        if time.monotonic() >= r["_dl"] or len(self.subs[rid]) >= len(self._alive()):
            r["status"] = "CLOSED"
            return "CLOSED"
        return "OPEN"

    async def submit(self, rid, uid, n):
        r = self.rounds[rid]
        if r["status"] != "OPEN" or time.monotonic() >= r["_dl"]:
            return {"status": "CLOSED"}
        if not self.pl.get(uid, {}).get("alive"):
            return {"status": "NOT_IN"}
        if uid in self.subs[rid]:
            return {"status": "DUPLICATE"}
        self.subs[rid][uid] = n
        return {"status": "OK", "match_id": 1, "all_in": len(self.subs[rid]) >= len(self._alive())}

    async def submissions(self, rid):
        return dict(self.subs[rid])

    async def apply_round(self, rid, outcome, end):
        r = self.rounds[rid]
        if r["status"] != "CLOSED" or self.match["status"] != "ACTIVE":
            return False
        for x in outcome.results:
            p = self.pl[x.user_id]
            if not p["alive"]:
                continue
            p["hp"] = max(0, min(engine.MAX_HP, p["hp"] + x.hp_delta))
            p["round_wins"] += int(x.is_winner)
            p["total_distance"] += float(x.distance or 0)
            if p["hp"] <= 0:
                p["alive"], p["eliminated_round"] = False, r["round_number"]
        r["status"] = "RESOLVED"
        self.match["zero_streak"] = 0 if r["sudden_death"] else (
            self.match["zero_streak"] + 1 if outcome.zero_damage else 0)
        self.applied += 1
        if end:
            self.match.update(status="FINISHED", winner_ids=list(end[0]), end_reason=end[1])
        return True

    async def claim_announce(self, _):
        if self.match["announced"] or self.match["status"] != "FINISHED":
            return False
        self.match["announced"] = True
        return True

    async def release_announce(self, _):
        self.match["announced"] = False

    async def update_match(self, _m, **f):
        self.match.update(f)

    async def cancel(self, _):
        if self.match["status"] in ("LOBBY", "ACTIVE"):
            self.match["status"] = "CANCELLED"
            return True
        return False


class FakeCtx:
    def __init__(self, uids, round_seconds=0.4, max_rounds=30):
        self.nw = FakeNW(uids, round_seconds)
        self.bot = FakeBot()
        self.settings = SimpleNamespace(nw_round_seconds=round_seconds, nw_max_rounds=max_rounds,
                                        nw_between_rounds=0.02, nw_lobby_countdown=0, nw_min_players=3)
        self.running_nw, self.nw_countdowns = set(), set()
        self.nw_events, self.nw_prompts, self.nw_round_msgs = {}, {}, {}
        self.users = SimpleNamespace(set_dm=lambda *a, **k: asyncio.sleep(0))
        self._locks, self._tasks = {}, set()

    def lock(self, key):
        return self._locks.setdefault(key, asyncio.Lock())

    def spawn(self, coro, name=None):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t


async def drive_players(ctx, behaviour, stop):
    """Plays the part of the DM handler: submit like nw_callback does and wake the loop when all are in."""
    seen = set()
    while not stop.is_set():
        for rid, r in list(ctx.nw.rounds.items()):
            if r["status"] == "OPEN" and rid not in seen:
                seen.add(rid)
                for u in [p["user_id"] for p in ctx.nw._alive()]:
                    n = behaviour(u, r["round_number"])
                    if n is None:
                        continue
                    res = await ctx.nw.submit(rid, u, n)
                    if res["status"] == "OK" and res["all_in"]:
                        ev = ctx.nw_events.get(res["match_id"])
                        if ev:
                            ev.set()
        await asyncio.sleep(0.005)


async def play(ctx, behaviour, timeout=60):
    stop = asyncio.Event()
    driver = asyncio.create_task(drive_players(ctx, behaviour, stop))
    try:
        await asyncio.wait_for(svc.run_match(ctx, 1), timeout)
    finally:
        stop.set()
        await driver
    for t in list(ctx._tasks):
        await t


# ───────────────────────── tests ─────────────────────────
def test_full_match_with_a_player_who_always_misses():
    ctx = FakeCtx([1, 2, 3, 4])
    rng = random.Random(7)
    asyncio.run(play(ctx, lambda u, n: None if u == 4 else rng.randint(1, 100)))
    m = ctx.nw.match
    assert m["status"] == "FINISHED" and m["announced"] and len(m["winner_ids"]) >= 1
    assert 4 not in m["winner_ids"]                                   # −2 HP every round → out in 3 rounds (5 HP)
    assert ctx.nw.pl[4]["alive"] is False and ctx.nw.pl[4]["eliminated_round"] == 3
    texts = [t for _c, t, _i in ctx.bot.sent] + [t for _c, _i, t in ctx.bot.edits]
    assert any("NUMBER WARS — FINAL" in t for t in texts)             # champion announced exactly once
    assert sum("NUMBER WARS — FINAL" in t for t in texts) == 1
    assert any("ROUND 1 — RESULTS" in t for t in texts)               # banner was edited into results
    assert any(c == 4 and "eliminated" in t for c, t, _i in ctx.bot.sent)   # elimination DM
    assert all(0 <= p["hp"] <= engine.MAX_HP for p in ctx.nw.pl.values())


def test_round_closes_early_when_everyone_locks_in():
    ctx = FakeCtx([1, 2, 3], round_seconds=30)                        # a 30 s deadline must NOT be waited out
    t0 = time.monotonic()
    asyncio.run(play(ctx, lambda u, n: {1: 1, 2: 100, 3: 50}[u] if n <= 2 else 1, timeout=20))
    assert time.monotonic() - t0 < 10
    assert ctx.nw.match["status"] == "FINISHED"


def test_everyone_picking_same_number_triggers_sudden_death_then_round_cap():
    ctx = FakeCtx([1, 2, 3], round_seconds=5, max_rounds=8)
    asyncio.run(play(ctx, lambda u, n: 50))
    m = ctx.nw.match
    assert m["status"] == "FINISHED" and m["end_reason"] == engine.END_ROUND_LIMIT and m["current_round"] == 8
    assert sorted(m["winner_ids"]) == [1, 2, 3]                       # nobody ever lost HP → joint champions
    sd = [r for r in ctx.nw.rounds.values() if r["sudden_death"]]
    assert sd and sd[0]["round_number"] == 4                          # after exactly 3 zero-damage rounds
    assert all(r["multiplier"] in (0.5, 0.8, 1.2, 1.5) for r in ctx.nw.rounds.values())


def test_deadline_closes_round_when_someone_never_answers():
    ctx = FakeCtx([1, 2, 3], round_seconds=0.3, max_rounds=1)
    asyncio.run(play(ctx, lambda u, n: None if u == 3 else 50))
    assert ctx.nw.pl[3]["hp"] == engine.MAX_HP - 2 and ctx.nw.pl[1]["hp"] == engine.MAX_HP       # missed → −2; tied winners → 0
    assert ctx.nw.match["status"] == "FINISHED"


def test_cancel_stops_the_loop_without_applying_anything():
    ctx = FakeCtx([1, 2, 3], round_seconds=30)

    async def scenario():
        task = asyncio.create_task(svc.run_match(ctx, 1))
        await asyncio.sleep(0.3)                                      # round 1 is open, nobody has answered
        match = await ctx.nw.get_match(1)
        assert match["status"] == "ACTIVE" and ctx.nw.rounds[1]["status"] == "OPEN"
        ctx.bot.sent.clear()
        await ctx.nw.cancel(1)
        ctx.nw_events[1].set()                                        # what cancel_match() does
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    assert ctx.nw.applied == 0 and ctx.nw.match["status"] == "CANCELLED"


def test_resume_after_restart_reuses_the_open_round_and_reprompts_only_missing_players():
    ctx = FakeCtx([1, 2, 3], round_seconds=0.6, max_rounds=1)

    async def scenario():
        # "crash": round 1 already opened + player 1 already locked a number before the restart
        opened = await ctx.nw.open_round(1, 0.6)
        assert opened["created"]
        await ctx.nw.submit(1, 1, 70)
        ctx.bot.sent.clear()
        stop = asyncio.Event()
        driver = asyncio.create_task(drive_players(ctx, lambda u, n: 10 if u != 1 else None, stop))
        await asyncio.wait_for(svc.run_match(ctx, 1), 20)             # what recover() spawns
        stop.set()
        await driver

    asyncio.run(scenario())
    assert ctx.nw.match["current_round"] == 1 and ctx.nw.applied == 1       # no duplicate round
    prompts = [c for c, t, _i in ctx.bot.sent if "Choose Your Number" in t]
    assert sorted(prompts) == [2, 3]                                       # player 1 already locked
    assert ctx.nw.subs[1][1] == 70                                         # their number survived the restart
    assert ctx.nw.match["status"] == "FINISHED"
