"""
Builds the per-week credit table the scoring-scale fit runs on: for each cached
season, every player's and defense group's summed credit per week - the same
quantity the app stores in weekly_player_scores - using the app's own name
matching (ingest_scores.norm_name / initial_last) against that season's roster.

Only FBS teams' players exist as assets (an FCS opponent's credit is dropped,
exactly as in the app). A team with fewer than 6 games in the cache is an FCS
school appearing as an opponent.

Writes data/cache/cfb_weekly_credits_<year>.json (gitignored):
    {"weeks": {"<week>": {"<asset key>": credit}},
     "played": {"<week>": [teams with a game]},
     "positions": {"<asset key>": "QB"|"RB"|"WR"|"TE"|"PK"|"DL"|"LB"|"DB"},
     "matching": {"credit_total": .., "credit_fcs": .., "credit_unmatched_fbs": ..}}
Asset keys: "P|<normalized name>|<team>" for a player, "D|<team>|<group>" for a defense group.

Usage:
    .venv/Scripts/python.exe src/fantasy_sports/sports/football/college/scripts/build_weekly_credits.py [--years 2022 2023 2024]
"""

import argparse
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(ROOT / "src"))
CACHE = ROOT / "data" / "cache"

from fantasy_sports.sports.football.college.ingest_scores import initial_last, norm_name
from fantasy_sports.sports.football.college.pipeline import process_plays
from fantasy_sports.sports.football.shared.defense import PositionResolver, TeamDefenseProfile

APP_POSITIONS = {"QB", "RB", "WR", "TE", "PK"}


def build_year(year: int) -> dict:
    games = {g["id"]: g for g in json.loads((CACHE / f"cfb_games_{year}.json").read_text())}
    roster = json.loads((CACHE / f"cfb_roster_{year}.json").read_text())
    resolver = PositionResolver(roster)
    profile = TeamDefenseProfile.for_season(year)

    n_games = Counter()
    for g in games.values():
        n_games[g["home_team"]] += 1
        n_games[g["away_team"]] += 1
    fbs = {t for t, n in n_games.items() if n >= 6}

    # the app's player index: (normalized full name, team) and (team, first initial, last name)
    by_full: dict[tuple, str] = {}
    positions: dict[str, str] = {}
    for r in roster:
        if r["position"] in APP_POSITIONS and r["team"] in fbs and r["first"] and r["last"]:
            key = f'P|{norm_name(r["first"] + " " + r["last"])}|{r["team"]}'
            by_full.setdefault((norm_name(r["first"] + " " + r["last"]), r["team"]), key)
            positions[key] = r["position"]
    by_initial: dict[tuple, set] = defaultdict(set)
    for (full, team), key in by_full.items():
        first, _, last = full.partition(" ")
        if first and last:
            by_initial[(team, first[0], last)].add(key)

    def asset_for_player(name: str, team: str) -> str | None:
        exact = by_full.get((norm_name(name), team))
        if exact is not None:
            return exact
        il = initial_last(name)
        found = by_initial.get((team, il[0], il[1])) if il else None
        return next(iter(found)) if found and len(found) == 1 else None

    plays_by_game = defaultdict(list)
    with gzip.open(CACHE / f"cfb_plays_{year}.jsonl.gz", "rt", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            plays_by_game[row["game_id"]].append(SimpleNamespace(**row))

    weeks: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    played: dict[int, set] = defaultdict(set)
    match = Counter()
    for gid, g in games.items():
        plays = plays_by_game.get(gid)
        if not plays or g["home_points"] is None or g["season_type"] != "regular":
            continue
        wk = g["week"]
        played[wk] |= {g["home_team"], g["away_team"]}
        result = process_plays(plays, resolver=resolver, team_profile=profile)
        for c in result.play_level_credits:
            a = abs(c.points)
            match["total"] += a
            if c.team not in fbs:
                match["fcs"] += a
                continue
            if c.position_group:
                key = f"D|{c.team}|{c.position_group}"
                positions[key] = c.position_group
            else:
                key = asset_for_player(c.player_name, c.team)
                if key is None:
                    match["unmatched_fbs"] += a
                    continue
            weeks[wk][key] += c.points
    return {
        "weeks": {str(w): dict(v) for w, v in weeks.items()},
        "played": {str(w): sorted(v & fbs) for w, v in played.items()},
        "positions": positions, "fbs_teams": sorted(fbs),
        "matching": {k: round(v, 1) for k, v in match.items()},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=[2022, 2023, 2024])
    args = ap.parse_args()
    for year in args.years:
        data = build_year(year)
        (CACHE / f"cfb_weekly_credits_{year}.json").write_text(json.dumps(data))
        m = data["matching"]
        print(f"{year}: {len(data['weeks'])} weeks, {len(data['positions'])} assets, "
              f"credit lost to FCS {m['fcs'] / m['total']:.1%}, unmatched FBS names {m.get('unmatched_fbs', 0) / m['total']:.1%}")


if __name__ == "__main__":
    main()
