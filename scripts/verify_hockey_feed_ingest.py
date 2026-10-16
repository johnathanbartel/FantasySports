"""
Checks the NHL feed ingest (platform/nhl_daily_sync.ingest_daily_slate_if_needed) against a fake NHL API: one game whose
play-by-play cannot be read must not stop the others from being stored, a game whose plays have not changed is left alone,
a failed schedule fetch is reported, and every outcome is in the report the live box score shows. Offline; run with .venv-api.
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DAY = datetime.date(2026, 10, 15)


def check(condition: bool, message: str) -> None:
    print(("ok    " if condition else "FAIL  ") + message)
    if not condition:
        sys.exit(1)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fs-ingest-", ignore_cleanup_errors=True) as tmp:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(tmp) / 'ingest').as_posix()}"
        from fantasy_sports.platform import nhl_daily_sync as sync
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import Game, RealTeam, Universe
        from fantasy_sports.platform.db.session import SessionLocal, engine
        from fantasy_sports.sports.hockey.models import GameEvent

        Base.metadata.create_all(bind=engine)
        with SessionLocal() as db:
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            db.add(nhl)
            db.flush()
            for abbr in ("BOS", "TOR", "NYR", "MTL", "SEA", "DET"):
                db.add(RealTeam(universe_id=nhl.id, name=abbr, external_ref=abbr))
            db.commit()

        nhl_ids = {"BOS": 1, "TOR": 2, "NYR": 3, "MTL": 4, "SEA": 5, "DET": 6}
        games = [(101, "TOR", "BOS"), (102, "MTL", "NYR"), (103, "DET", "SEA")]       # (id, away, home)
        plays = {101: 2, 103: 2}
        calls: list[str] = []
        state = {"schedule_ok": True}

        def fake_fetch(url: str, timeout: float = 6.0):
            calls.append(url)
            if "/schedule/" in url:
                if not state["schedule_ok"]:
                    return None
                return {"gameWeek": [{"date": DAY.isoformat(), "games": [
                    {"id": gid, "gameState": "LIVE", "startTimeUTC": "2026-10-15T23:00:00Z",
                     "awayTeam": {"abbrev": a, "score": 0}, "homeTeam": {"abbrev": h, "score": 1}} for gid, a, h in games]}]}
            gid = int(url.split("/gamecenter/")[1].split("/")[0])
            if gid == 102:
                return {"plays": []}                       # no team ids: cannot be ingested
            a, h = next((a, h) for g, a, h in games if g == gid)
            return {"homeTeam": {"id": nhl_ids[h]}, "awayTeam": {"id": nhl_ids[a]}, "rosterSpots": [],
                    "plays": [{"typeDescKey": "faceoff", "eventId": k, "periodDescriptor": {"number": 1, "periodType": "REG"},
                               "timeInPeriod": f"00:0{k}", "details": {"eventOwnerTeamId": nhl_ids[h]}} for k in range(1, plays[gid] + 1)]}

        sync._fetch_json = fake_fetch

        def events_by_game() -> dict[int, list[int]]:
            with SessionLocal() as db:
                out: dict[int, list[int]] = {}
                for gid, a, h in games:
                    row = [g for g in db.query(Game).all() if g.home_team_id == db.query(RealTeam).filter_by(external_ref=h).one().id]
                    out[gid] = sorted(e.id for e in db.query(GameEvent).filter_by(game_id=row[0].id)) if row else []
                return out

        def ingest() -> dict:
            report: dict = {}
            with SessionLocal() as db:
                nhl_u = db.query(Universe).filter_by(code="nhl").one()
                sync.ingest_daily_slate_if_needed(db, nhl_u, DAY, report)
                db.commit()
            return report

        # ---- one unreadable game does not cost the others -----------------------------------------------------------------------------
        report = ingest()
        by_game = events_by_game()
        check(len(by_game[101]) == 2 and len(by_game[103]) == 2, "the two readable games are stored even though a third failed")
        bad = next(g for g in report["games"] if g["nhl_id"] == 102)
        check(bad["error"] and "KeyError" in bad["error"], "the unreadable game is reported with its error, not swallowed")
        check(report["schedule"] == "ok" and len(report["games"]) == 3, "the report says the schedule was read and lists every game")
        with SessionLocal() as db:
            check({g.status for g in db.query(Game).all()} == {"live"} and db.query(Game).count() == 3, "all three games exist with their live status")

        # ---- unchanged plays are left alone; new plays are picked up ------------------------------------------------------------------
        before = events_by_game()
        report = ingest()
        check(events_by_game() == before, "a second ingest with identical plays rewrites nothing")
        check(next(g for g in report["games"] if g["nhl_id"] == 101)["pbp"].startswith("unchanged"), "...and says so")
        plays[101] = 3
        ingest()
        after = events_by_game()
        check(len(after[101]) == 3 and after[103] == before[103], "a game with new plays is re-ingested, a game without is untouched")

        # ---- the schedule cannot be read ----------------------------------------------------------------------------------------------
        state["schedule_ok"] = False
        report = ingest()
        check(report["schedule"].startswith("FETCH FAILED") and events_by_game() == after, "an unreadable schedule is reported and nothing is lost")
        print("feed ingest checks passed")


if __name__ == "__main__":
    main()
