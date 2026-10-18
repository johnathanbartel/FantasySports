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
from fantasy_sports.api.helpers import draftable_asset_age, draftable_asset_display_name, draftable_asset_position
from fantasy_sports.api.routers.recruiting import DraftStatus, _status
from fantasy_sports.platform import calendar, commissioner, entitlements, free_agency, game_lock, pick_trading, rankings, season, waivers
from fantasy_sports.platform.auction.constants import STARTING_BUDGET_BY_PROGRAM_SIZE
from fantasy_sports.platform.ai import ai_roster
from fantasy_sports.platform.league_features import draft_state, is_college, roster_limit
from fantasy_sports.platform.draft_stats import current_season_stats, stats_by_asset
from fantasy_sports.platform.player_rates import rates_by_asset
from fantasy_sports.platform.db.models.core import (
    Conference,
    DefenseGroup,
    Division,
    DraftableAsset,
    FreeAgencyTransaction,
    League,
    LeagueUniverse,
    RealTeam,
    RecruitingAward,
    RosterEntry,
    Team,
    Universe,
    User,
    Week,
)
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.platform.recruiting.pricing import season_points_by_asset
from fantasy_sports.sports.football.models import WeeklyPlayerScore

router = APIRouter(tags=["league-home"])

def _tick(db: Session, league_id: int) -> None:
    """Waiver claims whose 24 hours are up, then the AI teams' due roster decisions (so they see who has cleared)."""
    waivers.process(db, league_id)
    ai_roster.tick(db, league_id)       # platform/ai/ai_roster.py


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
    owner_email: str | None = None
    conference_id: int | None
    conference_name: str | None
    division_id: int | None = None
    division_name: str | None = None
    season_goal: str
    weekly_budget: int
    roster_count: int
    open_spots: int
    poll_rank: int | None = None      # rank in the current poll, if the team is ranked
    is_claimed: bool = False
    invite_status: str = "ai"         # "ai", "pending_claim", "claimed"
    invite_url: str | None = None


class OverviewOut(BaseModel):
    league_id: int
    name: str
    draft_type: str
    universe_codes: list[str] = []
    slug: str | None = None
    logo_url: str | None = None
    size: int
    roster_size: int
    commissioner_user_id: int
    commissioner_name: str | None
    co_commissioner_user_ids: list[int] = []     # same powers as the commissioner, bar the role's own (platform/commissioner.py)
    season_weeks: int
    playoff_weeks: int
    draft: DraftStatus
    free_agency_open: bool
    ai_teams_with_open_spots: int
    teams: list[TeamSummary]
    is_college: bool = False
    draft_state: str = "not_started"     # not_started / in_progress / complete, whatever kind of draft the league runs
    draft_scheduled_at: datetime.datetime | None = None
    has_rankings: bool = False          # College Football / College Basketball only, to start
    teams_set_schedule: bool = False    # likewise: only college teams negotiate their own schedules
    league_format: str = "traditional_redraft"
    pick_trading_on: bool = False       # draft picks can be traded in this league (the Trades screen lists them)
    offseason: bool = False
    keepers_finalized: bool = True
    keeper_deadline: datetime.datetime | None = None    # naive UTC; Keeper leagues, shown on the roster page


@router.get("/leagues/{league_id}/overview", response_model=OverviewOut)
def league_overview(league_id: int, db: Session = Depends(get_db)):
    """Everything the Home, Teams and Roster tabs need in one call."""
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    _tick(db, league_id)       # AI teams' due roster decisions (platform/ai/ai_roster.py)
    db.commit()
    draft = _status(db, league_id)
    user_map = {u.id: u for u in db.query(User)}
    confs = {c.id: c.name for c in db.query(Conference).filter_by(league_id=league_id)}
    divs = {d.id: d.name for d in db.query(Division).filter_by(league_id=league_id)}
    counts: dict[int, int] = {}
    team_ids = [t.id for t in db.query(Team).filter_by(league_id=league_id)]
    for r in db.query(RosterEntry).filter(RosterEntry.team_id.in_(team_ids or [0])):
        counts[r.team_id] = counts.get(r.team_id, 0) + 1
    college = is_college(db, league_id)
    limit = roster_limit(db, league)
    poll_ranks = rankings.poll_ranks(db, league_id) if team_ids and college else {}
    teams = []
    for t in db.query(Team).filter_by(league_id=league_id).order_by(Team.id):
        u = user_map.get(t.owner_user_id) if t.owner_user_id else None
        if t.is_ai_owned or not t.owner_user_id:
            is_claimed = False
            invite_status = "ai"
            invite_url = None
        elif t.owner_user_id == league.commissioner_user_id:
            is_claimed = True
            invite_status = "claimed"
            invite_url = None
        elif u and u.password_hash is not None:
            is_claimed = True
            invite_status = "claimed"
            invite_url = None
        else:
            is_claimed = False
            invite_status = "pending_claim"
            invite_url = f"/static/join-league.html?league_id={league.id}&team_id={t.id}&email={u.email if u else ''}"

        teams.append(TeamSummary(
            id=t.id, name=t.name, program_size=t.program_size, is_ai_owned=t.is_ai_owned,
            owner_user_id=t.owner_user_id,
            owner_name=u.display_name if u else None,
            owner_email=u.email if u else None,
            conference_id=t.conference_id,
            conference_name=confs.get(t.conference_id),
            division_id=t.division_id,
            division_name=divs.get(t.division_id),
            season_goal=t.season_goal,
            weekly_budget=STARTING_BUDGET_BY_PROGRAM_SIZE[t.program_size], roster_count=counts.get(t.id, 0),
            open_spots=max(0, limit - counts.get(t.id, 0)), poll_rank=poll_ranks.get(t.id),
            is_claimed=is_claimed,
            invite_status=invite_status,
            invite_url=invite_url,
        ))
    universe_codes = [
        u.code for u in db.query(Universe).join(LeagueUniverse, LeagueUniverse.universe_id == Universe.id)
        .filter(LeagueUniverse.league_id == league_id).order_by(Universe.code)
    ]
    return OverviewOut(
        league_id=league.id, name=league.name, slug=league.slug, logo_url=league.logo_url, draft_type=league.draft_type, universe_codes=universe_codes, size=league.size,
        roster_size=limit, commissioner_user_id=league.commissioner_user_id,
        commissioner_name=user_map[league.commissioner_user_id].display_name if league.commissioner_user_id in user_map else None,
        co_commissioner_user_ids=sorted(commissioner.co_commissioner_ids(db, league_id)),
        season_weeks=season.SEASON_WEEKS,
        playoff_weeks=season.PLAYOFF_WEEKS, draft=draft, free_agency_open=free_agency.is_open(db, league_id),
        ai_teams_with_open_spots=sum(1 for t in teams if t.is_ai_owned and t.open_spots > 0), teams=teams,
        is_college=college, draft_state=draft_state(db, league_id), draft_scheduled_at=league.draft_scheduled_at,
        has_rankings=college, teams_set_schedule=college, league_format=league.league_format, pick_trading_on=pick_trading.enabled(league), offseason=league.offseason,
        keepers_finalized=league.keepers_finalized, keeper_deadline=league.keeper_deadline,
    )


# ------------------------------------------------------------------ free agency
class FreeAgentOut(BaseModel):
    draftable_asset_id: int
    display_name: str
    position: str | None
    real_team: str | None
    age: int | None = None
    last_season_points: float | None
    points_per_game: float | None = None
    this_season_points: float | None = None
    this_season_games: int | None = None
    asking_price: int   # PROVISIONAL rule - see platform/free_agency.py
    waiver_until: datetime.datetime | None = None   # on waivers (claim only) until then, naive UTC


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
    _tick(db, league_id)
    db.commit()
    is_open = free_agency.is_open(db, league_id)
    note = ("Asking prices are provisional: each player's recruiting price, revealed once recruiting ends."
            if is_open and is_college(db, league_id) else "Signing is free: any free agent can fill an open roster spot."
            if is_open else "Free agency opens when the draft ends.")
    if not is_open:
        return FreeAgentsOut(open=False, price_note=note, players=[])
    prices = free_agency.asking_prices(db, league)
    taken = free_agency.rostered_ids(db, league_id)
    schools = _school_names(db)
    free_ids = [a for a in prices if a not in taken]
    on_waivers = waivers.waiver_until(db, league_id, datetime.datetime.utcnow())
    stats = stats_by_asset(db, free_ids)
    rates = rates_by_asset(db, free_ids)
    current = current_season_stats(db, free_ids)
    assets = db.query(DraftableAsset).options(*_EAGER).filter(DraftableAsset.id.in_(free_ids or [0])).all()
    return FreeAgentsOut(open=True, price_note=note, players=[
        FreeAgentOut(
            draftable_asset_id=a.id, display_name=draftable_asset_display_name(a), position=draftable_asset_position(a),
            real_team=_school(a, schools), age=draftable_asset_age(a), last_season_points=stats.get(a.id, {}).get("points"),
            points_per_game=rates.get(a.id, {}).get("points_per_game"),
            this_season_points=current.get(a.id, {}).get("points"), this_season_games=current.get(a.id, {}).get("games"),
            asking_price=prices[a.id], waiver_until=on_waivers.get(a.id))
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
    limit = roster_limit(db, league)
    can_see = user is not None and (
        (not team.is_ai_owned and team.owner_user_id == user.id)
        or commissioner.is_commissioner(db, league, user.id)     # a commissioner sees every team's balance
    )
    money = free_agency.team_money(db, team) if can_see and is_college(db, team.league_id) else None
    return FaSummaryOut(
        team_id=team_id, roster_count=n, roster_size=limit, open_spots=max(0, limit - n),
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
    waivers.process(db, team.league_id)
    try:
        game_lock.assert_unlocked(db, team.league_id, [payload.draftable_asset_id])
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
        game_lock.assert_unlocked(db, team.league_id, [payload.draftable_asset_id])
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
    signed = (free_agency.ai_fill_rosters(db, league_id) if is_college(db, league_id)
              else free_agency.ai_fill_snake(db, league_id))
    db.commit()
    return {"signed": [{"team_id": t, "players": n} for t, n in signed.items()], "total": sum(signed.values())}


# ------------------------------------------------------------------ waivers
class WaiverOrderRow(BaseModel):
    position: int
    team_id: int
    team_name: str
    goal_diff: int | None = None       # the differential the order was built on (None under the draft fallback)
    moved_back: bool = False           # won a claim since the reset


class WaiverPlayerOut(BaseModel):
    draftable_asset_id: int
    display_name: str
    position: str | None
    real_team: str | None
    until: datetime.datetime           # naive UTC
    dropped_by_team_id: int | None
    claimed: bool = False              # the requested team has a pending claim


class WaiversOut(BaseModel):
    basis: str                         # "goal_diff" / "draft" / "commissioner"
    basis_note: str
    reset_at: datetime.datetime        # the last reset, naive UTC
    next_reset_at: datetime.datetime
    order: list[WaiverOrderRow]
    players: list[WaiverPlayerOut]


@router.get("/leagues/{league_id}/waivers", response_model=WaiversOut)
def league_waivers(league_id: int, team_id: int | None = None, db: Session = Depends(get_db)):
    """The waiver order, when it next resets, and the players on waivers now. team_id marks that team's pending claims."""
    if db.get(League, league_id) is None:
        raise HTTPException(404, "League not found")
    now = datetime.datetime.utcnow()
    _tick(db, league_id)
    db.commit()
    order = waivers.waiver_order(db, league_id, now)
    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=league_id)}
    note = ("Set by hand by a commissioner."
            if order.basis == waivers.BASIS_COMMISSIONER else
            "Reverse of goal differential through the last fully completed week: the worst differential claims first."
            if order.basis == waivers.BASIS_GOAL_DIFF else
            "Reverse of the first round of the draft, until a full week of games has been completed.")
    note += " It resets at midnight Pacific every Saturday night, and a team that wins a claim drops to the back until then."
    until = waivers.waiver_until(db, league_id, now)
    mine = waivers.pending_claims(db, team_id) if team_id is not None else set()
    schools = _school_names(db)
    assets = db.query(DraftableAsset).options(*_EAGER).filter(DraftableAsset.id.in_(list(until) or [0])).all()
    by = waivers.dropped_by(db, league_id, list(until))
    return WaiversOut(
        basis=order.basis, basis_note=note, reset_at=order.reset_at, next_reset_at=waivers.next_reset(now),
        order=[WaiverOrderRow(position=i, team_id=t, team_name=names.get(t, str(t)), goal_diff=order.goal_diff.get(t),
                              moved_back=t in order.moved_back) for i, t in enumerate(order.teams, start=1)],
        players=sorted((WaiverPlayerOut(
            draftable_asset_id=a.id, display_name=draftable_asset_display_name(a), position=draftable_asset_position(a),
            real_team=_school(a, schools), until=until[a.id], dropped_by_team_id=by.get(a.id), claimed=a.id in mine)
            for a in assets), key=lambda p: (p.until, p.display_name)))


@router.post("/teams/{team_id}/waivers/claim")
def waiver_claim(team_id: int, payload: FaActionRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Claims a player on waivers. The claim is filled when his 24 hours end, if this team is first in the waiver order
    among the teams that claimed him and still has a roster spot (and money, in a college league)."""
    team = assert_can_manage_team(db, user, team_id)
    try:
        game_lock.assert_unlocked(db, team.league_id, [payload.draftable_asset_id])
        claim = waivers.place_claim(db, team, payload.draftable_asset_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return {"draftable_asset_id": claim.draftable_asset_id, "available_at": claim.available_at}


@router.post("/teams/{team_id}/waivers/cancel")
def waiver_cancel(team_id: int, payload: FaActionRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    team = assert_can_manage_team(db, user, team_id)
    try:
        waivers.cancel_claim(db, team, payload.draftable_asset_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return {"cancelled": payload.draftable_asset_id}


@router.get("/leagues/{league_id}/transactions", response_model=list[TransactionOut])
def transactions(league_id: int, limit: int = 300, db: Session = Depends(get_db)):
    """The league's transaction log, newest first: recruiting awards (who
    recruited whom, never what they bid), free-agent signings and releases."""
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    _tick(db, league_id)
    db.commit()
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
