"""
Draft pick commentary: a provider-neutral interface plus the deterministic, local provider that ships (FS-031).

The interface takes a PickContext of facts the platform already stores and returns a Commentary. Nothing here calls
a model or the network. A future provider (FS-032 seam) implements the same Protocol; the default stays local.

Facts used, and only these: the round and pick number, the team, the player and his position, how many players of
that position the team and the league have already taken in this draft, and - when stored - the player's rank by
last-season points among the draft pool. Anything missing is named in `unavailable` and the text says so; no
statistic is estimated or invented. Wording is chosen by pick number, so the same pick always reads the same.

Value / reach: with a known rank, the pick is a "value" when it came at least VALUE_MARGIN slots later than the
player's rank, a "reach" when at least VALUE_MARGIN slots earlier, otherwise "fair". VALUE_MARGIN is a product
default, not a fitted number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

VALUE_MARGIN = 10
EVENT_VERSION = "1.0"


@dataclass(frozen=True)
class PickContext:
    pick_number: int
    round: int
    teams: int                       # teams in the draft
    team_name: str
    player_name: str
    position: str | None
    team_position_count: int         # players at this position the team already held before this pick
    league_position_count: int       # players at this position taken league-wide before this pick
    pool_rank: int | None            # 1-based rank by last-season points in the pool, None when no points are stored
    ranked_pool_size: int | None
    is_autopick: bool = False


@dataclass(frozen=True)
class Commentary:
    text: str
    tags: tuple[str, ...]
    value_label: str                 # value / fair / reach / unknown
    unavailable: tuple[str, ...] = ()
    inputs: dict = field(default_factory=dict)


class CommentaryProvider(Protocol):
    """What a commentary source provides. Implementations must be deterministic for a given context unless they
    label themselves otherwise, and must not invent statistics."""

    provider_id: str

    def describe(self, ctx: PickContext) -> Commentary: ...


def value_label(pick_number: int, pool_rank: int | None) -> str:
    if pool_rank is None:
        return "unknown"
    if pick_number - pool_rank >= VALUE_MARGIN:
        return "value"
    if pool_rank - pick_number >= VALUE_MARGIN:
        return "reach"
    return "fair"


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


class DeterministicCommentary:
    provider_id = "deterministic-v1"

    _VALUE = (
        "{team} get a bargain: {player} was ranked {rank} on last season's points and goes at pick {pick}.",
        "Nice value for {team}. {player} ranked {rank} by last season's points and is still there at pick {pick}.",
    )
    _REACH = (
        "That's early for {player}: pick {pick}, but he ranked {rank} on last season's points. {team} must see something.",
        "{team} take {player} at {pick}, ahead of his {rank} ranking on last season's points. A reach by that measure.",
    )
    _FAIR = (
        "{team} take {player} at pick {pick}, right around his {rank} ranking on last season's points.",
        "{player} to {team} at {pick}. That lines up with a {rank} ranking on last season's points.",
    )
    _UNKNOWN = (
        "{team} select {player} at pick {pick}. There are no stored points to rank him against, so no value call.",
        "{player} goes to {team} at {pick}. Without stored points there is nothing to measure this pick against.",
    )

    def describe(self, ctx: PickContext) -> Commentary:
        label = value_label(ctx.pick_number, ctx.pool_rank)
        pool = {"value": self._VALUE, "reach": self._REACH, "fair": self._FAIR, "unknown": self._UNKNOWN}[label]
        base = pool[ctx.pick_number % len(pool)].format(
            team=ctx.team_name, player=ctx.player_name, pick=ctx.pick_number, rank=ctx.pool_rank)
        parts, tags, unavailable = [base], [label], []
        if ctx.pool_rank is None:
            unavailable.append("last-season points rank")
        pos = ctx.position
        if pos:
            if ctx.league_position_count == 0:
                parts.append(f"First {pos} off the board.")
                tags.append("first-at-position")
            else:
                parts.append(f"The {ordinal(ctx.league_position_count + 1)} {pos} taken so far.")
            if ctx.team_position_count == 0:
                parts.append(f"It fills a need: {ctx.team_name} had no {pos} yet.")
                tags.append("fills-need")
            elif ctx.team_position_count >= 2:
                parts.append(f"{ctx.team_name} already hold {ctx.team_position_count} at {pos}, so this adds depth.")
                tags.append("depth")
        else:
            unavailable.append("position")
        if ctx.is_autopick:
            parts.append("The clock ran out, so this was an automatic pick.")
            tags.append("autopick")
        text = " ".join(parts)
        return Commentary(text=text, tags=tuple(tags), value_label=label, unavailable=tuple(unavailable),
                          inputs={"pool_rank": ctx.pool_rank, "ranked_pool_size": ctx.ranked_pool_size,
                                  "team_position_count": ctx.team_position_count,
                                  "league_position_count": ctx.league_position_count})


_provider: CommentaryProvider = DeterministicCommentary()


def get_provider() -> CommentaryProvider:
    return _provider


def set_provider(provider: CommentaryProvider) -> None:
    """Swap the commentary source (tests, and the future provider seam). The default is the local one."""
    global _provider
    _provider = provider
