"""
Draft grades and the initial power rankings (NHL), both built on projected points (platform/hockey_projection.py).

DRAFT GRADE. Two equal parts, each turned into a z-score across the league's teams:

  * HAUL   the projected points of the best lineup the team's picks can field (1 C, 2 W, 2 D, 1 G, depth covering games a
           starter is projected to miss) - "who drafted the most projected points".
  * VALUE  the sum over the team's picks of (the player's projected points - what a pick at that number is expected to
           return). The expectation is the projection board: every NHL player projected, best first, smoothed over
           half a round on each side, read at the pick number. Take the board's best remaining player every time and you
           score about zero; reach and you lose, find a player the board has far lower and you win.

    score = 0.5 * z(haul) + 0.5 * z(value)           letter from the score (LETTERS)

A grade is relative: it says how a team did against the rest of its own league, so an average draft is a B-/C+ and the
league's letters spread from A to F. Position is not adjusted - a point is a point - so the grade follows the lineup rule
for the haul but the value part can reward taking a goalie early if goalies project high. Snake drafts only (the pick number
is what the value is measured against).

PICK SUGGESTIONS. On the clock, the three best available players in a position the team still needs, ranked by projected points
above what the board expects at this pick number (so the same ranking as grading would give each pick). Need: a group short of
its starters (1 C, 2 W, 2 D, 1 G) first; once the starters are filled, a group short of its depth target (4 C, 8 W, 8 D, 4 G,
the four lines of docs/hockey_spec.md section 4); once those are filled too, every group.

POWER RANKINGS. Teams ordered by the projected points of the best lineup their current roster can field (the same HAUL
measure, on rosters instead of picks). Rank 1 is the highest. Nothing about results enters them: this is the starting point
before any game is played.

Pure functions first, then the database wrappers. No scoring or attribution code is touched.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import (
    Draft, DraftPick, DraftableAsset, LeagueUniverse, RosterEntry, Team, Universe,
)
from fantasy_sports.platform.hockey_projection import STARTERS, LineupProjection, Projection, lineup_projection, project_pool
from fantasy_sports.platform.pecking_order import GROUP_BY_POSITION

# (minimum score, letter), best first
LETTERS = [(1.4, "A+"), (1.0, "A"), (0.7, "A-"), (0.4, "B+"), (0.15, "B"), (-0.1, "B-"), (-0.35, "C+"), (-0.6, "C"),
           (-0.85, "C-"), (-1.1, "D+"), (-1.4, "D")]
BEST_PICKS = 3
DEPTH_TARGET = {"C": 4, "W": 8, "D": 8, "G": 4}
SUGGESTIONS = 3


def letter_for(score: float) -> str:
    for floor, letter in LETTERS:
        if score >= floor:
            return letter
    return "F"


def zscores(values: list[float]) -> list[float]:
    """Each value's distance from the mean in standard deviations; all zeros when the values are all equal."""
    sd = statistics.pstdev(values) if len(values) > 1 else 0.0
    mean = statistics.fmean(values) if values else 0.0
    return [(v - mean) / sd if sd > 1e-12 else 0.0 for v in values]


def expected_by_pick(board: list[float], n_picks: int, half_window: int) -> list[float]:
    """Expected projected points at overall pick 1..n_picks: the board (points, best first) averaged over `half_window`
    picks either side. Past the end of the board the last value repeats; an empty board expects 0."""
    if not board:
        return [0.0] * n_picks
    out = []
    for n in range(1, n_picks + 1):
        i = min(n - 1, len(board) - 1)
        window = board[max(0, i - half_window): i + half_window + 1]
        out.append(statistics.fmean(window))
    return out


@dataclass
class PickResult:
    pick_number: int
    round: int
    team_id: int
    asset_id: int
    projected_points: float
    expected_points: float

    @property
    def value(self) -> float:
        return self.projected_points - self.expected_points


def grade_teams(picks: list[PickResult], projections: dict[int, Projection], team_ids: list[int]) -> list[dict]:
    """The graded teams, best first. `picks` are the made picks; a team that made none is graded on nothing (haul 0)."""
    haul: dict[int, LineupProjection] = {}
    value: dict[int, float] = {t: 0.0 for t in team_ids}
    mine: dict[int, list[PickResult]] = {t: [] for t in team_ids}
    for pk in picks:
        mine.setdefault(pk.team_id, []).append(pk)
        value[pk.team_id] = value.get(pk.team_id, 0.0) + pk.value
    for t in mine:
        haul[t] = lineup_projection([projections[p.asset_id] for p in mine[t] if p.asset_id in projections])
    ids = list(team_ids)
    z_haul = dict(zip(ids, zscores([haul[t].total for t in ids])))
    z_value = dict(zip(ids, zscores([value[t] for t in ids])))
    rows = []
    for t in ids:
        score = 0.5 * z_haul[t] + 0.5 * z_value[t]
        ordered = sorted(mine[t], key=lambda p: p.value, reverse=True)
        rows.append({"team_id": t, "score": round(score, 3), "grade": letter_for(score),
                     "haul_points": round(haul[t].total, 1), "value_points": round(value[t], 1),
                     "haul_z": round(z_haul[t], 2), "value_z": round(z_value[t], 2),
                     "picks": len(mine[t]), "best_picks": [p.pick_number for p in ordered[:BEST_PICKS]],
                     "worst_picks": [p.pick_number for p in ordered[-BEST_PICKS:][::-1]] if len(ordered) > BEST_PICKS else []})
    rows.sort(key=lambda r: (-r["score"], -r["haul_points"], r["team_id"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def needed_groups(counts: dict[str, int]) -> list[str]:
    """The position groups a team should draft from: short of starters, else short of depth, else all (see the docstring)."""
    short = [g for g in STARTERS if counts.get(g, 0) < STARTERS[g]]
    if not short:
        short = [g for g in DEPTH_TARGET if counts.get(g, 0) < DEPTH_TARGET[g]]
    return short or list(STARTERS)


def rank_suggestions(candidates: list[tuple[int, Projection]], groups: list[str], expected: float, limit: int = SUGGESTIONS) -> list[dict]:
    """The best `limit` of (asset id, projection) in `groups`, by projected points above `expected`; ties by points then id."""
    pool = [(a, p) for a, p in candidates if p.group in groups]
    pool.sort(key=lambda ap: (-ap[1].points, ap[0]))
    return [{"asset_id": a, "group": p.group, "projected_points": round(p.points, 1), "projected_games": round(p.games),
             "expected_points": round(expected, 1), "value": round(p.points - expected, 1), "source": p.source}
            for a, p in pool[:limit]]


# ---- database wrappers ----------------------------------------------------------------------------------------------

def is_nhl_league(db: Session, league_id: int) -> bool:
    return db.query(LeagueUniverse).join(Universe, Universe.id == LeagueUniverse.universe_id) \
        .filter(LeagueUniverse.league_id == league_id, Universe.code == "nhl").first() is not None


def roster_group_counts(db: Session, owned: set[int], projections: dict[int, Projection]) -> dict[str, int]:
    """Players per position group among `owned` asset ids. A player the model has no group for (e.g. a dual-eligible "F")
    is counted from his listed position."""
    counts: dict[str, int] = {}
    for a in owned:
        pr = projections.get(a)
        if pr is not None:
            counts[pr.group] = counts.get(pr.group, 0) + 1
    for a in db.query(DraftableAsset).filter(DraftableAsset.id.in_(owned or {0})):
        if a.id not in projections and a.player and GROUP_BY_POSITION.get((a.player.position or "").upper()):
            g = GROUP_BY_POSITION[(a.player.position or "").upper()]
            counts[g] = counts.get(g, 0) + 1
    return counts


def _names(db: Session, asset_ids: set[int]) -> dict[int, dict]:
    out = {}
    for a in db.query(DraftableAsset).filter(DraftableAsset.id.in_(asset_ids or {0})):
        out[a.id] = {"name": a.player.full_name if a.player else "?", "position": a.player.position if a.player else None}
    return out


def draft_grades(db: Session, league_id: int, projections: dict[int, Projection] | None = None) -> dict:
    """{"available": bool, "reason": str | None, "teams": [...], "picks": [...]} for the league's newest completed snake
    draft. Never raises for a missing draft or missing data - it says why nothing is graded."""
    draft = (db.query(Draft).filter(Draft.league_id == league_id, Draft.status == "complete", Draft.is_snake.is_(True))
             .order_by(Draft.id.desc()).first())
    if draft is None:
        return {"available": False, "reason": "No completed snake draft to grade yet", "teams": [], "picks": []}
    projections = project_pool(db) if projections is None else projections
    if not projections:
        return {"available": False, "reason": "No projections yet: no NHL history has been stored", "teams": [], "picks": []}
    teams = {t.id: t.name for t in db.query(Team).filter_by(league_id=league_id)}
    made = (db.query(DraftPick).filter(DraftPick.draft_id == draft.id, DraftPick.draftable_asset_id.isnot(None))
            .order_by(DraftPick.pick_number).all())
    board = sorted((p.points for p in projections.values()), reverse=True)
    last_pick = max([p.pick_number for p in made], default=0)
    expected = expected_by_pick(board, last_pick, max(2, len(teams) // 2))
    results = [PickResult(p.pick_number, p.round, p.team_id, p.draftable_asset_id,
                          projections[p.draftable_asset_id].points if p.draftable_asset_id in projections else 0.0,
                          expected[p.pick_number - 1]) for p in made]
    graded = grade_teams(results, projections, list(teams))
    for r in graded:
        r["team_name"] = teams[r["team_id"]]
    info = _names(db, {p.asset_id for p in results})
    return {"available": True, "reason": None, "draft_id": draft.id,
            "method": "50% projected points of the best lineup drafted, 50% projected points above the board at each pick",
            "teams": graded,
            "picks": [{"pick_number": p.pick_number, "round": p.round, "team_id": p.team_id, "asset_id": p.asset_id,
                       **info.get(p.asset_id, {}), "projected_points": round(p.projected_points, 1),
                       "expected_points": round(p.expected_points, 1), "value": round(p.value, 1)} for p in results]}


def power_rankings(db: Session, league_id: int, projections: dict[int, Projection] | None = None) -> dict:
    """Teams ranked by the projected points of the best lineup each roster can field."""
    projections = project_pool(db) if projections is None else projections
    if not projections:
        return {"available": False, "reason": "No projections yet: no NHL history has been stored", "teams": []}
    rows = []
    for t in db.query(Team).filter_by(league_id=league_id).order_by(Team.id):
        asset_ids = [e.draftable_asset_id for e in db.query(RosterEntry).filter_by(team_id=t.id)]
        lineup = lineup_projection([projections[a] for a in asset_ids if a in projections])
        rows.append((t, lineup))
    rows.sort(key=lambda r: (-r[1].total, r[0].name))
    info = _names(db, {p.asset_id for _, lu in rows for p in lu.starters})
    return {"available": True, "reason": None, "teams": [
        {"rank": i, "team_id": t.id, "team_name": t.name, "projected_points": round(lu.total, 1),
         "starters_points": round(lu.starters_points, 1), "depth_points": round(lu.depth_points, 1),
         "starters": [{"asset_id": p.asset_id, "group": p.group, **info.get(p.asset_id, {}),
                       "projected_points": round(p.points, 1), "projected_games": round(p.games), "source": p.source}
                      for p in lu.starters]}
        for i, (t, lu) in enumerate(rows, 1)]}


def pick_suggestions(db: Session, draft: Draft, available_asset_ids: set[int], projections: dict[int, Projection] | None = None,
                     team_id: int | None = None) -> dict:
    """The suggestions for the pick on the clock (or for `team_id`, at that pick number). `available_asset_ids` is the pool
    still open. Never raises for a finished draft or missing data - it says why there is nothing to suggest."""
    nxt = (db.query(DraftPick).filter(DraftPick.draft_id == draft.id, DraftPick.draftable_asset_id.is_(None))
           .order_by(DraftPick.pick_number).first())
    if nxt is None:
        return {"available": False, "reason": "The draft has no open pick", "suggestions": []}
    projections = project_pool(db) if projections is None else projections
    if not projections:
        return {"available": False, "reason": "No projections yet: no NHL history has been stored", "suggestions": []}
    team = team_id if team_id is not None else nxt.team_id
    owned = {e.draftable_asset_id for e in db.query(RosterEntry).filter_by(team_id=team)} | {
        p.draftable_asset_id for p in db.query(DraftPick).filter(DraftPick.draft_id == draft.id, DraftPick.team_id == team,
                                                                  DraftPick.draftable_asset_id.isnot(None))}
    counts = roster_group_counts(db, owned, projections)
    groups = needed_groups(counts)
    n_teams = db.query(Team).filter_by(league_id=draft.league_id).count()
    board = sorted((p.points for p in projections.values()), reverse=True)
    expected = expected_by_pick(board, nxt.pick_number, max(2, n_teams // 2))[nxt.pick_number - 1]
    out = rank_suggestions([(a, projections[a]) for a in available_asset_ids if a in projections], groups, expected)
    info = _names(db, {r["asset_id"] for r in out})
    for r in out:
        r.update(info.get(r["asset_id"], {}))
    return {"available": True, "reason": None, "pick_number": nxt.pick_number, "round": nxt.round, "team_id": team,
            "needs": groups, "roster_counts": counts, "suggestions": out}
