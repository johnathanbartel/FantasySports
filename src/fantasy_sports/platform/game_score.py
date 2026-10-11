"""
The Game Score: a deterministic, explainable 0-100 leverage rating for one NHL daily game
(docs/hockey_spec.md section 8.2). It is a hand-built formula, not a learned model.

Inputs are the final results published ONE WEEK AHEAD: a game on date D is rated from standings built from
FINAL games dated on or before D - PUBLISH_LEAD_DAYS (an `as_of` override exists for audits). Once those
games are final the rating never changes, so repeat reads agree.

Components, each in [0, 1] (A and B are the two teams; groups are conferences, or the whole league if
teams have none):

  margin    1 / (1 + |series wins A - series wins B|)            closer in the standings -> higher
  cutoff    mean over A, B of 1 / (1 + |series wins - line|)     nearer the playoff line -> higher
            line = midpoint of the last team in and first team out (top max(1, size // 2) make it);
            0 when there is no race (everyone qualifies)
  rivalry   n / (n + RIVALRY_HALF_LIFE) * (1 - |w_A - w_B| / n)   n = prior head-to-head daily games, w = daily
            wins; more history and a more even history -> higher; 0 with no history
  urgency   1 - series remaining after this one / total series   later in the season -> higher

  score = 100 * ( W_RIVALRY * rivalry + W_URGENCY * urgency
                  + urgency * (W_MARGIN * margin + W_CUTOFF * cutoff) )
  weights 0.15 + 0.25 + 0.30 + 0.30 = 1.00, so the maximum is exactly 100, and the standings terms are scaled
  by urgency so an opening-week game rates low however tight the (empty) standings are. The score is
  monotonic non-decreasing in every component. Result is rounded to an int and clamped to 0-100.
"""

import datetime
from dataclasses import dataclass

from fantasy_sports.platform.series_standings import (
    GameRecord, PairRecord, SeriesRecord, TeamStanding, compute_pair_records, compute_standings,
)

PUBLISH_LEAD_DAYS = 7
W_RIVALRY, W_URGENCY, W_MARGIN, W_CUTOFF = 0.15, 0.25, 0.30, 0.30
RIVALRY_HALF_LIFE = 10  # head-to-head daily games at which the volume factor reaches 0.5


@dataclass(frozen=True)
class GameScore:
    score: int
    margin: float
    cutoff: float
    rivalry: float
    urgency: float
    as_of: datetime.date


def _group_standings(standings: list[TeamStanding], group_of: dict[int, int | None]) -> dict[int | None, list[TeamStanding]]:
    groups: dict[int | None, list[TeamStanding]] = {}
    for s in standings:
        groups.setdefault(group_of.get(s.team_id), []).append(s)
    return groups


def _cutoff_line(group: list[TeamStanding]) -> float | None:
    spots = max(1, len(group) // 2)
    if spots >= len(group):
        return None
    return (group[spots - 1].series_wins + group[spots].series_wins) / 2


def rivalry_intensity(pair: PairRecord | None) -> float:
    if pair is None or pair.games == 0:
        return 0.0
    n = pair.games
    return n / (n + RIVALRY_HALF_LIFE) * (1 - abs(pair.daily_low_wins - pair.daily_high_wins) / n)


def combine(margin: float, cutoff: float, rivalry: float, urgency: float) -> int:
    raw = 100 * (W_RIVALRY * rivalry + W_URGENCY * urgency + urgency * (W_MARGIN * margin + W_CUTOFF * cutoff))
    return max(0, min(100, round(raw)))


def rate_league(team_ids: list[int], series: list[SeriesRecord], games: list[GameRecord],
                group_of: dict[int, int | None], targets: list[GameRecord] | None = None,
                as_of_override: datetime.date | None = None) -> dict[tuple[int, datetime.date], GameScore]:
    """Rate every game (or `targets`), keyed (series_id, calendar_date). Pure and deterministic."""
    series_by_id = {s.series_id: s for s in series}
    round_starts = sorted({s.start_date for s in series})
    total = len(round_starts)
    cache: dict[datetime.date, tuple[dict, dict]] = {}
    out: dict[tuple[int, datetime.date], GameScore] = {}
    for g in (targets if targets is not None else games):
        s = series_by_id[g.series_id]
        as_of = as_of_override or g.calendar_date - datetime.timedelta(days=PUBLISH_LEAD_DAYS)
        if as_of not in cache:
            standings = compute_standings(team_ids, series, games, as_of)
            groups = _group_standings(standings, group_of)
            cache[as_of] = ({st.team_id: st for st in standings},
                            {gid: (grp, _cutoff_line(grp)) for gid, grp in groups.items()}, compute_pair_records(series, games, as_of))
        by_team, group_info, pairs = cache[as_of]
        a, b = by_team[s.team_a_id], by_team[s.team_b_id]
        margin = 1 / (1 + abs(a.series_wins - b.series_wins))
        prox = []
        for st in (a, b):
            _, line = group_info[group_of.get(st.team_id)]
            prox.append(0.0 if line is None else 1 / (1 + abs(st.series_wins - line)))
        cutoff = sum(prox) / 2
        rivalry = rivalry_intensity(pairs.get((min(s.team_a_id, s.team_b_id), max(s.team_a_id, s.team_b_id))))
        remaining = sum(1 for d in round_starts if d > s.start_date)
        urgency = 1 - remaining / total if total else 0.0
        out[(g.series_id, g.calendar_date)] = GameScore(combine(margin, cutoff, rivalry, urgency), margin, cutoff,
                                                         rivalry, urgency, as_of)
    return out
