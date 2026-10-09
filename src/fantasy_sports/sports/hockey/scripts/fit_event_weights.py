"""
Estimate the goals-equivalent value of a hit (pipeline.HIT_CREDIT) from the
data, in the spirit of football/professional/scripts/fit_defense_weights.py.

Method: for every hit, take the hitting team's net expected goals (own xG
minus the opponent's xG, MoneyPuck pre-shot xG) over the WINDOW seconds
after the hit, and subtract that team's average net xG over the same-length
windows of the whole game (so strong teams do not look good for hitting).
The mean difference is the value of a hit in goals. A hit earns credit only
if its mean effect is positive and more than two standard errors from zero;
otherwise HIT_CREDIT stays 0.0 and hits are reported as a count only.

Reads only. Usage:
    DATABASE_URL=sqlite:///./copy.db python fit_event_weights.py [--window 10]
"""

import argparse
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from fantasy_sports.platform.db.models.core import Game
from fantasy_sports.platform.db.session import SessionLocal
from fantasy_sports.sports.hockey.models import GameEvent, Shot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, default=10, help="seconds after a hit to measure")
    args = parser.parse_args()

    with SessionLocal() as db:
        hits = defaultdict(list)
        for e in db.query(GameEvent).filter(GameEvent.event_type == "hit"):
            if e.game_seconds is not None and e.team_id is not None:
                hits[e.game_id].append((e.game_seconds, e.team_id))
        shots = defaultdict(list)
        for s in db.query(Shot).filter(Shot.game_id.in_(list(hits))):
            try:
                t = int(s.clock)
            except (TypeError, ValueError):
                continue
            shots[s.game_id].append((t, s.shooting_team_id, s.pre_shot_xg or 0.0))
        games = {g.id: g for g in db.query(Game).filter(Game.id.in_(list(hits)))}

    effects = []
    for game_id, game_hits in hits.items():
        game = games[game_id]
        game_shots = shots.get(game_id, [])
        length = max([t for t, _, _ in game_shots] + [3600]) or 3600
        totals = defaultdict(float)  # team -> net xG over the whole game
        for t, team, xg in game_shots:
            for side in (game.home_team_id, game.away_team_id):
                totals[side] += xg if team == side else -xg
        for t, team in game_hits:
            net = 0.0
            for st, steam, xg in game_shots:
                if t < st <= t + args.window:
                    net += xg if steam == team else -xg
            baseline = totals[team] * args.window / length
            effects.append(net - baseline)

    n = len(effects)
    mean = statistics.mean(effects)
    se = statistics.stdev(effects) / math.sqrt(n)
    print(f"{n} hits, window {args.window}s")
    print(f"Mean net xG swing after a hit vs team baseline: {mean:+.5f} goals (SE {se:.5f}, t = {mean / se:+.2f})")
    credit = mean if mean > 2 * se else 0.0
    print(f"Fitted HIT_CREDIT = {credit:.5f}"
          + ("" if credit else "  (effect not distinguishable from zero or negative: hits earn no credit)"))


if __name__ == "__main__":
    main()
