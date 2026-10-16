"""Season rollover: archive the finished season, reset what belongs to one season, and set up the next draft.

League Format (leagues.league_format) decides what carries over and how the next draft is ordered:
  * traditional_redraft  - rosters reset; draft order is randomized on draft day (the commissioner starts it).
  * future_pick_redraft  - rosters reset; snake draft in reverse order of the standings.
  * keeper               - owners pick keepers (up to keeper_limit, or the whole roster) before the commissioner's
                           deadline; teams that don't are trimmed automatically, fewest games played first, and everyone
                           not kept is released. Draft in reverse standings order, not a snake. Draft style
                           (keeper_draft_style): rookie draft only, rookies and refills combined, or a rookie draft then
                           a refill draft. A team that keeps its whole roster only gets the rookie draft.
  * dynasty              - whole rosters stay; a rookie draft in reverse standings order, not a snake.
Rookie drafts draw on players flagged Player.is_rookie; nothing sets that flag yet, so they can't run until it does.
A team that has to be under the roster limit before a draft (a full keeper or any Dynasty team) is frozen out of it until
it drops players: the "roster freeze". Other teams just get fewer picks, as many as they have room for.
Future Draft Pick Redraft is treated like Traditional for rosters (they reset), which was an assumption.

The archive (LeagueSeason) is a self-contained JSON snapshot so league history survives later schema changes.
Only snake-draft leagues roll over for now. Nothing here touches scoring or attribution.
"""

import datetime
import statistics

from sqlalchemy.orm import Session

from fantasy_sports.platform import scoring, series_standings
from fantasy_sports.platform.db.models.core import (
    BankedCredit, Draft, DraftableAsset, DraftPick, FreeAgencyTransaction, KeeperCandidate, KeeperSubmission, League,
    LeagueSeason, LeagueSeries, LineupEntry,
    MatchupRequest, RosterEntry, ScheduleSettings, SeriesDailyGame, SimulatorEvent, SimulatorGame, SimulatorReveal, Team,
    WaiverClaim, Week, WeeklyMatchup, WeeklyTeamScore,
)
from fantasy_sports.platform.league_features import draft_state, roster_limit

RESETS_ROSTERS = ("traditional_redraft", "future_pick_redraft")


class RolloverError(ValueError):
    pass


def _asset_name(a: DraftableAsset) -> str:
    if a.asset_type == "player":
        return a.player.full_name
    return f"{a.defense_group.real_team.name if a.defense_group.real_team else '?'} {a.defense_group.position_group}"


def final_standings(db: Session, league: League) -> list[dict]:
    """Every team best-first: [{rank, team_id, team_name, wins, losses, ties, ...}]."""
    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=league.id)}
    if league.series_length_days is not None:
        team_ids, series, games = series_standings.load_league(db, league.id)
        rows = series_standings.compute_standings(team_ids, series, games, rules=series_standings.rules_for(league))
        return [{"rank": r.rank, "team_id": r.team_id, "team_name": names[r.team_id], "wins": r.wins, "losses": r.losses,
                 "ties": r.ties, "goals_for": r.goals_for, "goals_against": r.goals_against, "points": r.points,
                 "win_pct": round(r.win_pct, 4)} for r in rows]
    rows = scoring.standings(db, league.id)
    return [{**r, "rank": i} for i, r in enumerate(rows, start=1)]


def season_champion(standings: list[dict]) -> int | None:
    """The team that won the season. These leagues have no playoffs yet, so it is the best record; when playoffs exist this is
    the one place to read their winner instead. The champion picks last in the next draft."""
    return standings[0]["team_id"] if standings else None


def draft_order(db: Session, league: League) -> list[int] | None:
    """Team ids in pick order for the next draft, or None when the order is random on draft day. Read from the archived
    season's final standings: the live standings are cleared by the rollover."""
    if league.league_format == "traditional_redraft":
        return None
    archive = db.query(LeagueSeason).filter_by(league_id=league.id, season_number=league.season_number - 1).one_or_none()
    if archive is None:
        raise RolloverError("The last season isn't archived, so there is no standings order to draft in")
    # Worst record first. Ties on wins and losses are already broken by goal differential (series_standings.RANK_KEYS).
    # Playoff results only matter for the champion, who picks last.
    order = [r["team_id"] for r in reversed(archive.data["standings"])]
    if archive.champion_team_id in order:
        order.remove(archive.champion_team_id)
        order.append(archive.champion_team_id)
    return order


def _snapshot(db: Session, league: League, standings: list[dict]) -> dict:
    teams = db.query(Team).filter_by(league_id=league.id).order_by(Team.id).all()
    team_ids = [t.id for t in teams]
    matchup_rows = db.query(WeeklyMatchup).filter_by(league_id=league.id).order_by(WeeklyMatchup.id).all()
    weeks = {w.id: w for w in db.query(Week).filter(Week.id.in_([m.week_id for m in matchup_rows] or [0]))}
    matchups = [{"week": weeks[m.week_id].week_number if m.week_id in weeks else None,
                 "season_year": weeks[m.week_id].season_year if m.week_id in weeks else None,
                 "team_a_id": m.team_a_id, "team_b_id": m.team_b_id, "team_a_score": m.team_a_score,
                 "team_b_score": m.team_b_score, "winner_team_id": m.winner_team_id} for m in matchup_rows]
    scores = [{"team_id": s.team_id, "week_id": s.week_id, "total_points": s.total_points}
              for s in db.query(WeeklyTeamScore).filter(WeeklyTeamScore.team_id.in_(team_ids or [0]))]
    rosters = {t.id: [{"asset_id": e.draftable_asset_id, "name": _asset_name(e.draftable_asset), "acquired_via": e.acquired_via,
                       "on_injured_list": e.on_injured_list}
                      for e in db.query(RosterEntry).filter_by(team_id=t.id).order_by(RosterEntry.id)] for t in teams}
    drafts = []
    for d in db.query(Draft).filter_by(league_id=league.id, season_number=league.season_number).order_by(Draft.id):
        drafts.append({"draft_id": d.id, "status": d.status, "is_snake": d.is_snake, "picks": [
            {"round": p.round, "pick_number": p.pick_number, "team_id": p.team_id, "asset_id": p.draftable_asset_id,
             "name": _asset_name(p.draftable_asset) if p.draftable_asset else None, "is_autopick": p.is_autopick}
            for p in db.query(DraftPick).filter_by(draft_id=d.id).order_by(DraftPick.pick_number)]})
    transactions = [{"team_id": x.team_id, "asset_id": x.draftable_asset_id, "kind": x.kind, "price": x.price,
                     "reason": x.reason, "at": x.created_at.isoformat() if x.created_at else None}
                    for x in db.query(FreeAgencyTransaction).filter_by(league_id=league.id).order_by(FreeAgencyTransaction.id)]
    series = []
    for s in db.query(LeagueSeries).filter_by(league_id=league.id).order_by(LeagueSeries.start_date, LeagueSeries.id):
        series.append({"team_a_id": s.team_a_id, "team_b_id": s.team_b_id, "start_date": str(s.start_date),
                       "end_date": str(s.end_date), "total_days": s.total_days, "games": [
                           {"date": str(g.calendar_date), "home_team_id": g.home_team_id, "away_team_id": g.away_team_id,
                            "home_score": g.home_score, "away_score": g.away_score, "status": g.status}
                           for g in db.query(SeriesDailyGame).filter_by(series_id=s.id)
                           .order_by(SeriesDailyGame.calendar_date, SeriesDailyGame.id)]})
    return {"teams": [{"id": t.id, "name": t.name, "owner_user_id": t.owner_user_id, "is_ai_owned": t.is_ai_owned} for t in teams],
            "standings": standings, "matchups": matchups, "weekly_scores": scores, "series": series,
            "rosters": rosters, "drafts": drafts, "transactions": transactions}


def _clear_season(db: Session, league_id: int, team_ids: list[int]) -> None:
    """Deletes what belongs to one season (the archive has already captured it), child rows first like league_lifecycle."""
    series_ids = [r[0] for r in db.query(LeagueSeries.id).filter_by(league_id=league_id)]
    game_ids = [r[0] for r in db.query(SeriesDailyGame.id).filter(SeriesDailyGame.series_id.in_(series_ids))] if series_ids else []
    sim_ids = [r[0] for r in db.query(SimulatorGame.id).filter(SimulatorGame.daily_game_id.in_(game_ids))] if game_ids else []
    if sim_ids:
        db.query(SimulatorReveal).filter(SimulatorReveal.simulator_game_id.in_(sim_ids)).delete(synchronize_session=False)
        db.query(SimulatorEvent).filter(SimulatorEvent.simulator_game_id.in_(sim_ids)).delete(synchronize_session=False)
        db.query(SimulatorGame).filter(SimulatorGame.id.in_(sim_ids)).delete(synchronize_session=False)
    db.query(BankedCredit).filter(BankedCredit.league_id == league_id).delete(synchronize_session=False)
    if series_ids:
        db.query(SeriesDailyGame).filter(SeriesDailyGame.series_id.in_(series_ids)).delete(synchronize_session=False)
        db.query(LeagueSeries).filter(LeagueSeries.id.in_(series_ids)).delete(synchronize_session=False)
    db.query(WeeklyMatchup).filter_by(league_id=league_id).delete(synchronize_session=False)
    db.query(MatchupRequest).filter_by(league_id=league_id).delete(synchronize_session=False)
    db.query(ScheduleSettings).filter_by(league_id=league_id).delete(synchronize_session=False)
    db.query(WaiverClaim).filter_by(league_id=league_id).delete(synchronize_session=False)
    if team_ids:
        db.query(LineupEntry).filter(LineupEntry.team_id.in_(team_ids)).delete(synchronize_session=False)
        db.query(WeeklyTeamScore).filter(WeeklyTeamScore.team_id.in_(team_ids)).delete(synchronize_session=False)


def draft_plan(league: League) -> list[str]:
    """The offseason drafts this league runs, in order (Draft.kind)."""
    if league.league_format == "dynasty":
        return ["rookie"]
    if league.league_format == "keeper":
        return {"rookie_resign": ["rookie"], "combined": ["combined"], "separate": ["rookie", "refill"]}[league.keeper_draft_style]
    return ["standard"]


def next_draft_kind(db: Session, league: League) -> str | None:
    """The next offseason draft still to run (started drafts count as run), or None when the plan is done."""
    done = {d.kind for d in db.query(Draft).filter_by(league_id=league.id, season_number=league.season_number)
            if d.status in ("in_progress", "complete")}
    return next((k for k in draft_plan(league) if k not in done), None)


def draft_is_snake(league: League) -> bool:
    return league.league_format in RESETS_ROSTERS


def kind_rounds(db: Session, league: League, kind: str, full_keeper: bool = False) -> int:
    """Rounds a team takes part in for this kind of draft. Full keepers (a whole roster kept, and all of Dynasty) get the
    rookie draft only."""
    rookie, refill = league.offseason_draft_rounds or 0, league.offseason_refill_rounds or 0
    if kind == "standard":
        return roster_limit(db, league)
    if kind == "rookie":
        return rookie
    if kind == "refill":
        return 0 if full_keeper else refill
    return rookie if full_keeper else rookie + refill


def after_draft_complete(db: Session, league: League) -> None:
    """Ends the offseason once the last draft of the plan is done. Does not commit."""
    if league.offseason and next_draft_kind(db, league) is None:
        league.offseason = False


def keeper_cap(db: Session, league: League) -> int:
    """The most players a team may keep: the commissioner's keeper limit, or its whole roster."""
    full = roster_limit(db, league)
    return min(full, league.keeper_limit) if league.keeper_limit is not None else full


def _candidates(db: Session, league: League, team_id: int) -> list[KeeperCandidate]:
    return (db.query(KeeperCandidate).filter_by(team_id=team_id, season_number=league.season_number)
            .order_by(KeeperCandidate.roster_order).all())


def is_full_keeper(db: Session, league: League, team_id: int) -> bool:
    """Dynasty teams, and Keeper teams that kept every player they had: the rookie draft is all they get."""
    if league.league_format == "dynasty":
        return True
    if league.league_format != "keeper":
        return False
    cands = _candidates(db, league, team_id)
    return bool(cands) and all(c.kept for c in cands)


def submit_keepers(db: Session, league: League, team: Team, asset_ids: list[int], now: datetime.datetime | None = None) -> None:
    """A Keeper team's chosen keepers. Everyone else on the roster is released when keepers close. Flushes."""
    now = now or datetime.datetime.utcnow()
    if league.league_format != "keeper" or not league.offseason:
        raise RolloverError("There is no keeper selection open for this league")
    process_keeper_deadline(db, league, now)
    if league.keepers_finalized:
        raise RolloverError("Keepers are closed")
    cands = {c.draftable_asset_id: c for c in _candidates(db, league, team.id)}
    chosen = set(asset_ids)
    if len(chosen) != len(asset_ids) or not chosen <= set(cands):
        raise RolloverError("Keepers have to be distinct players from this team's roster")
    cap = keeper_cap(db, league)
    if len(chosen) > cap:
        raise RolloverError(f"This league lets a team keep at most {cap} players")
    for aid, c in cands.items():
        c.kept = aid in chosen
    sub = db.query(KeeperSubmission).filter_by(team_id=team.id, season_number=league.season_number).one_or_none()
    if sub is None:
        db.add(KeeperSubmission(league_id=league.id, team_id=team.id, season_number=league.season_number,
                                submitted_at=now, auto=False))
    else:
        sub.submitted_at, sub.auto = now, False
    db.flush()


def _release(db: Session, team: Team, asset_id: int) -> None:
    from fantasy_sports.platform import pecking_order
    db.query(RosterEntry).filter_by(team_id=team.id, draftable_asset_id=asset_id).delete(synchronize_session=False)
    db.query(LineupEntry).filter_by(team_id=team.id, draftable_asset_id=asset_id).delete(synchronize_session=False)
    pecking_order.unrank_player(db, team, asset_id)


def _z_scores(values: list[float | None]) -> list[float]:
    """Standard deviations from the mean for each value; a missing value, or a pool with no spread, scores 0 (average)."""
    known = [v for v in values if v is not None]
    if len(known) < 2:
        return [0.0] * len(values)
    mean, sd = statistics.fmean(known), statistics.pstdev(known)
    return [0.0 if v is None or sd == 0 else (v - mean) / sd for v in values]


def ai_keeper_scores(db: Session, league: League) -> dict[int, float]:
    """KeeperCandidate id -> the AI's keeper score: standard deviations of age BELOW the mean (younger is better), plus
    standard deviations of points per game ABOVE the mean, plus standard deviations of games played ABOVE the mean, all
    measured across every player on a roster in the league. The three count equally."""
    cands = db.query(KeeperCandidate).filter_by(league_id=league.id, season_number=league.season_number).all()
    age_z = _z_scores([c.age for c in cands])
    ppg_z = _z_scores([c.points_per_game for c in cands])
    gp_z = _z_scores([float(c.games_played) for c in cands])
    return {c.id: -a + p + g for c, a, p, g in zip(cands, age_z, ppg_z, gp_z)}


def finalize_keepers(db: Session, league: League, now: datetime.datetime | None = None) -> None:
    """Closes keeper selection. A team that hasn't submitted keeps its most-played players up to the keeper limit (the ones
    with the fewest games played last season are trimmed); every player not kept is released. Flushes, does not commit."""
    now = now or datetime.datetime.utcnow()
    if league.keepers_finalized:
        return
    cap = keeper_cap(db, league)
    submitted = {s.team_id for s in db.query(KeeperSubmission).filter_by(league_id=league.id, season_number=league.season_number)}
    scores = ai_keeper_scores(db, league)
    for team in db.query(Team).filter_by(league_id=league.id).order_by(Team.id):
        cands = _candidates(db, league, team.id)
        if team.id not in submitted:
            if team.is_ai_owned:
                ranked = sorted(cands, key=lambda c: (-scores[c.id], c.roster_order))
            else:
                ranked = sorted(cands, key=lambda c: (-c.games_played, c.roster_order))
            keep = {c.id for c in ranked[:cap]}
            for c in cands:
                c.kept = c.id in keep
            db.add(KeeperSubmission(league_id=league.id, team_id=team.id, season_number=league.season_number,
                                    submitted_at=now, auto=True))
        for c in cands:
            if not c.kept:
                _release(db, team, c.draftable_asset_id)
    league.keepers_finalized = True
    db.flush()


def process_keeper_deadline(db: Session, league: League, now: datetime.datetime | None = None) -> bool:
    """Finalizes keepers if the commissioner's deadline has passed. There is no scheduler, so this runs whenever the
    league's keepers or draft are looked at. True if it just finalized."""
    now = now or datetime.datetime.utcnow()
    if league.offseason and not league.keepers_finalized and league.keeper_deadline and now >= league.keeper_deadline:
        finalize_keepers(db, league, now)
        return True
    return False


def championship_status(db: Session, league: League) -> tuple[bool, str]:
    """(complete, why not). A new season can only start once the championship is complete. These leagues have no playoffs yet,
    so there is no championship to complete and this stays False; when playoffs exist this is the one place that says when
    they are done (their winner is what season_champion should then return)."""
    return False, ("The championship isn't complete: the playoffs haven't been built yet, so a new season can't be started. "
                   "Start New Season unlocks once the championship is complete.")


def roll_over(db: Session, league: League, offseason_rounds: int | None = None, refill_rounds: int | None = None,
              today: datetime.date | None = None) -> LeagueSeason:
    """Archives the current season and opens the offseason. Flushes, does not commit."""
    if league.draft_type != "snake":
        raise RolloverError("Season rollover is only available for snake-draft leagues for now")
    if league.offseason:
        raise RolloverError("This league is already in the offseason")
    done, why = championship_status(db, league)
    if not done:
        raise RolloverError(why)
    if draft_state(db, league.id) != "complete":
        raise RolloverError("The league's draft has to finish before the season can roll over")
    fmt = league.league_format
    keeps = fmt not in RESETS_ROSTERS
    if keeps and (offseason_rounds is None or offseason_rounds < 1):
        raise RolloverError("Set how many rookie draft rounds the next draft has (at least 1)")
    needs_refill = fmt == "keeper" and league.keeper_draft_style in ("combined", "separate")
    if needs_refill and (refill_rounds is None or refill_rounds < 1):
        raise RolloverError("Set how many refill rounds the next draft has (at least 1) - this league's keeper draft style uses them")
    today = today or datetime.date.today()
    if fmt == "keeper" and (league.keeper_deadline is None or league.keeper_deadline <= datetime.datetime.utcnow()):
        raise RolloverError("Set the keeper deadline in League Settings first - it has to be a date in the future")

    standings = final_standings(db, league)
    teams = db.query(Team).filter_by(league_id=league.id).order_by(Team.id).all()
    team_ids = [t.id for t in teams]
    archive = LeagueSeason(league_id=league.id, season_number=league.season_number, league_format=fmt,
                           archived_at=datetime.datetime.utcnow(), champion_team_id=season_champion(standings),
                           data=_snapshot(db, league, standings))
    db.add(archive)
    stats, ages = {}, {}
    if fmt == "keeper":
        from fantasy_sports.platform.draft_stats import current_season_stats, last_completed_season_year
        from fantasy_sports.platform.player_age import age_on
        asset_ids = [r[0] for r in db.query(RosterEntry.draftable_asset_id).filter(RosterEntry.team_id.in_(team_ids or [0]))]
        # Always the season that just finished: if the new season's dates have begun, last season's games, not the new one's.
        stats = current_season_stats(db, asset_ids, season=last_completed_season_year(today))
        for a in db.query(DraftableAsset).filter(DraftableAsset.id.in_(asset_ids or [0])):
            if a.asset_type == "player":
                ages[a.id] = age_on(a.player.birth_date, today)
    _clear_season(db, league.id, team_ids)
    if not keeps and team_ids:
        db.query(RosterEntry).filter(RosterEntry.team_id.in_(team_ids)).delete(synchronize_session=False)
    league.season_number += 1
    league.offseason = True
    league.offseason_draft_rounds = offseason_rounds if keeps else None
    league.offseason_refill_rounds = refill_rounds if needs_refill else None
    if fmt != "keeper":
        league.keeper_deadline = None
    league.keepers_finalized = fmt != "keeper"
    if fmt == "keeper":
        for t in teams:
            for i, e in enumerate(db.query(RosterEntry).filter_by(team_id=t.id).order_by(RosterEntry.id)):
                s = stats.get(e.draftable_asset_id) or {"games": 0, "points": 0.0}
                db.add(KeeperCandidate(league_id=league.id, team_id=t.id, draftable_asset_id=e.draftable_asset_id,
                                       season_number=league.season_number, games_played=s["games"],
                                       points_per_game=round(s["points"] / s["games"], 3) if s["games"] else 0.0,
                                       age=ages.get(e.draftable_asset_id), kept=False, roster_order=i))
    db.flush()
    return archive


def offseason_draft_plan(db: Session, league: League, kind: str, order: list[int], snake: bool) -> list[tuple[int, int]]:
    """(team_id, round) pick slots, in order, for the next offseason draft. Each team takes part in as many rounds as it has
    roster room for. A team that kept its whole roster (and every Dynasty team) has to drop players first - the roster
    freeze - so it can take its rookie rounds; any other team just gets fewer picks. Raises RolloverError on a freeze."""
    limit = roster_limit(db, league)
    rounds_for: dict[int, int] = {}
    frozen = []
    for team_id in order:
        full = is_full_keeper(db, league, team_id)
        want = kind_rounds(db, league, kind, full)
        room = limit - db.query(RosterEntry).filter_by(team_id=team_id, on_injured_list=False).count()
        if full and room < want:
            frozen.append(f"{db.get(Team, team_id).name} ({limit - room} players, needs room for {want})")
        rounds_for[team_id] = max(0, min(want, room))
    if frozen:
        raise RolloverError("Roster freeze: these teams have to drop players before the draft - " + "; ".join(frozen))
    slots = []
    for r in range(1, max(rounds_for.values(), default=0) + 1):
        this_round = order if (r % 2 == 1 or not snake) else order[::-1]
        slots += [(t, r) for t in this_round if r <= rounds_for[t]]
    return slots


def future_picks(db: Session, league: League, team_id: int) -> dict:
    """A Future Draft Pick Redraft team's picks in the next draft, for its roster page: one per round. The pick positions
    are known once the season is archived (reverse standings, champion last, snake) or the draft has started."""
    from fantasy_sports.platform import pick_trading
    rounds = roster_limit(db, league)
    n = db.query(Team).filter_by(league_id=league.id).count()
    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=league.id)}
    season = pick_trading.next_draft_season(league)
    owners = pick_trading.owner_map(db, league.id, season)
    team_ids = sorted(names)
    order = None
    if league.offseason:
        try:
            order = draft_order(db, league)
        except RolloverError:
            order = None
    draft = (db.query(Draft).filter_by(league_id=league.id, season_number=league.season_number).order_by(Draft.id.desc()).first()
             if league.offseason else None)   # outside the offseason that draft is this season's, not the next one
    actual = {p.round: p.pick_number for p in db.query(DraftPick).filter_by(draft_id=draft.id, team_id=team_id)} if draft else {}
    picks = []
    for r in range(1, rounds + 1):
        for original in team_ids:
            if owners.get((r, original), original) != team_id:
                continue
            overall = None
            if draft is not None:       # the draft exists: the actual pick this slot became
                overall = next((p.pick_number for p in db.query(DraftPick).filter_by(draft_id=draft.id, round=r, team_id=team_id)), None)
            if overall is None and order and original in order:
                i = order.index(original)
                overall = (r - 1) * n + (i if r % 2 == 1 else n - 1 - i) + 1
            picks.append({"round": r, "pick_in_round": ((overall - 1) % n + 1) if overall else None, "overall": overall,
                          "original_team_id": original, "original_team_name": names[original], "own": original == team_id})
    return {"season_number": season, "order_set": order is not None or bool(actual), "picks": picks}


def pool_for_kind(assets: list[DraftableAsset], kind: str) -> list[DraftableAsset]:
    """The draftable pool of an offseason draft: rookies only for a rookie draft, everyone else for a refill draft."""
    if kind not in ("rookie", "refill"):
        return assets
    is_rookie = lambda a: a.asset_type == "player" and bool(a.player.is_rookie)
    return [a for a in assets if is_rookie(a) == (kind == "rookie")]
