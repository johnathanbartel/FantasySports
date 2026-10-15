"""
Versioned, sport-agnostic contract for progressive ("reveal as it happens") simulated games.

A sport adapter (hockey: FS-026) turns its scoring inputs into a SimScript: an ordered, immutable list of
events that together tell one finished game across `periods` periods of `period_seconds` seconds. The
platform stores the script once and reveals it over a real-time window (live viewers) or on request
(replay / skip to box score). Nothing in this package knows how a goal is valued: scores arrive on the
events as running totals, and the validator only enforces that the story is consistent and never goes
backwards.

Version: CONTRACT_VERSION is "major.minor". A script is accepted when its major matches SUPPORTED_MAJOR
(minor bumps only add optional fields). Every snapshot and event the API returns carries the version.

Event totals (`home_score`, `away_score`, `home_shots`, `away_shots`) are the running totals AFTER the
event. Validation rules (all enforced by validate_script):
  * seq runs 1..N with no gaps; game_second is non-decreasing; period is non-decreasing and
    (period-1)*period_seconds <= game_second <= period*period_seconds;
  * every total is non-negative and never decreases from one event to the next;
  * a score changes only on a `score` event, only for that event's `team_side`, and only upward; shots change
    only on `shot`, `score` or `save` events and only for that event's `team_side`;
  * exactly one `game_end` event, it is last, it sits at game_second == periods*period_seconds, and it leaves
    the totals unchanged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

CONTRACT_VERSION = "1.0"
SUPPORTED_MAJOR = 1

MAX_PERIODS = 9
MAX_PERIOD_SECONDS = 1200  # series_daily_games.seconds_remaining is capped at 1200
MAX_EVENTS = 2000
MAX_HEADLINE = 200

EVENT_KINDS = ("period_start", "period_end", "shot", "save", "score", "penalty", "narrative", "game_end")
SHOT_KINDS = ("shot", "save", "score")
TEAM_SIDES = ("home", "away")


class ContractError(ValueError):
    """The script (or a request built from it) breaks the contract."""


@dataclass(frozen=True)
class SimEventInput:
    seq: int
    kind: str
    period: int
    game_second: int  # seconds elapsed since the opening faceoff, across all periods
    team_side: str | None  # the team the event is about / credited to: "home", "away" or None
    home_score: int
    away_score: int
    home_shots: int
    away_shots: int
    headline: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "seq": self.seq, "kind": self.kind, "period": self.period, "game_second": self.game_second,
            "team_side": self.team_side, "home_score": self.home_score, "away_score": self.away_score,
            "home_shots": self.home_shots, "away_shots": self.away_shots, "headline": self.headline,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SimEventInput":
        try:
            return cls(
                seq=int(data["seq"]), kind=str(data["kind"]), period=int(data["period"]),
                game_second=int(data["game_second"]), team_side=data.get("team_side"),
                home_score=int(data["home_score"]), away_score=int(data["away_score"]),
                home_shots=int(data["home_shots"]), away_shots=int(data["away_shots"]),
                headline=str(data["headline"]), detail=dict(data.get("detail") or {}),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ContractError(f"Malformed event: {e!r}")


@dataclass(frozen=True)
class SimScript:
    contract_version: str
    sport: str
    periods: int
    period_seconds: int
    window_seconds: int  # real seconds over which a live viewer sees the game play out
    events: tuple[SimEventInput, ...]
    provenance: Mapping[str, Any] = field(default_factory=dict)  # who built it and from what (adapter-defined)

    @property
    def total_seconds(self) -> int:
        return self.periods * self.period_seconds

    def to_dict(self) -> dict:
        return {
            "contract_version": self.contract_version, "sport": self.sport, "periods": self.periods,
            "period_seconds": self.period_seconds, "window_seconds": self.window_seconds,
            "events": [e.to_dict() for e in self.events], "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SimScript":
        try:
            return cls(
                contract_version=str(data["contract_version"]), sport=str(data["sport"]),
                periods=int(data["periods"]), period_seconds=int(data["period_seconds"]),
                window_seconds=int(data["window_seconds"]),
                events=tuple(SimEventInput.from_dict(e) for e in data["events"]),
                provenance=dict(data.get("provenance") or {}),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ContractError(f"Malformed script: {e!r}")

    def digest(self) -> str:
        """Stable fingerprint of the full script, used to make publishing idempotent."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SportSimulatorAdapter(Protocol):
    """What a sport package provides. The platform never calls it while serving a request: scripts are
    built ahead of time and published with service.publish_script."""

    sport: str

    def build_script(self, game_inputs: Mapping[str, Any], seed: int) -> SimScript: ...


def version_supported(version: str) -> bool:
    try:
        major, minor = version.split(".")
        int(minor)
        return int(major) == SUPPORTED_MAJOR
    except ValueError:
        return False


@dataclass(frozen=True)
class _Totals:
    game_second: int = 0
    period: int = 1
    home_score: int = 0
    away_score: int = 0
    home_shots: int = 0
    away_shots: int = 0


def validate_script(script: SimScript, partial: bool = False) -> None:
    """Raises ContractError describing the first violation. With partial=True the events are a PREFIX of a game that
    is still being published period by period: every event rule still applies, but no game_end is required (and none
    may exist yet)."""
    if not version_supported(script.contract_version):
        raise ContractError(
            f"Unsupported contract version {script.contract_version!r} (this server speaks {CONTRACT_VERSION})")
    if not script.sport:
        raise ContractError("sport is required")
    if not 1 <= script.periods <= MAX_PERIODS:
        raise ContractError(f"periods must be 1-{MAX_PERIODS}")
    if not 1 <= script.period_seconds <= MAX_PERIOD_SECONDS:
        raise ContractError(f"period_seconds must be 1-{MAX_PERIOD_SECONDS}")
    if script.window_seconds < 1:
        raise ContractError("window_seconds must be at least 1")
    events = script.events
    if not events:
        raise ContractError("A script needs at least one event")
    if len(events) > MAX_EVENTS:
        raise ContractError(f"A script may hold at most {MAX_EVENTS} events")

    before: SimEventInput | _Totals = _Totals()
    for i, e in enumerate(events, start=1):
        where = f"event {i}"
        if e.seq != i:
            raise ContractError(f"{where}: seq must be {i} (contiguous from 1), got {e.seq}")
        if e.kind not in EVENT_KINDS:
            raise ContractError(f"{where}: unknown kind {e.kind!r}")
        if e.team_side is not None and e.team_side not in TEAM_SIDES:
            raise ContractError(f"{where}: team_side must be 'home', 'away' or null")
        if not e.headline or len(e.headline) > MAX_HEADLINE:
            raise ContractError(f"{where}: headline must be 1-{MAX_HEADLINE} characters")
        if not 1 <= e.period <= script.periods:
            raise ContractError(f"{where}: period {e.period} is outside 1-{script.periods}")
        lo, hi = (e.period - 1) * script.period_seconds, e.period * script.period_seconds
        if not lo <= e.game_second <= hi:
            raise ContractError(f"{where}: game_second {e.game_second} is outside period {e.period} ({lo}-{hi})")
        if min(e.home_score, e.away_score, e.home_shots, e.away_shots) < 0:
            raise ContractError(f"{where}: totals cannot be negative")
        if e.game_second < before.game_second:
            raise ContractError(f"{where}: game_second runs backwards ({before.game_second} -> {e.game_second})")
        if e.period < before.period:
            raise ContractError(f"{where}: period runs backwards")
        d_home, d_away = e.home_score - before.home_score, e.away_score - before.away_score
        s_home, s_away = e.home_shots - before.home_shots, e.away_shots - before.away_shots
        if min(d_home, d_away) < 0:
            raise ContractError(f"{where}: a displayed score decreased")
        if min(s_home, s_away) < 0:
            raise ContractError(f"{where}: a shot total decreased")
        if (d_home or d_away) and e.kind != "score":
            raise ContractError(f"{where}: only a 'score' event may change the score")
        if e.kind == "score":
            if e.team_side is None:
                raise ContractError(f"{where}: a 'score' event needs a team_side")
            if d_home + d_away < 1:
                raise ContractError(f"{where}: a 'score' event must raise the score")
            if (d_away if e.team_side == "home" else d_home):
                raise ContractError(f"{where}: a 'score' event may only change its own team_side's score")
        if s_home or s_away:
            if e.kind not in SHOT_KINDS:
                raise ContractError(f"{where}: only shot/save/score events may change shot totals")
            if e.team_side is None:
                raise ContractError(f"{where}: a shot-changing event needs a team_side")
            if (s_away if e.team_side == "home" else s_home):
                raise ContractError(f"{where}: shots may only change for the event's own team_side")
        before = e

    if partial:
        if any(e.kind == "game_end" for e in events):
            raise ContractError("A game that is still being published cannot hold a game_end event")
        return
    if sum(1 for e in events if e.kind == "game_end") != 1 or events[-1].kind != "game_end":
        raise ContractError("Exactly one game_end event is required, and it must be last")
    end = events[-1]
    if end.game_second != script.total_seconds:
        raise ContractError(f"game_end must sit at game_second {script.total_seconds}")
    if len(events) > 1:
        prior = events[-2]
        if (end.home_score, end.away_score, end.home_shots, end.away_shots) != (
                prior.home_score, prior.away_score, prior.home_shots, prior.away_shots):
            raise ContractError("game_end must leave the totals unchanged")
