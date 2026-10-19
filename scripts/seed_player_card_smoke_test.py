"""
Player card, through the real HTTP API (FastAPI TestClient) on a throwaway SQLite file (player_card_smoke_test.db):
GET /leagues/{id}/players/{asset}/card for an NHL league - bio, injury, simulation stats and fantasy game log counted from a
published simulated game (goals, assists, shots, saves, shutouts), the spoiler rule, schedule, how and when he was acquired,
league transactions, % owned across leagues (mock drafts left out), the trade-block flag, and the buttons the viewer gets
(add / claim / cancel claim / drop / trade) in each situation.

    .venv-api/Scripts/python.exe scripts/seed_player_card_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("PLAYER_CARD_SMOKE_DB", "player_card_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists() and not os.environ.get("PLAYER_CARD_KEEP_DB"):      # PLAYER_CARD_KEEP_DB=1 keeps the data to look at in a browser
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    Draft, DraftableAsset, FreeAgencyTransaction, Game, League, LeagueSeries, LeagueUniverse, Player, RealTeam, RosterEntry,
    SeriesDailyGame, SimulatorEvent, SimulatorGame, Team, TradeBlockEntry, User, Week,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

checks = 0


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()
now = datetime.datetime.utcnow()

nhl, _ = seed_hockey_reference_data(db)
bos = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
tor = RealTeam(universe_id=nhl.id, name="Toronto Maple Leafs", external_ref="TOR")
db.add_all([bos, tor])
db.flush()
mk = lambda name, pos, team=bos, **kw: Player(universe_id=nhl.id, real_team_id=team.id, full_name=name, position=pos, **kw)   # noqa: E731
p_sk = mk("Sam Skater", "C", birth_date=datetime.date(2000, 5, 1), injury_status="DTD", injury_note="Lower body",
          injury_updated_at=now - datetime.timedelta(days=1))
p_g = mk("Gus Goalie", "G")
p_fa = mk("Free Agent", "C")
p_wv = mk("Waiver Wally", "C")
p_other = mk("Other Owned", "C", tor)
p_spare = mk("Spare Player", "C", tor)
db.add_all([p_sk, p_g, p_fa, p_wv, p_other, p_spare])
db.flush()
asset = {}
for key, p in (("sk", p_sk), ("g", p_g), ("fa", p_fa), ("wv", p_wv), ("other", p_other), ("spare", p_spare)):
    a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
    db.add(a)
    db.flush()
    asset[key] = a.id
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "a", "b", "stranger")}
db.add_all(users.values())
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


HDR = {n: login(u) for n, u in users.items()}
resp = client.post("/leagues", json={"name": "Card", "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"], "size": 4},
                   headers=HDR["commish"])
assert resp.status_code == 200, resp.text
lid = resp.json()["id"]
team_a = Team(league_id=lid, name="Alpha", owner_user_id=users["a"].id)
team_b = Team(league_id=lid, name="Bravo", owner_user_id=users["b"].id)
ai_1 = Team(league_id=lid, name="Charlie AI", owner_user_id=None, is_ai_owned=True)
ai_2 = Team(league_id=lid, name="Delta AI", owner_user_id=None, is_ai_owned=True)
db.add_all([team_a, team_b, ai_1, ai_2])
db.flush()
A, B, C, D = team_a.id, team_b.id, ai_1.id, ai_2.id
acquired = now - datetime.timedelta(days=3)
db.add_all([
    RosterEntry(team_id=C, draftable_asset_id=asset["sk"], acquired_via="free_agency", acquired_at=acquired),
    RosterEntry(team_id=D, draftable_asset_id=asset["g"], acquired_via="draft", acquired_at=acquired),
    RosterEntry(team_id=A, draftable_asset_id=asset["spare"], acquired_via="auction", acquired_at=acquired),
    RosterEntry(team_id=B, draftable_asset_id=asset["other"], acquired_via="draft", acquired_at=acquired),
])
db.add(Draft(league_id=lid, status="complete"))
db.add(TradeBlockEntry(league_id=lid, team_id=C, draftable_asset_id=asset["sk"], created_at=now))
db.add(FreeAgencyTransaction(league_id=lid, team_id=C, draftable_asset_id=asset["sk"], kind="add", price=0, reason="user",
                             created_at=acquired))
db.add(FreeAgencyTransaction(league_id=lid, team_id=B, draftable_asset_id=asset["wv"], kind="drop", price=0, reason="user",
                             created_at=now - datetime.timedelta(hours=2)))

# One finished simulated game between the two AI teams (so it is open to everyone): the skater scored twice and had one
# assist plus a shot on goal; his own goalie (same side) kept the other side off the board (shutout) and faced 3 shots.
series = LeagueSeries(league_id=lid, team_a_id=C, team_b_id=D, start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 10, 10),
                      total_days=7, team_a_wins=1, team_b_wins=0, status="IN_PROGRESS")
db.add(series)
db.flush()
dg = SeriesDailyGame(series_id=series.id, calendar_date=datetime.date(2026, 10, 4), home_team_id=C, away_team_id=D, home_score=2,
                     away_score=0, status="FINAL", current_period=3, seconds_remaining=0)
db.add(dg)
db.flush()
sim = SimulatorGame(daily_game_id=dg.id, contract_version="1", sport="hockey", periods=3, period_seconds=1200, window_seconds=60,
                    event_count=4, script_digest="x", status="FINAL",
                    provenance={"window_state": {"lineup": {"home": [asset["sk"], asset["g"]], "away": []}}})
db.add(sim)
db.flush()


def ev(seq, kind, side, hs, as_, hsh, ash, credits=None, **detail):
    db.add(SimulatorEvent(simulator_game_id=sim.id, seq=seq, kind=kind, period=1, game_second=seq * 10, team_side=side, home_score=hs,
                          away_score=as_, home_shots=hsh, away_shots=ash, headline=kind, detail={"credits": credits or [], **detail}))


ev(1, "score", "home", 1, 0, 1, 0, [{"asset_id": asset["sk"], "role": "shooter", "points": 1.0}])
ev(2, "score", "home", 2, 0, 2, 0, [{"asset_id": asset["other"], "role": "shooter", "points": 1.0},
                                     {"asset_id": asset["sk"], "role": "primary_assist", "points": 0.5}])
ev(3, "shot", "home", 2, 0, 3, 0, [{"asset_id": asset["sk"], "role": "shooter", "points": 0.1}], origin="earned")
ev(4, "shot", "away", 2, 0, 3, 3, origin="attempt", asset_id=asset["g"])

# Schedule: Boston has a game tomorrow.
wk = Week(universe_id=nhl.id, season_year=2026, week_number=3)
db.add(wk)
db.flush()
db.add(Game(universe_id=nhl.id, week_id=wk.id, home_team_id=bos.id, away_team_id=tor.id,
            kickoff_at=now + datetime.timedelta(days=1), status="scheduled"))
db.commit()


def card(asset_key, who=None):
    resp = client.get(f"/leagues/{lid}/players/{asset[asset_key]}/card", headers=HDR[who] if who else {})
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- the skater ---------------------------------------------------------------------------------------------------------------
c = card("sk")
check(c["sport"] == "hockey" and c["bio"]["display_name"] == "Sam Skater" and c["bio"]["position"] == "C", "bio")
check(c["bio"]["real_team"] == "Boston Bruins" and c["bio"]["age"] is not None, "team and age")
check(c["injury"]["status"] == "DTD" and c["injury"]["note"] == "Lower body", "injury status and note")
s = c["sim_stats"]
check((s["games_played"], s["goals"], s["assists"], s["points"], s["shots"]) == (1, 1, 1, 2, 2),
      f"simulation stats: one game, 1 G, 1 A, 2 PTS, 2 shots on goal (got {s})")
check(len(c["game_log"]) == 1 and c["game_log"][0]["goals"] == 1 and c["game_log"][0]["opponent"] == "Delta AI"
      and c["game_log"][0]["result"] == "W 2-0", f"game log row {c['game_log']}")
check(len(c["schedule"]) == 1 and c["schedule"][0]["opponent"] == "Toronto Maple Leafs" and c["schedule"][0]["home"], "schedule")
o = c["ownership"]
check(o["status"] == "rostered" and o["team_name"] == "Charlie AI" and o["acquired_via"] == "free_agency" and o["acquired_at"], "acquired")
check(o["on_trade_block"] is True, "trade block flag")
check(any(t["kind"] == "added" and "Charlie AI" in t["text"] for t in c["transactions"]), "league transaction listed")
check(any(n["kind"] == "move" for n in c["news"]), "news includes the league move")
check(c["pct_owned"]["leagues_owned"] == 1 and c["pct_owned"]["leagues_total"] >= 1, f"% owned {c['pct_owned']}")
check(c["actions"]["my_team_id"] is None and not any(c["actions"][k] for k in ("can_add", "can_claim", "can_drop", "can_trade")),
      "anonymous viewer: no buttons")

# --- the goalie: saves and shutout ----------------------------------------------------------------------------------------------
g = card("g")
check(g["sim_stats"]["saves"] == 3 and g["sim_stats"]["shutouts"] == 1, f"goalie saves/shutout {g['sim_stats']}")
check(g["game_log"][0]["saves"] == 3 and g["game_log"][0]["shutout"] is True, "goalie log row")
check(g["ownership"]["acquired_via"] == "draft", "goalie came by the draft")

# --- spoiler rule: a game with a human owner stays hidden until opened ----------------------------------------------------------------
dg.home_team_id = A
sim_lineup = dict(sim.provenance)
sim_lineup["window_state"] = {"lineup": {"home": [asset["sk"], asset["g"]], "away": []}}
sim.provenance = sim_lineup
db.commit()
c = card("sk", "stranger")
check(c["sim_stats"]["games_played"] == 0 and c["log_hidden_games"] == 1 and c["game_log"] == [], "unopened human game is held back")
dg.home_team_id = C
db.commit()

# --- buttons --------------------------------------------------------------------------------------------------------------------
a = card("fa", "a")["actions"]
check(a["my_team_id"] == A and a["can_add"] and not a["can_drop"] and not a["can_trade"], f"free agent: Add only ({a})")
a = card("spare", "a")["actions"]
check(a["can_drop"] and not a["can_add"] and not a["can_trade"], f"own player: Drop ({a})")
a = card("other", "a")["actions"]
check(a["can_trade"] and not a["can_drop"], f"another team's player: Trade ({a})")
c = card("wv", "a")
check(c["ownership"]["status"] == "waivers" and c["actions"]["can_claim"] and not c["actions"]["can_add"], f"waivers: Claim ({c['actions']})")
resp = client.post(f"/teams/{A}/waivers/claim", json={"draftable_asset_id": asset["wv"]}, headers=HDR["a"])
check(resp.status_code == 200, resp.text)
a = card("wv", "a")["actions"]
check(a["can_cancel_claim"] and not a["can_claim"], "after claiming: Cancel claim")

# --- the card follows the action ------------------------------------------------------------------------------------------------------
resp = client.post(f"/teams/{A}/free-agency/add", json={"draftable_asset_id": asset["fa"]}, headers=HDR["a"])
check(resp.status_code == 200, resp.text)
c = card("fa", "a")
check(c["ownership"]["team_name"] == "Alpha" and c["ownership"]["acquired_via"] == "free_agency" and c["actions"]["can_drop"],
      "after an add the card shows him on my team with Drop")

# --- % owned leaves mock drafts out ---------------------------------------------------------------------------------------------------
mock = League(name="Mock", commissioner_user_id=users["commish"].id, size=2, is_mock_draft=True)
db.add(mock)
db.flush()
db.add(LeagueUniverse(league_id=mock.id, universe_id=nhl.id))
db.commit()
before = card("sk")["pct_owned"]["leagues_total"]
check(before == client.get(f"/leagues/{lid}/players/{asset['sk']}/card").json()["pct_owned"]["leagues_total"], "stable")
mock_team = Team(league_id=mock.id, name="M1", owner_user_id=users["a"].id)
db.add(mock_team)
db.flush()
db.add(RosterEntry(team_id=mock_team.id, draftable_asset_id=asset["sk"], acquired_via="draft"))
db.commit()
check(card("sk")["pct_owned"]["leagues_owned"] == 1, "a mock draft's roster is not counted in % owned")

resp = client.get(f"/leagues/{lid}/players/999999/card")
check(resp.status_code == 404, "unknown player: 404")

print(f"OK - {checks} checks passed")
db.close()
engine.dispose()
if db_path.exists() and not os.environ.get("PLAYER_CARD_KEEP_DB"):      # PLAYER_CARD_KEEP_DB=1 keeps the data to look at in a browser
    db_path.unlink()
