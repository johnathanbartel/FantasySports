"""
Period-by-period publishing of a hockey window game (docs/hockey_spec.md section 6.5).

A fantasy period can be published as soon as its real periods have ended (division.py says which they are). The
scoreboard is then normalized from the credit EARNED SO FAR, with a ratchet so it only ever climbs:

  After period k of P (f = k / P) the credit through period k is normalized exactly as a whole game is
  (normalize.py), with every scale taken at fraction f: expected total goals T x f, the regression prior p x f, the
  error term's sigma and cap x f (the same seeded draw is used at every k, only scaled). The goals shown are then
  max(goals already published, normalized goals): a side never loses a goal.

  After the last period f = 1, so the result is the whole-game normalization of all the credit, unless an early
  overshoot forced a ratchet. Two guards keep the guarantees of section 6.2 in that case: a level final still gets one
  deciding goal for the side with the larger margin, and a raw leader whose expected margin is outside the upset
  window is never left trailing (the leader is given the goals it needs, which only ever adds goals).

Everything here is pure and seeded: the same (credit, seed, game) gives the same period, and a period built twice is
identical, so a retried publish can never differ from the first.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from fantasy_sports.platform.simulator.contract import SimEventInput
from fantasy_sports.sports.hockey.simulator.attribution import build_ledger, build_value_ledger, tie_out_ledger, tie_out_value_ledger
from fantasy_sports.sports.hockey.simulator.division import Division
from fantasy_sports.sports.hockey.simulator.model import (
    EPS, NOISE_CAP, NOISE_SIGMA, PERIODS, PRIOR_POINTS, TOTAL_GOALS, VALUE_NOISE_CAP, VALUE_NOISE_SIGMA, VALUE_SCALE, HockeyGameInputs,
    InputError, Ledger, validate_inputs,
)
from fantasy_sports.sports.hockey.simulator.normalize import derive_rng, truncated_normal, _round_half_up
from fantasy_sports.sports.hockey.simulator.timeline import build_period_events


@dataclass(frozen=True)
class PeriodNorm:
    period: int
    home_goals: int              # cumulative goals shown after this period
    away_goals: int
    add_home: int                # goals scored in this period
    add_away: int
    expected_margin: float       # d at fraction f
    noise: float                 # the error term applied at this fraction
    tiebreak_side: str | None    # last period only: side given the deciding goal when the final was level
    ratcheted: bool              # the whole-credit result was below what was already published (it was raised)
    forced_leader: bool          # last period only: goals were added so the raw leader is not left trailing
    lambda_home: float = 0.0     # value model: expected goals so far (VALUE_SCALE x net value)
    lambda_away: float = 0.0

    def to_dict(self) -> dict:
        return {**dataclasses.asdict(self), "expected_margin": round(self.expected_margin, 6), "noise": round(self.noise, 6)}


def noise_unit(seed: int, daily_game_id: int) -> float:
    """The seeded error draw, before scaling by the fraction played. Identical for every period of a game."""
    return truncated_normal(derive_rng(seed, daily_game_id, "noise"), NOISE_SIGMA, NOISE_CAP)


def noise_units(seed: int, daily_game_id: int) -> tuple[float, float]:
    """The value model's seeded error draws, one per side, before scaling by the fraction played."""
    rng = derive_rng(seed, daily_game_id, "noise-value")
    return truncated_normal(rng, VALUE_NOISE_SIGMA, VALUE_NOISE_CAP), truncated_normal(rng, VALUE_NOISE_SIGMA, VALUE_NOISE_CAP)


def normalize_value_through(ledger: Ledger, eps_units: tuple[float, float], period: int, prev_home: int, prev_away: int,
                            periods: int = PERIODS) -> PeriodNorm:
    """Value model: goals so far from the net value created so far. Nothing fixes the total.

      expected goals  lambda = VALUE_SCALE x V (V from attribution.build_value_ledger, cumulative through `period`: value already
                      accrues as the periods are played, so there is no fraction-of-game scaling)
      error           each side's seeded bounded draw, scaled by the fraction of the game played (it grows into the final)
      goals           round_half_up(lambda + error), never below zero, never below what is already published (ratchet)
      last period     a level game gets one deciding goal for the side ahead on lambda + error (level on that too: the raw leader);
                      a raw leader whose expected margin is at least twice the error bound is never left trailing.
    """
    f = period / periods
    lam_h, lam_a = VALUE_SCALE * ledger.offense["home"], VALUE_SCALE * ledger.offense["away"]
    e_h, e_a = eps_units[0] * f, eps_units[1] * f
    whole = (max(0, _round_half_up(lam_h + e_h)), max(0, _round_half_up(lam_a + e_a)))
    g_h, g_a = max(whole[0], prev_home), max(whole[1], prev_away)
    ratcheted = (g_h, g_a) != whole
    d = lam_h - lam_a

    tiebreak = None
    forced = False
    if period == periods:
        if g_h == g_a:
            lead = (lam_h + e_h) - (lam_a + e_a)
            lead = lead if abs(lead) > EPS else d
            if abs(lead) > EPS:
                tiebreak = "home" if lead > 0 else "away"
                if tiebreak == "home":
                    g_h += 1
                else:
                    g_a += 1
        leader = "home" if d > EPS else "away" if d < -EPS else None
        if leader and abs(d) >= 2.0 * VALUE_NOISE_CAP - EPS:
            while (g_h <= g_a) if leader == "home" else (g_a <= g_h):
                if leader == "home":
                    g_h += 1
                else:
                    g_a += 1
                forced = True
    return PeriodNorm(period, g_h, g_a, g_h - prev_home, g_a - prev_away, d, e_h - e_a, tiebreak, ratcheted, forced, lam_h, lam_a)


def normalize_through(ledger: Ledger, eps_unit: float, period: int, prev_home: int, prev_away: int,
                      periods: int = PERIODS) -> PeriodNorm:
    """Cumulative normalization after `period`, ratcheted against the goals already published."""
    f = period / periods
    total, prior = TOTAL_GOALS * f, PRIOR_POINTS * f
    o_h, o_a = ledger.offense["home"], ledger.offense["away"]
    d = total * (o_h - o_a) / (o_h + o_a + 2.0 * prior)
    eps = eps_unit * f
    d_noisy = max(-total, min(total, d + eps))
    whole = (_round_half_up((total + d_noisy) / 2.0), _round_half_up((total - d_noisy) / 2.0))
    g_h, g_a = max(whole[0], prev_home), max(whole[1], prev_away)
    ratcheted = (g_h, g_a) != whole

    tiebreak = None
    forced = False
    if period == periods:
        if g_h == g_a:
            lead = d_noisy if abs(d_noisy) > EPS else d
            if abs(lead) > EPS:
                tiebreak = "home" if lead > 0 else "away"
                if tiebreak == "home":
                    g_h += 1
                else:
                    g_a += 1
        leader = "home" if d > EPS else "away" if d < -EPS else None
        if leader and abs(d) >= NOISE_CAP - EPS:             # outside the upset window the raw leader must win
            while (g_h <= g_a) if leader == "home" else (g_a <= g_h):
                if leader == "home":
                    g_h += 1
                else:
                    g_a += 1
                forced = True
    return PeriodNorm(period, g_h, g_a, g_h - prev_home, g_a - prev_away, d, eps, tiebreak, ratcheted, forced)


def restrict(inputs: HockeyGameInputs, division: Division, periods: set[int]) -> HockeyGameInputs:
    """The inputs holding only the credit and shot attempts of real periods that belong to the given fantasy periods."""
    def keep(item) -> bool:
        return division.slot(item.asset_id, item.real_second).fantasy_period in periods

    def side(s):
        return dataclasses.replace(s, lines=tuple(l for l in s.lines if keep(l)), attempts=tuple(a for a in s.attempts if keep(a)))

    return dataclasses.replace(inputs, home=side(inputs.home), away=side(inputs.away))


def build_period(inputs: HockeyGameInputs, division: Division, period: int, seed: int, daily_game_id: int,
                 prev: dict[str, int], value_model: bool = False) -> tuple[list[SimEventInput], PeriodNorm, dict]:
    """Build fantasy period `period`. `inputs` hold ALL credit available through `period` (earlier periods' credit is
    needed to normalize cumulatively); `prev` is the last published event's totals (home_score, away_score, home_shots,
    away_shots; zeros before the first period). Returns (events with seq from 1, the period's normalization, a summary
    of the cumulative accounting for provenance)."""
    validate_inputs(inputs, value_model)
    through = restrict(inputs, division, set(range(1, period + 1)))
    only = restrict(inputs, division, {period})
    if value_model:
        ledger_through = build_value_ledger(through)
        tie_out_value_ledger(through, ledger_through)
        ledger_only = build_value_ledger(only)
        norm = normalize_value_through(ledger_through, noise_units(seed, daily_game_id), period, prev["home_score"], prev["away_score"])
    else:
        ledger_through = build_ledger(through)
        tie_out_ledger(through, ledger_through)
        ledger_only = build_ledger(only)
        norm = normalize_through(ledger_through, noise_unit(seed, daily_game_id), period, prev["home_score"], prev["away_score"])
    rng = derive_rng(seed, daily_game_id, f"story:{period}")
    filler = derive_rng(seed, daily_game_id, f"filler:{period}")      # its own stream: the story's draws are unchanged
    events = build_period_events(
        only, ledger_only, {"home": norm.add_home, "away": norm.add_away}, norm.tiebreak_side, rng, division, period, prev,
        filler_rng=filler)
    if (events[-1].home_score, events[-1].away_score) != (norm.home_goals, norm.away_goals):
        raise InputError("period events do not add up to the normalized goals")
    summary = {
        "raw": {"home": round(ledger_through.raw["home"], 6), "away": round(ledger_through.raw["away"], 6),
                "margin": round(ledger_through.raw_margin, 6)},
        "offense": {"home": round(ledger_through.offense["home"], 6), "away": round(ledger_through.offense["away"], 6)},
        "inverted_value": {"home": round(ledger_through.negative["away"], 6), "away": round(ledger_through.negative["home"], 6)},
    }
    if value_model:
        summary["value"] = {
            "created": {s: round(ledger_through.created[s], 6) for s in ("home", "away")},
            "saved": {s: round(ledger_through.saved[s], 6) for s in ("home", "away")},
            "net": {s: round(ledger_through.offense[s], 6) for s in ("home", "away")},
            "expected_goals": {"home": round(norm.lambda_home, 4), "away": round(norm.lambda_away, 4)}}
    return events, norm, summary
