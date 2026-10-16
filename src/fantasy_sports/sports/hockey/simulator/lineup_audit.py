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

Nothing here writes anything.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from fantasy_sports.platform import pecking_order as po
from fantasy_sports.platform.db.models.core import DraftableAsset, LeagueSeries, SeriesDailyGame, Team
from fantasy_sports.platform.simulator import service
from fantasy_sports.sports.hockey.simulator import window_engine


def _names(db: Session, asset_ids) -> list[str]:
    out = []
    for aid in sorted(asset_ids):
        asset = db.get(DraftableAsset, aid)
        out.append(asset.player.full_name if asset is not None and asset.player is not None else f"asset {aid}")
    return out


def lineup_audit(db: Session, league_id: int, only_game_id: int | None = None) -> list[dict]:
    universe_id = window_engine._universe_id(db, league_id)
    rows = (db.query(SeriesDailyGame).join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
            .filter(LeagueSeries.league_id == league_id, SeriesDailyGame.window_index.isnot(None))
            .order_by(SeriesDailyGame.calendar_date, SeriesDailyGame.id).all())
    if only_game_id is not None:
        rows = [r for r in rows if r.id == only_game_id]
    out = []
    for dg in rows:
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
        out.append({"daily_game_id": dg.id, "window_index": dg.window_index, "start": dg.calendar_date.isoformat(),
                    "end": (dg.calendar_date_end or dg.calendar_date).isoformat(), "game_status": dg.status,
                    "published_periods": published, "schedule_verified_at_freeze": verified, "stored_games_in_window": len(games),
                    "repaired": state.get("lineup_repaired"), "status": status, "sides": sides})
    return out
