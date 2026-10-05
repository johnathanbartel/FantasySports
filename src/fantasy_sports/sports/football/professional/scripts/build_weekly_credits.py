"""
Builds the per-week credit table the NFL scoring-scale fit runs on: for each season, every player's and defense
unit's summed credit per regular-season week - the same quantity the app stores in weekly_player_scores - from the
app's own NFL pipeline (professional/pipeline.py), so the fit sees exactly what a scored league sees.

Downloads nflverse play-by-play, schedule and season rosters (nfl_data_py; no key) once per season and caches them
as small JSON. Only players on that season's roster in a fantasy position exist as assets (like the app's roster
ingest); credit to anyone else is dropped and reported.

Writes data/cache/nfl_weekly_credits_<year>.json (gitignored):
    {"weeks":     {"<week>": {"<asset key>": credit}},
     "played":    {"<week>": [teams with a game]},
     "positions": {"<asset key>": "QB"|"RB"|"WR"|"TE"|"K"|"DL"|"LB"|"DB"|"DEF"},
     "games":     [[week, home, away, home_score, away_score], ...],
     "matching":  {"credit_total": .., "credit_unmatched": ..}}
Asset keys: "P|<gsis_id>" for a player (offense, kicker or individual defender), "D|<team>|<DL|LB|DB|DEF>" for a unit.
Team abbreviations are normalised to today's (OAK->LV, SD->LAC, STL->LA) so a unit is the same asset across years.

Usage (data-pipeline env, needs the network):
    .venv/Scripts/python.exe src/fantasy_sports/sports/football/professional/scripts/build_weekly_credits.py [--years 2018 ... 2024]
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(ROOT / "src"))
CACHE = ROOT / "data" / "cache"

import nfl_data_py as nfl

from fantasy_sports.platform.defense_modes import defender_group
from fantasy_sports.sports.football.professional.defense import DefenseBaseline, EventWeights, PositionResolver
from fantasy_sports.sports.football.professional.pipeline import process_plays

OFFENSE = {"QB", "RB", "WR", "TE", "K"}
TEAM_FIX = {"OAK": "LV", "SD": "LAC", "STL": "LA"}
GROUP_ROLE = {"TEAM": "DEF", "DL": "DL", "LB": "LB", "DB": "DB"}


def norm_team(t):
    return TEAM_FIX.get(t, t)


def build_year(year: int) -> dict:
    pbp = nfl.import_pbp_data([year], downcast=False)
    pbp = pbp[(pbp.season_type == "REG") & pbp.play_type.isin(("run", "pass", "field_goal"))]
    sched = nfl.import_schedules([year])
    sched = sched[sched.game_type == "REG"]
    roster = nfl.import_seasonal_rosters([year]).dropna(subset=["player_id", "position"])
    resolver = PositionResolver.from_roster_frame(roster)

    pos_of: dict[str, str] = {}
    for r in roster.itertuples():
        p = str(r.position).upper()
        p = "K" if p == "PK" else p
        grp = defender_group(p)
        pos_of[str(r.player_id)] = p if p in OFFENSE else (grp or "")
    pos_of = {k: v for k, v in pos_of.items() if v}

    result = process_plays(pbp, resolver=resolver, individual_weights=EventWeights.load(),
                           defense_baseline=DefenseBaseline.load())
    weeks: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    positions: dict[str, str] = {}
    match = Counter()
    for c in result.play_level_credits:
        week = int(c.play.week)
        a = abs(c.points)
        match["credit_total"] += a
        if c.player_id is not None:
            pos = pos_of.get(c.player_id)
            if pos is None:
                match["credit_unmatched"] += a
                continue
            key = f"P|{c.player_id}"
        else:
            pos = GROUP_ROLE[c.position_group]
            key = f"D|{norm_team(c.team)}|{pos}"
        positions[key] = pos
        weeks[week][key] += c.points

    played: dict[int, set] = defaultdict(set)
    games = []
    for g in sched.itertuples():
        if g.home_score != g.home_score:      # unplayed
            continue
        h, a = norm_team(g.home_team), norm_team(g.away_team)
        played[int(g.week)] |= {h, a}
        games.append([int(g.week), h, a, float(g.home_score), float(g.away_score)])
    return {
        "weeks": {str(w): dict(v) for w, v in sorted(weeks.items())},
        "played": {str(w): sorted(v) for w, v in sorted(played.items())},
        "positions": positions, "games": games,
        "matching": {k: round(v, 1) for k, v in match.items()},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=list(range(2018, 2025)))
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    for year in args.years:
        data = build_year(year)
        (CACHE / f"nfl_weekly_credits_{year}.json").write_text(json.dumps(data))
        m = data["matching"]
        print(f"{year}: {len(data['weeks'])} weeks, {len(data['games'])} games, {len(data['positions'])} assets, "
              f"unmatched credit {m['credit_unmatched'] / m['credit_total']:.1%}", flush=True)


if __name__ == "__main__":
    main()
