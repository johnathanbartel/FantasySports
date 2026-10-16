"""
The live engine for NHL window games (docs/hockey_spec.md sections 5, 6.1 and 6.5): one fantasy matchup is one window
(a series_daily_games row with window_index), published period by period as its real periods end.

`tick(db, daily_game, now_utc)` is idempotent and safe to call as often as you like; each call does whatever is due:

  1. LOCK. At the window's first real kickoff the lineup is frozen: each side's starters from the pecking order, where
     every scheduled goalie counts as confirmed (there is no start evidence before the games). Nothing changes after.
  2. SELECT. Each starter's counting game is chosen at HIS lock - the kickoff of his earliest game in the window - by
     Best Matchup (fewer NHL standings points, read from the stored snapshot). The choice is stored and never redone.
  3. DIVIDE. The chronological division is built from the counting games (a still-undecided player is projected on his
     earliest game, which can only affect periods that cannot be published yet).
  4. PUBLISH. Fantasy period k is published when (a) its members are certain - at least k x n real periods have already
     begun, n being the number of counting games - (b) every real period in it has nominally ended, and (c) the stored
     feed shows each of them complete. Its events are appended to the game; published events never change.

Pure with respect to the clock: `now_utc` is a parameter. Only the database is touched, never the network.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy.orm import Session

from fantasy_sports.platform import pecking_order as po
from fantasy_sports.platform.db.models.core import (
    Game, LeagueUniverse, RealTeam, SeriesDailyGame, SimulatorEvent, Team, Universe,
)
from fantasy_sports.platform.simulator import service, wallclock
from fantasy_sports.platform.simulator.contract import CONTRACT_VERSION, SimEventInput, SimScript
from fantasy_sports.sports.hockey import pipeline
from fantasy_sports.sports.hockey.models import GameEvent, NhlStandingsSnapshot
from fantasy_sports.sports.hockey.simulator import loader, model, progressive
from fantasy_sports.sports.hockey.simulator.attribution import box_score
from fantasy_sports.sports.hockey.simulator.division import CLOCK_TIMEZONE, REAL_PERIOD, Division, division_table
from fantasy_sports.sports.hockey.simulator.model import (
    ADAPTER_ID, ADAPTER_VERSION, DEFAULT_WINDOW_SECONDS, NOISE_CAP, PERIOD_SECONDS, PERIODS, CountingGame, HockeyGameInputs,
    SideInput,
)
from fantasy_sports.sports.hockey.simulator.progressive import restrict


@dataclass
class TickResult:
    status: str                 # waiting / opened / published / idle
    published_periods: int = 0
    detail: str = ""


def _universe_id(db: Session, league_id: int) -> int:
    nhl = db.query(Universe).join(LeagueUniverse, LeagueUniverse.universe_id == Universe.id).filter(
        LeagueUniverse.league_id == league_id, Universe.code == "nhl").first()
    if nhl is None:
        raise model.InputError("Not an NHL league")
    return nhl.id


def window_games(db: Session, universe_id: int, start: datetime.date, end: datetime.date) -> list[Game]:
    """Stored real games kicking off in [start, end], earliest first."""
    begin = datetime.datetime.combine(start, datetime.time.min)
    stop = datetime.datetime.combine(end, datetime.time.min) + datetime.timedelta(days=1)
    return (db.query(Game).filter(Game.universe_id == universe_id, Game.kickoff_at >= begin, Game.kickoff_at < stop)
            .order_by(Game.kickoff_at, Game.id).all())


def freeze_lineup(db: Session, team: Team, real_team_ids: set[int]) -> list[int]:
    """Asset ids of the team's starters for a window. Every scheduled goalie counts as confirmed: before the games there
    is no evidence of who starts, so the highest-ranked scheduled goalie is the starter."""
    roster = po.roster_players(db, team.id)
    scheduled = frozenset(pid for pid, (_, pl) in roster.items() if pl.real_team_id in real_team_ids)
    order = {g: [pid for pid in ids if pid in roster] for g, ids in po.load_order(db, team.id).items()}
    resolution = po.resolve_starters(order, scheduled, scheduled)
    return [roster[pid][0] for group in po.GROUPS for pid in resolution.starters.get(group, [])]


def snapshot_points(db: Session, universe_id: int, team_id: int, lock_at: datetime.datetime):
    """(points, snapshot date) of the latest stored standings snapshot on or before the lock's date, else None."""
    row = (db.query(NhlStandingsSnapshot).filter(
        NhlStandingsSnapshot.universe_id == universe_id, NhlStandingsSnapshot.real_team_id == team_id,
        NhlStandingsSnapshot.as_of_date <= lock_at.date()).order_by(NhlStandingsSnapshot.as_of_date.desc()).first())
    return None if row is None else (row.points, row.as_of_date)


def select_game(db: Session, universe_id: int, asset_id: int, candidates: list[Game], real_team_id: int) -> dict:
    """The counting game among the player's candidates: his only game, else Best Matchup at his lock."""
    lock_at = min(g.kickoff_at for g in candidates)
    if len(candidates) == 1:
        return {"game_id": candidates[0].id, "source": "single", "lock_at": lock_at.isoformat()}
    pool, points, dates = [], {}, {}
    for g in candidates:
        opponent = g.away_team_id if g.home_team_id == real_team_id else g.home_team_id
        pool.append(pipeline.PlayerGame(asset_id, g.id, g.kickoff_at.date(), 0.0, kickoff_at=g.kickoff_at, opponent=opponent))
        found = snapshot_points(db, universe_id, opponent, lock_at)
        if found is not None:
            points[g.id], dates[g.id] = found
    if len(points) == len(pool):
        by_opponent = {p.opponent: points[p.game_id] for p in pool}
        best, _ = pipeline._best_matchup(pool, lock_at, lambda opp, lock: by_opponent[opp])
        return {"game_id": best.game_id, "source": "best_matchup", "lock_at": lock_at.isoformat(),
                "opponent_points": {str(g): p for g, p in points.items()},
                "snapshot_dates": {str(g): d.isoformat() for g, d in dates.items()}}
    # No standings were ever stored for this lock: decide on the schedule alone (earlier kickoff, then lower id) and say so.
    best = min(pool, key=lambda g: (g.kickoff_at, g.game_id))
    return {"game_id": best.game_id, "source": "no_standings", "lock_at": lock_at.isoformat()}


def real_period_complete(db: Session, game: Game, period: int) -> bool:
    """Whether the stored feed shows real period `period` of `game` complete: the game is final, or play from a later
    period exists. Overtime is part of period 3, so period 3 needs a final game."""
    if game.status == "final":
        return True
    if period >= PERIODS:
        return False
    return db.query(GameEvent.id).filter(GameEvent.game_id == game.id, GameEvent.period > period).first() is not None


def _utc_to_local(now_utc: datetime.datetime) -> datetime.datetime:
    return wallclock.utc_to_local(now_utc, CLOCK_TIMEZONE)


def _last_totals(db: Session, sim) -> dict[str, int]:
    last = db.query(SimulatorEvent).filter_by(simulator_game_id=sim.id).order_by(SimulatorEvent.seq.desc()).first()
    if last is None:
        return {"home_score": 0, "away_score": 0, "home_shots": 0, "away_shots": 0}
    return {"home_score": last.home_score, "away_score": last.away_score, "home_shots": last.home_shots, "away_shots": last.away_shots}


def tick(db: Session, daily_game: SeriesDailyGame, now_utc: datetime.datetime, seed: int | None = None) -> TickResult:
    """Do whatever is due for this window game at `now_utc`. Flushes; the caller commits."""
    if daily_game.window_index is None or daily_game.calendar_date_end is None:
        raise model.InputError("window_engine works on window rows only")
    if daily_game.status == "FINAL":
        return TickResult("idle", PERIODS)
    now = _utc_to_local(now_utc)
    league_id = daily_game.series.league_id
    universe_id = _universe_id(db, league_id)
    home, away = db.get(Team, daily_game.home_team_id), db.get(Team, daily_game.away_team_id)
    games = {g.id: g for g in window_games(db, universe_id, daily_game.calendar_date, daily_game.calendar_date_end)}
    seed = daily_game.id if seed is None else seed
    labels = loader._team_labels(db, [t for g in games.values() for t in (g.home_team_id, g.away_team_id)])
    rosters = {"home": po.roster_players(db, home.id), "away": po.roster_players(db, away.id)}
    roster_teams = {pl.real_team_id for r in rosters.values() for _, (_, pl) in r.items() if pl.real_team_id}
    relevant = [g for g in games.values() if g.home_team_id in roster_teams or g.away_team_id in roster_teams]
    if not relevant:
        return TickResult("waiting", 0, "no real games in this window involve either roster")
    first_kickoff = min(g.kickoff_at for g in relevant)

    sim = service.get_sim(db, daily_game.id)
    opened = False
    if sim is None:
        if now < first_kickoff:
            return TickResult("waiting", 0, f"locks at {first_kickoff.isoformat()}")
        window_real = {t for g in relevant for t in (g.home_team_id, g.away_team_id)}
        lineup = {"home": freeze_lineup(db, home, window_real), "away": freeze_lineup(db, away, window_real)}
        state = {"lineup": lineup, "selections": {}, "locked_at": first_kickoff.isoformat()}
        opening = SimEventInput(1, "period_start", 1, 0, None, 0, 0, 0, 0, "Period 1 begins", {})
        base = {"adapter": ADAPTER_ID, "adapter_version": ADAPTER_VERSION, "seed": seed, "published": "by_period",
                "teams": {"home": {"team_id": home.id, "name": home.name}, "away": {"team_id": away.id, "name": away.name}},
                "window": {"window_index": daily_game.window_index, "start": daily_game.calendar_date.isoformat(),
                           "end": daily_game.calendar_date_end.isoformat()},
                "window_state": state}
        script = SimScript(CONTRACT_VERSION, "hockey", PERIODS, PERIOD_SECONDS, DEFAULT_WINDOW_SECONDS, (opening,), base)
        sim = service.publish_script(db, daily_game, script, partial=True)
        service.start_game(db, sim, now_utc)
        opened = True
    prov = dict(sim.provenance or {})
    state = prov["window_state"]

    # ---- the starters, their candidate games and (when locked) counting games ---------------------------------------
    side_of, assets, real_team_of = {}, {}, {}
    for side in ("home", "away"):
        by_asset = {aid: pl for _, (aid, pl) in rosters[side].items()}
        for aid in state["lineup"][side]:
            side_of[aid], assets[aid], real_team_of[aid] = side, by_asset[aid], by_asset[aid].real_team_id
    selections = dict(state["selections"])
    counting: list[CountingGame] = []
    for aid in sorted(assets):
        cands = [g for g in relevant if real_team_of[aid] in (g.home_team_id, g.away_team_id)]
        if not cands:
            continue
        lock_at = min(g.kickoff_at for g in cands)
        rec = selections.get(str(aid))
        if rec is None and (len(cands) == 1 or now >= lock_at):
            rec = select_game(db, universe_id, aid, cands, real_team_of[aid])
            selections[str(aid)] = rec
        chosen = games[rec["game_id"]] if rec else min(cands, key=lambda g: (g.kickoff_at, g.id))   # projected until locked
        at_home = real_team_of[aid] == chosen.home_team_id
        counting.append(CountingGame(aid, assets[aid].full_name, side_of[aid], chosen.id, chosen.kickoff_at,
                                     f"vs {labels.get(chosen.away_team_id, '')}" if at_home else f"@ {labels.get(chosen.home_team_id, '')}",
                                     assets[aid].position or ""))
    division = Division.build(counting)
    n = len(counting)

    # ---- publish every period that is due -----------------------------------------------------------------------------
    published = sim.published_periods or 0
    new_periods = []
    while published < PERIODS and n:
        k = published + 1
        chunk = division.chunks[k - 1]
        begun = sum(1 for s in division.slots if s.start <= now)
        if begun < k * n or any(now < s.start + REAL_PERIOD for s in chunk):
            break
        if not all(real_period_complete(db, games[s.game_id], s.period) for s in chunk):
            break
        inputs = _inputs(db, daily_game, home, away, assets, side_of, counting, selections, state)
        prev = _last_totals(db, sim)
        events, norm, summary = progressive.build_period(inputs, division, k, seed, daily_game.id, prev)
        through = restrict(inputs, division, set(range(1, k + 1)))
        new_prov = _provenance(prov, state | {"selections": selections}, division, counting, through, summary, norm, k)
        service.append_period(db, sim, k, events, new_prov)
        prov, published = new_prov, k
        new_periods.append(k)
    if not new_periods:
        # Keep the clock and the stored choices current (a lock may have passed, a projection may have firmed up).
        fresh = dict(prov)
        fresh["window_state"] = state | {"selections": selections}
        fresh["wall_clock"] = division.wall_clock().to_dict() if n else prov.get("wall_clock")
        if fresh != prov:
            service.update_provenance(db, sim, fresh)
    service.sync(db, sim, now_utc)
    if new_periods:
        return TickResult("published", published, f"published period(s) {new_periods}")
    return TickResult("opened" if opened else "idle", published)


def _inputs(db: Session, daily_game: SeriesDailyGame, home: Team, away: Team, assets: dict, side_of: dict,
            counting: list[CountingGame], selections: dict, state: dict) -> HockeyGameInputs:
    """The credit of every starter's counting game, tagged and split by side. Credit of real periods that have not
    been published yet is filtered out later by `restrict`; credit of games other than the counting game never enters."""
    chosen = {a: s["game_id"] for a, s in ((int(k), v) for k, v in selections.items())}
    lines, attempts = loader._credits_for(db, {a: assets[a] for a in chosen}, sorted(set(chosen.values())), tag_games=True)
    lines = [l for l in lines if chosen.get(l.asset_id) == l.game_id]
    attempts = [a for a in attempts if chosen.get(a.asset_id) == a.game_id]
    sides = {}
    for key, team in (("home", home), ("away", away)):
        starters = tuple(sorted((a, assets[a].full_name) for a in state["lineup"][key]))
        sides[key] = SideInput(team_id=team.id, name=team.name, lines=tuple(l for l in lines if side_of.get(l.asset_id) == key),
                               attempts=tuple(a for a in attempts if side_of.get(a.asset_id) == key), starters=starters)
    return HockeyGameInputs(daily_game_id=daily_game.id, calendar_date=daily_game.calendar_date.isoformat(), home=sides["home"],
                            away=sides["away"], window_index=daily_game.window_index, week_start=pipeline.week_start(daily_game.calendar_date).isoformat(),
                            counting_games=tuple(counting))


def _provenance(prov: dict, state: dict, division: Division, counting: list[CountingGame], through: HockeyGameInputs,
                summary: dict, norm: progressive.PeriodNorm, k: int) -> dict:
    out = dict(prov)
    out["window_state"] = state
    out["wall_clock"] = division.wall_clock().to_dict()
    out["division"] = division_table(
        division, counting, [l for s in ("home", "away") for l in through.side(s).lines],
        [a for s in ("home", "away") for a in through.side(s).attempts],
        {"home": through.home.name, "away": through.away.name})
    out["box_score"] = box_score(through)
    out.update(summary)
    history = list(prov.get("normalization", {}).get("periods", []))
    history.append(norm.to_dict())
    raw_leader = "home" if norm.expected_margin > 0 else "away" if norm.expected_margin < 0 else None
    winner = "home" if norm.home_goals > norm.away_goals else "away" if norm.away_goals > norm.home_goals else None
    out["normalization"] = {
        "home_goals": norm.home_goals, "away_goals": norm.away_goals, "expected_margin": round(norm.expected_margin, 6),
        "noise": round(norm.noise, 6), "cap": NOISE_CAP, "tiebreak_side": norm.tiebreak_side,
        "upset": bool(k == PERIODS and raw_leader and winner and raw_leader != winner), "periods": history}
    out["periods_published"] = k
    out["inputs_digest"] = _digest(state, counting)
    return out


def _digest(state: dict, counting: list[CountingGame]) -> str:
    import hashlib
    import json
    payload = {"lineup": state["lineup"], "selections": state["selections"],
               "counting": [[c.asset_id, c.game_id, c.kickoff_at.isoformat()] for c in counting]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
