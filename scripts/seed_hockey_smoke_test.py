"""
Stands up a blank NHL project inside the existing multi-sport platform and
round-trips it through the real HTTP API (via FastAPI's TestClient): create
an "nhl" universe + roster slot definitions (see
src/fantasy_sports/sports/hockey/README.md) -> seed a couple of real NHL
teams and players across every hockey position -> create a league on that
universe -> run a snake draft covering a skater and a goalie -> read the
pick history back out through the API.

This does not exercise hockey scoring - the xG credit-attribution pipeline
described in the README is a data-model-only stand-up at this stage, not
implemented yet (see README.md's Data pipeline section).

Uses its own throwaway SQLite file (hockey_smoke_test.db) so it never
touches fantasy.db or any other real dev database. Run from the repo root
with the API venv:

    .venv-api/Scripts/python.exe scripts/seed_hockey_smoke_test.py
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Mock external network calls to keep test offline and hermetic
_fetch_patcher = patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None)
_fetch_patcher.start()
_injury_patcher = patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None)
_injury_patcher.start()

db_path = Path(os.environ.get("HOCKEY_SMOKE_DB", "hockey_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"

# Fresh DB file every run.
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.platform.db.models.core import (
    DraftableAsset,
    Player,
    RealTeam,
    Team,
    User,
)

# Registers the hockey-specific tables (shots/shot_credits) on Base.metadata
# even though this smoke test doesn't use them yet - proves the data model
# import-wires cleanly into the shared schema.
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app)

def seed_reference_data(db):
    """Reference/admin data - not something a user action creates."""
    nhl, _ = seed_hockey_reference_data(db)

    bruins = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
    leafs = RealTeam(universe_id=nhl.id, name="Toronto Maple Leafs", external_ref="TOR")
    db.add_all([bruins, leafs])
    db.flush()

    players = [
        Player(universe_id=nhl.id, real_team_id=bruins.id, full_name="David Pastrnak", position="RW"),
        Player(universe_id=nhl.id, real_team_id=bruins.id, full_name="Charlie McAvoy", position="D"),
        Player(universe_id=nhl.id, real_team_id=bruins.id, full_name="Jeremy Swayman", position="G"),
        Player(universe_id=nhl.id, real_team_id=leafs.id, full_name="Auston Matthews", position="C"),
        Player(universe_id=nhl.id, real_team_id=leafs.id, full_name="William Nylander", position="RW"),
        Player(
            universe_id=nhl.id,
            real_team_id=leafs.id,
            full_name="Outdated Roster Player",
            position="C",
            active=False,
        ),
    ]
    for i in range(60):   # a draft fills every roster spot (18 per team), so the pool must cover 2 teams x 18
        players.append(Player(universe_id=nhl.id, real_team_id=leafs.id, full_name=f"Depth Skater {i:02d}", position="C"))
    db.add_all(players)
    db.flush()

    assets = [
        DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id) for p in players
    ]
    db.add_all(assets)
    db.flush()

    commissioner = User(email="johnathan@example.com", display_name="Johnathan")
    db.add(commissioner)
    db.commit()

    by_name = {p.full_name: a for p, a in zip(players, assets)}
    return {"nhl": nhl, "assets_by_name": by_name, "commissioner": commissioner}


def check_injured_list(db, headers, skater, goalie):
    """Injury status on the player card data, and the injured list: eligibility, capacity, lines, roster count."""
    from fantasy_sports.platform import free_agency, injuries
    from fantasy_sports.platform.db.models.core import RosterEntry

    entry = db.query(RosterEntry).filter_by(draftable_asset_id=skater.id).one()
    team_id = entry.team_id

    def read():
        resp = client.get(f"/teams/{team_id}/pecking-order")
        assert resp.status_code == 200, resp.text
        return resp.json()

    def il(asset_id, on):
        return client.post(f"/teams/{team_id}/injured-list", json={"draftable_asset_id": asset_id, "on": on}, headers=headers)

    def ranked_ids(body):
        return {p["draftable_asset_id"] for g in body["groups"] for p in g["ranked"] + g["unranked"]}

    assert skater.id in ranked_ids(read())
    assert il(skater.id, True).status_code == 400, "a healthy player can't go on the injured list"

    # Day-to-day shows on the card but isn't IL-eligible.
    injuries.set_injury(db, skater.player, "DTD", "Upper body")
    db.commit()
    card = next(p for g in read()["groups"] for p in g["ranked"] + g["unranked"] if p["draftable_asset_id"] == skater.id)
    assert (card["injury_status"], card["injury_note"]) == ("DTD", "Upper body"), card
    assert il(skater.id, True).status_code == 400, "day-to-day players stay on the lines"

    count_before = free_agency.roster_count(db, team_id)
    injuries.set_injury(db, skater.player, "IR", "Lower body")
    db.commit()
    resp = il(skater.id, True)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [p["draftable_asset_id"] for p in body["injured_list"]] == [skater.id]
    assert body["injured_list"][0]["injury_status"] == "IR"
    assert skater.id not in ranked_ids(body), "injured-list players leave the lines"
    assert free_agency.roster_count(db, team_id) == count_before - 1, "the injured list frees a roster spot"
    assert il(skater.id, True).status_code == 400, "already on the list"

    resp = il(skater.id, False)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["injured_list"] == [] and skater.id in ranked_ids(body)
    assert il(skater.id, False).status_code == 400, "not on the list"
    print("Injured list checks passed: status on the card, IL eligibility, off the lines, roster spot freed, activation.")


def check_injury_sync(db, headers, healed):
    """ESPN report parsing and application, offline, from a saved sample of the real response."""
    import json
    from fantasy_sports.platform import injury_sync
    from fantasy_sports.platform.db.models.core import Player, RealTeam, Universe

    nhl = db.query(Universe).filter_by(code="nhl").one()
    ana = RealTeam(universe_id=nhl.id, name="Anaheim Ducks", external_ref="ANA")
    db.add(ana)
    db.flush()
    granlund = Player(universe_id=nhl.id, real_team_id=ana.id, full_name="Mikael Granlund", position="C")
    db.add(granlund)
    db.flush()
    db.commit()
    assert healed.player.injury_status == "IR"   # left injured by check_injured_list; not in the sample report

    sample = json.loads((Path(__file__).parent / "fixtures" / "espn_injuries_sample.json").read_text(encoding="utf-8"))
    entries = injury_sync.parse_report(sample)
    assert len(entries) == 128 and {e.status for e in entries} == {"DTD", "OUT", "IR"}
    assert injury_sync.apply_report(db, entries[:5]) == [], "a short report is ignored"
    assert healed.player.injury_status == "IR"

    changes = {c.name: c for c in injury_sync.apply_report(db, entries)}
    db.commit()
    assert (granlund.injury_status, granlund.injury_note) == ("DTD", "Upper Body"), (granlund.injury_status, granlund.injury_note)
    assert (changes["Mikael Granlund"].old_status, changes["Mikael Granlund"].new_status) == (None, "DTD")
    assert healed.player.injury_status is None and changes[healed.player.full_name].new_status is None
    assert injury_sync.apply_report(db, entries) == [], "re-applying the same report changes nothing"

    # Alerts go to the team that rosters an affected player (here: Matthews healed, on a team with an owner token).
    from fantasy_sports.platform.db.models.core import InjuryAlert, RosterEntry
    owner_team = db.query(RosterEntry).filter_by(draftable_asset_id=healed.id).one().team_id
    resp = client.get(f"/teams/{owner_team}/injury-alerts", headers=headers)
    assert resp.status_code == 200, resp.text
    alerts = resp.json()
    assert [(a["player_name"], a["old_status"], a["new_status"]) for a in alerts] == [("Auston Matthews", "IR", None)], alerts
    assert db.query(InjuryAlert).filter_by(player_name="Mikael Granlund").count() == 0, "nobody rosters Granlund"
    assert client.post(f"/teams/{owner_team}/injury-alerts/read", headers=headers).json() == {"dismissed": 1}
    assert client.get(f"/teams/{owner_team}/injury-alerts", headers=headers).json() == []
    print("Injury sync checks passed: parsed ESPN sample, matched by name, short report ignored, healed players cleared.")


def main():
    db = SessionLocal()
    ref = seed_reference_data(db)

    resp = client.post("/sessions", json={"user_id": ref["commissioner"].id})
    assert resp.status_code == 200, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['token']}"}

    # --- League + teams -----------------------------------------------
    resp = client.post("/leagues", json={
        "name": "NHL Stand-Up League",
        "commissioner_user_id": ref["commissioner"].id,
        "universe_codes": ["nhl"],
        "size": 2,
    }, headers=headers)
    assert resp.status_code == 200, resp.text
    league = resp.json()
    print(f"Created league {league['id']}: {league['name']} (universe: nhl)")

    team_a = Team(league_id=league["id"], name="Team A", owner_user_id=ref["commissioner"].id)
    team_b = Team(league_id=league["id"], name="Team B", owner_user_id=ref["commissioner"].id)
    db.add_all([team_a, team_b])
    db.commit()
    print(f"Teams: {team_a.id} ({team_a.name}), {team_b.id} ({team_b.name})")

    # --- Draft: a skater and a goalie from the same universe pool -------
    resp = client.post("/drafts/start", json={"league_id": league["id"]})
    assert resp.status_code == 200, resp.text
    draft = resp.json()
    print(f"Started draft {draft['id']}, status={draft['status']}")

    board = client.get(f"/drafts/{draft['id']}/board").json()
    print(f"Draft board: {[b['display_name'] for b in board]}")
    assert "Outdated Roster Player" not in {b["display_name"] for b in board}

    matthews = ref["assets_by_name"]["Auston Matthews"]
    swayman = ref["assets_by_name"]["Jeremy Swayman"]

    resp = client.post(
        f"/drafts/{draft['id']}/pick", json={"draftable_asset_id": matthews.id}, headers=headers,
    )
    assert resp.status_code == 200, resp.text
    print(f"Pick 1: {resp.json()['display_name']} -> team {resp.json()['team_id']}")

    resp = client.post(
        f"/drafts/{draft['id']}/pick", json={"draftable_asset_id": swayman.id}, headers=headers,
    )
    assert resp.status_code == 200, resp.text
    print(f"Pick 2: {resp.json()['display_name']} -> team {resp.json()['team_id']} "
          f"(a goalie, drafted from the same pool as the center above)")

    # Fill remaining picks via autopick.
    for _ in range(2):
        resp = client.post(f"/drafts/{draft['id']}/autopick")
        if resp.status_code == 200:
            print(f"Autopick: {resp.json()['display_name']} -> team {resp.json()['team_id']}")

    picks = client.get(f"/drafts/{draft['id']}/picks").json()
    print(f"Full pick history ({len(picks)} picks): "
          f"{[(p['round'], p['pick_number'], p['display_name']) for p in picks]}")

    check_injured_list(db, headers, matthews, swayman)
    check_injury_sync(db, headers, matthews)

    resp = client.get("/health")
    assert resp.status_code == 200, resp.text
    print(f"\nAPI health check: {resp.json()}")

    print("\nNHL stand-up smoke test passed: universe + roster slots + draft "
          "round-tripped end to end on the shared platform.")


if __name__ == "__main__":
    main()
