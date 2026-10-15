"""
NHL series standings and head-to-head (rivalry) history (see docs/data_contract.md, "NHL series standings").

Everything here is derived on read from series_daily_games; no totals are stored, so there is nothing to
drift. league_series.team_a_wins/team_b_wins are NOT consulted.

Rules:
  * Every day is an individual game. Regular-season standings count daily games only; who wins a multi-day
    series does not matter until the playoffs, so series results never enter the standings.
  * Only FINAL daily games count. A game with equal scores is a tie (neither W nor L).
  * Ranking (best first): W-L record (wins desc, then losses asc), goal differential desc, head-to-head
    victories desc, goals for desc, goals against asc, team id asc. Head-to-head victories are daily wins
    over the other teams tied on W-L and goal differential. Teams equal on every key except team id share a
    rank (id only orders them).
  * Series results are still derived for rivalry history (a series is decided when all its games are FINAL
    and the side with more daily wins takes it; equal wins is a tie) but are informational only.
  * Rivalry pairs are keyed (league_id, low team id, high team id). Team ids are league-scoped, so two
    seasons/leagues never collide; a cross-season franchise key does not exist in the schema yet.
"""

import datetime
from dataclasses import dataclass, field

from sqlalchemy import func
from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import LeagueSeries, SeriesDailyGame, Team

RANK_KEYS = "wins desc, losses asc, goal_diff desc, h2h_wins desc, goals_for desc, goals_against asc, team_id asc"


@dataclass(frozen=True)
class GameRecord:
    series_id: int
    calendar_date: datetime.date
    home_team_id: int
    away_team_id: int
    home_score: int
    away_score: int
    final: bool


@dataclass(frozen=True)
class SeriesRecord:
    series_id: int
    team_a_id: int
    team_b_id: int
    start_date: datetime.date
    total_days: int
    games: int = 0          # scheduled game rows in the series (a window row each); 0 = total_days (legacy daily rows)

    @property
    def expected_games(self) -> int:
        return self.games or self.total_days


@dataclass
class TeamStanding:
    team_id: int
    wins: int = 0
    losses: int = 0
    ties: int = 0
    goals_for: int = 0
    goals_against: int = 0
    h2h_wins: int = 0  # wins over the other teams tied with this one on W-L and goal differential
    rank: int = 0

    @property
    def goal_diff(self) -> int:
        return self.goals_for - self.goals_against

    def sort_key(self) -> tuple:
        return (-self.wins, self.losses, -self.goal_diff, -self.h2h_wins, -self.goals_for, self.goals_against)


@dataclass
class PairRecord:
    low_id: int
    high_id: int
    series_low_wins: int = 0
    series_high_wins: int = 0
    series_ties: int = 0
    daily_low_wins: int = 0
    daily_high_wins: int = 0
    daily_ties: int = 0
    goals_low: int = 0
    goals_high: int = 0
    games: int = field(default=0)

    @property
    def series_played(self) -> int:
        return self.series_low_wins + self.series_high_wins + self.series_ties


def load_league(db: Session, league_id: int) -> tuple[list[int], list[SeriesRecord], list[GameRecord]]:
    """(team ids, series, games) as plain records so every calculation below is pure and re-runnable."""
    team_ids = sorted(t.id for t in db.query(Team.id).filter_by(league_id=league_id))
    series_rows = list(db.query(LeagueSeries).filter_by(league_id=league_id).order_by(LeagueSeries.start_date, LeagueSeries.id))
    counts = dict(db.query(SeriesDailyGame.series_id, func.count(SeriesDailyGame.id)).filter(
        SeriesDailyGame.series_id.in_([s.id for s in series_rows] or [0])).group_by(SeriesDailyGame.series_id))
    series = [SeriesRecord(s.id, s.team_a_id, s.team_b_id, s.start_date, s.total_days, counts.get(s.id, 0)) for s in series_rows]
    ids = [s.series_id for s in series]
    games = []
    if ids:
        rows = db.query(SeriesDailyGame).filter(SeriesDailyGame.series_id.in_(ids)).order_by(
            SeriesDailyGame.calendar_date, SeriesDailyGame.id)
        games = [GameRecord(g.series_id, g.calendar_date, g.home_team_id, g.away_team_id, g.home_score, g.away_score,
                            g.status == "FINAL") for g in rows]
    return team_ids, series, games


def _counted(games: list[GameRecord], as_of: datetime.date | None) -> list[GameRecord]:
    return [g for g in games if g.final and (as_of is None or g.calendar_date <= as_of)]


def decided_series(series: list[SeriesRecord], games: list[GameRecord], as_of: datetime.date | None = None
                   ) -> dict[int, tuple[int, int]]:
    """series_id -> (team_a daily wins, team_b daily wins) for series whose every game is counted."""
    counted = _counted(games, as_of)
    by_series: dict[int, list[GameRecord]] = {}
    for g in counted:
        by_series.setdefault(g.series_id, []).append(g)
    out = {}
    for s in series:
        gs = by_series.get(s.series_id, [])
        if len(gs) != s.expected_games:
            continue
        a = sum(1 for g in gs if _winner(g) == s.team_a_id)
        b = sum(1 for g in gs if _winner(g) == s.team_b_id)
        out[s.series_id] = (a, b)
    return out


def _winner(g: GameRecord) -> int | None:
    if g.home_score > g.away_score:
        return g.home_team_id
    if g.away_score > g.home_score:
        return g.away_team_id
    return None


def compute_standings(team_ids: list[int], series: list[SeriesRecord], games: list[GameRecord],
                      as_of: datetime.date | None = None) -> list[TeamStanding]:
    rows = {t: TeamStanding(t) for t in team_ids}
    results: list[tuple[int, int]] = []  # (winner, loser) per decided game
    for g in _counted(games, as_of):
        for tid, gf, ga in ((g.home_team_id, g.home_score, g.away_score), (g.away_team_id, g.away_score, g.home_score)):
            r = rows.get(tid)
            if r is None:
                continue
            r.goals_for += gf
            r.goals_against += ga
            if gf > ga:
                r.wins += 1
            elif gf < ga:
                r.losses += 1
            else:
                r.ties += 1
        w = _winner(g)
        if w is not None:
            results.append((w, g.away_team_id if w == g.home_team_id else g.home_team_id))
    buckets: dict[tuple, list[TeamStanding]] = {}
    for r in rows.values():
        buckets.setdefault((r.wins, r.losses, r.goal_diff), []).append(r)
    for members in buckets.values():
        ids = {m.team_id for m in members}
        if len(ids) < 2:
            continue
        for w, l in results:
            if w in ids and l in ids:
                rows[w].h2h_wins += 1
    ordered = sorted(rows.values(), key=lambda r: (*r.sort_key(), r.team_id))
    prev_key, prev_rank = None, 0
    for i, r in enumerate(ordered, start=1):
        key = r.sort_key()
        r.rank = prev_rank if key == prev_key else i
        prev_key, prev_rank = key, r.rank
    return ordered


def compute_pair_records(series: list[SeriesRecord], games: list[GameRecord], as_of: datetime.date | None = None
                         ) -> dict[tuple[int, int], PairRecord]:
    pairs: dict[tuple[int, int], PairRecord] = {}

    def pair(x: int, y: int) -> PairRecord:
        lo, hi = min(x, y), max(x, y)
        return pairs.setdefault((lo, hi), PairRecord(lo, hi))

    counted_by_series: dict[int, list[GameRecord]] = {}
    for g in _counted(games, as_of):
        counted_by_series.setdefault(g.series_id, []).append(g)
    decided = decided_series(series, games, as_of)
    for s in series:
        p = pair(s.team_a_id, s.team_b_id)
        for g in counted_by_series.get(s.series_id, []):
            p.games += 1
            w = _winner(g)
            if w is None:
                p.daily_ties += 1
            elif w == p.low_id:
                p.daily_low_wins += 1
            else:
                p.daily_high_wins += 1
            low_goals = g.home_score if g.home_team_id == p.low_id else g.away_score
            high_goals = g.away_score if g.home_team_id == p.low_id else g.home_score
            p.goals_low += low_goals
            p.goals_high += high_goals
        if s.series_id in decided:
            a, b = decided[s.series_id]
            if a == b:
                p.series_ties += 1
            else:
                winner = s.team_a_id if a > b else s.team_b_id
                if winner == p.low_id:
                    p.series_low_wins += 1
                else:
                    p.series_high_wins += 1
    return pairs
