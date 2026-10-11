"""
Offline verification for the slow / asynchronous snake draft clock (FS-030).

Part A checks the pure clock arithmetic (overnight windows, wrap past midnight, offsets, boundaries, round trips).
Part B drives real drafts through the HTTP API (FastAPI TestClient, throwaway SQLite) with an injected clock:
live and hours-per-pick drafts, pause / resume, overnight freezing, overdue autopick, restart safety, double
processing, authorization, and the original untimed draft. No scheduler, no external calls.

Run from the repository root with:

    .venv-api/Scripts/python.exe scripts/verify_async_snake_draft.py
"""

from __future__ import annotations

import datetime as dt
import os
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def at(day: int, hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime(2026, 10, day, hour, minute, second)


def part_a() -> None:
    from fantasy_sports.platform import draft_clock as dc

    night = dc.make_window(22 * 60, 6 * 60, 0)   # 22:00 - 06:00 UTC
    add = lambda start, seconds, w=night: dc.add_active_seconds(start, seconds, w)  # noqa: E731
    H = 3600

    check(add(at(10, 12), H) == at(10, 13), "daytime allowance: 12:00 + 1h = 13:00")
    check(add(at(10, 21, 30), H) == at(11, 6, 30), "starts 21:30, 1h allowance: 30 min before the window, 30 min after (06:30)")
    check(add(at(10, 23), 2 * H) == at(11, 8), "starts inside the window (23:00), 2h: waits until 06:00, ends 08:00")
    check(add(at(10, 22), H) == at(11, 7), "starts exactly when the window opens (22:00): the window does not count")
    check(add(at(11, 6), H) == at(11, 7), "starts exactly when the window closes (06:00): counts immediately")
    check(add(at(10, 20), 2 * H) == at(10, 22), "allowance ends exactly at 22:00: the deadline is the window start")
    check(add(at(10, 20), 10 * H) == at(11, 14), "10h from 20:00 spans one night: 2h + 8h after 06:00 = 14:00")
    check(add(at(10, 20), 40 * H) == at(13, 12), "40h from 20:00 spans three nights (2h + 16h + 16h + 6h of active time)")
    check(add(at(10, 12), 0) == at(10, 12) and add(at(10, 23), 0) == at(11, 6), "a zero allowance is the next active instant")
    check(add(at(10, 12), 90, None) == at(10, 12, 1, 30), "no window: plain addition")

    # Offset: 22:00-06:00 at UTC-5 is 03:00-11:00 UTC.
    est = dc.make_window(22 * 60, 6 * 60, -300)
    check(est.contains(at(10, 3)) and est.contains(at(10, 10, 59)) and not est.contains(at(10, 11)) and not est.contains(at(10, 2, 59)),
          "a UTC-5 window covers 03:00-11:00 UTC")
    check(add(at(10, 2), 2 * H, est) == at(10, 12), "UTC-5: 02:00 UTC + 2h allowance = 1h before the window, 1h after it (12:00)")
    day_window = dc.make_window(12 * 60, 13 * 60, 60)      # 12:00-13:00 at UTC+1 = 11:00-12:00 UTC (no wrap)
    check(day_window.contains(at(10, 11, 30)) and not day_window.contains(at(10, 12)) and add(at(10, 10, 30), H, day_window) == at(10, 12, 30),
          "a non-wrapping window (lunch) freezes time too")

    # Round trips, seeded.
    rng = random.Random(30)
    ok = True
    for _ in range(3000):
        start_m, end_m = rng.randrange(1440), rng.randrange(1440)
        if start_m == end_m:
            continue
        w = dc.make_window(start_m, end_m, rng.choice([-480, -300, 0, 60, 330]))
        a = at(10, 0) + dt.timedelta(seconds=rng.randrange(0, 5 * 86400))
        s = rng.randrange(0, 4 * 86400)
        b = dc.add_active_seconds(a, s, w)
        if not w.contains(b):   # a deadline inside the window only happens at its opening edge; skip those
            ok &= abs(dc.active_seconds_between(a, b, w) - s) < 1e-6
            ok &= dc.add_active_seconds(a, dc.active_seconds_between(a, b, w), w) == b
        ok &= dc.active_seconds_between(a, a, w) == 0 and dc.active_seconds_between(b, a, w) == 0
    check(ok, "3000 random windows: add_active_seconds and active_seconds_between are inverses")
    check(dc.active_seconds_between(at(10, 21), at(11, 7), night) == 2 * H, "21:00 -> 07:00 holds exactly 2 active hours (1h + 1h)")
    check(dc.active_seconds_between(at(10, 23), at(11, 5), night) == 0, "a span entirely inside the window holds no active time")
    check(dc.active_seconds_between(at(10, 8), at(12, 8), night) == 32 * H, "two full days hold 32 active hours")

    # Validation.
    def bad(fn, fragment, label):
        try:
            fn()
        except dc.ClockError as e:
            check(fragment in str(e), f"{label} rejected ({e})")
            return
        raise AssertionError(f"{label} accepted")
    from fantasy_sports.platform.db.models.core import Draft
    d = Draft(league_id=1)
    bad(lambda: dc.configure(d, 5), "between", "5-second picks")
    bad(lambda: dc.configure(d, 8 * 86400), "between", "8-day picks")
    bad(lambda: dc.configure(d, 60, 600, None), "both a start and an end", "half an overnight window")
    bad(lambda: dc.configure(d, 60, 600, 600), "different times", "empty overnight window")
    bad(lambda: dc.configure(d, 60, 600, 700, 900), "UTC offset", "offset beyond +/-14h")
    bad(lambda: dc.configure(d, None, 600, 700), "needs a pick time", "overnight window without a pick time")
    bad(lambda: dc.parse_hhmm("25:00"), "22:00", "hour 25")
    bad(lambda: dc.parse_hhmm("noon"), "22:00", "non-numeric time")
    check(dc.parse_hhmm("22:00") == 1320 and dc.parse_hhmm("") is None and dc.hhmm(1320) == "22:00", "HH:MM round trip")
    dc.configure(d, 7200, 1320, 360, 0)
    fast = Draft(league_id=1)
    fast.pick_seconds = 30
    check(dc.mode(d) == "async" and dc.mode(Draft(league_id=1)) == "untimed" and dc.mode(fast) == "live",
          "mode: untimed / live (< 1h) / async (>= 1h)")


def main() -> None:
    part_a()
    with tempfile.TemporaryDirectory(prefix="fs030-draft-", dir=ROOT, ignore_cleanup_errors=True) as temp_dir:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir) / 'draft').as_posix()}"

        from fastapi.testclient import TestClient

        from fantasy_sports.api.auth import create_session
        from fantasy_sports.api.main import app
        from fantasy_sports.api.routers import draft as draft_router
        from fantasy_sports.platform import draft_clock as dc
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            Draft, DraftableAsset, DraftPick, League, LeagueUniverse, Player, RosterEntry, Team, Universe, User,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine

        Base.metadata.create_all(bind=engine)
        client = TestClient(app)

        with SessionLocal() as db:
            commissioner = User(email="comm@example.com", display_name="Commissioner")
            owners = [User(email=f"o{i}@example.com", display_name=f"Owner{i}") for i in range(2)]
            other_comm = User(email="other@example.com", display_name="Other")
            db.add_all([commissioner, other_comm, *owners])
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            db.add(nhl)
            db.flush()
            for i in range(60):
                p = Player(universe_id=nhl.id, full_name=f"Player {i:02d}", position="C")
                db.add(p)
                db.flush()
                db.add(DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id))
            leagues = {}
            for name in ("legacy", "live", "async", "twin_a", "twin_b", "pause", "night", "boot"):
                lg = League(name=name, commissioner_user_id=commissioner.id, size=4, draft_rounds=3, series_length_days=None)
                db.add(lg)
                db.flush()
                db.add(LeagueUniverse(league_id=lg.id, universe_id=nhl.id))
                teams = [Team(league_id=lg.id, name=f"{name}-A", owner_user_id=owners[0].id, is_ai_owned=False),
                         Team(league_id=lg.id, name=f"{name}-B", owner_user_id=owners[1].id, is_ai_owned=False),
                         Team(league_id=lg.id, name=f"{name}-C", is_ai_owned=True), Team(league_id=lg.id, name=f"{name}-D", is_ai_owned=True)]
                db.add_all(teams)
                db.flush()
                leagues[name] = {"id": lg.id, "teams": [t.id for t in teams]}
            db.commit()
            tok = {n: create_session(db, u.id).token for n, u in dict(commissioner=commissioner, other=other_comm, a=owners[0], b=owners[1]).items()}
            assets = [a.id for a in db.query(DraftableAsset).order_by(DraftableAsset.id)]
        auth = {n: {"Authorization": f"Bearer {t}"} for n, t in tok.items()}

        clock = {"now": at(10, 12)}
        draft_router.utcnow = lambda: clock["now"]

        def set_now(value):
            clock["now"] = value

        def start(league: str, body=None, who="commissioner"):
            return client.post("/drafts/start", json={"league_id": leagues[league]["id"], **(body or {})}, headers=auth[who] if who else {})

        def clk(draft_id):
            return client.get(f"/drafts/{draft_id}/clock").json()

        def picks(draft_id):
            return client.get(f"/drafts/{draft_id}/picks").json()

        # ---- the original untimed draft is unchanged ----------------------------------------------------------------
        r = start("legacy", who=None)
        check(r.status_code == 200 and r.json()["pick_seconds"] is None and r.json()["paused"] is False and r.json()["deadline_at"] is None,
              "an untimed draft starts without sign-in, exactly as before")
        legacy = r.json()["id"]
        c = clk(legacy)
        check(c["clock_status"] == "untimed" and c["mode"] == "untimed" and c["remaining_seconds"] is None and c["deadline_at"] is None,
              "the clock endpoint reports untimed with no deadline")
        set_now(at(15, 3))   # days later: nothing happens to an untimed draft
        check(client.post(f"/drafts/{legacy}/process-overdue").json()["processed"] == 0, "an untimed draft is never auto-picked")
        r = client.post(f"/drafts/{legacy}/pick", json={"draftable_asset_id": assets[7]}, headers=auth["a"])
        check(r.status_code == 200 and r.json()["is_autopick"] is False, "a manual pick days later still works")
        r = client.post(f"/drafts/{legacy}/autopick")
        check(r.status_code == 200 and r.json()["team_id"] == leagues["legacy"]["teams"][1], "legacy autopick works for any team, no sign-in")
        check(client.post(f"/drafts/{legacy}/pause", headers=auth["commissioner"]).status_code == 409, "an untimed draft cannot be paused")
        for _ in range(10):
            client.post(f"/drafts/{legacy}/autopick")
        check(client.get(f"/drafts/{legacy}").json()["status"] == "complete", "the untimed draft runs to completion")
        set_now(at(10, 12))

        # ---- configuration + authorization -------------------------------------------------------------------------
        check(start("live", {"pick_seconds": 60}, who=None).status_code == 401, "a timed start needs a signed-in user")
        check(start("live", {"pick_seconds": 60}, who="a").status_code == 403, "a timed start is commissioner-only")
        check(start("live", {"pick_seconds": 60}, who="other").status_code == 403, "another league's commissioner cannot start it")
        for body, fragment in [({"pick_seconds": 3}, "between"), ({"pick_seconds": 60, "overnight_start": "22:00"}, "both a start"),
                               ({"pick_seconds": 60, "overnight_start": "22:00", "overnight_end": "22:00"}, "different times"),
                               ({"pick_seconds": 60, "overnight_start": "99:00", "overnight_end": "06:00"}, "22:00"),
                               ({"overnight_start": "22:00", "overnight_end": "06:00"}, "pick time")]:
            r = start("live", body)
            check(r.status_code == 400 and fragment in r.json()["detail"], f"bad clock config rejected: {body}")
        check(client.get(f"/drafts/league/{leagues['live']['id']}").status_code == 404, "failed starts leave no draft behind")

        # ---- live mode: seconds per pick ----------------------------------------------------------------------------
        r = start("live", {"pick_seconds": 60})
        live = r.json()["id"]
        check(r.status_code == 200 and r.json()["pick_seconds"] == 60 and r.json()["deadline_at"].startswith("2026-10-10T12:01:00"),
              "a 60-second draft starts with deadline = start + 60s")
        teams = leagues["live"]["teams"]
        c = clk(live)
        check(c["mode"] == "live" and c["clock_status"] == "running" and c["remaining_seconds"] == 60 and c["on_clock_team_id"] == teams[0]
              and c["pick_number"] == 1, "live clock: running, 60s left, team A on the clock")
        set_now(at(10, 12, 0, 20))
        check(clk(live)["remaining_seconds"] == 40, "20 seconds later 40 remain")
        r = client.post(f"/drafts/{live}/pick", json={"draftable_asset_id": assets[3]}, headers=auth["b"])
        check(r.status_code == 403, "the wrong team cannot pick")
        r = client.post(f"/drafts/{live}/pick", json={"draftable_asset_id": assets[3]}, headers=auth["a"])
        check(r.status_code == 200 and clk(live)["deadline_at"].startswith("2026-10-10T12:01:20") and clk(live)["on_clock_team_id"] == teams[1],
              "a manual pick starts the next pick's clock from the moment it was made (12:00:20 + 60s)")
        check(client.post(f"/drafts/{live}/autopick").status_code == 409 and client.post(f"/drafts/{live}/autopick").json()["detail"].startswith("That team still"),
              "a human team with time left cannot be auto-picked")

        # Late manual pick: the autopick happens first and the late request is told so.
        set_now(at(10, 12, 3, 0))     # B's pick (due 12:01:20) and C's (due 12:02:20) are overdue; D's is due 12:03:20
        r = client.post(f"/drafts/{live}/pick", json={"draftable_asset_id": assets[9]}, headers=auth["b"])
        check(r.status_code == 409 and "ran out" in r.json()["detail"], "a late manual pick is refused: the clock already made the pick")
        ps = [p for p in picks(live) if p["draftable_asset_id"]]
        check([p["pick_number"] for p in ps] == [1, 2, 3] and all(p["is_autopick"] for p in ps[1:]),
              "both overdue picks were made in order (picks 2 and 3), and nothing further")
        stamp = {p["pick_number"]: p["picked_at"] for p in ps}
        check(stamp[2].startswith("2026-10-10T12:01:20") and stamp[3].startswith("2026-10-10T12:02:20"),
              "autopicks are stamped with their own deadlines (12:01:20, 12:02:20), not the time they were processed")
        check(ps[1]["display_name"] == "Player 00", "the autopick is the first asset on the board (deterministic)")
        c = clk(live)
        check(c["on_clock_team_id"] == teams[3] and c["remaining_seconds"] == 20 and c["deadline_at"].startswith("2026-10-10T12:03:20"),
              "team D is on the clock with 20s left (its clock started at C's missed deadline, not at 12:03:00)")
        before = picks(live)
        again = client.post(f"/drafts/{live}/process-overdue").json()
        check(again["processed"] == 0 and picks(live) == before, "processing again changes nothing (double-processing safe)")

        # ---- determinism: lazily applied late vs promptly applied ---------------------------------------------------
        def run_twin(name: str, poll_every: int | None):
            d = start(name, {"pick_seconds": 45}).json()["id"]
            t = at(10, 12)
            end = t + dt.timedelta(minutes=30)
            if poll_every is None:
                set_now(end)
                client.post(f"/drafts/{d}/process-overdue")
            else:
                while t < end:
                    t += dt.timedelta(seconds=poll_every)
                    set_now(min(t, end))
                    client.get(f"/drafts/{d}/clock")
            return [(p["pick_number"], p["team_id"] - leagues[name]["teams"][0], p["display_name"], p["picked_at"], p["is_autopick"])
                    for p in picks(d)]
        set_now(at(10, 12))
        prompt = run_twin("twin_a", 7)
        set_now(at(10, 12))
        late = run_twin("twin_b", None)
        check(prompt == late and len(late) == 12 and all(x[4] for x in late),
              "the same draft, processed every 7s or once after 30 minutes, ends with identical picks and timestamps")

        # ---- pause / resume --------------------------------------------------------------------------------------------
        set_now(at(11, 9))
        r = start("pause", {"pick_seconds": 300})
        pdraft = r.json()["id"]
        set_now(at(11, 9, 1, 40))      # 100s used, 200 left
        url = f"/drafts/{pdraft}"
        check(client.post(f"{url}/pause").status_code == 401, "pausing requires sign-in")
        check(client.post(f"{url}/pause", headers=auth["a"]).status_code == 403, "an owner cannot pause")
        check(client.post(f"{url}/pause", headers=auth["other"]).status_code == 403, "another league's commissioner cannot pause")
        r = client.post(f"{url}/pause", headers=auth["commissioner"]).json()
        check(r["clock_status"] == "paused" and r["paused"] and r["remaining_seconds"] == 200 and r["deadline_at"] is None,
              "pausing freezes the remaining 200s and removes the deadline")
        set_now(at(11, 21))
        check(clk(pdraft)["remaining_seconds"] == 200 and client.post(f"{url}/process-overdue").json()["processed"] == 0,
              "twelve hours later the paused clock still shows 200s and nothing was auto-picked")
        r = client.post(f"{url}/pause", headers=auth["commissioner"]).json()
        check(r["remaining_seconds"] == 200 and r["paused"], "pausing again is a no-op (idempotent)")
        r = client.post(f"{url}/pick", json={"draftable_asset_id": assets[4]}, headers=auth["a"])
        check(r.status_code == 409 and "paused" in r.json()["detail"], "picks are refused while paused")
        r = client.post(f"{url}/autopick")
        check(r.status_code == 409 and "paused" in r.json()["detail"], "autopick is refused while paused")
        resume_at = at(11, 21, 0, 30)
        set_now(resume_at)
        r = client.post(f"{url}/resume", headers=auth["commissioner"]).json()
        check(r["clock_status"] == "running" and r["remaining_seconds"] == 200 and r["deadline_at"].startswith("2026-10-11T21:03:50"),
              "resuming restarts the clock with exactly the 200s that were frozen")
        check(client.post(f"{url}/resume", headers=auth["commissioner"]).json()["deadline_at"] == r["deadline_at"], "resuming again is a no-op")
        check(client.post(f"{url}/resume", headers=auth["a"]).status_code == 403 and client.post(f"{url}/resume").status_code == 401,
              "resume is commissioner-only")
        # pause after the time has run out: the pick is made first
        set_now(at(11, 21, 4, 0))
        r = client.post(f"{url}/pause", headers=auth["commissioner"]).json()
        ps = [p for p in picks(pdraft) if p["draftable_asset_id"]]
        check(len(ps) == 1 and ps[0]["is_autopick"] and r["paused"] and r["remaining_seconds"] == 290 and r["pick_number"] == 2,
              "pausing an expired pick does not revive it: it is auto-picked at its deadline and the next pick is paused with 290s (it started at the missed deadline)")
        client.post(f"{url}/resume", headers=auth["commissioner"])

        # ---- overnight window --------------------------------------------------------------------------------------------
        set_now(at(12, 20, 30))
        r = start("night", {"pick_seconds": 7200, "overnight_start": "22:00", "overnight_end": "06:00", "utc_offset_minutes": 0})
        ndraft = r.json()["id"]
        check(r.json()["deadline_at"].startswith("2026-10-13T06:30:00"),
              "a 2h pick started at 20:30 with a 22:00-06:00 pause is due at 06:30 the next morning")
        c = clk(ndraft)
        check(c["mode"] == "async" and c["overnight"] == {"start": "22:00", "end": "06:00", "utc_offset_minutes": 0}
              and c["clock_status"] == "running" and c["remaining_seconds"] == 7200, "async clock reports the window and the full 2h at the start")
        set_now(at(12, 21, 30))
        check(clk(ndraft)["remaining_seconds"] == 3600, "at 21:30 one hour is left")
        for when in [at(12, 22, 0), at(12, 23, 0), at(13, 3, 0), at(13, 5, 59, 59)]:
            set_now(when)
            c = clk(ndraft)
            check(c["remaining_seconds"] == 1800 and c["clock_status"] == "overnight" and c["overnight_active"] is True
                  and c["resumes_at"].startswith("2026-10-13T06:00:00"), f"overnight at {when:%H:%M:%S}: 30 min left, frozen, resumes 06:00")
        set_now(at(13, 6, 0))
        check(clk(ndraft)["clock_status"] == "running" and clk(ndraft)["remaining_seconds"] == 1800, "at 06:00 the clock runs again with the same 30 minutes")
        set_now(at(13, 6, 10))
        check(clk(ndraft)["remaining_seconds"] == 1200, "at 06:10 twenty minutes remain")
        set_now(at(13, 6, 29, 59))
        check(client.post(f"/drafts/{ndraft}/process-overdue").json()["processed"] == 0, "one second before the deadline nothing happens")
        set_now(at(13, 6, 30))
        r = client.post(f"/drafts/{ndraft}/process-overdue").json()
        check(r["processed"] == 1 and r["picks"][0]["picked_at"].startswith("2026-10-13T06:30:00") and r["clock"]["deadline_at"].startswith("2026-10-13T08:30:00"),
              "exactly at the deadline the pick is made and the next 2h clock starts at 06:30 (due 08:30)")
        # A commissioner pause during the overnight window does not eat the frozen time either.
        set_now(at(13, 8, 0))
        client.post(f"/drafts/{ndraft}/pause", headers=auth["commissioner"])
        set_now(at(13, 22, 30))
        check(clk(ndraft)["remaining_seconds"] == 1800, "paused at 08:00 with 30 min left; still 30 min at 22:30")
        r = client.post(f"/drafts/{ndraft}/resume", headers=auth["commissioner"]).json()
        check(r["clock_status"] == "overnight" and r["remaining_seconds"] == 1800 and r["deadline_at"].startswith("2026-10-14T06:30:00"),
              "resuming inside the overnight window keeps the 30 minutes and resumes counting at 06:00")
        # A pick made one second before the window starts.
        set_now(at(14, 5, 0))
        client.post(f"/drafts/{ndraft}/process-overdue")
        set_now(at(14, 21, 59, 59))
        c = clk(ndraft)
        pick_team = c["on_clock_team_id"]
        owner_hdr = {leagues["night"]["teams"][0]: auth["a"], leagues["night"]["teams"][1]: auth["b"]}.get(pick_team)
        if owner_hdr:
            r = client.post(f"/drafts/{ndraft}/pick", json={"draftable_asset_id": assets[40]}, headers=owner_hdr)
            check(r.status_code == 200, "a human pick one second before the window opens")
            expected = dc.add_active_seconds(at(14, 21, 59, 59), 7200, dc.make_window(1320, 360, 0))
            check(clk(ndraft)["deadline_at"].startswith(expected.isoformat()[:19]) and expected == at(15, 7, 59, 59),
                  "its 2h allowance spends 1 second before the window and 1h59m59s after it (07:59:59)")

        # ---- restart safety ------------------------------------------------------------------------------------------------
        set_now(at(16, 9))
        boot = start("boot", {"pick_seconds": 90}).json()["id"]
        snapshot = clk(boot)
        engine.dispose()                      # drop every connection, as a process restart would
        restarted = TestClient(app)
        check(restarted.get(f"/drafts/{boot}/clock").json() == snapshot, "a new process reads the identical clock from the database")
        set_now(at(16, 9, 20))
        r = restarted.post(f"/drafts/{boot}/process-overdue").json()
        check(r["processed"] == 12 and r["clock"]["clock_status"] == "complete",
              "after a restart a long outage is caught up in one call (12 picks), each stamped with its own deadline")
        stamps = [p["picked_at"] for p in picks(boot)]
        check(stamps == [(at(16, 9) + dt.timedelta(seconds=90 * (i + 1))).isoformat() for i in range(12)],
              "every autopick is stamped exactly 90s after the previous one, regardless of when it was applied")
        c = clk(boot)
        check(c["clock_status"] == "complete" and c["deadline_at"] is None and c["remaining_seconds"] is None and c["on_clock_team_id"] is None,
              "a finished draft has no deadline")
        check(restarted.post(f"/drafts/{boot}/pause", headers=auth["commissioner"]).status_code == 409, "a finished draft cannot be paused")

        with SessionLocal() as db:
            d = db.get(Draft, boot)
            check(d.deadline_at is None and d.paused is False, "completion clears the stored deadline")
            check(db.query(DraftPick).filter_by(draft_id=boot, is_autopick=True).count() == 12 and
                  db.query(RosterEntry).count() >= 12, "autopicks create roster entries like manual picks")

        # ---- the AI-team path still works on a timed draft ---------------------------------------------------------------
        set_now(at(17, 9))
        ai = start("async", {"pick_seconds": 3600}).json()["id"]
        t = leagues["async"]["teams"]
        client.post(f"/drafts/{ai}/pick", json={"draftable_asset_id": assets[1]}, headers=auth["a"])
        client.post(f"/drafts/{ai}/pick", json={"draftable_asset_id": assets[2]}, headers=auth["b"])
        r = client.post(f"/drafts/{ai}/autopick")
        check(r.status_code == 200 and r.json()["team_id"] == t[2] and r.json()["is_autopick"], "an AI-owned team can be auto-picked at any time on a timed draft")
        check(clk(ai)["mode"] == "async" and clk(ai)["remaining_seconds"] == 3600, "an AI autopick starts the next team's full clock")

    import shutil
    import subprocess
    node = shutil.which("node")
    check(node is not None, "node is available")
    proc = subprocess.run([node, str(ROOT / "scripts" / "check_draft_clock_logic.js"), str(ROOT / "web" / "static" / "draft-clock-logic.js")],
                          capture_output=True, text=True)
    print(proc.stdout, end="")
    check(proc.returncode == 0, "draft clock page logic passes under Node" + (f": {proc.stderr.strip()}" if proc.returncode else ""))
    html = (ROOT / "web" / "static" / "draft-board.html").read_text(encoding="utf-8")
    check("draft-clock-logic.js" in html and "process-overdue" in html and "Pick clock" in html,
          "the draft board loads the clock logic and offers clock setup")

    print("\nAll async snake draft checks passed.")


if __name__ == "__main__":
    main()
