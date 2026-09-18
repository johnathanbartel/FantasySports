from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from fantasy_sports.api.schemas import LeagueCreateRequest, LeagueOut, StandingsRow, TeamOut
from fantasy_sports.platform.db.models.core import League, LeagueUniverse, Team, Universe, WeeklyMatchup
from fantasy_sports.platform.db.session import get_db

router = APIRouter(prefix="/leagues", tags=["leagues"])


@router.post("", response_model=LeagueOut)
def create_league(payload: LeagueCreateRequest, db: Session = Depends(get_db)):
    universes = db.query(Universe).filter(Universe.code.in_(payload.universe_codes)).all()
    if len(universes) != len(payload.universe_codes):
        found = {u.code for u in universes}
        missing = set(payload.universe_codes) - found
        raise HTTPException(400, f"Unknown universe code(s): {sorted(missing)}")

    for code, mode in payload.defense_modes.items():
        if mode not in ("group", "individual"):
            raise HTTPException(400, f"Invalid defense_mode '{mode}' for universe '{code}'")
    unsupported = [
        u.code for u in universes
        if payload.defense_modes.get(u.code) == "individual" and not u.supports_individual_defense
    ]
    if unsupported:
        raise HTTPException(400, f"Universe(s) {sorted(unsupported)} don't support individual defense")

    league = League(
        name=payload.name,
        commissioner_user_id=payload.commissioner_user_id,
        size=payload.size,
        draft_rounds=payload.draft_rounds,
    )
    db.add(league)
    db.flush()  # assigns league.id without committing yet

    for universe in universes:
        db.add(LeagueUniverse(
            league_id=league.id, universe_id=universe.id,
            defense_mode=payload.defense_modes.get(universe.code, "group"),
        ))

    db.commit()
    db.refresh(league)
    return league


@router.get("/{league_id}", response_model=LeagueOut)
def get_league(league_id: int, db: Session = Depends(get_db)):
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    return league


@router.get("/{league_id}/teams", response_model=list[TeamOut])
def get_league_teams(league_id: int, db: Session = Depends(get_db)):
    """Team-name resolution for any screen that shows picks/lineups/matchups
    by team - those only carry team_id, not a display name."""
    teams = db.query(Team).filter(Team.league_id == league_id).order_by(Team.id).all()
    if not teams:
        raise HTTPException(404, "League not found or has no teams")
    return teams


@router.get("/{league_id}/standings", response_model=list[StandingsRow])
def get_standings(league_id: int, db: Session = Depends(get_db)):
    """Derived on read from weekly_matchups - not a stored table, so it
    can't drift out of sync with the matchup results it's built from."""
    teams = db.query(Team).filter(Team.league_id == league_id).all()
    if not teams:
        raise HTTPException(404, "League not found or has no teams")
    team_names = {t.id: t.name for t in teams}

    wins = defaultdict(int)
    losses = defaultdict(int)
    points_for = defaultdict(float)
    points_against = defaultdict(float)

    matchups = (
        db.query(WeeklyMatchup)
        .filter(WeeklyMatchup.league_id == league_id, WeeklyMatchup.winner_team_id.isnot(None))
        .all()
    )
    for m in matchups:
        points_for[m.team_a_id] += m.team_a_score or 0.0
        points_for[m.team_b_id] += m.team_b_score or 0.0
        points_against[m.team_a_id] += m.team_b_score or 0.0
        points_against[m.team_b_id] += m.team_a_score or 0.0
        loser_id = m.team_b_id if m.winner_team_id == m.team_a_id else m.team_a_id
        wins[m.winner_team_id] += 1
        losses[loser_id] += 1

    rows = [
        StandingsRow(
            team_id=team_id,
            team_name=team_names[team_id],
            wins=wins[team_id],
            losses=losses[team_id],
            points_for=points_for[team_id],
            points_against=points_against[team_id],
        )
        for team_id in team_names
    ]
    rows.sort(key=lambda r: (-r.wins, -r.points_for))
    return rows
