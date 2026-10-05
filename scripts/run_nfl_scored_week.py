"""
Builds an NFL league on a throwaway database, runs its snake draft and one scored week through the real API and
scoring code, then runs scripts/audit_scored_week.py on it. This is the NFL scale's end-to-end realism check.

The picks and lineups come from a plan made WITHOUT looking at the audited week (prior-season credit, the same
drafter model the scale was fitted on): src/fantasy_sports/sports/football/professional/scripts/plan_audit_league.py.

Prepare once (data-pipeline env, needs the network; never point these at a database you care about):
    $env:DATABASE_URL = "sqlite:///./nfl_score_run.db"
    .venv-api/Scripts/python.exe -c "import sys; sys.path[:0]=['src']; from fantasy_sports.platform.db.base import Base; from fantasy_sports.platform.db.session import engine; from fantasy_sports.platform.db.models import core; from fantasy_sports.sports.football import models; Base.metadata.create_all(engine)"
    .venv-api/Scripts/python.exe scripts/seed_nfl_universe.py
    .venv/Scripts/python.exe scripts/ingest_nfl_rosters_and_stats.py --season 2024 --week 1
    .venv/Scripts/python.exe scripts/ingest_nfl_week_scores.py --season 2024 --week 1
    .venv/Scripts/python.exe src/fantasy_sports/sports/football/professional/scripts/build_weekly_credits.py --years 2023 2024
    .venv/Scripts/python.exe src/fantasy_sports/sports/football/professional/scripts/plan_audit_league.py --mode group --season 2024

Then (API env; writes nfl_score_run.db, adding one league per run):
    .venv-api/Scripts/python.exe scripts/run_nfl_scored_week.py --mode group --week 1
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
ap = argparse.ArgumentParser()
ap.add_argument("--db", default="nfl_score_run.db")
ap.add_argument("--mode", choices=["group", "team", "individual"], default="group")
ap.add_argument("--week", type=int, default=1)
ap.add_argument("--teams", type=int, default=12)
args = ap.parse_args()
db_path = (ROOT / args.db).resolve()
if not db_path.exists():
    sys.exit(f"{db_path} missing - see the preparation steps in this file's docstring")
os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import lineups
from fantasy_sports.platform.db.models import core  # noqa: F401
from fantasy_sports.platform.db.models.core import DefenseGroup, DraftableAsset, Player, RealTeam, Team, User
from fantasy_sports.platform.db.session import SessionLocal


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    plan_path = ROOT / "data" / "cache" / f"nfl_audit_league_{args.mode}.json"
    check(plan_path.exists(), f"{plan_path} missing - run plan_audit_league.py --mode {args.mode}")
    plan = json.loads(plan_path.read_text())
    check(len(plan["teams"]) == args.teams, f"plan has {len(plan['teams'])} teams, not {args.teams}")

    client = TestClient(app)
    db = SessionLocal()

    # plan key -> draftable asset id
    asset_of: dict[str, int] = {}
    nfl_players = {p.external_ref: a.id for a, p in db.query(DraftableAsset, Player).join(Player, Player.id == DraftableAsset.player_id)
                   if p.external_ref}
    for gsis, aid in nfl_players.items():
        asset_of[f"P|{gsis}"] = aid
    teams_by_ref = {t.external_ref: t.id for t in db.query(RealTeam) if t.external_ref}
    for a, g in db.query(DraftableAsset, DefenseGroup).join(DefenseGroup, DefenseGroup.id == DraftableAsset.defense_group_id):
        ref = next((r for r, tid in teams_by_ref.items() if tid == g.real_team_id), None)
        if ref:
            asset_of[f"D|{ref}|{g.position_group}"] = a.id

    user = User(email=f"nfl-audit-{args.mode}@example.com", display_name="NFL Audit")
    db.add(user)
    db.commit()
    token = client.post("/sessions", json={"user_id": user.id}).json()["token"]
    H = {"Authorization": "Bearer " + token}

    r = client.post("/leagues", json={"name": f"NFL audit ({args.mode})", "commissioner_user_id": user.id,
                                      "universe_codes": ["nfl"], "size": args.teams, "draft_type": "snake",
                                      "defense_modes": {"nfl": args.mode}}, headers=H)
    check(r.status_code == 200, r.text)
    league = r.json()
    lid = league["id"]
    team_ids = []
    for i in range(args.teams):
        r = client.post("/teams", json={"league_id": lid, "name": f"Audit Team {i + 1}", "owner_user_id": user.id,
                                        "program_size": "small"}, headers=H)
        check(r.status_code == 200, r.text)
        team_ids.append(r.json()["id"])
    draft = client.post("/drafts/start", json={"league_id": lid}).json()

    # play the planned draft: each pick goes to the team on the clock, its next planned asset
    queues = {tid: [asset_of[k] for k in t["draft"] if k in asset_of] for tid, t in zip(team_ids, plan["teams"])}
    made = 0
    while True:
        open_pick = next((p for p in client.get(f"/drafts/{draft['id']}/picks").json() if p["draftable_asset_id"] is None), None)
        if open_pick is None or not queues[open_pick["team_id"]]:
            break              # draft done, or the team on the clock has nothing left in the plan (partial roster)
        r = client.post(f"/drafts/{draft['id']}/pick", json={"draftable_asset_id": queues[open_pick["team_id"]].pop(0)}, headers=H)
        check(r.status_code == 200, r.text)
        made += 1
    print(f"League {lid} ({args.mode}, {args.teams} teams, draft_rounds {league['draft_rounds']}): {made} planned picks made")

    # lineups for the audited week from the same prior-season plan
    week = next(w for w in client.get(f"/leagues/{lid}/weeks").json() if w["week_number"] == args.week)
    for tid, t in zip(team_ids, plan["teams"]):
        team = db.get(Team, tid)
        roster = {a.id for a in lineups.roster_assets(db, tid)}
        assign = {slot: [asset_of[k] for k in keys if asset_of.get(k) in roster]
                  for slot, keys in t["lineup"].items()}
        pairs = lineups.build_lineup(db, team, {s: ids for s, ids in assign.items() if ids})
        lineups.save_lineup(db, team, week["id"], pairs)
    db.commit()
    r = client.post(f"/leagues/{lid}/schedule/auto", json={"weeks": 1}, headers=H)
    check(r.status_code == 200, r.text)
    print(f"Lineups set for week {args.week}; schedule created ({r.json()['matchups']} matchups)")

    sys.stdout.flush()
    code = subprocess.call([sys.executable, str(ROOT / "scripts" / "audit_scored_week.py"), args.db,
                            "--league", str(lid), "--week", str(args.week)])
    sys.exit(code)


main()
