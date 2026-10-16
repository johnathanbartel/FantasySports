"""
Read-only: how many goals the value model gives for the value teams actually create, and the VALUE_SCALE that would give a target average.

    .\\.venv-api\\Scripts\\python.exe scripts/calibrate_hockey_value_scale.py --league 9 [--target-goals 3.0]

Looks at every NHL window game of the league that was simulated by the value model (provenance model = "value") and reads the net value and
the goals of its last published period. The suggested scale is target goals / mean net value per side. The database is whatever DATABASE_URL
points at (this script does not read .env); on PostgreSQL the transaction is opened READ ONLY. Changing VALUE_SCALE in
src/fantasy_sports/sports/hockey/simulator/model.py is a scoring decision for the owner; this only measures.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--league", type=int, required=True, help="league id (not a game id)")
    parser.add_argument("--target-goals", type=float, default=3.0, help="average goals per side you want (default 3.0, the NHL average)")
    args = parser.parse_args(argv)

    from sqlalchemy import text

    from fantasy_sports.platform.db.models.core import LeagueSeries, SeriesDailyGame, SimulatorGame
    from fantasy_sports.platform.db.session import SessionLocal, engine
    from fantasy_sports.sports.hockey.simulator.model import VALUE_SCALE

    url = engine.url
    print(f"Database: {url.drivername} host={url.host or '(local file)'} name={url.database}")
    nets, goals, played = [], [], 0
    with SessionLocal() as db:
        if engine.dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        rows = (db.query(SimulatorGame).join(SeriesDailyGame, SeriesDailyGame.id == SimulatorGame.daily_game_id)
                .join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
                .filter(LeagueSeries.league_id == args.league, SeriesDailyGame.window_index.isnot(None)).all())
        for sim in rows:
            prov = sim.provenance or {}
            if prov.get("model") != "value" or not prov.get("value") or (sim.published_periods or 0) < 3:
                continue
            played += 1
            for side in ("home", "away"):
                nets.append(prov["value"]["net"][side])
                goals.append(prov["normalization"][f"{side}_goals"])
        db.rollback()
    if not played:
        print("No finished window game simulated by the value model in this league yet.")
        return 0
    mean_net, mean_goals = statistics.mean(nets), statistics.mean(goals)
    print(f"{played} finished games ({len(nets)} sides): mean net value {mean_net:.3f}, mean goals {mean_goals:.2f}, "
          f"goals range {min(goals)} to {max(goals)}, current VALUE_SCALE {VALUE_SCALE}")
    if mean_net > 0:
        print(f"For {args.target_goals:.1f} goals a side on average, VALUE_SCALE would be about {args.target_goals / mean_net:.2f}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
