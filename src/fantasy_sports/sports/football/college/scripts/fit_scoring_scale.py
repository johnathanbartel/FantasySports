"""
Fits the credit -> fantasy-points scale (platform/scoring_scale.py) from the cached
seasons' per-week credits (build_weekly_credits.py).

The question a lineup's raw credit has to answer: what is a normal lineup's credit in THIS league? Drafted
starters beat the pool, and the deeper the league drafts (more teams) the weaker its starters, so the
expected lineup credit depends on the league size and the lineup shape. So we simulate leagues:

  * pool = a season's players (its roster); snake draft, value-over-replacement, drafters rank
    players by their PRIOR season's per-game credit (+ per-team noise, tau) - so regression to the
    mean, transfers, and injuries/byes (a 0 week) are all in the realized numbers;
  * lineup = 1 QB, 2 RB, 2 WR, 1 TE, 1 FLEX, 1 DL, 1 LB, 1 DB, 1 K, set from the same prior ranking;
  * score each lineup on the season's ACTUAL weekly credits.

For each league size N the fit records the mean credit of a starter in each slot (slot_mean) and the
sd of lineup credit (lineup_sd). Together with the NCAA per-team points mean/sd (from the same cached
games) those are the whole scale.

Usage:
    .venv/Scripts/python.exe src/fantasy_sports/sports/football/college/scripts/fit_scoring_scale.py [--seeds 60]
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

SEASON_WEEKS = 10                      # platform/season.py
LEAGUE_SIZES = [4, 6, 8, 10, 12, 14, 16, 20, 24]
STARTERS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "PK": 1, "DL": 1, "LB": 1, "DB": 1}   # + 1 FLEX (RB/WR/TE)
DRAFT_ROUNDS = 21                      # 11 starters + 10 bench
CAPS = {"QB": 3, "RB": 6, "WR": 6, "TE": 3, "PK": 2, "DL": 2, "LB": 2, "DB": 2}
REPL_DEPTH = {"QB": 1.0, "RB": 2.4, "WR": 2.4, "TE": 1.2, "PK": 1.0, "DL": 1.0, "LB": 1.0, "DB": 1.0}  # starters/team, incl. FLEX share
POS_SLOT = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "PK": "K", "DL": "DL", "LB": "LB", "DB": "DB"}
SLOT_ORDER = ["QB", "RB", "WR", "TE", "FLEX", "DL", "LB", "DB", "K"]
POOL_DEPTH = 260                        # candidates per position (more than any league drafts)


def load_year(year: int) -> dict:
    d = json.loads((CACHE / f"cfb_weekly_credits_{year}.json").read_text())
    weeks = list(range(1, SEASON_WEEKS + 1))
    credit = defaultdict(lambda: np.zeros(len(weeks)))
    for w, assets in d["weeks"].items():
        if int(w) in weeks:
            for k, v in assets.items():
                credit[k][int(w) - 1] += v
    team_games = defaultdict(int)
    for w, teams in d["played"].items():
        for t in teams:
            team_games[t] += 1
    return {"credit": credit, "positions": d["positions"], "team_games": team_games}


def asset_name(key: str) -> tuple:
    kind, a, b = key.split("|")
    return ("D", a, b) if kind == "D" else ("P", a, b)     # ("P", name, team) / ("D", team, group)


def prior_value(prev: dict) -> dict:
    """(name, position) or (team, group) -> prior-season credit per team-game (all weeks)."""
    tot = defaultdict(float)
    pos = prev["positions"]
    d = json.loads(Path(prev["_path"]).read_text())
    for w, assets in d["weeks"].items():
        for k, v in assets.items():
            kind, a, b = k.split("|")
            tot[(a, pos[k]) if kind == "P" else (a, b)] += v          # player: (name, pos); defense: (team, group)
    return tot


def build_pool(year: int, data: dict, prev_value: dict) -> dict:
    """position -> (asset keys, prior values) for the season being drafted, best prior first."""
    pos = data["positions"]
    per_pos = defaultdict(list)
    for key, p in pos.items():
        kind, a, b = key.split("|")
        if kind == "P":
            prior = prev_value.get((a, p))
        else:
            prior = prev_value.get((a, b))
        games = 12.0
        per_pos[p].append((key, (prior / games) if prior is not None else -1e9))
    pool = {}
    for p, rows in per_pos.items():
        rows.sort(key=lambda r: -r[1])
        rows = rows[:POOL_DEPTH]
        pool[p] = (np.array([r[0] for r in rows], dtype=object), np.array([r[1] for r in rows]))
    return pool


def draft_league(pool: dict, n: int, rng: np.random.Generator, tau_frac: float) -> list:
    """Snake draft, returns per team the drafted asset keys by position."""
    repl, sd = {}, {}
    for p, (keys, prior) in pool.items():
        idx = min(int(round(n * REPL_DEPTH[p])), len(prior) - 1)
        repl[p] = prior[idx]
        sd[p] = float(np.std(prior[: max(idx, 10)]))
    # per-team perceived values = prior + noise
    perceived = {p: prior[None, :] + rng.normal(0, tau_frac * max(sd[p], 1e-6), (n, len(prior))) * (prior[None, :] > -1e8)
                 for p, (keys, prior) in pool.items()}
    order = {p: np.argsort(-perceived[p], axis=1) for p in pool}
    ptr = {(t, p): 0 for t in range(n) for p in pool}
    taken = {p: np.zeros(len(pool[p][0]), dtype=bool) for p in pool}
    have = [defaultdict(list) for _ in range(n)]
    needs = {**STARTERS}
    for rnd in range(DRAFT_ROUNDS):
        teams = range(n) if rnd % 2 == 0 else range(n - 1, -1, -1)
        for t in teams:
            unmet = {p: max(0, needs[p] - len(have[t][p])) for p in needs}
            remaining = DRAFT_ROUNDS - rnd
            forced = sum(unmet.values()) >= remaining
            best = None
            for p in pool:
                if len(have[t][p]) >= CAPS[p] or (forced and unmet[p] == 0):
                    continue
                i = ptr[(t, p)]
                o = order[p][t]
                while i < len(o) and taken[p][o[i]]:
                    i += 1
                ptr[(t, p)] = i
                if i >= len(o):
                    continue
                j = o[i]
                if pool[p][1][j] < -1e8:
                    continue
                vor = perceived[p][t, j] - repl[p]
                if best is None or vor > best[0]:
                    best = (vor, p, j)
            if best is None:
                continue
            _, p, j = best
            taken[p][j] = True
            have[t][p].append(pool[p][0][j])
    return have


def lineup_of(have: dict, prior_of: dict) -> dict:
    """slot -> list of asset keys, by prior rank."""
    lineup, flex_pool = {}, []
    for p, n_start in STARTERS.items():
        ranked = sorted(have.get(p, []), key=lambda k: -prior_of[k])
        lineup[POS_SLOT[p]] = ranked[:n_start]
        if p in ("RB", "WR", "TE"):
            flex_pool += ranked[n_start:]
    flex = sorted(flex_pool, key=lambda k: -prior_of[k])[:1]
    lineup["FLEX"] = flex
    return lineup


def simulate(year: int, n: int, seeds: int, tau_frac: float, data: dict, pool: dict, seed0: int = 0) -> dict:
    """{'lineup': [n_teams x weeks array per league], 'slot': {'RB#1': [n_teams x weeks per league], ...}}"""
    credit = data["credit"]
    prior_of = {}
    for p, (keys, prior) in pool.items():
        prior_of.update(dict(zip(keys, prior)))
    zero = np.zeros(SEASON_WEEKS)
    out = {"lineup": [], "slot": defaultdict(list)}
    for s in range(seeds):
        rng = np.random.default_rng(1000 * year + 31 * n + seed0 + s)
        have = draft_league(pool, n, rng, tau_frac)
        lineups = [lineup_of(h, prior_of) for h in have]
        team_week = np.zeros((n, SEASON_WEEKS))
        slot_week: dict[str, np.ndarray] = defaultdict(lambda: np.zeros((n, SEASON_WEEKS)))
        for ti, lu in enumerate(lineups):
            for sl, keys in lu.items():
                for i, k in enumerate(keys):
                    c = credit.get(k, zero)
                    team_week[ti] += c
                    slot_week[f"{sl}#{i + 1}"][ti] += c
        out["lineup"].append(team_week)
        for name, arr in slot_week.items():
            out["slot"][name].append(arr)
    return out


def ncaa_points(years):
    pts = []
    for y in years:
        games = json.loads((CACHE / f"cfb_games_{y}.json").read_text())
        n = defaultdict(int)
        for g in games:
            n[g["home_team"]] += 1
            n[g["away_team"]] += 1
        fbs = {t for t, c in n.items() if c >= 6}
        for g in games:
            if g["season_type"] != "regular" or g["home_points"] is None:
                continue
            if g["home_team"] in fbs:
                pts.append(g["home_points"])
            if g["away_team"] in fbs:
                pts.append(g["away_points"])
    return np.array(pts, dtype=float)


SLOT_INSTANCES = ["QB#1", "RB#1", "RB#2", "WR#1", "WR#2", "TE#1", "FLEX#1", "DL#1", "LB#1", "DB#1", "K#1"]
SOFTPLUS_A = 3.0        # points; how soft the floor at 0 is


def softplus(z, a=SOFTPLUS_A):
    return a * np.logaddexp(0.0, np.asarray(z) / a)


def simulate_all(years, seeds, tau, sizes=None, seed0=0):
    result = {}
    for year in years:
        data = load_year(year)
        prev = load_year(year - 1)
        prev["_path"] = str(CACHE / f"cfb_weekly_credits_{year - 1}.json")
        pool = build_pool(year, data, prior_value(prev))
        result[year] = {n: simulate(year, n, seeds, tau, data, pool, seed0) for n in (sizes or LEAGUE_SIZES)}
    return result


def fit_params(sims: dict, years, pts: np.ndarray, tau: float) -> dict:
    """The whole scale, from simulated leagues `sims` (year -> N -> simulate() output)."""
    params = {"years_fit": list(years), "tau": tau, "season_weeks": SEASON_WEEKS,
              "ncaa_ppg": round(float(pts.mean()), 4), "ncaa_ppg_sd": round(float(pts.std()), 4),
              "league_sizes": LEAGUE_SIZES, "slot_mean": {}, "softplus_A": SOFTPLUS_A}
    for sl in SLOT_INSTANCES:
        params["slot_mean"][sl] = [round(float(np.mean([np.mean(w) for y in years for w in sims[y][n]["slot"][sl]])), 4)
                                   for n in LEAGUE_SIZES]
    mu = {n: sum(params["slot_mean"][sl][i] for sl in SLOT_INSTANCES) for i, n in enumerate(LEAGUE_SIZES)}
    # b: match the spread of scores to the NCAA's, using lineup credit's spread around its league-size mean
    dev = np.concatenate([(w - mu[n]).ravel() for y in years for n in LEAGUE_SIZES for w in sims[y][n]["lineup"]])
    params["lineup_sd"] = round(float(dev.std()), 4)
    params["b"] = round(float(pts.std() / dev.std()), 5)
    # c: shift so the floored scores still average exactly the NCAA mean over the reference leagues
    z = params["ncaa_ppg"] + params["b"] * dev
    lo, hi = -5.0, 5.0
    for _ in range(60):
        c = (lo + hi) / 2
        if softplus(z + c).mean() > params["ncaa_ppg"]:
            hi = c
        else:
            lo = c
    params["softplus_c"] = round(c, 5)
    return params


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--tau", type=float, default=0.25, help="drafter noise as a fraction of a position's spread")
    ap.add_argument("--years", type=int, nargs="+", default=[2023, 2024])
    args = ap.parse_args()
    sims = simulate_all(args.years, args.seeds, args.tau)
    params = fit_params(sims, args.years, ncaa_points([2022, 2023, 2024]), args.tau)
    OUT.write_text(json.dumps(params, indent=1))
    print(json.dumps(params, indent=1))


if __name__ == "__main__":
    main()
