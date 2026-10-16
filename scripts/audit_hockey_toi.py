"""
Read-only: for one NHL window game, every starter's counting game, whether that game is final, whether its ice times are stored,
the starter's stored ice time, and whether the engine counts him as having sat out. Also lists the replacements the engine made.

    .\\.venv-api\\Scripts\\python.exe scripts/audit_hockey_toi.py --game 1849

`--game` is a daily game id (a window game). The database is whatever DATABASE_URL points at (this script does not read .env).
On PostgreSQL the transaction is opened READ ONLY, so nothing can be written.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def explain(db, dg, state, side, asset_id) -> None:
    """Why no replacement was (or would be) found for a starter who sat out: every ranked player in his group, with the reason he is
    skipped or accepted. Read-only."""
    from fantasy_sports.platform import pecking_order as po
    from fantasy_sports.platform.db.models.core import Team
    from fantasy_sports.sports.hockey.models import PlayerGameToi
    from fantasy_sports.sports.hockey.simulator import window_engine as we

    team = db.get(Team, dg.home_team_id if side == "home" else dg.away_team_id)
    universe_id = we._universe_id(db, dg.series.league_id)
    games = {g.id: g for g in we.window_games(db, universe_id, dg.calendar_date, dg.calendar_date_end)}
    roster = po.roster_players(db, team.id)
    by_asset = {aid: pl for _, (aid, pl) in roster.items()}
    window_real = {t for g in games.values() for t in (g.home_team_id, g.away_team_id)}
    order = po.load_order(db, team.id)
    player = we.frozen_player(db, by_asset, asset_id)
    group = next((g for g in po.GROUPS if player.id in order[g]), po.GROUP_BY_POSITION.get(player.position))
    taken = set(state["lineup"][side])
    print(f"      pecking order {group} of {team.name}: {[roster[p][1].full_name for p in order[group] if p in roster] or 'EMPTY'}")
    for pid in order[group]:
        if pid not in roster:
            print(f"      - player {pid}: not on the active roster (dropped or on the injured list)")
            continue
        aid, pl = roster[pid]
        if aid in taken:
            why = "already in the lineup"
        elif pl.real_team_id not in window_real:
            why = "his real team has no game in the window"
        else:
            import datetime
            from fantasy_sports.platform.simulator import wallclock
            now = wallclock.utc_to_local(datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), we.CLOCK_TIMEZONE)
            relevant = list(games.values())
            game = we._counting_game_of(db, universe_id, aid, pl, relevant, games, dict(state["selections"]), now)
            if game is None:
                why = "his counting game is not decided yet: ok, would be the replacement"
            else:
                rows = dict(db.query(PlayerGameToi.draftable_asset_id, PlayerGameToi.toi_seconds).filter_by(game_id=game.id).all())
                detail = f"game {game.id} status={game.status} toi={rows.get(aid)} game_has_toi_rows={bool(rows)}"
                why = f"{detail}: " + ("SAT OUT TOO, skipped" if we.sat_out(db, game, aid) else "ok, would be the replacement")
        print(f"      - {pl.full_name}: {why}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game", type=int, required=True, help="daily (window) game id")
    args = parser.parse_args(argv)

    from sqlalchemy import text

    from fantasy_sports.platform.db.models.core import DraftableAsset, Game, SeriesDailyGame
    from fantasy_sports.platform.db.session import SessionLocal, engine
    from fantasy_sports.platform.simulator import service
    from fantasy_sports.sports.hockey.models import PlayerGameToi
    from fantasy_sports.sports.hockey.simulator import window_engine

    url = engine.url
    print(f"Database: {url.drivername} host={url.host or '(local file)'} name={url.database}")
    with SessionLocal() as db:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        dg = db.get(SeriesDailyGame, args.game)
        sim = service.get_sim(db, args.game) if dg is not None else None
        if dg is None or sim is None or not (sim.provenance or {}).get("window_state"):
            print(f"No simulated window game {args.game}.")
            return 2
        state = sim.provenance["window_state"]
        print(f"Game #{dg.id} window {dg.calendar_date}..{dg.calendar_date_end}, published periods {sim.published_periods or 0}")
        print(f"Replacements recorded: {len(state.get('subs') or [])}")
        universe_id = window_engine._universe_id(db, dg.series.league_id)
        window = window_engine.window_games(db, universe_id, dg.calendar_date, dg.calendar_date_end)

        def label(asset_id):
            a = db.get(DraftableAsset, asset_id)
            return a.player.full_name if a is not None and a.player is not None else f"asset {asset_id}"

        for sub in state.get("subs") or []:
            out_asset = db.get(DraftableAsset, sub["out"])
            team_id = out_asset.player.real_team_id if out_asset is not None and out_asset.player is not None else None
            print(f"  {sub['side']}: {label(sub['out'])} OUT -> {label(sub['in'])} IN ({sub['mode']}, counting game {sub['game_id']}, at {sub['at']})")
            for g in window:
                if team_id in (g.home_team_id, g.away_team_id):
                    rows = dict(db.query(PlayerGameToi.draftable_asset_id, PlayerGameToi.toi_seconds).filter_by(game_id=g.id).all())
                    mark = "  <- counting game" if g.id == sub["game_id"] else ""
                    print(f"      {label(sub['out'])} in game {g.id} ({g.kickoff_at:%m-%d %H:%M}, {g.status}): toi={rows.get(sub['out'])}{mark}")
        for side in ("home", "away"):
            print(f"\n{side}:")
            for aid in state["lineup"][side]:
                asset = db.get(DraftableAsset, aid)
                name = asset.player.full_name if asset is not None and asset.player is not None else f"asset {aid}"
                rec = (state.get("selections") or {}).get(str(aid))
                game = db.get(Game, rec["game_id"]) if rec else None
                if game is None:
                    print(f"  {name}: no counting game chosen yet")
                    continue
                rows = dict(db.query(PlayerGameToi.draftable_asset_id, PlayerGameToi.toi_seconds).filter_by(game_id=game.id).all())
                toi = rows.get(aid)
                out = window_engine.sat_out(db, game, aid)
                print(f"  {name}: game {game.id} status={game.status} game_has_toi_rows={bool(rows)} his_toi={toi} sat_out={out}")
                if out:
                    explain(db, dg, state, side, aid)
        db.rollback()
    return 0


if __name__ == "__main__":
    sys.exit(main())
