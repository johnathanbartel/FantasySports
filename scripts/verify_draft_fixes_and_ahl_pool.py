"""Verification script for draft fixes and AHL call-up player pool inclusion.

Verifies:
1. Autopick restriction: Non-commissioner signed-in users receive 403 when attempting autopick for AI teams;
   commissioner can autopick; unauthenticated legacy scripts remain supported.
2. Prominent "Open Draft Board": draft_scheduled_at is returned in OverviewOut.
3. AHL call-up activation: Inactive players appearing in NHL game roster spots or team rosters
   are reactivated (active=True), their real_team_id is updated, and they appear in league_pool().
4. New call-up players without prior records get created as active DraftableAssets.
"""

import datetime as dt
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DB_FILE = ROOT / "verify_draft_fixes_test.db"
if DB_FILE.exists():
    try:
        DB_FILE.unlink()
    except Exception:
        pass

os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"

from fastapi.testclient import TestClient
from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    Draft, DraftableAsset, DraftPick, League, LeagueUniverse, Player, RealTeam, Team, Universe, User, UserSession,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.defense_modes import league_pool
from fantasy_sports.platform.free_agency import pool_ids
from fantasy_sports.platform import nhl_daily_sync


def check(cond: bool, msg: str):
    if not cond:
        raise AssertionError(msg)
    print(f"ok  {msg}")


def run_checks():
    Base.metadata.create_all(bind=engine)
    client = TestClient(app)

    with SessionLocal() as db:
        # Create users: commissioner and regular manager
        commish = User(email="commish_test@example.com", display_name="Commish")
        manager = User(email="manager_test@example.com", display_name="Manager")
        db.add_all([commish, manager])
        db.flush()

        comm_sess = UserSession(user_id=commish.id, token="token_commish_test")
        mgr_sess = UserSession(user_id=manager.id, token="token_manager_test")
        db.add_all([comm_sess, mgr_sess])
        db.flush()

        comm_hdr = {"Authorization": "Bearer token_commish_test"}
        mgr_hdr = {"Authorization": "Bearer token_manager_test"}

        # Create NHL universe
        nhl = db.query(Universe).filter_by(code="nhl").first()
        if not nhl:
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            db.add(nhl)
            db.flush()

        # Real NHL teams
        bruins = db.query(RealTeam).filter_by(universe_id=nhl.id, external_ref="BOS").first()
        if not bruins:
            bruins = RealTeam(universe_id=nhl.id, external_ref="BOS", name="Boston Bruins")
            db.add(bruins)
            db.flush()

        leafs = db.query(RealTeam).filter_by(universe_id=nhl.id, external_ref="TOR").first()
        if not leafs:
            leafs = RealTeam(universe_id=nhl.id, external_ref="TOR", name="Toronto Maple Leafs")
            db.add(leafs)
            db.flush()

        # Add initial player pool (20 active players)
        for i in range(20):
            p = Player(universe_id=nhl.id, real_team_id=bruins.id, full_name=f"NHL Pro {i}", position="C", active=True, external_ref=f"nhl_pro_{i}")
            db.add(p)
            db.flush()
            db.add(DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id))

        # Add inactive AHL player (sent down earlier or missing from season summary)
        ahl_player = Player(
            universe_id=nhl.id, real_team_id=None, full_name="AHL Callup Matthew Poitras",
            position="C", active=False, external_ref="990001",
        )
        db.add(ahl_player)
        db.flush()
        ahl_asset = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=ahl_player.id)
        db.add(ahl_asset)
        db.flush()

        # Scheduled draft time 45 minutes from now
        sched_time = dt.datetime.utcnow() + dt.timedelta(minutes=45)

        # Create League
        league = League(
            name="Test Draft Board League",
            commissioner_user_id=commish.id,
            size=4,
            draft_rounds=2,
            draft_type="snake",
            draft_scheduled_at=sched_time,
            series_length_days=7,
        )
        db.add(league)
        db.flush()
        db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id, defense_mode="group"))

        # Teams: Team 1 (Manager), Team 2 (AI), Team 3 (AI), Team 4 (Commish)
        t1 = Team(league_id=league.id, name="Manager Team", owner_user_id=manager.id, is_ai_owned=False)
        t2 = Team(league_id=league.id, name="AI Team 1", owner_user_id=None, is_ai_owned=True)
        t3 = Team(league_id=league.id, name="AI Team 2", owner_user_id=None, is_ai_owned=True)
        t4 = Team(league_id=league.id, name="Commish Team", owner_user_id=commish.id, is_ai_owned=False)
        db.add_all([t1, t2, t3, t4])
        db.commit()

        league_id = league.id
        ahl_player_id = ahl_player.id
        ahl_asset_id = ahl_asset.id
        bruins_id = bruins.id

    # ---- 1. Verify Prominent "Open Draft Board" Data Contract ----
    r_ov = client.get(f"/leagues/{league_id}/overview")
    check(r_ov.status_code == 200, "overview endpoint succeeds")
    ov_data = r_ov.json()
    check("draft_scheduled_at" in ov_data, "draft_scheduled_at is present in OverviewOut")
    check(ov_data["draft_scheduled_at"] is not None, "draft_scheduled_at is populated")
    print(f"ok  draft_scheduled_at returned: {ov_data['draft_scheduled_at']}")

    # ---- 2. Verify AHL Player Pool Initially Excludes Inactive Player ----
    with SessionLocal() as db:
        pool = league_pool(db, league_id)
        pool_asset_ids = {a.id for a in pool}
        check(ahl_asset_id not in pool_asset_ids, "inactive AHL player is excluded from initial league_pool")
        p_ids = pool_ids(db, league_id)
        check(ahl_asset_id not in p_ids, "inactive AHL player is excluded from free agency pool_ids")

    # ---- 3. Verify AHL Call-up Reactivation via Game Roster Spots ----
    # Mock PBP data containing ahl_player (990001) as called up to Boston Bruins
    mock_pbp = {
        "homeTeam": {"id": 1, "abbrev": "BOS"},
        "awayTeam": {"id": 2, "abbrev": "TOR"},
        "rosterSpots": [
            {
                "playerId": 990001,
                "teamId": 1,
                "firstName": {"default": "Matthew"},
                "lastName": {"default": "Poitras"},
                "positionCode": "C",
            },
            {
                "playerId": 990002,  # Brand new call-up without existing record
                "teamId": 1,
                "firstName": {"default": "Fabian"},
                "lastName": {"default": "Lysell"},
                "positionCode": "R",
            }
        ],
        "plays": [],
    }
    mock_schedule = {
        "gameWeek": [
            {
                "date": "2026-10-14",
                "games": [
                    {
                        "id": 2026020001,
                        "homeTeam": {"id": 1, "abbrev": "BOS", "score": 3},
                        "awayTeam": {"id": 2, "abbrev": "TOR", "score": 2},
                        "gameState": "FINAL",
                        "startTimeUTC": "2026-10-14T23:00:00Z",
                    }
                ]
            }
        ]
    }

    with patch("fantasy_sports.platform.nhl_daily_sync._fetch_json") as mock_fetch:
        def fetch_side_effect(url, timeout=6.0):
            if "schedule" in url:
                return mock_schedule
            if "play-by-play" in url:
                return mock_pbp
            return None
        mock_fetch.side_effect = fetch_side_effect

        with SessionLocal() as db:
            nhl_u = db.query(Universe).filter_by(code="nhl").first()
            games_cnt = nhl_daily_sync.ingest_daily_slate_if_needed(db, nhl_u, dt.date(2026, 10, 14))
            db.commit()
            check(games_cnt == 1, "ingest_daily_slate_if_needed processed 1 game")

    # Verify AHL players are now active and in the pool!
    with SessionLocal() as db:
        p1 = db.get(Player, ahl_player_id)
        check(p1.active is True, "existing AHL player was reactivated (active=True)")
        check(p1.real_team_id == bruins_id, "existing AHL player assigned to Boston Bruins")

        pool = league_pool(db, league_id)
        pool_asset_ids = {a.id for a in pool}
        check(ahl_asset_id in pool_asset_ids, "reactivated AHL player is now present in league_pool")

        # Check brand new callup (Fabian Lysell, 990002)
        p2 = db.query(Player).filter_by(external_ref="990002").first()
        check(p2 is not None, "new call-up player record created")
        check(p2.active is True, "new call-up player is active")
        check(p2.position == "RW", "new call-up position mapped correctly (R -> RW)")
        new_asset = db.query(DraftableAsset).filter_by(player_id=p2.id).first()
        check(new_asset is not None, "DraftableAsset created for new call-up")
        check(new_asset.id in pool_asset_ids, "new call-up is present in league_pool")

    # ---- 4. Verify AHL Roster Call-up Ingestion via sync_nhl_roster_callups ----
    mock_roster_data = {
        "forwards": [
            {
                "id": 990003,
                "firstName": {"default": "Georgii"},
                "lastName": {"default": "Merkulov"},
                "positionCode": "C",
            }
        ],
        "defensemen": [],
        "goalies": [],
    }
    with patch("fantasy_sports.platform.nhl_daily_sync._fetch_json") as mock_fetch:
        mock_fetch.return_value = mock_roster_data
        with SessionLocal() as db:
            nhl_u = db.query(Universe).filter_by(code="nhl").first()
            bruins_team = db.get(RealTeam, bruins_id)
            activated = nhl_daily_sync.sync_nhl_roster_callups(db, nhl_u, [bruins_team])
            check(activated == 1, "sync_nhl_roster_callups added 1 call-up")

        with SessionLocal() as db:
            p3 = db.query(Player).filter_by(external_ref="990003").first()
            check(p3 is not None and p3.active is True, "roster callup Merkulov created as active")
            a3 = db.query(DraftableAsset).filter_by(player_id=p3.id).first()
            check(a3 is not None and a3.id in {a.id for a in league_pool(db, league_id)},
                  "Merkulov is now draftable in league_pool")

    # ---- 5. Verify Autopick Guard: Commissioner Only for AI Teams ----
    r_start = client.post("/drafts/start", json={"league_id": league_id, "pick_seconds": 60}, headers=comm_hdr)
    check(r_start.status_code == 200, f"timed draft started: {r_start.text}")
    draft_id = r_start.json()["id"]

    # Pick 1 belongs to Manager Team (t1). Make pick 1 manually:
    with SessionLocal() as db:
        first_asset = league_pool(db, league_id)[0]
        first_asset_id = first_asset.id

    r_p1 = client.post(f"/drafts/{draft_id}/pick", json={"draftable_asset_id": first_asset_id}, headers=mgr_hdr)
    check(r_p1.status_code == 200, "manager made pick 1")

    # Pick 2 belongs to AI Team 1 (t2).
    # Manager (non-commissioner) attempts autopick for AI team:
    r_mgr_ai = client.post(f"/drafts/{draft_id}/autopick", headers=mgr_hdr)
    check(r_mgr_ai.status_code == 403, "non-commissioner manager is rejected with 403 from autopicking AI team")
    check("commissioner" in r_mgr_ai.json()["detail"].lower(), "403 error detail explains commissioner required")

    # Commissioner calls autopick for AI team:
    r_comm_ai = client.post(f"/drafts/{draft_id}/autopick", headers=comm_hdr)
    check(r_comm_ai.status_code == 200, "commissioner successfully autopicked for AI team")
    check(r_comm_ai.json()["is_autopick"] is True, "pick is marked is_autopick=True")

    # Pick 3 belongs to AI Team 2 (t3).
    # Unauthenticated call (backward compatibility for offline scripts):
    r_anon_ai = client.post(f"/drafts/{draft_id}/autopick")
    check(r_anon_ai.status_code == 200, "unauthenticated request succeeds for backward compatibility")

    print("\nAll draft fixes and AHL call-up pool verification checks PASSED successfully!")


if __name__ == "__main__":
    try:
        run_checks()
    finally:
        engine.dispose()
        if DB_FILE.exists():
            try:
                DB_FILE.unlink()
            except Exception:
                pass
