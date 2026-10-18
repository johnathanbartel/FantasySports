"""
Commissioner tools through the real HTTP API (FastAPI TestClient) on a throwaway SQLite file
(commissioner_tools_smoke_test.db): co-commissioners (who can add them, what they can and can't do), transaction counts
and their adjustment, the FAAB refusal in a league with no budget, the hand-set waiver order and its reset, roster
edits (add / drop / move, waivers or not), lines edited for another person's team, league history edits, the action
log, and the "league settings changed" emails.

    .venv-api/Scripts/python.exe scripts/seed_commissioner_tools_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

db_path = Path(os.environ.get("COMMISSIONER_SMOKE_DB", "commissioner_tools_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
os.environ.pop("FSC_SMTP_HOST", None)               # no outside calls: emails are only recorded
os.environ.pop("FSC_GRANT_ALL_PREMIUM", None)
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import commissioner, waivers
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    CommissionerAction, Draft, DraftableAsset, EmailOutbox, FreeAgencyTransaction, League, LeagueSeason, LeagueUniverse,
    Player, RealTeam, RosterEntry, Team, Universe, User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app, follow_redirects=False)
db = SessionLocal()
checks = 0


def check(cond, msg):
    global checks
    checks += 1
    print(("ok    " if cond else "FAIL  ") + msg)
    if not cond:
        raise SystemExit(1)


# ---- seed: a 4-team NHL league: commissioner, co-commissioner-to-be and a manager each own a team; one AI team ----------
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "co", "manager", "outsider")}
db.add_all(users.values())
nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL")
db.add(nhl)
db.flush()
league = League(name="Commish League", commissioner_user_id=users["commish"].id, size=4, draft_rounds=3, draft_type="snake",
                series_length_days=7)
db.add(league)
db.flush()
db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id, defense_mode="group"))
owners = [users["commish"].id, users["co"].id, users["manager"].id, None]
teams = [Team(league_id=league.id, name=f"Team {i}", owner_user_id=o, is_ai_owned=o is None, program_size="small")
         for i, o in enumerate(owners, start=1)]
school = RealTeam(universe_id=nhl.id, name="Boston", external_ref="BOS")
db.add_all(teams + [school])
db.flush()
assets, players = [], []
for i in range(1, 9):
    p = Player(universe_id=nhl.id, real_team_id=school.id, full_name=f"Skater {i}", position="C")
    db.add(p)
    db.flush()
    a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
    db.add(a)
    assets.append(a)
    players.append(p)
db.flush()
db.add(Draft(league_id=league.id, status="complete"))
db.add(RosterEntry(team_id=teams[2].id, draftable_asset_id=assets[0].id, acquired_via="draft"))     # manager has Skater 1
db.add(RosterEntry(team_id=teams[1].id, draftable_asset_id=assets[1].id, acquired_via="draft"))     # co has Skater 2
db.commit()
LID = league.id
T = [t.id for t in teams]                       # commish, co, manager, AI
A = [a.id for a in assets]
P = [p.id for p in players]


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


H = {n: login(u) for n, u in users.items()}
U = {n: u.id for n, u in users.items()}

# ---- who can open the tab ---------------------------------------------------------------------------------------------------
check(client.get(f"/leagues/{LID}/commissioner", headers=H["manager"]).status_code == 403, "a manager can't open the commissioner overview")
check(client.get(f"/leagues/{LID}/commissioner", headers=H["outsider"]).status_code == 403, "an outsider can't either")
check(client.get(f"/leagues/{LID}/commissioner").status_code == 401, "no token is a 401")
ov = client.get(f"/leagues/{LID}/commissioner", headers=H["commish"]).json()
check(ov["is_primary"] and [p["role"] for p in ov["commissioners"]] == ["commissioner"], "the commissioner sees themselves, primary")
check({p["user_id"] for p in ov["eligible_co_commissioners"]} == {U["co"], U["manager"]}, "managers are eligible; the commissioner and outsiders aren't")
check(not ov["has_budget"] and all(t["faab_balance"] is None for t in ov["teams"]), "a pro league has no FAAB balances")
check(ov["waiver_basis"] == "draft", "waiver order starts on the draft fallback")

# ---- co-commissioners -------------------------------------------------------------------------------------------------------
def add_co(uid, who="commish"):
    return client.post(f"/leagues/{LID}/co-commissioners", json={"user_id": uid}, headers=H[who])


check(add_co(U["outsider"]).status_code == 400, "a co-commissioner has to manage a team in the league")
check(add_co(U["commish"]).status_code == 400, "the commissioner can't be their own co-commissioner")
check(add_co(U["co"], who="manager").status_code == 403, "a manager can't add a co-commissioner")
resp = add_co(U["co"])
check(resp.status_code == 200 and [p["role"] for p in resp.json()] == ["commissioner", "co_commissioner"], "the commissioner adds a co-commissioner")
check(add_co(U["co"]).status_code == 400, "adding the same person again is refused")
check(add_co(U["manager"], who="co").status_code == 403, "a co-commissioner can't add another")
check(client.get(f"/leagues/{LID}/commissioner", headers=H["co"]).json()["is_primary"] is False, "the co-commissioner sees the tab, not primary")
check(client.get(f"/leagues/{LID}/overview").json()["co_commissioner_user_ids"] == [U["co"]], "the overview names the co-commissioner")
check(client.delete(f"/leagues/{LID}", headers=H["co"]).status_code == 403, "a co-commissioner can't delete the league")
check(client.delete(f"/leagues/{LID}/co-commissioners/{U['co']}", headers=H["manager"]).status_code == 403, "a manager can't remove one")

# ---- counters: transactions -------------------------------------------------------------------------------------------------
now = datetime.datetime.utcnow()
db.add(FreeAgencyTransaction(league_id=LID, team_id=T[2], draftable_asset_id=A[2], kind="add", price=0, reason="user", created_at=now))
db.add(FreeAgencyTransaction(league_id=LID, team_id=T[2], draftable_asset_id=A[3], kind="add", price=0, reason="ai_fill", created_at=now))
db.commit()
counts = {c["team_id"]: c for c in client.get(f"/leagues/{LID}/transaction-counts").json()}
check(counts[T[2]]["total"] == 1 and counts[T[0]]["total"] == 0, "counts include a team's own signings, not the AI's required fill")
resp = client.put(f"/leagues/{LID}/teams/{T[2]}/transactions", json={"total": 7}, headers=H["co"])
check(resp.status_code == 200 and resp.json()["total"] == 7 and resp.json()["adjustment"] == 6, "a co-commissioner sets a count; the gap is stored as an adjustment")
db.add(FreeAgencyTransaction(league_id=LID, team_id=T[2], draftable_asset_id=A[4], kind="add", price=0, reason="user", created_at=now))
db.commit()
check({c["team_id"]: c for c in client.get(f"/leagues/{LID}/transaction-counts").json()}[T[2]]["total"] == 8, "later signings add to the adjusted count")
check(client.put(f"/leagues/{LID}/teams/{T[2]}/transactions", json={"total": -1}, headers=H["commish"]).status_code == 422, "a negative count is refused")
check(client.put(f"/leagues/{LID}/teams/{T[2]}/transactions", json={"total": 3}, headers=H["manager"]).status_code == 403, "a manager can't edit counts")
check(client.put(f"/leagues/{LID}/teams/{T[2]}/faab", json={"balance": 100}, headers=H["commish"]).status_code == 400, "no FAAB balance to set in a league with no budget")

# ---- waiver order -----------------------------------------------------------------------------------------------------------
want = [T[0], T[2], T[1], T[3]]
check(client.put(f"/leagues/{LID}/waiver-order", json={"team_ids": want[:3]}, headers=H["commish"]).status_code == 400, "the order has to list every team")
check(client.put(f"/leagues/{LID}/waiver-order", json={"team_ids": want[:3] + [T[0]]}, headers=H["commish"]).status_code == 400, "...once each")
check(client.put(f"/leagues/{LID}/waiver-order", json={"team_ids": want}, headers=H["manager"]).status_code == 403, "a manager can't set it")
resp = client.put(f"/leagues/{LID}/waiver-order", json={"team_ids": want}, headers=H["co"])
check(resp.status_code == 200 and resp.json()["basis"] == "commissioner" and resp.json()["team_ids"] == want, "a co-commissioner sets the waiver order")
w = client.get(f"/leagues/{LID}/waivers").json()
check(w["basis"] == "commissioner" and [r["team_id"] for r in w["order"]] == want, "the Transactions tab's waiver list shows it")
later = waivers.next_reset(datetime.datetime.utcnow()) + datetime.timedelta(minutes=1)
check(waivers.waiver_order(db, LID, later).basis != "commissioner", "it lapses at the next weekly reset")

# ---- rosters ----------------------------------------------------------------------------------------------------------------
def roster(action, **kw):
    return client.post(f"/leagues/{LID}/commissioner/roster", json={"action": action, **kw}, headers=H["co"])


def rostered(team, asset):
    db.expire_all()
    return db.query(RosterEntry).filter_by(team_id=team, draftable_asset_id=asset).count() == 1


hits = client.get(f"/leagues/{LID}/commissioner/players", params={"q": "skater 5"}, headers=H["co"]).json()
check([h["display_name"] for h in hits] == ["Skater 5"] and hits[0]["team_id"] is None, "player search finds a free agent")
check(client.get(f"/leagues/{LID}/commissioner/players", params={"q": "s"}, headers=H["co"]).status_code == 400, "search needs two letters")
check(client.get(f"/leagues/{LID}/commissioner/players", params={"q": "skater"}, headers=H["manager"]).status_code == 403, "managers can't search through the commissioner tool")
check(roster("add", team_id=T[0], draftable_asset_id=A[4]).status_code == 200 and rostered(T[0], A[4]), "adds a free agent to a team, free")
check(roster("add", team_id=T[1], draftable_asset_id=A[4]).status_code == 400, "can't add someone already on a roster")
check(roster("add", team_id=999, draftable_asset_id=A[5]).status_code == 400, "can't add to a team outside the league")
db.expire_all()
check(commissioner.transaction_counts(db, LID)[T[0]]["total"] == 0, "a commissioner's roster edit isn't the team's transaction")
check(roster("drop", team_id=T[0], draftable_asset_id=A[4]).status_code == 200 and not rostered(T[0], A[4]), "drops a player")
check(waivers.waiver_until(db, LID, datetime.datetime.utcnow()) == {}, "a commissioner drop skips waivers by default")
check(roster("add", team_id=T[0], draftable_asset_id=A[4]).status_code == 200, "...so he can be added straight back")
check(roster("drop", team_id=T[0], draftable_asset_id=A[4], put_on_waivers=True).status_code == 200
      and A[4] in waivers.waiver_until(db, LID, datetime.datetime.utcnow()), "put_on_waivers gives him the usual 24 hours")
check(roster("move", team_id=T[0], from_team_id=T[2], draftable_asset_id=A[0]).status_code == 200
      and rostered(T[0], A[0]) and not rostered(T[2], A[0]), "moves a player between two teams")
check(roster("move", team_id=T[0], from_team_id=T[2], draftable_asset_id=A[0]).status_code == 400, "...and not from a team that doesn't have him")
check(roster("move", team_id=T[0], from_team_id=T[0], draftable_asset_id=A[0]).status_code == 400, "...or to the same team")
check(client.post(f"/leagues/{LID}/commissioner/roster", json={"action": "add", "team_id": T[0], "draftable_asset_id": A[5]},
                  headers=H["manager"]).status_code == 403, "a manager can't use the roster tool")
check(client.post(f"/leagues/{LID}/commissioner/roster", json={"action": "teleport", "team_id": T[0], "draftable_asset_id": A[5]},
                  headers=H["co"]).status_code == 422, "an unknown action is refused")

# ---- lines for another person's team ----------------------------------------------------------------------------------------
lines = {"assignments": {"C": [P[1]]}}      # Skater 2 is on the co-commissioner's team (T[1]); the commissioner edits it
check(client.put(f"/teams/{T[1]}/pecking-order", json=lines, headers=H["outsider"]).status_code == 403, "an outsider can't set another team's lines")
check(client.put(f"/teams/{T[1]}/pecking-order", json=lines, headers=H["manager"]).status_code == 403, "a manager can't either")
resp = client.put(f"/teams/{T[1]}/pecking-order", json=lines, headers=H["commish"])
check(resp.status_code == 200, f"the commissioner sets a manager's lines ({resp.status_code} {resp.text[:120]})")
db.expire_all()
check(db.query(CommissionerAction).filter_by(league_id=LID, action="lines_edit", team_id=T[1]).count() == 1, "...and it is logged")
client.put(f"/teams/{T[1]}/pecking-order", json=lines, headers=H["co"])
db.expire_all()
check(db.query(CommissionerAction).filter_by(league_id=LID, action="lines_edit", team_id=T[1]).count() == 1, "an owner editing their own lines isn't logged")
resp = client.put(f"/teams/{T[3]}/pecking-order", json={"assignments": {}}, headers=H["commish"])
check(resp.status_code == 403, "an AI team's lines still need commissioner mode")

# ---- league history ---------------------------------------------------------------------------------------------------------
rows = [{"team_name": "The Old Guard", "wins": 12, "losses": 4}, {"team_id": T[0], "wins": 10, "losses": 6, "ties": 1}]
resp = client.post(f"/leagues/{LID}/seasons", json={"season_number": 0, "standings": rows, "champion_team_id": T[0], "label": "2023",
                                                   "note": "Imported from the old site"}, headers=H["co"])
check(resp.status_code == 200 and resp.json()["standings"][0]["rank"] == 1 and resp.json()["label"] == "2023", "adds a season from before the platform")
check(client.post(f"/leagues/{LID}/seasons", json={"season_number": 0, "standings": rows}, headers=H["co"]).status_code == 400, "...not twice")
check(client.post(f"/leagues/{LID}/seasons", json={"season_number": 1, "standings": rows}, headers=H["co"]).status_code == 400, "...and not as the current season")
check(client.post(f"/leagues/{LID}/seasons", json={"season_number": -1, "standings": [{"team_id": 9999}]}, headers=H["co"]).status_code == 400, "standings rows must name a team in the league")
check(client.post(f"/leagues/{LID}/seasons", json={"season_number": -1, "standings": [{"team_name": "X", "wins": -2}]}, headers=H["co"]).status_code == 400, "negative records are refused")
check(client.post(f"/leagues/{LID}/seasons", json={"season_number": -1, "standings": rows}, headers=H["manager"]).status_code == 403, "a manager can't edit history")
resp = client.patch(f"/leagues/{LID}/seasons/0", json={"champion_team_id": T[2], "note": "Corrected", "standings": [
    {"team_id": T[2], "wins": 14, "losses": 2}, {"team_name": "The Old Guard", "wins": 12, "losses": 4}]}, headers=H["commish"])
check(resp.status_code == 200 and resp.json()["champion_team_id"] == T[2] and resp.json()["standings"][0]["team_id"] == T[2], "edits a season's champion and standings")
listed = client.get(f"/leagues/{LID}/seasons").json()
check(listed[0]["season_number"] == 0 and listed[0]["note"] == "Corrected" and listed[0]["edited_at"] and listed[0]["label"] == "2023", "history lists the note, label and edit time")
resp = client.patch(f"/leagues/{LID}/seasons/0", json={"note": None}, headers=H["commish"])
check(resp.status_code == 200 and resp.json()["note"] is None and resp.json()["champion_team_id"] == T[2], "sending only a field leaves the rest; null clears a note")
check(client.patch(f"/leagues/{LID}/seasons/0", json={"champion_team_id": 9999}, headers=H["commish"]).status_code == 400, "the champion must be a team in the league")
check(client.patch(f"/leagues/{LID}/seasons/42", json={"note": "x"}, headers=H["commish"]).status_code == 400, "an unknown season is refused")
league_row = db.get(League, LID)
league_row.offseason, league_row.season_number = True, 2
db.add(LeagueSeason(league_id=LID, season_number=1, league_format="traditional_redraft", archived_at=now, data={"standings": []}))
db.commit()
check(client.patch(f"/leagues/{LID}/seasons/1", json={"standings": rows}, headers=H["commish"]).status_code == 400, "last season's standings are locked while they set the next draft's order")
check(client.patch(f"/leagues/{LID}/seasons/1", json={"note": "fine"}, headers=H["commish"]).status_code == 200, "...but its note can change")
league_row.offseason, league_row.season_number = False, 1
db.commit()

# ---- emails when settings change --------------------------------------------------------------------------------------------
db.expire_all()
before = db.query(EmailOutbox).filter_by(kind="settings_changed").count()
resp = client.patch(f"/leagues/{LID}/settings", json={"standings_mode": "win_pct", "points_per_win": 2}, headers=H["co"])
check(resp.status_code == 200, "a co-commissioner changes league settings")
db.expire_all()
mails = db.query(EmailOutbox).filter_by(kind="settings_changed").order_by(EmailOutbox.id).all()[before:]
check({m.to_email for m in mails} == {"commish@example.com", "co@example.com", "manager@example.com"}, "every person in the league is emailed once (not AI teams, not outsiders)")
body = mails[0].body
check("co changed the settings of Commish League" in body and "Standings sorted by: points -> win_pct" in body and "Points per win: 3 -> 2" in body,
      "the email says who changed what")
check(all(m.status == "not_configured" for m in mails), "with no SMTP server the emails are recorded, not sent")
client.patch(f"/leagues/{LID}/settings", json={"standings_mode": "win_pct"}, headers=H["co"])
db.expire_all()
check(db.query(EmailOutbox).filter_by(kind="settings_changed").count() == before + len(mails), "saving without a change sends nothing")
client.patch(f"/leagues/{LID}", json={"name": "Renamed League"}, headers=H["commish"])
db.expire_all()
check(db.query(EmailOutbox).filter(EmailOutbox.body.like("%League name: Commish League -> Renamed League%")).count() == 3, "renaming emails everyone too")
check(client.patch(f"/leagues/{LID}/settings", json={"standings_mode": "points"}, headers=H["manager"]).status_code == 403, "a manager can't change settings")

# ---- the action log, and removing a co-commissioner -------------------------------------------------------------------------
ov = client.get(f"/leagues/{LID}/commissioner", headers=H["commish"]).json()
acts = {a["action"] for a in ov["recent_actions"]}
check({"co_commissioner_added", "transactions_set", "waiver_order_set", "roster_add", "roster_drop", "roster_move", "season_added", "season_edited", "lines_edit"} <= acts,
      "the action log records every kind of edit")
check(all(a["user_name"] for a in ov["recent_actions"]), "...with who did it")
check(len(ov["recent_emails"]) >= 3 and {e["status"] for e in ov["recent_emails"]} == {"not_configured"}, "recent emails are listed with their status")
tx = {t["team_id"]: t for t in ov["teams"]}
check(tx[T[2]]["transactions"]["total"] == 8, "the overview carries the adjusted counts")
check(client.delete(f"/leagues/{LID}/co-commissioners/{U['co']}", headers=H["commish"]).status_code == 200, "the commissioner removes the co-commissioner")
check(client.get(f"/leagues/{LID}/commissioner", headers=H["co"]).status_code == 403, "who loses access at once")
add_co(U["co"])
check(client.delete(f"/leagues/{LID}/co-commissioners/{U['co']}", headers=H["co"]).status_code == 200, "a co-commissioner can step down")
check(client.delete(f"/leagues/{LID}/co-commissioners/{U['co']}", headers=H["commish"]).status_code == 400, "removing someone who isn't one is refused")

# ---- deleting the league cleans up the new tables ---------------------------------------------------------------------------
add_co(U["co"])
check(client.delete(f"/leagues/{LID}", headers=H["commish"]).status_code == 200, "the commissioner deletes the league")
db.expire_all()
check(db.query(CommissionerAction).filter_by(league_id=LID).count() == 0 and db.query(EmailOutbox).filter_by(league_id=LID).count() == 0,
      "its action log and emails go with it")

print(f"\nAll {checks} commissioner-tool checks passed.")
db.close()
engine.dispose()
if db_path.exists():
    db_path.unlink()
