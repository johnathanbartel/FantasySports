"""
Round-trips draft-pick trading through the real HTTP API (FastAPI TestClient) on a throwaway SQLite file
(pick_trade_smoke_test.db): which picks each league format can trade, ownership, validation, human and AI trades, the AI's
pick valuation, and the offseason draft handing a traded pick to its new owner.

    .venv-api/Scripts/python.exe scripts/seed_pick_trade_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()
# Starting a new season waits for the championship, which these leagues can't play yet: the tests stand in for it.
import fantasy_sports.platform.season_rollover as _season_rollover

_real_championship_status = _season_rollover.championship_status
patch("fantasy_sports.platform.season_rollover.championship_status", return_value=(True, "")).start()

db_path = Path(os.environ.get("PICK_TRADE_SMOKE_DB", "pick_trade_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, LeagueSeries, Player, RealTeam, SeriesDailyGame, Team, TradedPick, User,
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
players = [Player(universe_id=nhl.id, real_team_id=real.id, full_name=f"Skater {i:03d}", position="C") for i in range(400)]
db.add_all(players)
db.flush()
db.add_all([DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id) for p in players])
commissioner = User(email="commish@example.com", display_name="Commish")
other = User(email="other@example.com", display_name="Other")
db.add_all([commissioner, other])
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


H, H_OTHER = login(commissioner), login(other)


def make_league(name, fmt):
    # FS-036: a FREE league can't trade picks, so these are PAID leagues with every team's dues already secured for the seasons
    # whose picks are traded below, and no review window (accepted trades execute at once, as these checks expect).
    resp = client.post("/leagues", json={"name": name, "commissioner_user_id": commissioner.id, "universe_codes": ["nhl"],
                                          "size": 4, "league_type": "PAID", "annual_dues_cents": 5000}, headers=H)
    assert resp.status_code == 200, resp.text
    lid = resp.json()["id"]
    assert client.patch(f"/leagues/{lid}/settings", json={"league_format": fmt, "trade_review_hours": 0}, headers=H).status_code == 200
    teams = [Team(league_id=lid, name=f"{name} T{i}", owner_user_id=commissioner.id) for i in range(4)]
    db.add_all(teams)
    db.commit()
    for t in teams:
        for season in range(2, 6):
            resp = client.post(f"/leagues/{lid}/teams/{t.id}/dues", headers=H, json={
                "season_number": season, "amount_cents": 5000, "status": "paid", "payment_reference": f"dues-{t.id}-{season}"})
            assert resp.status_code == 200, resp.text
    return lid, [t.id for t in teams]


def run_draft(lid, headers=None):
    resp = client.post("/drafts/start", json={"league_id": lid}, headers=headers)
    assert resp.status_code == 200, resp.text
    draft_id = resp.json()["id"]
    for _ in range(600):
        if client.post(f"/drafts/{draft_id}/autopick").status_code != 200:
            break
    return client.get(f"/drafts/{draft_id}/picks").json()


def give_results(lid, best_first):
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


def picks_of(lid, tid):
    resp = client.get(f"/leagues/{lid}/teams/{tid}/tradeable-picks")
    assert resp.status_code == 200, resp.text
    return resp.json()


def key(p):
    return (p["season"], p["round"], p["original_team_id"])


def propose(lid, me, them, send=(), receive=(), send_picks=(), receive_picks=(), headers=H):
    return client.post(f"/leagues/{lid}/trades", headers=headers, json={
        "proposer_team_id": me, "counterparty_team_id": them, "send_asset_ids": list(send), "receive_asset_ids": list(receive),
        "send_picks": [{"season": s, "round": r, "original_team_id": o} for s, r, o in send_picks],
        "receive_picks": [{"season": s, "round": r, "original_team_id": o} for s, r, o in receive_picks]})


# ---- Future Draft Pick Redraft: next draft only, every round, one per team per round ---------------------------
lid, (t0, t1, t2, t3) = make_league("Fut", "future_pick_redraft")
first = run_draft(lid)
rounds = max(p["round"] for p in first)
mine = picks_of(lid, t0)
assert {p["season"] for p in mine} == {2} and len(mine) == rounds and {p["original_team_id"] for p in mine} == {t0}
assert all(p["projected_overall"] for p in mine), "every pick shows where it would land today"

# Validation
assert propose(lid, t0, t1, send_picks=[(2, 1, t1)], receive_picks=[(2, 2, t1)]).status_code == 400, "can't send a pick you don't own"
assert propose(lid, t0, t1, send_picks=[(3, 1, t0)], receive_picks=[(3, 1, t1)]).status_code == 400, "only the next draft"
assert propose(lid, t0, t1, send_picks=[(2, rounds + 1, t0)], receive_picks=[(2, 1, t1)]).status_code == 400, "no such round"
assert propose(lid, t0, t1, send_picks=[(2, 1, t0), (2, 1, t0)], receive_picks=[(2, 1, t1)]).status_code == 400, "listed twice"
assert propose(lid, t0, t1, send_picks=[(2, 1, t0)]).status_code == 400, "each side must give something"

# Human to human: round 1 for round 5 (picks only, no players), accepted by the other side.
resp = propose(lid, t0, t1, send_picks=[(2, 1, t0)], receive_picks=[(2, 5, t1)])
assert resp.status_code == 200, resp.text
trade = resp.json()
assert trade["status"] == "proposed" and trade["send_picks"][0]["round"] == 1 and trade["receive_picks"][0]["original_team_name"].endswith("T1")
resp = client.post(f"/trades/{trade['id']}/respond", json={"team_id": t1, "accept": True}, headers=H)
assert resp.status_code == 200 and resp.json()["status"] == "accepted", resp.text
assert (2, 1, t0) not in {key(p) for p in picks_of(lid, t0)} and (2, 5, t1) in {key(p) for p in picks_of(lid, t0)}
assert (2, 1, t0) in {key(p) for p in picks_of(lid, t1)} and (2, 5, t1) not in {key(p) for p in picks_of(lid, t1)}
assert propose(lid, t0, t1, send_picks=[(2, 1, t0)], receive_picks=[(2, 1, t1)]).status_code == 400, "t0 no longer owns it"
card = client.get(f"/leagues/{lid}/teams/{t0}/future-picks").json()
assert any(p["round"] == 5 and p["own"] is False and p["original_team_id"] == t1 for p in card["picks"]), "roster card shows acquired picks"
assert not any(p["round"] == 1 for p in card["picks"]), "and not the one traded away"

# AI teams value a pick by where it lands today (the Nth pick is worth the Nth best player's points).
ai = db.get(Team, t3)
ai.owner_user_id, ai.is_ai_owned = None, True
db.commit()
points = [100.0 - i for i in range(300)]
with patch("fantasy_sports.platform.pick_trading.ranked_points", return_value=points):
    # Nobody has results yet, so the order is by team id: worst-first is t3, t2, t1, t0 and the rounds snake.
    from fantasy_sports.platform import pick_trading
    from fantasy_sports.platform.db.models.core import League
    league = db.get(League, lid)
    order = pick_trading.projected_order(db, league)
    assert order == [t3, t2, t1, t0], order
    assert pick_trading.projected_overall(league, order, 1, t3) == 1 and pick_trading.projected_overall(league, order, 1, t0) == 4
    assert pick_trading.projected_overall(league, order, 2, t3) == 8, "snake: round 2 reverses"
    assert pick_trading.pick_value(points, 4) == 97.0 and pick_trading.pick_value(points, 10_000) == 0.0
    # A human offers a high pick for a low one: the AI comes out ahead and approves.
    good = propose(lid, t2, t3, send_picks=[(2, 2, t2)], receive_picks=[(2, rounds, t3)])
    assert good.status_code == 200, good.text
    ai_vote = next(v for v in good.json()["votes"] if v["team_id"] == t3)
    assert ai_vote["vote"] == "approve", good.json()["votes"]
    # A human asks for the AI's first-round pick for a last-round one: the AI would lose value and rejects.
    bad = propose(lid, t2, t3, send_picks=[(2, rounds, t2)], receive_picks=[(2, 1, t3)])
    assert bad.status_code == 200, bad.text
    ai_vote = next(v for v in bad.json()["votes"] if v["team_id"] == t3)
    assert ai_vote["vote"] == "reject", bad.json()["votes"]
    assert bad.json()["status"] != "accepted"
    for tr in (good.json(), bad.json()):
        client.post(f"/trades/{tr['id']}/cancel", headers=H)
ai.owner_user_id, ai.is_ai_owned = commissioner.id, False
db.commit()

# The offseason draft gives a traded pick to its new owner.
best_first = [t3, t1, t0, t2]
give_results(lid, best_first)
assert client.post(f"/leagues/{lid}/rollover", json={}, headers=H).status_code == 200
second = run_draft(lid, headers=H)
def team_order(rnd):
    return [p["team_id"] for p in second if p["round"] == rnd]


# Worst-first is t2, t0, t1, t3. Round 1: t0's slot now belongs to t1. Round 5 (odd, so the same direction): t1's slot belongs to t0.
assert team_order(1) == [t2, t1, t1, t3], team_order(1)
assert team_order(5) == [t2, t0, t0, t3], team_order(5)
assert team_order(2) == [t3, t1, t0, t2], "snake: an untouched round just reverses"
assert propose(lid, t0, t1, send_picks=[(2, 7, t0)], receive_picks=[(2, 7, t1)]).status_code == 400, "that draft has been held"

# ---- Traditional Redraft: no pick trading --------------------------------------------------------------------
lid_t, (a0, a1, a2, a3) = make_league("Trad", "traditional_redraft")
run_draft(lid_t)
assert picks_of(lid_t, a0) == []
assert propose(lid_t, a0, a1, send_picks=[(2, 1, a0)], receive_picks=[(2, 1, a1)]).status_code == 400

# ---- Keeper and Dynasty: the next N drafts, rookie rounds ------------------------------------------------------
for fmt in ("keeper", "dynasty"):
    lid_k, (k0, k1, k2, k3) = make_league(fmt.title(), fmt)
    run_draft(lid_k)
    mine = picks_of(lid_k, k0)
    assert {p["season"] for p in mine} == {2, 3} and len(mine) == 2 * 3, "default: two drafts, three rookie rounds"
    assert client.patch(f"/leagues/{lid_k}/settings", json={"pick_trade_seasons": 3}, headers=H_OTHER).status_code == 403
    resp = client.patch(f"/leagues/{lid_k}/settings", json={"pick_trade_seasons": 3}, headers=H)
    assert resp.status_code == 200 and resp.json()["pick_trade_seasons"] == 3
    assert {p["season"] for p in picks_of(lid_k, k0)} == {2, 3, 4}
    trade = propose(lid_k, k0, k1, send_picks=[(3, 2, k0)], receive_picks=[(4, 1, k1)])
    assert trade.status_code == 200, trade.text
    assert client.post(f"/trades/{trade.json()['id']}/respond", json={"team_id": k1, "accept": True}, headers=H).json()["status"] == "accepted"
    assert (4, 1, k1) in {key(p) for p in picks_of(lid_k, k0)} and (3, 2, k0) in {key(p) for p in picks_of(lid_k, k1)}
    assert propose(lid_k, k0, k1, send_picks=[(2, 4, k0)], receive_picks=[(2, 1, k1)]).status_code == 400, "only three rookie rounds"

# The commissioner can switch pick trading off: nothing is tradeable, new trades with picks are refused, and a pick that has
# already changed hands stays with its owner. Switching it back on restores everything.
lid_o, (o0, o1, o2, o3) = make_league("Toggle", "keeper")
run_draft(lid_o)
assert propose(lid_o, o0, o1, send_picks=[(2, 1, o0)], receive_picks=[(2, 1, o1)]).status_code == 200
assert client.get(f"/leagues/{lid_o}/overview").json()["pick_trading_on"] is True
assert client.patch(f"/leagues/{lid_o}/settings", json={"pick_trading_enabled": False}, headers=H_OTHER).status_code == 403
resp = client.patch(f"/leagues/{lid_o}/settings", json={"pick_trading_enabled": False}, headers=H)
assert resp.status_code == 200 and resp.json()["pick_trading_enabled"] is False
assert picks_of(lid_o, o0) == [], "nothing to trade while it is off"
off = propose(lid_o, o0, o1, send_picks=[(2, 2, o0)], receive_picks=[(2, 2, o1)])
assert off.status_code == 400 and "turned off" in off.json()["detail"], off.text
assert client.get(f"/leagues/{lid_o}/overview").json()["pick_trading_on"] is False
assert client.patch(f"/leagues/{lid_o}/settings", json={"pick_trading_enabled": True}, headers=H).json()["pick_trading_enabled"] is True
assert len(picks_of(lid_o, o0)) == 6 and propose(lid_o, o0, o1, send_picks=[(2, 2, o0)], receive_picks=[(2, 2, o1)]).status_code == 200

# Deleting a league clears its traded picks.
assert client.delete(f"/leagues/{lid}", headers=H).status_code == 200
db.expire_all()
assert db.query(TradedPick).filter_by(league_id=lid).count() == 0

print("Pick trading smoke test passed: formats, ownership, validation, human and AI trades, valuation, draft ownership.")
