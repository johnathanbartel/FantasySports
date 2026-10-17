"""Playoff probability: how likely each team is to make the playoff field (and to earn a bye), by simulating the rest of the season.

This is an estimate and nothing else. It never marks a team eliminated: elimination stays the exact proof in
platform/elimination_engine.py, and a team with a 0.0% estimate is still active until that proof says otherwise.

How it works
------------
1. Pythagorean record. Each team's strength is  GF^x / (GF^x + GA^x)  from its goals scored and against so far. Because few games
   are played early on, each team is first padded with PRIOR_GAMES games of league-average scoring, which pulls an early-season
   record toward .500 and keeps a team with no goals against from being rated a sure thing.
2. Win probability (log5, Bill James). For teams with strengths a and b, the chance A beats B is
       (a - a*b) / (a + b - 2*a*b)
   No home advantage is modelled. A share of games end level in sports that allow it (hockey); that share is the league's own rate
   so far, and the rest of the probability is split between the two sides by log5.
3. Simulate. Every unplayed game is drawn, the league's own ranking (points or win %, then its tiebreakers) and playoff seeding
   are applied to the finished season, and the share of simulated seasons in which a team makes the field is its probability.
   Goals are drawn too, because goal tiebreakers need them: the loser scores a Poisson number of goals around its expected rate and
   the winner adds a margin of at least one.

Pure and sport-neutral: it works on the same Snapshot as the elimination proof, so a sport gains playoff odds by getting an adapter
in platform/elimination.py.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from fantasy_sports.platform import elimination_engine as engine
from fantasy_sports.platform import playoffs
from fantasy_sports.platform import series_standings as ss

DEFAULT_SIMULATIONS = 2000
PYTHAGOREAN_EXPONENT = 2.0      # hockey scoring runs best near 2; baseball is ~1.83, football ~2.37, basketball ~14
PRIOR_GAMES = 10                # games of league-average scoring each team starts with
WIN_MARGIN_MEAN = 0.8           # the winner's margin is 1 + Poisson(this)


@dataclass
class TeamOdds:
    team_id: int
    games_played: int = 0
    goals_for: float = 0.0
    goals_against: float = 0.0
    pythagorean: float = 0.5            # raw record from goals so far (0.5 before any games)
    strength: float = 0.5               # pythagorean after the prior
    playoff: float = 0.0                # share of simulated seasons in the field
    bye: float = 0.0                    # share with a first-round bye
    mean_points: float = 0.0            # mean value of the league's primary number (points or win %) at season's end
    eliminated: bool = False            # filled in by the caller from the exact proof, not by this estimate


@dataclass
class OddsReport:
    simulations: int
    exponent: float
    tie_rate: float
    teams: dict[int, TeamOdds] = field(default_factory=dict)


def pythagorean(goals_for: float, goals_against: float, exponent: float = PYTHAGOREAN_EXPONENT) -> float:
    """GF^x / (GF^x + GA^x); .500 when neither side has scored."""
    if goals_for <= 0 and goals_against <= 0:
        return 0.5
    f, a = goals_for ** exponent, goals_against ** exponent
    return f / (f + a)


def log5(a: float, b: float) -> float:
    """Probability that a team of strength a beats one of strength b (Bill James' log5)."""
    d = a + b - 2 * a * b
    return 0.5 if d <= 0 else (a - a * b) / d


def team_strengths(snap: engine.Snapshot, exponent: float = PYTHAGOREAN_EXPONENT, prior_games: int = PRIOR_GAMES
                   ) -> dict[int, TeamOdds]:
    """Each team's record so far and its strength after padding with league-average games."""
    rows = {t: TeamOdds(t) for t in snap.team_ids}
    for g in snap.results:
        for tid, gf, ga in ((g.home, g.home_score, g.away_score), (g.away, g.away_score, g.home_score)):
            r = rows.get(tid)
            if r is not None:
                r.games_played += 1
                r.goals_for += gf
                r.goals_against += ga
    played = sum(r.games_played for r in rows.values())
    league_avg = (sum(r.goals_for for r in rows.values()) / played) if played else 1.0
    for r in rows.values():
        r.pythagorean = pythagorean(r.goals_for, r.goals_against, exponent)
        r.strength = pythagorean(r.goals_for + prior_games * league_avg, r.goals_against + prior_games * league_avg, exponent)
    return rows


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def simulate(snap: engine.Snapshot, simulations: int = DEFAULT_SIMULATIONS, seed: int | None = None,
             exponent: float = PYTHAGOREAN_EXPONENT, prior_games: int = PRIOR_GAMES) -> OddsReport:
    """Plays the rest of the season `simulations` times and reports each team's playoff and bye share. A fixed seed repeats exactly."""
    rng = random.Random(seed)
    odds = team_strengths(snap, exponent, prior_games)
    n_played = len(snap.results)
    tie_rate = (sum(1 for g in snap.results if g.home_score == g.away_score) / n_played) if (snap.ties_possible and n_played) else 0.0
    report = OddsReport(simulations, exponent, tie_rate, odds)
    if len(snap.team_ids) < 2 or simulations < 1:
        return report

    rules = snap.standings
    division_of = snap.division_of
    if rules.division_of is None:
        rules = ss.StandingsRules(rules.mode, rules.points_win, rules.points_tie, rules.tiebreakers, division_of, rules.draw_seed)

    # Games already played, as the template every simulated season starts from.
    base = {t: ss.TeamStanding(t) for t in snap.team_ids}
    base_pairs: list[tuple[int, int]] = []
    for g in snap.results:
        same = division_of.get(g.home) is not None and division_of.get(g.home) == division_of.get(g.away)
        for tid, gf, ga in ((g.home, g.home_score, g.away_score), (g.away, g.away_score, g.home_score)):
            r = base.get(tid)
            if r is None:
                continue
            r.goals_for += gf
            r.goals_against += ga
            name = "wins" if gf > ga else "losses" if gf < ga else "ties"
            setattr(r, name, getattr(r, name) + 1)
            if same:
                setattr(r, "div_" + name, getattr(r, "div_" + name) + 1)
        if g.home_score != g.away_score:
            base_pairs.append((g.home, g.away) if g.home_score > g.away_score else (g.away, g.home))

    league_avg = (sum(r.goals_for for r in base.values()) / max(1, 2 * n_played)) if n_played else 3.0
    # Per-game expected goals for each side, from the team's own rate against the league rate (padded like the strengths).
    scored = {t: (odds[t].goals_for + prior_games * league_avg) / (odds[t].games_played + prior_games) for t in snap.team_ids}
    allowed = {t: (odds[t].goals_against + prior_games * league_avg) / (odds[t].games_played + prior_games) for t in snap.team_ids}
    avg = league_avg if league_avg > 0 else 1.0

    plan = []   # (home, away, p_home_win, p_tie, same_division, lam_home, lam_away)
    for f in snap.remaining:
        if f.home not in base or f.away not in base:
            continue
        p_win = log5(odds[f.home].strength, odds[f.away].strength) * (1 - tie_rate)
        same = division_of.get(f.home) is not None and division_of.get(f.home) == division_of.get(f.away)
        plan.append((f.home, f.away, p_win, tie_rate, same, scored[f.home] * allowed[f.away] / avg,
                     scored[f.away] * allowed[f.home] / avg))

    in_field = dict.fromkeys(snap.team_ids, 0)
    with_bye = dict.fromkeys(snap.team_ids, 0)
    primary_sum = dict.fromkeys(snap.team_ids, 0.0)
    for _ in range(simulations):
        rows = {t: _copy(r) for t, r in base.items()}
        pairs = list(base_pairs)
        for home, away, p_win, p_tie, same, lam_h, lam_a in plan:
            u = rng.random()
            if u < p_tie:
                hs = as_ = _poisson(rng, (lam_h + lam_a) / 2)
            elif u < p_tie + p_win:
                as_ = _poisson(rng, lam_a)
                hs = as_ + 1 + _poisson(rng, WIN_MARGIN_MEAN)
            else:
                hs = _poisson(rng, lam_h)
                as_ = hs + 1 + _poisson(rng, WIN_MARGIN_MEAN)
            for tid, gf, ga in ((home, hs, as_), (away, as_, hs)):
                r = rows[tid]
                r.goals_for += gf
                r.goals_against += ga
                name = "wins" if gf > ga else "losses" if gf < ga else "ties"
                setattr(r, name, getattr(r, name) + 1)
                if same:
                    setattr(r, "div_" + name, getattr(r, "div_" + name) + 1)
            if hs != as_:
                pairs.append((home, away) if hs > as_ else (away, home))
        ordered = ss.rank_standings(rows, pairs, rules)
        for r in ordered:
            primary_sum[r.team_id] += r.primary or 0.0
        for f in playoffs.seed_teams([r.team_id for r in ordered], division_of, snap.playoffs):
            in_field[f["team_id"]] += 1
            if f["bye"]:
                with_bye[f["team_id"]] += 1
    for t, o in odds.items():
        o.playoff = in_field[t] / simulations
        o.bye = with_bye[t] / simulations
        o.mean_points = primary_sum[t] / simulations
    return report


def _copy(r: ss.TeamStanding) -> ss.TeamStanding:
    return ss.TeamStanding(r.team_id, r.wins, r.losses, r.ties, r.goals_for, r.goals_against,
                           div_wins=r.div_wins, div_losses=r.div_losses, div_ties=r.div_ties)
