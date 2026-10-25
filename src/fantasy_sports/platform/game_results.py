"""
The one canonical final result of an NHL fantasy game.

A FINAL game's score is the last running total of its published simulation (SimulatorGame / SimulatorEvent: the script is
immutable once written). series_daily_games.home_score / away_score is only a mirror of it, kept so legacy rows and live
games still have a score; a mirror can drift (it is advanced with max() while a game is live, and a game that was simulated
again or corrected keeps the larger of two versions). Every reader that totals results - the scoreboard, the schedule, the
series standings, rivalries and Game Score - takes the score from here, so they can never disagree.

Whether a viewer has watched a game decides only what that viewer is shown (platform/spoilers.py); it never decides
whether, or with which score, a FINAL game counts.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import LeagueSeries, SeriesDailyGame, SimulatorEvent, SimulatorGame


def final_scores(db: Session, daily_game_ids: list[int] | None = None, league_id: int | None = None) -> dict[int, tuple[int, int]]:
    """daily_game_id -> (home, away) from the last event of each FINAL simulation. Games without a FINAL simulation are absent."""
    last_seq = (db.query(SimulatorEvent.simulator_game_id.label("sid"), func.max(SimulatorEvent.seq).label("seq"))
                .group_by(SimulatorEvent.simulator_game_id).subquery())
    q = (db.query(SimulatorGame.daily_game_id, SimulatorEvent.home_score, SimulatorEvent.away_score)
         .join(last_seq, last_seq.c.sid == SimulatorGame.id)
         .join(SimulatorEvent, (SimulatorEvent.simulator_game_id == last_seq.c.sid) & (SimulatorEvent.seq == last_seq.c.seq))
         .filter(SimulatorGame.status == "FINAL"))
    if league_id is not None:
        q = (q.join(SeriesDailyGame, SeriesDailyGame.id == SimulatorGame.daily_game_id)
             .join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id).filter(LeagueSeries.league_id == league_id))
    if daily_game_ids is not None:
        if not daily_game_ids:
            return {}
        q = q.filter(SimulatorGame.daily_game_id.in_(daily_game_ids))
    return {gid: (h, a) for gid, h, a in q}


def score_of(game: SeriesDailyGame, canonical: dict[int, tuple[int, int]]) -> tuple[int, int]:
    """The canonical (home, away) of a FINAL game, else the stored mirror."""
    if game.status == "FINAL" and game.id in canonical:
        return canonical[game.id]
    return game.home_score, game.away_score


@dataclass(frozen=True)
class Drift:
    daily_game_id: int
    calendar_date: object
    home_team_id: int
    away_team_id: int
    stored: tuple[int, int]
    canonical: tuple[int, int]


def drift(db: Session, league_id: int) -> list[Drift]:
    """FINAL games whose stored mirror differs from the canonical result (read-only; a repair is a separate, approved step)."""
    canonical = final_scores(db, league_id=league_id)
    rows = (db.query(SeriesDailyGame).join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
            .filter(LeagueSeries.league_id == league_id, SeriesDailyGame.status == "FINAL")
            .order_by(SeriesDailyGame.calendar_date, SeriesDailyGame.id))
    return [Drift(g.id, g.calendar_date, g.home_team_id, g.away_team_id, (g.home_score, g.away_score), canonical[g.id])
            for g in rows if g.id in canonical and canonical[g.id] != (g.home_score, g.away_score)]
