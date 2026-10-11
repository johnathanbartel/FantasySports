"""
Score normalization and the bounded error model (docs/hockey_spec.md 6.2-6.3).

Model, in order:

  1. Shares.   λ_home = T * (O_home + p) / (O_home + O_away + 2p),  λ_away = T - λ_home
               T = TOTAL_GOALS, p = PRIOR_POINTS, O = offense from attribution.build_ledger.
               d = λ_home - λ_away = T * (O_home - O_away) / (O_home + O_away + 2p)
               d has the sign of the raw margin and grows monotonically with it.
  2. Error.    ε ~ N(0, σ²) truncated to [-C, C] (σ = NOISE_SIGMA, C = NOISE_CAP), drawn from a seeded stream.
               d' = d + ε, clamped to [-T, T].  λ'_home = (T + d')/2,  λ'_away = (T - d')/2.
  3. Rounding. goals = floor(λ' + 0.5) per side. Rounding is monotone, so goals_home >= goals_away whenever
               d' > 0 (and the reverse for d' < 0).
  4. Tie.      If the rounded goals are level, the side with the positive d' gets one deciding goal (d' = 0
               defers to the raw leader; a dead-level raw game with d' = 0 stays level).

Guarantees (checked by check_ordering and the audit script):

  * The scoreboard winner is the side with the larger d'. Because |ε| <= C, d' has the sign of d whenever
    |d| >= C, so a raw leader can only be upset when its expected margin |d| is below NOISE_CAP (1 goal).
  * Winning margin is monotone in the raw margin for a fixed ε.
  * Identical (offense, seed) always gives an identical result.
"""

from __future__ import annotations

import hashlib
import math
import random

from fantasy_sports.sports.hockey.simulator.model import (
    EPS, NOISE_CAP, NOISE_SIGMA, PRIOR_POINTS, TOTAL_GOALS, Ledger, Normalized, TieOutError,
)


def derive_rng(seed: int, daily_game_id: int, stream: str) -> random.Random:
    """An independent, reproducible stream per (seed, game, purpose). Uses SHA-256 so the stream does not
    depend on Python's string hashing."""
    digest = hashlib.sha256(f"{seed}|{daily_game_id}|{stream}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def truncated_normal(rng: random.Random, sigma: float, cap: float) -> float:
    """Box-Muller, redrawn until inside [-cap, cap] (at most 100 draws, then clipped). sigma 0 gives 0."""
    if sigma <= 0 or cap <= 0:
        return 0.0
    for _ in range(100):
        u1, u2 = rng.random(), rng.random()
        z = math.sqrt(-2.0 * math.log(max(u1, 1e-12))) * math.cos(2.0 * math.pi * u2)
        eps = sigma * z
        if abs(eps) <= cap:
            return eps
    return max(-cap, min(cap, eps))


def expected_margin(offense_home: float, offense_away: float) -> float:
    return TOTAL_GOALS * (offense_home - offense_away) / (offense_home + offense_away + 2.0 * PRIOR_POINTS)


def _round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def normalize(ledger: Ledger, rng: random.Random, sigma: float = NOISE_SIGMA, cap: float = NOISE_CAP) -> Normalized:
    d = expected_margin(ledger.offense["home"], ledger.offense["away"])
    eps = truncated_normal(rng, sigma, cap)
    d_noisy = max(-TOTAL_GOALS, min(TOTAL_GOALS, d + eps))
    lam_h, lam_a = (TOTAL_GOALS + d_noisy) / 2.0, (TOTAL_GOALS - d_noisy) / 2.0
    g_h, g_a = _round_half_up(lam_h), _round_half_up(lam_a)

    tiebreak = None
    if g_h == g_a:
        lead = d_noisy if abs(d_noisy) > EPS else d
        if abs(lead) > EPS:
            tiebreak = "home" if lead > 0 else "away"
            if tiebreak == "home":
                g_h += 1
            else:
                g_a += 1

    raw_leader = "home" if d > EPS else "away" if d < -EPS else None
    winner = "home" if g_h > g_a else "away" if g_a > g_h else None
    upset = raw_leader is not None and winner is not None and winner != raw_leader
    return Normalized(g_h, g_a, d, eps, d_noisy, lam_h, lam_a, tiebreak, upset, sigma, cap)


def check_ordering(norm: Normalized) -> None:
    """The documented bound on upsets. Raises TieOutError when violated."""
    if abs(norm.expected_margin) >= norm.cap - EPS:
        leader = "home" if norm.expected_margin > 0 else "away"
        winner = "home" if norm.home_goals > norm.away_goals else "away" if norm.away_goals > norm.home_goals else None
        if winner != leader:
            raise TieOutError(
                f"expected margin {norm.expected_margin:.3f} is outside the upset window "
                f"(cap {norm.cap}) but the {winner or 'nobody'} won")
    if norm.upset and abs(norm.expected_margin) >= norm.cap - EPS:
        raise TieOutError("an upset happened outside the upset window")
