"""
Inputs, constants and validation for the hockey simulator adapter (FS-026).

Every number that shapes a scoreboard is declared here, once, with its meaning. They are PRODUCT DEFAULTS
chosen to produce NHL-shaped scoreboards, not values fitted to data; docs/hockey_simulator_scoring.md lists
each one so the owner can approve or change it. Changing any constant changes the output of every seed, so
the adapter version below must be bumped with it.

Attribution (who earns what) is NOT defined here: credit lines are produced by sports/hockey/pipeline.py
(goal = 1.0 split shooter/assists with the assist share 2:1 primary:secondary; blocked shots credit the
prevented xG; goalie credit is GSAx = pre-shot xG - goal, empty-net shots excluded) and loaded from the
stored shot_credits / event_credits rows. This package only reads those credits.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

ADAPTER_ID = "hockey-xg"
ADAPTER_VERSION = "1.0"

# --- Scoreboard shape (PROVISIONAL product defaults; see docs/hockey_simulator_scoring.md) -----------------
TOTAL_GOALS = 6.0       # expected combined goals before noise: two teams x the ~3.0 NHL per-team average
PRIOR_POINTS = 1.0      # each side gets this many goal-equivalents added before taking shares (regresses blowouts)
NOISE_SIGMA = 0.5       # std dev of the Gaussian error term on the expected goal margin, in goals
NOISE_CAP = 1.0         # hard bound on that error term (goals): an upset needs |expected margin| < NOISE_CAP

PERIODS = 3
PERIOD_SECONDS = 1200
GAME_SECONDS = PERIODS * PERIOD_SECONDS
DEFAULT_WINDOW_SECONDS = 3 * 3600  # real time a live viewer takes to see the game (3 hours)
MAX_TEXTURE_EVENTS_PER_SIDE = 40   # saves / blocks / chances shown per side, highest value first
MAX_SHOT_EVENTS_PER_SIDE = 60
TIEBREAK_WINDOW = (3300, 3590)     # game seconds in which a tied scoreboard's deciding goal lands

ROLES = ("shooter", "primary_assist", "secondary_assist", "goalie", "blocker")
SKATER_GOAL_ROLES = ("shooter", "primary_assist", "secondary_assist")
EPS = 1e-9


class InputError(ValueError):
    """The credit inputs are unusable (not a scoring defect: nothing has been scored yet)."""


class TieOutError(AssertionError):
    """A value that must tie out does not. Stop and report: never patch around it."""


@dataclass(frozen=True)
class CreditLine:
    """One credited contribution, as stored by the pipeline. `points` is signed; only goalie lines (GSAx on
    a goal against) can be negative. `group_id` ties together the lines of one real goal or block."""
    line_id: str
    group_id: str
    asset_id: int
    player_name: str
    position: str            # C / W / D / G
    role: str                # one of ROLES
    points: float
    real_second: int = 0     # seconds into the real NHL game; only used to place the story in time
    xg: float | None = None  # pre-shot xG of the underlying shot, when there is one
    is_goal: bool = False    # the underlying shot was a goal
    empty_net: bool = False  # the underlying shot was into an empty net (never creditable to a goalie)


@dataclass(frozen=True)
class ShotAttempt:
    """A non-goal shot on goal by a lineup skater. Used for the shot counter and the ticker only."""
    attempt_id: str
    asset_id: int
    player_name: str
    real_second: int = 0
    xg: float = 0.0


@dataclass(frozen=True)
class SideInput:
    team_id: int
    name: str
    lines: tuple[CreditLine, ...] = ()
    attempts: tuple[ShotAttempt, ...] = ()


@dataclass(frozen=True)
class HockeyGameInputs:
    daily_game_id: int
    calendar_date: str
    home: SideInput
    away: SideInput

    def side(self, key: str) -> SideInput:
        return self.home if key == "home" else self.away


def validate_inputs(inputs: HockeyGameInputs) -> None:
    seen: set[str] = set()
    owner: dict[int, str] = {}
    for key in ("home", "away"):
        for line in inputs.side(key).lines:
            if line.role not in ROLES:
                raise InputError(f"{line.line_id}: unknown role {line.role!r}")
            if not math.isfinite(line.points):
                raise InputError(f"{line.line_id}: points must be finite")
            if line.points < -EPS and line.role != "goalie":
                raise InputError(f"{line.line_id}: only goalie credit can be negative")
            if line.role == "goalie" and line.empty_net:
                raise InputError(f"{line.line_id}: empty-net shots must be excluded from goalie credit")
            if line.line_id in seen:
                raise InputError(f"{line.line_id}: duplicate line id")
            seen.add(line.line_id)
            if owner.setdefault(line.asset_id, key) != key:
                raise InputError(f"asset {line.asset_id} appears on both sides")
        for a in inputs.side(key).attempts:
            if not math.isfinite(a.xg) or a.xg < 0:
                raise InputError(f"{a.attempt_id}: attempt xg must be a non-negative number")


@dataclass
class Contribution:
    """A unit of value on one side's ledger: either earned by that side's players or inverted from the
    opponent's negative credit. `value` is always positive."""
    item_id: str
    side: str
    kind: str                # goal_credit / block / save / inverted
    value: float
    real_second: int
    lines: tuple[CreditLine, ...] = ()
    source: CreditLine | None = None   # the negative line an inverted item comes from


@dataclass
class Ledger:
    """The pre-normalization accounting for a game (see attribution.build_ledger)."""
    raw: dict[str, float]                  # signed team totals S (own credits, negatives included)
    positive: dict[str, float]             # P: sum of positive lines
    negative: dict[str, float]             # N: sum of |negative lines|
    offense: dict[str, float]              # O = P + opponent's N
    items: dict[str, list[Contribution]] = field(default_factory=dict)

    @property
    def raw_margin(self) -> float:
        return self.raw["home"] - self.raw["away"]

    @property
    def offense_margin(self) -> float:
        return self.offense["home"] - self.offense["away"]


@dataclass(frozen=True)
class Normalized:
    home_goals: int
    away_goals: int
    expected_margin: float      # d: expected goals margin before noise
    noise: float                # epsilon actually applied
    noisy_margin: float         # d' = d + epsilon
    lambda_home: float
    lambda_away: float
    tiebreak_side: str | None   # side given the deciding goal when the rounded scoreboard tied
    upset: bool                 # the scoreboard winner differs from the raw leader
    sigma: float
    cap: float

    def to_dict(self) -> Mapping[str, object]:
        return {
            "home_goals": self.home_goals, "away_goals": self.away_goals,
            "expected_margin": round(self.expected_margin, 6), "noise": round(self.noise, 6),
            "noisy_margin": round(self.noisy_margin, 6), "lambda_home": round(self.lambda_home, 6),
            "lambda_away": round(self.lambda_away, 6), "tiebreak_side": self.tiebreak_side,
            "upset": self.upset, "sigma": self.sigma, "cap": self.cap,
        }
