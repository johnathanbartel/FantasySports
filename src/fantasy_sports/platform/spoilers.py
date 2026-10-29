"""
Which finished simulated games a viewer may see results from (the scoreboard's no-spoiler rule).

A FINAL game is open to a viewer when both teams are AI, or the viewer has opened it, or one of the two owners has
(the owner opening it makes it public). Everything that totals simulated results (league leaders, series standings)
counts only open games, so those pages never give away a score the owner has not watched yet.

The series standings use standings_held instead: one table for every viewer, where a final also counts once the next game of
either team has started, so an owner who never watches cannot hold the league's table back.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import (
    LeagueSeries, SeriesDailyGame, SimulatorGame, SimulatorReveal, Team,
)


def final_sims(db: Session, league_id: int, viewer_user_id: int | None) -> tuple[list[tuple[SimulatorGame, SeriesDailyGame]], list[tuple[SimulatorGame, SeriesDailyGame]]]:
    """(open, held back): every FINAL simulated game of the league, split by whether this viewer may see its result."""
    sims = (db.query(SimulatorGame, SeriesDailyGame).join(SeriesDailyGame, SeriesDailyGame.id == SimulatorGame.daily_game_id)
            .join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
            .filter(LeagueSeries.league_id == league_id, SimulatorGame.status == "FINAL").all())
    teams = {t.id: t for t in db.query(Team).filter_by(league_id=league_id)}
    revealed: dict[int, set[int]] = {}
    ids = [s.id for s, _ in sims]
    if ids:
        for sim_id, uid in db.query(SimulatorReveal.simulator_game_id, SimulatorReveal.user_id).filter(
                SimulatorReveal.simulator_game_id.in_(ids)):
            revealed.setdefault(sim_id, set()).add(uid)
    shown, held = [], []
    for sim, dg in sims:
        home, away = teams.get(dg.home_team_id), teams.get(dg.away_team_id)
        ai_vs_ai = bool(home and away and home.is_ai_owned and away.is_ai_owned)
        seen = revealed.get(sim.id, set())
        owners = {t.owner_user_id for t in (home, away) if t and t.owner_user_id}
        open_ = ai_vs_ai or (viewer_user_id is not None and viewer_user_id in seen) or bool(owners & seen)
        (shown if open_ else held).append((sim, dg))
    return shown, held


def standings_held(db: Session, league_id: int, today: datetime.date) -> set[int]:
    """Daily game ids of FINAL games the standings leave out for now, the same for every viewer.

    A final is held while nobody it concerns has opened it: it counts as soon as both teams are AI, either owner has opened it, or
    the next game of either team has started (so an owner who never watches cannot stall the table). A game with no next game is
    released the day after it ends. Whether a particular viewer has watched never matters here: one table for the whole league.
    """
    _, held = final_sims(db, league_id, None)
    if not held:
        return set()
    rows = (db.query(SeriesDailyGame).join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
            .filter(LeagueSeries.league_id == league_id).all())
    by_team: dict[int, list[SeriesDailyGame]] = {}
    for g in rows:
        for tid in (g.home_team_id, g.away_team_id):
            by_team.setdefault(tid, []).append(g)
    out = set()
    for _, dg in held:
        end = dg.calendar_date_end or dg.calendar_date
        released = False
        has_next = False
        for tid in (dg.home_team_id, dg.away_team_id):
            later = [g for g in by_team.get(tid, []) if g.calendar_date > end]
            if later:
                has_next = True
                nxt = min(later, key=lambda g: (g.calendar_date, g.id))
                if nxt.status != "SCHEDULED" or nxt.calendar_date <= today:
                    released = True
        if not has_next and end < today:
            released = True
        if not released:
            out.add(dg.id)
    return out
