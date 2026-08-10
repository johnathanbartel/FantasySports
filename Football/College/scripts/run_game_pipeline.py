"""
Step 4 of the NCAAF pipeline: run one real game through the whole chain -
raw plays -> ppa -> attributed player points -> team-week total - and
sanity-check the output against that game's real final score.

Scope note: this only attributes rushing and passing plays (the pieces
built so far). Field goals, punts, kickoffs, and defensive plays have no
per-play `ppa` from CFBD and aren't attributed to individual players yet -
kicker marginal-EP and defensive group attribution are separate, not-yet-
built pieces of the spec. The team-total comparison below is therefore
against offensive scrimmage plays only, not the final score outright - see
the printed caveat.

Usage:
    python run_game_pipeline.py --year 2024 --week 1 --team Georgia
"""

import argparse
import os
import re
from collections import defaultdict
from pathlib import Path

import cfbd
from dotenv import load_dotenv

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from Football.common.attribution import YacEpaLookup, split_passing_epa_incomplete

RUSH_RE = re.compile(r"^(?P<runner>.+?) run (?:for|to)\b")
PASS_COMPLETE_RE = re.compile(r"^(?P<passer>.+?) pass complete to (?P<receiver>.+?) for\b")
PASS_INCOMPLETE_RE = re.compile(r"^(?P<passer>.+?) pass incomplete(?: to (?P<receiver>.+))?$")
SACK_RE = re.compile(r"^(?P<passer>.+?) sacked by\b")
INTERCEPTION_RE = re.compile(r"^(?P<passer>.+?) pass intercepted\b")


def get_api_client() -> cfbd.ApiClient:
    load_dotenv()
    api_key = os.environ.get("CFBD_API_KEY")
    if not api_key:
        raise SystemExit("CFBD_API_KEY not set - see .env.example at the repo root.")
    return cfbd.ApiClient(cfbd.Configuration(access_token=api_key))


def attribute_play(play, lookup: YacEpaLookup, credits: dict, unattributed: list):
    """Adds this play's ppa credit into `credits` (player -> team, points),
    or appends it to `unattributed` if we don't have a parser rule for it."""
    ppa = play.ppa
    play_type = play.play_type
    text = play.play_text or ""

    if play_type in ("Rush", "Rushing Touchdown"):
        m = RUSH_RE.match(text)
        if not m:
            unattributed.append(play)
            return
        credits[(m["runner"], play.offense)] += ppa

    elif play_type in ("Pass Reception", "Passing Touchdown"):
        m = PASS_COMPLETE_RE.match(text)
        if not m:
            unattributed.append(play)
            return
        split = lookup.split(ppa, play.yards_gained)
        credits[(m["passer"], play.offense)] += split.qb_credit
        credits[(m["receiver"], play.offense)] += split.receiver_credit

    elif play_type == "Pass Incompletion":
        m = PASS_INCOMPLETE_RE.match(text)
        if not m:
            unattributed.append(play)
            return
        split = split_passing_epa_incomplete(ppa)
        credits[(m["passer"], play.offense)] += split.qb_credit

    elif play_type == "Sack":
        m = SACK_RE.match(text)
        if not m:
            unattributed.append(play)
            return
        # v1: full (negative) credit to the QB. The sacking defender's
        # positive mirror credit is part of the not-yet-built defensive
        # group attribution.
        credits[(m["passer"], play.offense)] += ppa

    elif play_type == "Pass Interception Return":
        m = INTERCEPTION_RE.match(text)
        if not m:
            unattributed.append(play)
            return
        # v1: full (negative) credit to the QB, same caveat as sacks.
        credits[(m["passer"], play.offense)] += ppa

    else:
        unattributed.append(play)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--week", type=int, default=1)
    parser.add_argument("--team", type=str, default="Georgia")
    args = parser.parse_args()

    client = get_api_client()
    games_api = cfbd.GamesApi(client)
    plays_api = cfbd.PlaysApi(client)

    games = games_api.get_games(year=args.year, week=args.week, team=args.team)
    if not games:
        raise SystemExit("No game found for that year/week/team.")
    game = games[0]

    plays = plays_api.get_plays(
        year=args.year, week=args.week, season_type="regular", team=args.team
    )

    lookup = YacEpaLookup()
    credits: dict[tuple[str, str], float] = defaultdict(float)
    unattributed: list = []
    scrimmage_ppa_by_team: dict[str, float] = defaultdict(float)

    for play in plays:
        if play.ppa is None:
            continue
        scrimmage_ppa_by_team[play.offense] += play.ppa
        attribute_play(play, lookup, credits, unattributed)

    print(f"{game.away_team} {game.away_points} @ {game.home_team} {game.home_points}")
    print(f"({len(plays)} plays pulled, {len(unattributed)} scrimmage plays with ppa "
          f"but no attribution-parser match)\n")

    print("Player fantasy points (rushing + passing attribution only):")
    for (player, team), pts in sorted(credits.items(), key=lambda kv: -kv[1]):
        print(f"  {pts:+6.2f}  {player:<22} {team}")

    print("\nTeam scrimmage-play ppa sum vs. real final score:")
    real_score = {game.home_team: game.home_points, game.away_team: game.away_points}
    for team, total_ppa in scrimmage_ppa_by_team.items():
        print(f"  {team:<20} scrimmage ppa sum = {total_ppa:+6.2f}   real score = {real_score.get(team)}")

    print(
        "\nCaveat: scrimmage ppa sum excludes field goals, punts, kickoffs, and "
        "defensive/return scores (CFBD gives no per-play ppa for those, and "
        "kicker marginal-EP / defensive group attribution aren't built yet), "
        "so it will systematically undercount teams that scored via those "
        "paths. This checks whether the rush/pass EP model and attribution "
        "logic are sane, not the full-score validation from the spec yet."
    )

    if unattributed:
        print(f"\n{len(unattributed)} unattributed scrimmage plays (regex didn't match):")
        for p in unattributed[:10]:
            print(f"  {p.play_type}: {p.play_text}")


if __name__ == "__main__":
    main()
