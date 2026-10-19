"""
Acquisition limits on a throwaway SQLite file (acquisition_limits_smoke_test.db): off by default, set at league creation
and in league settings (any of day / week / game / series / season), enforced on free-agent signings and waiver claims.
Checks that each period resets when it should (Pacific day, Sunday-Saturday week, a hockey team's game window and series,
never within a season), that trades-style/AI/commissioner signings are not counted, that AI teams are never limited, that
a claim over the limit fails when decided, and the settings validation.

    .venv-api/Scripts/python.exe scripts/seed_acquisition_limits_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("ACQUISITION_LIMITS_SMOKE_DB", "acquisition_limits_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import acquisition_limits, free_agency, waivers
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    Draft, DraftableAsset, FreeAgencyTransaction, League, LeagueSeries, Player, RealTeam, RosterEntry, SeriesDailyGame, Team, User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

checks = 0


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


def raises(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except ValueError as e:
        return str(e)
    return None


Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()
now = datetime.datetime(2026, 10, 14, 18, 0)          # Wednesday 11:00 Pacific
day = datetime.timedelta(days=1)

nhl, _ = seed_hockey_reference_data(db)
bos = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
db.add(bos)
db.flush()
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "a", "b")}
db.add_all(users.values())
db.commit()
resp = client.post("/sessions", json={"user_id": users["commish"].id})
HDR = {"Authorization": f"Bearer {resp.json()['token']}"}
counter = [0]


def new_league(**extra):
    """A no-waivers league with two person teams, one AI team, a finished draft and 12 free players. Returns (league, A, B, AI, assets)."""
    db.commit()
    counter[0] += 1
    resp = client.post("/leagues", json={"name": f"L{counter[0]}", "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"],
                                         "size": 4, "acquisition_type": "no_waivers", **extra}, headers=HDR)
    assert resp.status_code == 200, resp.text
    lg = db.get(League, resp.json()["id"])
    teams = [Team(league_id=lg.id, name=n, owner_user_id=u, is_ai_owned=u is None)
             for n, u in (("Alpha", users["a"].id), ("Bravo", users["b"].id), ("Robo", None))]
    db.add_all(teams)
    players = [Player(universe_id=nhl.id, real_team_id=bos.id, full_name=f"L{counter[0]} P{i}", position="C") for i in range(12)]
    db.add_all(players)
    db.flush()
    assets = []
    for p in players:
        a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
        db.add(a)
        db.flush()
        assets.append(a.id)
    db.add(Draft(league_id=lg.id, status="complete", completed_at=now - 5 * day))
    db.commit()
    return resp.json(), lg, teams[0], teams[1], teams[2], assets


def sign(team, asset, at, reason="user"):
    return free_agency.add_player(db, team, asset, reason, at)


def fill(team, assets, at, n):
    """Signs n players at `at`; returns the assets used."""
    for a in assets[:n]:
        sign(team, a, at)
    return assets[n:]


# --- off by default ---------------------------------------------------------------------------------------------------------------
out, lg, A, B, AI, p = new_league()
check(out["acquisition_limit"] is None and out["acquisition_limit_period"] == "week", "off by default, period week")
check(lg.acquisition_limit is None and acquisition_limits.problem(db, A, now) is None, "no limit: no problem")
fill(A, p, now, 6)
check(acquisition_limits.summary(db, A, lg, now) is None, "no limit: no summary")

# --- set at creation: per day ----------------------------------------------------------------------------------------------------
out, lg, A, B, AI, p = new_league(acquisition_limit=2, acquisition_limit_period="day")
check(out["acquisition_limit"] == 2 and out["acquisition_limit_period"] == "day", "creation saves the limit and period")
rest = fill(A, p, now, 2)
msg = raises(sign, A, rest[0], now)
check(msg and "limit of 2" in msg and "per day" in msg, f"day: third signing refused ({msg})")
check(acquisition_limits.summary(db, A, lg, now) == {"limit": 2, "period": "day", "used": 2, "left": 0}, "day: summary counts it")
check(acquisition_limits.problem(db, B, now) is None, "day: another team has its own count")
sign(B, rest[0], now)
check(acquisition_limits.problem(db, AI, now) is None, "day: an AI team is never limited")
sign(A, rest[1], now + day)
check(acquisition_limits.used(db, A, now + day) == 1, "day: the next Pacific day starts over")
check(acquisition_limits.used(db, A, now + datetime.timedelta(hours=12)) == 2, "day: 23:00 Pacific is still the same day")

# --- uncounted reasons --------------------------------------------------------------------------------------------------------
out, lg, A, B, AI, p = new_league(acquisition_limit=1, acquisition_limit_period="season")
db.add(RosterEntry(team_id=A.id, draftable_asset_id=p[0], acquired_via="free_agency"))
db.add(FreeAgencyTransaction(league_id=lg.id, team_id=A.id, draftable_asset_id=p[0], kind="add", price=0, reason="ai_fill", created_at=now))
db.add(FreeAgencyTransaction(league_id=lg.id, team_id=A.id, draftable_asset_id=p[1], kind="add", price=0, reason="Traded from Bravo", created_at=now))
db.add(FreeAgencyTransaction(league_id=lg.id, team_id=A.id, draftable_asset_id=p[2], kind="add", price=0, reason="commissioner", created_at=now))
db.flush()
check(acquisition_limits.used(db, A, now) == 0, "AI fill, trades and commissioner signings don't count")
sign(A, p[3], now)
check(raises(sign, A, p[4], now + 100 * day), "season: never resets inside the season")

# --- per week (Sunday - Saturday, Pacific) -----------------------------------------------------------------------------------------
out, lg, A, B, AI, p = new_league(acquisition_limit=2, acquisition_limit_period="week")
fill(A, p, now, 2)                                                      # Wednesday
check(raises(sign, A, p[2], now + 2 * day), "week: still over on Friday")
sat = datetime.datetime(2026, 10, 18, 6, 0)                              # Saturday 23:00 Pacific
check(raises(sign, A, p[2], sat), "week: still over late Saturday night")
sign(A, p[2], datetime.datetime(2026, 10, 18, 9, 0))                    # Sunday 01:00-02:00 Pacific (works with or without tzdata)
check(acquisition_limits.used(db, A, datetime.datetime(2026, 10, 18, 9, 0)) == 1, "week: starts over at midnight Pacific Sunday")

# --- game / series for a hockey league ----------------------------------------------------------------------------------------------
out, lg, A, B, AI, p = new_league(acquisition_limit=1, acquisition_limit_period="game")
lg.series_length_days = 7
s1 = LeagueSeries(league_id=lg.id, team_a_id=A.id, team_b_id=B.id, start_date=datetime.date(2026, 10, 11), end_date=datetime.date(2026, 10, 17),
                  total_days=7)
s2 = LeagueSeries(league_id=lg.id, team_a_id=A.id, team_b_id=B.id, start_date=datetime.date(2026, 10, 18), end_date=datetime.date(2026, 10, 24),
                  total_days=7)
db.add_all([s1, s2])
db.flush()
D = datetime.date
for s, windows in ((s1, ((1, D(2026, 10, 11), D(2026, 10, 13)), (2, D(2026, 10, 14), D(2026, 10, 15)), (3, D(2026, 10, 16), D(2026, 10, 17)))),
                   (s2, ((1, D(2026, 10, 18), D(2026, 10, 20)),))):
    for idx, start, end in windows:
        db.add(SeriesDailyGame(series_id=s.id, calendar_date=start, calendar_date_end=end, window_index=idx,
                               home_team_id=A.id, away_team_id=B.id))
db.flush()
sign(A, p[0], now)                                                       # window 2 (Oct 14-15)
check(raises(sign, A, p[1], now + day), "game: still the same window on Oct 15")
sign(A, p[1], now + 2 * day)                                             # window 3 (Oct 16-17)
check(acquisition_limits.used(db, A, now + 2 * day) == 1, "game: a new window starts over")
check(raises(sign, A, p[2], now + 2 * day), "game: limit applies in the new window")
# between games: the days after a window end belong to the next game
check(acquisition_limits.used(db, A, datetime.datetime(2026, 10, 17, 20, 0)) == 1, "game: counted inside its window")
lg.acquisition_limit_period = "series"
db.flush()
check(acquisition_limits.used(db, A, now + 2 * day) == 2 and raises(sign, A, p[2], now + 2 * day), "series: both signings are in the same series")
check(acquisition_limits.used(db, A, datetime.datetime(2026, 10, 19, 18, 0)) == 0, "series: the next series starts over")
sign(A, p[2], datetime.datetime(2026, 10, 19, 18, 0))

# --- other sports: game and series fall back to the week ------------------------------------------------------------------------------
out, lg, A, B, AI, p = new_league(acquisition_limit=1, acquisition_limit_period="series")
lg.series_length_days = None
db.flush()
sign(A, p[0], now)
check(raises(sign, A, p[1], now + day), "no series schedule: series is the week")
check(acquisition_limits.used(db, A, datetime.datetime(2026, 10, 19, 18, 0)) == 0, "no series schedule: next week starts over")

# --- waiver claims ----------------------------------------------------------------------------------------------------------------
out, lg, A, B, AI, p = new_league(acquisition_limit=1, acquisition_limit_period="week")
lg.acquisition_type = "waivers"
db.flush()
for t, a in ((B, p[0]), (B, p[1])):                                      # B drops two players: both are held 24 hours
    db.add(RosterEntry(team_id=t.id, draftable_asset_id=a, acquired_via="draft"))
    db.flush()
    free_agency.drop_player(db, t, a, "user", now)
c1 = waivers.place_claim(db, A, p[0], 0, now)
c2 = waivers.place_claim(db, A, p[1], 0, now)                            # nothing won yet, so both can be placed
waivers.process(db, lg.id, now + datetime.timedelta(hours=25))
check(c1.status == "won", "claims: the first is won")
check(c2.status == "failed" and "limit of 1" in (c2.note or ""), f"claims: the second fails over the limit ({c2.note})")
check(raises(waivers.place_claim, db, A, p[2], 0, now + 2 * day) is not None, "claims: a new claim is refused once at the limit")

# --- settings -----------------------------------------------------------------------------------------------------------------------
out, lg, A, B, AI, p = new_league()
url = f"/leagues/{lg.id}/settings"
r = client.patch(url, json={"acquisition_limit": 3, "acquisition_limit_period": "series"}, headers=HDR)
check(r.status_code == 200 and r.json()["acquisition_limit"] == 3 and r.json()["acquisition_limit_period"] == "series", f"settings: set ({r.text[:200]})")
r = client.patch(url, json={"acquisition_limit_period": "game"}, headers=HDR)
check(r.status_code == 200 and r.json()["acquisition_limit"] == 3 and r.json()["acquisition_limit_period"] == "game", "settings: period alone changes only the period")
for period in ("day", "week", "season"):
    r = client.patch(url, json={"acquisition_limit_period": period}, headers=HDR)
    check(r.status_code == 200 and r.json()["acquisition_limit_period"] == period, f"settings: {period} accepted")
r = client.patch(url, json={"acquisition_limit": None}, headers=HDR)
check(r.status_code == 200 and r.json()["acquisition_limit"] is None, "settings: null turns the limit off")
check(client.patch(url, json={"acquisition_limit": 0}, headers=HDR).status_code == 422, "settings: 0 is refused (off is null)")
check(client.patch(url, json={"acquisition_limit_period": "month"}, headers=HDR).status_code == 422, "settings: unknown period refused")
check(client.patch(url, json={"acquisition_limit": 2}, headers={"Authorization": "Bearer nope"}).status_code in (401, 403), "settings: commissioner only")
bad = client.post("/leagues", json={"name": "bad", "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"], "size": 4,
                                    "acquisition_limit": 0}, headers=HDR)
check(bad.status_code == 422, "creation: a limit of 0 is refused")

print(f"OK - {checks} checks passed")
