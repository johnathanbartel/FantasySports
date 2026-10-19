"""
Chat through the real HTTP API (FastAPI TestClient) on a throwaway SQLite file (chat_smoke_test.db): league room,
draft room and direct messages - who may use them, validation, the rate limit, removal rules, DM privacy (not even the
commissioner reads one), polling with after_id, and unread counts.

    .venv-api/Scripts/python.exe scripts/seed_chat_smoke_test.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

db_path = Path(os.environ.get("CHAT_SMOKE_DB", "chat_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import chat
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import Draft, Team, User
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()
seed_hockey_reference_data(db)
users = {n: User(email=f"{n}@example.com", display_name=n.title()) for n in ("commish", "ann", "bob", "outsider")}
db.add_all(users.values())
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


HDR = {n: login(u) for n, u in users.items()}
UID = {n: u.id for n, u in users.items()}
checks = 0


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


resp = client.post("/leagues", json={"name": "Chat League", "commissioner_user_id": UID["commish"],
                                     "universe_codes": ["nhl"], "size": 4}, headers=HDR["commish"])
assert resp.status_code == 200, resp.text
LID = resp.json()["id"]
# two human managers and an AI team
db.add_all([Team(league_id=LID, name="Ann's Team", owner_user_id=UID["ann"], is_ai_owned=False, program_size="small"),
            Team(league_id=LID, name="Bob's Team", owner_user_id=UID["bob"], is_ai_owned=False, program_size="small"),
            Team(league_id=LID, name="Robot FC", owner_user_id=None, is_ai_owned=True, program_size="small")])
draft = Draft(league_id=LID, status="in_progress")
other_league_draft = Draft(league_id=LID + 99, status="pending")
db.add_all([draft, other_league_draft])
db.commit()
DID = draft.id


def get(who, thread="league", **kw):
    return client.get(f"/leagues/{LID}/chat", params={"thread": thread, **kw}, headers=HDR[who])


def post(who, thread, body):
    return client.post(f"/leagues/{LID}/chat", json={"thread": thread, "body": body}, headers=HDR[who])


# --- who may use it -----------------------------------------------------------------------------------------------------
check(client.get(f"/leagues/{LID}/chat").status_code == 401, "signing in is required")
check(get("outsider").status_code == 403, "a stranger can't read the league room")
check(post("outsider", "league", "hi").status_code == 403, "a stranger can't post")
r0 = get("ann"); assert r0.status_code == 200, r0.text
check(r0.json()["messages"] == [], "the room starts empty")

# --- league room ------------------------------------------------------------------------------------------------------------
first = post("ann", "league", "  Good luck everyone  ")
check(first.status_code == 200 and first.json()["body"] == "Good luck everyone", "a post is trimmed and returned")
check(first.json()["sender_name"] == "Ann" and first.json()["team_name"] == "Ann's Team", "it carries the name and team")
second = post("commish", "league", "Draft is Sunday")
check(second.json()["team_name"] is None, "a commissioner with no team shows just their name")
msgs = get("bob").json()["messages"]
check([m["body"] for m in msgs] == ["Good luck everyone", "Draft is Sunday"], "everyone sees the room oldest first")
check(get("bob", after_id=first.json()["id"]).json()["messages"][0]["id"] == second.json()["id"], "after_id returns only newer messages")
check(len(get("bob", before_id=second.json()["id"]).json()["messages"]) == 1, "before_id pages back")

# --- validation -------------------------------------------------------------------------------------------------------------
check(post("ann", "league", "   ").status_code == 422, "an empty message is refused")
check(post("ann", "league", "x" * 1001).status_code == 422, "an over-long message is refused")
check(post("ann", "nonsense", "hi").status_code == 422, "a bad thread is refused")
check(post("ann", "dm:abc", "hi").status_code == 422, "a non-numeric dm id is refused")
check(post("ann", f"draft:{other_league_draft.id}", "hi").status_code == 404, "another league's draft isn't reachable")
check(post("ann", "dm:%d" % UID["ann"], "hi").status_code == 422, "no messaging yourself")
check(post("ann", "dm:%d" % UID["outsider"], "hi").status_code == 404, "no messaging someone outside the league")

# --- removal -----------------------------------------------------------------------------------------------------------------
bobs = post("bob", "league", "annoying message").json()
check(client.delete(f"/leagues/{LID}/chat/{bobs['id']}", headers=HDR["ann"]).status_code == 403, "another manager can't remove it")
check(client.delete(f"/leagues/{LID}/chat/{bobs['id']}", headers=HDR["commish"]).status_code == 200, "the commissioner can")
gone = [m for m in get("ann").json()["messages"] if m["id"] == bobs["id"]][0]
check(gone["deleted"] and gone["body"] is None, "a removed message keeps its place but loses its text")
mine = post("ann", "league", "typo").json()
check(client.delete(f"/leagues/{LID}/chat/{mine['id']}", headers=HDR["ann"]).status_code == 200, "you can remove your own")
check(client.delete(f"/leagues/{LID}/chat/99999", headers=HDR["ann"]).status_code == 404, "an unknown message is a 404")

# --- draft room --------------------------------------------------------------------------------------------------------------
check(post("ann", f"draft:{DID}", "taking the top forward").status_code == 200, "a manager posts in the draft room")
check(get("bob", f"draft:{DID}").json()["messages"][0]["body"] == "taking the top forward", "others see it there")
check(all("forward" not in (m["body"] or "") for m in get("bob").json()["messages"]), "and not in the league room")
check(get("outsider", f"draft:{DID}").status_code == 403, "a stranger can't read it")

# --- direct messages ---------------------------------------------------------------------------------------------------------
dm = post("ann", "dm:%d" % UID["bob"], "trade you my 1st for your goalie?")
check(dm.status_code == 200, "a manager messages another")
check(get("bob", "dm:%d" % UID["ann"]).json()["messages"][0]["body"].startswith("trade you"), "the recipient reads it")
check(get("ann", "dm:%d" % UID["bob"]).json()["messages"][0]["id"] == dm.json()["id"], "so does the sender")
check(get("commish", "dm:%d" % UID["bob"]).json()["messages"] == [], "the commissioner reading their own thread with bob sees nothing of it")
check(get("commish", "dm:%d" % UID["ann"]).json()["messages"] == [], "...nor with ann")
check(client.delete(f"/leagues/{LID}/chat/{dm.json()['id']}", headers=HDR["commish"]).status_code == 403, "a commissioner can't remove a DM")
check(client.delete(f"/leagues/{LID}/chat/{dm.json()['id']}", headers=HDR["bob"]).status_code == 403, "nor can the recipient")

# --- unread counts -----------------------------------------------------------------------------------------------------------
post("commish", "league", "one")
post("commish", "league", "two")
s = client.get(f"/leagues/{LID}/chat/summary", headers=HDR["bob"]).json()
check(s["league_unread"] >= 2 and s["draft"]["draft_id"] == DID, "bob has unread league messages and a draft room")
check(s["draft"]["unread"] == 1 and s["dms"][0]["unread"] == 1 and s["dms"][0]["user_id"] == UID["ann"], "and unread draft and DM counts")
check(s["total_unread"] == s["league_unread"] + 1 + 1, "the total adds them up")
check(client.post(f"/leagues/{LID}/chat/read", json={"thread": "league"}, headers=HDR["bob"]).status_code == 200, "mark the league room read")
check(client.get(f"/leagues/{LID}/chat/summary", headers=HDR["bob"]).json()["league_unread"] == 0, "league unread clears")
a = client.get(f"/leagues/{LID}/chat/summary", headers=HDR["ann"]).json()
check(a["dms"][0]["unread"] == 0, "the sender's own DM isn't unread for them")
members = client.get(f"/leagues/{LID}/chat/members", headers=HDR["ann"]).json()
check({m["user_id"] for m in members} == {UID["bob"], UID["commish"]}, "members lists everyone but you")

# --- rate limit --------------------------------------------------------------------------------------------------------------
codes = [post("bob", "league", f"spam {i}").status_code for i in range(chat.RATE_LIMIT_MESSAGES + 3)]
check(429 in codes, "a burst of messages is rate limited")

print(f"chat smoke test passed ({checks} checks)")
