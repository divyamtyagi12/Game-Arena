"""T20 match simulation: real ball-by-ball scorekeeping, winner derived from the simulated scores. Pure (rng injected).

State machine per innings: legal-ball counter, over/ball, strike rotation, wickets, extras, bowler quotas (max 4 overs,
no consecutive overs when avoidable), batting order, free hit after a no-ball. A match ends when the chasing side passes
the target, is all out, or finishes 20 overs; a tie goes to Super Overs (repeated, with a documented safety cap).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from .ball_model import (DEATH, MID, PP, aggression, batter_quality, bowler_quality, dismissal_kind, extras_probs,
                         outcome_probs, phase_of, power_quality, sample, sk)
from .model import Player, Squad
from .standings import nrr_balls

MAX_OVERS = 20
MAX_BOWLER_OVERS = 4
MAX_SUPER_OVERS = 5          # safety cap; after it the winner = more boundaries in the 20-over innings, then a seeded coin flip
BOWLER_POOL = 6


@dataclass
class BatLine:
    player: Player
    pos: int
    did_bat: bool = False
    runs: int = 0
    balls: int = 0
    fours: int = 0
    sixes: int = 0
    out: bool = False
    dismissal: str | None = None
    bowler_id: str | None = None
    fielder_id: str | None = None
    form: float = 0.0
    dots_run: int = 0


@dataclass
class BowlLine:
    player: Player
    balls: int = 0
    runs: int = 0
    wickets: int = 0
    maidens: int = 0
    wides: int = 0
    noballs: int = 0
    dots: int = 0
    over_runs: int = 0
    form: float = 0.0


@dataclass
class InningsResult:
    innings_no: int
    is_super_over: bool
    batting: Squad
    bowling: Squad
    runs: int
    wickets: int
    legal_balls: int
    extras: dict[str, int]
    fow: list[dict]
    target: int | None
    all_out: bool
    bat_lines: list[BatLine]
    bowl_lines: list[BowlLine]
    balls: list[list]
    death_runs: dict[str, int] = field(default_factory=dict)     # player_id -> runs in overs 17-20
    death_wkts: dict[str, int] = field(default_factory=dict)

    @property
    def balls_for_nrr(self) -> int:
        return nrr_balls(self.legal_balls, self.all_out)


def _form(rng: random.Random, consistency: int) -> float:
    return rng.gauss(0.0, 0.22 * (1.55 - consistency / 100.0))


def batting_order(squad: Squad) -> list[Player]:
    """The manager's CONFIRMED order when the squad carries one (squad.ordered), else the engine's rating-based order."""
    if squad.ordered:
        return list(squad.players)
    return sorted(squad.players, key=lambda p: -(0.7 * p.ratings.batting_rating + 0.3 * p.ratings.power_hitting
                                                  + (3 if p.role in ("BAT", "WK") else 0)))


def keeper_of(squad: Squad) -> Player:
    return max(squad.players, key=lambda p: (p.ratings.wicketkeeping_rating, p.role == "WK"))


class InningsSim:
    def __init__(self, rng: random.Random, bat: Squad, bowl: Squad, innings_no: int, *, target: int | None = None,
                 pitch: float = 1.0, super_over: bool = False, order: list[Player] | None = None,
                 bowlers: list[Player] | None = None, max_wkts: int = 10):
        self.rng, self.bat, self.bowl, self.no = rng, bat, bowl, innings_no
        self.target, self.pitch, self.so = target, pitch, super_over
        self.max_wkts = 2 if super_over else max_wkts
        self.max_overs = 1 if super_over else MAX_OVERS
        self.keeper = keeper_of(bowl)
        self.field_q = sk(sum(p.ratings.fielding_rating for p in bowl.players) / 11.0)
        ordered = order or batting_order(bat)
        self.lines = [BatLine(p, i + 1, form=_form(rng, p.ratings.batting_consistency)) for i, p in enumerate(ordered)]
        self.by_id = {l.player.id: l for l in self.lines}
        pool = bowlers or self._pick_pool()
        self.bowl_lines = {p.id: BowlLine(p, form=_form(rng, p.ratings.bowling_consistency)) for p in pool}
        self.runs = self.wkts = self.legal = 0
        self.extras = {"wides": 0, "noballs": 0, "byes": 0, "legbyes": 0}
        self.fow: list[dict] = []
        self.events: list[list] = []
        self.seq = 0
        self.death_runs: dict[str, int] = {}
        self.death_wkts: dict[str, int] = {}
        self.next_in = 2
        self.striker, self.non = self.lines[0], self.lines[1]
        self.striker.did_bat = self.non.did_bat = True
        self.free_hit = False
        self.prev_bowler: str | None = None

    # ── bowling plan ──
    def _pick_pool(self) -> list[Player]:
        ranked = sorted(self.bowl.players, key=lambda p: -(p.ratings.bowling_rating + (8 if p.role in ("BOWL", "AR") else 0)))
        return ranked[:BOWLER_POOL]

    def _choose_bowler(self, over: int) -> BowlLine:
        phase = phase_of(over)
        remaining_overs = self.max_overs - over
        elig = [b for b in self.bowl_lines.values() if b.balls < (24 if not self.so else 6)]
        if self.so:
            return max(elig, key=lambda b: b.player.ratings.bowling_rating + b.player.ratings.death_overs_bowling)
        non_consec = [b for b in elig if b.player.id != self.prev_bowler]
        cand = non_consec or elig
        # feasibility: never strand the remaining overs on bowlers whose quota is spent
        def score(b: BowlLine) -> float:
            q = bowler_quality(b.player, phase)
            quota_left = 4 - b.balls // 6
            need_bonus = 0.25 if quota_left * 1 >= remaining_overs else 0.0
            return q + 0.04 * quota_left + need_bonus + self.rng.gauss(0, 0.12)
        return max(cand, key=score)

    # ── scoring primitives ──
    def _swap(self) -> None:
        self.striker, self.non = self.non, self.striker

    def _new_batter(self, out_line: BatLine) -> bool:
        """Replace the dismissed batter. False when the innings is over (no batter left)."""
        if self.next_in >= len(self.lines) or self.wkts >= self.max_wkts:
            return False
        nl = self.lines[self.next_in]
        self.next_in += 1
        nl.did_bat = True
        if out_line is self.striker:
            self.striker = nl
        else:
            self.non = nl
        return True

    def _fielder_for(self, kind: str, bowler: Player) -> Player | None:
        if kind in ("bowled", "lbw"):
            return None
        if kind == "stumped":
            return self.keeper
        others = [p for p in self.bowl.players if p.id != bowler.id]
        if kind == "caught":
            x = self.rng.random()
            if x < 0.09:
                return bowler                                          # caught & bowled
            if x < 0.09 + (0.30 if bowler.bowling_type == "PACE" else 0.12):
                return self.keeper                                    # caught behind
        w = [max(5.0, p.ratings.fielding_rating) for p in others]
        return self.rng.choices(others, weights=w)[0]

    def _record(self, over: int, ball_no: int, bl: BowlLine, runs_bat: int, extra_type: str | None, extra_runs: int,
                wkind: str | None, out_player: BatLine | None, fielder: Player | None, legal: bool, striker_id: str,
                non_id: str) -> None:
        self.seq += 1
        self.events.append([self.seq, over, ball_no, striker_id, non_id, bl.player.id, runs_bat, extra_type or "",
                            extra_runs, wkind or "", out_player.player.id if out_player else "",
                            fielder.id if fielder else "", legal])

    # ── the delivery ──
    def run(self) -> InningsResult:
        rng = self.rng
        done = False
        for over in range(self.max_overs):
            if done:
                break
            bl = self._choose_bowler(over)
            self.prev_bowler = bl.player.id
            bl.over_runs = 0
            legal_in_over = 0
            deliveries_guard = 0
            while legal_in_over < 6 and not done:
                deliveries_guard += 1
                if deliveries_guard > 40:
                    raise RuntimeError("runaway over")
                phase = DEATH if self.so else phase_of(over)
                st, ns = self.striker, self.non
                wq = bowler_quality(bl.player, phase) + bl.form
                balls_left = self.max_overs * 6 - self.legal
                needed = (self.target - self.runs) if self.target is not None else None
                agg = aggression(phase, self.wkts, balls_left, needed, self.legal)
                p_wide, p_nb, p_bye, p_lb = extras_probs(wq, bl.player.ratings.bowling_consistency,
                                                          self.keeper.ratings.wicketkeeping_rating)
                if self.free_hit:
                    p_nb_eff = 0.0
                else:
                    p_nb_eff = p_nb
                x = rng.random()
                ball_no = legal_in_over + 1
                sid, nid = st.player.id, ns.player.id
                if x < p_wide:                                           # ── wide
                    extra = 1 + (4 if rng.random() < 0.025 else 0)
                    self.runs += extra
                    self.extras["wides"] += extra
                    bl.runs += extra
                    bl.wides += extra
                    bl.over_runs += extra
                    self._record(over, ball_no, bl, 0, "wide", extra, None, None, None, False, sid, nid)
                    if needed is not None and self.runs >= self.target:
                        done = True
                    continue
                if x < p_wide + p_nb_eff:                               # ── no-ball (free hit follows)
                    probs = outcome_probs(phase, batter_quality(st.player, phase) + st.form, power_quality(st.player),
                                          sk(st.player.ratings.strike_rotation), wq, agg, 1.0, self.field_q, self.pitch, True)
                    res = sample(rng, probs)
                    runs_bat = 0 if res == "W" else int(res)
                    total = 1 + runs_bat
                    self.runs += total
                    self.extras["noballs"] += 1
                    st.runs += runs_bat
                    st.balls += 1
                    st.fours += runs_bat == 4
                    st.sixes += runs_bat == 6
                    bl.runs += total
                    bl.noballs += 1
                    bl.over_runs += total
                    self.free_hit = True
                    self._record(over, ball_no, bl, runs_bat, "noball", 1, None, None, None, False, sid, nid)
                    if runs_bat % 2 == 1:
                        self._swap()
                    if self.target is not None and self.runs >= self.target:
                        done = True
                    continue

                # ── legal delivery
                free_hit = self.free_hit
                self.free_hit = False
                legal_in_over += 1
                self.legal += 1
                bl.balls += 1
                settle = 1.0 + 0.4 * max(0.0, (8 - st.balls) / 8.0)
                bq = batter_quality(st.player, phase) + st.form
                probs = outcome_probs(phase, bq, power_quality(st.player) + st.form * 0.5,
                                      sk(st.player.ratings.strike_rotation), wq, agg, settle, self.field_q,
                                      self.pitch, free_hit)
                y = rng.random()
                ex_type, ex_runs = None, 0
                if y < p_bye:
                    ex_type, ex_runs = "bye", 1 if rng.random() > 0.04 else 4
                elif y < p_bye + p_lb:
                    ex_type, ex_runs = "legbye", 1 if rng.random() > 0.03 else 4
                res = sample(rng, probs) if ex_type is None else "0"
                runs_bat, wkind, out_line, fielder = 0, None, None, None
                if res == "W":
                    wkind = dismissal_kind(rng, bl.player.bowling_type, free_hit)
                    out_line = st if (wkind != "run out" or rng.random() < 0.62) else ns
                    if wkind == "caught" or wkind == "stumped" or wkind == "run out":
                        fielder = self._fielder_for(wkind, bl.player)
                    if wkind == "run out":
                        runs_bat = 0
                else:
                    runs_bat = int(res)
                st.balls += 1
                st.runs += runs_bat
                st.fours += runs_bat == 4
                st.sixes += runs_bat == 6
                bl.runs += runs_bat
                bl.over_runs += runs_bat
                total = runs_bat
                if ex_type:
                    key = "byes" if ex_type == "bye" else "legbyes"
                    self.extras[key] += ex_runs
                    total += ex_runs
                self.runs += total
                if over >= 16 and not self.so:
                    self.death_runs[st.player.id] = self.death_runs.get(st.player.id, 0) + runs_bat
                if runs_bat == 0 and not ex_type and not wkind:
                    bl.dots += 1
                if wkind:
                    out_line.out = True
                    out_line.dismissal = wkind
                    credit = wkind in ("bowled", "lbw", "caught", "stumped")
                    out_line.bowler_id = bl.player.id if credit else None
                    out_line.fielder_id = fielder.id if fielder else None
                    self.wkts += 1
                    if credit:
                        bl.wickets += 1
                        if over >= 16 and not self.so:
                            self.death_wkts[bl.player.id] = self.death_wkts.get(bl.player.id, 0) + 1
                    self.fow.append({"wkt": self.wkts, "score": self.runs, "over": f"{over}.{ball_no}",
                                     "player": out_line.player.name, "player_id": out_line.player.id})
                    self._record(over, ball_no, bl, runs_bat, ex_type, ex_runs, wkind, out_line, fielder, True, sid, nid)
                    if not self._new_batter(out_line):
                        done = True
                else:
                    self._record(over, ball_no, bl, runs_bat, ex_type, ex_runs, None, None, None, True, sid, nid)
                    if (runs_bat + (ex_runs if ex_type else 0)) % 2 == 1:
                        self._swap()
                if self.wkts >= self.max_wkts:
                    done = True
                if self.target is not None and self.runs >= self.target:
                    done = True
            # end of over
            if legal_in_over == 6 and bl.over_runs == 0:
                bl.maidens += 1
            if legal_in_over == 6 and not done:
                self._swap()
        all_out = self.wkts >= self.max_wkts
        return InningsResult(
            innings_no=self.no, is_super_over=self.so, batting=self.bat, bowling=self.bowl, runs=self.runs,
            wickets=self.wkts, legal_balls=self.legal, extras=dict(self.extras), fow=self.fow, target=self.target,
            all_out=all_out, bat_lines=self.lines, bowl_lines=[b for b in self.bowl_lines.values() if b.balls or b.wides or b.noballs],
            balls=self.events, death_runs=self.death_runs, death_wkts=self.death_wkts)


# ───────────────────────── match ─────────────────────────
@dataclass
class MatchOutcome:
    team1: Squad                # batted first
    team2: Squad
    toss_winner: int
    toss_decision: str
    innings: list[InningsResult]
    winner: int | None
    result_type: str            # RUNS | WICKETS | TIE_SUPER_OVER | NO_RESULT
    margin: int | None
    super_overs: int
    pom: tuple[int, str] | None            # (team_id, player_id)
    best_bowler: tuple[int, str] | None    # (team_id, player_id)
    summary: str
    seed: str


def _super_over(rng: random.Random, first: Squad, second: Squad, no: int, used_bat: set, used_bowl: set):
    def order(sq: Squad) -> list[Player]:
        pool = [p for p in batting_order(sq) if (sq.team_id, p.id) not in used_bat] or batting_order(sq)
        best = sorted(pool, key=lambda p: -(p.ratings.power_hitting + p.ratings.death_overs_batting + p.ratings.batting_rating))
        return best[:3] + [p for p in batting_order(sq) if p not in best[:3]]

    def bowler(sq: Squad) -> list[Player]:
        pool = [p for p in sq.players if (sq.team_id, p.id) not in used_bowl] or list(sq.players)
        return [max(pool, key=lambda p: p.ratings.bowling_rating + p.ratings.death_overs_bowling)]

    i1 = InningsSim(rng, first, second, no, super_over=True, order=order(first), bowlers=bowler(second)).run()
    i2 = InningsSim(rng, second, first, no + 1, super_over=True, target=i1.runs + 1, order=order(second),
                    bowlers=bowler(first)).run()
    # keyed by (team, player): the same cricketer can be in both squads
    for inn, sq in ((i1, first), (i2, second)):
        for l in inn.bat_lines:
            if l.out:
                used_bat.add((sq.team_id, l.player.id))
    for inn, sq in ((i1, second), (i2, first)):
        for b in inn.bowl_lines:
            used_bowl.add((sq.team_id, b.player.id))
    return i1, i2


def simulate_match(sq_a: Squad, sq_b: Squad, rng: random.Random, *, seed: str = "",
                   order_a: list[Player] | None = None, order_b: list[Player] | None = None) -> MatchOutcome:
    """sq_a is the 'home' side. Winner is derived from the simulated ball-by-ball scores."""
    toss_winner = sq_a if rng.random() < 0.5 else sq_b
    pitch = max(0.88, min(1.12, rng.gauss(1.0, 0.05)))
    field_first = rng.random() < 0.62
    other = sq_b if toss_winner is sq_a else sq_a
    first, second = (other, toss_winner) if field_first else (toss_winner, other)
    decision = "FIELD" if field_first else "BAT"
    order1 = order_a if first is sq_a else order_b
    order2 = order_b if second is sq_b else order_a

    i1 = InningsSim(rng, first, second, 1, pitch=pitch, order=order1).run()
    i2 = InningsSim(rng, second, first, 2, target=i1.runs + 1, pitch=pitch, order=order2).run()
    innings = [i1, i2]
    super_overs = 0
    winner: Squad | None
    if i2.runs > i1.runs:
        winner, rtype, margin = second, "WICKETS", 10 - i2.wickets
        res = f"{second.name} won by {margin} wicket{'s' if margin != 1 else ''}"
    elif i1.runs > i2.runs:
        winner, rtype, margin = first, "RUNS", i1.runs - i2.runs
        res = f"{first.name} won by {margin} run{'s' if margin != 1 else ''}"
    else:
        used_bat: set = set()
        used_bowl: set = set()
        so_first, so_second = second, first               # the side that batted second bats first in the Super Over
        winner = None
        n = 3
        while super_overs < MAX_SUPER_OVERS and winner is None:
            s1, s2 = _super_over(rng, so_first, so_second, n, used_bat, used_bowl)
            innings += [s1, s2]
            super_overs += 1
            n += 2
            if s2.runs > s1.runs:
                winner = so_second
            elif s1.runs > s2.runs:
                winner = so_first
            else:
                so_first, so_second = so_second, so_first
        if winner is None:                                  # documented safety fallback
            def bnd(inn):
                return sum(l.fours + l.sixes for l in inn.bat_lines)
            b1, b2 = bnd(i1), bnd(i2)
            winner = first if b1 > b2 else second if b2 > b1 else (first if rng.random() < 0.5 else second)
        rtype, margin = "TIE_SUPER_OVER", None
        res = f"Match tied — {winner.name} won the Super Over" + (f" (after {super_overs} Super Overs)" if super_overs > 1 else "")
    pom, best = _awards(innings[:2], winner)
    return MatchOutcome(first, second, toss_winner.team_id, decision, innings, winner.team_id, rtype, margin,
                        super_overs, pom, best, res, seed)


# ───────────────────────── Player of the Match / best bowler ─────────────────────────
def impact(innings: list[InningsResult], winner_team: int) -> dict[tuple[int, str], float]:
    """Documented performance score, computed ONLY from the simulated scorecard:
       batting  runs + fours + 2*sixes  (+ strike-rate bonus 0.1*(SR-130) when 15+ runs; x1.15 in a successful chase)
       bowling  25*wickets + 8*maidens + economy bonus 3*(8.5-econ) per 2+ overs
       fielding 8 per catch/run-out, 10 per stumping      pressure  +0.3/run and +6/wicket in overs 17-20
       players of the losing side count at 85% (a POM from the losing team needs a much bigger performance)."""
    pts: dict[tuple[int, str], float] = {}          # keyed by (team_id, player_id): a cricketer may play for BOTH sides
    chase_win = len(innings) > 1 and innings[1].batting.team_id == winner_team and innings[1].runs > innings[0].runs
    for inn in innings:
        for l in inn.bat_lines:
            if not l.did_bat:
                continue
            p = l.runs + l.fours + 2 * l.sixes
            if l.runs >= 15 and l.balls:
                p += 0.1 * (l.runs / l.balls * 100 - 130)
            if chase_win and inn.batting.team_id == winner_team:
                p *= 1.15
            p += 0.3 * inn.death_runs.get(l.player.id, 0)
            k = (inn.batting.team_id, l.player.id)
            pts[k] = pts.get(k, 0) + p
            fk = (inn.bowling.team_id, l.fielder_id)
            if l.out and l.fielder_id and l.dismissal in ("caught", "run out"):
                pts[fk] = pts.get(fk, 0) + 8
            if l.out and l.fielder_id and l.dismissal == "stumped":
                pts[fk] = pts.get(fk, 0) + 10
        for b in inn.bowl_lines:
            p = 25 * b.wickets + 8 * b.maidens + 6 * inn.death_wkts.get(b.player.id, 0)
            if b.balls >= 12:
                p += 3 * (8.5 - b.runs * 6 / b.balls) * (b.balls / 24)
            k = (inn.bowling.team_id, b.player.id)
            pts[k] = pts.get(k, 0) + p
    return pts


def _awards(innings: list[InningsResult], winner_team: int) -> tuple[tuple[int, str], tuple[int, str] | None]:
    """Returns ((team_id, player_id) of the Player of the Match, (team_id, player_id) of the best bowler)."""
    pts = impact(innings, winner_team)
    adj = {k: v * (1.0 if k[0] == winner_team else 0.85) for k, v in pts.items()}
    pom = max(adj, key=lambda k: (adj[k], k[1], k[0]))
    best, key = None, None
    for inn in innings:
        for b in inn.bowl_lines:
            k = (b.wickets, -b.runs, b.balls, b.player.id, inn.bowling.team_id)
            if key is None or k > key:
                best, key = (inn.bowling.team_id, b.player.id), k
    return pom, best


# ───────────────────────── persistence payload ─────────────────────────
def to_payload(m: MatchOutcome, *, store_balls: bool = True) -> dict[str, Any]:
    innings = []
    for inn in m.innings:
        batting = [{"player_id": l.player.id, "pos": l.pos, "did_bat": l.did_bat, "runs": l.runs, "balls": l.balls,
                    "fours": l.fours, "sixes": l.sixes, "out": l.out, "dismissal": l.dismissal,
                    "bowler_id": l.bowler_id, "fielder_id": l.fielder_id} for l in inn.bat_lines]
        bowling = [{"player_id": b.player.id, "legal_balls": b.balls, "runs": b.runs, "wickets": b.wickets,
                    "maidens": b.maidens, "wides": b.wides, "noballs": b.noballs, "dots": b.dots} for b in inn.bowl_lines]
        row = {"innings_no": inn.innings_no, "is_super_over": inn.is_super_over, "batting_team_id": inn.batting.team_id,
               "bowling_team_id": inn.bowling.team_id, "runs": inn.runs, "wickets": inn.wickets,
               "legal_balls": inn.legal_balls, "extras": inn.extras, "fall_of_wickets": inn.fow,
               "target": inn.target, "all_out": inn.all_out, "batting": batting, "bowling": bowling}
        if store_balls:
            row["balls"] = inn.balls
        innings.append(row)
    a, b = m.innings[0], m.innings[1]
    nrr = {
        "team1": {"runs_for": a.runs, "balls_faced": a.balls_for_nrr, "runs_against": b.runs, "balls_bowled": b.balls_for_nrr},
        "team2": {"runs_for": b.runs, "balls_faced": b.balls_for_nrr, "runs_against": a.runs, "balls_bowled": a.balls_for_nrr},
    }
    return {"team1_id": m.team1.team_id, "team2_id": m.team2.team_id, "toss_winner_id": m.toss_winner,
            "toss_decision": m.toss_decision, "winner_team_id": m.winner, "result_type": m.result_type,
            "margin": m.margin, "super_overs": m.super_overs,
            "pom_player_id": m.pom[1] if m.pom else None, "pom_team_id": m.pom[0] if m.pom else None,
            "best_bowler_id": m.best_bowler[1] if m.best_bowler else None,
            "best_bowler_team_id": m.best_bowler[0] if m.best_bowler else None,
            "summary": m.summary, "seed": m.seed, "innings": innings, "nrr": nrr}
