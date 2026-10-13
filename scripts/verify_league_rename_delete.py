"""
End-to-end verification of commissioner league rename and cascading deletion:
1. Validates authentication & authorization (401 for unauthenticated, 403 for non-commissioner).
2. Validates renaming updates the league and reflects in GET and dashboard.
3. Populates cascading child records (teams, drafts, picks, transactions, matchups).
4. Validates deletion cleans up all cascading records without FK violations.
5. Confirms non-league data (users, universes, reference data) is left intact.

Uses a throwaway SQLite database (verify_rename_delete.db).
Run with:
    .venv-api/Scripts/python.exe scripts/verify_league_rename_delete.py
"""

import os
import sys
from pathlib import Path

from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Mock external network calls to keep test offline and hermetic
_fetch_patcher = patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None)
_fetch_patcher.start()

DB_FILE = ROOT / "verify_rename_delete.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"

from fastapi.testclient import TestClient
from sqlalchemy import event

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.db.models.core import (
    Conference,
    Draft,
    DraftPick,
    DraftableAsset,
    FreeAgencyTransaction,
    League,
    LeagueUniverse,
    Player,
    RealTeam,
    RosterSlotDefinition,
    Team,
    Universe,
    User,
    WeeklyMatchup,
)

# Enable SQLite foreign key constraints so FK violations are caught immediately
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

Base.metadata.create_all(bind=engine)
client = TestClient(app)


def seed_reference_data(db):
    univ = Universe(sport="football", level="college", code="ncaaf", display_name="NCAAF")
    db.add(univ)
    db.flush()

    team_real = RealTeam(universe_id=univ.id, name="State College", conference="SEC", classification="fbs")
    db.add(team_real)
    db.flush()

    player = Player(universe_id=univ.id, real_team_id=team_real.id, full_name="Star Player", position="QB")
    db.add(player)
    db.flush()

    asset = DraftableAsset(universe_id=univ.id, asset_type="player", player_id=player.id)
    db.add(asset)

    slot = RosterSlotDefinition(
        universe_id=univ.id, slot_code="QB", eligible_positions=["QB"], slot_count=1, is_starter=True
    )
    db.add(slot)

    commish = User(email="commish@example.com", display_name="Commissioner Dan")
    stranger = User(email="stranger@example.com", display_name="Stranger Bob")
    db.add_all([commish, stranger])
    db.commit()

    return {"universe": univ, "commish": commish, "stranger": stranger, "asset": asset}


def main():
    db = SessionLocal()
    ref = seed_reference_data(db)
    commish_id = ref["commish"].id
    stranger_id = ref["stranger"].id
    asset_id = ref["asset"].id

    # 1. Authenticate both users
    r_c = client.post("/sessions", json={"user_id": commish_id})
    assert r_c.status_code == 200, r_c.text
    commish_headers = {"Authorization": f"Bearer {r_c.json()['token']}"}

    r_s = client.post("/sessions", json={"user_id": stranger_id})
    assert r_s.status_code == 200, r_s.text
    stranger_headers = {"Authorization": f"Bearer {r_s.json()['token']}"}

    # 2. Create a league
    r_lg = client.post("/leagues", json={
        "name": "Original League Name",
        "commissioner_user_id": commish_id,
        "universe_codes": ["ncaaf"],
        "size": 4,
        "draft_rounds": 4,
    }, headers=commish_headers)
    assert r_lg.status_code == 200, r_lg.text
    league_id = r_lg.json()["id"]
    assert r_lg.json()["name"] == "Original League Name"
    print(f"[OK] League created with id={league_id}")

    # 3. Test Rename Permissions & Validation
    # Unauthenticated -> 401
    r = client.patch(f"/leagues/{league_id}", json={"name": "Hacked Name"})
    assert r.status_code == 401, f"Expected 401, got {r.status_code}"

    # Stranger (non-commissioner) -> 403
    r = client.patch(f"/leagues/{league_id}", json={"name": "Stranger Name"}, headers=stranger_headers)
    assert r.status_code == 403, f"Expected 403, got {r.status_code}"

    # Empty name -> 400 or 422
    r = client.patch(f"/leagues/{league_id}", json={"name": "   "}, headers=commish_headers)
    assert r.status_code in (400, 422), f"Expected 400/422 for empty name, got {r.status_code}"

    # Successful rename by commissioner -> 200
    r = client.patch(f"/leagues/{league_id}", json={"name": "Championship League 2026"}, headers=commish_headers)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Championship League 2026"

    # Verify GET reflects new name
    r_get = client.get(f"/leagues/{league_id}")
    assert r_get.status_code == 200
    assert r_get.json()["name"] == "Championship League 2026"

    # Verify Dashboard reflects new name
    r_dash = client.get("/leagues/mine/dashboard", headers=commish_headers)
    assert r_dash.status_code == 200
    leagues_in_dash = [l for l in r_dash.json() if l["league_id"] == league_id]
    assert len(leagues_in_dash) == 1
    assert leagues_in_dash[0]["name"] == "Championship League 2026"
    assert leagues_in_dash[0]["is_commissioner"] is True
    print("[OK] League rename verified (auth, validation, persistence, dashboard)")

    # 4. Fill league with teams & cascading entities
    r_ai = client.post(f"/leagues/{league_id}/ai-teams", headers=commish_headers)
    assert r_ai.status_code == 200, r_ai.text

    # Add draft, draft pick, transaction
    draft = Draft(league_id=league_id, status="pending")
    db.add(draft)
    db.flush()

    team = db.query(Team).filter_by(league_id=league_id).first()
    assert team is not None

    pick = DraftPick(draft_id=draft.id, team_id=team.id, round=1, pick_number=1, draftable_asset_id=asset_id)
    db.add(pick)

    import datetime

    tx = FreeAgencyTransaction(
        league_id=league_id, team_id=team.id, draftable_asset_id=asset_id,
        kind="add", reason="free_agent_claim",
        created_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
    )
    db.add(tx)
    db.commit()

    # Verify records exist before deletion
    assert db.query(Team).filter_by(league_id=league_id).count() > 0
    assert db.query(Draft).filter_by(league_id=league_id).count() > 0
    assert db.query(DraftPick).filter_by(draft_id=draft.id).count() > 0
    assert db.query(FreeAgencyTransaction).filter_by(league_id=league_id).count() > 0
    assert db.query(Conference).filter_by(league_id=league_id).count() > 0
    assert db.query(LeagueUniverse).filter_by(league_id=league_id).count() > 0

    draft_id = draft.id

    # 5. Test Delete Permissions
    # Unauthenticated -> 401
    r = client.delete(f"/leagues/{league_id}")
    assert r.status_code == 401, f"Expected 401, got {r.status_code}"

    # Stranger (non-commissioner) -> 403
    r = client.delete(f"/leagues/{league_id}", headers=stranger_headers)
    assert r.status_code == 403, f"Expected 403, got {r.status_code}"

    # 6. Delete league by Commissioner -> 200
    r_del = client.delete(f"/leagues/{league_id}", headers=commish_headers)
    assert r_del.status_code == 200, r_del.text
    assert r_del.json()["status"] == "deleted"
    assert r_del.json()["league_id"] == league_id
    print("[OK] League delete endpoint returned 200 deleted")

    # 7. Verify cascading records are cleanly gone
    db.expire_all()
    assert db.get(League, league_id) is None
    assert db.query(Team).filter_by(league_id=league_id).count() == 0
    assert db.query(Draft).filter_by(league_id=league_id).count() == 0
    assert db.query(DraftPick).filter_by(draft_id=draft_id).count() == 0
    assert db.query(FreeAgencyTransaction).filter_by(league_id=league_id).count() == 0
    assert db.query(Conference).filter_by(league_id=league_id).count() == 0
    assert db.query(LeagueUniverse).filter_by(league_id=league_id).count() == 0

    # GET league -> 404
    r_get404 = client.get(f"/leagues/{league_id}")
    assert r_get404.status_code == 404

    # Dashboard no longer contains this league
    r_dash_after = client.get("/leagues/mine/dashboard", headers=commish_headers)
    assert r_dash_after.status_code == 200
    assert all(l["league_id"] != league_id for l in r_dash_after.json())

    # Deleting already-deleted league -> 404
    r_del404 = client.delete(f"/leagues/{league_id}", headers=commish_headers)
    assert r_del404.status_code == 404

    # 8. Verify global reference data is intact
    assert db.query(User).count() == 2
    assert db.query(Universe).count() == 1
    assert db.query(Player).count() == 1
    assert db.query(DraftableAsset).count() == 1

    print("[OK] All cascading child records removed; global records intact")

    # 9. Verify deletion of an NHL league with Series, Daily Games, Simulator, Banked Credits & Redraft
    db.commit()

    r_nhl = client.post("/leagues", json={
        "name": "NHL Series League",
        "commissioner_user_id": commish_id,
        "universe_codes": ["nhl"],
        "size": 4,
        "draft_rounds": 18,
        "series_length_days": 7,
    }, headers=commish_headers)
    assert r_nhl.status_code == 200, r_nhl.text
    nhl_lid = r_nhl.json()["id"]

    client.post(f"/leagues/{nhl_lid}/ai-teams", headers=commish_headers)
    nhl_teams = db.query(Team).filter_by(league_id=nhl_lid).all()
    assert len(nhl_teams) == 4

    from fantasy_sports.platform.db.models.core import (
        BankedCredit,
        LeagueSeries,
        SeriesDailyGame,
        SimulatorEvent,
        SimulatorGame,
        SimulatorReveal,
        WeeklyContest,
        WeeklyContestEntry,
        WeeklyContestPairing,
        WeeklyContestSlot,
        WeeklyPlayerSalary,
        WeeklyRedraftSettings,
    )

    series = LeagueSeries(
        league_id=nhl_lid, team_a_id=nhl_teams[0].id, team_b_id=nhl_teams[1].id,
        start_date=datetime.date(2026, 10, 15), end_date=datetime.date(2026, 10, 21), total_days=7,
    )
    db.add(series)
    db.flush()

    daily_game = SeriesDailyGame(
        series_id=series.id, home_team_id=nhl_teams[0].id, away_team_id=nhl_teams[1].id,
        calendar_date=datetime.date(2026, 10, 15), is_matchup_day=True,
    )
    db.add(daily_game)
    db.flush()

    sim_game = SimulatorGame(
        daily_game_id=daily_game.id, contract_version="v1", sport="hockey",
        periods=3, period_seconds=1200, window_seconds=3600,
        event_count=1, script_digest="test_digest", status="SCHEDULED",
    )
    db.add(sim_game)
    db.flush()

    sim_event = SimulatorEvent(
        simulator_game_id=sim_game.id, seq=1, kind="goal", period=1, game_second=100,
        team_side="home", home_score=1, away_score=0, home_shots=1, away_shots=0,
        headline="Goal scored",
    )
    sim_reveal = SimulatorReveal(
        simulator_game_id=sim_game.id, user_id=commish_id,
        mode="WATCHING", cursor_seq=1,
    )
    db.add_all([sim_event, sim_reveal])

    bank_credit = BankedCredit(
        league_id=nhl_lid, team_id=nhl_teams[0].id, draftable_asset_id=asset_id,
        source_calendar_date=datetime.date(2026, 10, 14), source_key="test_credit_1",
        credit_points=1.5, status="ASSIGNED", applied_points=1.5,
        assigned_daily_game_id=daily_game.id, assignment_mode="MANUAL",
    )
    db.add(bank_credit)

    redraft_settings = WeeklyRedraftSettings(league_id=nhl_lid, base_cap=50000, cap_policy="FIXED")
    db.add(redraft_settings)

    contest = WeeklyContest(
        league_id=nhl_lid, week_index=1, start_date=datetime.date(2026, 10, 15),
        end_date=datetime.date(2026, 10, 21), lock_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
        cap_applied=50000, status="OPEN",
    )
    db.add(contest)
    db.flush()

    pairing = WeeklyContestPairing(contest_id=contest.id, team_a_id=nhl_teams[0].id, team_b_id=nhl_teams[1].id)
    salary = WeeklyPlayerSalary(contest_id=contest.id, draftable_asset_id=asset_id, salary=8000)
    entry = WeeklyContestEntry(contest_id=contest.id, team_id=nhl_teams[0].id, salary_total=8000, cap=50000, status="DRAFT")
    db.add_all([pairing, salary, entry])
    db.flush()

    slot = WeeklyContestSlot(entry_id=entry.id, slot_code="C", slot_index=1, draftable_asset_id=asset_id, salary=8000)
    db.add(slot)
    db.commit()

    # Verify counts before deletion
    assert db.query(SimulatorReveal).filter_by(simulator_game_id=sim_game.id).count() == 1
    assert db.query(SimulatorEvent).filter_by(simulator_game_id=sim_game.id).count() == 1
    assert db.query(SimulatorGame).filter_by(id=sim_game.id).count() == 1
    assert db.query(BankedCredit).filter_by(league_id=nhl_lid).count() == 1
    assert db.query(SeriesDailyGame).filter_by(id=daily_game.id).count() == 1
    assert db.query(LeagueSeries).filter_by(id=series.id).count() == 1
    assert db.query(WeeklyContestSlot).filter_by(entry_id=entry.id).count() == 1
    assert db.query(WeeklyContestEntry).filter_by(id=entry.id).count() == 1
    assert db.query(WeeklyContest).filter_by(id=contest.id).count() == 1

    sim_game_id = sim_game.id
    daily_game_id = daily_game.id
    series_id = series.id
    entry_id = entry.id
    contest_id = contest.id

    # Delete the NHL league via API
    r_nhl_del = client.delete(f"/leagues/{nhl_lid}", headers=commish_headers)
    assert r_nhl_del.status_code == 200, r_nhl_del.text

    # Verify all NHL child records are cleanly purged
    db.expire_all()
    assert db.get(League, nhl_lid) is None
    assert db.query(SimulatorReveal).filter_by(simulator_game_id=sim_game_id).count() == 0
    assert db.query(SimulatorEvent).filter_by(simulator_game_id=sim_game_id).count() == 0
    assert db.query(SimulatorGame).filter_by(id=sim_game_id).count() == 0
    assert db.query(BankedCredit).filter_by(league_id=nhl_lid).count() == 0
    assert db.query(SeriesDailyGame).filter_by(id=daily_game_id).count() == 0
    assert db.query(LeagueSeries).filter_by(id=series_id).count() == 0
    assert db.query(WeeklyContestSlot).filter_by(entry_id=entry_id).count() == 0
    assert db.query(WeeklyContestEntry).filter_by(id=entry_id).count() == 0
    assert db.query(WeeklyContest).filter_by(id=contest_id).count() == 0
    assert db.query(Team).filter_by(league_id=nhl_lid).count() == 0

    print("[OK] NHL league with series, simulator, banked credits & redraft cleanly deleted")
    print("\nAll league rename and delete checks PASSED!")


if __name__ == "__main__":
    try:
        main()
    finally:
        if DB_FILE.exists():
            try:
                DB_FILE.unlink()
            except Exception:
                pass
