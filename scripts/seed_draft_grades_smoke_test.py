"""
Projected points, draft grades and power rankings (NHL), on a throwaway SQLite file (draft_grades_smoke_test.db):
the aging curves and the projection math (regression to the pool, aging both ways, no-history prior, games played from
ice time), the lineup value with depth cover, the grade maths (z-scores, letters, pick value against the board), and
GET /leagues/{id}/draft-grades and /power-rankings through the real HTTP API (FastAPI TestClient).

    .venv-api/Scripts/python.exe scripts/seed_draft_grades_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("DRAFT_GRADES_SMOKE_DB", "draft_grades_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists() and not os.environ.get("DRAFT_GRADES_KEEP_DB"):      # DRAFT_GRADES_KEEP_DB=1 keeps the data to look at in a browser
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import draft_grades, hockey_projection as hp
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    Draft, DraftPick, DraftableAsset, Game, Player, RealTeam, RosterEntry, Team, User, Week,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.draft_stats import nhl_season_year
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import PlayerGameToi, Shot, ShotCredit

checks = 0


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


# ---- pure: aging curves ----------------------------------------------------------------------------------------------
for grp in hp.CURVE_GROUPS:
    curve = hp.AGE_CURVES[grp]
    peak_age = max(curve, key=lambda a: curve[a])
    check(curve[peak_age] == 1.0, f"{grp} curve peaks at exactly 1.0")
    ages = sorted(curve)
    check(all(curve[a] <= curve[a + 1] for a in ages if a < peak_age), f"{grp} rises to its peak")
    check(all(curve[a] >= curve[a + 1] for a in ages if a >= peak_age and a + 1 in curve), f"{grp} falls after its peak")
check(hp.aging_factor("F", None) == 1.0, "unknown age: no adjustment")
check(hp.aging_factor("F", 12) == hp.AGE_CURVES["F"][18] and hp.aging_factor("F", 55) == hp.AGE_CURVES["F"][40], "ages clamp to the table")
check(max(hp.AGE_CURVES["D"], key=lambda a: hp.AGE_CURVES["D"][a]) >= max(hp.AGE_CURVES["F"], key=lambda a: hp.AGE_CURVES["F"][a]),
      "defensemen peak no earlier than forwards")
check(max(hp.AGE_CURVES["G"], key=lambda a: hp.AGE_CURVES["G"][a]) > 26, "goalies peak later")
check(hp.curve_group("LW") == "F" and hp.curve_group("C") == "F" and hp.curve_group("D") == "D" and hp.curve_group("G") == "G"
      and hp.curve_group("QB") is None and hp.curve_group(None) is None, "curve groups")

# ---- pure: projection math -------------------------------------------------------------------------------------------
T = 2026
born_25 = datetime.date(T - 25, 1, 1)
priors = hp.PoolPriors(rate={"F": 0.30, "D": 0.20, "G": 0.40}, gp={"F": 70.0, "D": 70.0, "G": 45.0})
full = lambda rate, season=T - 1, games=82: hp.SeasonLine(season, games, rate * games)   # noqa: E731

star = hp.project_player(1, "C", born_25, [full(0.60)], priors, T)
check(star.source == "history" and star.seasons_used == 1, "history projection")
check(priors.rate["F"] < star.rate < 0.60, "a hot season is regressed toward the pool average, not kept whole")
more = hp.project_player(1, "C", born_25, [full(0.60, T - 1), full(0.60, T - 2), full(0.60, T - 3)], priors, T)
check(more.rate > star.rate, "three seasons of the same rate are trusted more than one")
few = hp.project_player(1, "C", born_25, [hp.SeasonLine(T - 1, 8, 8 * 0.60)], priors, T)
check(few.rate < star.rate, "8 games of the same rate are trusted less than 82")
check(few.games < star.games, "and project fewer games")
check(star.games <= hp.TEAM_GAMES, "games never exceed the schedule")
old = hp.project_player(2, "C", datetime.date(T - 36, 1, 1), [full(0.40)], priors, T)
young = hp.project_player(3, "C", datetime.date(T - 24, 1, 1), [full(0.40)], priors, T)
check(young.age == 24 and old.age == 36, "ages computed for the target season")
check(old.rate < young.rate, "the 36-year-old with the same history projects lower than the 24-year-old")
unk = hp.project_player(6, "C", None, [full(0.40)], priors, T)
check(unk.age is None and unk.rate > 0, "unknown birth date still projects")
rook = hp.project_player(7, "W", datetime.date(T - 19, 6, 1), [], priors, T)
check(rook.source == "prior" and rook.seasons_used == 0, "no history: prior")
check(rook.rate < priors.rate["F"] and rook.games < priors.gp["F"], "a no-history player is projected below the pool average")
check(hp.project_player(8, "QB", None, [], priors, T) is None, "a non-hockey position gets no projection")
check(hp.project_player(9, "C", None, [full(0.5)], hp.PoolPriors(), T) is None, "no pool prior: no projection, never an invented number")
check(hp.project_player(10, "C", None, [full(0.5, season=T - 5)], priors, T).source == "prior", "seasons outside the window don't count")
pp = hp.pool_priors([("F", None, [hp.SeasonLine(T - 1, 80, 24.0), hp.SeasonLine(T - 1, 40, 6.0), hp.SeasonLine(T - 1, 5, 99.0)])], T)
check(abs(pp.rate["F"] - (24 + 6 + 99) / 125) < 1e-9, "pool rate is games-weighted")
check(abs(pp.gp["F"] - 60.0) < 1e-9, "pool games ignore tiny seasons")

# ---- pure: lineup with depth cover -----------------------------------------------------------------------------------
mk = lambda aid, grp, games, rate: hp.Projection(aid, grp, 25, games, rate, games * rate, "history", 1)   # noqa: E731
lu = hp.lineup_projection([mk(1, "C", 82, 0.5), mk(2, "C", 82, 0.3), mk(3, "W", 82, 0.4), mk(4, "W", 82, 0.4), mk(5, "W", 82, 0.1),
                           mk(6, "D", 82, 0.2), mk(7, "D", 82, 0.2), mk(8, "G", 82, 0.6)])
check(abs(lu.starters_points - 82 * (0.5 + 0.4 + 0.4 + 0.2 + 0.2 + 0.6)) < 1e-9 and lu.depth_points == 0, "full-time starters need no cover")
lu = hp.lineup_projection([mk(1, "G", 60, 0.6), mk(2, "G", 40, 0.4)])
check(abs(lu.depth_points - 22 * 0.4) < 1e-9, "the backup covers the starter's 22 missed games")
lu = hp.lineup_projection([mk(1, "G", 60, 0.6), mk(2, "G", 10, 0.4)])
check(abs(lu.depth_points - 10 * 0.4) < 1e-9, "cover is capped by the backup's own games")
lu = hp.lineup_projection([mk(1, "C", 82, 0.5)])
check(lu.total == lu.starters_points == 82 * 0.5 and len(lu.starters) == 1, "missing slots add nothing")

# ---- pure: grade maths -----------------------------------------------------------------------------------------------
check(draft_grades.letter_for(2.0) == "A+" and draft_grades.letter_for(0.0) == "B-" and draft_grades.letter_for(-3.0) == "F", "letters")
z = draft_grades.zscores([1.0, 2.0, 3.0])
check(abs(sum(z)) < 1e-9 and z[0] < 0 < z[2], "z-scores centre on zero")
check(draft_grades.zscores([5.0, 5.0]) == [0.0, 0.0], "equal values: no spread, no z")
exp = draft_grades.expected_by_pick([10, 9, 8, 7, 6, 5], 8, 1)
check(abs(exp[0] - 9.5) < 1e-9 and abs(exp[1] - 9.0) < 1e-9 and exp[7] == exp[5], "expected-by-pick smooths the board and repeats its end")
check(draft_grades.expected_by_pick([], 3, 2) == [0.0, 0.0, 0.0], "no board expects nothing")

# ---- database + API --------------------------------------------------------------------------------------------------
Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()
nhl, _ = seed_hockey_reference_data(db)
rt = [RealTeam(universe_id=nhl.id, name=f"Team {i}", external_ref=f"T{i}") for i in range(2)]
db.add_all(rt)
db.flush()

target = nhl_season_year()
seasons = [target - 1, target - 2, target - 3]
weeks = {}
games_by_season = {}
for s in seasons + [target]:
    w = Week(universe_id=nhl.id, season_year=s, week_number=1)
    db.add(w)
    db.flush()
    weeks[s] = w
    gl = [Game(universe_id=nhl.id, week_id=w.id, home_team_id=rt[0].id, away_team_id=rt[1].id, status="final") for _ in range(20)]
    db.add_all(gl)
    db.flush()
    games_by_season[s] = [g.id for g in gl]

# 24 players, best first; positions cycle so every block of six has C W W D D G. Quality falls 0.60 -> 0.14 points per game.
POS = ["C", "W", "W", "D", "D", "G"]
players, assets = [], []
for i in range(24):
    p = Player(universe_id=nhl.id, real_team_id=rt[i % 2].id, full_name=f"Player {i:02d}", position=POS[i % 6],
               birth_date=datetime.date(target - 26, 3, 1), external_ref=f"P{i}")
    db.add(p)
    db.flush()
    a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
    db.add(a)
    db.flush()
    players.append(p)
    assets.append(a)
    rate = 0.60 - 0.02 * i
    for s in seasons:
        for n, gid in enumerate(games_by_season[s]):
            shot = Shot(game_id=gid, source="test", external_shot_id=f"{a.id}-{s}-{n}", shooter_asset_id=a.id)
            db.add(shot)
            db.flush()
            db.add(ShotCredit(shot_id=shot.id, draftable_asset_id=a.id, role="shooter", credit_points=rate))
# a rookie with no history, and a player credited only this (unfinished) season, who must not count
rookie = Player(universe_id=nhl.id, real_team_id=rt[0].id, full_name="Rookie Nobody", position="C", birth_date=datetime.date(target - 19, 5, 1), external_ref="R1")
db.add(rookie)
db.flush()
rookie_asset = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=rookie.id)
db.add(rookie_asset)
db.flush()
shot = Shot(game_id=games_by_season[target][0], source="test", external_shot_id="rk-now", shooter_asset_id=rookie_asset.id)
db.add(shot)
db.flush()
db.add(ShotCredit(shot_id=shot.id, draftable_asset_id=rookie_asset.id, role="shooter", credit_points=9.0))
# ice time with no credit still counts as a game played: player 23 skated in one more game last season
extra = Game(universe_id=nhl.id, week_id=weeks[target - 1].id, home_team_id=rt[0].id, away_team_id=rt[1].id, status="final")
db.add(extra)
db.flush()
db.add(PlayerGameToi(game_id=extra.id, draftable_asset_id=assets[23].id, toi_seconds=900))
db.add(PlayerGameToi(game_id=extra.id, draftable_asset_id=assets[22].id, toi_seconds=0))       # dressed, never played
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish",)}
db.add_all(users.values())
db.commit()

hist = hp.load_history(db, target)
check({ln.season for ln in hist[assets[0].id]} == set(seasons), "history reads the three completed seasons")
check(rookie_asset.id not in hist, "the current, unfinished season is not history")
line = {ln.season: ln for ln in hist[assets[23].id]}[target - 1]
check(line.games == 21, "a game with ice time but no credit counts as played")
line22 = {ln.season: ln for ln in hist[assets[22].id]}[target - 1]
check(line22.games == 20, "a dressed player with 0 seconds did not play")
check(abs({ln.season: ln for ln in hist[assets[0].id]}[target - 1].points - 20 * 0.60) < 1e-9, "points are the stored credit")

proj = hp.project_pool(db)
check(len(proj) == 25, "every active NHL skater and goalie is projected")
check(proj[rookie_asset.id].source == "prior" and proj[assets[0].id].source == "history", "rookie by prior, veterans by history")
check(proj[assets[0].id].points > proj[assets[12].id].points > proj[assets[23].id].points, "better history, more projected points")
check(proj[assets[0].id].games <= hp.TEAM_GAMES, "games within the schedule")

resp = client.post("/sessions", json={"user_id": users["commish"].id})
hdr = {"Authorization": f"Bearer {resp.json()['token']}"}
resp = client.post("/leagues", json={"name": "Grades", "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"], "size": 4},
                   headers=hdr)
assert resp.status_code == 200, resp.text
lid = resp.json()["id"]
teams = [Team(league_id=lid, name=n, owner_user_id=None, is_ai_owned=True) for n in ("Alpha", "Bravo", "Charlie", "Delta")]
db.add_all(teams)
db.commit()
tid = [t.id for t in teams]

r = client.get(f"/leagues/{lid}/draft-grades")
check(r.status_code == 200 and r.json()["available"] is False and "draft" in r.json()["reason"].lower(), "no draft yet: says so")
check(client.get("/leagues/99999/draft-grades").status_code == 404, "unknown league: 404")

# Snake draft over 6 rounds. Pick n takes player n-1, except Delta reaches at pick 1 (player 10) and the best player falls to pick 11.
draft = Draft(league_id=lid, status="complete", is_snake=True)
db.add(draft)
db.flush()
order = list(range(24))
order[0], order[10] = 10, 0
owner = {}
for n in range(1, 25):
    rnd = (n - 1) // 4 + 1
    slot = (n - 1) % 4
    team = tid[slot] if rnd % 2 == 1 else tid[3 - slot]
    owner[n] = team
    db.add(DraftPick(draft_id=draft.id, round=rnd, pick_number=n, team_id=team, draftable_asset_id=assets[order[n - 1]].id))
    db.add(RosterEntry(team_id=team, draftable_asset_id=assets[order[n - 1]].id, acquired_via="draft"))
db.commit()

g = client.get(f"/leagues/{lid}/draft-grades").json()
check(g["available"] and g["draft_id"] == draft.id and len(g["teams"]) == 4 and len(g["picks"]) == 24, "grades for a completed draft")
check([t["rank"] for t in g["teams"]] == [1, 2, 3, 4], "teams ranked 1..4")
check(all(t["grade"] in {l for _, l in draft_grades.LETTERS} | {"F"} for t in g["teams"]), "every team has a letter")
check(abs(sum(t["score"] for t in g["teams"])) < 0.01, "scores centre on zero across the league")
by_pick = {p["pick_number"]: p for p in g["picks"]}
check(by_pick[1]["value"] < 0, "reaching for a worse player at pick 1 is a negative value")
check(by_pick[11]["value"] > 0, "the best player falling to pick 11 is a positive value")
check(by_pick[11]["name"] == "Player 00" and by_pick[1]["name"] == "Player 10", "pick rows carry the player")
check(abs(by_pick[1]["value"] - (by_pick[1]["projected_points"] - by_pick[1]["expected_points"])) < 0.11, "value = projected - expected")
alpha = next(t for t in g["teams"] if t["team_id"] == tid[0])
# independent of the projection code: each team's lineup quality straight from the players' true rates (player i = 0.60 - 0.02 i)
need = {"C": 1, "W": 2, "D": 2, "G": 1}
quality = {t: 0.0 for t in tid}
for t in tid:
    mine = [order[n - 1] for n in owner if owner[n] == t]
    for grp, k in need.items():
        quality[t] += sum(sorted((0.60 - 0.02 * i for i in mine if hp.GROUP_BY_POSITION[POS[i % 6]] == grp), reverse=True)[:k])
by_quality = sorted(tid, key=lambda t: -quality[t])
by_haul = [t["team_id"] for t in sorted(g["teams"], key=lambda t: -t["haul_points"])]
check(by_haul[-1] == by_quality[-1] and by_haul[0] != by_quality[-1],
      "the weakest lineup (by true quality) has the lowest haul; depth cover can reorder the teams above it")
check(g["teams"][0]["score"] >= g["teams"][-1]["score"], "best first")
check(11 in next(t for t in g["teams"] if t["team_id"] == owner[11])["best_picks"], "the steal is among its team's best picks")

pr = client.get(f"/leagues/{lid}/power-rankings").json()
check(pr["available"] and [t["rank"] for t in pr["teams"]] == [1, 2, 3, 4], "power rankings for every team")
check(pr["teams"][0]["projected_points"] >= pr["teams"][-1]["projected_points"], "ranked by projected points")
check({t["team_id"]: t["projected_points"] for t in pr["teams"]}[tid[0]] == alpha["haul_points"],
      "right after the draft, rosters = picks: power ranking equals the drafted haul")
top = pr["teams"][0]
counts = {}
for st in top["starters"]:
    counts[st["group"]] = counts.get(st["group"], 0) + 1
check(counts and all(counts[k] <= hp.STARTERS[k] for k in counts), "never more starters than 1 C, 2 W, 2 D, 1 G")

# trading changes rosters, not the draft grade: move the top team's best player to the last-ranked team
last = pr["teams"][-1]["team_id"]
best = db.query(RosterEntry).filter_by(team_id=top["team_id"], draftable_asset_id=top["starters"][0]["asset_id"]).one()
best.team_id = last
db.commit()
pr2 = client.get(f"/leagues/{lid}/power-rankings").json()
check(pr2["teams"][0]["team_id"] != top["team_id"] or pr2["teams"][0]["projected_points"] != top["projected_points"], "a trade moves the power rankings")
check(client.get(f"/leagues/{lid}/draft-grades").json()["teams"] == g["teams"], "but never the draft grade")

# ---- pick suggestions: a second league with a draft in progress ------------------------------------------------------
check(draft_grades.needed_groups({}) == ["C", "W", "D", "G"], "empty roster needs every group")
check(draft_grades.needed_groups({"C": 1, "W": 2, "D": 2}) == ["G"], "starters first: only the goalie is missing")
check(draft_grades.needed_groups({"C": 1, "W": 2, "D": 2, "G": 1}) == ["C", "W", "D", "G"], "starters full: depth is the need")
check(draft_grades.needed_groups({"C": 4, "W": 8, "D": 8, "G": 4}) == ["C", "W", "D", "G"], "everything full: every group")
check(draft_grades.needed_groups({"C": 0, "W": 8, "D": 8, "G": 4}) == ["C"], "short starters win over depth")
check(draft_grades.needed_groups({"C": 4, "W": 8, "D": 2, "G": 1}) == ["D", "G"], "depth need lists every short group")

resp = client.post("/leagues", json={"name": "Live", "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"], "size": 4}, headers=hdr)
lid2 = resp.json()["id"]
teams2 = [Team(league_id=lid2, name=n, owner_user_id=None, is_ai_owned=True) for n in ("W1", "W2", "W3", "W4")]
db.add_all(teams2)
db.flush()
draft2 = Draft(league_id=lid2, status="in_progress", is_snake=True)
db.add(draft2)
db.flush()
for n in range(1, 9):
    db.add(DraftPick(draft_id=draft2.id, round=(n - 1) // 4 + 1, pick_number=n, team_id=teams2[(n - 1) % 4].id))
db.commit()
t0 = teams2[0].id
rate = lambda i: 0.60 - 0.02 * i      # noqa: E731

r = client.get(f"/drafts/{draft2.id}/suggestions").json()
top3 = sorted(proj, key=lambda a: -proj[a].points)[:3]
check(r["available"] and r["pick_number"] == 1 and r["team_id"] == t0 and len(r["suggestions"]) == 3, "suggestions for the pick on the clock")
check([x["asset_id"] for x in r["suggestions"]] == top3, "an empty roster needs everything: the three best projected players")
check([x["value"] for x in r["suggestions"]] == sorted((x["value"] for x in r["suggestions"]), reverse=True), "best value first")
check(all(abs(x["value"] - (x["projected_points"] - x["expected_points"])) < 0.11 for x in r["suggestions"]), "value = projected - expected")
check(all("name" in x and "position" in x for x in r["suggestions"]), "suggestions carry the player")

db.add_all([RosterEntry(team_id=t0, draftable_asset_id=assets[0].id, acquired_via="draft"),        # C
            RosterEntry(team_id=t0, draftable_asset_id=assets[5].id, acquired_via="draft")])       # G
db.commit()
r = client.get(f"/drafts/{draft2.id}/suggestions").json()
check(r["needs"] == ["W", "D"], "a team with its C and G needs wingers and defensemen")
check({x["group"] for x in r["suggestions"]} <= {"W", "D"}, "only players in a position of need")
check(not {assets[0].id, assets[5].id} & {x["asset_id"] for x in r["suggestions"]}, "rostered players are never suggested")
best_need = [assets[i].id for i in range(24) if POS[i % 6] in ("W", "D")][:3]
check([x["asset_id"] for x in r["suggestions"]] == best_need, "the three best wingers/defensemen by projection")
other = client.get(f"/drafts/{draft2.id}/suggestions?team_id={teams2[1].id}").json()
check(other["needs"] == ["C", "W", "D", "G"] and other["team_id"] == teams2[1].id, "another team's needs on request")

db.add_all([RosterEntry(team_id=t0, draftable_asset_id=assets[i].id, acquired_via="draft") for i in (1, 2, 3, 4)])    # W W D D
db.commit()
r = client.get(f"/drafts/{draft2.id}/suggestions").json()
check(r["roster_counts"] == {"C": 1, "G": 1, "W": 2, "D": 2} and r["needs"] == ["C", "W", "D", "G"], "starters full: depth is the need")
check(client.get("/drafts/99999/suggestions").status_code == 404, "unknown draft: 404")
done = client.get(f"/drafts/{draft.id}/suggestions").json()
check(done["available"] is False and "open pick" in done["reason"], "a finished draft has nothing to suggest")

if os.environ.get("DRAFT_GRADES_KEEP_DB"):
    print(f"kept {db_path} (league {lid})")
    sys.exit(0)

# a pool with no history projects nobody and says why
db.query(ShotCredit).delete()
db.query(PlayerGameToi).delete()
db.commit()
r = client.get(f"/leagues/{lid}/power-rankings").json()
check(r["available"] is False and "history" in r["reason"], "no stored history: unavailable, with a reason")
check(client.get(f"/leagues/{lid}/draft-grades").json()["available"] is False, "same for draft grades")

print(f"draft grades smoke test OK ({checks} checks)")
