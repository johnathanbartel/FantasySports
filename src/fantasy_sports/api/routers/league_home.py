"""
Endpoints behind the league home page's tabs (web/static/league.html):
overview (Home / Teams / Roster), free agency + the transactions log
(Transactions), and player stats (Stats). Standings reuse GET /leagues/{id}/standings.
"""

import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload, selectinload

from fantasy_sports.api.auth import assert_can_manage_team, assert_commissioner, current_user, optional_user
from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
from fantasy_sports.api.routers.recruiting import DraftStatus, _status
from fantasy_sports.platform import calendar, entitlements, free_agency, rankings, season
from fantasy_sports.platform.auction.constants import STARTING_BUDGET_BY_PROGRAM_SIZE
from fantasy_sports.platform.db.models.core import (
    Conference,
    DefenseGroup,
    DraftableAsset,
    FreeAgencyTransaction,
    League,
    LeagueUniverse,
    RealTeam,
    RecruitingAward,
    RosterEntry,
    Team,
    User,
    Week,
)
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.platform.recruiting.pricing import season_points_by_asset
from fantasy_sports.sports.football.models import WeeklyPlayerScore

router = APIRouter(tags=["league-home"])

# Eager-load what draftable_asset_display_name / the school lookup touch, so listing a pool of
# thousands of players isn't thousands of tiny queries.
_EAGER = (joinedload(DraftableAsset.player), joinedload(DraftableAsset.defense_group))


class ConfigOut(BaseModel):
    current_season: int | None     # the season lineups are set for
    last_season: int | None        # the last completed season - the year the board's fantasy points are from
    season_weeks: int
    playoff_weeks: int


@router.get("/config", response_model=ConfigOut)
def config(db: Session = Depends(get_db)):
    """Season facts the pages label things with, read from the data."""
    return ConfigOut(current_season=calendar.current_season_year(db), last_season=calendar.last_points_season(db),
                     season_weeks=season.SEASON_WEEKS, playoff_weeks=season.PLAYOFF_WEEKS)


# ------------------------------------------------------------------ overview
class TeamSummary(BaseModel):
    id: int
    name: str
    program_size: str
    is_ai_owned: bool
    owner_user_id: int | None
    owner_name: str | None
    conference_id: int | None
    conference_name: str | None
    season_goal: str
    weekly_budget: int
    roster_count: int
    open_spots: int
    poll_rank: int | None = None      # rank in the current poll, if the team is ranked


class OverviewOut(BaseModel):
    league_id: int
    name: str
    draft_type: str
    size: int
    roster_size: int
    commissioner_user_id: int
    commissioner_name: str | None
    season_weeks: int
    playoff_weeks: int
    draft: DraftStatus
    free_agency_open: bool
    ai_teams_with_open_spots: int
    teams: list[TeamSummary]


@router.get("/leagues/{league_id}/overview", response_model=OverviewOut)
def league_overview(league_id: int, db: Session = Depends(get_db)):
    """Everything the Home, Teams and Roster tabs need in one call."""
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    draft = _status(db, league_id)
    users = {u.id: u.display_name for u in db.query(User)}
    confs = {c.id: c.name for c in db.query(Conference).filter_by(league_id=league_id)}
    counts: dict[int, int] = {}
    team_ids = [t.id for t in db.query(Team).filter_by(league_id=league_id)]
    for r in db.query(RosterEntry).filter(RosterEntry.team_id.in_(team_ids or [0])):
        counts[r.team_id] = counts.get(r.team_id, 0) + 1
    poll_ranks = rankings.poll_ranks(db, league_id) if team_ids else {}
    teams = [
        TeamSummary(
            id=t.id, name=t.name, program_size=t.program_size, is_ai_owned=t.is_ai_owned,
            owner_user_id=t.owner_user_id, owner_name=users.get(t.owner_user_id), conference_id=t.conference_id,
            conference_name=confs.get(t.conference_id), season_goal=t.season_goal,
            weekly_budget=STARTING_BUDGET_BY_PROGRAM_SIZE[t.program_size], roster_count=counts.get(t.id, 0),
            open_spots=max(0, league.draft_rounds - counts.get(t.id, 0)), poll_rank=poll_ranks.get(t.id),
        )
        for t in db.query(Team).filter_by(league_id=league_id).order_by(Team.id)
    ]
    return OverviewOut(
        league_id=league.id, name=league.name, draft_type=league.draft_type, size=league.size,
        roster_size=league.draft_rounds, commissioner_user_id=league.commissioner_user_id,
        commissioner_name=users.get(league.commissioner_user_id), season_weeks=season.SEASON_WEEKS,
        playoff_weeks=season.PLAYOFF_WEEKS, draft=draft, free_agency_open=free_agency.is_open(db, league_id),
        ai_teams_with_open_spots=sum(1 for t in teams if t.is_ai_owned and t.open_spots > 0), teams=teams,
    )


# ------------------------------------------------------------------ free agency
class FreeAgentOut(BaseModel):
    draftable_asset_id: int
    display_name: str
    position: str | None
    real_team: str | None
    last_season_points: float | None
    asking_price: int   # PROVISIONAL rule - see platform/free_agency.py


class FreeAgentsOut(BaseModel):
    open: bool
    price_note: str
    players: list[FreeAgentOut]


def _school_names(db: Session) -> dict[int, str]:
    return {t.id: t.name for t in db.query(RealTeam)}


def _school(a: DraftableAsset, schools: dict[int, str]) -> str | None:
    return schools.get(a.player.real_team_id if a.player else a.defense_group.real_team_id)


@router.get("/leagues/{league_id}/free-agents", response_model=FreeAgentsOut)
def free_agents(league_id: int, db: Session = Depends(get_db)):
    """Players in the league's pool that no team rosters, with asking prices.
    Free agency opens when the recruiting draft ends."""
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    is_open = free_agency.is_open(db, league_id)
    note = ("Asking prices are provisional: each player's recruiting price, revealed once recruiting ends."
            if is_open else "Free agency opens when the recruiting draft ends.")
    if not is_open:
        return FreeAgentsOut(open=False, price_note=note, players=[])
    prices = free_agency.asking_prices(db, league)
    taken = free_agency.rostered_ids(db, league_id)
    schools = _school_names(db)
    points = season_points_by_asset(db)
    assets = db.query(DraftableAsset).options(*_EAGER).filter(
        DraftableAsset.id.in_([a for a in prices if a not in taken] or [0])).all()
    return FreeAgentsOut(open=True, price_note=note, players=[
        FreeAgentOut(
            draftable_asset_id=a.id, display_name=draftable_asset_display_name(a), position=draftable_asset_position(a),
            real_team=_school(a, schools), last_season_points=points.get(a.id), asking_price=prices[a.id])
        for a in assets
    ])


class FaSummaryOut(BaseModel):
    team_id: int
    roster_count: int
    roster_size: int
    open_spots: int
    # Private to the team's owner (and a commissioner-mode commissioner acting for an AI team):
    fa_money: int | None
    roster_cost: int | None
    fa_spent: int | None


@router.get("/teams/{team_id}/free-agency/summary", response_model=FaSummaryOut)
def fa_summary(team_id: int, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found")
    league = db.get(League, team.league_id)
    n = free_agency.roster_count(db, team_id)
    can_see = user is not None and (
        (not team.is_ai_owned and team.owner_user_id == user.id)
        or (team.is_ai_owned and league.commissioner_user_id == user.id
            and entitlements.has_feature(db, user, entitlements.COMMISSIONER_MODE))
    )
    money = free_agency.team_money(db, team) if can_see else None
    return FaSummaryOut(
        team_id=team_id, roster_count=n, roster_size=league.draft_rounds, open_spots=max(0, league.draft_rounds - n),
        fa_money=money["fa_money"] if money else None, roster_cost=money["roster_cost"] if money else None,
        fa_spent=money["fa_spent"] if money else None)


class FaActionRequest(BaseModel):
    draftable_asset_id: int


class TransactionOut(BaseModel):
    at: datetime.datetime
    team_id: int
    kind: str                  # "recruited" / "added" / "dropped"
    draftable_asset_id: int
    display_name: str
    position: str | None
    real_team: str | None
    price: int | None          # a free-agent signing's price. Recruiting bid amounts are never shown.
    reason: str | None         # "user" / "commissioner" / "ai_fill" for free agency
    week: int | None           # the recruiting week, for "recruited"


def _txn_out(db: Session, txn: FreeAgencyTransaction, schools: dict[int, str]) -> TransactionOut:
    a = txn.draftable_asset
    return TransactionOut(
        at=txn.created_at, team_id=txn.team_id, kind="added" if txn.kind == "add" else "dropped",
        draftable_asset_id=a.id, display_name=draftable_asset_display_name(a), position=draftable_asset_position(a),
        real_team=_school(a, schools), price=txn.price if txn.kind == "add" else None, reason=txn.reason, week=None)


@router.post("/teams/{team_id}/free-agency/add", response_model=TransactionOut)
def fa_add(team_id: int, payload: FaActionRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Signs a free agent. The caller must own the team (or, for an AI team, be
    its commissioner with commissioner mode). Needs an open roster spot and
    enough free-agency money."""
    team = assert_can_manage_team(db, user, team_id)
    reason = free_agency.COMMISSIONER if team.is_ai_owned else free_agency.USER
    try:
        txn = free_agency.add_player(db, team, payload.draftable_asset_id, reason)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return _txn_out(db, txn, _school_names(db))


@router.post("/teams/{team_id}/free-agency/drop", response_model=TransactionOut)
def fa_drop(team_id: int, payload: FaActionRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Releases a player to the free-agent pool (no refund)."""
    team = assert_can_manage_team(db, user, team_id)
    reason = free_agency.COMMISSIONER if team.is_ai_owned else free_agency.USER
    try:
        txn = free_agency.drop_player(db, team, payload.draftable_asset_id, reason)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return _txn_out(db, txn, _school_names(db))


@router.post("/leagues/{league_id}/free-agency/ai-fill")
def run_ai_fill(league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Commissioner action: makes every AI team with open roster spots fill them
    through free agency. This already happens automatically when recruiting
    closes; this covers a league whose draft closed before that rule existed.
    Idempotent."""
    assert_commissioner(db, user, league_id)
    if not free_agency.is_open(db, league_id):
        raise HTTPException(400, "Free agency opens when the recruiting draft ends")
    signed = free_agency.ai_fill_rosters(db, league_id)
    db.commit()
    return {"signed": [{"team_id": t, "players": n} for t, n in signed.items()], "total": sum(signed.values())}


@router.get("/leagues/{league_id}/transactions", response_model=list[TransactionOut])
def transactions(league_id: int, limit: int = 300, db: Session = Depends(get_db)):
    """The league's transaction log, newest first: recruiting awards (who
    recruited whom, never what they bid), free-agent signings and releases."""
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    schools = _school_names(db)
    out = [_txn_out(db, t, schools) for t in
           db.query(FreeAgencyTransaction).filter_by(league_id=league_id).order_by(FreeAgencyTransaction.id.desc()).limit(limit)]
    from fantasy_sports.platform.db.models.core import RecruitingDraft
    draft = db.query(RecruitingDraft).filter_by(league_id=league_id).first()
    if draft is not None:
        for a in db.query(RecruitingAward).filter_by(draft_id=draft.id):
            asset = a.draftable_asset
            out.append(TransactionOut(
                at=a.awarded_at, team_id=a.team_id, kind="recruited", draftable_asset_id=asset.id,
                display_name=draftable_asset_display_name(asset), position=draftable_asset_position(asset),
                real_team=_school(asset, schools), price=None, reason=None, week=a.period_number))
    # newest first; within one moment (the AI's fill) keep a stable, readable order
    out.sort(key=lambda t: (t.at, t.team_id), reverse=True)
    return out[:limit]


# ------------------------------------------------------------------ stats
class StatRow(BaseModel):
    draftable_asset_id: int
    display_name: str
    position: str | None
    real_team: str | None
    fantasy_team_id: int | None       # the team that rosters him in this league, or None for a free agent
    last_season_points: float | None         # last season, ESPN standard scoring
    weekly: dict[int, float]          # week number -> the platform's own (EPA) fantasy points that week


@router.get("/leagues/{league_id}/stats", response_model=list[StatRow])
def league_stats(league_id: int, db: Session = Depends(get_db)):
    """Every player and defense group in the league's pool with last season's
    ESPN-standard points and the platform's weekly points for the weeks it has
    scored (2024 weeks 1-3 in the seeded data)."""
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    ids = free_agency.pool_ids(db, league_id)
    owner = {}
    team_ids = [t.id for t in db.query(Team).filter_by(league_id=league_id)]
    for r in db.query(RosterEntry).filter(RosterEntry.team_id.in_(team_ids or [0])):
        owner[r.draftable_asset_id] = r.team_id
    universe_ids = [lu.universe_id for lu in db.query(LeagueUniverse).filter_by(league_id=league_id)]
    current = calendar.current_season_year(db, universe_ids)
    week_no = {w.id: w.week_number for w in db.query(Week).filter(Week.season_year == current)}   # this season's weeks only
    weekly: dict[int, dict[int, float]] = {}
    for s in db.query(WeeklyPlayerScore).filter(WeeklyPlayerScore.week_id.in_(list(week_no) or [0])):
        weekly.setdefault(s.draftable_asset_id, {})[week_no[s.week_id]] = round(s.points, 1)
    points = season_points_by_asset(db)
    schools = _school_names(db)
    return [
        StatRow(
            draftable_asset_id=a.id, display_name=draftable_asset_display_name(a), position=draftable_asset_position(a),
            real_team=_school(a, schools), fantasy_team_id=owner.get(a.id), last_season_points=points.get(a.id),
            weekly=weekly.get(a.id, {}))
        for a in db.query(DraftableAsset).options(*_EAGER).filter(DraftableAsset.id.in_(ids or [0]))
    ]
