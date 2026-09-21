"""
Stage 2: does EPA predict real scoreboard results, and at what exchange rate?

Runs every cached FBS game (fetch_season_plays.py) through the SAME attribution
the product uses, rolls it up to four team units per game:

    offense  scrimmage EPA the team's offense earned
    kicking  field-goal marginal EP (attribute_kicker)
    punting  punter credit (shared/punting.py) - credit is the PUNTING team's;
             it is charged against the RECEIVING team's EPA
    defense  -(opponent scrimmage EPA) - shown for reference; it is the mirror of the
             opponent's offense, so it is not a separate regressor at team level

A team's EPA in a game:   EPA = offense + kicking - opponent punting
and the game's EPA battle: EPA_A - EPA_B.  Reports how well that battle predicts
the winner and the margin, and fits points = base + k * EPA so the exchange rate
to a football score is measured, not guessed.

Also derives and writes the punt baseline (college/data/punt_ep_baseline.json).

Usage:
    .venv/Scripts/python.exe src/fantasy_sports/sports/football/college/scripts/calibrate_team_epa.py [--years 2022 2023 2024]
"""

import argparse
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(ROOT / "src"))
CACHE = ROOT / "data" / "cache"

from fantasy_sports.sports.football.college.pipeline import process_plays
from fantasy_sports.sports.football.shared.defense import PositionResolver, TeamDefenseProfile
from fantasy_sports.sports.football.shared.punting import (
    EpCurve, PuntBaseline, attribute_punter, landing_value, parse_punt,
)


def load_year(year: int):
    games = {g["id"]: g for g in json.loads((CACHE / f"cfb_games_{year}.json").read_text())}
    plays_by_game = defaultdict(list)
    with gzip.open(CACHE / f"cfb_plays_{year}.jsonl.gz", "rt", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            plays_by_game[row["game_id"]].append(SimpleNamespace(**row))
    resolver = PositionResolver(json.loads((CACHE / f"cfb_roster_{year}.json").read_text()))
    return games, plays_by_game, resolver, TeamDefenseProfile.for_season(year)


def r2(y, yhat):
    return 1 - ((y - yhat) ** 2).sum() / ((y - y.mean()) ** 2).sum()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=[2022, 2023, 2024])
    args = ap.parse_args()

    curve = EpCurve()
    seasons = [load_year(y) for y in args.years]

    # Pass 1: punt baseline from every punt in the sample.
    punts = []
    for _, plays_by_game, _, _ in seasons:
        for plays in plays_by_game.values():
            for p in plays:
                if p.play_type == "Punt":
                    parsed = parse_punt(p.play_text, p.yards_to_goal)
                    if parsed:
                        punts.append((p.yards_to_goal, landing_value(parsed, curve)))
    baseline = PuntBaseline.derive(punts)
    baseline.save(f"derived from {len(punts)} FBS punts, seasons {args.years}", len(punts))
    print(f"punts parsed: {len(punts)}")

    # Pass 2: per team-game units.
    rows = []  # one per team-game: dict
    unparsed_punts = 0
    for games, plays_by_game, resolver, profile in seasons:
        for gid, g in games.items():
            plays = plays_by_game.get(gid)
            if not plays or g["home_points"] is None:
                continue
            res = process_plays(plays, resolver=resolver, team_profile=profile)
            punt_credit = defaultdict(float)
            for p in plays:
                if p.play_type == "Punt":
                    out = attribute_punter(p, curve, baseline)
                    if out is None:
                        unparsed_punts += 1
                    else:
                        punt_credit[p.offense] += out[1]
            teams = (g["home_team"], g["away_team"])
            pts = {g["home_team"]: g["home_points"], g["away_team"]: g["away_points"]}
            for me, opp in (teams, teams[::-1]):
                rows.append({
                    "game": gid, "team": me, "opp": opp, "pts": pts[me], "opp_pts": pts[opp],
                    "off": res.scrimmage_ppa_by_team.get(me, 0.0),
                    "kick": res.fg_credit_by_team.get(me, 0.0),
                    "punt": punt_credit.get(me, 0.0),
                    "opp_punt": punt_credit.get(opp, 0.0),
                })
    print(f"team-games: {len(rows)}  (non-punt-parsed or blocked punts: {unparsed_punts})\n")

    def col(k):
        return np.array([r[k] for r in rows], dtype=float)

    pts, off, kick, punt, opp_punt = col("pts"), col("off"), col("kick"), col("punt"), col("opp_punt")
    epa = off + kick - opp_punt

    print(f"real points/team-game: mean {pts.mean():.1f}  sd {pts.std():.1f}")
    for name, v in (("offense", off), ("kicking", kick), ("punting (own)", punt)):
        print(f"  {name:14s} per game: mean {v.mean():+.2f}  sd {v.std():.2f}   corr with points {np.corrcoef(v, pts)[0, 1]:+.3f}")
    print(f"  corr(punt received, points) {np.corrcoef(-opp_punt, pts)[0, 1]:+.3f}\n")

    # points = base + k * EPA
    X = np.column_stack([np.ones_like(epa), epa])
    (base, k), *_ = np.linalg.lstsq(X, pts, rcond=None)
    print(f"points = {base:.2f} + {k:.3f} * EPA        R^2 = {r2(pts, X @ [base, k]):.3f}   corr = {np.corrcoef(epa, pts)[0, 1]:.3f}")

    # Unit-by-unit exchange rates (does one EPA point from each unit buy the same points?)
    X4 = np.column_stack([np.ones_like(epa), off, kick, -opp_punt])
    b, *_ = np.linalg.lstsq(X4, pts, rcond=None)
    print(f"points = {b[0]:.2f} + {b[1]:.3f}*offense + {b[2]:.3f}*kicking + {b[3]:.3f}*(-opp punting)   R^2 = {r2(pts, X4 @ b):.3f}")

    # The EPA battle: pair the two sides of each game.
    by_game = defaultdict(list)
    for r, e in zip(rows, epa):
        by_game[r["game"]].append((r, e))
    diff, margin = [], []
    for pair in by_game.values():
        if len(pair) != 2:
            continue
        (ra, ea), (rb, eb) = pair
        diff.append(ea - eb)
        margin.append(ra["pts"] - ra["opp_pts"])
    diff, margin = np.array(diff), np.array(margin)
    decided = margin != 0
    acc = (np.sign(diff[decided]) == np.sign(margin[decided])).mean()
    Xm = np.column_stack([diff])
    km = float(np.linalg.lstsq(Xm, margin, rcond=None)[0][0])
    print(f"\nEPA battle over {len(diff)} games: winner picked correctly {acc:.1%}")
    print(f"margin = {km:.3f} * EPA diff   R^2 = {r2(margin, diff * km):.3f}   corr = {np.corrcoef(diff, margin)[0, 1]:.3f}")
    print(f"real margin sd {margin.std():.1f}   EPA-diff sd {diff.std():.1f}")


if __name__ == "__main__":
    main()
