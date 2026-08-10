from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.helpers import draftable_asset_display_name
from api.schemas import MatchupOut, PlayCreditDetail, TeamWeeklyScoreOut, WeeklyScoreBreakdown
from db.models.core import DraftableAsset, Game, LineupEntry, WeeklyMatchup
from db.models.football import Play, PlayCredit, WeeklyPlayerScore
from db.session import get_db

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
    """Sums that team's active lineup's WeeklyPlayerScore rows for the
    week. Computed live from lineup + player scores (the source of truth)
    rather than read from a pre-populated cache, since the batch job that
    would populate weekly_team_scores isn't built in this pass."""
    lineup_asset_ids = [
        e.draftable_asset_id
        for e in db.query(LineupEntry).filter(
            LineupEntry.team_id == team_id, LineupEntry.week_id == week_id
        )
    ]
    if not lineup_asset_ids:
        return TeamWeeklyScoreOut(team_id=team_id, week_id=week_id, total_points=0.0)

    scores = (
        db.query(WeeklyPlayerScore)
        .filter(
            WeeklyPlayerScore.draftable_asset_id.in_(lineup_asset_ids),
            WeeklyPlayerScore.week_id == week_id,
        )
        .all()
    )
    total = sum(s.points for s in scores)
    return TeamWeeklyScoreOut(team_id=team_id, week_id=week_id, total_points=total)


@router.get("/matchups/{matchup_id}", response_model=MatchupOut)
def get_matchup(matchup_id: int, db: Session = Depends(get_db)):
    matchup = db.get(WeeklyMatchup, matchup_id)
    if matchup is None:
        raise HTTPException(404, "Matchup not found")
    return matchup
