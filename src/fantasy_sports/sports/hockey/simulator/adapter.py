"""
The hockey SportSimulatorAdapter (platform/simulator/contract.py): credit lines in, a validated progressive
FS-025 script out. Pure and offline: no database, no clock, no network. The same (inputs, seed, window) always
yields a byte-identical script, which is what makes publishing idempotent and replays reproducible.

Pipeline: validate inputs -> ledger (raw totals, inversion) -> tie-outs -> normalize (seeded bounded error)
-> timeline (seeded story) -> contract validation -> post-build tie-outs.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from typing import Iterable, Mapping

from fantasy_sports.platform.simulator.contract import CONTRACT_VERSION, SimScript, validate_script
from fantasy_sports.sports.hockey.simulator import model, windows
from fantasy_sports.sports.hockey.simulator.attribution import box_score, build_ledger, tie_out_ledger
from fantasy_sports.sports.hockey.simulator.model import (
    ADAPTER_ID, ADAPTER_VERSION, DEFAULT_WINDOW_SECONDS, GAME_SECONDS, PERIOD_SECONDS, PERIODS, HockeyGameInputs,
    Ledger, Normalized, TieOutError, validate_inputs,
)
from fantasy_sports.sports.hockey.simulator.normalize import check_ordering, derive_rng, normalize
from fantasy_sports.sports.hockey.simulator.timeline import build_events


def inputs_digest(inputs: HockeyGameInputs) -> str:
    payload = []
    for side in ("home", "away"):
        s = inputs.side(side)
        payload.append({
            "team_id": s.team_id, "name": s.name,
            "lines": [[l.line_id, l.group_id, l.asset_id, l.role, round(l.points, 9), l.real_second]
                      + ([l.game_id] if l.game_id is not None else []) for l in sorted(s.lines, key=lambda l: l.line_id)],
            "attempts": [[a.attempt_id, a.asset_id, round(a.xg, 9), a.real_second]
                         + ([a.game_id] if a.game_id is not None else []) for a in
                         sorted(s.attempts, key=lambda a: a.attempt_id)],
        })
    if inputs.window_index is not None:      # only window games: a plain game's digest is unchanged
        payload.append({"window": inputs.window_index, "week_start": inputs.week_start})
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def tie_out_script(ledger: Ledger, norm: Normalized, script: SimScript) -> None:
    """Post-build identities: the story matches the numbers it was built from."""
    last = script.events[-1]
    if (last.home_score, last.away_score) != (norm.home_goals, norm.away_goals):
        raise TieOutError("final scoreboard differs from the normalized goals")
    if last.home_shots < last.home_score or last.away_shots < last.away_score:
        raise TieOutError("a side has fewer shots than goals")
    score_events = [e for e in script.events if e.kind == "score"]
    for side, goals in (("home", norm.home_goals), ("away", norm.away_goals)):
        if sum(1 for e in score_events if e.team_side == side) != goals:
            raise TieOutError(f"{side}: goal events do not add up to {goals}")
    check_ordering(norm)
    if abs(ledger.offense_margin - ledger.raw_margin) > 1e-8:
        raise TieOutError("net margin changed")


class HockeySimulatorAdapter:
    sport = "hockey"

    def __init__(self, window_seconds: int = DEFAULT_WINDOW_SECONDS):
        self.window_seconds = window_seconds

    def build_window_script(self, game_inputs: HockeyGameInputs, game_dates: Mapping[int, datetime.date], seed: int,
                            assignments=(), scheduled_games: Mapping[int, Iterable[int]] | None = None) -> SimScript:
        """A window game from ALL of the week's credit for the starters: each player's one counting game is chosen
        (Best Game, or the manager's surplus-game moves in `assignments`: pipeline.WindowAssignment), only that game's
        credit is kept, and the usual script is built from it. `game_dates` maps real game id -> calendar date."""
        resolved, report = windows.resolve_window_inputs(game_inputs, game_dates, assignments, scheduled_games)
        return self.build_script(resolved, seed, extra_provenance={"window": report})

    def build_script(self, game_inputs: Mapping | HockeyGameInputs, seed: int,
                     extra_provenance: Mapping | None = None) -> SimScript:
        if not isinstance(game_inputs, HockeyGameInputs):
            raise model.InputError("game_inputs must be a HockeyGameInputs")
        inputs = game_inputs
        validate_inputs(inputs)
        ledger = build_ledger(inputs)
        tie_out_ledger(inputs, ledger)
        norm = normalize(ledger, derive_rng(seed, inputs.daily_game_id, "noise"))
        events = build_events(inputs, ledger, norm, derive_rng(seed, inputs.daily_game_id, "story"))
        provenance = {
            "adapter": ADAPTER_ID, "adapter_version": ADAPTER_VERSION, "seed": seed,
            "inputs_digest": inputs_digest(inputs),
            "constants": {
                "total_goals": model.TOTAL_GOALS, "prior_points": model.PRIOR_POINTS,
                "noise_sigma": model.NOISE_SIGMA, "noise_cap": model.NOISE_CAP,
            },
            "raw": {"home": round(ledger.raw["home"], 6), "away": round(ledger.raw["away"], 6),
                    "margin": round(ledger.raw_margin, 6)},
            "offense": {"home": round(ledger.offense["home"], 6), "away": round(ledger.offense["away"], 6)},
            "inverted_value": {"home": round(ledger.negative["away"], 6), "away": round(ledger.negative["home"], 6)},
            "normalization": dict(norm.to_dict()),
            "teams": {"home": {"team_id": inputs.home.team_id, "name": inputs.home.name},
                      "away": {"team_id": inputs.away.team_id, "name": inputs.away.name}},
            "box_score": box_score(inputs),
        }
        if extra_provenance:
            provenance.update(extra_provenance)
        script = SimScript(
            contract_version=CONTRACT_VERSION, sport=self.sport, periods=PERIODS, period_seconds=PERIOD_SECONDS,
            window_seconds=self.window_seconds, events=tuple(events), provenance=provenance)
        validate_script(script)
        tie_out_script(ledger, norm, script)
        return script
