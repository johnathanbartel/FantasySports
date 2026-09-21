"""
Derives college/data/defense_shares.json for shared/defense.py:

  group_baseline_per_play
                   what each group earns on an average defended play - subtracted
                   so a league-average DL/LB/DB group nets ~0 (see defense.py)
  shares           how DL/LB/DB split a play's value when no defender is named,
                   measured from plays where the position IS known:
                     sack / interception / forced_fumble / incompletion
                         from the named sacker / interceptor / forcer / breakup
                     run_<band> / catch_<band>
                         from named tacklers ("Tackled by A.Thomas; K.Fortner"),
                         banded by yards gained (short <=2, mid 3-9, long 10+)

Needs the cache from fetch_season_plays.py (plays + rosters).

Usage:
    .venv/Scripts/python.exe src/fantasy_sports/sports/football/college/scripts/derive_defense_shares.py [--years 2022 2023 2024]
"""

import argparse
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(ROOT / "src"))
CACHE = ROOT / "data" / "cache"
OUT = Path(__file__).resolve().parents[1] / "data" / "defense_shares.json"
PROFILE_OUT = Path(__file__).resolve().parents[1] / "data" / "team_defense_profile.json"

from fantasy_sports.sports.football.shared.defense import (
    BROKEN_UP_RE, DEFENSIVE_TYPES, FORCED_BY_RE, GROUPS, INTERCEPTED_BY_RE, INTERCEPTED_RE,
    DEFAULT_PRIOR_FRAC, RUN_TYPES, SACK_BY_RE, DefenseShares, PositionResolver, TeamDefenseProfile,
    raw_credits, yards_band,
)

TACKLED_RE = re.compile(r"Tackled by (?P<n>[^@]+?)(?: at |\.$|$)")
MIN_N = 30  # below this a band falls back to its class-wide share


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=[2022, 2023, 2024])
    args = ap.parse_args()

    counts: dict[str, Counter] = defaultdict(Counter)
    for year in args.years:
        resolver = PositionResolver(json.loads((CACHE / f"cfb_roster_{year}.json").read_text()))
        with gzip.open(CACHE / f"cfb_plays_{year}.jsonl.gz", "rt", encoding="utf-8") as f:
            for line in f:
                p = SimpleNamespace(**json.loads(line))
                if p.play_type not in DEFENSIVE_TYPES or p.ppa is None:
                    continue
                text = p.play_text or ""

                def named(rx, key):
                    m = rx.search(text)
                    g = resolver.resolve(p.defense, m["n"]) if m else None
                    if g:
                        counts[key][g] += 1

                if p.play_type == "Sack":
                    named(SACK_BY_RE, "sack")
                elif p.play_type in ("Pass Interception Return", "Interception Return Touchdown", "Interception"):
                    for rx in (INTERCEPTED_RE, INTERCEPTED_BY_RE):
                        m = rx.search(text)
                        g = resolver.resolve(p.defense, m["n"]) if m else None
                        if g:
                            counts["interception"][g] += 1
                            break
                elif p.play_type == "Fumble Recovery (Opponent)":
                    named(FORCED_BY_RE, "forced_fumble")
                elif p.play_type == "Pass Incompletion":
                    named(BROKEN_UP_RE, "incompletion")
                elif p.play_type in ("Rush", "Rushing Touchdown", "Pass Reception", "Passing Touchdown"):
                    m = TACKLED_RE.search(text)
                    if m:
                        kind = "run" if p.play_type in RUN_TYPES else "catch"
                        for nm in m["n"].split(";"):
                            g = resolver.resolve(p.defense, nm.strip())
                            if g:
                                counts[f"{kind}_{yards_band(p.yards_gained)}"][g] += 1
                                counts[f"{kind}_all"][g] += 1

    def share(key):
        c = counts[key]
        n = sum(c.values())
        return {g: round(c[g] / n, 4) for g in GROUPS}, n

    shares, ns = {}, {}
    for key in ("sack", "interception", "forced_fumble", "incompletion"):
        shares[key], ns[key] = share(key)
    for kind in ("run", "catch"):
        overall, _ = share(f"{kind}_all")
        for band in ("short", "mid", "long"):
            s, n = share(f"{kind}_{band}")
            shares[f"{kind}_{band}"], ns[f"{kind}_{band}"] = (s, n) if n >= MIN_N else (overall, n)

    # Per-team profiles from each season's own defensive stats.
    profiles = {}
    for year in args.years:
        games = json.loads((CACHE / f"cfb_games_{year}.json").read_text())
        teams = {g["home_team"] for g in games} | {g["away_team"] for g in games}
        stats = json.loads((CACHE / f"cfb_defstats_{year}.json").read_text())
        profiles[year] = TeamDefenseProfile.build(stats, teams)
    PROFILE_OUT.write_text(json.dumps({
        "source": "CFBD player season stats (tackles, sacks, PD+QB hurries, INT) by position group",
        "prior_frac": DEFAULT_PRIOR_FRAC,
        "seasons": {str(y): p.table for y, p in profiles.items()},
    }))

    # Pass 2: the per-play baseline, from the shares and profiles just measured.
    uncentered = DefenseShares.uncentered(shares)
    totals, n_plays = Counter(), 0
    for year in args.years:
        resolver = PositionResolver(json.loads((CACHE / f"cfb_roster_{year}.json").read_text()))
        with gzip.open(CACHE / f"cfb_plays_{year}.jsonl.gz", "rt", encoding="utf-8") as f:
            for line in f:
                raw = raw_credits(SimpleNamespace(**json.loads(line)), resolver, uncentered, profiles[year])
                if raw:
                    n_plays += 1
                    for g, (c, _) in raw.items():
                        totals[g] += c

    data = {
        "source": f"derived from named defenders/tacklers in FBS play text, seasons {args.years}",
        "group_baseline_per_play": {g: round(totals[g] / n_plays, 5) for g in GROUPS},
        "defended_plays": n_plays,
        "shares": shares,
        "sample_sizes": ns,
    }
    OUT.write_text(json.dumps(data, indent=1))
    print(json.dumps(data, indent=1))


if __name__ == "__main__":
    main()
