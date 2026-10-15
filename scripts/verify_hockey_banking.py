"""
Offline verification for NHL off-day credit banking (FS-028): idempotent ingestion, assignment rules (one credit
per game, 1.50 cap, no reassignment, eligible window), the explainable recommendation, authorization, and the
24-hour fallback processor. Throwaway SQLite, real HTTP API (FastAPI TestClient), injected clock, no external calls.

Run from the repository root with:

    .venv-api/Scripts/python.exe scripts/verify_hockey_banking.py
"""

from __future__ import annotations

import datetime
import math
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


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fs028-bank-", dir=ROOT, ignore_cleanup_errors=True) as temp_dir:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir) / 'bank').as_posix()}"

        from fastapi.testclient import TestClient
        from sqlalchemy.exc import IntegrityError

        from fantasy_sports.api.auth import create_session
        from fantasy_sports.api.main import app
        from fantasy_sports.platform import banking, series_schedule
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            BankedCredit, DraftableAsset, League, LeagueSeries, LeagueUniverse, Player, RosterEntry, SeriesDailyGame,
            Team, Universe, User,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine

        Base.metadata.create_all(bind=engine)
        client = TestClient(app)
        D0 = datetime.date(2026, 10, 7)

        def day(n: int) -> datetime.date:  # 1-based day of the schedule
            return D0 + datetime.timedelta(days=n - 1)

        with SessionLocal() as db:
            commissioner = User(email="comm@example.com", display_name="Commissioner")
            owner1, owner2 = User(email="o1@example.com", display_name="Owner1"), User(email="o2@example.com", display_name="Owner2")
            db.add_all([commissioner, owner1, owner2])
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            cfb = Universe(sport="football", level="college", code="cfb", display_name="CFB", supports_individual_defense=True)
            db.add_all([nhl, cfb])
            db.flush()
            league = League(name="Bank League", commissioner_user_id=commissioner.id, size=4, series_length_days=7)
            fb_league = League(name="FB League", commissioner_user_id=commissioner.id, size=2)
            db.add_all([league, fb_league])
            db.flush()
            db.add_all([LeagueUniverse(league_id=league.id, universe_id=nhl.id), LeagueUniverse(league_id=fb_league.id, universe_id=cfb.id)])
            t1 = Team(league_id=league.id, name="T1", owner_user_id=owner1.id, is_ai_owned=False)
            t2 = Team(league_id=league.id, name="T2", owner_user_id=owner2.id, is_ai_owned=False)
            t3, t4 = Team(league_id=league.id, name="T3", is_ai_owned=True), Team(league_id=league.id, name="T4", is_ai_owned=True)
            fb_team = Team(league_id=fb_league.id, name="FBT", owner_user_id=owner1.id, is_ai_owned=False)
            db.add_all([t1, t2, t3, t4, fb_team])
            db.flush()

            def player(team: Team, name: str, universe: Universe = nhl) -> int:
                p = Player(universe_id=universe.id, full_name=name, position="C")
                db.add(p)
                db.flush()
                a = DraftableAsset(universe_id=universe.id, asset_type="player", player_id=p.id)
                db.add(a)
                db.flush()
                db.add(RosterEntry(team_id=team.id, draftable_asset_id=a.id))
                return a.id

            assets = {n: player(t1, n) for n in ("McDavid", "Draisaitl", "Nurse", "Bouchard", "Skinner")}
            foreign = player(t2, "Other Owner Player")
            fb_asset = player(fb_team, "Football Guy", cfb)
            series_schedule.generate_schedule(db, league, D0, cycles=2, window_rows=False)   # 2 cycles: plenty of games in the window
            db.commit()
            ids = dict(t1=t1.id, t2=t2.id, t3=t3.id, t4=t4.id, league=league.id, fb_team=fb_team.id, fb_league=fb_league.id)
            tok = {n: create_session(db, u.id).token for n, u in dict(commissioner=commissioner, owner1=owner1, owner2=owner2).items()}
        auth = {n: {"Authorization": f"Bearer {t}"} for n, t in tok.items()}

        # ---- fixture shaping: day 4 is an off day league-wide; days 1-3 are final with chosen results ----------
        with SessionLocal() as db:
            for g in db.query(SeriesDailyGame).filter(SeriesDailyGame.calendar_date == day(4)):
                g.is_matchup_day = False
            round1 = db.query(LeagueSeries).filter_by(league_id=ids["league"], start_date=D0).all()
            t1_series = next(s for s in round1 if ids["t1"] in (s.team_a_id, s.team_b_id))
            x_id = t1_series.team_b_id if t1_series.team_a_id == ids["t1"] else t1_series.team_a_id
            other_series = next(s for s in round1 if s.id != t1_series.id)
            round2 = db.query(LeagueSeries).filter_by(league_id=ids["league"], start_date=day(8)).all()
            r2 = next(s for s in round2 if ids["t1"] in (s.team_a_id, s.team_b_id))
            y_id = r2.team_b_id if r2.team_a_id == ids["t1"] else r2.team_a_id
            winner_in_other = y_id if y_id in (other_series.team_a_id, other_series.team_b_id) else other_series.team_a_id
            for g in db.query(SeriesDailyGame).filter(SeriesDailyGame.calendar_date <= day(3),
                                                      SeriesDailyGame.series_id.in_([t1_series.id, other_series.id])):
                win = ids["t1"] if g.series_id == t1_series.id else winner_in_other
                g.status = "FINAL"
                g.home_score, g.away_score = (3, 1) if g.home_team_id == win else (1, 3)
            db.commit()
            games_by_date = {g.calendar_date: g for g in db.query(SeriesDailyGame).filter(
                (SeriesDailyGame.home_team_id == ids["t1"]) | (SeriesDailyGame.away_team_id == ids["t1"]))}
            gid = {n: games_by_date[day(n)].id for n in range(1, 20) if day(n) in games_by_date}
            foreign_game = next(g.id for g in db.query(SeriesDailyGame).filter(SeriesDailyGame.calendar_date == day(5))
                                if ids["t1"] not in (g.home_team_id, g.away_team_id))

        clock = {"now": datetime.datetime.combine(day(4), datetime.time(21, 0))}
        banking.utcnow = lambda: clock["now"]
        NOW = clock["now"]

        def ingest(asset: str | int, points: float, key: str | None = None, date=day(4), team=None, when=None):
            with SessionLocal() as db:
                c, created = banking.ingest_credit(db, team_id=team or ids["t1"], asset_id=assets[asset] if isinstance(asset, str) else asset,
                                                   source_date=date, points=points, now=when or clock["now"], source_key=key)
                db.commit()
                return c.id, created

        def expect_error(fn, code: str, label: str):
            try:
                fn()
            except banking.BankingError as e:
                check(e.code == code, f"{label} -> {code}")
                return
            raise AssertionError(f"{label} was accepted")

        # ---- ingestion -----------------------------------------------------------------------------------------
        def raw(**kw):
            def run():
                with SessionLocal() as db:
                    banking.ingest_credit(db, **kw)
            return run
        base = dict(team_id=ids["t1"], asset_id=assets["McDavid"], source_date=day(4), points=1.35, now=NOW)
        expect_error(raw(**{**base, "points": 0.0}), "NON_POSITIVE", "zero credit")
        expect_error(raw(**{**base, "points": -0.4}), "NON_POSITIVE", "negative credit")
        expect_error(raw(**{**base, "points": math.nan}), "NON_POSITIVE", "NaN credit")
        expect_error(raw(**{**base, "asset_id": foreign}), "NOT_ROSTERED", "a player from another team")
        expect_error(raw(**{**base, "source_date": day(2)}), "NOT_OFF_DAY", "a matchup day")
        expect_error(raw(**{**base, "team_id": ids["fb_team"], "asset_id": fb_asset}), "NOT_NHL", "a football league")
        expect_error(raw(**{**base, "team_id": 99999}), "NOT_FOUND", "an unknown team")

        c_mcd, created = ingest("McDavid", 1.35)
        check(created, "a valid off-day credit is stored")
        again, created2 = ingest("McDavid", 1.35)
        check(not created2 and again == c_mcd, "ingesting the same player/date again is a no-op (same row)")
        again, created3 = ingest("McDavid", 9.99)
        check(not created3 and again == c_mcd, "a re-run with different points does not change the stored credit")
        with SessionLocal() as db:
            check(db.query(BankedCredit).count() == 1 and db.get(BankedCredit, c_mcd).credit_points == 1.35,
                  "exactly one row, first write wins")
        c_drai, _ = ingest("Draisaitl", 1.80)
        c_nurse, _ = ingest("Nurse", 0.40)
        c_bouch, _ = ingest("Bouchard", 0.90, key="event:777")
        check(ingest("Bouchard", 0.90, key="event:777")[1] is False and ingest("Bouchard", 0.90, key="event:778")[1] is True,
              "an explicit source key is the idempotency key (a different key is a different credit)")

        # ---- listing + authorization ---------------------------------------------------------------------------
        url = f"/teams/{ids['t1']}/banked-credits"
        check(client.get(url).status_code == 401, "listing requires a signed-in user")
        check(client.get(url, headers=auth["owner2"]).status_code == 403, "another owner cannot list the credits")
        check(client.get(url, headers=auth["commissioner"]).status_code == 403, "the commissioner cannot read a human team's credits")
        r = client.get(url, headers=auth["owner1"])
        body = r.json()
        check(r.status_code == 200 and body["cap"] == 1.5 and body["fallback_hours"] == 24 and len(body["credits"]) == 5,
              "the owner lists 5 credits with the cap and fallback hours")
        by_id = {c["id"]: c for c in body["credits"]}
        check(by_id[c_drai]["capped"] is True and by_id[c_drai]["credit_points"] == 1.8 and by_id[c_mcd]["capped"] is False,
              "a 1.80 credit is flagged capped; a 1.35 credit is not")
        check(by_id[c_mcd]["player_name"] == "McDavid" and by_id[c_mcd]["status"] == "AVAILABLE"
              and by_id[c_mcd]["fallback_at"].startswith(str((NOW + datetime.timedelta(hours=24)).date())),
              "each credit shows its player, status and the 24-hour fallback time")
        check(client.get(url + "?status=NOPE", headers=auth["owner1"]).status_code == 400, "an unknown status filter is rejected")
        check(client.get(f"/teams/{ids['fb_team']}/banked-credits", headers=auth["owner1"]).json()["detail"]["code"] == "NOT_NHL",
              "a football team is refused")

        # ---- eligible games and the recommendation -------------------------------------------------------------
        with SessionLocal() as db:
            eligible = [g.calendar_date for g in banking.eligible_games(db, db.get(BankedCredit, c_mcd), NOW)]
        check(eligible == [day(n) for n in range(5, 12)], "eligible: matchup days 5-11 (after the off day, within 7 days)")

        # Strength input present: round-1 opponent X lost 3, round-2 opponent Y won 3.
        r = client.get(f"/banked-credits/{c_mcd}/recommendation", headers=auth["owner1"])
        rec = r.json()
        check(r.status_code == 200 and rec["basis"] == "opponent_strength" and rec["confidence"] == "PARTIAL",
              "with opponent records but no projections the recommendation is PARTIAL on opponent strength")
        check(rec["recommended_game_id"] == gid[8] and rec["options"][0]["opponent_id"] == y_id,
              "it recommends the first game against the stronger opponent (Y, 3-0)")
        check(rec["inputs_available"] == {"projected_margin": False, "opponent_strength": True, "win_probability_swing": False},
              "inputs_available says which inputs exist")
        check(any("No probability is estimated" in s for s in rec["reasons"]), "the explanation says what was not used")
        check(all(o["projected_margin"] is None and o["win_probability_swing"] is None for o in rec["options"]),
              "missing projection fields are null, never fabricated")
        check([o["rank"] for o in rec["options"]] == list(range(1, 8)) and rec["options"][-1]["opponent_id"] == x_id,
              "all 7 options are ranked; the weakest opponent is last")
        check(client.get(f"/banked-credits/{c_mcd}/recommendation", headers=auth["owner1"]).json() == rec,
              "the recommendation is deterministic")
        capped_rec = client.get(f"/banked-credits/{c_drai}/recommendation", headers=auth["owner1"]).json()
        check(any("1.50" in s and "1.80" in s for s in capped_rec["reasons"]), "a capped credit's explanation states the cap")
        check(client.get(f"/banked-credits/{c_mcd}/recommendation", headers=auth["owner2"]).status_code == 403,
              "another owner cannot see the recommendation")
        check(client.get("/banked-credits/99999/recommendation", headers=auth["owner1"]).status_code == 404, "unknown credit is 404")

        # Neutral: an opponent needs 3 final games before its record counts; as of day 2 nobody has them.
        with SessionLocal() as db:
            for g in db.query(SeriesDailyGame).filter(SeriesDailyGame.calendar_date == day(2)):
                g.is_matchup_day = False
            db.commit()
        c_neutral, _ = ingest("Skinner", 0.70, date=day(2))
        with SessionLocal() as db:
            neutral = banking.recommend(db, db.get(BankedCredit, c_neutral), NOW)
        check(neutral.basis == "earliest_game" and neutral.confidence == "NEUTRAL" and neutral.recommended_game_id == gid[5]
              and "Neutral" in neutral.reasons[0] and neutral.inputs_available["opponent_strength"] is False,
              "with no usable inputs: explicit NEUTRAL recommendation, the earliest game")
        with SessionLocal() as db:
            check(banking.recommend(db, db.get(BankedCredit, c_neutral), NOW).recommended_game_id == neutral.recommended_game_id,
                  "the neutral recommendation is deterministic")
            proj = {gid[n]: {"projected_margin": 0.2 * n, "win_probability_swing": 0.01 * (10 - n)} for n in range(5, 12)}
            full = banking.recommend(db, db.get(BankedCredit, c_mcd), NOW, proj)
        check(full.confidence == "FULL" and full.basis == "win_probability_swing" and full.recommended_game_id == gid[5],
              "supplied projections give a FULL recommendation ranked by win-probability swing")

        # ---- assignment ------------------------------------------------------------------------------------------
        def assign(credit_id: int, game_id: int, who="owner1"):
            return client.post(f"/banked-credits/{credit_id}/assign", json={"daily_game_id": game_id}, headers=auth[who])

        check(client.post(f"/banked-credits/{c_mcd}/assign", json={"daily_game_id": gid[8]}).status_code == 401, "assigning requires sign-in")
        check(assign(c_mcd, gid[8], "owner2").status_code == 403, "another owner cannot assign")
        for label, game, code in [("a game of another team", foreign_game, "NOT_YOUR_GAME"), ("the day before the credit", gid[3], "BEFORE_CREDIT"),
                                  ("the off day itself", gid[4], "OFF_DAY"), ("a game beyond the 7-day window", gid[12 + 1], "OUTSIDE_WINDOW")]:
            r = assign(c_mcd, game)
            check(r.status_code == 400 and r.json()["detail"]["code"] == code, f"{label} is rejected ({code})")
        check(assign(c_mcd, 99999).status_code == 404, "an unknown game is 404")
        with SessionLocal() as db:
            db.get(SeriesDailyGame, gid[6]).status = "LIVE_SIMULATING"
            db.commit()
        r = assign(c_mcd, gid[6])
        check(r.status_code == 400 and r.json()["detail"]["code"] == "GAME_STARTED", "a game that has started is rejected")
        with SessionLocal() as db:
            db.get(SeriesDailyGame, gid[6]).status = "SCHEDULED"
            db.commit()
        clock["now"] = datetime.datetime.combine(day(7), datetime.time(9, 0))
        r = assign(c_mcd, gid[5])
        check(r.status_code == 400 and r.json()["detail"]["code"] == "IN_THE_PAST", "a game before today is rejected")
        clock["now"] = NOW

        r = assign(c_mcd, gid[8])
        check(r.status_code == 200 and r.json()["status"] == "ASSIGNED" and r.json()["applied_points"] == 1.35
              and r.json()["assignment_mode"] == "MANUAL" and r.json()["assigned_daily_game_id"] == gid[8],
              "the owner accepts the recommendation: ASSIGNED, applied 1.35, MANUAL")
        r = assign(c_mcd, gid[9])
        check(r.status_code == 409 and r.json()["detail"]["code"] == "ALREADY_ASSIGNED", "reassignment is rejected")
        r = assign(c_drai, gid[8])
        check(r.status_code == 409 and r.json()["detail"]["code"] == "GAME_HAS_CREDIT", "a second credit cannot take the same game (one per game)")
        r = assign(c_drai, gid[9])
        check(r.status_code == 200 and r.json()["applied_points"] == 1.5 and r.json()["credit_points"] == 1.8 and r.json()["capped"],
              "a 1.80 credit is applied at the 1.50 cap and keeps its earned value")
        check(client.get(f"/banked-credits/{c_mcd}/recommendation", headers=auth["owner1"]).status_code == 409,
              "an assigned credit has no recommendation")
        with SessionLocal() as db:
            assigned = banking.assigned_for_game(db, gid[8])
            check([c.id for c in assigned] == [c_mcd] and assigned[0].applied_points == 1.35, "assigned_for_game exposes the credit for scoring")
            others = banking.eligible_games(db, db.get(BankedCredit, c_nurse), NOW)
            check(gid[8] not in [g.id for g in others] and gid[9] not in [g.id for g in others],
                  "games that already hold a credit drop out of everyone's options")

        # Database-level guarantees (not just application checks).
        with SessionLocal() as db:
            c = db.get(BankedCredit, c_nurse)
            c.status, c.assigned_daily_game_id, c.applied_points = "ASSIGNED", gid[8], 0.4
            try:
                db.flush()
                raise AssertionError("two credits on one game were stored")
            except IntegrityError:
                check(True, "the database refuses a second credit on the same game")
            db.rollback()
            c = db.get(BankedCredit, c_nurse)
            c.status, c.assigned_daily_game_id, c.applied_points = "ASSIGNED", gid[10], 2.0
            try:
                db.flush()
                raise AssertionError("an applied amount above the cap was stored")
            except IntegrityError:
                check(True, "the database refuses an applied amount above 1.50")
            db.rollback()
            c = db.get(BankedCredit, c_nurse)
            c.status = "ASSIGNED"
            try:
                db.flush()
                raise AssertionError("an ASSIGNED credit without a game was stored")
            except IntegrityError:
                check(True, "the database refuses an ASSIGNED credit with no game")
            db.rollback()

        # ---- 24-hour fallback ---------------------------------------------------------------------------------------
        fb_url = f"/leagues/{ids['league']}/banked-credits/process-fallback"
        check(client.post(fb_url).status_code == 401, "fallback processing requires sign-in")
        check(client.post(fb_url, headers=auth["owner1"]).status_code == 403, "fallback processing is commissioner-only")
        check(client.post(f"/leagues/{ids['fb_league']}/banked-credits/process-fallback", headers=auth["commissioner"]).status_code == 400,
              "fallback processing refuses a non-NHL league")
        clock["now"] = NOW + datetime.timedelta(hours=23, minutes=59)
        r = client.post(fb_url, headers=auth["commissioner"]).json()
        check(r["assigned"] == [] and r["expired"] == [] and r["waiting"] >= 3, "before 24 hours nothing is auto-assigned")
        clock["now"] = NOW + datetime.timedelta(hours=24)
        with SessionLocal() as db:
            before = {c.id: (c.status, c.assigned_daily_game_id) for c in db.query(BankedCredit)}
        r = client.post(fb_url, headers=auth["commissioner"]).json()
        auto = {a["credit_id"]: a["daily_game_id"] for a in r["assigned"]}
        check(set(auto) == {c_nurse, c_bouch, c_bouch + 1}, f"at 24 hours the unclaimed credits are auto-assigned ({len(auto)})")
        check(auto[c_nurse] == gid[5] and auto[c_bouch] == gid[6] and auto[c_bouch + 1] == gid[7],
              "each goes to the immediately next eligible game, in banked order, skipping games that hold a credit")
        check(r["expired"] == [c_neutral], "a credit whose window has no free game left expires instead")
        with SessionLocal() as db:
            rows = {c.id: c for c in db.query(BankedCredit)}
            check(rows[c_nurse].assignment_mode == "AUTO" and rows[c_nurse].applied_points == 0.4, "auto-assigned credits are marked AUTO")
            check((rows[c_mcd].status, rows[c_mcd].assigned_daily_game_id) == before[c_mcd], "manually assigned credits are untouched")
            check({c.status for c in rows.values()} == {"ASSIGNED", "EXPIRED"}, "no credit is left AVAILABLE after the fallback")
            snapshot = {c.id: (c.status, c.assigned_daily_game_id, c.applied_points, c.assigned_at) for c in rows.values()}
        again = client.post(fb_url, headers=auth["commissioner"]).json()
        with SessionLocal() as db:
            check(again["assigned"] == [] and again["expired"] == []
                  and snapshot == {c.id: (c.status, c.assigned_daily_game_id, c.applied_points, c.assigned_at) for c in db.query(BankedCredit)},
                  "running the fallback again changes nothing (idempotent)")

        # Expiry: no eligible game left.
        with SessionLocal() as db:
            for g in db.query(SeriesDailyGame).filter(SeriesDailyGame.calendar_date == day(20)):
                g.is_matchup_day = False
            db.commit()
        c_late, _ = ingest("Skinner", 0.55, date=day(20), when=clock["now"])
        clock["now"] = datetime.datetime.combine(day(40), datetime.time(12, 0))
        r = client.post(fb_url, headers=auth["commissioner"]).json()
        check(c_late in r["expired"], "a credit with no eligible game left expires at fallback time")
        with SessionLocal() as db:
            c = db.get(BankedCredit, c_late)
            check(c.status == "EXPIRED" and c.status_reason == "NO_ELIGIBLE_GAME" and c.expired_at and c.assigned_daily_game_id is None,
                  "expired credits record why and when")
        r = assign(c_late, gid[5])
        check(r.status_code == 409 and r.json()["detail"]["code"] == "EXPIRED", "an expired credit cannot be assigned")
        listed = client.get(url + "?status=EXPIRED", headers=auth["owner1"]).json()["credits"]
        check(sorted(c["id"] for c in listed) == sorted([c_neutral, c_late]), "the status filter lists only expired credits")

        # A fresh league's schedule/standings regression is covered by the dedicated verify scripts.
        engine.dispose()

    print("\nAll NHL off-day banking checks passed.")


if __name__ == "__main__":
    main()
