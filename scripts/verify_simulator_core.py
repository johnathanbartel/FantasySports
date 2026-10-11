"""
Offline verification for the progressive simulator core (FS-025).

Uses synthetic events only (no sport scoring, no randomness, no external services): a deterministic
three-period script is published for one series daily game, then revealed through the real HTTP API with an
injected clock. Checks the contract validator, immutability, lifecycle transitions, monotonic displayed
scores, idempotent polling, and that one viewer's reveal never affects another's.

Run from the repository root with:

    .venv-api/Scripts/python.exe scripts/verify_simulator_core.py
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def synthetic_script(window_seconds: int = 3600):
    """3 periods x 1200 s. Home 3 - away 2. Totals are tracked here so the spec below stays readable."""
    from fantasy_sports.platform.simulator.contract import CONTRACT_VERSION, SimEventInput, SimScript
    spec = [
        ("period_start", 1, 0, None), ("shot", 1, 300, "home"), ("save", 1, 600, "away"), ("score", 1, 900, "home"),
        ("period_end", 1, 1200, None), ("period_start", 2, 1200, None), ("score", 2, 1500, "away"),
        ("shot", 2, 2000, "home"), ("score", 2, 2300, "away"), ("period_end", 2, 2400, None),
        ("period_start", 3, 2400, None), ("score", 3, 2900, "home"), ("score", 3, 3300, "home"),
        ("game_end", 3, 3600, None),
    ]
    hs = as_ = hsh = ash = 0
    events = []
    for i, (kind, period, second, side) in enumerate(spec, start=1):
        if kind in ("score", "shot", "save"):
            if side == "home":
                hsh += 1
            else:
                ash += 1
        if kind == "score":
            if side == "home":
                hs += 1
            else:
                as_ += 1
        events.append(SimEventInput(i, kind, period, second, side, hs, as_, hsh, ash, f"{kind} {i}", {"n": i}))
    return SimScript(CONTRACT_VERSION, "synthetic", 3, 1200, window_seconds, tuple(events), {"source": "verify"})


def expect_invalid(script, fragment: str, label: str) -> None:
    from fantasy_sports.platform.simulator.contract import ContractError, validate_script
    try:
        validate_script(script)
    except ContractError as e:
        check(fragment in str(e), f"{label} rejected ({e})")
        return
    raise AssertionError(f"{label} was accepted")


def mutate(script, index: int, **changes):
    events = list(script.events)
    events[index] = dataclasses.replace(events[index], **changes)
    return dataclasses.replace(script, events=tuple(events))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fs025-sim-", dir=ROOT, ignore_cleanup_errors=True) as temp_dir:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir) / 'sim').as_posix()}"

        from fastapi.testclient import TestClient

        from fantasy_sports.api.auth import create_session
        from fantasy_sports.api.main import app
        from fantasy_sports.platform import series_schedule
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            League, LeagueSeries, LeagueUniverse, SeriesDailyGame, SimulatorEvent, Team, Universe, User,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine
        from fantasy_sports.platform.simulator import contract, service, state

        # ---- Contract validation (pure) -------------------------------------------------------------------
        good = synthetic_script()
        contract.validate_script(good)
        check(good.digest() == synthetic_script().digest(), "script digest is stable")
        check(contract.SimScript.from_dict(good.to_dict()) == good, "script survives a dict round trip")
        expect_invalid(dataclasses.replace(good, contract_version="2.0"), "Unsupported contract version", "major version 2")
        expect_invalid(mutate(good, 3, seq=9), "seq must be", "gap in seq")
        expect_invalid(mutate(good, 11, home_score=0), "decreased", "score that goes down after a goal")
        expect_invalid(mutate(good, 1, home_score=1), "only a 'score' event", "score change on a non-score event")
        expect_invalid(mutate(good, 3, away_score=1), "own team_side", "score event moving the other side")
        expect_invalid(mutate(good, 2, game_second=100), "runs backwards", "clock running backwards")
        expect_invalid(mutate(good, 6, period=1), "outside period 1", "period 2 event stamped period 1")
        expect_invalid(mutate(mutate(good, 4, period=2), 5, period=1), "period runs backwards", "period running backwards")
        expect_invalid(mutate(good, 1, period=4), "outside 1-3", "period beyond the script")
        expect_invalid(mutate(good, 1, kind="mystery"), "unknown kind", "unknown event kind")
        expect_invalid(mutate(good, 1, kind="narrative"), "shot totals", "narrative event changing shots")
        expect_invalid(mutate(good, 13, home_score=9), "only a 'score' event", "game_end changing the totals")
        expect_invalid(dataclasses.replace(good, events=good.events[:-1]), "game_end", "script without game_end")
        expect_invalid(dataclasses.replace(good, period_seconds=1201), "period_seconds", "period longer than the column allows")
        expect_invalid(dataclasses.replace(good, events=()), "at least one", "empty script")

        # ---- Fixture league ------------------------------------------------------------------------------
        Base.metadata.create_all(bind=engine)
        start = datetime.date(2026, 10, 7)
        with SessionLocal() as db:
            commissioner = User(email="comm@example.com", display_name="Commissioner")
            viewer_a, viewer_b, viewer_c = (User(email=f"{n}@example.com", display_name=n) for n in "abc")
            db.add_all([commissioner, viewer_a, viewer_b, viewer_c])
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            db.add(nhl)
            db.flush()
            league = League(name="SimLeague", commissioner_user_id=commissioner.id, size=4, series_length_days=5)
            db.add(league)
            db.flush()
            db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id))
            for i in range(4):
                db.add(Team(league_id=league.id, name=f"T{i + 1}", is_ai_owned=True))
            db.flush()
            series_schedule.generate_schedule(db, league, start)
            db.commit()
            games = db.query(SeriesDailyGame).order_by(SeriesDailyGame.id).all()
            game_id, game2_id = games[0].id, games[5].id  # day 1 and day 1 of the next series
            series_id = games[0].series_id
            tokens = {n: create_session(db, u.id).token for n, u in
                      dict(commissioner=commissioner, a=viewer_a, b=viewer_b, c=viewer_c).items()}
        auth = {n: {"Authorization": f"Bearer {t}"} for n, t in tokens.items()}

        # ---- Publishing: idempotent, immutable ---------------------------------------------------------------
        with SessionLocal() as db:
            dg = db.get(SeriesDailyGame, game_id)
            sim = service.publish_script(db, dg, good)
            db.commit()
            first_id = sim.id
        with SessionLocal() as db:
            dg = db.get(SeriesDailyGame, game_id)
            again = service.publish_script(db, dg, good)
            check(again.id == first_id and db.query(SimulatorEvent).count() == len(good.events),
                  "publishing the identical script again is a no-op")
            try:
                service.publish_script(db, dg, mutate(good, 1, headline="edited"))
                raise AssertionError("a different script was accepted")
            except service.SimulatorError as e:
                check(e.code == "ALREADY_PUBLISHED", "a different script for the same game is refused")
            ev = db.query(SimulatorEvent).first()
            ev.headline = "tampered"
            try:
                db.flush()
                raise AssertionError("event update was allowed")
            except ValueError as e:
                check("immutable" in str(e), "an event cannot be updated through the ORM")
            db.rollback()
            ev = db.query(SimulatorEvent).first()
            db.delete(ev)
            try:
                db.flush()
                raise AssertionError("event delete was allowed")
            except ValueError as e:
                check("immutable" in str(e), "an event cannot be deleted through the ORM")
            db.rollback()
            started = db.get(SeriesDailyGame, game2_id)
            started.status = "LIVE_SIMULATING"
            try:
                service.publish_script(db, started, good)
                raise AssertionError("script accepted for a started game")
            except service.SimulatorError as e:
                check(e.code == "GAME_STARTED", "a started game cannot be given a script")
            db.rollback()

        # ---- HTTP flow with an injected clock ---------------------------------------------------------------
        t0 = datetime.datetime(2026, 10, 7, 19, 0, 0)
        clock = {"now": t0 - datetime.timedelta(minutes=5)}
        service.utcnow = lambda: clock["now"]
        client = TestClient(app)
        url = f"/simulator/games/{game_id}"

        def at(seconds: float):
            clock["now"] = t0 + datetime.timedelta(seconds=seconds)

        r = client.get("/simulator/contract")
        check(r.json()["contract_version"] == "1.0" and "score" in r.json()["event_kinds"], "contract endpoint reports version 1.0")
        check(client.get(f"{url}/state").status_code == 401, "state requires a signed-in user")
        check(client.get("/simulator/games/99999/state", headers=auth["a"]).status_code == 404, "unknown game is 404")
        r = client.get(f"/simulator/games/{game2_id}/state", headers=auth["a"])
        check(r.status_code == 200 and r.json()["published"] is False and r.json()["status"] == "SCHEDULED",
              "an unpublished game reads as scheduled and unpublished")
        check(client.get(f"/simulator/games/{game2_id}/events", headers=auth["a"]).status_code == 409,
              "events for an unpublished game are a 409")

        r = client.get(f"{url}/state", headers=auth["a"]).json()
        check(r["published"] and r["status"] == "SCHEDULED" and r["veiled"] and r["home_score"] is None,
              "scheduled: veiled, no scores exposed")
        r = client.post(f"{url}/watch", headers=auth["a"])
        check(r.status_code == 409 and r.json()["detail"]["code"] == "NOT_STARTED", "cannot watch before the game starts")
        r = client.post(f"{url}/skip", headers=auth["a"])
        check(r.status_code == 409 and r.json()["detail"]["code"] == "NOT_FINAL", "cannot skip to the box score before it is final")
        check(client.post(f"{url}/start", headers=auth["a"]).status_code == 403, "starting a game is commissioner-only")

        at(0)
        r = client.post(f"{url}/start", headers=auth["commissioner"])
        check(r.status_code == 200 and r.json()["status"] == "LIVE", "commissioner starts the game")
        at(30)
        r = client.post(f"{url}/start", headers=auth["commissioner"])
        check(r.status_code == 200, "starting again is idempotent")
        with SessionLocal() as db:
            check(db.get(SimulatorEvent, 1) is not None and service.get_sim(db, game_id).started_at == t0,
                  "a repeat start keeps the original start time")
            check(db.get(SeriesDailyGame, game_id).status == "LIVE_SIMULATING"
                  and db.get(LeagueSeries, series_id).status == "IN_PROGRESS", "daily game and series move to in-progress")

        # Veil: nothing leaks before a viewer opts in.
        at(1000)
        r = client.get(f"{url}/state", headers=auth["b"]).json()
        check(r["status"] == "LIVE" and r["veiled"] and r["home_score"] is None and r["period"] is None,
              "live but veiled: lifecycle only")
        r = client.get(f"{url}/events", headers=auth["b"]).json()
        check(r["events"] == [] and r["available_through"] == 0, "a veiled viewer gets no events")
        check(client.get(f"{url}/final", headers=auth["b"]).status_code == 409, "final is unavailable while live")

        # Independent live watchers.
        r = client.post(f"{url}/watch", headers=auth["a"])
        check(r.status_code == 200 and r.json()["reveal_mode"] == "WATCHING" and not r.json()["veiled"], "viewer A opens the game")
        r = client.post(f"{url}/watch", headers=auth["a"])
        check(r.status_code == 200, "watching twice is idempotent")
        poll1 = client.get(f"{url}/events?after_seq=0", headers=auth["a"]).json()
        poll2 = client.get(f"{url}/events?after_seq=0", headers=auth["a"]).json()
        check(poll1 == poll2, "polling is idempotent: the same request returns the same page")
        check([e["seq"] for e in poll1["events"]] == [1, 2, 3, 4] and poll1["next_seq"] == 4,
              "at t=1000s the live viewer sees events 1-4 only")
        check(poll1["events"][-1]["home_score"] == 1 and poll1["events"][-1]["away_score"] == 0, "scoreboard after the first goal is 1-0")
        r = client.get(f"{url}/state", headers=auth["a"]).json()
        check((r["period"], r["seconds_remaining"], r["home_score"], r["away_score"], r["home_shots"]) == (1, 200, 1, 0, 2),
              "live clock reads period 1, 3:20 left, 1-0, 2 home shots")
        r = client.get(f"{url}/state", headers=auth["b"]).json()
        check(r["veiled"] and r["home_score"] is None, "viewer B is still veiled after A opened the game")

        r = client.get(f"{url}/events?after_seq=2&limit=1", headers=auth["a"]).json()
        check([e["seq"] for e in r["events"]] == [3] and r["has_more"] is True and r["next_seq"] == 3, "limit and after_seq page the feed")
        check(client.get(f"{url}/events?after_seq=-1", headers=auth["a"]).status_code == 400, "a negative cursor is rejected")

        with SessionLocal() as db:
            dg = db.get(SeriesDailyGame, game_id)
            check((dg.home_score, dg.away_score, dg.current_period, dg.seconds_remaining) == (1, 0, 1, 200),
                  "the daily-game row mirrors the public live state")

        r = client.post(f"{url}/skip", headers=auth["a"])
        check(r.status_code == 409 and r.json()["detail"]["code"] == "NOT_FINAL", "cannot skip while live")
        r = client.post(f"{url}/progress", json={"cursor_seq": 14}, headers=auth["a"])
        check(r.status_code == 400 and r.json()["detail"]["code"] == "AHEAD_OF_GAME", "cannot acknowledge events that have not happened")
        r = client.post(f"{url}/progress", json={"cursor_seq": 4}, headers=auth["a"])
        check(r.json()["cursor_seq"] == 4, "progress records the live cursor")
        r = client.post(f"{url}/progress", json={"cursor_seq": 2}, headers=auth["a"])
        check(r.json()["cursor_seq"] == 4, "a lower progress report cannot rewind the cursor")
        check(client.post(f"{url}/progress", json={"cursor_seq": 1}, headers=auth["b"]).status_code == 409,
              "a veiled viewer cannot report progress")

        # Displayed scores never decrease across the whole live window.
        seen = [(0, 0)]
        for sec in range(1000, 3601, 100):
            at(sec)
            s = client.get(f"{url}/state", headers=auth["a"]).json()
            seen.append((s["home_score"], s["away_score"]))
        check(all(b[0] >= a[0] and b[1] >= a[1] for a, b in zip(seen, seen[1:])), "displayed scores never decrease while polling")
        check(seen[-1] == (3, 2), "the game ends 3-2")

        # Final.
        at(3600)
        r = client.get(f"{url}/state", headers=auth["a"]).json()
        check(r["status"] == "FINAL" and r["can_skip"] and r["home_score"] == 3, "at the end of the window the game is FINAL")
        with SessionLocal() as db:
            dg = db.get(SeriesDailyGame, game_id)
            check((dg.status, dg.home_score, dg.away_score, dg.current_period, dg.seconds_remaining) == ("FINAL", 3, 2, 3, 0),
                  "the daily-game row is finalised with the final score")
            check(db.get(LeagueSeries, series_id).status == "IN_PROGRESS", "the series stays in progress until every day is final")
            finalised = service.get_sim(db, game_id).finalized_at
            check(finalised == t0 + datetime.timedelta(seconds=3600), "finalised_at is the deterministic window end")
        at(99999)
        client.get(f"{url}/state", headers=auth["a"])
        with SessionLocal() as db:
            check(service.get_sim(db, game_id).finalized_at == finalised, "syncing again later changes nothing (idempotent)")

        r = client.get(f"{url}/final", headers=auth["a"])
        check(r.status_code == 403 and r.json()["detail"]["code"] == "REVEAL_REQUIRED",
              "viewer A has not seen the ending, so the final snapshot is withheld")
        r = client.post(f"{url}/progress", json={"cursor_seq": 14}, headers=auth["a"])
        check(r.json()["is_complete"] is True and r.json()["provenance"] == {"source": "verify"}, "watching to the last event completes the game")
        r = client.get(f"{url}/final", headers=auth["a"]).json()
        check((r["home_score"], r["away_score"], r["home_shots"], r["away_shots"]) == (3, 2, 5, 3)
              and len(r["scoring_events"]) == 5 and r["contract_version"] == "1.0", "final snapshot: score, shots, scoring events")

        # Independent skip, after the fact.
        r = client.get(f"{url}/state", headers=auth["b"]).json()
        check(r["veiled"] and r["home_score"] is None and r["status"] == "FINAL", "B never opened the game: a finished game is still veiled")
        check(client.get(f"{url}/final", headers=auth["b"]).status_code == 403, "veiled B cannot read the final")
        r = client.post(f"{url}/skip", headers=auth["b"])
        check(r.status_code == 200 and r.json()["reveal_mode"] == "SKIPPED" and r.json()["home_score"] == 3, "B skips to the box score")
        check(client.post(f"{url}/skip", headers=auth["b"]).status_code == 200, "skipping twice is idempotent")
        r = client.post(f"{url}/watch", headers=auth["b"])
        check(r.json()["reveal_mode"] == "SKIPPED", "watching after skipping does not downgrade the reveal")
        check(client.get(f"{url}/final", headers=auth["b"]).status_code == 200, "B can read the final")
        r = client.get(f"{url}/events", headers=auth["b"]).json()
        check(len(r["events"]) == 14 and r["has_more"] is False, "B can page the entire feed after skipping")

        # Independent replay with a resumable cursor.
        r = client.get(f"{url}/state", headers=auth["c"]).json()
        check(r["veiled"], "C is veiled")
        r = client.post(f"{url}/watch", headers=auth["c"]).json()
        check(r["reveal_mode"] == "WATCHING" and (r["home_score"], r["away_score"]) == (0, 0) and r["cursor_seq"] == 0
              and not r["is_complete"], "C replays from 0-0 at the start")
        r = client.post(f"{url}/progress", json={"cursor_seq": 7}, headers=auth["c"]).json()
        check((r["home_score"], r["away_score"], r["period"], r["seconds_remaining"]) == (1, 1, 2, 900),
              "after event 7 the replay scoreboard reads 1-1, period 2, 15:00 left")
        r = client.get(f"{url}/events?after_seq=7", headers=auth["c"]).json()
        check([e["seq"] for e in r["events"]] == list(range(8, 15)), "a reconnecting replay resumes from its cursor")
        check(client.get(f"{url}/final", headers=auth["c"]).status_code == 403, "C cannot read the final mid-replay")
        a_state = client.get(f"{url}/state", headers=auth["a"]).json()
        check(a_state["is_complete"] and a_state["home_score"] == 3, "A's finished view is unaffected by C's replay")

        # Cursor survives a "restart": nothing is held in memory.
        client2 = TestClient(app)
        check(client2.get(f"{url}/state", headers=auth["c"]).json()["cursor_seq"] == 7, "reveal cursors are persisted, not held in memory")

        # Pure state arithmetic.
        shape = state.GameShape(3, 1200, 3600)
        check(state.clock_at(shape, 0) == (1, 1200) and state.clock_at(shape, 1200) == (1, 0) and state.clock_at(shape, 1201) == (2, 1199)
              and state.clock_at(shape, 3600) == (3, 0), "clock arithmetic at period boundaries")
        check(state.public_elapsed(shape, "LIVE", t0, t0 - datetime.timedelta(seconds=5)) == 0, "a clock behind the start reads zero")
        check(state.reveal_bound("LIVE", None, 5, 14) == 0 and state.reveal_bound("LIVE", "WATCHING", 5, 14) == 5
              and state.reveal_bound("FINAL", "SKIPPED", 14, 14) == 14, "reveal bounds by mode")

        # Football/other leagues' tables untouched; simulator is not tied to hockey scoring.
        with SessionLocal() as db:
            check(db.query(SimulatorEvent).count() == 14, "exactly one script's events are stored")

    print("\nAll progressive simulator core checks passed.")


if __name__ == "__main__":
    main()
