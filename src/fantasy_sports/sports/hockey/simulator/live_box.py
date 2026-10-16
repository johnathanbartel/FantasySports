"""
The live, unveiled box score of one NHL window matchup: a READ-ONLY transparency view (no spoiler veil, no writes).

It answers three questions while the games are still being played:

  * which real NHL games count for which starters, and why (candidates, the Best Matchup choice, locked or projected);
  * which real periods (player x real game x period 1..3) were assigned to which fantasy period by the chronological
    division, and what each real period's wall-clock / feed state is right now;
  * what credit every starter has earned in each of those real periods so far, straight from the stored credit rows,
    next to what the simulator has actually published.

Nothing here scores anything: the numbers are the stored credit lines (loader._credits_for) grouped by the division
that window_engine.tick builds. Where the live feed has run ahead of the published script, both are shown, so a gap
between "earned" and "published" is visible instead of hidden.
"""

from __future__ import annotations

import datetime
import types

from sqlalchemy.orm import Session

from fantasy_sports.platform import pecking_order as po
from fantasy_sports.platform.db.models.core import DraftableAsset, SeriesDailyGame, Team
from fantasy_sports.platform.simulator import service
from fantasy_sports.sports.hockey.models import GameEvent, Shot
from fantasy_sports.sports.hockey.simulator import loader, window_engine
from fantasy_sports.sports.hockey.simulator.division import REAL_PERIOD, Division, is_overtime, real_period_of
from fantasy_sports.sports.hockey.simulator.model import PERIODS, ROLES, CountingGame
from fantasy_sports.sports.hockey.simulator.normalize import derive_rng
from fantasy_sports.sports.hockey.simulator.timeline import filler_shots

ROLE_ORDER = ("shooter", "primary_assist", "secondary_assist", "blocker", "goalie")


def _slot_state(now: datetime.datetime, start: datetime.datetime) -> str:
    if now < start:
        return "not_started"
    return "in_progress" if now < start + REAL_PERIOD else "ended"


def _feed_period(db: Session, game_id: int) -> int:
    """Highest real period that has any stored play-by-play (0 when the feed is empty)."""
    row = db.query(GameEvent.period).filter(GameEvent.game_id == game_id).order_by(GameEvent.period.desc()).first()
    return int(row[0]) if row and row[0] is not None else 0


def _lineups(db: Session, teams: dict, rosters: dict, lineup: dict, games: dict, labels: dict, window_real: set) -> dict:
    """How the pecking order turned into starters, per side: every ranked player with his games in the window and whether he
    started (frozen lineup) or would start if the lineup were resolved right now, the vacancy reasons, and the rostered
    players who are not ranked at all (they can never start). Read-only; the rule is po.resolve_starters, unchanged."""
    out = {}
    for side, team in teams.items():
        roster = rosters[side]
        order = {g: [pid for pid in ids if pid in roster] for g, ids in po.load_order(db, team.id).items()}
        scheduled = frozenset(pid for pid, (_, pl) in roster.items() if pl.real_team_id in window_real)
        now_res = po.resolve_starters(order, scheduled, scheduled)
        frozen = set(lineup.get(side, []))

        def games_of(pl):
            return [{"date": g.kickoff_at.date().isoformat(),
                     "opponent": ("vs " + labels.get(g.away_team_id, "")) if g.home_team_id == pl.real_team_id else ("@ " + labels.get(g.home_team_id, "")),
                     "status": g.status}
                    for g in sorted(games.values(), key=lambda g: (g.kickoff_at, g.id)) if pl.real_team_id in (g.home_team_id, g.away_team_id)]

        ranked_ids = {pid for ids in order.values() for pid in ids}
        groups = []
        for group in po.GROUPS:
            rows = []
            for rank, pid in enumerate(order[group], start=1):
                asset_id, pl = roster[pid]
                rows.append({"rank": rank, "player": pl.full_name, "position": pl.position,
                             "real_team": labels.get(pl.real_team_id, ""), "games": games_of(pl),
                             "has_game": pid in scheduled, "frozen_starter": asset_id in frozen,
                             "starter_now": pid in now_res.starters.get(group, [])})
            groups.append({"group": group, "need": po.STARTERS[group], "depth": po.DEPTH[group], "ranked": rows,
                           "vacancies": [v.reason for v in now_res.vacancies if v.position_group == group]})
        out[side] = {
            "team": team.name, "roster_size": len(roster), "ranked_count": len(ranked_ids), "groups": groups,
            "frozen_starters": len(frozen),
            "unranked": [{"player": pl.full_name, "position": pl.position, "real_team": labels.get(pl.real_team_id, ""),
                          "games": len(games_of(pl))} for pid, (_, pl) in sorted(roster.items()) if pid not in ranked_ids]}
    return out


def live_box_score(db: Session, daily_game: SeriesDailyGame, now_utc: datetime.datetime) -> dict:
    if daily_game.window_index is None or daily_game.calendar_date_end is None:
        raise ValueError("The live box score is for NHL window matchups only")
    now = window_engine._utc_to_local(now_utc)
    league_id = daily_game.series.league_id
    universe_id = window_engine._universe_id(db, league_id)
    home, away = db.get(Team, daily_game.home_team_id), db.get(Team, daily_game.away_team_id)
    games = {g.id: g for g in window_engine.window_games(db, universe_id, daily_game.calendar_date, daily_game.calendar_date_end)}
    labels = loader._team_labels(db, [t for g in games.values() for t in (g.home_team_id, g.away_team_id)])
    rosters = {"home": po.roster_players(db, home.id), "away": po.roster_players(db, away.id)}
    roster_teams = {pl.real_team_id for r in rosters.values() for _, (_, pl) in r.items() if pl.real_team_id}
    relevant = [g for g in games.values() if g.home_team_id in roster_teams or g.away_team_id in roster_teams]
    names = {"home": home.name, "away": away.name}
    out: dict = {
        "daily_game_id": daily_game.id, "window_index": daily_game.window_index,
        "window": {"start": daily_game.calendar_date.isoformat(), "end": daily_game.calendar_date_end.isoformat()},
        "now_local": now.isoformat(timespec="seconds"), "teams": names, "status": daily_game.status,
        "scoreboard": {"home": daily_game.home_score, "away": daily_game.away_score},
        "real_games_in_window": len(games), "real_games_relevant": len(relevant),
    }
    if not relevant:
        out.update(state="no_games", note="No stored real NHL games in this window involve either roster.", players=[], periods=[])
        return out

    sim = service.get_sim(db, daily_game.id)
    prov = dict(sim.provenance or {}) if sim is not None else {}
    wstate = prov.get("window_state")
    first_kickoff = min(g.kickoff_at for g in relevant)
    out["first_kickoff"] = first_kickoff.isoformat()
    if wstate:
        lineup, stored_sel, frozen = wstate["lineup"], dict(wstate.get("selections") or {}), True
    else:
        window_real = {t for g in relevant for t in (g.home_team_id, g.away_team_id)}
        lineup = {"home": window_engine.freeze_lineup(db, home, window_real), "away": window_engine.freeze_lineup(db, away, window_real)}
        stored_sel, frozen = {}, False
    out["lineup_frozen"] = frozen
    out["ingest"] = prov.get("ingest") or {}
    out["real_games"] = [{
        "game_id": g.id, "kickoff": g.kickoff_at.isoformat(), "matchup": f"{labels.get(g.away_team_id, '')} @ {labels.get(g.home_team_id, '')}",
        "status": g.status, "events_stored": db.query(GameEvent.id).filter_by(game_id=g.id).count(),
        "shots_stored": db.query(Shot.id).filter_by(game_id=g.id).count(), "feed_period": _feed_period(db, g.id)}
        for g in sorted(relevant, key=lambda g: (g.kickoff_at, g.id))]
    out["lineups"] = _lineups(db, {"home": home, "away": away}, rosters, lineup, games, labels,
                              {t for g in relevant for t in (g.home_team_id, g.away_team_id)})
    out["published_periods"] = (sim.published_periods or 0) if sim is not None else 0
    out["sim_status"] = sim.status if sim is not None else None

    side_of, assets, real_team_of = {}, {}, {}
    left_roster: dict[str, list[str]] = {"home": [], "away": []}
    for side in ("home", "away"):
        by_asset = {aid: pl for _, (aid, pl) in rosters[side].items()}
        for aid in lineup[side]:
            pl = window_engine.frozen_player(db, by_asset, aid)
            if pl is None:
                continue
            if aid not in by_asset:
                left_roster[side].append(pl.full_name)
            side_of[aid], assets[aid], real_team_of[aid] = side, pl, pl.real_team_id

    counting: list[CountingGame] = []
    chosen: dict[int, int] = {}
    pick: dict[int, dict] = {}
    # Frozen starters the matchup no longer counts. window_engine.tick only counts a frozen starter who is still on the
    # team's active roster and has a candidate game, so a starter who was dropped, traded or put on the injured list after the
    # lock (or whose game left the stored schedule) silently shrinks the matchup. Say so, by name.
    dropped: dict[str, list[dict]] = {"home": [], "away": []}
    for side in ("home", "away"):
        for aid in lineup[side]:
            if aid not in assets:
                asset = db.get(DraftableAsset, aid)
                dropped[side].append({"player": asset.player.full_name if asset is not None and asset.player is not None else f"asset {aid}",
                                      "reason": "no longer exists as a player"})
    for aid in sorted(assets):
        cands = [g for g in relevant if real_team_of[aid] in (g.home_team_id, g.away_team_id)]
        if not cands:
            pick[aid] = {"candidates": [], "source": "no_games", "state": "no games in this window"}
            dropped[side_of[aid]].append({"player": assets[aid].full_name, "reason": "no stored real game in this window"})
            continue
        lock_at = min(g.kickoff_at for g in cands)
        rec, state = stored_sel.get(str(aid)), "locked (stored)"
        if rec is None and (len(cands) == 1 or now >= lock_at):
            rec, state = window_engine.select_game(db, universe_id, aid, cands, real_team_of[aid]), "locked (decided now, not yet stored)"
        if rec is None:
            state = "projected on his earliest game; locks at the kickoff"
        game = games[rec["game_id"]] if rec else min(cands, key=lambda g: (g.kickoff_at, g.id))
        at_home = real_team_of[aid] == game.home_team_id
        label = f"vs {labels.get(game.away_team_id, '')}" if at_home else f"@ {labels.get(game.home_team_id, '')}"
        counting.append(CountingGame(aid, assets[aid].full_name, side_of[aid], game.id, game.kickoff_at, label, assets[aid].position or ""))
        chosen[aid] = game.id
        pick[aid] = {
            "state": state, "source": rec["source"] if rec else "projected", "lock_at": lock_at.isoformat(),
            "opponent_points": rec.get("opponent_points") if rec else None,
            "candidates": [{"game_id": g.id, "kickoff": g.kickoff_at.isoformat(), "chosen": g.id == game.id,
                            "opponent": labels.get(g.away_team_id if real_team_of[aid] == g.home_team_id else g.home_team_id, "")}
                           for g in sorted(cands, key=lambda g: (g.kickoff_at, g.id))]}
    for side in ("home", "away"):
        out["lineups"][side]["dropped_from_matchup"] = dropped[side]
        out["lineups"][side]["left_roster_still_counted"] = left_roster[side]
    if not counting:
        out.update(state="no_counting_games", note="No starter has a game in this window.", players=[], periods=[])
        return out

    division = Division.build(counting)
    n = len(counting)
    lines, attempts = loader._credits_for(db, {a: assets[a] for a in chosen}, sorted(set(chosen.values())), tag_games=True)
    lines = [l for l in lines if chosen.get(l.asset_id) == l.game_id]
    attempts = [a for a in attempts if chosen.get(a.asset_id) == a.game_id]

    # points per (asset, real period), by role, plus goal / shot counts
    cell: dict[tuple[int, int], dict] = {}
    for l in lines:
        c = cell.setdefault((l.asset_id, real_period_of(l.real_second)), {"points": 0.0, "by_role": {}, "goals": 0, "shots": 0, "ot": False})
        c["points"] += l.points
        c["by_role"][l.role] = c["by_role"].get(l.role, 0.0) + l.points
        c["goals"] += 1 if (l.role == "shooter" and l.is_goal) else 0
        c["ot"] = c["ot"] or is_overtime(l.real_second)
    for a in attempts:
        c = cell.setdefault((a.asset_id, real_period_of(a.real_second)), {"points": 0.0, "by_role": {}, "goals": 0, "shots": 0, "ot": False})
        c["shots"] += 1
        c["ot"] = c["ot"] or is_overtime(a.real_second)
    for c in cell.values():
        c["shots"] += c["goals"]
    # The filler shots (seeded, fabricated for realism): the very ones the simulator publishes, so rows and scoreboard agree.
    seed = prov.get("seed", daily_game.id)
    shape = types.SimpleNamespace(counting_games=tuple(counting), home=types.SimpleNamespace(name=names["home"]),
                                  away=types.SimpleNamespace(name=names["away"]))
    filler: dict[tuple[int, int], int] = {}
    for k in range(1, PERIODS + 1):
        for raw in filler_shots(shape, division, k, derive_rng(seed, daily_game.id, f"filler:{k}")):
            key = (raw.detail["asset_id"], raw.detail["real_period"])
            filler[key] = filler.get(key, 0) + 1
            c = cell.setdefault(key, {"points": 0.0, "by_role": {}, "goals": 0, "shots": 0, "ot": False})
            c["shots"] += 1

    info = {c.asset_id: c for c in counting}
    published = out["published_periods"]
    begun = sum(1 for s in division.slots if s.start <= now)
    periods = []
    for k, chunk in enumerate(division.chunks, start=1):
        rows = []
        side_pts = {"home": 0.0, "away": 0.0}
        blockers = []
        for s in chunk:
            g, cg = games[s.game_id], info[s.asset_id]
            c = cell.get((s.asset_id, s.period), {"points": 0.0, "by_role": {}, "goals": 0, "shots": 0, "ot": False})
            side_pts[cg.side] += c["points"]
            nominal = _slot_state(now, s.start)
            complete = window_engine.real_period_complete(db, g, s.period)
            rows.append({
                "asset_id": s.asset_id, "player": cg.player_name, "side": cg.side, "team": names[cg.side],
                "game_id": s.game_id, "game": cg.opponent, "game_date": cg.kickoff_at.date().isoformat(),
                "game_status": g.status, "real_period": s.period, "nominal_start": s.start.isoformat(),
                "nominal_state": nominal, "feed_period": _feed_period(db, s.game_id), "feed_complete": complete,
                "ot": c["ot"], "points": round(c["points"], 4), "by_role": {r: round(v, 4) for r, v in c["by_role"].items()},
                "shots": c["shots"], "filler_shots": filler.get((s.asset_id, s.period), 0), "goals": c["goals"], "projected": pick[s.asset_id]["state"].startswith("projected")})
        if k > published:
            if begun < k * n:
                blockers.append(f"membership not certain yet: {begun} of the {k * n} real periods needed have begun")
            late = sum(1 for s in chunk if now < s.start + REAL_PERIOD)
            if late:
                blockers.append(f"{late} of its {len(chunk)} real periods have not nominally ended")
            lacking = sum(1 for s in chunk if not window_engine.real_period_complete(db, games[s.game_id], s.period))
            if lacking:
                blockers.append(f"{lacking} of its real periods are not complete in the stored feed")
            if k > published + 1:
                blockers.append(f"period {published + 1} must publish first")
        periods.append({
            "fantasy_period": k, "real_periods": len(rows), "published": k <= published, "waiting_on": blockers,
            "credit": {"home": round(side_pts["home"], 4), "away": round(side_pts["away"], 4)}, "rows": rows})

    # what the simulator has published so far, per fantasy period
    norm = (prov.get("normalization") or {})
    hist = {p["period"]: p for p in (norm.get("periods") or [])}
    for p in periods:
        h = hist.get(p["fantasy_period"])
        if h:
            p["published_goals"] = {"home": h["add_home"], "away": h["add_away"], "home_total": h["home_goals"],
                                    "away_total": h["away_goals"], "expected_margin": h["expected_margin"], "noise": h["noise"],
                                    "ratcheted": h["ratcheted"]}

    # per-player rows: credit by fantasy period, live total, and the published box-score total
    stored_box = {(side, r["asset_id"]): r["points"] for side in ("home", "away") for r in (prov.get("box_score") or {}).get(side, [])}
    k_of = {(s.asset_id, s.period): s.fantasy_period for s in division.slots}
    players = []
    for aid in sorted(info, key=lambda a: (info[a].side != "home", info[a].player_name)):
        by_k = [0.0] * PERIODS
        by_role: dict[str, float] = {}
        for (a, rp), c in cell.items():
            if a == aid:
                by_k[k_of[(a, rp)] - 1] += c["points"]
                for r, v in c["by_role"].items():
                    by_role[r] = by_role.get(r, 0.0) + v
        total = sum(by_k)
        published_total = sum(by_k[: min(published, PERIODS)])
        stored = stored_box.get((info[aid].side, aid), 0.0)
        players.append({
            "asset_id": aid, "player": info[aid].player_name, "position": assets[aid].position, "side": info[aid].side,
            "team": names[info[aid].side], "selection": pick[aid],
            "by_fantasy_period": [round(v, 4) for v in by_k], "total": round(total, 4),
            "by_role": {r: round(by_role[r], 4) for r in ROLE_ORDER if r in by_role},
            "published_total": round(stored, 4), "expected_published": round(published_total, 4),
            "ties_out": abs(stored - published_total) < 1e-3})

    shots = service.live_shots(sim, service._events(db, sim), now_utc) if sim is not None else (0, 0)
    out.update(
        shots_on_goal={"home": shots[0], "away": shots[1]},
        engine={"last_ran_utc": prov.get("ticked_at"), "error": prov.get("engine_error"),
                "estimated_shots_stored": len(prov.get("live_shots") or [])},
        state="live", players=players, periods=periods, counting_games=n, real_periods_pooled=division.pooled,
        chunk_size=division.chunk_size, real_periods_begun=begun,
        side_credit={s: round(sum(p["credit"][s] for p in periods), 4) for s in ("home", "away")},
        normalization={k: v for k, v in norm.items() if k != "periods"} if norm else None,
        notes=[
            "Credit is read from stored credit rows as the feed is ingested; a real period still in progress shows what has arrived so far.",
            "A fantasy period is published only after all its real periods have nominally ended and the stored feed shows each complete.",
            "'Published' columns are what the simulator has actually written; 'Earned' is the live credit. A gap is the unpublished part.",
        ])
    return out
