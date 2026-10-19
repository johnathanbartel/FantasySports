"""The pre-draft hub: where a league's managers get ready before they enter the draft room.

What lives here:

  * Auto draft. Each team chooses how the picks made for it are chosen: 'fantasymotion' (the platform's own suggestion,
    platform/ai/snake_ai.py - the default) or 'my_rankings' (the first player still available in the team's own ranking,
    saved in team_draft_rankings). It applies to every pick the system makes for a person-managed team - a pick whose clock
    ran out, or the commissioner's autopick - never to AI-owned teams. When the ranking has nobody left, the FantasyMotion
    suggestion fills in, so a short ranking never stalls a draft.
  * Pick slots. Which pick numbers a team holds, as far as they are known before the draft starts: exactly with a manual
    draft order (and, for an offseason draft, the standings order with traded picks applied), by round only when the order is
    random on draft day. Once the draft exists the real pick rows are used.
  * Mock draft built to the league: same sport and defense mode, same number of teams and rounds, the same draft position as
    the team will pick from when the order is manual, and the team's own rankings and auto-draft choice carried over.

Pick trading itself stays in platform/pick_trading.py and the Trades tab; the hub shows what can be traded and links there.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from fantasy_sports.platform import draft_order, mock_drafts, season_rollover
from fantasy_sports.platform.db.models.core import (
    Draft,
    DraftableAsset,
    DraftPick,
    League,
    LeagueUniverse,
    RosterEntry,
    Team,
    TeamDraftPrefs,
    TeamDraftRanking,
    TradedPick,
    Universe,
)
from fantasy_sports.platform.defense_modes import league_pool
from fantasy_sports.platform.league_features import is_college, roster_spots

MODES = ("fantasymotion", "my_rankings")
DEFAULT_MODE = "fantasymotion"
MAX_RANKED = 1000


class HubError(ValueError):
    """A request the hub can't honour; the message is meant for the person."""


# ---------------------------------------------------------------- auto draft

def get_mode(db: Session, team_id: int) -> str:
    row = db.query(TeamDraftPrefs).filter_by(team_id=team_id).one_or_none()
    return row.autodraft_mode if row is not None else DEFAULT_MODE


def set_mode(db: Session, team: Team, mode: str, now: datetime.datetime | None = None) -> None:
    if mode not in MODES:
        raise HubError(f"Auto draft must be one of {', '.join(MODES)}")
    now = now or datetime.datetime.utcnow()
    row = db.query(TeamDraftPrefs).filter_by(team_id=team.id).one_or_none()
    if row is None:
        db.add(TeamDraftPrefs(team_id=team.id, autodraft_mode=mode, updated_at=now))
    else:
        row.autodraft_mode = mode
        row.updated_at = now
    db.flush()


def ranked_asset_ids(db: Session, team_id: int) -> list[int]:
    return [r[0] for r in db.query(TeamDraftRanking.draftable_asset_id).filter_by(team_id=team_id)
            .order_by(TeamDraftRanking.rank)]


def ranking_pool(db: Session, league: League) -> list[DraftableAsset]:
    """The players a team can rank for the league's next draft: the pool, minus anyone already on a roster."""
    pool = league_pool(db, league.id)
    rostered = {r[0] for r in db.query(RosterEntry.draftable_asset_id).join(Team, Team.id == RosterEntry.team_id)
                .filter(Team.league_id == league.id)}
    pool = [a for a in pool if a.id not in rostered]
    if league.offseason:
        kind = season_rollover.next_draft_kind(db, league)
        if kind:
            pool = season_rollover.pool_for_kind(pool, kind)
    return pool


def set_rankings(db: Session, team: Team, asset_ids: list[int]) -> int:
    """Replaces the team's ranking, first = rank 1. Players outside the draft's pool are refused, a repeat is refused.
    Flushes; returns how many are ranked."""
    if len(asset_ids) > MAX_RANKED:
        raise HubError(f"A ranking can hold at most {MAX_RANKED} players")
    if len(set(asset_ids)) != len(asset_ids):
        raise HubError("A player is listed twice in the ranking")
    league = db.get(League, team.league_id)
    allowed = {a.id for a in ranking_pool(db, league)}
    unknown = [a for a in asset_ids if a not in allowed]
    if unknown:
        raise HubError(f"{len(unknown)} of the players aren't in this league's draft pool")
    db.query(TeamDraftRanking).filter_by(team_id=team.id).delete(synchronize_session=False)
    db.flush()
    for rank, asset_id in enumerate(asset_ids, start=1):
        db.add(TeamDraftRanking(team_id=team.id, draftable_asset_id=asset_id, rank=rank))
    db.flush()
    return len(asset_ids)


def ranked_choice(db: Session, team: Team, available_asset_ids: set[int]) -> int | None:
    """The asset the team's own ranking picks, or None when the pick should be made the FantasyMotion way: an AI-owned
    team, a team on the FantasyMotion setting, or a ranking with nobody left available."""
    if team.is_ai_owned or get_mode(db, team.id) != "my_rankings":
        return None
    for asset_id in ranked_asset_ids(db, team.id):
        if asset_id in available_asset_ids:
            return asset_id
    return None


# ---------------------------------------------------------------- pick slots

def draft_rounds(db: Session, league: League) -> int:
    """Rounds of the league's first draft: one per roster spot outside college, the league's own setting in college."""
    if is_college(db, league.id):
        return league.draft_rounds
    return roster_spots(db, league.id) or league.draft_rounds


def snake_overall(position: int, size: int, rnd: int, snake: bool = True) -> int:
    """The overall pick number for the 1-based draft position in a round."""
    index = position - 1 if (rnd % 2 == 1 or not snake) else size - position
    return (rnd - 1) * size + index + 1


def _team_slots(db: Session, league: League, draft: Draft | None) -> tuple[str, list[tuple[int, int, int]] | None]:
    """(source, slots): slots are (pick_number, round, team_id) in pick order, or None while the order isn't set.
    source: 'draft' (the pick rows exist), 'plan' (worked out from the saved order) or 'random' (order set on draft day)."""
    if draft is not None:
        picks = db.query(DraftPick).filter_by(draft_id=draft.id).order_by(DraftPick.pick_number).all()
        return "draft", [(p.pick_number, p.round, p.team_id) for p in picks]
    if league.offseason:
        kind = season_rollover.next_draft_kind(db, league)
        if kind is None:
            return "random", None
        try:
            order = season_rollover.draft_order(db, league)
            if order is None:
                return "random", None
            plan = season_rollover.offseason_draft_plan(db, league, kind, order, season_rollover.draft_is_snake(league))
        except season_rollover.RolloverError:
            return "random", None
        owners = {(r.round, r.original_team_id): r.owner_team_id
                  for r in db.query(TradedPick).filter_by(league_id=league.id, season_number=league.season_number)}
        return "plan", [(n, rnd, owners.get((rnd, team_id), team_id)) for n, (team_id, rnd) in enumerate(plan, start=1)]
    teams = draft_order.manual_order(db, league)
    if teams is None:
        return "random", None
    rounds, size = draft_rounds(db, league), len(teams)
    slots = []
    for rnd in range(1, rounds + 1):
        order = teams if rnd % 2 == 1 else list(reversed(teams))
        slots.extend(((rnd - 1) * size + i + 1, rnd, t.id) for i, t in enumerate(order))
    return "plan", slots


def current_draft(db: Session, league: League) -> Draft | None:
    """The draft that is running or about to: the league's newest draft for this season that isn't finished, else None."""
    return (db.query(Draft).filter(Draft.league_id == league.id, Draft.season_number == league.season_number,
                                   Draft.status == "in_progress").order_by(Draft.id.desc()).first())


def my_picks(db: Session, league: League, team: Team, draft: Draft | None) -> dict:
    """The picks the team holds in the next draft. When the order isn't known yet only the rounds are listed."""
    source, slots = _team_slots(db, league, draft)
    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=league.id)}
    if slots is not None:
        mine = [{"pick_number": n, "round": rnd, "made": bool(draft and db.query(DraftPick.id).filter(
            DraftPick.draft_id == draft.id, DraftPick.pick_number == n, DraftPick.draftable_asset_id.isnot(None)).first())}
                for n, rnd, team_id in slots if team_id == team.id]
        return {"order_known": True, "source": source, "picks": mine, "total_picks": len(slots)}
    rounds = (season_rollover.kind_rounds(db, league, season_rollover.next_draft_kind(db, league) or "standard")
              if league.offseason else draft_rounds(db, league))
    return {"order_known": False, "source": source, "total_picks": rounds * league.size,
            "picks": [{"pick_number": None, "round": r, "made": False} for r in range(1, rounds + 1)]}


# ---------------------------------------------------------------- mock draft

def mock_blocker(db: Session, league: League) -> str | None:
    """None if a mock draft can be built from this league, else why not."""
    if league.is_mock_draft:
        return "This is already a mock draft."
    if league.draft_type != "snake":
        return "Mock drafts are available for snake drafts."
    if league.offseason:
        return "Mock drafts copy a league's first draft; offseason rookie and refill drafts can't be mocked yet."
    if not db.query(LeagueUniverse).filter_by(league_id=league.id).count():
        return "This league has no sport set up yet."
    return None


def create_mock(db: Session, league: League, user, team: Team, ai_names: list[str], pick_seconds: int) -> League:
    """A mock draft with the same specs as the league: sport(s) and defense mode, team count, rounds, draft position (when the
    league's order is manual) and the manager's own rankings and auto-draft choice. The manager is the only person in it.
    Flushes; the caller commits and starts it."""
    why = mock_blocker(db, league)
    if why:
        raise HubError(why)
    lus = db.query(LeagueUniverse).filter_by(league_id=league.id).order_by(LeagueUniverse.id).all()
    universes = [db.get(Universe, lu.universe_id) for lu in lus]
    rounds = draft_rounds(db, league)
    now = datetime.datetime.utcnow()
    mock = mock_drafts.create(
        db, creator=user, name=f"Mock draft: {league.name}"[:120], universe=universes[0], size=league.size, human_slots=1,
        rounds=rounds, start_at=now, visibility="private", fill_ai_if_unfilled=True, pick_seconds=pick_seconds,
        ai_names=ai_names)
    mock_lus = db.query(LeagueUniverse).filter_by(league_id=mock.id).all()
    mock_lus[0].defense_mode = lus[0].defense_mode
    for lu in lus[1:]:
        db.add(LeagueUniverse(league_id=mock.id, universe_id=lu.universe_id, defense_mode=lu.defense_mode))
    mock_team = db.query(Team).filter_by(league_id=mock.id, owner_user_id=user.id).one()

    # The same pick position, when the league fixed one.
    manual = draft_order.manual_order(db, league)
    if manual is not None:
        position = next((i for i, t in enumerate(manual, start=1) if t.id == team.id), None)
        if position is not None:
            others = [t.id for t in db.query(Team).filter_by(league_id=mock.id).order_by(Team.id) if t.id != mock_team.id]
            order = others[:position - 1] + [mock_team.id] + others[position - 1:]
            draft_order.set_order(db, mock, "manual", order)

    # Their rankings and auto-draft choice go with them (a mock has its own team and the same player ids).
    ranked = [a for a in ranked_asset_ids(db, team.id)]
    if ranked:
        set_rankings(db, mock_team, ranked)
    set_mode(db, mock_team, get_mode(db, team.id))
    db.flush()
    return mock
