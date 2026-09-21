"""
The preseason schedule stage (see platform/schedule.py for the rules), the week-10 conference
championships, and the rankings / poll.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_can_manage_team, assert_commissioner, current_user, optional_user
from fantasy_sports.platform import rankings, schedule, scoring, season
from fantasy_sports.platform.db.models.core import MatchupRequest, Team, User, Week, WeeklyMatchup
from fantasy_sports.platform.db.session import get_db

router = APIRouter(tags=["schedule"])


class RequestCreate(BaseModel):
    week_id: int
    to_team_id: int
    offer_amount: int = 0


class RespondBody(BaseModel):
    accept: bool


def _can_manage(db: Session, user: User | None, team_id: int) -> bool:
    if user is None:
        return False
    try:
        assert_can_manage_team(db, user, team_id)
        return True
    except HTTPException:
        return False


def _req_out(db: Session, r: MatchupRequest) -> dict:
    return {"id": r.id, "week_id": r.week_id, "from_team_id": r.from_team_id, "from_team_name": db.get(Team, r.from_team_id).name,
            "to_team_id": r.to_team_id, "to_team_name": db.get(Team, r.to_team_id).name, "offer_amount": r.offer_amount,
            "status": r.status}


@router.get("/leagues/{league_id}/schedule/state")
def schedule_state(league_id: int, db: Session = Depends(get_db)):
    weeks = scoring.season_weeks(db, league_id)
    return {"status": schedule.stage_status(db, league_id),
            "has_schedule": db.query(WeeklyMatchup).filter_by(league_id=league_id).count() > 0,
            "regular_weeks": season.REGULAR_SEASON_WEEKS, "championship_week": season.CHAMPIONSHIP_WEEK,
            "weeks": [{"week_id": w.id, "week_number": w.week_number, "start_date": w.start_date,
                       "locked": scoring.is_locked(w)} for w in weeks if w.week_number <= season.CHAMPIONSHIP_WEEK]}


@router.get("/teams/{team_id}/schedule")
def team_schedule(team_id: int, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    """The team's regular season, week by week: its opponent, whether that game is an agreement or still the
    premade one, the teams it could ask instead, and (for the team's managers) its requests and offers."""
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found")
    mine = _can_manage(db, user, team_id)
    stage_open = schedule.stage_status(db, team.league_id) == "open"
    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=team.league_id)}
    weeks = []
    for w in schedule.regular_weeks(db, team.league_id):
        m = schedule._matchup(db, team.league_id, w.id, team_id)
        opp = None if m is None else (m.team_b_id if m.team_a_id == team_id else m.team_a_id)
        agreed = team_id in schedule.agreed_teams(db, team.league_id, w.id)
        editable = stage_open and not scoring.is_locked(w) and not agreed
        entry = {"week_id": w.id, "week_number": w.week_number, "start_date": w.start_date, "opponent_id": opp,
                 "opponent_name": names.get(opp), "agreement": agreed, "editable": editable and mine,
                 "available": [{"id": t.id, "name": t.name} for t in schedule.available_opponents(db, team, w.id)] if editable else []}
        if mine:
            entry["outgoing"] = next((_req_out(db, r) for r in db.query(MatchupRequest).filter_by(
                from_team_id=team_id, week_id=w.id, status="pending")), None)
            entry["incoming"] = [_req_out(db, r) for r in db.query(MatchupRequest).filter_by(
                to_team_id=team_id, week_id=w.id, status="pending")]
        weeks.append(entry)
    out = {"team_id": team_id, "status": "open" if stage_open else "closed", "weeks": weeks}
    if mine:
        out["cash_available"] = schedule.cash_available(db, team)
    return out


@router.post("/teams/{team_id}/schedule/requests")
def create_request(team_id: int, payload: RequestCreate, db: Session = Depends(get_db), user: User = Depends(current_user)):
    team = assert_can_manage_team(db, user, team_id)
    try:
        req = schedule.request_matchup(db, team, payload.week_id, payload.to_team_id, payload.offer_amount)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return _req_out(db, req)


def _request(db: Session, request_id: int) -> MatchupRequest:
    req = db.get(MatchupRequest, request_id)
    if req is None:
        raise HTTPException(404, "Request not found")
    return req


@router.delete("/schedule/requests/{request_id}")
def withdraw_request(request_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    req = _request(db, request_id)
    assert_can_manage_team(db, user, req.from_team_id)
    try:
        schedule.withdraw_request(db, req)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return _req_out(db, req)


@router.post("/schedule/requests/{request_id}/respond")
def respond_to_request(request_id: int, payload: RespondBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """The receiving team accepts or declines. Accepting moves the cash and rewires the two teams' weeks."""
    req = _request(db, request_id)
    assert_can_manage_team(db, user, req.to_team_id)
    try:
        schedule.respond(db, req, payload.accept)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return _req_out(db, req)


@router.post("/leagues/{league_id}/schedule/close")
def close_schedule_stage(league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Commissioner action: AI teams answer their requests, everything else still open is declined, and the stage closes."""
    assert_commissioner(db, user, league_id)
    if schedule.stage_status(db, league_id) != "open":
        raise HTTPException(400, "The schedule stage is already closed")
    result = schedule.close_stage(db, league_id)
    db.commit()
    return result


@router.get("/leagues/{league_id}/championships")
def league_championships(league_id: int, db: Session = Depends(get_db)):
    """Week 10: each conference's #1 v #2, #3 v #4, ...; an odd conference's worst team has a bye and its season is over.
    Set once every regular-season week has been scored."""
    c = scoring.championships(db, league_id)
    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=league_id)}
    return {"ready": c["ready"], "week_id": c["week_id"],
            "games": [{"conference_id": conf, "team_a_id": a, "team_a_name": names[a], "team_b_id": b, "team_b_name": names[b]}
                      for conf, a, b in c["games"]],
            "byes": [{"team_id": t, "team_name": names[t]} for t in c["byes"]]}


@router.get("/leagues/{league_id}/rankings")
def league_rankings(league_id: int, db: Session = Depends(get_db)):
    """The simple ranking (by projected remaining EPA credit) and the AI voter poll built from it."""
    if not db.query(Team).filter_by(league_id=league_id).count():
        raise HTTPException(404, "League not found or has no teams")
    return rankings.poll(db, league_id)
