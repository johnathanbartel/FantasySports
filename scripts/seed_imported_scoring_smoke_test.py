"""
Imported scoring (FS-037) and per-league NBA layouts, against a throwaway SQLite file (imported_scoring_smoke_test.db).

Checks: the fixture line 30 PTS / 4 3PM / 10 REB / 5 AST / 2 STL / 1 BLK / 3 TO scores 36.0 (with the double-double);
a triple-double scores both bonuses (the flagged TRIPLE_DOUBLE_ALSO_COUNTS_DOUBLE rule); a second create_formula raises;
unknown ESPN stat ids, repeats and non-ESPN sources raise; no API route edits a formula; create-league refuses
scoring_system='imported' and the settings route can't change it; the league payload shows the read-only formula; a
realistic league keeps its scale context while scale features skip an imported league without error; per-league
layouts (10 starters / 12 bench / 3 IR), IR outside the roster size and never auto-filled, and the default 5 + 10
layout untouched.

    .venv-api/Scripts/python.exe scripts/seed_imported_scoring_smoke_test.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

db_path = Path(os.environ.get("IMPORTED_SCORING_SMOKE_DB", "imported_scoring_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import imported_scoring as imp, lineups, rankings, scoring
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import ImportedScoringItem, League, RosterSlotDefinition, Team, User
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.league_features import roster_spots
from fantasy_sports.platform.nba_reference import apply_league_layout
from fantasy_sports.platform.roster_fill import fill_slots

Base.metadata.create_all(bind=engine)
client = TestClient(app)

ITEMS = [{"stat_id": 0, "points": 0.5}, {"stat_id": 17, "points": 0.5}, {"stat_id": 6, "points": 1},
         {"stat_id": 3, "points": 1}, {"stat_id": 2, "points": 2}, {"stat_id": 1, "points": 2},
         {"stat_id": 11, "points": -1}, {"stat_id": 37, "points": 1}, {"stat_id": 38, "points": 2}]


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    return False


def main():
    db = SessionLocal()
    user = User(email="imp@example.com", display_name="Commissioner")
    db.add(user)
    db.commit()
    headers = {"Authorization": f"Bearer {client.post('/sessions', json={'user_id': user.id}).json()['token']}"}

    def create(**extra):
        body = {"name": "NBA", "commissioner_user_id": user.id, "universe_codes": ["nba"], "size": 2}
        body.update(extra)
        return client.post("/leagues", json=body, headers=headers)

    # --- create-league never offers imported scoring --------------------------------------------------------------
    assert create(scoring_system="imported").status_code == 400
    assert create(scoring_system="bogus").status_code == 400
    r = create(scoring_system="realistic")
    assert r.status_code == 200 and r.json()["scoring_system"] == "realistic" and r.json()["imported_scoring"] == []
    realistic = db.get(League, r.json()["id"])
    assert create().json()["scoring_system"] == "realistic"

    # --- the imported league --------------------------------------------------------------------------------------
    imported = db.get(League, create().json()["id"])
    db.add_all(Team(league_id=imported.id, name=f"T{i}", owner_user_id=user.id) for i in range(2))
    rows = imp.create_formula(db, imported, ITEMS, "espn")
    db.commit()
    assert imported.scoring_system == "imported" and len(rows) == 9
    f = imp.formula(db, imported.id)
    assert f == {"PTS": 0.5, "3PM": 0.5, "REB": 1, "AST": 1, "STL": 2, "BLK": 2, "TO": -1, "DD": 1, "TD": 2}, f

    # a. the fixture line: 15 + 2 + 10 + 5 + 4 + 2 - 3 + 1 (DD) = 36.0
    line = {"PTS": 30, "3PM": 4, "REB": 10, "AST": 5, "STL": 2, "BLK": 1, "TO": 3}
    assert imp.score_stat_line(f, line) == 36.0, imp.score_stat_line(f, line)
    # b. a triple-double gets the DD and the TD bonus (flagged rule); a near miss gets neither
    td = {"PTS": 20, "REB": 10, "AST": 10}
    assert imp.TRIPLE_DOUBLE_ALSO_COUNTS_DOUBLE is True
    assert imp.score_stat_line(f, td) == 10 + 10 + 10 + 1 + 2, imp.score_stat_line(f, td)
    assert imp.score_stat_line(f, {"PTS": 9, "REB": 9, "AST": 9}) == 4.5 + 9 + 9
    assert imp.score_stat_line(f, {"PTS": 10, "REB": 10, "DD": 5, "TD": 5}) == 5 + 10 + 1, "supplied DD/TD are ignored"
    assert imp.score_stat_line(f, {}) == 0.0

    # c. frozen: a second create_formula raises, nothing is written, no route edits it, settings can't change it
    assert raises(imp.ImportedScoringError, imp.create_formula, db, imported, ITEMS, "espn")
    assert raises(imp.ImportedScoringError, imp.create_formula, db, realistic, ITEMS, "yahoo")
    assert db.query(ImportedScoringItem).filter_by(league_id=imported.id).count() == 9
    editors = [(sorted(rt.methods), rt.path) for rt in app.routes
               if getattr(rt, "methods", None) and set(rt.methods) & {"POST", "PUT", "PATCH", "DELETE"}
               and ("scoring" in rt.path or "formula" in rt.path)]
    assert editors == [], editors
    r = client.patch(f"/leagues/{imported.id}/settings", json={"scoring_system": "realistic"}, headers=headers)
    assert r.status_code in (200, 422), r.text
    r = client.patch(f"/leagues/{realistic.id}/settings", json={"scoring_system": "imported"}, headers=headers)
    assert r.status_code in (200, 422), r.text
    db.expire_all()
    assert db.get(League, imported.id).scoring_system == "imported"
    assert db.get(League, realistic.id).scoring_system == "realistic", "no league can switch into imported scoring"
    assert db.query(ImportedScoringItem).filter_by(league_id=imported.id).count() == 9
    body = client.get(f"/leagues/{imported.id}").json()
    assert body["scoring_system"] == "imported"
    assert {i["stat_code"] for i in body["imported_scoring"]} == set(f)
    assert all(i["source_platform"] == "espn" for i in body["imported_scoring"])

    # d. unknown ids / repeats / empty raise, and leave nothing behind
    fresh = db.get(League, create().json()["id"])
    assert raises(imp.ImportedScoringError, imp.create_formula, db, fresh,
                  [{"stat_id": 0, "points": 1}, {"stat_id": 999, "points": 1}], "espn")
    assert raises(imp.ImportedScoringError, imp.create_formula, db, fresh,
                  [{"stat_id": 0, "points": 1}, {"stat_id": 0, "points": 2}], "espn")
    assert raises(imp.ImportedScoringError, imp.create_formula, db, fresh, [], "espn")
    db.rollback()
    assert db.get(League, fresh.id).scoring_system == "realistic"
    assert db.query(ImportedScoringItem).filter_by(league_id=fresh.id).count() == 0

    # e/f. a realistic league keeps the scale path; scale features skip an imported league without error
    db.add_all(Team(league_id=realistic.id, name=f"R{i}", owner_user_id=user.id) for i in range(2))
    db.commit()
    assert scoring.league_context(db, realistic.id) is not None
    assert scoring.league_context(db, imported.id) is None
    imp_team = db.query(Team).filter_by(league_id=imported.id).first()
    assert scoring.team_week_score(db, imp_team.id, 1) == 0.0, "no box scores ingested yet: a team scores 0"
    assert rankings.poll(db, imported.id) == {"as_of_week": 0, "size": 0, "voters": 0, "remaining_weeks": [],
                                              "ranked": [], "others": [], "epa_ranking": []}

    # --- per-league layouts ---------------------------------------------------------------------------------------
    assert roster_spots(db, realistic.id) == 15
    layout = apply_league_layout(db, imported, g=1, f=1, util=3, bench=12, ir=3)
    db.commit()
    slots = lineups.league_slots(db, imported.id)
    assert {s.slot_code: s.slot_count for s in slots} == {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1,
                                                          "UTIL": 3, "BENCH": 12, "IR": 3}
    assert sum(s.slot_count for s in slots if s.is_starter) == 10
    assert roster_spots(db, imported.id) == 22 and imported.draft_rounds == 22, "IR is not a roster spot"
    assert len(layout) == 10 and all(s.league_id == imported.id for s in slots)
    # the default league and the universe rows are untouched
    default = {s.slot_code: s.slot_count for s in lineups.league_slots(db, realistic.id)}
    assert default == {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "BENCH": 10}, default
    assert db.query(RosterSlotDefinition).filter_by(league_id=None).count() == 6
    # a custom layout can be replaced until a lineup uses it; the IR slot is never auto-filled
    apply_league_layout(db, imported, util=1, bench=2, ir=1)
    small = lineups.league_slots(db, imported.id)
    filled, left = fill_slots(small, [(i, "PG") for i in range(1, 12)])
    counts = {s.slot_code: len(c) for s, c in filled}
    assert counts["IR"] == 0 and counts["BENCH"] == 2 and len(left) > 0, counts
    db.commit()
    print("Imported scoring smoke test passed: fixture 36.0, TD stacking (flagged), frozen formula, create refused, "
          "scale features skip imported leagues, 10/12/3 layout with IR outside the roster size.")


if __name__ == "__main__":
    try:
        main()
    finally:
        engine.dispose()
        db_path.unlink(missing_ok=True)
