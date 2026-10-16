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
        print(f"Replacements recorded: {state.get('subs') or 'none'}")
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
                print(f"  {name}: game {game.id} status={game.status} game_has_toi_rows={bool(rows)} his_toi={toi} "
                      f"sat_out={window_engine.sat_out(db, game, aid)}")
        db.rollback()
    return 0


if __name__ == "__main__":
    sys.exit(main())
