"""
Offline verification for NHL series standings, rivalry history and the Game Score (FS-024).

Builds NHL leagues in a throwaway SQLite database, generates schedules through the real API, writes seeded
daily results straight into series_daily_games, and checks the API against an independent brute-force tally.
No external services are called.

Run from the repository root with:

    .venv-api/Scripts/python.exe scripts/verify_hockey_series_standings.py
"""

from __future__ import annotations

import datetime
import itertools
import os
import random
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def brute_standings(games: list[dict], teams: list[int]) -> dict[int, dict]:
    """Independent tally from plain dicts: final games only, each day its own game. Series never count."""
    t = {i: dict(w=0, l=0, t=0, gf=0, ga=0) for i in teams}
    for g in games:
        if g["status"] != "FINAL":
            continue
        h, a, hs, as_ = g["home"], g["away"], g["hs"], g["as"]
        t[h]["gf"] += hs; t[h]["ga"] += as_; t[a]["gf"] += as_; t[a]["ga"] += hs
        if hs == as_:
            t[h]["t"] += 1; t[a]["t"] += 1
        else:
            w, l = (h, a) if hs > as_ else (a, h)
            t[w]["w"] += 1; t[l]["l"] += 1
    return t


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fs024-standings-", dir=ROOT) as temp_dir:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir) / 'standings').as_posix()}"

        from fastapi.testclient import TestClient

        from fantasy_sports.api.auth import create_session
        from fantasy_sports.api.main import app
        from fantasy_sports.platform import game_score
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            League, LeagueSeries, LeagueUniverse, SeriesDailyGame, Team, Universe, User,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine

        Base.metadata.create_all(bind=engine)
        client = TestClient(app)
        start = datetime.date(2026, 10, 7)

        with SessionLocal() as db:
            commissioner = User(email="c@example.com", display_name="Commissioner")
            other = User(email="o@example.com", display_name="Other")
            db.add_all([commissioner, other])
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            cfb = Universe(sport="football", level="college", code="cfb", display_name="CFB", supports_individual_defense=True)
            db.add_all([nhl, cfb])
            db.flush()

            def make_league(name: str, universe: Universe, teams: int, length: int | None) -> League:
                league = League(name=name, commissioner_user_id=commissioner.id, size=teams, series_length_days=length)
                db.add(league)
                db.flush()
                db.add(LeagueUniverse(league_id=league.id, universe_id=universe.id))
                for i in range(teams):
                    db.add(Team(league_id=league.id, name=f"{name} T{i + 1}", is_ai_owned=True))
                db.flush()
                return league

            small = make_league("S4", nhl, 4, 5)
            big = make_league("B12", nhl, 12, 7)
            football = make_league("CFB", cfb, 12, None)
            db.commit()
            lid = dict(small=small.id, big=big.id, football=football.id)
            token = create_session(db, commissioner.id).token
            other_token = create_session(db, other.id).token
        auth = {"Authorization": f"Bearer {token}"}
        body = {"start_date": start.isoformat(), "cycles": 2}

        for key in ("small", "big"):
            r = client.post(f"/leagues/{lid[key]}/series-schedule", json=body, headers=auth)
            check(r.status_code == 200, f"{key} schedule created (2 cycles)")

        def load(league_id: int):
            with SessionLocal() as db:
                series = [dict(id=s.id, a=s.team_a_id, b=s.team_b_id, days=s.total_days)
                          for s in db.query(LeagueSeries).filter_by(league_id=league_id)]
                games = [dict(id=g.id, series_id=g.series_id, date=g.calendar_date, home=g.home_team_id, away=g.away_team_id,
                              hs=g.home_score, **{"as": g.away_score}, status=g.status)
                         for g in db.query(SeriesDailyGame).join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
                         .filter(LeagueSeries.league_id == league_id).order_by(SeriesDailyGame.calendar_date, SeriesDailyGame.id)]
                teams = sorted(t.id for t in db.query(Team).filter_by(league_id=league_id))
            return teams, series, games

        steams, sseries, sgames = load(lid["small"])
        base = f"/leagues/{lid['small']}"

        # Opening state: nothing final -> all zeros, one shared rank, no rivalry history.
        st = client.get(f"{base}/series-standings").json()
        check(st["tiebreak_order"].startswith("wins desc, losses asc, goal_diff desc, h2h_wins desc"), "tie-break order is published")
        check(all(r["rank"] == 1 and r["wins"] == 0 and r["goal_diff"] == 0 for r in st["standings"]),
              "opening standings: all zero, shared rank 1")
        check(client.get(f"{base}/rivalries").json()["rivalries"] == [], "no rivalry history before any final game")
        check(client.get(f"/leagues/{lid['football']}/series-standings").status_code == 400, "non-NHL league rejected")
        check(client.get("/leagues/99999/series-standings").status_code == 404, "unknown league is 404")

        # Seed deterministic results for the first cycle; leave the final game of the last series incomplete.
        rng = random.Random(24)
        first_cycle = sorted({g["date"] for g in sgames})[:15]  # 3 rounds x 5 days
        with SessionLocal() as db:
            rows = db.query(SeriesDailyGame).order_by(SeriesDailyGame.calendar_date, SeriesDailyGame.id).all()
            round3 = [g for g in rows if g.calendar_date == first_cycle[-1]]
            for g in rows:
                if g.calendar_date in first_cycle:
                    g.home_score, g.away_score, g.status = rng.randint(0, 5), rng.randint(0, 5), "FINAL"
            # A daily tie and a tied series: in round 1 force series A to 2 wins each + one tie.
            round1 = [g for g in rows if g.calendar_date in first_cycle[:5]]
            by_series = defaultdict(list)
            for g in round1:
                by_series[g.series_id].append(g)
            tied_series_id = sorted(by_series)[0]
            ts = by_series[tied_series_id]
            home_of = lambda g: g.home_team_id
            a_id = db.get(LeagueSeries, tied_series_id).team_a_id
            for i, g in enumerate(ts):
                if i == 4:
                    g.home_score, g.away_score = 3, 3
                else:
                    a_wins_here = i < 2
                    a_home = g.home_team_id == a_id
                    g.home_score, g.away_score = (4, 1) if a_wins_here == a_home else (1, 4)
            for g in round3[:1]:
                g.status, g.home_score, g.away_score = "LIVE_SIMULATING", 2, 1  # incomplete: not counted
            db.commit()

        steams, sseries, sgames = load(lid["small"])
        expected = brute_standings(sgames, steams)
        st = client.get(f"{base}/series-standings").json()
        got = {r["team_id"]: r for r in st["standings"]}
        for tid, e in expected.items():
            r = got[tid]
            check((r["wins"], r["losses"], r["ties"], r["goals_for"], r["goals_against"]) ==
                  (e["w"], e["l"], e["t"], e["gf"], e["ga"]), f"team {tid}: standings tie out to the brute-force daily tally")
        check(not any(k.startswith("series") for k in st["standings"][0]), "standings carry no series wins/losses")
        check(sum(r["ties"] for r in got.values()) >= 2, "equal-score games are ties, not W or L")
        check(sum(r["goals_for"] for r in got.values()) == sum(r["goals_against"] for r in got.values()),
              "league goals for equal goals against")
        incomplete = [g for g in sgames if g["status"] != "FINAL"]
        check(len(incomplete) > 0 and all(r["wins"] + r["losses"] + r["ties"] <= 15 for r in got.values()),
              "non-final games are excluded from the tallies")

        # Ranking order and tie-break keys.
        order = st["standings"]
        keys = [(-r["wins"], r["losses"], -r["goal_diff"]) for r in order]
        check(keys == sorted(keys), "rows are ordered by the published tie-break keys")
        check(order[0]["rank"] == 1 and all(a["rank"] <= b["rank"] for a, b in zip(order, order[1:])), "ranks never decrease")
        check(all("h2h_wins" in r for r in order), "head-to-head wins are exposed")

        # Tie-break layering on synthetic daily results (pure function): W-L, goal diff, h2h, goals for.
        from fantasy_sports.platform.series_standings import GameRecord, SeriesRecord, compute_standings
        d = datetime.date(2026, 1, 1)

        def one(sid, home, away, hs, as_, days=1):
            return SeriesRecord(sid, home, away, d, days), GameRecord(sid, d, home, away, hs, as_, True)

        # Goals for: teams 1 and 3 both win 1-0 in series with equal goal diff (+7), but 3 scores more.
        s1 = SeriesRecord(1, 1, 2, d, 5)
        s2 = SeriesRecord(2, 3, 4, d, 5)
        gs = []
        for i in range(5):
            day = d + datetime.timedelta(days=i)
            win = i < 3
            gs.append(GameRecord(1, day, 1, 2, 3 if win else 0, 0 if win else 1, True))
            gs.append(GameRecord(2, day, 3, 4, 4 if win else 0, 1 if win else 1, True))
        tab = compute_standings([1, 2, 3, 4], [s1, s2], gs)
        check([r.team_id for r in tab] == [3, 1, 4, 2] and tab[0].goal_diff == tab[1].goal_diff == 7,
              "equal W-L and goal diff fall through to goals for")
        check(len({r.rank for r in tab}) == 4, "goals for separates the ranks")

        # Head-to-head: teams 1 and 2 are 1-1 with equal goal diff and goals for; 1 beat 2.
        r1, g1 = one(1, 1, 2, 2, 1)  # 1 beats 2
        r2, g2 = one(2, 3, 1, 2, 1)  # 3 beats 1
        r3, g3 = one(3, 2, 4, 2, 1)  # 2 beats 4
        tab = compute_standings([1, 2, 3, 4], [r1, r2, r3], [g1, g2, g3])
        by = {r.team_id: r for r in tab}
        check(by[1].goal_diff == by[2].goal_diff == 0 and by[1].goals_for == by[2].goals_for == 3,
              "teams 1 and 2 are level on W-L, goal diff and goals for")
        check([r.team_id for r in tab] == [3, 1, 2, 4] and by[1].h2h_wins == 1 and by[2].h2h_wins == 0
              and by[1].rank == 2 and by[2].rank == 3, "head-to-head win breaks the tie")
        # Fully level teams share a rank; team id only orders them.
        t1, h1 = one(1, 1, 2, 1, 0)
        t2, h2 = one(2, 3, 4, 1, 0)
        tab = compute_standings([1, 2, 3, 4], [t1, t2], [h1, h2])
        check([r.team_id for r in tab][:2] == [1, 3] and tab[0].rank == tab[1].rank == 1 and tab[2].rank == 3,
              "fully level teams share a rank, ordered by team id")

        # Repeat reads are identical, and the data endpoint doesn't write.
        check(client.get(f"{base}/series-standings").json() == st, "repeat standings reads are identical")
        as_of = client.get(f"{base}/series-standings", params={"as_of": first_cycle[1].isoformat()}).json()
        check(sum(r["wins"] + r["losses"] + r["ties"] for r in as_of["standings"]) == 2 * 2 * 2,
              "as_of limits the tally to games on or before that date (2 days x 2 games x 2 teams)")

        # Rivalry history.
        riv = client.get(f"{base}/rivalries").json()["rivalries"]
        check(len(riv) == 6, "every pair has met in the first cycle, so six rivalry rows")
        for p in riv:
            lo, hi = p["team_low_id"], p["team_high_id"]
            mine = [g for g in sgames if g["status"] == "FINAL" and {g["home"], g["away"]} == {lo, hi}]
            lo_goals = sum(g["hs"] if g["home"] == lo else g["as"] for g in mine)
            hi_goals = sum(g["as"] if g["home"] == lo else g["hs"] for g in mine)
            lo_w = sum(1 for g in mine if (g["hs"] > g["as"]) == (g["home"] == lo) and g["hs"] != g["as"])
            hi_w = sum(1 for g in mine if (g["hs"] > g["as"]) == (g["home"] == hi) and g["hs"] != g["as"])
            check((p["goals_low"], p["goals_high"], p["daily_low_wins"], p["daily_high_wins"]) == (lo_goals, hi_goals, lo_w, hi_w)
                  and p["net_goal_diff_low"] == lo_goals - hi_goals and 0 <= p["intensity"] < 1,
                  f"rivalry {lo}-{hi} ties out to its games")
        one = client.get(f"{base}/rivalries/{riv[0]['team_high_id']}/{riv[0]['team_low_id']}").json()
        check(one["team_low_id"] == riv[0]["team_low_id"] and one["goals_low"] == riv[0]["goals_low"],
              "pair lookup is order-insensitive")
        check(client.get(f"{base}/rivalries/{steams[0]}/{steams[0]}").status_code == 404, "a team can't rival itself")
        check(client.get(f"{base}/rivalries", params={"team_id": steams[0]}).json()["rivalries"].__len__() == 3,
              "team filter keeps that team's three pairs")

        # Game Score: bounded, deterministic, breakdown present.
        gsr = client.get(f"{base}/game-scores").json()
        check(abs(sum(gsr["weights"].values()) - 1.0) < 1e-9, "weights sum to 1")
        check(all(0 <= g["score"] <= 100 for g in gsr["games"]), "every small-league Game Score is within 0-100")
        check(client.get(f"{base}/game-scores").json() == gsr, "repeat Game Score reads are identical")
        g0 = gsr["games"][0]
        check(all(0 <= g0[k] <= 1 for k in ("margin", "cutoff", "rivalry", "urgency")), "components are within 0-1")
        single = client.get(f"/series-daily-games/{g0['daily_game_id']}/game-score").json()
        check(single == g0, "per-game endpoint matches the league listing")
        opening = [g for g in gsr["games"] if g["calendar_date"] == start.isoformat()]
        last_day = max(g["calendar_date"] for g in gsr["games"])
        late = [g for g in gsr["games"] if g["calendar_date"] == last_day]
        check(max(g["score"] for g in opening) <= 15 and min(g["score"] for g in late) > max(g["score"] for g in opening),
              "opening-week games rate low and late-season games rate higher")
        check(all(g["standings_as_of"] == (datetime.date.fromisoformat(g["calendar_date"])
                                           - datetime.timedelta(days=7)).isoformat() for g in gsr["games"]),
              "ratings use standings published one week ahead")
        check(all(g["rivalry"] == 0 for g in opening), "opening games have no rivalry history")

        # Formula monotonicity and bounds on a grid.
        grid = [0, 0.25, 0.5, 0.75, 1.0]
        bounded = all(0 <= game_score.combine(m, c, r, u) <= 100 for m, c, r, u in itertools.product(grid, repeat=4))
        check(bounded, "combine() stays within 0-100 across the whole component grid")
        check(game_score.combine(1, 1, 1, 1) == 100 and game_score.combine(0, 0, 0, 0) == 0, "extremes map to exactly 100 and 0")
        mono = True
        for m, c, r, u in itertools.product(grid[:-1], repeat=4):
            i = grid.index(m)
            base_score = game_score.combine(m, c, r, u)
            for bumped in (game_score.combine(grid[i + 1], c, r, u), game_score.combine(m, grid[grid.index(c) + 1], r, u),
                           game_score.combine(m, c, grid[grid.index(r) + 1], u), game_score.combine(m, c, r, grid[grid.index(u) + 1])):
                mono &= bumped >= base_score
        check(mono, "combine() is monotonic non-decreasing in every component")
        check(game_score.combine(1, 1, 0, 0) == 0, "tight standings alone carry no leverage in week one")

        # Publish writes ratings for unfinished games only; commissioner only.
        pub = f"{base}/game-scores/publish"
        check(client.post(pub).status_code == 401, "publish requires sign-in")
        check(client.post(pub, headers={"Authorization": f"Bearer {other_token}"}).status_code == 403, "publish is commissioner-only")
        r = client.post(pub, headers=auth)
        check(r.status_code == 200, "commissioner can publish")
        with SessionLocal() as db:
            rows = db.query(SeriesDailyGame).join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id).filter(
                LeagueSeries.league_id == lid["small"]).all()
            by_id = {g["daily_game_id"]: g["score"] for g in r.json()["games"]}
            check(all(g.game_score_rating == by_id[g.id] for g in rows if g.status != "FINAL"), "stored ratings match the computed scores")
            check(any(g.status == "FINAL" and g.game_score_rating == 50 for g in rows), "FINAL games keep their stored rating")

        # 12-team league with seeded results across the whole schedule: every rating bounded, standings tie out.
        bteams, bseries, bgames = load(lid["big"])
        rng = random.Random(1224)
        with SessionLocal() as db:
            all_rows = db.query(SeriesDailyGame).join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id).filter(
                LeagueSeries.league_id == lid["big"]).all()
            cutoff_date = sorted({g.calendar_date for g in all_rows})[len(all_rows) // 12 * 3 // 2]
            for g in all_rows:
                if g.calendar_date <= cutoff_date:
                    g.home_score, g.away_score, g.status = rng.randint(0, 6), rng.randint(0, 6), "FINAL"
            db.commit()
        bteams, bseries, bgames = load(lid["big"])
        bexp = brute_standings(bgames, bteams)
        bst = client.get(f"/leagues/{lid['big']}/series-standings").json()["standings"]
        check(all((r["wins"], r["losses"], r["ties"], r["goal_diff"]) ==
                  (bexp[r["team_id"]]["w"], bexp[r["team_id"]]["l"], bexp[r["team_id"]]["t"],
                   bexp[r["team_id"]]["gf"] - bexp[r["team_id"]]["ga"]) for r in bst),
              "12-team standings tie out to the brute-force tally")
        bsc = client.get(f"/leagues/{lid['big']}/game-scores").json()["games"]
        check(len(bsc) == len(all_rows) and all(0 <= g["score"] <= 100 and isinstance(g["score"], int) for g in bsc),
              f"all {len(all_rows)} twelve-team Game Scores (one per window game) are integers within 0-100")
        check(max(g["score"] for g in bsc) > 30 and min(g["score"] for g in bsc) < 15, "scores span a useful range")
        engine.dispose()  # release the SQLite file so the temp directory can be removed

    print("\nAll NHL series standings and Game Score checks passed.")


if __name__ == "__main__":
    main()
