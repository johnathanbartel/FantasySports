"""
Pre-normalization accounting: player box score, team totals, and the inversion of negative credit.

Definitions (S = signed team total, P = sum of the team's positive lines, N = sum of |negative lines|):

    S_side      = P_side - N_side                      the team's own net credit (what the box score adds up to)
    O_side      = P_side + N_other                     "offense": own positive credit plus the opponent's
                                                       negative credit, inverted into opponent-positive events
    O_home - O_away = S_home - S_away                  inversion never changes the net margin

The scoreboard is built from O (all non-negative, so the story only climbs); the box score keeps the signed
lines. Nothing here is random.
"""

from __future__ import annotations

from collections import defaultdict

from fantasy_sports.sports.hockey.simulator.model import (
    EPS, Contribution, CreditLine, HockeyGameInputs, Ledger, SKATER_GOAL_ROLES, TieOutError,
)


def build_ledger(inputs: HockeyGameInputs) -> Ledger:
    raw = {"home": 0.0, "away": 0.0}
    positive = {"home": 0.0, "away": 0.0}
    negative = {"home": 0.0, "away": 0.0}
    items: dict[str, list[Contribution]] = {"home": [], "away": []}
    other = {"home": "away", "away": "home"}

    for side in ("home", "away"):
        groups: dict[str, list[CreditLine]] = defaultdict(list)
        for line in inputs.side(side).lines:
            raw[side] += line.points
            if line.points > EPS:
                positive[side] += line.points
            elif line.points < -EPS:
                negative[side] += -line.points
            if line.role in SKATER_GOAL_ROLES:
                groups[line.group_id].append(line)
            elif line.role == "blocker" and line.points > EPS:
                items[side].append(Contribution(line.line_id, side, "block", line.points, line.real_second, (line,)))
            elif line.role == "goalie":
                if line.points > EPS:
                    items[side].append(Contribution(line.line_id, side, "save", line.points, line.real_second, (line,)))
                elif line.points < -EPS:
                    items[other[side]].append(Contribution(
                        f"inv:{line.line_id}", other[side], "inverted", -line.points, line.real_second, (), line))
        for group_id, lines in sorted(groups.items()):
            value = sum(l.points for l in lines)
            if value > EPS:
                lines = sorted(lines, key=lambda l: l.line_id)
                items[side].append(Contribution(f"goal:{group_id}", side, "goal_credit", value,
                                                min(l.real_second for l in lines), tuple(lines)))
    offense = {s: positive[s] + negative[other[s]] for s in ("home", "away")}
    for side in items:
        items[side].sort(key=lambda c: c.item_id)
    return Ledger(raw=raw, positive=positive, negative=negative, offense=offense, items=items)


def box_score(inputs: HockeyGameInputs) -> dict[str, list[dict]]:
    """Per-player signed totals by role, best first. This never depends on the normalized scoreboard."""
    out: dict[str, list[dict]] = {}
    for side in ("home", "away"):
        players: dict[int, dict] = {}
        for line in inputs.side(side).lines:
            p = players.setdefault(line.asset_id, {
                "asset_id": line.asset_id, "name": line.player_name, "position": line.position,
                "points": 0.0, "by_role": defaultdict(float)})
            p["points"] += line.points
            p["by_role"][line.role] += line.points
        rows = []
        for p in players.values():
            rows.append({**p, "points": round(p["points"], 6),
                         "by_role": {r: round(v, 6) for r, v in sorted(p["by_role"].items())}})
        rows.sort(key=lambda r: (-r["points"], r["asset_id"]))
        out[side] = rows
    return out


def tie_out_ledger(inputs: HockeyGameInputs, ledger: Ledger, tol: float = 1e-9) -> None:
    """Raises TieOutError unless every pre-normalization identity holds."""
    scores = box_score(inputs)
    for side in ("home", "away"):
        by_player = sum(p["points"] for p in scores[side])
        if abs(by_player - ledger.raw[side]) > 1e-5:  # box score rows are rounded to 6 decimals
            raise TieOutError(f"{side}: player points {by_player} != team total {ledger.raw[side]}")
        by_role = sum(sum(p["by_role"].values()) for p in scores[side])
        if abs(by_role - ledger.raw[side]) > 1e-5:
            raise TieOutError(f"{side}: role totals {by_role} != team total {ledger.raw[side]}")
        if abs(ledger.positive[side] - ledger.negative[side] - ledger.raw[side]) > tol:
            raise TieOutError(f"{side}: P - N != S")
        value = sum(c.value for c in ledger.items[side])
        goalie_noise = 0.0  # zero lines carry no value and are not items
        if abs(value + goalie_noise - ledger.offense[side]) > 1e-8:
            raise TieOutError(f"{side}: contribution items {value} != offense {ledger.offense[side]}")
    if abs(ledger.offense_margin - ledger.raw_margin) > 1e-8:
        raise TieOutError("inversion changed the net margin")
    total_in = sum(ledger.positive.values()) + sum(ledger.negative.values())
    total_out = sum(ledger.offense.values())
    if abs(total_in - total_out) > 1e-8:
        raise TieOutError("inversion did not conserve total value")
