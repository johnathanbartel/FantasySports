"""
The fitted scale from a team's lineup-summed EPA credit for a week to fantasy points.

    to_fantasy_points(raw_team_credit, context) -> float

    z      = ncaa_ppg + b * (raw_team_credit - expected_lineup_credit(context)) + c
    points = A * ln(1 + exp(z / A))                              (softplus: a smooth floor at 0)

What each piece is for (numbers and how they were fitted: docs/scoring_scale_fit_report.md):

  * ncaa_ppg           the NCAA FBS points per team per game (all regular-season games, FBS teams). A
                       lineup with the league's expected credit scores exactly this.
  * expected_lineup_credit(context)
                       what a lineup like this league's normally scores in credit. Drafted starters are
                       far better than the pool average (~+20 a week vs ~0), and the deeper a league
                       drafts the weaker its starters get (about +23 at 6 teams, +18 at 20), so it
                       depends on the league size and the lineup's slots. Measured by simulating
                       drafted leagues on real 2023-24 seasons.
  * b                  credit -> points. Chosen so the spread of scores matches the NCAA's spread of
                       team points; it comes out ~0.97, i.e. one credit is ~one football point.
  * c, A               the floor. A strictly-increasing softplus keeps a very bad week above 0 without
                       a clip (a clip would create ties). c re-centres it so the mean stays ncaa_ppg.

Strictly increasing in raw_team_credit for any context (b > 0, softplus' > 0), the same for every team in a
league, no randomness. The map depends only on the league (its size and slots), never on the other teams'
scores, so a score does not change when other teams play.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping

_PARAMS_PATH = Path(__file__).resolve().parents[1] / "sports" / "football" / "college" / "data" / "scoring_scale.json"


@dataclass(frozen=True)
class ScaleContext:
    """What the scale needs to know about a league.

    n_teams        the number of teams in the league (count the Team rows; league.size is what was
                   requested, not necessarily what exists).
    starter_slots  the league's STARTING lineup slots as {slot_code: how many}, e.g.
                   {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "DL": 1, "LB": 1, "DB": 1, "K": 1}
                   (RosterSlotDefinition rows with is_starter, slot_code -> slot_count; bench excluded).
    """
    n_teams: int
    starter_slots: Mapping[str, int]


@lru_cache(maxsize=1)
def _params(path: str = str(_PARAMS_PATH)) -> dict:
    return json.loads(Path(path).read_text())


def _interp(n: int, sizes: list[int], values: list[float]) -> float:
    """Linear in league size; flat beyond the fitted range (4-24 teams)."""
    if n <= sizes[0]:
        return values[0]
    if n >= sizes[-1]:
        return values[-1]
    i = bisect_right(sizes, n) - 1
    frac = (n - sizes[i]) / (sizes[i + 1] - sizes[i])
    return values[i] + frac * (values[i + 1] - values[i])


def expected_lineup_credit(context: ScaleContext, params: dict | None = None) -> float:
    """Expected weekly credit of a lineup in this league: the sum over its starting slots of the fitted mean
    credit of a starter in that slot at this league size. `params` overrides the fitted file (used to test a fit
    on held-out seasons)."""
    p = params or _params()
    total = 0.0
    for slot, count in context.starter_slots.items():
        for i in range(1, count + 1):
            series = p["slot_mean"].get(f"{slot}#{i}")
            if series is None:
                raise ValueError(
                    f"No fitted scale for lineup slot {slot}#{i}; the scale was fitted for "
                    f"{sorted(p['slot_mean'])}. Refit (college/scripts/fit_scoring_scale.py) for a new lineup shape.")
            total += _interp(context.n_teams, p["league_sizes"], series)
    return total


def to_fantasy_points(raw_team_credit: float, context: ScaleContext, params: dict | None = None) -> float:
    p = params or _params()
    z = p["ncaa_ppg"] + p["b"] * (raw_team_credit - expected_lineup_credit(context, p)) + p["softplus_c"]
    a = p["softplus_A"]
    t = z / a
    # softplus, overflow-safe: a*ln(1+e^t) = a*max(t,0) + a*ln(1+e^-|t|)
    return a * (max(t, 0.0) + math.log1p(math.exp(-abs(t))))


def context_for_league(db, league_id: int) -> ScaleContext:
    """Build the context from the app's tables (imported lazily so the scale itself has no DB dependency)."""
    from fantasy_sports.platform.db.models.core import LeagueUniverse, RosterSlotDefinition, Team

    n_teams = db.query(Team).filter_by(league_id=league_id).count()
    universe_ids = [lu.universe_id for lu in db.query(LeagueUniverse).filter_by(league_id=league_id)]
    slots: dict[str, int] = {}
    for d in db.query(RosterSlotDefinition).filter(RosterSlotDefinition.universe_id.in_(universe_ids or [0]),
                                                   RosterSlotDefinition.is_starter.is_(True)):
        slots[d.slot_code] = slots.get(d.slot_code, 0) + d.slot_count
    return ScaleContext(n_teams=n_teams, starter_slots=slots)
