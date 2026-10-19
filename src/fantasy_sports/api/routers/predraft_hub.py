"""Pre-draft hub: the room a league's managers get ready in before the draft (rules in platform/predraft_hub.py).

  GET  /leagues/{id}/predraft-hub            when the draft is, the caller's picks, pick trading, auto draft, mock draft
  GET  /leagues/{id}/predraft-hub/players    the players a team can rank (the draft pool, best last-season points first)
  PUT  /teams/{id}/draft-rankings            replace the team's ranking
  PUT  /teams/{id}/autodraft                 'fantasymotion' or 'my_rankings'
  POST /leagues/{id}/predraft-hub/mock-draft builds a mock draft to the league's specs and starts it

Rankings and the auto-draft choice belong to a team's manager alone: nobody else, the commissioner included, reads or sets them.
"""

import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_owns_team, current_user, optional_user
from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
from fantasy_sports.api.routers import mock_drafts as mock_router
from fantasy_sports.api.routers.leagues import AI_TEAM_NAMES
from fantasy_sports.platform import mock_drafts, pick_trading, predraft_hub
from fantasy_sports.platform.db.models.core import DraftableAsset, League, LeagueUniverse, RealTeam, Team, Universe, User
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.platform.draft_stats import stats_by_asset
from fantasy_sports.platform.league_features import draft_state
from fantasy_sports.platform.recruiting.pricing import season_points_by_asset

router = APIRouter(tags=["predraft-hub"])


class HubPick(BaseModel):
    pick_number: int | None      # None while the draft order isn't set (random order, set when the draft starts)
    round: int
    made: bool


class HubTradePick(BaseModel):
    season: int
    round: int
    original_team_id: int
    original_team_name: str
    projected_overall: int | None


class HubTeam(BaseModel):
    id: int
    name: str
    draft_position: int | None


class PicksOut(BaseModel):
    team_id: int | None
    team_name: str | None
    order_known: bool
    order_note: str
    total_picks: int
    picks: list[HubPick]


class TradingOut(BaseModel):
    available: bool
    reason: str | None
    picks: list[HubTradePick]
    trades_url: str


class AutoDraftOut(BaseModel):
    mode: Literal["fantasymotion", "my_rankings"]
    ranked_count: int
    can_edit: bool


class MockOut(BaseModel):
    available: bool
    reason: str | None
    size: int
    rounds: int
    universe_codes: list[str]
    your_position: int | None


class RumorsOut(BaseModel):
    available: bool
    message: str


class HubOut(BaseModel):
    league_id: int
    league_name: str
    draft_type: str
    draft_state: str                                  # not_started / in_progress / complete
    draft_scheduled_at: datetime.datetime | None      # naive UTC
    draft_order_mode: str
    draft_id: int | None
    size: int
    rounds: int
    universe_codes: list[str]
    can_enter_room: bool
    room_url: str
    is_member: bool
    teams: list[HubTeam]
    picks: PicksOut
    trading: TradingOut
    auto_draft: AutoDraftOut | None
    mock_draft: MockOut
    rumors: RumorsOut


class PlayerOut(BaseModel):
    draftable_asset_id: int
    display_name: str
    position: str | None
    team: str | None
    points: float | None
    rank: int | None             # the caller's own ranking, if they have one


class PlayersOut(BaseModel):
    total: int
    players: list[PlayerOut]


class RankingsIn(BaseModel):
    draftable_asset_ids: list[int] = Field(max_length=predraft_hub.MAX_RANKED)


class RankingsOut(BaseModel):
    draftable_asset_ids: list[int]


class AutoDraftIn(BaseModel):
    mode: Literal["fantasymotion", "my_rankings"]


class MockIn(BaseModel):
    pick_seconds: int = Field(default=mock_drafts.DEFAULT_PICK_SECONDS, ge=10, le=600)


class MockCreated(BaseModel):
    league_id: int
    team_id: int
    room_url: str
    your_position: int | None


def _league(db: Session, league_id: int) -> League:
    league = db.get(League, league_id)
    if league is None or league.is_mock_draft:
        raise HTTPException(404, "League not found")
    return league


def _my_team(db: Session, league: League, user: User | None) -> Team | None:
    if user is None:
        return None
    return db.query(Team).filter(Team.league_id == league.id, Team.owner_user_id == user.id).first()


def _universe_codes(db: Session, league_id: int) -> list[str]:
    return [r[0] for r in db.query(Universe.code).join(LeagueUniverse, LeagueUniverse.universe_id == Universe.id)
            .filter(LeagueUniverse.league_id == league_id).order_by(LeagueUniverse.id)]


def _room_url(league: League, team: Team | None) -> str:
    return f"/static/draft-board.html?league_id={league.id}" + (f"&team_id={team.id}" if team else "")


def _trading(db: Session, league: League, team: Team | None) -> TradingOut:
    trades_url = f"/static/league.html?league_id={league.id}&tab=trades" + (f"&team_id={team.id}" if team else "")
    if league.is_mock_draft or not pick_trading.format_supports(league):
        return TradingOut(available=False, trades_url=trades_url, picks=[],
                          reason="This league's format has no pick trading: the order is random on draft day.")
    if not league.pick_trading_enabled:
        return TradingOut(available=False, trades_url=trades_url, picks=[],
                          reason="The commissioner has turned draft pick trading off.")
    if team is None:
        return TradingOut(available=False, trades_url=trades_url, picks=[], reason="Only managers can trade picks.")
    order = pick_trading.projected_order(db, league)
    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=league.id)}
    picks = [HubTradePick(season=p["season"], round=p["round"], original_team_id=p["original_team_id"],
                          original_team_name=names.get(p["original_team_id"], "?"),
                          projected_overall=pick_trading.projected_overall(league, order, p["round"], p["original_team_id"]))
             for p in pick_trading.owned_picks(db, league, team.id)]
    return TradingOut(available=True, reason=None, picks=picks, trades_url=trades_url)


@router.get("/leagues/{league_id}/predraft-hub", response_model=HubOut)
def get_hub(league_id: int, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    league = _league(db, league_id)
    if league.draft_type != "snake":
        raise HTTPException(400, "The pre-draft hub is for snake drafts; recruiting leagues use the recruiting board.")
    state = draft_state(db, league.id)
    db.commit()           # draft_state closes a finished draft
    draft = predraft_hub.current_draft(db, league)
    team = _my_team(db, league, user)
    picks = predraft_hub.my_picks(db, league, team, draft) if team else {
        "order_known": False, "source": "random", "picks": [], "total_picks": 0}
    if picks["order_known"]:
        note = "These are your picks." if picks["source"] == "draft" else "Your picks, from the order the commissioner set."
    else:
        note = "The pick order is set when the draft starts. Your position isn't known yet."
    teams = [HubTeam(id=t.id, name=t.name, draft_position=t.draft_position if league.draft_order_mode == "manual" else None)
             for t in db.query(Team).filter_by(league_id=league.id).order_by(Team.id)]
    if league.draft_order_mode == "manual":
        teams.sort(key=lambda t: (t.draft_position is None, t.draft_position or 0, t.id))
    blocker = predraft_hub.mock_blocker(db, league)
    mock_position = None
    if team is not None and league.draft_order_mode == "manual":
        mock_position = team.draft_position
    return HubOut(
        league_id=league.id, league_name=league.name, draft_type=league.draft_type, draft_state=state,
        draft_scheduled_at=league.draft_scheduled_at, draft_order_mode=league.draft_order_mode,
        draft_id=draft.id if draft else None, size=league.size,
        rounds=predraft_hub.draft_rounds(db, league), universe_codes=_universe_codes(db, league.id),
        can_enter_room=state != "complete", room_url=_room_url(league, team), is_member=team is not None,
        teams=teams,
        picks=PicksOut(team_id=team.id if team else None, team_name=team.name if team else None, order_note=note,
                       order_known=picks["order_known"], total_picks=picks["total_picks"],
                       picks=[HubPick(**p) for p in picks["picks"]]),
        trading=_trading(db, league, team),
        auto_draft=AutoDraftOut(mode=predraft_hub.get_mode(db, team.id), ranked_count=len(predraft_hub.ranked_asset_ids(db, team.id)),
                                can_edit=state == "not_started" or state == "in_progress") if team else None,
        mock_draft=MockOut(available=blocker is None and team is not None,
                           reason=blocker or (None if team else "Only managers can run a mock draft."),
                           size=league.size, rounds=predraft_hub.draft_rounds(db, league),
                           universe_codes=_universe_codes(db, league.id), your_position=mock_position),
        rumors=RumorsOut(available=False, message="Draft rumors are coming soon."),
    )


@router.get("/leagues/{league_id}/predraft-hub/players", response_model=PlayersOut)
def hub_players(league_id: int, q: str = "", position: str = "", limit: int = 200, offset: int = 0,
                db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    """The draft pool for ranking, best last-season points first. Filter by name or position; the caller's own rank rides along."""
    league = _league(db, league_id)
    team = _my_team(db, league, user)
    limit = max(1, min(limit, 500))
    points = season_points_by_asset(db)
    rows = []
    for a in predraft_hub.ranking_pool(db, league):
        name = draftable_asset_display_name(a)
        pos = draftable_asset_position(a)
        if position and pos != position:
            continue
        if q and q.strip().lower() not in name.lower():
            continue
        rows.append((a, name, pos))
    rows.sort(key=lambda r: (-points.get(r[0].id, -1.0), r[1]))
    total = len(rows)
    page = rows[max(0, offset):max(0, offset) + limit]
    stats = stats_by_asset(db, [a.id for a, _, _ in page if a.asset_type == "player"])
    own = {aid: i for i, aid in enumerate(predraft_hub.ranked_asset_ids(db, team.id), start=1)} if team else {}
    teams = {rt.id: rt.name for rt in db.query(RealTeam)}
    out = []
    for a, name, pos in page:
        real = teams.get(a.player.real_team_id) if a.asset_type == "player" and a.player is not None and a.player.real_team_id else None
        out.append(PlayerOut(draftable_asset_id=a.id, display_name=name, position=pos, team=real,
                             points=stats.get(a.id, {}).get("points"), rank=own.get(a.id)))
    return PlayersOut(total=total, players=out)


@router.get("/teams/{team_id}/draft-rankings", response_model=RankingsOut)
def get_rankings(team_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    team = assert_owns_team(db, user, team_id)
    return RankingsOut(draftable_asset_ids=predraft_hub.ranked_asset_ids(db, team.id))


@router.put("/teams/{team_id}/draft-rankings", response_model=RankingsOut)
def put_rankings(team_id: int, payload: RankingsIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    team = assert_owns_team(db, user, team_id)
    try:
        predraft_hub.set_rankings(db, team, payload.draftable_asset_ids)
    except predraft_hub.HubError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    db.commit()
    return RankingsOut(draftable_asset_ids=predraft_hub.ranked_asset_ids(db, team.id))


@router.put("/teams/{team_id}/autodraft", response_model=AutoDraftOut)
def put_autodraft(team_id: int, payload: AutoDraftIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    team = assert_owns_team(db, user, team_id)
    try:
        predraft_hub.set_mode(db, team, payload.mode)
    except predraft_hub.HubError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    db.commit()
    return AutoDraftOut(mode=predraft_hub.get_mode(db, team.id), ranked_count=len(predraft_hub.ranked_asset_ids(db, team.id)),
                        can_edit=True)


@router.post("/leagues/{league_id}/predraft-hub/mock-draft", response_model=MockCreated)
def start_mock(league_id: int, payload: MockIn | None = None, db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    """Builds a mock draft to this league's specs, with the caller as its only person, and starts it straight away."""
    league = _league(db, league_id)
    team = _my_team(db, league, user)
    if team is None:
        raise HTTPException(403, "Only a manager in this league can run its mock draft")
    pick_seconds = payload.pick_seconds if payload else mock_drafts.DEFAULT_PICK_SECONDS
    try:
        mock = predraft_hub.create_mock(db, league, user, team, AI_TEAM_NAMES, pick_seconds)
    except predraft_hub.HubError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    from fantasy_sports.platform.defense_modes import league_pool
    pool = len(league_pool(db, mock.id))
    need = mock.size * mock.draft_rounds
    if pool < need:
        db.rollback()
        raise HTTPException(400, f"The player pool has {pool} players but {mock.size} teams x {mock.draft_rounds} rounds need {need}.")
    db.commit()
    if not mock_router.start(db, mock):
        raise HTTPException(500, "The mock draft couldn't start")
    mock_team = db.query(Team).filter_by(league_id=mock.id, owner_user_id=user.id).one()
    return MockCreated(league_id=mock.id, team_id=mock_team.id, room_url=_room_url(mock, mock_team),
                       your_position=draft_order_position(db, mock, mock_team))


def draft_order_position(db: Session, league: League, team: Team) -> int | None:
    """The team's saved 1-based draft position, None while the order is random."""
    return team.draft_position if league.draft_order_mode == "manual" else None
