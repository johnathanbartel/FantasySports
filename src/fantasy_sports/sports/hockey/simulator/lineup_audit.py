"""
League-wide check of every NHL window game's lineup against the pecking order (READ-ONLY).

For each window game it compares the lineup the engine froze with the lineup the pecking order gives on the schedule stored
NOW (po.resolve_starters, the same rule the engine uses). A frozen lineup that is smaller or different because the games of
the window were not all stored when it froze shows up here by name, together with what can still be done about it:

  * ok                 the frozen lineup is what the pecking order gives on the full schedule
  * not started        no simulation yet; the lineup will be frozen on the full schedule
  * repairs on sync    frozen on a possibly partial schedule and nothing is published yet: the engine resolves it again once
  * locked, differs    frozen on a possibly partial schedule and periods are already published: published events never change
  * frozen, differs    frozen on the full schedule; today's pecking order differs only because it was edited since (not an error)
  * old scoring model  periods already published under the earlier fixed-total scoring (before the value model); the lineup may be fine

`audit` is read-only. `resimulate_locked` is the one operation that deletes (a game's simulation, so it can be simulated again);
it only touches games the audit calls "locked, differs", does nothing unless `execute` is true, and is run by an operator from
scripts/resimulate_hockey_games.py (never from the web app).
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from fantasy_sports.platform import pecking_order as po
from fantasy_sports.platform.db.models.core import DraftableAsset, LeagueSeries, SeriesDailyGame, SimulatorGame, Team
from fantasy_sports.platform.simulator import service
from fantasy_sports.sports.hockey.simulator import window_engine


def _names(db: Session, asset_ids) -> list[str]:
    out = []
    for aid in sorted(asset_ids):
        asset = db.get(DraftableAsset, aid)
        out.append(asset.player.full_name if asset is not None and asset.player is not None else f"asset {aid}")
    return out


def lineup_audit(db: Session, league_id: int, only_game_id: int | None = None, since: datetime.date | None = None,
                 progress=None, started_only: bool = False) -> list[dict]:
    """One entry per window game of the league (or only `only_game_id`; or only windows ending on or after `since`). Read-only.
    `progress(done, total, daily_game_id)` is called after each game: the audit reads a lot per game, which is slow on a remote database.
    A league's whole season of window rows exists from the day its schedule is generated; `started_only` keeps just the games that have a
    simulation, the only ones whose frozen lineup can be wrong."""
    universe_id = window_engine._universe_id(db, league_id)
    rows = (db.query(SeriesDailyGame).join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
            .filter(LeagueSeries.league_id == league_id, SeriesDailyGame.window_index.isnot(None))
            .order_by(SeriesDailyGame.calendar_date, SeriesDailyGame.id).all())
    if only_game_id is not None:
        rows = [r for r in rows if r.id == only_game_id]
    if since is not None:
        rows = [r for r in rows if (r.calendar_date_end or r.calendar_date) >= since]
    if started_only and rows:
        have_sim = {r[0] for r in db.query(SimulatorGame.daily_game_id).filter(SimulatorGame.daily_game_id.in_([r.id for r in rows]))}
        rows = [r for r in rows if r.id in have_sim]
    out = []
    for index, dg in enumerate(rows, start=1):
        home, away = db.get(Team, dg.home_team_id), db.get(Team, dg.away_team_id)
        games = window_engine.window_games(db, universe_id, dg.calendar_date, dg.calendar_date_end or dg.calendar_date)
        rosters = {"home": po.roster_players(db, home.id), "away": po.roster_players(db, away.id)}
        roster_teams = {pl.real_team_id for r in rosters.values() for _, (_, pl) in r.items() if pl.real_team_id}
        relevant = [g for g in games if g.home_team_id in roster_teams or g.away_team_id in roster_teams]
        window_real = {t for g in relevant for t in (g.home_team_id, g.away_team_id)}
        sim = service.get_sim(db, dg.id)
        state = ((sim.provenance or {}).get("window_state") if sim is not None else None) or {}
        published = (sim.published_periods or 0) if sim is not None else 0
        verified = bool(state.get("schedule_verified"))
        sides = {}
        differs = False
        for side, team in (("home", home), ("away", away)):
            now_ids = set(window_engine.freeze_lineup(db, team, window_real)) if relevant else set()
            frozen_ids = set(state.get("lineup", {}).get(side, [])) if state else None
            entry = {"team": team.name, "would_start_now": len(now_ids), "frozen": None if frozen_ids is None else len(frozen_ids)}
            if frozen_ids is not None:
                entry["missing_from_frozen"] = _names(db, now_ids - frozen_ids)
                entry["only_in_frozen"] = _names(db, frozen_ids - now_ids)
                differs = differs or frozen_ids != now_ids
            sides[side] = entry
        if sim is None or not state:
            status = "not started"
        elif not differs:
            status = "ok"
        elif verified:
            status = "frozen, differs"
        elif published == 0:
            status = "repairs on sync"
        else:
            status = "locked, differs"
        if status in ("ok", "frozen, differs") and published > 0 and (sim.provenance or {}).get("model") != "value":
            status = "old scoring model"
        out.append({"daily_game_id": dg.id, "window_index": dg.window_index, "start": dg.calendar_date.isoformat(),
                    "end": (dg.calendar_date_end or dg.calendar_date).isoformat(), "game_status": dg.status,
                    "published_periods": published, "schedule_verified_at_freeze": verified, "stored_games_in_window": len(games),
                    "repaired": state.get("lineup_repaired"), "status": status, "sides": sides})
        if progress is not None:
            progress(index, len(rows), dg.id)
    return out


def resimulate_locked(db: Session, league_id: int, game_ids: list[int] | None = None, execute: bool = False,
                      since: datetime.date | None = None, progress=None, include_old_model: bool = False) -> list[dict]:
    """Void the simulation of every window game the audit calls "locked, differs" (or only those of `game_ids`; with
    `include_old_model` also games still on the earlier scoring model), so the next
    window-engine sync simulates it again from the stored feed on a lineup resolved on the full schedule. A dry run (the default)
    only reports. Games in any other state are never touched. Flushes, does not commit. Returns what was (or would be) done."""
    out = []
    for entry in lineup_audit(db, league_id, since=since, progress=progress, started_only=True):
        if game_ids is not None and entry["daily_game_id"] not in game_ids:
            continue
        if entry["status"] != "locked, differs" and not (include_old_model and entry["status"] == "old scoring model"):
            out.append({"daily_game_id": entry["daily_game_id"], "action": f"left alone ({entry['status']})", "sides": entry["sides"]})
            continue
        if execute:
            dg = db.get(SeriesDailyGame, entry["daily_game_id"])
            service.reset_simulation(db, dg)
        out.append({"daily_game_id": entry["daily_game_id"], "action": "voided" if execute else "would be voided", "sides": entry["sides"],
                    "published_periods": entry["published_periods"]})
    return out
