"""
The Commissioner tab's endpoints (web/static/league.html): co-commissioners, per-team counters (transactions, FAAB), the waiver
order, roster edits for any team, league history edits and the commissioner action log. Lineups and lines are edited through
the ordinary lineup / pecking-order endpoints, which accept a commissioner for any team (api/auth.assert_can_manage_team).

Everything here needs a commissioner or co-commissioner, except where the docstring says "primary" (the league's own
commissioner) - adding and removing co-commissioners. See platform/commissioner.py for the rules.
"""

import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from fantasy_sports.api.auth import assert_commissioner, assert_primary_commissioner, current_user
from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
from fantasy_sports.platform import commissioner, free_agency, notifications, waivers
from fantasy_sports.platform.db.models.core import (
    CommissionerAction,
    DraftableAsset,
    EmailOutbox,
    League,
    LeagueSeason,
    RosterEntry,
    Team,
    User,
)
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.platform.league_features import is_college

router = APIRouter(tags=["commissioner"])


def _league(db: Session, league_id: int) -> League:
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    return league


def _team(db: Session, league_id: int, team_id: int) -> Team:
    team = db.get(Team, team_id)
    if team is None or team.league_id != league_id:
        raise HTTPException(404, "Team not found in this league")
    return team


def _run(db: Session, fn, *args, **kwargs):
    """Runs a platform/commissioner.py action, turning its refusals into HTTP errors."""
    try:
        return fn(*args, **kwargs)
    except PermissionError as e:
        db.rollback()
        raise HTTPException(403, str(e))
    except commissioner.CommissionerError as e:
        db.rollback()
        raise HTTPException(400, str(e))


# ---- everyone can read the transaction counts (standings show them) -----------------------------------------------------
class TransactionCountOut(BaseModel):
    team_id: int
    signings: int
    trades: int
    adjustment: int
    total: int


@router.get("/leagues/{league_id}/transaction-counts", response_model=list[TransactionCountOut])
def transaction_counts(league_id: int, db: Session = Depends(get_db)):
    """Each team's transaction count: its free-agent / waiver signings plus completed trades, plus the commissioner's adjustment."""
    _league(db, league_id)
    counts = commissioner.transaction_counts(db, league_id)
    return [TransactionCountOut(team_id=t, **c) for t, c in sorted(counts.items())]


# ---- the tab's overview -------------------------------------------------------------------------------------------------
class CommissionerPerson(BaseModel):
    user_id: int
    display_name: str
    role: str                    # "commissioner" / "co_commissioner"


class CommissionerTeamOut(BaseModel):
    team_id: int
    name: str
    owner_user_id: int | None
    owner_name: str | None
    is_ai_owned: bool
    transactions: TransactionCountOut
    faab_balance: int | None     # free-agency money; None where the league has no budget
    faab_adjustment: int
    waiver_position: int


class ActionOut(BaseModel):
    id: int
    at: datetime.datetime
    user_id: int
    user_name: str | None
    action: str
    team_id: int | None
    team_name: str | None
    detail: dict | None


class EmailOut(BaseModel):
    id: int
    at: datetime.datetime
    kind: str
    to_email: str
    subject: str
    status: str


class CommissionerOverviewOut(BaseModel):
    league_id: int
    is_primary: bool
    has_budget: bool
    waiver_basis: str
    commissioners: list[CommissionerPerson]
    eligible_co_commissioners: list[CommissionerPerson]       # managers in the league who could be added
    teams: list[CommissionerTeamOut]
    recent_actions: list[ActionOut]
    recent_emails: list[EmailOut]
    smtp_configured: bool


@router.get("/leagues/{league_id}/commissioner", response_model=CommissionerOverviewOut)
def commissioner_overview(league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Everything the Commissioner tab shows. Commissioner or co-commissioner."""
    league = assert_commissioner(db, user, league_id)
    now = datetime.datetime.utcnow()
    waivers.process(db, league_id, now)
    db.commit()
    users = {u.id: u for u in db.query(User)}
    teams = db.query(Team).filter_by(league_id=league_id).order_by(Team.id).all()
    team_names = {t.id: t.name for t in teams}
    counts = commissioner.transaction_counts(db, league_id)
    order = waivers.waiver_order(db, league_id, now)
    position = {t: i for i, t in enumerate(order.teams, start=1)}
    college = is_college(db, league_id)
    cids = commissioner.co_commissioner_ids(db, league_id)

    def person(uid: int, role: str) -> CommissionerPerson:
        return CommissionerPerson(user_id=uid, display_name=users[uid].display_name if uid in users else str(uid), role=role)

    team_rows = []
    for t in teams:
        money = free_agency.team_money(db, t) if college else None
        team_rows.append(CommissionerTeamOut(
            team_id=t.id, name=t.name, owner_user_id=t.owner_user_id, is_ai_owned=t.is_ai_owned,
            owner_name=users[t.owner_user_id].display_name if t.owner_user_id in users else None,
            transactions=TransactionCountOut(team_id=t.id, **counts[t.id]),
            faab_balance=money["fa_money"] if money else None, faab_adjustment=money["faab_adjustment"] if money else 0,
            waiver_position=position.get(t.id, 0)))
    team_rows.sort(key=lambda r: r.waiver_position)
    eligible = sorted({t.owner_user_id for t in teams if t.owner_user_id and not t.is_ai_owned} - cids - {league.commissioner_user_id})
    actions = (db.query(CommissionerAction).filter_by(league_id=league_id).order_by(CommissionerAction.id.desc()).limit(60).all())
    emails = db.query(EmailOutbox).filter_by(league_id=league_id).order_by(EmailOutbox.id.desc()).limit(25).all()
    return CommissionerOverviewOut(
        league_id=league_id, is_primary=league.commissioner_user_id == user.id, has_budget=college,
        waiver_basis=order.basis,
        commissioners=[person(league.commissioner_user_id, "commissioner")] + [person(u, "co_commissioner") for u in sorted(cids)],
        eligible_co_commissioners=[person(u, "co_commissioner") for u in eligible],
        teams=team_rows,
        recent_actions=[ActionOut(id=a.id, at=a.created_at, user_id=a.user_id, user_name=users[a.user_id].display_name if a.user_id in users else None,
                                  action=a.action, team_id=a.team_id, team_name=team_names.get(a.team_id), detail=a.detail) for a in actions],
        recent_emails=[EmailOut(id=e.id, at=e.created_at, kind=e.kind, to_email=e.to_email, subject=e.subject, status=e.status) for e in emails],
        smtp_configured=notifications.smtp_configured())


# ---- co-commissioners ---------------------------------------------------------------------------------------------------
class CoCommissionerRequest(BaseModel):
    user_id: int


@router.post("/leagues/{league_id}/co-commissioners", response_model=list[CommissionerPerson])
def add_co_commissioner(league_id: int, payload: CoCommissionerRequest, background: BackgroundTasks,
                        db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Primary commissioner: makes a manager in the league a co-commissioner. They are emailed, and so is the league."""
    league = assert_primary_commissioner(db, user, league_id)
    _run(db, commissioner.add_co_commissioner, db, league, user.id, payload.user_id)
    new = db.get(User, payload.user_id)
    notifications.queue_message(
        db, league, "co_commissioner_added", f"{league.name}: a new co-commissioner",
        f"{user.display_name} made {new.display_name} a co-commissioner of {league.name}. A co-commissioner can do everything "
        "the commissioner can, except add or remove co-commissioners or delete the league.")
    db.commit()
    background.add_task(notifications.deliver_pending)
    return _people(db, league)


@router.delete("/leagues/{league_id}/co-commissioners/{user_id}", response_model=list[CommissionerPerson])
def remove_co_commissioner(league_id: int, user_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Primary commissioner removes a co-commissioner; a co-commissioner may remove themselves."""
    league = assert_commissioner(db, user, league_id)
    _run(db, commissioner.remove_co_commissioner, db, league, user.id, user_id)
    db.commit()
    return _people(db, league)


def _people(db: Session, league: League) -> list[CommissionerPerson]:
    users = {u.id: u for u in db.query(User).filter(User.id.in_(commissioner.commissioner_ids(db, league)))}
    ids = [league.commissioner_user_id] + sorted(commissioner.co_commissioner_ids(db, league.id))
    return [CommissionerPerson(user_id=i, display_name=users[i].display_name, role="commissioner" if i == league.commissioner_user_id else "co_commissioner")
            for i in ids if i in users]


# ---- counters -----------------------------------------------------------------------------------------------------------
class TransactionsSetRequest(BaseModel):
    total: int = Field(ge=0, le=100_000)


@router.put("/leagues/{league_id}/teams/{team_id}/transactions", response_model=TransactionCountOut)
def set_transactions(league_id: int, team_id: int, payload: TransactionsSetRequest, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """Sets a team's transaction count. The signings themselves are untouched: the difference is stored as an adjustment."""
    assert_commissioner(db, user, league_id)
    team = _team(db, league_id, team_id)
    row = _run(db, commissioner.set_transaction_count, db, team, user.id, payload.total)
    db.commit()
    return TransactionCountOut(team_id=team.id, **row)


class FaabSetRequest(BaseModel):
    balance: int = Field(ge=0, le=10_000_000)


@router.put("/leagues/{league_id}/teams/{team_id}/faab", response_model=CommissionerTeamOut)
def set_faab(league_id: int, team_id: int, payload: FaabSetRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Sets a team's FAAB balance (its free-agency money). College leagues only: elsewhere there is no budget."""
    league = assert_commissioner(db, user, league_id)
    team = _team(db, league_id, team_id)
    _run(db, commissioner.set_faab_balance, db, team, user.id, payload.balance)
    db.commit()
    money = free_agency.team_money(db, team)
    counts = commissioner.transaction_counts(db, league_id)
    position = {t: i for i, t in enumerate(waivers.waiver_order(db, league_id).teams, start=1)}
    owner = db.get(User, team.owner_user_id) if team.owner_user_id else None
    return CommissionerTeamOut(
        team_id=team.id, name=team.name, owner_user_id=team.owner_user_id, owner_name=owner.display_name if owner else None,
        is_ai_owned=team.is_ai_owned, transactions=TransactionCountOut(team_id=team.id, **counts[team.id]),
        faab_balance=money["fa_money"], faab_adjustment=money["faab_adjustment"], waiver_position=position.get(team.id, 0))


class WaiverOrderRequest(BaseModel):
    team_ids: list[int]


@router.put("/leagues/{league_id}/waiver-order")
def set_waiver_order(league_id: int, payload: WaiverOrderRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Sets the waiver order by hand: every team, first claim first. It holds until the next weekly reset."""
    league = assert_commissioner(db, user, league_id)
    waivers.process(db, league_id)
    _run(db, commissioner.set_waiver_order, db, league, user.id, payload.team_ids)
    db.commit()
    order = waivers.waiver_order(db, league_id)
    return {"basis": order.basis, "team_ids": order.teams, "next_reset_at": waivers.next_reset(datetime.datetime.utcnow())}


# ---- rosters ------------------------------------------------------------------------------------------------------------
class PlayerHit(BaseModel):
    draftable_asset_id: int
    display_name: str
    position: str | None
    team_id: int | None          # the team that rosters him, None = free agent


@router.get("/leagues/{league_id}/commissioner/players", response_model=list[PlayerHit])
def search_players(league_id: int, q: str, limit: int = 25, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Players in the league's pool matching a name (at least two letters), with the team that has each, if any."""
    assert_commissioner(db, user, league_id)
    q = q.strip().lower()
    if len(q) < 2:
        raise HTTPException(400, "Type at least two letters")
    ids = free_agency.pool_ids(db, league_id)
    owner = {}
    team_ids = [t.id for t in db.query(Team.id).filter_by(league_id=league_id)]
    for r in db.query(RosterEntry).filter(RosterEntry.team_id.in_(team_ids or [0])):
        owner[r.draftable_asset_id] = r.team_id
    hits = []
    query = db.query(DraftableAsset).options(joinedload(DraftableAsset.player), joinedload(DraftableAsset.defense_group))
    for a in query.filter(DraftableAsset.id.in_(ids or [0])):
        name = draftable_asset_display_name(a)
        if q in name.lower():
            hits.append(PlayerHit(draftable_asset_id=a.id, display_name=name, position=draftable_asset_position(a), team_id=owner.get(a.id)))
    hits.sort(key=lambda h: (h.team_id is not None, h.display_name))
    return hits[:max(1, min(limit, 50))]


class RosterEditRequest(BaseModel):
    action: str = Field(pattern="^(add|drop|move)$")
    team_id: int                                  # add / drop: the team; move: the team the player goes TO
    draftable_asset_id: int
    from_team_id: int | None = None               # move only
    ignore_roster_limit: bool = False
    put_on_waivers: bool = False                  # drop only: give him the usual 24 hours on waivers


@router.post("/leagues/{league_id}/commissioner/roster")
def edit_roster(league_id: int, payload: RosterEditRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Adds a free agent to a team, drops a player from it, or moves a player between two teams - free, and logged."""
    league = assert_commissioner(db, user, league_id)
    a = payload.action
    if a == "add":
        _run(db, commissioner.add_player, db, league, user.id, payload.team_id, payload.draftable_asset_id, payload.ignore_roster_limit)
    elif a == "drop":
        _run(db, commissioner.drop_player, db, league, user.id, payload.team_id, payload.draftable_asset_id, payload.put_on_waivers)
    else:
        if payload.from_team_id is None:
            raise HTTPException(400, "Say which team the player is moving from")
        _run(db, commissioner.move_player, db, league, user.id, payload.from_team_id, payload.team_id, payload.draftable_asset_id,
             payload.ignore_roster_limit)
    db.commit()
    return {"status": "ok", "action": a}


# ---- league history -----------------------------------------------------------------------------------------------------
class StandingsRowIn(BaseModel):
    team_id: int | None = None
    team_name: str | None = Field(default=None, max_length=80)
    wins: int = 0
    losses: int = 0
    ties: int = 0


class SeasonEditRequest(BaseModel):
    champion_team_id: int | None = None
    standings: list[StandingsRowIn] | None = None
    note: str | None = Field(default=None, max_length=500)
    label: str | None = Field(default=None, max_length=40)


class SeasonCreateRequest(BaseModel):
    season_number: int
    standings: list[StandingsRowIn]
    champion_team_id: int | None = None
    note: str | None = Field(default=None, max_length=500)
    label: str | None = Field(default=None, max_length=40)


def _season_out(row: LeagueSeason) -> dict:
    return {"season_number": row.season_number, "champion_team_id": row.champion_team_id, "note": row.commissioner_note,
            "label": (row.data or {}).get("label"), "edited_at": row.edited_at, "standings": (row.data or {}).get("standings", [])}


@router.patch("/leagues/{league_id}/seasons/{season_number}")
def edit_season(league_id: int, season_number: int, payload: SeasonEditRequest, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    """Corrects an archived season: the champion, the final standings, a label and a note."""
    league = assert_commissioner(db, user, league_id)
    row = _run(db, commissioner.edit_season, db, league, user.id, season_number, set(payload.model_fields_set),
               champion_team_id=payload.champion_team_id,
               standings=[r.model_dump() for r in payload.standings] if payload.standings is not None else None,
               note=payload.note, label=payload.label)
    db.commit()
    return _season_out(row)


@router.post("/leagues/{league_id}/seasons")
def add_season(league_id: int, payload: SeasonCreateRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Adds a season played before the league joined the platform to its history."""
    league = assert_commissioner(db, user, league_id)
    row = _run(db, commissioner.add_past_season, db, league, user.id, payload.season_number,
               [r.model_dump() for r in payload.standings], champion_team_id=payload.champion_team_id, note=payload.note,
               label=payload.label)
    db.commit()
    return _season_out(row)
