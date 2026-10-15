"""
Which finished simulated games a viewer may see results from (the scoreboard's no-spoiler rule).

A FINAL game is open to a viewer when both teams are AI, or the viewer has opened it, or one of the two owners has
(the owner opening it makes it public). Everything that totals simulated results (league leaders, series standings)
counts only open games, so those pages never give away a score the owner has not watched yet.
"""

from __future__ import annotations

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
