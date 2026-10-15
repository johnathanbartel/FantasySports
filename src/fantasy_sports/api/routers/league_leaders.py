"""
NHL league leaders: what players have done in this league's SIMULATED games (never real-life stats).

Everything is counted from the published simulator records of FINAL games, so it can only ever show what the
simulation itself produced:

  * GP   starts in the game's frozen lineup (older scripts without a lineup: the players on its box score)
  * G/A  credits on the script's goal events (shooter / primary or secondary assist); team goals the script gives to
         nobody are not credited
  * SV   for each goalie in the lineup: the shots the other side took minus the goals it scored
  * SO   the goalie's side kept the other side off the scoreboard

Spoiler rule (same as the scoreboard): a game counts for a viewer only once it is FINAL and either both teams are AI,
or the viewer or one of the two owners has opened it.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from fantasy_sports.api.auth import optional_user
from fantasy_sports.api.helpers import draftable_asset_display_name
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, League, LeagueSeries, RealTeam, RosterEntry, SeriesDailyGame, SimulatorEvent, SimulatorGame,
    SimulatorReveal, Team, User,
)
from fantasy_sports.platform.db.session import get_db

router = APIRouter(tags=["league-leaders"])

_ASSISTS = ("primary_assist", "secondary_assist")


class LeaderRow(BaseModel):
    draftable_asset_id: int
    display_name: str
    position: str | None
    real_team: str | None
    fantasy_team_id: int | None
    games_played: int
    goals: int
    assists: int
    points: int
    saves: int
    shutouts: int


class LeadersOut(BaseModel):
    games_counted: int
    games_hidden: int          # finished games this viewer has not opened yet (their stats are held back)
    leaders: list[LeaderRow]


@router.get("/leagues/{league_id}/leaders", response_model=LeadersOut)
def league_leaders(league_id: int, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    if league.series_length_days is None:
        raise HTTPException(400, "League leaders are only available for NHL leagues")

    sims = (db.query(SimulatorGame, SeriesDailyGame).join(SeriesDailyGame, SeriesDailyGame.id == SimulatorGame.daily_game_id)
            .join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
            .filter(LeagueSeries.league_id == league_id, SimulatorGame.status == "FINAL").all())
    teams = {t.id: t for t in db.query(Team).filter_by(league_id=league_id)}
    sim_ids = [s.id for s, _ in sims]
    revealed: dict[int, set[int]] = {}
    if sim_ids:
        for sim_id, uid in db.query(SimulatorReveal.simulator_game_id, SimulatorReveal.user_id).filter(
                SimulatorReveal.simulator_game_id.in_(sim_ids)):
            revealed.setdefault(sim_id, set()).add(uid)

    visible = []
    for sim, dg in sims:
        home, away = teams.get(dg.home_team_id), teams.get(dg.away_team_id)
        ai_vs_ai = bool(home and away and home.is_ai_owned and away.is_ai_owned)
        seen = revealed.get(sim.id, set())
        owners = {t.owner_user_id for t in (home, away) if t and t.owner_user_id}
        if ai_vs_ai or (user is not None and user.id in seen) or (owners & seen):
            visible.append((sim, dg))
    hidden = len(sims) - len(visible)

    stat: dict[int, dict[str, int]] = {}
    row = lambda a: stat.setdefault(a, {"gp": 0, "g": 0, "a": 0, "sv": 0, "so": 0})   # noqa: E731
    goalie_games: list[tuple[int, int, int]] = []        # (asset id, saves, goals against)
    vis_ids = [s.id for s, _ in visible]
    totals = {}
    if vis_ids:
        for sim_id, hs, as_, hsh, ash in (db.query(SimulatorEvent.simulator_game_id, func.max(SimulatorEvent.home_score),
                                                  func.max(SimulatorEvent.away_score), func.max(SimulatorEvent.home_shots),
                                                  func.max(SimulatorEvent.away_shots))
                                          .filter(SimulatorEvent.simulator_game_id.in_(vis_ids)).group_by(SimulatorEvent.simulator_game_id)):
            totals[sim_id] = {"home_score": hs, "away_score": as_, "home_shots": hsh, "away_shots": ash}
        for sim_id, detail in (db.query(SimulatorEvent.simulator_game_id, SimulatorEvent.detail)
                               .filter(SimulatorEvent.simulator_game_id.in_(vis_ids), SimulatorEvent.kind == "score")):
            for c in (detail or {}).get("credits", []):
                if c.get("role") == "shooter":
                    row(c["asset_id"])["g"] += 1
                elif c.get("role") in _ASSISTS:
                    row(c["asset_id"])["a"] += 1

    lineups: list[tuple[SimulatorGame, dict[str, list[int]]]] = []
    for sim, _ in visible:
        prov = sim.provenance or {}
        lineup = (prov.get("window_state") or {}).get("lineup")
        if not lineup:
            lineup = {side: [p["asset_id"] for p in (prov.get("box_score") or {}).get(side, [])] for side in ("home", "away")}
        lineups.append((sim, lineup))
        for ids in lineup.values():
            for a in ids:
                row(a)["gp"] += 1

    assets = {a.id: a for a in db.query(DraftableAsset).options(joinedload(DraftableAsset.player)).filter(
        DraftableAsset.id.in_(list(stat) or [0]))}
    for sim, lineup in lineups:
        t = totals.get(sim.id)
        if t is None:
            continue
        for side, other in (("home", "away"), ("away", "home")):
            against = t[f"{other}_score"]
            faced = t[f"{other}_shots"]
            for a in lineup.get(side, []):
                asset = assets.get(a)
                if asset is not None and asset.player is not None and asset.player.position == "G":
                    row(a)["sv"] += max(0, faced - against)
                    row(a)["so"] += 1 if against == 0 else 0

    owner = {}
    if assets:
        for r in db.query(RosterEntry).filter(RosterEntry.team_id.in_(list(teams) or [0]), RosterEntry.draftable_asset_id.in_(list(assets))):
            owner[r.draftable_asset_id] = r.team_id
    schools = {t.id: t.name for t in db.query(RealTeam)}
    leaders = []
    for a, s in stat.items():
        asset = assets.get(a)
        if asset is None or asset.player is None:
            continue
        leaders.append(LeaderRow(
            draftable_asset_id=a, display_name=draftable_asset_display_name(asset), position=asset.player.position,
            real_team=schools.get(asset.player.real_team_id), fantasy_team_id=owner.get(a), games_played=s["gp"],
            goals=s["g"], assists=s["a"], points=s["g"] + s["a"], saves=s["sv"], shutouts=s["so"]))
    return LeadersOut(games_counted=len(visible), games_hidden=hidden, leaders=leaders)
