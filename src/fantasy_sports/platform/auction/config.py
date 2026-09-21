"""
Configuration objects and round-window math for the Timed Auction Draft.

Everything a league can tune about an auction lives in `AuctionConfig`: how
many per-slot rounds the Recruiting Period is divided into, how long each
round's bidding window is, and which budget tiers exist. Nothing in this
module knows about football - `sports/football/college/auction.py` supplies
the NCAAF values, and another sport supplies its own without touching this
file.

The one platform-wide rule enforced here is the 3-minute round floor: a round
window may never be shorter than `MINIMUM_ROUND_SECONDS`. A sport may raise
its own floor (`AuctionConfig.minimum_round_seconds`), never lower it below
the platform value.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

# Platform-wide floor on a single round's bidding window: 3 minutes.
MINIMUM_ROUND_SECONDS = 180


@dataclass(frozen=True)
class TierConfig:
    """A program-size budget tier (Large/Moderate/Small for college football)."""

    code: str
    display_name: str
    starting_budget: int
    sort_order: int = 0

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("Tier code is required")
        if self.starting_budget <= 0:
            raise ValueError(f"Tier {self.code!r} needs a positive starting budget")


@dataclass(frozen=True)
class RoundWindow:
    """The computed bidding window for one per-slot round."""

    round_number: int
    opens_at: datetime.datetime
    closes_at: datetime.datetime

    @property
    def duration_seconds(self) -> int:
        return int((self.closes_at - self.opens_at).total_seconds())


@dataclass(frozen=True)
class AuctionConfig:
    """Per-sport / per-league auction settings.

    Exactly one of `round_seconds` or `recruiting_period_seconds` is normally
    supplied:

    - `round_seconds` sets the per-round window directly. Below the floor is a
      configuration error, not something to silently correct.
    - `recruiting_period_seconds` sets the *total* period and lets the window
      fall out of it (period / slots). Here the floor clamps instead of
      raising: a commissioner who asks for a 30-minute period across 20 slots
      gets 20 three-minute rounds and a longer period, rather than an error.

    With neither supplied, rounds run at the floor.
    """

    slots: int
    tiers: tuple[TierConfig, ...]
    default_tier_code: str
    round_seconds: int | None = None
    recruiting_period_seconds: int | None = None
    minimum_round_seconds: int = MINIMUM_ROUND_SECONDS
    min_bid: int = 1
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.slots <= 0:
            raise ValueError("An auction needs at least one per-slot round")
        if not self.tiers:
            raise ValueError("An auction needs at least one budget tier")
        if self.minimum_round_seconds < MINIMUM_ROUND_SECONDS:
            raise ValueError(
                f"A sport may raise the round floor but not lower it below "
                f"{MINIMUM_ROUND_SECONDS}s (got {self.minimum_round_seconds}s)"
            )
        codes = [t.code for t in self.tiers]
        if len(set(codes)) != len(codes):
            raise ValueError(f"Duplicate tier codes: {sorted(codes)}")
        budgets = [t.starting_budget for t in self.tiers]
        if len(set(budgets)) != len(budgets):
            # Tiers exist to be *different* sizes; two tiers with the same
            # budget is a config mistake, not a meaningful setup.
            raise ValueError(f"Tiers must have distinct starting budgets: {sorted(budgets)}")
        if self.default_tier_code not in codes:
            raise ValueError(
                f"default_tier_code {self.default_tier_code!r} is not one of {sorted(codes)}"
            )
        if self.round_seconds is not None and self.round_seconds < self.minimum_round_seconds:
            raise ValueError(
                f"Round window {self.round_seconds}s is below this league's "
                f"{self.minimum_round_seconds}s floor"
            )
        if self.min_bid <= 0:
            raise ValueError("min_bid must be positive")

    def tier(self, code: str) -> TierConfig:
        for tier in self.tiers:
            if tier.code == code:
                return tier
        raise KeyError(f"Unknown tier code {code!r}")

    def resolved_round_seconds(self) -> int:
        """The actual per-round bidding window, floor applied."""
        if self.round_seconds is not None:
            return self.round_seconds
        if self.recruiting_period_seconds is not None:
            derived = self.recruiting_period_seconds // self.slots
            return max(derived, self.minimum_round_seconds)
        return self.minimum_round_seconds

    def total_period_seconds(self) -> int:
        """How long the whole Recruiting Period actually runs.

        This is the resolved window times the slot count, which can exceed a
        requested `recruiting_period_seconds` when the floor clamped upward.
        """
        return self.resolved_round_seconds() * self.slots


def plan_round_windows(
    start_at: datetime.datetime,
    slots: int,
    round_seconds: int,
    minimum_round_seconds: int = MINIMUM_ROUND_SECONDS,
) -> list[RoundWindow]:
    """Divide a Recruiting Period starting at `start_at` into back-to-back
    per-slot round windows.

    Round N opens exactly when round N-1 closes, so the windows tile the
    period with no gap and no overlap.
    """
    if slots <= 0:
        raise ValueError("An auction needs at least one per-slot round")
    if round_seconds < minimum_round_seconds:
        raise ValueError(
            f"Round window {round_seconds}s is below the {minimum_round_seconds}s floor"
        )

    window = datetime.timedelta(seconds=round_seconds)
    windows: list[RoundWindow] = []
    opens_at = start_at
    for round_number in range(1, slots + 1):
        closes_at = opens_at + window
        windows.append(
            RoundWindow(round_number=round_number, opens_at=opens_at, closes_at=closes_at)
        )
        opens_at = closes_at
    return windows
