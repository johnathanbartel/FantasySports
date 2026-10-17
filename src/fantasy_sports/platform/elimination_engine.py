"""Exact mathematical elimination: which teams can no longer make the playoffs, or a first-round bye, whatever happens in the
games still to play.

Pure and sport-neutral. It works on a Snapshot (the teams, the games played, the games still to play, and the league's standings
and playoff rules); platform/elimination.py builds a Snapshot for each sport and writes the result.

How a team T is judged
----------------------
T is alive when SOME way the remaining games can finish puts T in the field. Looking for that way:
  * T wins all of its own remaining games. That is never worse for T (more points for T, fewer for its opponents, a better
    head-to-head and division record), so if any finish works, one with T winning everything does.
  * The other remaining games are searched exhaustively (depth first, with memoisation and bound pruning). A game can end in a
    win for either side or, where the sport allows, a tie. The search stops at once when even the worst case for T still lets it
    in, or when the best case already shuts it out.
  * At the end of a finish, the teams level with T on the primary number (points or win %) are separated with the league's
    tiebreakers, one at a time, in the commissioner's order.

What counts as proof
--------------------
A team is eliminated only when NO finish lets it in. Wherever the outcome depends on something this module cannot decide from
who-beat-whom alone, the question is left open in T's favour, so a team is never eliminated by mistake:
  * Margin tiebreakers (goal differential, goals scored/allowed) are open for a pair while either team still has a game left,
    because the score of that game is not known. They are exact once both teams are done.
  * Head-to-head is exact for any size of tie (wins over the other tied teams, counting the games still to be played) as long as
    every team that could be in the tie is known; when an open margin question leaves the group uncertain it stays open.
  * Division record applies only when every tied team shares a division, otherwise it is open.
  * The random draw is open until the last game of the league has been played. A tie that every configured tiebreaker has
    settled as dead level falls to team id, which is how the league's own ranking breaks it.
Once no game is left anywhere, the league's own ranking and seeding decide, so the answer matches the standings exactly.
If the search is cut off by the node budget the team is reported undetermined and stays alive; it is re-checked after the next
game, so a hard case can only delay an elimination, never cause a wrong one.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from fantasy_sports.platform import playoffs
from fantasy_sports.platform import series_standings as ss
from fantasy_sports.platform import tiebreakers as tb

DEFAULT_NODE_BUDGET = 3_000_000


@dataclass(frozen=True)
class Result:
    """A game that has been played."""
    home: int
    away: int
    home_score: float
    away_score: float


@dataclass(frozen=True)
class Fixture:
    """A game still to play."""
    home: int
    away: int


@dataclass
class Snapshot:
    team_ids: list[int]
    division_of: dict[int, int | None]
    results: list[Result]
    remaining: list[Fixture]
    standings: ss.StandingsRules
    playoffs: playoffs.PlayoffRules
    ties_possible: bool = True              # whether a game can end level (hockey: yes)


@dataclass
class Report:
    eliminated: set[int] = field(default_factory=set)          # cannot make the playoff field
    bye_eliminated: set[int] = field(default_factory=set)      # cannot reach a bye (includes the eliminated); empty if no byes
    undetermined: set[int] = field(default_factory=set)        # the search ran out of budget: treated as alive


class _OutOfBudget(Exception):
    pass


def analyze(snap: Snapshot, node_budget: int = DEFAULT_NODE_BUDGET) -> Report:
    if len(snap.team_ids) < 2:
        return Report()
    if not snap.remaining:
        return _settled(snap)
    base = _Base(snap)
    report = Report()
    for t in snap.team_ids:
        alive = _can_achieve(base, t, False, node_budget)
        if alive is None:
            report.undetermined.add(t)
            continue
        if not alive:
            report.eliminated.add(t)
            if snap.playoffs.byes > 0:
                report.bye_eliminated.add(t)
            continue
        if snap.playoffs.byes > 0 and _can_achieve(base, t, True, node_budget) is False:
            report.bye_eliminated.add(t)
    return report


def _settled(snap: Snapshot) -> Report:
    """No game left: the league's own ranking and seeding give the answer."""
    rows = {t: ss.TeamStanding(t) for t in snap.team_ids}
    pairs: list[tuple[int, int]] = []
    div = snap.division_of
    for g in snap.results:
        same = div.get(g.home) is not None and div.get(g.home) == div.get(g.away)
        for tid, gf, ga in ((g.home, g.home_score, g.away_score), (g.away, g.away_score, g.home_score)):
            r = rows.get(tid)
            if r is None:
                continue
            r.goals_for += gf
            r.goals_against += ga
            name = "wins" if gf > ga else "losses" if gf < ga else "ties"
            setattr(r, name, getattr(r, name) + 1)
            if same:
                setattr(r, "div_" + name, getattr(r, "div_" + name) + 1)
        if g.home_score != g.away_score:
            pairs.append((g.home, g.away) if g.home_score > g.away_score else (g.away, g.home))
    rules = snap.standings
    if rules.division_of is None:
        rules = ss.StandingsRules(rules.mode, rules.points_win, rules.points_tie, rules.tiebreakers, div, rules.draw_seed)
    ordered = ss.rank_standings(rows, pairs, rules)
    field_ = playoffs.seed_teams([r.team_id for r in ordered], div, snap.playoffs)
    in_field = {f["team_id"] for f in field_}
    has_bye = {f["team_id"] for f in field_ if f["bye"]}
    out = set(snap.team_ids) - in_field
    return Report(eliminated=out, bye_eliminated=(set(snap.team_ids) - has_bye) if snap.playoffs.byes > 0 else set())


class _Base:
    """Everything about the league that does not change while the remaining games are searched."""

    def __init__(self, snap: Snapshot):
        self.snap = snap
        self.ids = list(snap.team_ids)
        self.n = len(self.ids)
        idx = {t: i for i, t in enumerate(self.ids)}
        n = self.n
        rules = snap.standings
        self.keys = tuple(rules.tiebreakers)
        pct = rules.mode != "points"
        self.win_u = 2 if pct else rules.points_win         # what a win / a tie is worth in the primary number
        self.tie_u = 1 if pct else rules.points_tie
        self.val = [0] * n
        self.wins = [0] * n
        self.gf = [0.0] * n
        self.ga = [0.0] * n
        self.dn = [0] * n                                   # division record in half-wins (win 2, tie 1)
        self.dg = [0] * n                                   # division games, played and still to play
        self.h2h = [[0] * n for _ in range(n)]              # h2h[a][b] = wins by a over b
        played = [0] * n
        self.div = [snap.division_of.get(t) for t in self.ids]
        for g in snap.results:
            a, b = idx.get(g.home), idx.get(g.away)
            if a is None or b is None:
                continue
            same = self.div[a] is not None and self.div[a] == self.div[b]
            played[a] += 1
            played[b] += 1
            self.gf[a] += g.home_score
            self.ga[a] += g.away_score
            self.gf[b] += g.away_score
            self.ga[b] += g.home_score
            if same:
                self.dg[a] += 1
                self.dg[b] += 1
            if g.home_score > g.away_score:
                self._win(a, b, same)
            elif g.away_score > g.home_score:
                self._win(b, a, same)
            else:
                self.val[a] += self.tie_u
                self.val[b] += self.tie_u
                if same:
                    self.dn[a] += 1
                    self.dn[b] += 1
        self.fixtures: list[tuple[int, int, bool]] = []
        self.has_rem = [False] * n
        for f in snap.remaining:
            a, b = idx.get(f.home), idx.get(f.away)
            if a is None or b is None:
                continue
            same = self.div[a] is not None and self.div[a] == self.div[b]
            self.fixtures.append((a, b, same))
            self.has_rem[a] = self.has_rem[b] = True
            played[a] += 1
            played[b] += 1
            if same:
                self.dg[a] += 1
                self.dg[b] += 1
        # The primary number is compared across teams as val / den (den differs only when win % is used on uneven schedules).
        self.den = [max(1, 2 * played[i]) if pct else 1 for i in range(n)]
        self.dg = [max(1, g) for g in self.dg]
        self.tid = self.ids
        self.ndiv = len({d for d in self.div if d is not None})

    def _win(self, w: int, l: int, same: bool) -> None:
        self.val[w] += self.win_u
        self.wins[w] += 1
        self.h2h[w][l] += 1
        if same:
            self.dn[w] += 2


def _can_achieve(base: _Base, t: int, need_bye: bool, node_budget: int) -> bool | None:
    """Whether team index-by-id t can still make the field (or earn a bye). None = undetermined (out of budget)."""
    t = base.ids.index(t)
    n = base.n
    W, TU = base.win_u, base.tie_u
    den = base.den
    val = list(base.val)
    wins = list(base.wins)
    dn = list(base.dn)
    h2h_t_over = [0] * n            # T's wins over each team from here on
    others: list[tuple[int, int, bool]] = []
    for a, b, same in base.fixtures:
        if t in (a, b):
            o = b if a == t else a
            val[t] += W
            wins[t] += 1
            h2h_t_over[o] += 1
            if same:
                dn[t] += 2
        else:
            others.append((a, b, same))
    F, Dt = val[t], den[t]

    rem = [0] * n
    for a, b, _ in others:
        rem[a] += W
        rem[b] += W
    relevant = [x != t and (val[x] + rem[x]) * Dt >= F * den[x] for x in range(n)]
    fx = [f for f in others if relevant[f[0]] or relevant[f[1]]]
    rem = [0] * n
    for a, b, _ in fx:
        rem[a] += W
        rem[b] += W

    rules = base.snap.playoffs
    N, B = rules.teams, rules.byes
    qualify, first = rules.division_winners_qualify, rules.division_winners_seeded_first
    div = base.div
    tdiv = div[t]
    wt = min(base.ndiv, N)

    def ok(ahead: list[int]) -> bool:
        size = len(ahead)
        if not qualify:
            in_field, seed = size < N, size + 1
        else:
            hit = {div[x] for x in ahead if div[x] is not None}
            if tdiv is not None and tdiv not in hit:        # T leads its division
                others_ahead = len(hit)
                in_field = others_ahead < N
                seed = others_ahead + 1 if first else size + 1
            else:
                nonwin = size - len(hit)
                in_field = nonwin < N - wt
                seed = wt + 1 + nonwin if first else size + 1
        return in_field and (not need_bye or seed <= B)

    keys = base.keys
    games_left = bool(base.fixtures)
    uses_wins, uses_div = "wins" in keys, "division_record" in keys
    has_rem = base.has_rem
    gf, ga = base.gf, base.ga
    h2h = [row[:] for row in base.h2h]          # updated as the search plays out the other teams' games
    uses_h2h = "head_to_head" in keys
    fx_pairs = sorted({(a, b) for a, b, _ in fx} | {(b, a) for a, b, _ in fx})

    def ratio_cmp(n1: int, d1: int, n2: int, d2: int) -> int:
        x, y = n1 * d2, n2 * d1
        return (x > y) - (x < y)

    def tied_ahead(group: list[int]) -> list[int]:
        """Of the teams level with T on the primary number, those that finish definitely ahead of it."""
        ahead: list[int] = []
        opened: list[int] = []              # open questions: not counted as ahead (T's favour), but they may still be in the tie
        for key in keys:
            if not group:
                break
            kind = tb.TIEBREAKERS[key].kind
            if kind == tb.DRAW:
                if games_left:
                    opened += group
                else:
                    seed = base.snap.standings.draw_seed
                    dt = tb.draw_value(seed, base.tid[t])
                    ahead += [x for x in group if tb.draw_value(seed, base.tid[x]) > dt]
                group = []
                break
            if kind == tb.MARGIN:
                nxt = []
                for x in group:
                    if has_rem[t] or has_rem[x]:
                        opened.append(x)
                        continue
                    if key == "goal_differential":
                        c = ((gf[x] - ga[x]) > (gf[t] - ga[t])) - ((gf[x] - ga[x]) < (gf[t] - ga[t]))
                    elif key == "goals_for":
                        c = (gf[x] > gf[t]) - (gf[x] < gf[t])
                    else:
                        c = (ga[x] < ga[t]) - (ga[x] > ga[t])
                    if c > 0:
                        ahead.append(x)
                    elif c == 0:
                        nxt.append(x)
                group = nxt
            elif key == "wins":
                nxt = []
                for x in group:
                    if wins[x] > wins[t]:
                        ahead.append(x)
                    elif wins[x] == wins[t]:
                        nxt.append(x)
                group = nxt
            elif key == "head_to_head":
                if opened:                                  # the tied group is not known for sure yet
                    opened += group
                    group = []
                    break
                members = group + [t]

                def inside(x: int) -> int:
                    """Wins over the other tied teams, counting the games T is going to win."""
                    wins_over = sum(h2h[x][y] for y in members if y != x)
                    return wins_over + (sum(h2h_t_over[y] for y in members if y != t) if x == t else 0)

                vt = inside(t)
                nxt = []
                for x in group:
                    vx = inside(x)
                    if vx > vt:
                        ahead.append(x)
                    elif vx == vt:
                        nxt.append(x)
                group = nxt
            elif key == "division_record":
                divisions = {div[i] for i in group + opened + [t]}
                if len(divisions) != 1 or None in divisions:
                    opened += group
                    group = []
                    break
                nxt = []
                for x in group:
                    c = ratio_cmp(dn[x], base.dg[x], dn[t], base.dg[t])
                    if c > 0:
                        ahead.append(x)
                    elif c == 0:
                        nxt.append(x)
                group = nxt
        if group:                           # level on every tiebreaker, all of them settled: the league's final order is by team id
            ahead += [x for x in group if base.tid[x] < base.tid[t]]
        return ahead

    def leaf_ahead() -> list[int]:
        ahead, tied = [], []
        for x in range(n):
            if x == t:
                continue
            c = val[x] * Dt - F * den[x]
            if c > 0:
                ahead.append(x)
            elif c == 0:
                tied.append(x)
        if tied:
            ahead += tied_ahead(tied)
        return ahead

    nodes = 0
    failed: set = set()
    count = len(fx)
    old_limit = sys.getrecursionlimit()
    if count + 200 > old_limit:
        sys.setrecursionlimit(count + 500)

    def dfs(i: int) -> bool:
        nonlocal nodes
        nodes += 1
        if nodes > node_budget:
            raise _OutOfBudget
        sure = [x for x in range(n) if x != t and val[x] * Dt > F * den[x]]
        if not ok(sure):
            return False
        possible = [x for x in range(n) if x != t and (val[x] + rem[x]) * Dt >= F * den[x]]
        if ok(possible):
            return True
        if i == count:
            return ok(leaf_ahead())
        key = (i, tuple(val), tuple(wins) if uses_wins else None, tuple(dn) if uses_div else None,
               tuple(h2h[a][b] for a, b in fx_pairs) if uses_h2h else None)
        if key in failed:
            return False
        a, b, same = fx[i]
        rem[a] -= W
        rem[b] -= W
        ra = val[a] / den[a] if relevant[a] else -1.0
        rb = val[b] / den[b] if relevant[b] else -1.0
        order = ("a", "tie", "b") if ra <= rb else ("b", "tie", "a")
        found = False
        for outcome in order:
            if outcome == "tie":
                if not base.snap.ties_possible:
                    continue
                val[a] += TU
                val[b] += TU
                if same:
                    dn[a] += 1
                    dn[b] += 1
                found = dfs(i + 1)
                val[a] -= TU
                val[b] -= TU
                if same:
                    dn[a] -= 1
                    dn[b] -= 1
            else:
                w = a if outcome == "a" else b
                lo = b if w == a else a
                val[w] += W
                wins[w] += 1
                h2h[w][lo] += 1
                if same:
                    dn[w] += 2
                found = dfs(i + 1)
                val[w] -= W
                wins[w] -= 1
                h2h[w][lo] -= 1
                if same:
                    dn[w] -= 2
            if found:
                break
        rem[a] += W
        rem[b] += W
        if not found:
            failed.add(key)
        return found

    try:
        return dfs(0)
    except _OutOfBudget:
        return None
    finally:
        sys.setrecursionlimit(old_limit)
