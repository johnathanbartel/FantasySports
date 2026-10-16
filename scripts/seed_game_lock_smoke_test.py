"""Offline check of the game-start lock (platform/game_lock.py): kickoff normalisation and the lock window.
No database, no network. Run with .venv-api."""

import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fantasy_sports.platform import game_lock as gl  # noqa: E402

D = datetime.datetime


def check(label, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + label + ("" if ok else f": got {got}, want {want}"))
    return ok


def game(k):
    return SimpleNamespace(kickoff_at=k)


results = [
    # US Eastern: EDT in October, EST in January
    check("EDT to UTC", gl.eastern_to_utc(D(2026, 10, 15, 19)), D(2026, 10, 15, 23)),
    check("EST to UTC", gl.eastern_to_utc(D(2026, 1, 15, 19)), D(2026, 1, 16, 0)),
    check("UTC to EDT", gl.utc_to_eastern(D(2026, 10, 15, 23)), D(2026, 10, 15, 19)),
    check("DST starts 2026-03-08", gl.eastern_offset(D(2026, 3, 8, 3)), datetime.timedelta(hours=4)),
    check("DST ends 2026-11-01", gl.eastern_offset(D(2026, 11, 1, 3)), datetime.timedelta(hours=5)),
    # NHL rows: Eastern wall clock, or UTC when the hour is under 5
    check("nhl eastern row", gl.kickoff_utc(game(D(2026, 10, 15, 19)), "nhl"), D(2026, 10, 15, 23)),
    check("nhl utc row", gl.kickoff_utc(game(D(2026, 10, 15, 23, 30)), "nhl") is not None, True),
    check("nhl utc-stored row", gl.kickoff_utc(game(D(2026, 10, 16, 0, 0)), "nhl"), D(2026, 10, 16, 0, 0)),
    # college is UTC; a date-only (midnight) kickoff counts from the start of the day, Eastern
    check("college utc", gl.kickoff_utc(game(D(2026, 10, 17, 16, 0)), "ncaaf"), D(2026, 10, 17, 16, 0)),
    check("nfl date-only", gl.kickoff_utc(game(D(2026, 10, 18)), "nfl"), D(2026, 10, 18, 4)),
    check("no kickoff", gl.kickoff_utc(game(None), "nfl"), None),
]

# week_over uses the Eastern calendar day
wk = SimpleNamespace(start_date=datetime.date(2026, 10, 12), end_date=datetime.date(2026, 10, 18))
results += [
    check("week not over on its last evening", gl.week_over(wk, D(2026, 10, 18, 23)), False),
    check("week over after midnight Eastern", gl.week_over(wk, D(2026, 10, 19, 5)), True),
]


# ---- per-player lock, against an in-memory database -------------------------------------------------------------
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from fantasy_sports.platform.db.base import Base  # noqa: E402
from fantasy_sports.platform.db.models.core import (  # noqa: E402
    DraftableAsset, Game, League, LeagueUniverse, Player, RealTeam, Universe, User, Week,
)
from fantasy_sports.sports.football.models import Play, PlayCredit, WeeklyPlayerScore  # noqa: F401,E402

engine = create_engine("sqlite://")
Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()


def universe(code):
    u = Universe(sport="x", level="pro", code=code, display_name=code)
    db.add(u)
    db.flush()
    return u


def team_and_asset(u, name):
    t = RealTeam(universe_id=u.id, name=name)
    db.add(t)
    db.flush()
    pl = Player(universe_id=u.id, real_team_id=t.id, full_name=name + " P", position="C")
    db.add(pl)
    db.flush()
    a = DraftableAsset(universe_id=u.id, asset_type="player", player_id=pl.id)
    db.add(a)
    db.flush()
    return t, a


user = User(email="a@b.c", display_name="a")
db.add(user)
db.flush()
league = League(name="L", commissioner_user_id=user.id, size=2, draft_rounds=1)
db.add(league)
db.flush()

# Football: a Thursday game has started, the Sunday one has not
nfl = universe("nfl")
db.add(LeagueUniverse(league_id=league.id, universe_id=nfl.id, defense_mode="group"))
t1, a_thu = team_and_asset(nfl, "Thu")
t2, a_thu_opp = team_and_asset(nfl, "ThuOpp")
t3, a_sun = team_and_asset(nfl, "Sun")
t4, a_sun_opp = team_and_asset(nfl, "SunOpp")
t5, a_bye = team_and_asset(nfl, "Bye")
wk = Week(universe_id=nfl.id, season_year=2026, week_number=7, start_date=datetime.date(2026, 10, 15),
          end_date=datetime.date(2026, 10, 19))
db.add(wk)
db.flush()
db.add_all([Game(universe_id=nfl.id, week_id=wk.id, home_team_id=t1.id, away_team_id=t2.id, kickoff_at=D(2026, 10, 16, 0, 20)),
            Game(universe_id=nfl.id, week_id=wk.id, home_team_id=t3.id, away_team_id=t4.id, kickoff_at=D(2026, 10, 18, 17, 0))])
db.flush()
now = D(2026, 10, 16, 3)       # Thursday 11pm Eastern: the Thursday game is under way
locked = gl.locked_assets(db, league.id, now)
results += [
    check("football: the started game's players lock", {a_thu.id, a_thu_opp.id} <= locked, True),
    check("football: a later game's players stay open", locked & {a_sun.id, a_sun_opp.id}, set()),
    check("football: a bye player stays open", a_bye.id in locked, False),
    check("football: all open before any kickoff", gl.locked_assets(db, league.id, D(2026, 10, 15, 12)), set()),
    check("football: roster moves reopen after the week", gl.locked_assets(db, league.id, D(2026, 10, 21, 12)), set()),
    check("football: lineups are locked for everyone once the week is over",
          gl.locked_assets_for_week(db, wk, D(2026, 10, 21, 12)), None),
]
try:
    gl.assert_unlocked(db, league.id, [a_sun.id], now)
    gl.assert_unlocked(db, league.id, [a_thu.id], now)
    results.append(check("assert_unlocked refuses a locked player", False, True))
except ValueError as e:
    results.append(check("assert_unlocked refuses a locked player", "locked" in str(e), True))

# Hockey: window 2 is Wed-Thu. Player X has Wed and Thu games; Wed has started, so he is locked even though the
# Thursday game is the one that would count. Player Y plays only Thursday and stays open.
league2 = League(name="H", commissioner_user_id=user.id, size=2, draft_rounds=1)
db.add(league2)
db.flush()
nhl = universe("nhl")
db.add(LeagueUniverse(league_id=league2.id, universe_id=nhl.id, defense_mode="group"))
hx, a_x = team_and_asset(nhl, "X")
hy, a_y = team_and_asset(nhl, "Y")
ho, _ = team_and_asset(nhl, "Opp")
hw = Week(universe_id=nhl.id, season_year=2026, week_number=1, start_date=datetime.date(2026, 10, 11),
          end_date=datetime.date(2026, 10, 17))
db.add(hw)
db.flush()
db.add_all([
    Game(universe_id=nhl.id, week_id=hw.id, home_team_id=hx.id, away_team_id=ho.id, kickoff_at=D(2026, 10, 14, 19, 0)),   # Wed 7pm ET
    Game(universe_id=nhl.id, week_id=hw.id, home_team_id=ho.id, away_team_id=hx.id, kickoff_at=D(2026, 10, 15, 19, 0)),   # Thu 7pm ET
    Game(universe_id=nhl.id, week_id=hw.id, home_team_id=hy.id, away_team_id=ho.id, kickoff_at=D(2026, 10, 15, 19, 30)),  # Thu
])
db.flush()
results += [
    check("hockey: first game of the window locks the player", gl.locked_assets(db, league2.id, D(2026, 10, 14, 23, 30)) >= {a_x.id}, True),
    check("hockey: a player with only a later game stays open", a_y.id in gl.locked_assets(db, league2.id, D(2026, 10, 14, 23, 30)), False),
    check("hockey: he stays locked after his second game", a_x.id in gl.locked_assets(db, league2.id, D(2026, 10, 15, 21, 0)), True),
    check("hockey: nobody is locked the next window (Fri)", gl.locked_assets(db, league2.id, D(2026, 10, 16, 14, 0)), set()),
    check("hockey: nor in the gap before Friday's games", a_x.id in gl.locked_assets(db, league2.id, D(2026, 10, 16, 12, 0)), False),
]

if not all(results):
    sys.exit("game-lock smoke test FAILED")
print("game-lock smoke test passed")
