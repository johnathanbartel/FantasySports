from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from fantasy_sports.api.helpers import draftable_asset_display_name
from fantasy_sports.api.auth import assert_commissioner, current_user
from fantasy_sports.api.schemas import (
    MatchupResultOut, PlayCreditDetail, ScheduleRequest, StarterScoreOut, TeamWeekBreakdownOut, TeamWeeklyScoreOut,
    WeeklyScoreBreakdown,
)
from fantasy_sports.platform import schedule, scoring
from fantasy_sports.platform.db.models.core import DraftableAsset, Game, Team, User, WeeklyMatchup
from fantasy_sports.sports.football.models import Play, PlayCredit
from fantasy_sports.platform.db.session import get_db

router = APIRouter(tags=["scoring"])


@router.get("/players/{draftable_asset_id}/weekly-score", response_model=WeeklyScoreBreakdown)
def get_weekly_score_breakdown(
    draftable_asset_id: int, week_id: int, db: Session = Depends(get_db)
):
    """Per-play credit breakdown for one player/defense-group in one week -
    the same shape run_game_pipeline.py already prints to the console,
    just served over the API instead."""
    asset = db.get(DraftableAsset, draftable_asset_id)
    if asset is None:
        raise HTTPException(404, "Draftable asset not found")

    credits = (
        db.query(PlayCredit, Play)
        .join(Play, Play.id == PlayCredit.play_id)
        .join(Game, Game.id == Play.game_id)
        .filter(PlayCredit.draftable_asset_id == draftable_asset_id, Game.week_id == week_id)
        .all()
    )
    details = [
        PlayCreditDetail(
            play_id=play.id, role=credit.role, credit_points=credit.credit_points,
            play_text=play.play_text,
        )
        for credit, play in credits
    ]
    total = sum(d.credit_points for d in details)

    return WeeklyScoreBreakdown(
        draftable_asset_id=draftable_asset_id,
        display_name=draftable_asset_display_name(asset),
        week_id=week_id,
        total_points=total,
        play_credits=details,
    )


@router.get("/teams/{team_id}/weekly-score", response_model=TeamWeeklyScoreOut)
def get_team_weekly_score(team_id: int, week_id: int, db: Session = Depends(get_db)):
    """The team's week in fantasy points: the fitted scale applied to its STARTERS' summed credit (bench doesn't count), computed live."""
    return TeamWeeklyScoreOut(team_id=team_id, week_id=week_id, total_points=scoring.team_week_score(db, team_id, week_id))


@router.get("/teams/{team_id}/week-breakdown", response_model=TeamWeekBreakdownOut)
def get_team_week_breakdown(team_id: int, week_id: int, db: Session = Depends(get_db)):
    """Each starter's score for the week and their total - the numbers behind a matchup score."""
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found")
    lines = scoring.lineup_breakdown(db, team_id, week_id)
    return TeamWeekBreakdownOut(
        team_id=team_id, team_name=team.name, week_id=week_id, total_points=scoring.team_week_score(db, team_id, week_id) if lines else 0.0,
        raw_credit=sum(l.points for l in lines),
        starters=[StarterScoreOut(slot_code=l.slot_code, draftable_asset_id=l.draftable_asset_id, display_name=l.display_name,
                                  position=l.position, real_team=l.real_team, played=l.played, points=l.points)
                  for l in lines])


def _result_out(db: Session, r: scoring.MatchupResult) -> MatchupResultOut:
    return MatchupResultOut(
        matchup_id=r.matchup_id, week_id=r.week_id, team_a_id=r.team_a_id, team_a_name=db.get(Team, r.team_a_id).name,
        team_b_id=r.team_b_id, team_b_name=db.get(Team, r.team_b_id).name, scored=r.scored, championship=r.championship,
        team_a_score=r.team_a_score, team_b_score=r.team_b_score, winner_team_id=r.winner_team_id)


@router.get("/matchups/{matchup_id}", response_model=MatchupResultOut)
def get_matchup(matchup_id: int, db: Session = Depends(get_db)):
    """A matchup with its score and winner derived live from the two lineups."""
    matchup = db.get(WeeklyMatchup, matchup_id)
    if matchup is None:
        raise HTTPException(404, "Matchup not found")
    scored = matchup.week_id in scoring.scored_week_ids(db, [matchup.week_id])
    return _result_out(db, scoring.matchup_result(db, matchup, scored))


@router.get("/leagues/{league_id}/matchups", response_model=list[MatchupResultOut])
def league_matchups(league_id: int, week_id: int | None = None, db: Session = Depends(get_db)):
    """The league's schedule with live results, optionally for one week."""
    out = []
    for wid, results in scoring.league_results(db, league_id).items():
        if week_id is None or wid == week_id:
            out.extend(_result_out(db, r) for r in results)
    return out


@router.post("/leagues/{league_id}/schedule/auto")
def create_schedule(league_id: int, payload: ScheduleRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Commissioner action: the premade regular-season schedule (round-robin, weeks 1-9) that teams can then renegotiate."""
    assert_commissioner(db, user, league_id)
    try:
        n = schedule.create_schedule(db, league_id, payload.weeks, replace=payload.replace)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return {"matchups": n}
