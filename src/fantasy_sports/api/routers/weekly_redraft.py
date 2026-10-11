"""
Weekly salary-cap redraft endpoints (rules: platform/weekly_redraft.py). Commissioner routes configure the mode, create
contests and publish salaries; team owners save and submit rosters. No currency or money flow exists anywhere here.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_commissioner, assert_owns_team, current_user
from fantasy_sports.api.schemas import (
    RedraftContestCreateRequest, RedraftContestOut, RedraftContestViewOut, RedraftEnableRequest, RedraftEntryOut,
    RedraftLeagueOut, RedraftPairingOut, RedraftRosterRequest, RedraftSalariesRequest, RedraftSlotOut,
)
from fantasy_sports.platform import weekly_redraft as wr
from fantasy_sports.platform.db.models.core import (
    League, Team, User, WeeklyContest, WeeklyContestEntry, WeeklyContestPairing, WeeklyPlayerSalary,
)
from fantasy_sports.platform.db.session import get_db

router = APIRouter(tags=["weekly-redraft"])

_NOTE = ("Weekly redraft uses a fixed salary cap. The rule that adjusts the cap from the previous week's finish is an "
         "open product decision and is not applied.")
_STATUS = {"NOT_FOUND": 404, "LOCKED": 409, "ALREADY_SUBMITTED": 409, "DUPLICATE": 409, "HAS_ENTRIES": 409,
           "HAS_SERIES": 409, "CAP_LOCKED": 409}


def _fail(e: wr.RedraftError) -> HTTPException:
    return HTTPException(_STATUS.get(e.code, 400), {"code": e.code, "message": str(e)})


def _league(db: Session, league_id: int) -> League:
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "League not found"})
    return league


def _contest(db: Session, contest_id: int) -> WeeklyContest:
    c = db.get(WeeklyContest, contest_id)
    if c is None:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Contest not found"})
    wr.sync(c, wr.utcnow())
    return c


def _entry_out(e: WeeklyContestEntry) -> RedraftEntryOut:
    return RedraftEntryOut(
        team_id=e.team_id, status=e.status, salary_total=e.salary_total, cap=e.cap, cap_remaining=e.cap - e.salary_total,
        submitted_at=e.submitted_at,
        slots=[RedraftSlotOut(slot_code=s.slot_code, slot_index=s.slot_index, draftable_asset_id=s.draftable_asset_id,
                              player_name=s.draftable_asset.player.full_name, position=s.draftable_asset.player.position,
                              salary=s.salary) for s in e.slots])


def _contest_out(db: Session, c: WeeklyContest) -> RedraftContestOut:
    st = wr.settings_of(db, c.league_id)
    entries = {e.team_id: e.status for e in db.query(WeeklyContestEntry).filter_by(contest_id=c.id)}
    pairs = db.query(WeeklyContestPairing).filter_by(contest_id=c.id).order_by(WeeklyContestPairing.id).all()
    return RedraftContestOut(
        id=c.id, league_id=c.league_id, week_index=c.week_index, start_date=c.start_date, end_date=c.end_date,
        lock_at=c.lock_at, status=c.status, cap=c.cap_applied, cap_policy=st.cap_policy if st else "FIXED",
        salaries_published=db.query(WeeklyPlayerSalary).filter_by(contest_id=c.id).count(),
        pairings=[RedraftPairingOut(team_a_id=p.team_a_id, team_b_id=p.team_b_id,
                                    team_a_status=entries.get(p.team_a_id, "NO_ENTRY"),
                                    team_b_status=entries.get(p.team_b_id, "NO_ENTRY")) for p in pairs])


@router.post("/leagues/{league_id}/weekly-redraft", response_model=RedraftLeagueOut)
def enable_weekly_redraft(league_id: int, payload: RedraftEnableRequest, db: Session = Depends(get_db),
                          user: User = Depends(current_user)):
    """Commissioner only. Puts an NHL league into weekly-redraft mode with a FIXED cap (a separate mode from series)."""
    league = assert_commissioner(db, user, league_id)
    try:
        wr.enable(db, league, payload.base_cap)
        db.commit()
    except wr.RedraftError as e:
        db.rollback()
        raise _fail(e)
    return _league_out(db, league)


def _league_out(db: Session, league: League) -> RedraftLeagueOut:
    st = wr.settings_of(db, league.id)
    contests = db.query(WeeklyContest).filter_by(league_id=league.id).order_by(WeeklyContest.week_index).all()
    now = wr.utcnow()
    for c in contests:
        wr.sync(c, now)
    db.commit()
    return RedraftLeagueOut(league_id=league.id, league_mode=league.league_mode, base_cap=st.base_cap if st else None,
                            cap_policy=st.cap_policy if st else None, contests=[_contest_out(db, c) for c in contests], note=_NOTE)


@router.get("/leagues/{league_id}/weekly-redraft", response_model=RedraftLeagueOut)
def read_weekly_redraft(league_id: int, db: Session = Depends(get_db)):
    return _league_out(db, _league(db, league_id))


@router.post("/leagues/{league_id}/weekly-redraft/contests", response_model=RedraftContestOut)
def create_contest(league_id: int, payload: RedraftContestCreateRequest, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    league = assert_commissioner(db, user, league_id)
    try:
        c = wr.create_contest(db, league, payload.start_date, payload.end_date, payload.lock_at)
        db.commit()
    except wr.RedraftError as e:
        db.rollback()
        raise _fail(e)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, {"code": "DUPLICATE", "message": "That contest already exists"})
    return _contest_out(db, c)


@router.post("/weekly-redraft/contests/{contest_id}/salaries")
def publish_salaries(contest_id: int, payload: RedraftSalariesRequest, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """Commissioner only. Replaces the contest's price list (before any roster is saved, and before the lock)."""
    c = _contest(db, contest_id)
    assert_commissioner(db, user, c.league_id)
    try:
        n = wr.set_salaries(db, c, payload.salaries, wr.utcnow())
        db.commit()
    except wr.RedraftError as e:
        db.rollback()
        raise _fail(e)
    return {"contest_id": c.id, "salaries_published": n}


@router.get("/weekly-redraft/contests/{contest_id}", response_model=RedraftContestViewOut)
def read_contest(contest_id: int, team_id: int | None = None, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    """The contest plus, for `team_id` (which the caller must own), their roster and opponent. The opponent's roster
    stays hidden until the contest locks."""
    c = _contest(db, contest_id)
    db.commit()
    mine = opp_entry = None
    opp_id = None
    if team_id is not None:
        team = assert_owns_team(db, user, team_id)
        if team.league_id != c.league_id:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "Team is not in this league"})
        opp_id = wr.opponent_of(db, c, team_id)
        e = wr.get_entry(db, c, team_id)
        mine = _entry_out(e) if e else None
        if opp_id is not None and c.status == "LOCKED":
            oe = wr.get_entry(db, c, opp_id)
            opp_entry = _entry_out(oe) if oe is not None and oe.status == "SUBMITTED" else None
    return RedraftContestViewOut(contest=_contest_out(db, c), opponent_team_id=opp_id, my_entry=mine, opponent_entry=opp_entry)


@router.put("/weekly-redraft/contests/{contest_id}/teams/{team_id}/roster", response_model=RedraftEntryOut)
def save_roster(contest_id: int, team_id: int, payload: RedraftRosterRequest, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    """Save (replace) the team's draft roster. Owner only; allowed until the lock; rejected for cap overflow, a repeated
    player, a wrong position, an unpriced player, or an already-submitted roster."""
    c = _contest(db, contest_id)
    team = assert_owns_team(db, user, team_id)
    if team.league_id != c.league_id:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Team is not in this league"})
    try:
        entry = wr.save_entry(db, c, team_id, payload.slots, wr.utcnow())
        db.commit()
    except wr.RedraftError as e:
        db.rollback()
        db.commit()   # keep a lazily applied lock
        raise _fail(e)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, {"code": "CONFLICT", "message": "The roster changed at the same time; try again"})
    return _entry_out(entry)


@router.post("/weekly-redraft/contests/{contest_id}/teams/{team_id}/submit", response_model=RedraftEntryOut)
def submit_roster(contest_id: int, team_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Make the saved roster final. Owner only; idempotent."""
    c = _contest(db, contest_id)
    team = assert_owns_team(db, user, team_id)
    if team.league_id != c.league_id:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Team is not in this league"})
    try:
        entry = wr.submit_entry(db, c, team_id, wr.utcnow())
        db.commit()
    except wr.RedraftError as e:
        db.rollback()
        db.commit()
        raise _fail(e)
    return _entry_out(entry)
