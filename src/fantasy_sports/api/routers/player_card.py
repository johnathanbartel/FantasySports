"""
Player card: everything the league pages show when a manager clicks a player's name, in one call.

GET /leagues/{league_id}/players/{draftable_asset_id}/card

  * bio, injury status, and "news" (injury changes and this league's moves involving him - there is no outside news feed)
  * fantasy game log: NHL = the simulated games he was in the frozen lineup for; football = his weekly points
  * schedule: his real team's upcoming games
  * simulation stats (NHL): GP, goals, assists, shots, saves, shutouts, counted from the published simulator records
  * league transactions (draft, signings, drops, trades), how and when his current team acquired him
  * % owned across every real (non-mock) league on the platform
  * trade-block flag and the actions the viewer may take (add / claim / drop / trade)

Spoiler rule (same as league leaders): simulated results count for a viewer only once the game is final and open to them
(platform/spoilers.py). The card never shows a result the leaders page would hold back.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from fantasy_sports.api.auth import optional_user
from fantasy_sports.api.helpers import draftable_asset_age, draftable_asset_display_name, draftable_asset_position
from fantasy_sports.platform import elimination, free_agency, game_lock, spoilers, waivers
from fantasy_sports.platform.db.models.core import (
    Draft, DraftableAsset, DraftPick, FreeAgencyTransaction, Game, InjuryAlert, League, LeagueUniverse, RealTeam,
    RecruitingAward, RecruitingDraft, RosterEntry, SeriesDailyGame, SimulatorEvent, Team, Trade, TradeBlockEntry,
    TradeItem, User, Week, WaiverClaim, WatchlistEntry,
)
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.sports.football.models import WeeklyPlayerScore

router = APIRouter(tags=["player-card"])

_ASSISTS = ("primary_assist", "secondary_assist")
_UPCOMING_GAMES = 8


class BioOut(BaseModel):
    display_name: str
    position: str | None
    real_team: str | None
    is_defense_group: bool = False
    age: int | None = None
    birth_date: datetime.date | None = None
    is_rookie: bool = False
    active: bool = True


class InjuryOut(BaseModel):
    status: str | None            # DTD / OUT / IR / LTIR, None = healthy
    note: str | None
    updated_at: datetime.datetime | None


class NewsItem(BaseModel):
    at: datetime.datetime | None
    kind: str                     # "injury" / "move"
    text: str


class GameLogRow(BaseModel):
    label: str                    # date (NHL) or "Week n" (football)
    opponent: str | None
    result: str | None            # "W 3-2" for a fantasy game, None for a football week
    points: float | None = None   # football weekly points
    goals: int = 0
    assists: int = 0
    shots: int = 0
    saves: int = 0
    shutout: bool = False


class SimStats(BaseModel):
    games_played: int = 0
    goals: int = 0
    assists: int = 0
    points: int = 0
    shots: int = 0
    saves: int = 0
    shutouts: int = 0


class ScheduleRow(BaseModel):
    when: datetime.datetime | None
    opponent: str
    home: bool
    week: int | None = None


class TxnOut(BaseModel):
    at: datetime.datetime | None
    team_id: int | None
    kind: str                     # drafted / recruited / added / dropped / traded / claimed
    text: str


class OwnershipOut(BaseModel):
    status: str                   # "rostered" / "free_agent" / "waivers" / "unavailable"
    team_id: int | None = None
    team_name: str | None = None
    owner_name: str | None = None
    acquired_via: str | None = None
    acquired_at: datetime.datetime | None = None
    acquired_text: str | None = None
    waiver_until: datetime.datetime | None = None
    on_trade_block: bool = False


class PctOwned(BaseModel):
    leagues_owned: int
    leagues_total: int
    percent: float | None


class ActionsOut(BaseModel):
    my_team_id: int | None = None
    can_add: bool = False
    can_claim: bool = False
    can_cancel_claim: bool = False
    can_drop: bool = False
    can_trade: bool = False
    can_watch: bool = False       # any player not on the viewer's own roster
    watching: bool = False        # he is on the viewer's watchlist
    note: str | None = None       # why the main action is unavailable, when it is


class PlayerCardOut(BaseModel):
    league_id: int
    draftable_asset_id: int
    sport: str                    # "hockey" / "football"
    bio: BioOut
    injury: InjuryOut
    news: list[NewsItem]
    game_log: list[GameLogRow]
    log_hidden_games: int = 0     # finished simulated games held back for this viewer (spoiler rule)
    schedule: list[ScheduleRow]
    sim_stats: SimStats | None    # NHL only
    transactions: list[TxnOut]
    ownership: OwnershipOut
    pct_owned: PctOwned
    actions: ActionsOut


def _team_names(db: Session, league_id: int) -> dict[int, Team]:
    return {t.id: t for t in db.query(Team).filter_by(league_id=league_id)}


def _tname(teams: dict[int, Team], team_id: int | None) -> str:
    t = teams.get(team_id) if team_id is not None else None
    return t.name if t else "a team"


def _hockey_log(db: Session, league: League, asset: DraftableAsset, user: User | None, teams: dict[int, Team]):
    """(game log rows newest first, season stats, number of held-back finished games)."""
    visible, held = spoilers.final_sims(db, league.id, user.id if user else None)
    is_goalie = asset.player is not None and asset.player.position == "G"
    played: list[tuple] = []          # (sim, dg, side)
    for sim, dg in visible:
        prov = sim.provenance or {}
        lineup = (prov.get("window_state") or {}).get("lineup")
        if not lineup:
            lineup = {side: [p["asset_id"] for p in (prov.get("box_score") or {}).get(side, [])] for side in ("home", "away")}
        for side in ("home", "away"):
            if asset.id in lineup.get(side, []):
                played.append((sim, dg, side))
    sim_ids = [s.id for s, _, _ in played]
    totals: dict[int, dict] = {}
    stat = {i: {"g": 0, "a": 0, "sog": 0} for i in sim_ids}
    if sim_ids:
        for sim_id, hs, as_, hsh, ash in (db.query(SimulatorEvent.simulator_game_id, func.max(SimulatorEvent.home_score),
                                                  func.max(SimulatorEvent.away_score), func.max(SimulatorEvent.home_shots),
                                                  func.max(SimulatorEvent.away_shots))
                                          .filter(SimulatorEvent.simulator_game_id.in_(sim_ids)).group_by(SimulatorEvent.simulator_game_id)):
            totals[sim_id] = {"home_score": hs, "away_score": as_, "home_shots": hsh, "away_shots": ash}
        for sim_id, kind, detail in (db.query(SimulatorEvent.simulator_game_id, SimulatorEvent.kind, SimulatorEvent.detail)
                                     .filter(SimulatorEvent.simulator_game_id.in_(sim_ids), SimulatorEvent.kind.in_(("score", "shot")))):
            detail = detail or {}
            for c in detail.get("credits", []):
                if c.get("asset_id") != asset.id:
                    continue
                if c.get("role") == "shooter":
                    stat[sim_id]["sog"] += 1
                    if kind == "score":
                        stat[sim_id]["g"] += 1
                elif kind == "score" and c.get("role") in _ASSISTS:
                    stat[sim_id]["a"] += 1
            if kind == "shot" and detail.get("origin") == "attempt" and detail.get("asset_id") == asset.id:
                stat[sim_id]["sog"] += 1

    rows, season = [], SimStats()
    for sim, dg, side in sorted(played, key=lambda x: (x[1].calendar_date, x[0].id), reverse=True):
        s = stat[sim.id]
        t = totals.get(sim.id)
        saves, shutout = 0, False
        if is_goalie and t is not None:
            other = "away" if side == "home" else "home"
            saves = max(0, t[f"{other}_shots"] - t[f"{other}_score"])
            shutout = t[f"{other}_score"] == 0
        own_score, opp_score = (dg.home_score, dg.away_score) if side == "home" else (dg.away_score, dg.home_score)
        opp_team = dg.away_team_id if side == "home" else dg.home_team_id
        result = f"{'W' if own_score > opp_score else 'L' if own_score < opp_score else 'T'} {own_score}-{opp_score}"
        label = dg.calendar_date.isoformat()
        rows.append(GameLogRow(label=label, opponent=_tname(teams, opp_team), result=result, goals=s["g"], assists=s["a"],
                               shots=s["sog"], saves=saves, shutout=shutout))
        season.games_played += 1
        season.goals += s["g"]
        season.assists += s["a"]
        season.shots += s["sog"]
        season.saves += saves
        season.shutouts += 1 if shutout else 0
    season.points = season.goals + season.assists
    return rows, season, len(held)


def _football_log(db: Session, asset: DraftableAsset, real_team_id: int | None) -> list[GameLogRow]:
    rows = (db.query(WeeklyPlayerScore, Week).join(Week, Week.id == WeeklyPlayerScore.week_id)
            .filter(WeeklyPlayerScore.draftable_asset_id == asset.id, Week.universe_id == asset.universe_id)
            .order_by(Week.season_year.desc(), Week.week_number.desc()).limit(20).all())
    names = {t.id: t.name for t in db.query(RealTeam)}
    opp: dict[int, str] = {}
    if real_team_id is not None and rows:
        for g in db.query(Game).filter(Game.week_id.in_([w.id for _, w in rows]),
                                       (Game.home_team_id == real_team_id) | (Game.away_team_id == real_team_id)):
            other = g.away_team_id if g.home_team_id == real_team_id else g.home_team_id
            opp[g.week_id] = ("vs " if g.home_team_id == real_team_id else "at ") + names.get(other, "?")
    return [GameLogRow(label=f"{w.season_year} Week {w.week_number}", opponent=opp.get(w.id), result=None, points=round(s.points, 1))
            for s, w in rows]


def _schedule(db: Session, real_team_id: int | None) -> list[ScheduleRow]:
    if real_team_id is None:
        return []
    now = datetime.datetime.utcnow() - datetime.timedelta(hours=6)
    names = {t.id: t.name for t in db.query(RealTeam)}
    games = (db.query(Game, Week).join(Week, Week.id == Game.week_id)
             .filter((Game.home_team_id == real_team_id) | (Game.away_team_id == real_team_id),
                     Game.status == "scheduled", Game.kickoff_at.isnot(None), Game.kickoff_at >= now)
             .order_by(Game.kickoff_at).limit(_UPCOMING_GAMES).all())
    out = []
    for g, w in games:
        home = g.home_team_id == real_team_id
        out.append(ScheduleRow(when=g.kickoff_at, opponent=names.get(g.away_team_id if home else g.home_team_id, "?"), home=home,
                               week=w.week_number))
    return out


def _percent_owned(db: Session, asset: DraftableAsset) -> PctOwned:
    """In how many real (non-mock) leagues on this player's universe he sits on a roster."""
    leagues = [l for (l,) in db.query(LeagueUniverse.league_id).join(League, League.id == LeagueUniverse.league_id)
               .filter(LeagueUniverse.universe_id == asset.universe_id, League.is_mock_draft.is_(False))]
    total = len(set(leagues))
    owned = (db.query(func.count(func.distinct(Team.league_id))).join(RosterEntry, RosterEntry.team_id == Team.id)
             .filter(RosterEntry.draftable_asset_id == asset.id, Team.league_id.in_(leagues or [0])).scalar() or 0)
    return PctOwned(leagues_owned=owned, leagues_total=total, percent=round(100.0 * owned / total, 1) if total else None)


def _history(db: Session, league_id: int, asset_id: int, teams: dict[int, Team]) -> list[TxnOut]:
    out: list[TxnOut] = []
    for pick in (db.query(DraftPick).join(Draft, Draft.id == DraftPick.draft_id)
                 .filter(Draft.league_id == league_id, DraftPick.draftable_asset_id == asset_id)):
        out.append(TxnOut(at=pick.picked_at, team_id=pick.team_id, kind="drafted",
                          text=f"Drafted by {_tname(teams, pick.team_id)} in round {pick.round} (pick {pick.pick_number})"))
    rdraft = db.query(RecruitingDraft).filter_by(league_id=league_id).first()
    if rdraft is not None:
        for a in db.query(RecruitingAward).filter_by(draft_id=rdraft.id, draftable_asset_id=asset_id):
            out.append(TxnOut(at=a.awarded_at, team_id=a.team_id, kind="recruited",
                              text=f"Recruited by {_tname(teams, a.team_id)} (week {a.period_number})"))
    for t in db.query(FreeAgencyTransaction).filter_by(league_id=league_id, draftable_asset_id=asset_id):
        verb = "signed" if t.kind == "add" else "dropped"
        out.append(TxnOut(at=t.created_at, team_id=t.team_id, kind="added" if t.kind == "add" else "dropped",
                          text=f"{_tname(teams, t.team_id)} {verb} him" + (f" for ${t.price}" if t.kind == "add" and t.price else "")))
    for c in db.query(WaiverClaim).filter_by(league_id=league_id, draftable_asset_id=asset_id, status="won"):
        out.append(TxnOut(at=c.resolved_at, team_id=c.team_id, kind="claimed", text=f"{_tname(teams, c.team_id)} won him on waivers"))
    for tr, item in (db.query(Trade, TradeItem).join(TradeItem, TradeItem.trade_id == Trade.id)
                     .filter(Trade.league_id == league_id, Trade.status == "accepted", TradeItem.draftable_asset_id == asset_id)):
        dest = tr.counterparty_team_id if item.sender_team_id == tr.proposer_team_id else tr.proposer_team_id
        out.append(TxnOut(at=tr.resolved_at or tr.created_at, team_id=dest, kind="traded",
                          text=f"Traded from {_tname(teams, item.sender_team_id)} to {_tname(teams, dest)}"))
    out.sort(key=lambda x: x.at or datetime.datetime.min, reverse=True)
    return out


def _news(db: Session, league_id: int, asset: DraftableAsset, txns: list[TxnOut], teams: dict[int, Team]) -> list[NewsItem]:
    items: list[NewsItem] = []
    seen: set[tuple] = set()
    for a in db.query(InjuryAlert).filter(InjuryAlert.draftable_asset_id == asset.id, InjuryAlert.team_id.in_(list(teams) or [0])):
        key = (a.created_at.replace(second=0, microsecond=0), a.new_status)
        if key in seen:
            continue
        seen.add(key)
        note = f" ({a.note})" if a.note else ""
        items.append(NewsItem(at=a.created_at, kind="injury",
                              text=f"Back to healthy{note}" if not a.new_status else f"Listed as {a.new_status}{note}"))
    items += [NewsItem(at=t.at, kind="move", text=t.text) for t in txns]
    items.sort(key=lambda n: n.at or datetime.datetime.min, reverse=True)
    return items[:12]


def _acquired_text(db: Session, league_id: int, entry: RosterEntry, via: str) -> str:
    label = {"draft": "Drafted", "auction": "Won at auction", "recruiting": "Recruited", "free_agency": "Signed as a free agent",
             "trade": "Acquired in a trade", "commissioner": "Added by the commissioner"}.get(via, via.replace("_", " ").capitalize())
    if via == "draft":
        pick = (db.query(DraftPick).join(Draft, Draft.id == DraftPick.draft_id)
                .filter(Draft.league_id == league_id, DraftPick.draftable_asset_id == entry.draftable_asset_id,
                        DraftPick.team_id == entry.team_id).first())
        if pick is not None:
            return f"{label}: round {pick.round}, pick {pick.pick_number}"
    return label


@router.get("/leagues/{league_id}/players/{draftable_asset_id}/card", response_model=PlayerCardOut)
def player_card(league_id: int, draftable_asset_id: int, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    asset = (db.query(DraftableAsset).options(joinedload(DraftableAsset.player), joinedload(DraftableAsset.defense_group))
             .filter(DraftableAsset.id == draftable_asset_id).first())
    in_league = asset is not None and db.query(LeagueUniverse).filter_by(league_id=league_id, universe_id=asset.universe_id).first()
    if asset is None or not in_league:
        raise HTTPException(404, "Player not found in this league")

    teams = _team_names(db, league_id)
    player = asset.player
    real_team_id = player.real_team_id if player else (asset.defense_group.real_team_id if asset.defense_group else None)
    real_team = db.get(RealTeam, real_team_id) if real_team_id else None
    hockey = league.series_length_days is not None

    bio = BioOut(display_name=draftable_asset_display_name(asset), position=draftable_asset_position(asset),
                 real_team=real_team.name if real_team else None, is_defense_group=player is None, age=draftable_asset_age(asset),
                 birth_date=player.birth_date if player else None, is_rookie=bool(player and player.is_rookie),
                 active=bool(player.active) if player else True)
    injury = InjuryOut(status=player.injury_status if player else None, note=player.injury_note if player else None,
                       updated_at=player.injury_updated_at if player else None)

    sim_stats, hidden = None, 0
    if hockey:
        log, sim_stats, hidden = _hockey_log(db, league, asset, user, teams)
    else:
        log = _football_log(db, asset, real_team_id)

    txns = _history(db, league_id, asset.id, teams)

    # ownership
    now = datetime.datetime.utcnow()
    entry = (db.query(RosterEntry).filter(RosterEntry.draftable_asset_id == asset.id, RosterEntry.team_id.in_(list(teams) or [0])).first())
    own = OwnershipOut(status="unavailable")
    until = None
    owner_user = None
    if entry is not None:
        team = teams[entry.team_id]
        owner_user = db.get(User, team.owner_user_id) if team.owner_user_id and not team.is_ai_owned else None
        via = entry.acquired_via or "draft"
        on_block = db.query(TradeBlockEntry).filter_by(league_id=league_id, team_id=team.id, draftable_asset_id=asset.id).first() is not None
        own = OwnershipOut(status="rostered", team_id=team.id, team_name=team.name, owner_name=owner_user.display_name if owner_user else "AI",
                           acquired_via=via, acquired_at=entry.acquired_at, acquired_text=_acquired_text(db, league_id, entry, via),
                           on_trade_block=on_block)
    elif asset.id in set(free_agency.pool_ids(db, league_id)):
        until = waivers.waiver_until(db, league_id, now).get(asset.id)
        own = OwnershipOut(status="waivers" if until else "free_agent", waiver_until=until)

    # what the viewer may do
    actions = ActionsOut()
    mine = None
    if user is not None:
        mine = next((t for t in teams.values() if t.owner_user_id == user.id and not t.is_ai_owned), None)
    if mine is not None:
        actions.my_team_id = mine.id
        actions.watching = db.query(WatchlistEntry).filter_by(team_id=mine.id, draftable_asset_id=asset.id).first() is not None
        actions.can_watch = not (own.status == "rostered" and own.team_id == mine.id)
        fa_open = free_agency.is_open(db, league_id)
        locked, lock_why = elimination.free_agency_locked(league, mine)
        try:
            game_lock.assert_unlocked(db, league_id, [asset.id])
            game_locked = None
        except ValueError as e:
            game_locked = str(e)
        if own.status == "rostered" and own.team_id == mine.id:
            actions.can_drop = fa_open and not locked and game_locked is None
            actions.note = (lock_why if locked else game_locked) if not actions.can_drop else None
        elif own.status == "rostered":
            actions.can_trade = True
        elif own.status == "free_agent":
            problem = None if not fa_open else free_agency.signing_problem(db, league, mine, asset.id)
            actions.can_add = fa_open and not locked and problem is None and game_locked is None
            actions.note = None if actions.can_add else ("Free agency opens when the draft ends" if not fa_open
                                                         else lock_why if locked else problem or game_locked)
        elif own.status == "waivers":
            pending = asset.id in waivers.pending_claims(db, mine.id)
            actions.can_cancel_claim = pending
            actions.can_claim = fa_open and not locked and not pending and game_locked is None
            actions.note = None if actions.can_claim or pending else (lock_why if locked else game_locked)

    return PlayerCardOut(
        league_id=league_id, draftable_asset_id=asset.id, sport="hockey" if hockey else "football", bio=bio, injury=injury,
        news=_news(db, league_id, asset, txns, teams), game_log=log, log_hidden_games=hidden, schedule=_schedule(db, real_team_id),
        sim_stats=sim_stats, transactions=txns, ownership=own, pct_owned=_percent_owned(db, asset), actions=actions)
