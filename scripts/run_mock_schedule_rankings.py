"""
End-to-end check of the PRESEASON SCHEDULE STAGE, the week-10 CONFERENCE CHAMPIONSHIPS and the
RANKINGS / POLL, through the real HTTP API on a throwaway copy of user_run.db (2026 season data).

  * premade schedule: weeks 1-9, everyone plays once a week, nobody plays anyone twice
  * requests: only teams without an agreement are offered; cash is capped at what a team has;
    the cash moves only when the request is accepted; declining moves nothing
  * an accepted request rewires both teams' weeks and pairs up their former opponents
  * AI teams answer at the close of the stage (one request -> accepted, several -> one of them);
    everything else open is declined; after the close nothing can change
  * championships: #1 v #2, #3 v #4, ...; an odd conference's worst team has a bye
  * rankings: the poll's size formula, ballots add up, the rank shows in the league overview

    .venv-api/Scripts/python.exe scripts/run_mock_schedule_rankings.py
"""

import os
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
LIVE_DB, RUN_DB = ROOT / "user_run.db", ROOT / "mock_schedule_run.db"
if not LIVE_DB.exists():
    sys.exit("user_run.db missing")
shutil.copyfile(LIVE_DB, RUN_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{RUN_DB.as_posix()}"
os.environ.pop("FSC_GRANT_ALL_PREMIUM", None)

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import entitlements, free_agency, rankings, schedule, scoring
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models import core  # noqa: F401
from fantasy_sports.platform.db.models.core import MatchupRequest, Team, User, WeeklyMatchup
from fantasy_sports.platform.db.session import SessionLocal, engine


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    Base.metadata.create_all(bind=engine)
    client = TestClient(app)
    db = SessionLocal()
    users = db.query(User).order_by(User.id).all()
    boss, u1, u2 = users[0], users[1], users[2]
    entitlements.grant(db, boss.id, entitlements.COMMISSIONER_MODE)
    db.commit()

    def login(uid):
        r = client.post("/sessions", json={"user_id": uid})
        check(r.status_code == 200, r.text)
        return {"Authorization": "Bearer " + r.json()["token"]}

    B, U1, U2 = login(boss.id), login(u1.id), login(u2.id)

    # ---- pure rules first -------------------------------------------------------------------------------
    check(schedule.pair_conference([1, 2, 3, 4]) == ([(1, 2), (3, 4)], None), "even conference pairing")
    check(schedule.pair_conference([1, 2, 3, 4, 5]) == ([(1, 2), (3, 4)], 5), "odd conference: worst team gets the bye")
    check(schedule.pair_conference([7]) == ([], 7) and schedule.pair_conference([]) == ([], None), "tiny conferences")
    check([rankings.poll_size(n) for n in (2, 3, 4, 8, 12, 20, 32, 64, 136)] == [1, 2, 3, 4, 5, 8, 13, 25, 25],
          f"poll size formula: {[rankings.poll_size(n) for n in (2, 3, 4, 8, 12, 20, 32, 64, 136)]}")
    print("Rules: championship pairings (even / odd / tiny conference) and the poll-size formula check out")

    # ---- a drafted league with a premade schedule --------------------------------------------------------
    league = client.post("/leagues/sandbox", json={"name": "Schedule Test", "recruiting_period_seconds": 3600}, headers=B).json()
    lid = league["id"]
    check(client.post(f"/recruiting/league/{lid}/simulate", headers=B).status_code == 200, "simulate")
    r = client.post(f"/leagues/{lid}/schedule/auto", json={}, headers=B)
    check(r.status_code == 200 and r.json()["matchups"] == 54, r.text)
    scoring.is_locked = lambda week, today=None: False          # the stage is tested independent of today's date
    weeks = client.get(f"/leagues/{lid}/weeks").json()
    W = {w["week_number"]: w["id"] for w in weeks}

    def check_pairings(label):
        db.expire_all()
        met = Counter()
        for n in range(1, 10):
            ms = db.query(WeeklyMatchup).filter_by(league_id=lid, week_id=W[n]).all()
            ids = [t for m in ms for t in (m.team_a_id, m.team_b_id)]
            check(len(ids) == 12 and len(set(ids)) == 12, f"{label}: week {n} isn't a valid pairing of all 12 teams")
            for m in ms:
                met[frozenset((m.team_a_id, m.team_b_id))] += 1
        check(all(v == 1 for v in met.values()) or label != "premade", "premade: someone plays the same team twice")

    check_pairings("premade")
    check(db.query(WeeklyMatchup).filter_by(league_id=lid, week_id=W[10]).count() == 0, "week 10 must not be premade")
    print("Premade schedule: weeks 1-9, all 12 teams play every week, no repeat opponents (week 10 left for championships)")

    teams = {t.id: t for t in db.query(Team).filter_by(league_id=lid)}
    human1, human2 = sorted(teams)[0], sorted(teams)[1]
    for tid, uid in ((human1, u1.id), (human2, u2.id)):
        teams[tid].is_ai_owned, teams[tid].owner_user_id = False, uid
    db.commit()
    ai = [t for t in sorted(teams) if t not in (human1, human2)]
    x, y = ai[0], ai[1]

    def money(tid):
        db.expire_all()
        return free_agency.team_money(db, db.get(Team, tid))["fa_money"]

    # ---- reading the schedule ------------------------------------------------------------------------------------
    s1 = client.get(f"/teams/{human1}/schedule", headers=U1).json()
    check(len(s1["weeks"]) == 9 and s1["cash_available"] == money(human1), "team schedule / cash")
    check(all(not w["agreement"] and human1 not in [a["id"] for a in w["available"]] and len(w["available"]) == 11 for w in s1["weeks"]),
          "with no agreements every other team is available")
    check("outgoing" not in client.get(f"/teams/{human1}/schedule").json()["weeks"][0], "requests are private to the team's managers")

    # ---- requests: validation ---------------------------------------------------------------------------------------
    w4, w5, w6 = W[4], W[5], W[6]
    body = lambda wk, to, offer=0: {"week_id": wk, "to_team_id": to, "offer_amount": offer}
    check(client.post(f"/teams/{human1}/schedule/requests", json=body(w4, x)).status_code == 401, "needs a token")
    check(client.post(f"/teams/{human1}/schedule/requests", json=body(w4, x), headers=U2).status_code == 403, "not your team")
    check(client.post(f"/teams/{human1}/schedule/requests", json=body(w4, human1), headers=U1).status_code == 400, "can't ask yourself")
    check(client.post(f"/teams/{human1}/schedule/requests", json=body(W[10], x), headers=U1).status_code == 400, "week 10 isn't arranged")
    cash1 = money(human1)
    check(client.post(f"/teams/{human1}/schedule/requests", json=body(w4, x, cash1 + 1), headers=U1).status_code == 400, "can't offer more than you have")
    check(client.post(f"/teams/{human1}/schedule/requests", json=body(w4, x, -5), headers=U1).status_code == 400, "negative offer")
    print("Requests: token, ownership, self-request, week 10 and over-budget offers are all refused")

    # ---- user -> user: cash moves only on acceptance ---------------------------------------------------------------------
    cash2 = money(human2)
    r = client.post(f"/teams/{human1}/schedule/requests", json=body(w5, human2, 25), headers=U1)
    check(r.status_code == 200, r.text)
    rid = r.json()["id"]
    check(money(human1) == cash1 and money(human2) == cash2, "cash must not move before acceptance")
    inc = client.get(f"/teams/{human2}/schedule", headers=U2).json()["weeks"][4]["incoming"]
    check(len(inc) == 1 and inc[0]["offer_amount"] == 25 and inc[0]["from_team_id"] == human1, "receiver sees the offer")
    room = client.get(f"/teams/{human1}/schedule", headers=U1).json()["cash_available"]
    check(room == cash1 - 25, f"pending offers reduce what can still be offered: {room} vs {cash1 - 25}")
    check(client.post(f"/schedule/requests/{rid}/respond", json={"accept": True}, headers=U1).status_code == 403, "only the receiver answers")
    # a new request for the same week replaces the old one
    r2 = client.post(f"/teams/{human1}/schedule/requests", json=body(w5, x, 5), headers=U1).json()
    db.expire_all()
    check(db.get(MatchupRequest, rid).status == "withdrawn", "a new request for the week should replace the old")
    check(client.post(f"/schedule/requests/{rid}/respond", json={"accept": True}, headers=U2).status_code == 400, "a withdrawn request can't be accepted")
    r3 = client.post(f"/teams/{human1}/schedule/requests", json=body(w5, human2, 25), headers=U1).json()
    check(client.delete(f"/schedule/requests/{r3['id']}", headers=U1).status_code == 200, "withdraw")
    r4 = client.post(f"/teams/{human1}/schedule/requests", json=body(w5, human2, 25), headers=U1).json()
    check(client.post(f"/schedule/requests/{r4['id']}/respond", json={"accept": False}, headers=U2).status_code == 200, "decline")
    check(money(human1) == cash1 and money(human2) == cash2, "declining moves no cash")
    r5 = client.post(f"/teams/{human1}/schedule/requests", json=body(w5, human2, 25), headers=U1).json()
    r = client.post(f"/schedule/requests/{r5['id']}/respond", json={"accept": True}, headers=U2)
    check(r.status_code == 200 and r.json()["status"] == "accepted", r.text)
    check(money(human1) == cash1 - 25 and money(human2) == cash2 + 25, "accepted: cash moves sender -> receiver")
    check_pairings("after agreement")
    db.expire_all()
    m = schedule._matchup(db, lid, w5, human1)
    check({m.team_a_id, m.team_b_id} == {human1, human2}, "the two teams now play each other in week 5")
    s2 = client.get(f"/teams/{human1}/schedule", headers=U1).json()["weeks"][4]
    check(s2["agreement"] and s2["opponent_id"] == human2 and not s2["editable"] and s2["available"] == [], "week 5 is now an agreement")
    avail_x = [a["id"] for a in client.get(f"/teams/{x}/schedule").json()["weeks"][4]["available"]] if False else \
        [t.id for t in schedule.available_opponents(db, db.get(Team, x), w5)]
    check(human1 not in avail_x and human2 not in avail_x and len(avail_x) == 9, f"teams with agreements aren't offered: {avail_x}")
    check(client.post(f"/teams/{human2}/schedule/requests", json=body(w5, x), headers=U2).status_code == 400, "a team with an agreement can't ask")
    check(any(client.post(f"/teams/{human2}/schedule/requests", json=body(w5, x), headers=U2).status_code == 400 for _ in [0]), "still refused")
    print(f"User-to-user: offer of $25 held back until accepted ({cash1} -> {cash1 - 25} and {cash2} -> {cash2 + 25}); "
          "weeks stay valid pairings; teams with an agreement drop out of the dropdown")

    # ---- AI answers at the close ---------------------------------------------------------------------------------------------
    # week 6: both humans ask AI team x (x must pick one); week 4: only human1 asks AI team y (accepted)
    a = client.post(f"/teams/{human1}/schedule/requests", json=body(w6, x, 10), headers=U1)
    b = client.post(f"/teams/{human2}/schedule/requests", json=body(w6, x, 20), headers=U2)
    c = client.post(f"/teams/{human1}/schedule/requests", json=body(w4, y, 15), headers=U1)
    d = client.post(f"/teams/{human2}/schedule/requests", json=body(W[7], human1, 5), headers=U2)      # to a user who never answers
    check(all(z.status_code == 200 for z in (a, b, c, d)), [z.text for z in (a, b, c, d)])
    check(client.post(f"/leagues/{lid}/schedule/close", headers=U1).status_code == 403, "only the commissioner closes")
    r = client.post(f"/leagues/{lid}/schedule/close", headers=B)
    check(r.status_code == 200 and r.json()["accepted"] == 2, r.text)
    db.expire_all()
    st = {q.id: q.status for q in db.query(MatchupRequest).filter_by(league_id=lid)}
    check(sorted([st[a.json()["id"]], st[b.json()["id"]]]) == ["accepted", "declined"], "AI team picks exactly one of two requests")
    check(st[c.json()["id"]] == "accepted", "a single request to an AI team is accepted")
    check(st[d.json()["id"]] == "declined", "an unanswered request to a user is declined at the close")
    check(all(v != "pending" for v in st.values()), "nothing is left pending")
    check_pairings("after close")
    check(client.post(f"/teams/{human1}/schedule/requests", json=body(W[8], x), headers=U1).status_code == 400, "closed stage refuses requests")
    check(client.post(f"/leagues/{lid}/schedule/close", headers=B).status_code == 400, "already closed")
    print("Close: AI team took one of two rival requests, took the lone request, declined nothing it shouldn't; user request left open was declined")
    tm = free_agency.team_money(db, db.get(Team, x))
    check(tm["schedule_cash_received"] in (10, 20), f"the AI team should have been paid the accepted offer: {tm}")

    # ---- championships and rankings ----------------------------------------------------------------------------------------------
    ch = client.get(f"/leagues/{lid}/championships").json()
    check(ch["ready"] is False and ch["games"] == [], "championships aren't set until week 9 is scored")
    conf_of = {t.id: t.conference_id for t in db.query(Team).filter_by(league_id=lid)}
    fake = [{"team_id": t} for t in sorted(teams)]
    games, byes = schedule.championship_bracket(db, lid, fake)
    check(len(games) == 6 and not byes and all(conf_of[g.team_a_id] == conf_of[g.team_b_id] for g in games), "12 teams -> 6 conference games")
    games, byes = schedule.championship_bracket(db, lid, fake[:11])
    check(len(games) == 5 and len(byes) == 1, "an odd conference leaves exactly one bye")

    # pretend weeks 1-9 are all scored (no lineups are set, so every game is a 0-0 tie): week 10 must then appear
    real_scored = scoring.scored_week_ids
    scoring.scored_week_ids = lambda db_, ids: set(ids) - {W[10]}
    try:
        ch = client.get(f"/leagues/{lid}/championships").json()
        check(ch["ready"] and len(ch["games"]) == 6 and ch["byes"] == [], f"championships once week 9 is scored: {ch}")
        for g in ch["games"]:
            check(conf_of[g["team_a_id"]] == conf_of[g["team_b_id"]] == g["conference_id"], "a championship is within one conference")
        played = client.get(f"/leagues/{lid}/matchups", params={"week_id": W[10]}).json()
        check(len(played) == 6 and all(m["championship"] and m["matchup_id"] is None and not m["scored"] for m in played), "week 10 matchups")
        st = client.get(f"/leagues/{lid}/standings").json()
        check(all(r["wins"] + r["losses"] + r["ties"] == 9 for r in st), "standings count the 9 regular-season games only")
    finally:
        scoring.scored_week_ids = real_scored

    rk = client.get(f"/leagues/{lid}/rankings").json()
    check(rk["size"] == 5 and len(rk["ranked"]) == 5 and rk["voters"] == 25, f"poll shape: {rk['size']}")
    check([r["rank"] for r in rk["ranked"]] == [1, 2, 3, 4, 5], "ranks")
    total = sum(r["points"] for r in rk["ranked"]) + sum(o["points"] for o in rk["others"])
    check(total == rk["voters"] * rk["size"] * (rk["size"] + 1) // 2, f"ballots don't add up: {total}")
    check(sum(r["first_place_votes"] for r in rk["ranked"]) == rk["voters"], "one first-place vote per voter")
    check([r["points"] for r in rk["ranked"]] == sorted((r["points"] for r in rk["ranked"]), reverse=True), "poll order")
    epa = [t["remaining_credit"] for t in rk["epa_ranking"]]
    check(len(epa) == 12 and epa == sorted(epa, reverse=True) and [t["epa_rank"] for t in rk["epa_ranking"]] == list(range(1, 13)), "EPA ranking order")
    check(rk["as_of_week"] == 3 and rk["remaining_weeks"] == [4, 5, 6, 7, 8, 9, 10], f"remaining weeks: {rk['remaining_weeks']}")
    check(client.get(f"/leagues/{lid}/rankings").json()["ranked"] == rk["ranked"], "the poll must be reproducible")
    ov = client.get(f"/leagues/{lid}/overview").json()
    ranked = {t["id"]: t["poll_rank"] for t in ov["teams"] if t["poll_rank"]}
    check(ranked == {r["team_id"]: r["rank"] for r in rk["ranked"]} and len(ranked) == 5, "poll rank appears on the overview teams")
    print(f"Rankings: top {rk['size']} of 12 polled by {rk['voters']} voters ({rk['ranked'][0]['team_name']} #1 with "
          f"{rk['ranked'][0]['first_place_votes']} first-place votes), {len(rk['others'])} teams receiving votes; ranks appear on the overview")
    print("\nSCHEDULE / CHAMPIONSHIP / RANKINGS CHECKS PASSED")


if __name__ == "__main__":
    main()
