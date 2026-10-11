"""
End-to-end verification of the NHL weekly salary-cap redraft backend (FS-033): explicit league mode, fixed cap,
contests and rotating private pairings, published salaries, roster validation (cap, duplicates, positions, pricing),
draft -> submit -> immutable, lock deadline and late edits, opponent privacy, authorization, replay safety, and that
no wagering vocabulary exists in the new code. Throwaway SQLite; real HTTP API; injected clock.

Run from the repository root with:

    .venv-api/Scripts/python.exe scripts/verify_weekly_redraft.py
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fs033-wr-", dir=ROOT, ignore_cleanup_errors=True) as temp_dir:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir) / 'wr').as_posix()}"

        from fastapi.testclient import TestClient
        from sqlalchemy.exc import IntegrityError

        from fantasy_sports.api.auth import create_session
        from fantasy_sports.api.main import app
        from fantasy_sports.platform import series_schedule
        from fantasy_sports.platform import weekly_redraft as wr
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            DraftableAsset, League, LeagueUniverse, Player, Team, Universe, User, WeeklyContestEntry, WeeklyContestSlot,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine

        Base.metadata.create_all(bind=engine)
        client = TestClient(app)

        with SessionLocal() as db:
            comm = User(email="comm@example.com", display_name="Comm")
            o1, o2 = User(email="o1@example.com", display_name="O1"), User(email="o2@example.com", display_name="O2")
            db.add_all([comm, o1, o2])
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            cfb = Universe(sport="football", level="college", code="cfb", display_name="CFB", supports_individual_defense=True)
            db.add_all([nhl, cfb])
            db.flush()
            positions = ["C"] * 4 + ["LW"] * 3 + ["RW"] * 3 + ["D"] * 5 + ["G"] * 3
            assets: dict[str, list[int]] = {"C": [], "W": [], "D": [], "G": []}
            for i, pos in enumerate(positions):
                p = Player(universe_id=nhl.id, full_name=f"Skater {i:02d} {pos}", position=pos)
                db.add(p)
                db.flush()
                a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
                db.add(a)
                db.flush()
                assets[{"LW": "W", "RW": "W"}.get(pos, pos)].append(a.id)
            retired = Player(universe_id=nhl.id, full_name="Retired Guy", position="C", active=False)
            db.add(retired)
            db.flush()
            retired_asset = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=retired.id)
            db.add(retired_asset)
            db.flush()

            def make_league(name, universe, size=4, mode_len=7):
                lg = League(name=name, commissioner_user_id=comm.id, size=size, series_length_days=mode_len if universe is nhl else None)
                db.add(lg)
                db.flush()
                db.add(LeagueUniverse(league_id=lg.id, universe_id=universe.id))
                return lg

            lg = make_league("Redraft", nhl)
            odd = make_league("Odd", nhl, 3)
            fb = make_league("Football", cfb, 2)
            sched = make_league("HasSeries", nhl)
            mk = lambda lgx, n, **kw: Team(league_id=lgx.id, name=n, **kw)  # noqa: E731
            teams = [mk(lg, "R1", owner_user_id=o1.id, is_ai_owned=False), mk(lg, "R2", owner_user_id=o2.id, is_ai_owned=False),
                     mk(lg, "R3", is_ai_owned=True), mk(lg, "R4", is_ai_owned=True)]
            db.add_all(teams + [mk(odd, f"O{i}", is_ai_owned=True) for i in range(3)] + [mk(fb, f"F{i}", is_ai_owned=True) for i in range(2)]
                       + [mk(sched, f"S{i}", is_ai_owned=True) for i in range(4)])
            db.flush()
            series_schedule.generate_schedule(db, sched, dt.date(2026, 11, 1))
            db.commit()
            ids = dict(lg=lg.id, odd=odd.id, fb=fb.id, sched=sched.id, t=[t.id for t in teams], retired=retired_asset.id)
            tok = {n: create_session(db, u.id).token for n, u in dict(comm=comm, o1=o1, o2=o2).items()}
        auth = {n: {"Authorization": f"Bearer {t}"} for n, t in tok.items()}

        clock = {"now": dt.datetime(2026, 11, 2, 12, 0)}
        wr.utcnow = lambda: clock["now"]
        L = ids["lg"]
        Q = f"/leagues/{L}/weekly-redraft"

        # ---- mode ---------------------------------------------------------------------------------------------------------
        check(client.post(Q, json={}).status_code == 401 and client.post(Q, json={}, headers=auth["o1"]).status_code == 403, "enabling is commissioner-only")
        check(client.post(f"/leagues/{ids['fb']}/weekly-redraft", json={}, headers=auth["comm"]).json()["detail"]["code"] == "NOT_NHL", "a football league cannot enable it")
        r = client.post(f"/leagues/{ids['sched']}/weekly-redraft", json={}, headers=auth["comm"])
        check(r.status_code == 409 and r.json()["detail"]["code"] == "HAS_SERIES", "a league with a series schedule cannot switch (separate modes)")
        check(client.post(Q, json={"base_cap": 0}, headers=auth["comm"]).status_code == 400, "a zero cap is refused")
        check(client.get(Q).json()["league_mode"] == "standard" and client.get(Q).json()["base_cap"] is None, "a league starts in standard mode")
        r = client.post(Q, json={}, headers=auth["comm"]).json()
        check(r["league_mode"] == "weekly_redraft" and r["base_cap"] == wr.NEUTRAL_FIXED_CAP == 50000 and r["cap_policy"] == "FIXED"
              and "open product decision" in r["note"], "enabling uses the neutral fixed cap and says the adjustment formula is undecided")
        check(client.post(Q, json={"base_cap": 30000}, headers=auth["comm"]).json()["base_cap"] == 30000, "the cap can be set before any contest exists")
        r = client.post(f"/leagues/{L}/series-schedule", json={"start_date": "2026-11-01"}, headers=auth["comm"])
        check(r.status_code == 400 and "weekly redraft" in r.json()["detail"], "a weekly-redraft league cannot also generate a series schedule")

        # ---- contests and pairings --------------------------------------------------------------------------------------------
        body = lambda d, lock: {"start_date": f"2026-11-{d:02d}", "end_date": f"2026-11-{d + 6:02d}", "lock_at": lock}  # noqa: E731
        C = f"{Q}/contests"
        check(client.post(C, json=body(3, "2026-11-03T00:00:00"), headers=auth["o1"]).status_code == 403, "creating a contest is commissioner-only")
        check(client.post(f"/leagues/{ids['odd']}/weekly-redraft/contests", json=body(3, "2026-11-03T00:00:00"), headers=auth["comm"]).status_code == 400, "a standard league has no contests")
        client.post(f"/leagues/{ids['odd']}/weekly-redraft", json={}, headers=auth["comm"])
        r = client.post(f"/leagues/{ids['odd']}/weekly-redraft/contests", json=body(3, "2026-11-03T00:00:00"), headers=auth["comm"])
        check(r.status_code == 400 and r.json()["detail"]["code"] == "BAD_TEAM_COUNT", "an odd number of teams cannot be paired")
        r = client.post(C, json=body(3, "2026-11-12T00:00:00"), headers=auth["comm"])
        check(r.status_code == 400 and r.json()["detail"]["code"] == "BAD_LOCK", "rosters must lock before the contest ends")
        r = client.post(C, json={"start_date": "2026-11-09", "end_date": "2026-11-03", "lock_at": "2026-11-03T00:00:00"}, headers=auth["comm"])
        check(r.status_code == 400 and r.json()["detail"]["code"] == "BAD_DATES", "a contest cannot end before it starts")
        contests = []
        for week, day in enumerate((3, 10, 17, 24), start=1):
            r = client.post(C, json=body(day, f"2026-11-{day:02d}T18:00:00"), headers=auth["comm"])
            check(r.status_code == 200 and r.json()["week_index"] == week and r.json()["cap"] == 30000 and r.json()["status"] == "OPEN", f"week {week} created with the cap snapshot (30000)")
            contests.append(r.json())
        check(client.post(C, json=body(3, "2026-11-03T18:00:00"), headers=auth["comm"]).status_code == 409, "a duplicate start date is refused (replay safe)")
        pairs = [frozenset(frozenset((p["team_a_id"], p["team_b_id"])) for p in c["pairings"]) for c in contests]
        t = ids["t"]
        for c in contests:
            seen = [x for p in c["pairings"] for x in (p["team_a_id"], p["team_b_id"])]
            check(sorted(seen) == sorted(t) and len(c["pairings"]) == 2, f"week {c['week_index']}: every team has exactly one opponent")
        check(len({pairs[0], pairs[1], pairs[2]}) == 3 and pairs[3] == pairs[0], "opponents rotate over 3 weeks and the cycle repeats in week 4")
        check(client.post(Q, json={"base_cap": 40000}, headers=auth["comm"]).json()["detail"]["code"] == "CAP_LOCKED", "the cap cannot change once contests exist")
        cid = contests[0]["id"]

        # ---- salaries ------------------------------------------------------------------------------------------------------------
        S = f"/weekly-redraft/contests/{cid}/salaries"
        pool = assets["C"] + assets["W"] + assets["D"] + assets["G"]
        prices = {a: 4000 + 500 * (i % 5) for i, a in enumerate(pool)}   # 4000..6000
        check(client.post(S, json={"salaries": {str(a): p for a, p in prices.items()}}, headers=auth["o1"]).status_code == 403, "publishing salaries is commissioner-only")
        r = client.post(S, json={"salaries": {str(ids["retired"]): 5000}}, headers=auth["comm"])
        check(r.status_code == 400 and r.json()["detail"]["code"] == "NOT_IN_POOL", "an inactive player is not in the pool")
        for bad, label in [({str(pool[0]): 0}, "zero"), ({str(pool[0]): -5}, "negative"), ({str(pool[0]): 30001}, "above the cap")]:
            r = client.post(S, json={"salaries": bad}, headers=auth["comm"])
            check(r.status_code == 400 and r.json()["detail"]["code"] == "BAD_SALARY", f"a {label} salary is refused")
        check(client.post(S, json={"salaries": {}}, headers=auth["comm"]).json()["detail"]["code"] == "EMPTY", "an empty price list is refused")
        R1 = f"/weekly-redraft/contests/{cid}/teams/{t[0]}/roster"
        r = client.put(R1, json={"slots": {"C": [assets["C"][0]]}}, headers=auth["o1"])
        check(r.status_code == 400 and r.json()["detail"]["code"] == "NO_SALARIES", "rosters cannot be built before salaries are published")
        r = client.post(S, json={"salaries": {str(a): p for a, p in prices.items() if a != assets["W"][5]}}, headers=auth["comm"])
        check(r.status_code == 200 and r.json()["salaries_published"] == len(pool) - 1, "salaries published for all but one player")
        check(client.post(S, json={"salaries": {str(a): p for a, p in prices.items() if a != assets["W"][5]}}, headers=auth["comm"]).status_code == 200,
              "publishing the same price list again is fine before any roster exists")

        # ---- roster validation ------------------------------------------------------------------------------------------------------
        c0, w = assets["C"], assets["W"]
        d, g = assets["D"], assets["G"]
        good = {"C": [c0[0]], "W": [w[0], w[1]], "D": [d[0], d[1]], "G": [g[0]]}
        total = sum(prices[a] for ids_ in good.values() for a in ids_)

        def put(slots, who="o1", team=None, contest=cid):
            tm = team or t[0]
            return client.put(f"/weekly-redraft/contests/{contest}/teams/{tm}/roster", json={"slots": slots}, headers=auth[who])

        def code(r):
            return r.json()["detail"]["code"] if r.status_code != 200 else "OK"
        check(code(put({"X": [1]})) == "BAD_SLOT", "an unknown slot is rejected")
        check(code(put({"W": [w[0], w[1], w[2]]})) == "TOO_MANY", "three wingers are rejected")
        check(code(put({"C": [c0[0]], "W": [c0[0]]})) in ("DUPLICATE_PLAYER", "INVALID_POSITION"), "the same player twice is rejected")
        check(code(put({"W": [w[0], w[0]]})) == "DUPLICATE_PLAYER", "a duplicate player in one position group is rejected")
        check(code(put({"C": [d[0]]})) == "INVALID_POSITION", "a defenseman cannot fill C")
        check(code(put({"G": [c0[0]]})) == "INVALID_POSITION", "a center cannot fill G")
        check(code(put({"W": [w[5]]})) == "UNPRICED_PLAYER", "a player with no salary cannot be rostered")
        check(code(put({"C": [ids["retired"]]})) == "NOT_IN_POOL", "a player outside the pool is rejected")
        check(put(good, who="o2").status_code == 403, "another owner cannot edit this roster")
        check(client.put(R1, json={"slots": good}).status_code == 401, "editing needs sign-in")
        check(client.put(f"/weekly-redraft/contests/{cid}/teams/{t[2]}/roster", json={"slots": good}, headers=auth["comm"]).status_code == 403, "nobody can edit an AI team's roster over HTTP")
        check(client.put(f"/weekly-redraft/contests/{cid}/teams/{ids['t'][0]}/roster", json={"slots": good}, headers=auth["o2"]).status_code == 403, "ownership is enforced per team")

        # Cap boundary: build a roster whose total is exactly the cap, then one unit over.
        with SessionLocal() as db:
            from fantasy_sports.platform.db.models.core import WeeklyPlayerSalary
            for row in db.query(WeeklyPlayerSalary).filter_by(contest_id=cid):
                if row.draftable_asset_id in (c0[1], w[3], w[4], d[2], d[3], g[1]):
                    row.salary = 5000     # six players x 5000 = exactly the 30000 cap
                if row.draftable_asset_id == g[2]:
                    row.salary = 5001
            db.commit()
        at_cap = {"C": [c0[1]], "W": [w[3], w[4]], "D": [d[2], d[3]], "G": [g[1]]}
        r = put(at_cap)
        check(r.status_code == 200 and r.json()["salary_total"] == 30000 and r.json()["cap_remaining"] == 0, "a roster exactly at the cap is accepted (cap_remaining 0)")
        over = {**at_cap, "G": [g[2]]}
        r = put(over)
        check(r.status_code == 400 and r.json()["detail"]["code"] == "CAP_EXCEEDED" and "30001" in r.json()["detail"]["message"], "one unit over the cap is rejected (30001 > 30000)")
        r = put(good)
        check(r.status_code == 200 and r.json()["status"] == "DRAFT" and r.json()["salary_total"] == total and r.json()["cap"] == 30000
              and r.json()["cap_remaining"] == 30000 - total and len(r.json()["slots"]) == 6, f"a valid roster saves as a DRAFT with exact cap math ({total} of 30000)")
        first = r.json()
        check(put(good).json() == first, "saving the same roster again leaves identical data (replay safe)")
        with SessionLocal() as db:
            check(db.query(WeeklyContestSlot).count() == 6 and db.query(WeeklyContestEntry).count() == 1, "replacing a draft leaves no duplicate rows")
        partial = put({"C": [c0[0]]})
        check(partial.status_code == 200 and len(partial.json()["slots"]) == 1, "a partial roster can be saved as a draft")
        r = client.post(f"/weekly-redraft/contests/{cid}/teams/{t[0]}/submit", headers=auth["o1"])
        check(r.status_code == 400 and r.json()["detail"]["code"] == "INCOMPLETE", "submitting an incomplete roster is rejected")
        check(client.post(f"/weekly-redraft/contests/{cid}/teams/{t[1]}/submit", headers=auth["o2"]).json()["detail"]["code"] == "NO_ROSTER", "submitting without saving is rejected")
        put(good)
        r = client.post(f"/weekly-redraft/contests/{cid}/teams/{t[0]}/submit", headers=auth["o1"])
        check(r.status_code == 200 and r.json()["status"] == "SUBMITTED" and r.json()["submitted_at"] is not None, "a full roster submits")
        sub = r.json()
        clock["now"] += dt.timedelta(minutes=5)
        check(client.post(f"/weekly-redraft/contests/{cid}/teams/{t[0]}/submit", headers=auth["o1"]).json() == sub, "submitting again is idempotent (same submitted_at)")
        r = put({"C": [c0[2]]})
        check(r.status_code == 409 and r.json()["detail"]["code"] == "ALREADY_SUBMITTED", "a submitted roster cannot be edited")
        check(client.post(S, json={"salaries": {str(pool[0]): 4000}}, headers=auth["comm"]).json()["detail"]["code"] == "HAS_ENTRIES", "prices cannot change once a roster exists")

        # ---- privacy ----------------------------------------------------------------------------------------------------------------
        opp = [p for p in contests[0]["pairings"] if t[0] in (p["team_a_id"], p["team_b_id"])][0]
        opp_id = opp["team_b_id"] if opp["team_a_id"] == t[0] else opp["team_a_id"]
        v = client.get(f"/weekly-redraft/contests/{cid}?team_id={t[0]}", headers=auth["o1"]).json()
        check(v["opponent_team_id"] == opp_id and v["my_entry"]["status"] == "SUBMITTED" and v["opponent_entry"] is None, "before the lock the opponent's roster is hidden")
        check(client.get(f"/weekly-redraft/contests/{cid}?team_id={t[0]}", headers=auth["o2"]).status_code == 403, "you can only view your own team")
        check(all(p["team_a_status"] in ("NO_ENTRY", "DRAFT", "SUBMITTED") for p in v["contest"]["pairings"]), "the contest shows who has submitted, not what")
        # Opponent (if human) submits too; an unsubmitted draft must never be shown.
        opp_owner = {t[1]: "o2"}.get(opp_id)
        if opp_owner:
            put(good, who=opp_owner, team=opp_id)
            check(client.get(f"/weekly-redraft/contests/{cid}?team_id={t[0]}", headers=auth["o1"]).json()["opponent_entry"] is None, "an opponent's saved draft is never visible")

        # ---- lock and late edits -------------------------------------------------------------------------------------------------------
        clock["now"] = dt.datetime(2026, 11, 3, 17, 59, 59)
        check(put(good, who="o2", team=t[1]).status_code in (200, 400), "one second before the lock edits are still evaluated normally")
        if opp_owner:
            client.post(f"/weekly-redraft/contests/{cid}/teams/{opp_id}/submit", headers=auth[opp_owner])
        clock["now"] = dt.datetime(2026, 11, 3, 18, 0, 0)
        r = put(good, who="o2", team=t[1])
        check(r.status_code == 409 and r.json()["detail"]["code"] == "LOCKED", "at the lock time a roster edit is rejected as late")
        r = client.post(f"/weekly-redraft/contests/{cid}/teams/{t[1]}/submit", headers=auth["o2"])
        check(r.status_code in (409, 200), "a submit at the lock is either idempotent (already final) or rejected")
        check(client.get(Q).json()["contests"][0]["status"] == "LOCKED" and client.get(Q).json()["contests"][1]["status"] == "OPEN", "only the locked contest reports LOCKED")
        check(client.post(S, json={"salaries": {str(pool[0]): 4000}}, headers=auth["comm"]).json()["detail"]["code"] == "LOCKED", "salaries cannot change after the lock")
        v = client.get(f"/weekly-redraft/contests/{cid}?team_id={t[0]}", headers=auth["o1"]).json()
        if opp_owner:
            check(v["opponent_entry"] is not None and v["opponent_entry"]["status"] == "SUBMITTED", "after the lock the opponent's submitted roster is revealed")
        else:
            check(v["opponent_entry"] is None, "an opponent with no submitted roster shows nothing after the lock")
        # A draft that was never submitted does not count, and cannot be rescued after the lock.
        cid2 = contests[1]["id"]
        client.post(f"/weekly-redraft/contests/{cid2}/salaries", json={"salaries": {str(a): p for a, p in prices.items()}}, headers=auth["comm"])
        clock["now"] = dt.datetime(2026, 11, 10, 9, 0)
        check(put(good, contest=cid2).status_code == 200, "week 2: a draft saves")
        clock["now"] = dt.datetime(2026, 11, 10, 18, 5)
        r = client.post(f"/weekly-redraft/contests/{cid2}/teams/{t[0]}/submit", headers=auth["o1"])
        check(r.status_code == 409 and r.json()["detail"]["code"] == "LOCKED", "a draft left unsubmitted at the lock cannot be submitted late")
        pr = client.get(f"/weekly-redraft/contests/{cid2}?team_id={t[0]}", headers=auth["o1"]).json()
        check(pr["my_entry"]["status"] == "DRAFT" and pr["contest"]["status"] == "LOCKED", "it stays a DRAFT, which the lock leaves uncounted")

        # ---- database guarantees, restart ----------------------------------------------------------------------------------------------------
        with SessionLocal() as db:
            e = db.query(WeeklyContestEntry).first()
            e.salary_total = e.cap + 1
            try:
                db.flush()
                raise AssertionError("an over-cap entry was stored")
            except IntegrityError:
                check(True, "the database refuses an entry over its cap")
            db.rollback()
            slot = db.query(WeeklyContestSlot).first()
            db.add(WeeklyContestSlot(entry_id=slot.entry_id, slot_code="C", slot_index=2, draftable_asset_id=slot.draftable_asset_id, salary=1))
            try:
                db.flush()
                raise AssertionError("a duplicate player was stored")
            except IntegrityError:
                check(True, "the database refuses the same player twice on one roster")
            db.rollback()
        before = client.get(f"/weekly-redraft/contests/{cid}?team_id={t[0]}", headers=auth["o1"]).json()
        engine.dispose()
        check(TestClient(app).get(f"/weekly-redraft/contests/{cid}?team_id={t[0]}", headers=auth["o1"]).json() == before, "a new process reads identical contest state")
        engine.dispose()

        # ---- no wagering ---------------------------------------------------------------------------------------------------------------------
        src = "\n".join((ROOT / f).read_text(encoding="utf-8") for f in (
            "src/fantasy_sports/platform/weekly_redraft.py", "src/fantasy_sports/api/routers/weekly_redraft.py"))
        check(not re.search(r"\b(wager|bet|bets|betting|odds|prize|prizes|payout|payouts|sportsbook|stake|entry[_ ]fee|buy[_-]?in|dollars|cash)\b", src, re.I),
              "the weekly redraft code contains no wagering vocabulary")
        check(not re.search(r"(?m)^\s*(import|from)\s+(requests|httpx|urllib|stripe|socket)", src), "no payment or network library is imported")

    print("\nAll weekly redraft checks passed.")


if __name__ == "__main__":
    main()
