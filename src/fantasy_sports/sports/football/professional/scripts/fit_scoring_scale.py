"""
Fits the NFL credit -> fantasy-points scale (platform/scoring_scale.py) from the cached seasons' per-week credits
(build_weekly_credits.py). Same method as college/scripts/fit_scoring_scale.py (docs/scoring_scale_fit_report.md),
run once for each way a league can draft defense (group / team / individual):

  * pool = a season's players and defense units; snake draft by value-over-replacement, drafters rank assets by
    their PRIOR season's per-game credit (+ per-team noise tau), so regression to the mean, transfers, injuries and
    byes (a 0 week) are all in the realized numbers;
  * lineup = the mode's starting slots (docs/nfl_spec.md), set from the same prior ranking;
  * every lineup is scored on the season's ACTUAL weekly credits, over all of its regular-season weeks.

Per mode and league size N the fit records the mean credit of a starter in each slot (slot_mean). With the NFL
points-per-team-game mean/sd from the same seasons' real games that is the whole scale. It then runs a
leave-one-season-out check: refit without a season and score that season's simulated leagues with the result.

Usage (data-pipeline env, after build_weekly_credits.py):
    .venv/Scripts/python.exe src/fantasy_sports/sports/football/professional/scripts/fit_scoring_scale.py [--seeds 100]
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(ROOT / "src"))
CACHE = ROOT / "data" / "cache"
OUT = Path(__file__).resolve().parents[1] / "data" / "scoring_scale.json"

from fantasy_sports.platform.scoring_scale import to_fantasy_points_flat

LEAGUE_SIZES = [4, 6, 8, 10, 12, 14, 16, 20, 24]
OFFENSE_STARTERS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1}          # + 1 FLEX (RB/WR/TE)
OFFENSE_CAPS = {"QB": 3, "RB": 6, "WR": 6, "TE": 3, "K": 2}
OFFENSE_REPL = {"QB": 1.0, "RB": 2.4, "WR": 2.4, "TE": 1.2, "K": 1.0}   # starters/team incl. FLEX share
PRIOR_GAMES = 17.0
POOL_DEPTH = 260
SOFTPLUS_A = 3.0

# defense_mode -> (starters, caps, replacement depth, draft rounds); rounds = roster size (platform/defense_modes.py)
MODES = {
    "group": dict(starters={"DL": 1, "LB": 1, "DB": 1}, caps={"DL": 2, "LB": 2, "DB": 2},
                  repl={"DL": 1.0, "LB": 1.0, "DB": 1.0}, rounds=19, dflex=0),
    "team": dict(starters={"DEF": 1}, caps={"DEF": 2}, repl={"DEF": 1.0}, rounds=15, dflex=0),
    "individual": dict(starters={"DL": 3, "LB": 3, "DB": 4}, caps={"DL": 6, "LB": 6, "DB": 7},
                       repl={"DL": 3.33, "LB": 3.33, "DB": 4.33}, rounds=32, dflex=1),
}
DEFENSE_POS = ("DL", "LB", "DB", "DEF")


def slot_instances(mode: str) -> list[str]:
    cfg = MODES[mode]
    out = []
    for slot, n in {**OFFENSE_STARTERS, "FLEX": 1, **cfg["starters"], **({"DFLEX": 1} if cfg["dflex"] else {})}.items():
        out += [f"{slot}#{i}" for i in range(1, n + 1)]
    return out


def load_year(year: int) -> dict:
    d = json.loads((CACHE / f"nfl_weekly_credits_{year}.json").read_text())
    n_weeks = max(int(w) for w in d["played"])
    credit = defaultdict(lambda: np.zeros(n_weeks))
    for w, assets in d["weeks"].items():
        for k, v in assets.items():
            credit[k][int(w) - 1] += v
    return {"credit": credit, "positions": d["positions"], "n_weeks": n_weeks, "games": d["games"]}


def prior_totals(prev: dict) -> dict:
    return {k: float(v.sum()) for k, v in prev["credit"].items()}


def build_pool(mode: str, data: dict, prior: dict) -> dict:
    """position -> (asset keys, prior per-game values), best prior first; assets with no prior season are not drafted."""
    wanted = set(OFFENSE_STARTERS) | set(MODES[mode]["starters"])
    per_pos = defaultdict(list)
    for key, p in data["positions"].items():
        if p not in wanted or key not in prior:
            continue
        is_unit = key.startswith("D|")
        if p in DEFENSE_POS and (mode == "individual") == is_unit:      # units for group/team, players for individual
            continue
        per_pos[p].append((key, prior[key] / PRIOR_GAMES))
    pool = {}
    for p, rows in per_pos.items():
        rows.sort(key=lambda r: -r[1])
        rows = rows[:POOL_DEPTH]
        pool[p] = (np.array([r[0] for r in rows], dtype=object), np.array([r[1] for r in rows]))
    return pool


def draft_league(pool: dict, mode: str, n: int, rng: np.random.Generator, tau_frac: float) -> list:
    cfg = MODES[mode]
    starters = {**OFFENSE_STARTERS, **cfg["starters"]}
    caps = {**OFFENSE_CAPS, **cfg["caps"]}
    repl_depth = {**OFFENSE_REPL, **cfg["repl"]}
    repl, sd = {}, {}
    for p, (keys, prior) in pool.items():
        idx = min(int(round(n * repl_depth[p])), len(prior) - 1)
        repl[p] = prior[idx]
        sd[p] = float(np.std(prior[: max(idx, 10)]))
    perceived = {p: prior[None, :] + rng.normal(0, tau_frac * max(sd[p], 1e-6), (n, len(prior)))
                 for p, (keys, prior) in pool.items()}
    order = {p: np.argsort(-perceived[p], axis=1) for p in pool}
    ptr = {(t, p): 0 for t in range(n) for p in pool}
    taken = {p: np.zeros(len(pool[p][0]), dtype=bool) for p in pool}
    have = [defaultdict(list) for _ in range(n)]
    rounds = cfg["rounds"]
    for rnd in range(rounds):
        teams = range(n) if rnd % 2 == 0 else range(n - 1, -1, -1)
        for t in teams:
            unmet = {p: max(0, starters[p] - len(have[t][p])) for p in starters if p in pool}
            forced = sum(unmet.values()) >= rounds - rnd
            best = None
            for p in pool:
                if len(have[t][p]) >= caps[p] or (forced and unmet.get(p, 0) == 0):
                    continue
                i = ptr[(t, p)]
                o = order[p][t]
                while i < len(o) and taken[p][o[i]]:
                    i += 1
                ptr[(t, p)] = i
                if i >= len(o):
                    continue
                j = o[i]
                vor = perceived[p][t, j] - repl[p]
                if best is None or vor > best[0]:
                    best = (vor, p, j)
            if best is None:
                continue
            _, p, j = best
            taken[p][j] = True
            have[t][p].append(pool[p][0][j])
    return have


def lineup_of(have: dict, mode: str, prior_of: dict) -> dict:
    cfg = MODES[mode]
    lineup, flex_pool, dflex_pool = {}, [], []
    for p, n_start in {**OFFENSE_STARTERS, **cfg["starters"]}.items():
        ranked = sorted(have.get(p, []), key=lambda k: -prior_of[k])
        lineup[p] = ranked[:n_start]
        if p in ("RB", "WR", "TE"):
            flex_pool += ranked[n_start:]
        elif p in ("DL", "LB", "DB") and cfg["dflex"]:
            dflex_pool += ranked[n_start:]
    lineup["FLEX"] = sorted(flex_pool, key=lambda k: -prior_of[k])[:1]
    if cfg["dflex"]:
        lineup["DFLEX"] = sorted(dflex_pool, key=lambda k: -prior_of[k])[:1]
    return lineup


def simulate(year: int, mode: str, n: int, seeds: int, tau: float, data: dict, pool: dict) -> dict:
    """{'lineup': [n_teams x weeks array per league], 'slot': {'RB#1': [n_teams x weeks per league], ...}}"""
    credit, weeks = data["credit"], data["n_weeks"]
    prior_of = {}
    for p, (keys, prior) in pool.items():
        prior_of.update(dict(zip(keys, prior)))
    zero = np.zeros(weeks)
    out = {"lineup": [], "slot": defaultdict(list)}
    for s in range(seeds):
        rng = np.random.default_rng(1000 * year + 31 * n + 7 * len(mode) + s)
        have = draft_league(pool, mode, n, rng, tau)
        team_week = np.zeros((n, weeks))
        slot_week: dict[str, np.ndarray] = defaultdict(lambda: np.zeros((n, weeks)))
        for ti, h in enumerate(have):
            for sl, keys in lineup_of(h, mode, prior_of).items():
                for i, k in enumerate(keys):
                    c = credit.get(k, zero)
                    team_week[ti] += c
                    slot_week[f"{sl}#{i + 1}"][ti] += c
        out["lineup"].append(team_week)
        for name, arr in slot_week.items():
            out["slot"][name].append(arr)
    return out


def real_points(years) -> np.ndarray:
    pts = []
    for y in years:
        for _w, _h, _a, hs, as_ in load_year(y)["games"]:
            pts += [hs, as_]
    return np.array(pts, dtype=float)


def softplus(z, a=SOFTPLUS_A):
    return a * np.logaddexp(0.0, np.asarray(z) / a)


def fit_mode(sims: dict, mode: str, years, pts: np.ndarray) -> dict:
    """One mode's block of the scale from simulated leagues `sims` (year -> N -> simulate() output)."""
    slots = slot_instances(mode)
    block = {"slot_mean": {sl: [round(float(np.mean([np.mean(w) for y in years for w in sims[y][n]["slot"][sl]]
                                                     )), 4) for n in LEAGUE_SIZES] for sl in slots}}
    mu = {n: sum(block["slot_mean"][sl][i] for sl in slots) for i, n in enumerate(LEAGUE_SIZES)}
    dev = np.concatenate([(w - mu[n]).ravel() for y in years for n in LEAGUE_SIZES for w in sims[y][n]["lineup"]])
    block["lineup_sd"] = round(float(dev.std()), 4)
    block["b"] = round(float(pts.std() / dev.std()), 5)
    z = pts.mean() + block["b"] * dev
    lo, hi = -5.0, 5.0
    for _ in range(60):
        c = (lo + hi) / 2
        if softplus(z + c).mean() > pts.mean():
            hi = c
        else:
            lo = c
    block["softplus_c"] = round(c, 5)
    return block


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=100)
    ap.add_argument("--tau", type=float, default=0.25, help="drafter noise as a fraction of a position's spread")
    ap.add_argument("--years", type=int, nargs="+", default=list(range(2019, 2025)))
    ap.add_argument("--modes", nargs="+", default=list(MODES))
    args = ap.parse_args()
    years = args.years
    pts = real_points(years)
    params = {"universe": "nfl", "years_fit": years, "tau": args.tau, "seeds": args.seeds,
              "ppg": round(float(pts.mean()), 4), "ppg_sd": round(float(pts.std()), 4),
              "league_sizes": LEAGUE_SIZES, "softplus_A": SOFTPLUS_A, "modes": {}}
    print(f"NFL points per team-game {params['ppg']} (sd {params['ppg_sd']}, n={len(pts)}) over {years}", flush=True)
    yearly_pts = {y: real_points([y]) for y in years}
    for mode in args.modes:
        sims = {}
        for y in years:
            data, prev = load_year(y), load_year(y - 1)
            pool = build_pool(mode, data, prior_totals(prev))
            sims[y] = {n: simulate(y, mode, n, args.seeds, args.tau, data, pool) for n in LEAGUE_SIZES}
            print(f"  {mode} {y}: simulated {len(LEAGUE_SIZES)} league sizes", flush=True)
        params["modes"][mode] = fit_mode(sims, mode, years, pts)
        # leave-one-season-out: fit without y, score y's leagues, compare to y's real points per team-game
        print(f"  {mode}: leave-one-season-out mean team score (target = that season's real ppg)")
        errs = []
        for y in years:
            rest = [x for x in years if x != y]
            block = fit_mode(sims, mode, rest, np.concatenate([yearly_pts[x] for x in rest]))
            p = {**{k: v for k, v in params.items() if k != "modes"}, **block, "ppg": float(np.concatenate(
                [yearly_pts[x] for x in rest]).mean())}
            means = []
            for n in LEAGUE_SIZES:
                mu_n = LEAGUE_SIZES.index(n)
                exp = sum(block["slot_mean"][sl][mu_n] for sl in slot_instances(mode))
                means.append(float(np.mean([to_fantasy_points_flat(w, exp, p) for w in sims[y][n]["lineup"]])))
            target = float(yearly_pts[y].mean())
            errs.append(np.mean(means) - target)
            print(f"    {y}: sizes 4..24 mean {np.mean(means):5.2f}  (min {min(means):5.2f} max {max(means):5.2f})  "
                  f"real {target:5.2f}  diff {np.mean(means) - target:+.2f}", flush=True)
        print(f"    {mode}: mean |diff| {np.mean(np.abs(errs)):.2f} points", flush=True)
    OUT.write_text(json.dumps(params, indent=1))
    print("wrote", OUT)
    for mode, b in params["modes"].items():
        print(mode, "b", b["b"], "c", b["softplus_c"], "lineup_sd", b["lineup_sd"],
              "12-team expected credit", round(sum(v[4] for v in b["slot_mean"].values()), 2))


if __name__ == "__main__":
    main()
