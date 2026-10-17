"""The standings tiebreakers a commissioner can choose, and the order they are applied in.

Teams are ranked on the league's primary number first (points or win percentage). Teams still level are separated by the
league's tiebreakers, in the order the commissioner set, each one only looking at the teams still tied after the one before.
leagues.tiebreakers stores that order; NULL means DEFAULT_ORDER, which is the order the standings always used.

The keys are sport-neutral: "goal_differential" is points for minus points against in a sport that scores points. Each key
has a kind that tells platform/elimination_engine.py how far it can be proven ahead of time:
  count   - settled by wins and losses alone (wins, head_to_head, division_record)
  margin  - depends on final scores, so it stays open while either team still has a game to play
  draw    - a random draw, only settled once every game in the league has been played
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

COUNT, MARGIN, DRAW = "count", "margin", "draw"


@dataclass(frozen=True)
class Tiebreaker:
    key: str
    label: str
    kind: str
    description: str


TIEBREAKERS: dict[str, Tiebreaker] = {t.key: t for t in (
    Tiebreaker("head_to_head", "Head-to-head", COUNT,
               "Wins against the other teams still tied (a two-team tie: the record between them)."),
    Tiebreaker("division_record", "Division record", COUNT,
               "Win percentage in games against teams in the same division. Used only when every tied team shares a division."),
    Tiebreaker("wins", "Most wins", COUNT, "The team with more wins."),
    Tiebreaker("goal_differential", "Goal differential", MARGIN, "Goals (points) scored minus goals (points) allowed."),
    Tiebreaker("goals_for", "Goals scored", MARGIN, "The team that scored more."),
    Tiebreaker("goals_against", "Goals allowed", MARGIN, "The team that allowed fewer."),
    Tiebreaker("coin_flip", "Random draw", DRAW, "A draw fixed for the league and season. It decides everything, so it goes last."),
)}

# The order the standings used before the commissioner could choose.
DEFAULT_ORDER: tuple[str, ...] = ("goal_differential", "head_to_head", "goals_for", "goals_against")


def validate(order: list[str]) -> str | None:
    """None when the list is a usable tiebreaker order, otherwise the reason it is not."""
    if len(order) > len(TIEBREAKERS):
        return "Too many tiebreakers"
    seen: set[str] = set()
    for key in order:
        if key not in TIEBREAKERS:
            return f"Unknown tiebreaker '{key}'"
        if key in seen:
            return f"Tiebreaker '{key}' is listed twice"
        seen.add(key)
    if "coin_flip" in order and order[-1] != "coin_flip":
        return "The random draw decides every tie, so it has to be the last tiebreaker"
    return None


def order_for(league) -> tuple[str, ...]:
    """The league's tiebreaker order (DEFAULT_ORDER until the commissioner sets one)."""
    stored = getattr(league, "tiebreakers", None)
    if stored is None:
        return DEFAULT_ORDER
    return tuple(stored)


def describe(order: tuple[str, ...] | list[str]) -> str:
    return ", ".join(TIEBREAKERS[k].label.lower() for k in order) or "none (team id)"


def draw_value(seed: str, team_id: int) -> int:
    """The team's number in the league's random draw: bigger wins. Fixed by the seed, so the same on every read."""
    return zlib.crc32(f"{seed}:{team_id}".encode())
