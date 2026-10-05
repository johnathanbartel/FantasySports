"""
Plans the drafted rosters and weekly lineups for an NFL scored-week audit league, the way the scale fit models
drafters (fit_scoring_scale.py): a snake draft by value over replacement on the PRIOR season's credit per game
(+ per-team noise), and lineups set from that same prior ranking. Nothing here looks at the audited week, so the
audit tests the scale on lineups a real drafter could have set - not on hindsight picks.

Offline: reads data/cache/nfl_weekly_credits_<season-1>.json and <season>.json (build_weekly_credits.py) for the
player pool, writes data/cache/nfl_audit_league_<mode>.json:
    {"mode":.., "season":.., "teams": [{"draft": [asset key, ...], "lineup": {"QB": [key], ...}}, ...]}
scripts/run_nfl_scored_week.py (API env) plays that plan through the real draft/lineup/scoring code.

Usage (data-pipeline env):
    .venv/Scripts/python.exe src/fantasy_sports/sports/football/professional/scripts/plan_audit_league.py --mode group --season 2024 [--teams 12 --seed 1]
"""

import argparse
import importlib.util
import json
from pathlib import Path

import numpy as np

_FIT = Path(__file__).with_name("fit_scoring_scale.py")
_spec = importlib.util.spec_from_file_location("nfl_fit_scoring_scale", _FIT)
fit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fit)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=list(fit.MODES), default="group")
    ap.add_argument("--season", type=int, default=2024)
    ap.add_argument("--teams", type=int, default=12)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--tau", type=float, default=0.25)
    args = ap.parse_args()

    data, prev = fit.load_year(args.season), fit.load_year(args.season - 1)
    pool = fit.build_pool(args.mode, data, fit.prior_totals(prev))
    prior_of = {}
    for _p, (keys, prior) in pool.items():
        prior_of.update(dict(zip(keys, prior)))
    rng = np.random.default_rng(args.seed)
    have = fit.draft_league(pool, args.mode, args.teams, rng, args.tau)
    teams = [{"draft": [k for ks in h.values() for k in ks], "lineup": fit.lineup_of(h, args.mode, prior_of)}
             for h in have]
    out = ROOT_CACHE / f"nfl_audit_league_{args.mode}.json"
    out.write_text(json.dumps({"mode": args.mode, "season": args.season, "teams": teams}))
    print(f"{args.mode}: planned {args.teams} teams, {sum(len(t['draft']) for t in teams)} picks -> {out}")


ROOT_CACHE = fit.CACHE

if __name__ == "__main__":
    main()
