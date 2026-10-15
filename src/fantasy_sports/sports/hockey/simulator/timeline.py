"""
Turns a normalized scoreboard plus the ledger's contributions into progressive FS-025 events.

What is fixed by the numbers (not by the story): the final goals, taken from normalize.normalize. What the
seeded story stream decides: which goal-sized contributions become scoreboard goals, and the placement of
anything with no real clock. Nothing here changes a player's points: box-score credit is untouched by
normalization.

Story rules:
  * Goals for a side are drawn without replacement from that side's goal-sized items (earned goal credit and
    inverted opponent errors), weighted by value (Efraimidis-Spirakis keys, seeded). If a side needs more
    goals than it has items, the rest are unattributed team goals.
  * A goal-sized item that is not drawn still appears (a shot, or a rush that is stopped) so its credit is
    visible in the ticker. Blocks and saves are texture only and never become goals.
  * Inverted items (opponent goalie errors) are shown as odd-man rushes / power-play chances for the side
    that benefits; the goalie's negative credit stays in the box score.
  * Every event sits strictly inside its FANTASY period. Where a fantasy period starts and ends is the chronological
    division of the matchup (division.py): an earned event is placed by the shared function from the nominal start of
    the real period it happened in, overtime at the end of the fantasy period holding that player's real P3. Events with
    no real clock (inverted opponent errors, unattributed team goals, the tie-break goal) are drawn inside the fantasy
    periods. Boundary events (period_start/period_end/game_end) are fixed.
  * If the rounded scoreboard tied, one deciding goal for the tie-break side is placed after all other goals.
  * Totals only ever climb, which the FS-025 validator enforces again before anything is published.
"""

from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass, field

from fantasy_sports.platform.simulator.contract import SimEventInput
from fantasy_sports.sports.hockey.simulator.division import Division, is_overtime
from fantasy_sports.sports.hockey.simulator.model import (
    GAME_SECONDS, MAX_SHOT_EVENTS_PER_SIDE, MAX_TEXTURE_EVENTS_PER_SIDE, PERIODS, PERIOD_SECONDS, TIEBREAK_WINDOW,
    Contribution, CreditLine, HockeyGameInputs, Ledger, Normalized,
)


TIEBREAK_LAST = 10 ** 9   # sorts the deciding goal after any overtime event that shares its second


@dataclass
class _Raw:
    second: int
    order: int
    kind: str
    side: str | None
    headline: str
    detail: dict = field(default_factory=dict)
    d_score: int = 0
    d_shot: int = 0
    tie: int = 0       # orders events that share a game second: overtime keeps its relative order, the tie-break goal is last


def place(second: int) -> int:
    """Map any second onto a game second strictly inside its period (1..1199 into the period)."""
    base = max(0, min(int(second), GAME_SECONDS - 1))
    period_index = base // PERIOD_SECONDS
    offset = min(max(base % PERIOD_SECONDS, 1), PERIOD_SECONDS - 1)
    return period_index * PERIOD_SECONDS + offset


def weighted_draw(items: list[Contribution], k: int, rng: random.Random) -> list[Contribution]:
    """k items without replacement, probability proportional to value; deterministic for a seeded rng."""
    keyed = []
    for item in items:  # items arrive sorted by item_id, so the rng consumption order is fixed
        u = max(rng.random(), 1e-12)
        keyed.append((math.log(u) / item.value, item.item_id, item))
    keyed.sort(key=lambda t: (-t[0], t[1]))
    return [t[2] for t in keyed[:k]]


def _credit_dict(line: CreditLine) -> dict:
    return {"asset_id": line.asset_id, "name": line.player_name, "role": line.role, "points": round(line.points, 6)}


def _earned_goal(side_name: str, item: Contribution, rng: random.Random) -> tuple[str, dict]:
    shooter = next((l for l in item.lines if l.role == "shooter"), None)
    assists = [l for l in item.lines if l.role in ("primary_assist", "secondary_assist")]
    assists.sort(key=lambda l: (l.role != "primary_assist", l.line_id))
    detail = {"origin": "earned", "credits": [_credit_dict(l) for l in item.lines], "value": round(item.value, 6)}
    lead = shooter.player_name if shooter else None
    if lead:
        text = f"GOAL! {lead} scores for {side_name}"
        if assists:
            text += " (assists: " + ", ".join(a.player_name for a in assists) + ")"
    else:
        text = f"GOAL! {side_name} score, set up by " + ", ".join(a.player_name for a in assists)
    return text, detail


def build_events(inputs: HockeyGameInputs, ledger: Ledger, norm: Normalized, rng: random.Random,
                 division: Division) -> list[SimEventInput]:
    counter = itertools.count()
    raw: list[_Raw] = []
    names = {"home": inputs.home.name, "away": inputs.away.name}
    goals = {"home": norm.home_goals, "away": norm.away_goals}
    goal_times: dict[str, list[int]] = {"home": [], "away": []}

    def add(second, kind, side, headline, detail, d_score=0, d_shot=0, tie=0):
        raw.append(_Raw(place(second), next(counter), kind, side, headline[:200], detail, d_score, d_shot, tie))

    def at(asset_id: int, real_second: int) -> tuple[int, int]:
        """(fantasy game second, tie key) of an event at `real_second` of the real game of `asset_id`."""
        return division.place(asset_id, real_second), (real_second if is_overtime(real_second) else 0)

    def lead_of(item: Contribution) -> CreditLine:
        return next((l for l in item.lines if l.role == "shooter"), item.lines[0])

    for side in ("home", "away"):
        other = "away" if side == "home" else "home"
        items = ledger.items[side]
        base_goals = goals[side] - (1 if norm.tiebreak_side == side else 0)
        goal_items = [i for i in items if i.kind in ("goal_credit", "inverted")]
        chosen = weighted_draw(goal_items, base_goals, rng)
        chosen_ids = {c.item_id for c in chosen}

        for item in chosen:
            if item.kind == "goal_credit":
                text, detail = _earned_goal(names[side], item, rng)
                lead = lead_of(item)
                second, tie = at(lead.asset_id, lead.real_second)
            else:
                src = item.source
                kind = rng.choice(["odd-man rush", "power-play goal"])
                text = f"GOAL! {names[side]} convert an {kind}" if kind == "odd-man rush" \
                    else f"GOAL! {names[side]} score on the power play"
                text += f" after {src.player_name} could not stop the chance"
                detail = {"origin": "inverted", "style": kind, "value": round(item.value, 6),
                          "caused_by": _credit_dict(src)}
                second, tie = division.draw(rng), 0
            detail["goal_for"] = side
            goal_times[side].append(place(second))
            add(second, "score", side, text, detail, d_score=1, d_shot=1, tie=tie)

        base_missing = base_goals - len(chosen)
        for _ in range(base_missing):
            second = division.draw(rng)
            goal_times[side].append(place(second))
            add(second, "score", side, f"GOAL! {names[side]} score",
                {"origin": "team", "credits": [], "goal_for": side}, d_score=1, d_shot=1)

        # Goal-sized items that did not make the scoreboard.
        for item in goal_items:
            if item.item_id in chosen_ids:
                continue
            if item.kind == "goal_credit":
                shooter = lead_of(item)
                second, tie = at(shooter.asset_id, shooter.real_second)
                add(second, "shot", side, f"{shooter.player_name} fires on net for {names[side]}",
                    {"origin": "earned", "credits": [_credit_dict(l) for l in item.lines],
                     "value": round(item.value, 6)}, d_shot=1, tie=tie)
            else:
                add(division.draw(rng), "narrative", side,
                    f"{names[side]} break in on a rush after {item.source.player_name} is beaten",
                    {"origin": "inverted", "value": round(item.value, 6), "caused_by": _credit_dict(item.source)})

        texture = [i for i in items if i.kind in ("block", "save")]
        texture.sort(key=lambda c: (-c.value, c.item_id))
        for item in sorted(texture[:MAX_TEXTURE_EVENTS_PER_SIDE], key=lambda c: c.item_id):
            line = item.lines[0]
            second, tie = at(line.asset_id, line.real_second)
            if item.kind == "block":
                add(second, "narrative", side, f"{line.player_name} blocks a dangerous shot for {names[side]}",
                    {"origin": "earned", "credits": [_credit_dict(line)], "value": round(item.value, 6)}, tie=tie)
            else:
                add(second, "save", side, f"{line.player_name} makes a save for {names[side]}",
                    {"origin": "earned", "credits": [_credit_dict(line)], "value": round(item.value, 6)}, tie=tie)

        attempts = sorted(inputs.side(side).attempts, key=lambda a: (-a.xg, a.attempt_id))[:MAX_SHOT_EVENTS_PER_SIDE]
        for a in sorted(attempts, key=lambda a: a.attempt_id):
            second, tie = at(a.asset_id, a.real_second)
            add(second, "shot", side, f"{a.player_name} shoots for {names[side]}",
                {"origin": "attempt", "xg": round(a.xg, 6), "asset_id": a.asset_id}, d_shot=1, tie=tie)

    if norm.tiebreak_side:
        side = norm.tiebreak_side
        latest = max([t for s in goal_times.values() for t in s], default=0)
        second = min(GAME_SECONDS - 1, max(rng.randint(*TIEBREAK_WINDOW), latest + 1))
        add(second, "score", side, f"GOAL! {names[side]} score the deciding goal",
            {"origin": "team", "credits": [], "goal_for": side, "tiebreak": True}, d_score=1, d_shot=1, tie=TIEBREAK_LAST)

    raw.sort(key=lambda r: (r.second, r.tie, r.order))
    return _assemble(inputs, raw, norm)


def _assemble(inputs: HockeyGameInputs, raw: list[_Raw], norm: Normalized) -> list[SimEventInput]:
    events: list[SimEventInput] = []
    totals = {"home_score": 0, "away_score": 0, "home_shots": 0, "away_shots": 0}

    def emit(kind, period, second, side, headline, detail):
        events.append(SimEventInput(
            seq=len(events) + 1, kind=kind, period=period, game_second=second, team_side=side,
            home_score=totals["home_score"], away_score=totals["away_score"],
            home_shots=totals["home_shots"], away_shots=totals["away_shots"],
            headline=headline, detail=detail))

    boundaries = {p: (p - 1) * PERIOD_SECONDS for p in range(1, PERIODS + 1)}
    index = 0
    for period in range(1, PERIODS + 1):
        emit("period_start", period, boundaries[period], None, f"Period {period} begins", {})
        limit = period * PERIOD_SECONDS
        while index < len(raw) and raw[index].second < limit:
            r = raw[index]
            if r.d_score:
                totals[f"{r.side}_score"] += r.d_score
            if r.d_shot:
                totals[f"{r.side}_shots"] += r.d_shot
            emit(r.kind, period, r.second, r.side, r.headline, r.detail)
            index += 1
        emit("period_end", period, limit, None, f"End of period {period}", {})
    final = f"Final: {inputs.home.name} {totals['home_score']}, {inputs.away.name} {totals['away_score']}"
    emit("game_end", PERIODS, GAME_SECONDS, None, final, {"tiebreak_side": norm.tiebreak_side})
    return events
