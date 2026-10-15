"""
Round-trips season rollover and the Keeper / Dynasty offseason through the real HTTP API (FastAPI TestClient) on a
throwaway SQLite file (rollover_smoke_test.db): for each League Format, finish a draft, give the teams real standings,
roll the season over, and check what carries over, keeper selection (owner picks, the deadline, the games-played
auto-trim), the roster freeze, the offseason drafts' pools, rounds and order, and the archive.

    .venv-api/Scripts/python.exe scripts/seed_rollover_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("ROLLOVER_SMOKE_DB", "rollover_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, KeeperCandidate, LeagueSeries, Player, RealTeam, RosterEntry, SeriesDailyGame, Team, User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()

nhl, _ = seed_hockey_reference_data(db)
real = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
db.add(real)
db.flush()
players = [Player(universe_id=nhl.id, real_team_id=real.id, full_name=f"Skater {i:03d}", position="C",
                  is_rookie=i >= 300) for i in range(400)]     # the last 100 are the rookie pool
db.add_all(players)
db.flush()
assets = [DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id) for p in players]
db.add_all(assets)
commissioner = User(email="commish@example.com", display_name="Commish")
other = User(email="other@example.com", display_name="Other")
db.add_all([commissioner, other])
db.commit()
rookie_asset_ids = {a.id for a, p in zip(assets, players) if p.is_rookie}


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


H, H_OTHER = login(commissioner), login(other)


def make_league(name, fmt, **settings):
    resp = client.post("/leagues", json={"name": name, "commissioner_user_id": commissioner.id, "universe_codes": ["nhl"],
                                          "size": 4}, headers=H)
    assert resp.status_code == 200, resp.text
    lid = resp.json()["id"]
    resp = client.patch(f"/leagues/{lid}/settings", json={"league_format": fmt, **settings}, headers=H)
    assert resp.status_code == 200, resp.text
    teams = [Team(league_id=lid, name=f"{name} T{i}", owner_user_id=commissioner.id) for i in range(4)]
    db.add_all(teams)
    db.commit()
    return lid, [t.id for t in teams]


def run_draft(league_id, headers=None):
    resp = client.post("/drafts/start", json={"league_id": league_id}, headers=headers)
    assert resp.status_code == 200, resp.text
    draft_id = resp.json()["id"]
    for _ in range(600):
        if client.post(f"/drafts/{draft_id}/autopick").status_code != 200:
            break
    picks = client.get(f"/drafts/{draft_id}/picks").json()
    assert client.get(f"/drafts/{draft_id}").json()["status"] == "complete"
    return picks


def order_of(picks, rnd):
    return [p["team_id"] for p in picks if p["round"] == rnd]


def picks_by_team(picks):
    out = {}
    for p in picks:
        out[p["team_id"]] = out.get(p["team_id"], 0) + 1
    return out


def roster_count(team_id):
    db.expire_all()
    return db.query(RosterEntry).filter_by(team_id=team_id).count()


def drop_players(team_ids, n):
    for tid in team_ids:
        for e in db.query(RosterEntry).filter_by(team_id=tid).order_by(RosterEntry.id.desc()).limit(n).all():
            db.delete(e)
    db.commit()


def give_results(lid, best_first):
    """Final games so the standings are real: each team beats every team below it in `best_first`."""
    day = datetime.date(2026, 1, 1)
    for i, winner in enumerate(best_first):
        for loser in best_first[i + 1:]:
            day += datetime.timedelta(days=1)
            s = LeagueSeries(league_id=lid, team_a_id=winner, team_b_id=loser, start_date=day, end_date=day, total_days=5,
                             status="FINAL")
            db.add(s)
            db.flush()
            db.add(SeriesDailyGame(series_id=s.id, calendar_date=day, home_team_id=winner, away_team_id=loser,
                                   home_score=4, away_score=1, status="FINAL"))
    db.commit()


def finish_season(name, fmt, **settings):
    lid, tids = make_league(name, fmt, **settings)
    first = run_draft(lid)
    best_first = [tids[3], tids[1], tids[0], tids[2]]          # deliberately not in id order
    give_results(lid, best_first)
    return lid, tids, first, best_first


def past(hours=1):
    return (datetime.datetime.utcnow() - datetime.timedelta(hours=hours)).isoformat()


def future(hours=48):
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=hours)).isoformat()


# ---- permissions: only the commissioner edits settings, anyone reads ------------------------------------------
lid, tids = make_league("Perm", "dynasty")
assert client.patch(f"/leagues/{lid}/settings", json={"league_format": "keeper"}, headers=H_OTHER).status_code == 403
assert client.patch(f"/leagues/{lid}/settings", json={"keeper_limit": 5}, headers=H_OTHER).status_code == 403
assert client.patch(f"/leagues/{lid}/settings", json={"league_format": "bogus"}, headers=H).status_code == 422
assert client.get(f"/leagues/{lid}").json()["league_format"] == "dynasty"
assert client.post(f"/leagues/{lid}/rollover", json={}, headers=H_OTHER).status_code == 403
assert client.post(f"/leagues/{lid}/rollover", json={}, headers=H).status_code == 409, "draft not finished"
assert client.patch(f"/leagues/{lid}/settings", json={"keeper_limit": 7}, headers=H).json()["keeper_limit"] == 7
assert client.patch(f"/leagues/{lid}/settings", json={"keeper_limit": None}, headers=H).json()["keeper_limit"] is None

# ---- Traditional Redraft: rosters reset, random order, snake -------------------------------------------------
lid, tids, first, best_first = finish_season("Trad", "traditional_redraft")
league = client.post(f"/leagues/{lid}/rollover", json={}, headers=H)
assert league.status_code == 200, league.text
league = league.json()
assert (league["season_number"], league["offseason"], league["keepers_finalized"]) == (2, True, True), league
assert all(roster_count(t) == 0 for t in tids), "traditional redraft clears rosters"
assert client.post(f"/leagues/{lid}/rollover", json={}, headers=H).status_code == 409, "already in the offseason"
assert client.post("/drafts/start", json={"league_id": lid}).status_code == 401, "offseason draft is commissioner-only"
assert client.get(f"/leagues/{lid}/overview").json()["draft_state"] == "not_started"
second = run_draft(lid, headers=H)
assert sorted(order_of(second, 1)) == sorted(tids) and order_of(second, 2) == order_of(second, 1)[::-1], "random order, snake"
assert client.get(f"/leagues/{lid}").json()["offseason"] is False
seasons = client.get(f"/leagues/{lid}/seasons").json()
assert [s["season_number"] for s in seasons] == [1] and [r["team_id"] for r in seasons[0]["standings"]] == best_first
season1 = client.get(f"/leagues/{lid}/seasons/1").json()
assert sum(len(v) for v in season1["data"]["rosters"].values()) == len(first)
assert len(season1["data"]["drafts"][0]["picks"]) == len(first) and len(season1["data"]["series"]) >= 6
assert client.get(f"/leagues/{lid}/seasons/9").status_code == 404

# ---- Future Draft Pick Redraft: rosters reset, reverse standings, snake ----------------------------------------
lid, tids, first, best_first = finish_season("Future", "future_pick_redraft")
assert client.post(f"/leagues/{lid}/rollover", json={}, headers=H).status_code == 200
assert all(roster_count(t) == 0 for t in tids)
second = run_draft(lid, headers=H)
assert order_of(second, 1) == best_first[::-1], (order_of(second, 1), best_first)
assert order_of(second, 2) == best_first, "snake: round 2 reverses"

# ---- Dynasty: whole roster kept, rookie draft only, reverse standings, no snake, roster freeze ----------------
lid, tids, first, best_first = finish_season("Dyn", "dynasty")
full = max(p["round"] for p in first)
assert client.post(f"/leagues/{lid}/rollover", json={}, headers=H).status_code == 409, "rookie rounds are required"
resp = client.post(f"/leagues/{lid}/rollover", json={"offseason_draft_rounds": 2}, headers=H)
assert resp.status_code == 200 and resp.json()["keepers_finalized"] is True, resp.text
assert all(roster_count(t) == full for t in tids)
freeze = client.post("/drafts/start", json={"league_id": lid}, headers=H)
assert freeze.status_code == 409 and "Roster freeze" in freeze.json()["detail"], freeze.text
drop_players(tids, 2)
second = run_draft(lid, headers=H)
assert order_of(second, 1) == best_first[::-1] and order_of(second, 2) == best_first[::-1], "reverse standings, no snake"
assert len(second) == 8 and all(roster_count(t) == full for t in tids)
assert {p["draftable_asset_id"] for p in second} <= rookie_asset_ids, "a rookie draft drafts rookies only"
assert client.get(f"/leagues/{lid}").json()["offseason"] is False

# ---- Keeper, rookie_resign: owners pick keepers, the rest are released, one rookie draft ------------------------
lid, tids, first, best_first = finish_season("KeepR", "keeper", keeper_draft_style="rookie_resign", keeper_limit=12)
assert client.post(f"/leagues/{lid}/rollover", json={"offseason_draft_rounds": 2}, headers=H).status_code == 409, "needs a deadline"
resp = client.post(f"/leagues/{lid}/rollover", json={"offseason_draft_rounds": 2, "keeper_deadline": future()}, headers=H)
assert resp.status_code == 200, resp.text
assert resp.json()["keepers_finalized"] is False and all(roster_count(t) == full for t in tids)
blocked = client.post("/drafts/start", json={"league_id": lid}, headers=H)
assert blocked.status_code == 409 and "Keepers" in blocked.json()["detail"], blocked.text
mine = client.get(f"/leagues/{lid}/teams/{tids[0]}/keepers").json()
assert mine["cap"] == 12 and len(mine["players"]) == full and mine["submitted"] is False
ids = [p["draftable_asset_id"] for p in mine["players"]]
assert client.put(f"/leagues/{lid}/teams/{tids[0]}/keepers", json={"draftable_asset_ids": ids[:13]}, headers=H).status_code == 409, "over the limit"
assert client.put(f"/leagues/{lid}/teams/{tids[0]}/keepers", json={"draftable_asset_ids": [-1]}, headers=H).status_code == 409
assert client.put(f"/leagues/{lid}/teams/{tids[0]}/keepers", json={"draftable_asset_ids": ids[6:16]}, headers=H_OTHER).status_code == 403
resp = client.put(f"/leagues/{lid}/teams/{tids[0]}/keepers", json={"draftable_asset_ids": ids[6:16]}, headers=H)
assert resp.status_code == 200 and resp.json()["submitted"] and sum(p["kept"] for p in resp.json()["players"]) == 10, resp.text
# Last season's games played decide the auto-trim: team 1 played the later roster spots more.
for c in db.query(KeeperCandidate).filter_by(team_id=tids[1]).all():
    c.games_played = c.roster_order
db.commit()
assert client.post(f"/leagues/{lid}/keepers/finalize", json={}, headers=H_OTHER).status_code == 403
resp = client.post(f"/leagues/{lid}/keepers/finalize", headers=H)
assert resp.status_code == 200 and resp.json()["keepers_finalized"] is True, resp.text
assert [roster_count(t) for t in tids] == [10, 12, 12, 12], [roster_count(t) for t in tids]
db.expire_all()
kept_orders = sorted(c.roster_order for c in db.query(KeeperCandidate).filter_by(team_id=tids[1], kept=True))
assert kept_orders == list(range(full - 12, full)), "fewest games played are trimmed"
assert client.put(f"/leagues/{lid}/teams/{tids[0]}/keepers", json={"draftable_asset_ids": ids[:5]}, headers=H).status_code == 409, "closed"
second = run_draft(lid, headers=H)
assert order_of(second, 1) == best_first[::-1] and order_of(second, 2) == best_first[::-1] and len(second) == 8
assert {p["draftable_asset_id"] for p in second} <= rookie_asset_ids
assert client.get(f"/leagues/{lid}").json()["offseason"] is False

# ---- Keeper, separate: a rookie draft, then a refill draft; a team that kept everyone gets rookies only -------
lid, tids, first, best_first = finish_season("KeepS", "keeper", keeper_draft_style="separate")
resp = client.post(f"/leagues/{lid}/rollover", json={"offseason_draft_rounds": 2, "keeper_deadline": future()}, headers=H)
assert resp.status_code == 409, "separate needs refill rounds"
resp = client.post(f"/leagues/{lid}/rollover", json={"offseason_draft_rounds": 2, "refill_rounds": 3,
                                                       "keeper_deadline": future()}, headers=H)
assert resp.status_code == 200, resp.text
ids0 = [p["draftable_asset_id"] for p in client.get(f"/leagues/{lid}/teams/{tids[0]}/keepers").json()["players"]]
ids1 = [p["draftable_asset_id"] for p in client.get(f"/leagues/{lid}/teams/{tids[1]}/keepers").json()["players"]]
assert client.put(f"/leagues/{lid}/teams/{tids[0]}/keepers", json={"draftable_asset_ids": ids0}, headers=H).status_code == 200   # all
assert client.put(f"/leagues/{lid}/teams/{tids[1]}/keepers", json={"draftable_asset_ids": ids1[:14]}, headers=H).status_code == 200
# The deadline passes: just reading the league closes keepers, trimming teams 2 and 3 (no limit: they keep everyone).
assert client.patch(f"/leagues/{lid}/settings", json={"keeper_deadline": past()}, headers=H).status_code == 200
assert client.get(f"/leagues/{lid}").json()["keepers_finalized"] is True
assert [roster_count(t) for t in tids] == [full, 14, full, full]
freeze = client.post("/drafts/start", json={"league_id": lid}, headers=H)
assert freeze.status_code == 409 and "Roster freeze" in freeze.json()["detail"], "full keepers drop players to fit rookies"
drop_players([tids[0], tids[2], tids[3]], 2)
rookies = run_draft(lid, headers=H)
assert len(rookies) == 8 and {p["draftable_asset_id"] for p in rookies} <= rookie_asset_ids
assert client.get(f"/leagues/{lid}").json()["offseason"] is True, "the refill draft is still to come"
assert client.get(f"/leagues/{lid}/overview").json()["draft_state"] == "not_started"
refill = run_draft(lid, headers=H)
assert tids[0] not in picks_by_team(refill) and tids[2] not in picks_by_team(refill) and tids[3] not in picks_by_team(refill), \
    "teams that kept everyone only get the rookie draft"
assert picks_by_team(refill) == {tids[1]: 2}, picks_by_team(refill)           # team 1 kept 14 + 2 rookies: room for 2, wants 3
assert not {p["draftable_asset_id"] for p in refill} & rookie_asset_ids, "a refill draft drafts non-rookies"
assert client.get(f"/leagues/{lid}").json()["offseason"] is False

# ---- Keeper, combined: one draft of rookies and refills ----------------------------------------------------------
lid, tids, first, best_first = finish_season("KeepC", "keeper", keeper_draft_style="combined", keeper_limit=16)
assert client.post(f"/leagues/{lid}/rollover", json={"offseason_draft_rounds": 2, "refill_rounds": 3,
                                                       "keeper_deadline": future()}, headers=H).status_code == 200
assert client.post(f"/leagues/{lid}/keepers/finalize", headers=H).status_code == 200      # nobody picked: all auto-trimmed to 16
assert [roster_count(t) for t in tids] == [16] * 4
combined = run_draft(lid, headers=H)
assert len(combined) == 4 * 2 and order_of(combined, 1) == best_first[::-1] and order_of(combined, 2) == best_first[::-1]
assert client.get(f"/leagues/{lid}").json()["offseason"] is False

print("Rollover smoke test passed: permissions, all four formats, keeper picks / deadline / auto-trim, roster freeze, "
      "draft kinds, order and pools, archive.")

# ---- a Keeper league left open in its keeper-selection phase (for looking at the page by hand) -----------------
lid, tids, first, best_first = finish_season("KeepOpen", "keeper", keeper_draft_style="separate", keeper_limit=14)
assert client.post(f"/leagues/{lid}/rollover", json={"offseason_draft_rounds": 2, "refill_rounds": 2,
                                                       "keeper_deadline": future(72)}, headers=H).status_code == 200
print(f"League {lid} (KeepOpen) is left in keeper selection.")
