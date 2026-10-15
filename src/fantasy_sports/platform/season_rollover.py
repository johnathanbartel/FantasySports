"""Season rollover: archive the finished season, reset what belongs to one season, and set up the next draft.

League Format (leagues.league_format) decides what carries over and how the next draft is ordered:
  * traditional_redraft  - rosters reset; draft order is randomized on draft day (the commissioner starts it).
  * future_pick_redraft  - rosters reset; snake draft in reverse order of the standings.
  * keeper / dynasty     - rosters stay; reverse order of the standings, not a snake. The commissioner sets the number
                           of (rookie) rounds.
Only Traditional Redraft was stated to reset rosters; Future Draft Pick Redraft is treated the same way, since a
snake draft of the whole roster only makes sense when the rosters are empty.

The archive (LeagueSeason) is a self-contained JSON snapshot so league history survives later schema changes.
Only snake-draft leagues roll over for now. Nothing here touches scoring or attribution.
"""

import datetime

from sqlalchemy.orm import Session

from fantasy_sports.platform import scoring, series_standings
from fantasy_sports.platform.db.models.core import (
    BankedCredit, Draft, DraftableAsset, DraftPick, FreeAgencyTransaction, League, LeagueSeason, LeagueSeries, LineupEntry,
    MatchupRequest, RosterEntry, ScheduleSettings, SeriesDailyGame, SimulatorEvent, SimulatorGame, SimulatorReveal, Team,
    WaiverClaim, Week, WeeklyMatchup, WeeklyTeamScore,
)
from fantasy_sports.platform.league_features import draft_state, roster_limit

RESETS_ROSTERS = ("traditional_redraft", "future_pick_redraft")


class RolloverError(ValueError):
    pass


def _asset_name(a: DraftableAsset) -> str:
    if a.asset_type == "player":
        return a.player.full_name
    return f"{a.defense_group.real_team.name if a.defense_group.real_team else '?'} {a.defense_group.position_group}"


def final_standings(db: Session, league: League) -> list[dict]:
    """Every team best-first: [{rank, team_id, team_name, wins, losses, ties, ...}]."""
    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=league.id)}
    if league.series_length_days is not None:
        team_ids, series, games = series_standings.load_league(db, league.id)
        rows = series_standings.compute_standings(team_ids, series, games)
        return [{"rank": r.rank, "team_id": r.team_id, "team_name": names[r.team_id], "wins": r.wins, "losses": r.losses,
                 "ties": r.ties, "goals_for": r.goals_for, "goals_against": r.goals_against} for r in rows]
    rows = scoring.standings(db, league.id)
    return [{**r, "rank": i} for i, r in enumerate(rows, start=1)]


def draft_order(db: Session, league: League) -> list[int] | None:
    """Team ids in pick order for the next draft, or None when the order is random on draft day."""
    if league.league_format == "traditional_redraft":
        return None
    return [r["team_id"] for r in reversed(final_standings(db, league))]


def _snapshot(db: Session, league: League, standings: list[dict]) -> dict:
    teams = db.query(Team).filter_by(league_id=league.id).order_by(Team.id).all()
    team_ids = [t.id for t in teams]
    matchup_rows = db.query(WeeklyMatchup).filter_by(league_id=league.id).order_by(WeeklyMatchup.id).all()
    weeks = {w.id: w for w in db.query(Week).filter(Week.id.in_([m.week_id for m in matchup_rows] or [0]))}
    matchups = [{"week": weeks[m.week_id].week_number if m.week_id in weeks else None,
                 "season_year": weeks[m.week_id].season_year if m.week_id in weeks else None,
                 "team_a_id": m.team_a_id, "team_b_id": m.team_b_id, "team_a_score": m.team_a_score,
                 "team_b_score": m.team_b_score, "winner_team_id": m.winner_team_id} for m in matchup_rows]
    scores = [{"team_id": s.team_id, "week_id": s.week_id, "total_points": s.total_points}
              for s in db.query(WeeklyTeamScore).filter(WeeklyTeamScore.team_id.in_(team_ids or [0]))]
    rosters = {t.id: [{"asset_id": e.draftable_asset_id, "name": _asset_name(e.draftable_asset), "acquired_via": e.acquired_via,
                       "on_injured_list": e.on_injured_list}
                      for e in db.query(RosterEntry).filter_by(team_id=t.id).order_by(RosterEntry.id)] for t in teams}
    drafts = []
    for d in db.query(Draft).filter_by(league_id=league.id, season_number=league.season_number).order_by(Draft.id):
        drafts.append({"draft_id": d.id, "status": d.status, "is_snake": d.is_snake, "picks": [
            {"round": p.round, "pick_number": p.pick_number, "team_id": p.team_id, "asset_id": p.draftable_asset_id,
             "name": _asset_name(p.draftable_asset) if p.draftable_asset else None, "is_autopick": p.is_autopick}
            for p in db.query(DraftPick).filter_by(draft_id=d.id).order_by(DraftPick.pick_number)]})
    transactions = [{"team_id": x.team_id, "asset_id": x.draftable_asset_id, "kind": x.kind, "price": x.price,
                     "reason": x.reason, "at": x.created_at.isoformat() if x.created_at else None}
                    for x in db.query(FreeAgencyTransaction).filter_by(league_id=league.id).order_by(FreeAgencyTransaction.id)]
    series = []
    for s in db.query(LeagueSeries).filter_by(league_id=league.id).order_by(LeagueSeries.start_date, LeagueSeries.id):
        series.append({"team_a_id": s.team_a_id, "team_b_id": s.team_b_id, "start_date": str(s.start_date),
                       "end_date": str(s.end_date), "total_days": s.total_days, "games": [
                           {"date": str(g.calendar_date), "home_team_id": g.home_team_id, "away_team_id": g.away_team_id,
                            "home_score": g.home_score, "away_score": g.away_score, "status": g.status}
                           for g in db.query(SeriesDailyGame).filter_by(series_id=s.id)
                           .order_by(SeriesDailyGame.calendar_date, SeriesDailyGame.id)]})
    return {"teams": [{"id": t.id, "name": t.name, "owner_user_id": t.owner_user_id, "is_ai_owned": t.is_ai_owned} for t in teams],
            "standings": standings, "matchups": matchups, "weekly_scores": scores, "series": series,
            "rosters": rosters, "drafts": drafts, "transactions": transactions}


def _clear_season(db: Session, league_id: int, team_ids: list[int]) -> None:
    """Deletes what belongs to one season (the archive has already captured it), child rows first like league_lifecycle."""
    series_ids = [r[0] for r in db.query(LeagueSeries.id).filter_by(league_id=league_id)]
    game_ids = [r[0] for r in db.query(SeriesDailyGame.id).filter(SeriesDailyGame.series_id.in_(series_ids))] if series_ids else []
    sim_ids = [r[0] for r in db.query(SimulatorGame.id).filter(SimulatorGame.daily_game_id.in_(game_ids))] if game_ids else []
    if sim_ids:
        db.query(SimulatorReveal).filter(SimulatorReveal.simulator_game_id.in_(sim_ids)).delete(synchronize_session=False)
        db.query(SimulatorEvent).filter(SimulatorEvent.simulator_game_id.in_(sim_ids)).delete(synchronize_session=False)
        db.query(SimulatorGame).filter(SimulatorGame.id.in_(sim_ids)).delete(synchronize_session=False)
    db.query(BankedCredit).filter(BankedCredit.league_id == league_id).delete(synchronize_session=False)
    if series_ids:
        db.query(SeriesDailyGame).filter(SeriesDailyGame.series_id.in_(series_ids)).delete(synchronize_session=False)
        db.query(LeagueSeries).filter(LeagueSeries.id.in_(series_ids)).delete(synchronize_session=False)
    db.query(WeeklyMatchup).filter_by(league_id=league_id).delete(synchronize_session=False)
    db.query(MatchupRequest).filter_by(league_id=league_id).delete(synchronize_session=False)
    db.query(ScheduleSettings).filter_by(league_id=league_id).delete(synchronize_session=False)
    db.query(WaiverClaim).filter_by(league_id=league_id).delete(synchronize_session=False)
    if team_ids:
        db.query(LineupEntry).filter(LineupEntry.team_id.in_(team_ids)).delete(synchronize_session=False)
        db.query(WeeklyTeamScore).filter(WeeklyTeamScore.team_id.in_(team_ids)).delete(synchronize_session=False)


def roll_over(db: Session, league: League, offseason_rounds: int | None = None) -> LeagueSeason:
    """Archives the current season and opens the offseason. Flushes, does not commit."""
    if league.draft_type != "snake":
        raise RolloverError("Season rollover is only available for snake-draft leagues for now")
    if league.offseason:
        raise RolloverError("This league is already in the offseason")
    if draft_state(db, league.id) != "complete":
        raise RolloverError("The league's draft has to finish before the season can roll over")
    keeps = league.league_format not in RESETS_ROSTERS
    if keeps and (offseason_rounds is None or offseason_rounds < 1):
        raise RolloverError("Set how many rounds the next draft has (at least 1) - Keeper and Dynasty drafts are rookie drafts")

    standings = final_standings(db, league)
    team_ids = [t.id for t in db.query(Team.id).filter_by(league_id=league.id)]
    archive = LeagueSeason(league_id=league.id, season_number=league.season_number, league_format=league.league_format,
                           archived_at=datetime.datetime.utcnow(), data=_snapshot(db, league, standings))
    db.add(archive)
    _clear_season(db, league.id, team_ids)
    if not keeps and team_ids:
        db.query(RosterEntry).filter(RosterEntry.team_id.in_(team_ids)).delete(synchronize_session=False)
    league.season_number += 1
    league.offseason = True
    league.offseason_draft_rounds = offseason_rounds if keeps else None
    db.flush()
    return archive


def next_draft_rounds(db: Session, league: League) -> int:
    """Rounds of the offseason draft: the commissioner's number for Keeper / Dynasty, a full roster otherwise."""
    return league.offseason_draft_rounds or roster_limit(db, league)


def teams_over_cap(db: Session, league: League, rounds: int) -> list[str]:
    """Teams whose roster plus the draft's rounds would not fit - they drop players during the offseason roster freeze."""
    limit = roster_limit(db, league)
    over = []
    for t in db.query(Team).filter_by(league_id=league.id):
        n = db.query(RosterEntry).filter_by(team_id=t.id, on_injured_list=False).count()
        if n + rounds > limit:
            over.append(f"{t.name} ({n} players, room for {max(0, limit - n)})")
    return over
