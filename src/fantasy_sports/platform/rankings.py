"""
Team rankings and the weekly poll.

PROJECTION. Each rostered player gets a projected weekly credit (raw EPA credit, the same unit as
weekly_player_scores): a season-to-date average shrunk toward a prior,

    projected = (credit so far + K * prior) / (games so far + K)          K = PRIOR_GAMES

  * prior  = his position's average per-game credit among rostered players this season, tilted by how
             he ranked at his position last season (ESPN points z-score, clipped to +/-2, worth
             TILT_SD of the position's spread). Before any game has been scored there is no
             season data, so the position averages fall back to the fitted scale's slot means
             (scoring_scale.json) and only the tilt separates players.
  * games  = weeks (so far) in which his real team played; a week he didn't earn credit counts as 0.
Real bye weeks and injuries aren't modelled yet: the schedule of future real games isn't ingested.

A team's projection for each remaining week (the weeks after the last scored one, through week 10)
is the credit of the best lineup its roster can field that week; RANK 1 is the highest total remaining
credit. This is the simple ranking; nothing about results so far enters it beyond the players' credit.

POLL. A finite Top-N, like the NCAA's Top 25 of ~136 teams. N grows with the league:

    N = min(25, max(4, ceil(0.4 * teams)), teams - 1)

40% because a 12-team league needs a real bubble (5 ranked, 7 not) rather than the NCAA's 18%;
capped at 25 like the AP; at least one team is always left out so "others receiving votes" is
never empty. An AI voter panel then builds the poll from the projections: each voter ranks his
top N by remaining credit plus his own bias (seeded by league and week, so the poll is the same
every time it is read and only changes when the data does), giving N points for 1st down to 1 for
Nth. Teams are ordered by points; first-place votes are counted; teams with points outside the top
N are "others receiving votes". The poll rank is what is shown next to a team's name.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy.orm import Session, joinedload

from fantasy_sports.api.helpers import draftable_asset_position
from fantasy_sports.platform import lineups, scoring, scoring_scale, season
from fantasy_sports.platform.db.models.core import DraftableAsset, Game, League, RosterEntry, Team
from fantasy_sports.platform.recruiting.pricing import season_points_by_asset
from fantasy_sports.sports.football.models import WeeklyPlayerScore

PRIOR_GAMES = 3
TILT_SD = 0.5
VOTERS = 25
VOTER_BIAS = 0.15        # a voter's bias, as a share of the spread of team projections
MAX_RANKED = 25

# position -> the fitted scale's slot keys (fallback prior before any 2026 credit exists)
_SLOT_KEYS = {"QB": ["QB#1"], "RB": ["RB#1", "RB#2"], "WR": ["WR#1", "WR#2"], "TE": ["TE#1"], "PK": ["K#1"],
              "DL": ["DL#1"], "LB": ["LB#1"], "DB": ["DB#1"]}


def poll_size(n_teams: int) -> int:
    return max(0, min(MAX_RANKED, max(4, math.ceil(0.4 * n_teams)), n_teams - 1))


@dataclass
class PlayerProjection:
    asset_id: int
    position: str | None
    games: int
    credit: float
    prior: float
    weekly: float           # projected credit per remaining week


def _asset_load():
    return joinedload(RosterEntry.draftable_asset).options(joinedload(DraftableAsset.player), joinedload(DraftableAsset.defense_group))


def _real_team_id(a: DraftableAsset) -> int | None:
    return a.player.real_team_id if a.player else a.defense_group.real_team_id


def scored_weeks(db: Session, league_id: int) -> tuple[list, list]:
    """(this season's weeks with games scored, in order; all this season's weeks)"""
    weeks = scoring.season_weeks(db, league_id)
    done = scoring.scored_week_ids(db, [w.id for w in weeks])
    return [w for w in weeks if w.id in done], weeks


def player_projections(db: Session, league_id: int) -> dict[int, PlayerProjection]:
    team_ids = [t.id for t in db.query(Team).filter_by(league_id=league_id)]
    assets = {e.draftable_asset_id: e.draftable_asset
              for e in db.query(RosterEntry).options(_asset_load()).filter(RosterEntry.team_id.in_(team_ids or [0]))}
    done, _ = scored_weeks(db, league_id)
    done_ids = [w.id for w in done]
    played: dict[int, set[int]] = defaultdict(set)              # week -> real teams that played
    for g in db.query(Game).filter(Game.week_id.in_(done_ids or [0])):
        played[g.week_id].update((g.home_team_id, g.away_team_id))
    credit: dict[int, float] = defaultdict(float)
    for s in db.query(WeeklyPlayerScore).filter(WeeklyPlayerScore.week_id.in_(done_ids or [0]),
                                                WeeklyPlayerScore.draftable_asset_id.in_(list(assets) or [0])):
        credit[s.draftable_asset_id] += s.points
    games = {aid: sum(1 for w in done if _real_team_id(a) in played[w.id]) for aid, a in assets.items()}
    pos = {aid: draftable_asset_position(a) for aid, a in assets.items()}
    last = season_points_by_asset(db)

    n_teams = len(team_ids)
    params = scoring_scale._params()
    by_pos_rate: dict[str, list[float]] = defaultdict(list)
    for aid in assets:
        if games[aid] > 0:
            by_pos_rate[pos[aid]].append(credit[aid] / games[aid])
    pos_mean, pos_sd = {}, {}
    for p, keys in _SLOT_KEYS.items():
        rates = by_pos_rate.get(p, [])
        if len(rates) >= 2:
            pos_mean[p], pos_sd[p] = statistics.fmean(rates), statistics.pstdev(rates) or 1.0
        else:                                                    # no season data yet: the fitted slot means
            pos_mean[p] = statistics.fmean(scoring_scale._interp(n_teams, params["league_sizes"], params["slot_mean"][k]) for k in keys)
            pos_sd[p] = 1.0
    pts_by_pos: dict[str, list[float]] = defaultdict(list)
    for aid in assets:
        if aid in last:
            pts_by_pos[pos[aid]].append(last[aid])

    out = {}
    for aid, a in assets.items():
        p = pos[aid]
        prior = pos_mean.get(p, 0.0)
        ref = pts_by_pos.get(p, [])
        if aid in last and len(ref) >= 2:
            sd = statistics.pstdev(ref) or 1.0
            z = max(-2.0, min(2.0, (last[aid] - statistics.fmean(ref)) / sd))
            prior += TILT_SD * pos_sd.get(p, 1.0) * z
        out[aid] = PlayerProjection(
            aid, p, games[aid], credit[aid], prior, (credit[aid] + PRIOR_GAMES * prior) / (games[aid] + PRIOR_GAMES))
    return out


def best_projected_lineup(db: Session, team: Team, proj: dict[int, PlayerProjection]) -> float:
    """Sum of projected weekly credit over the best legal starting lineup the roster can field."""
    remaining = sorted((e.draftable_asset for e in db.query(RosterEntry).options(_asset_load()).filter_by(team_id=team.id)),
                       key=lambda a: proj[a.id].weekly, reverse=True)
    total = 0.0
    for slot in lineups.league_slots(db, team.league_id):
        if not slot.is_starter:
            continue
        taken = 0
        for a in list(remaining):
            if taken == slot.slot_count:
                break
            if draftable_asset_position(a) in slot.eligible_positions:
                total += proj[a.id].weekly
                remaining.remove(a)
                taken += 1
    return total


def remaining_week_numbers(db: Session, league_id: int) -> list[int]:
    done, weeks = scored_weeks(db, league_id)
    last = done[-1].week_number if done else 0
    return [w.week_number for w in weeks if last < w.week_number <= season.CHAMPIONSHIP_WEEK]


def team_projections(db: Session, league_id: int) -> list[dict]:
    """Every team with its projected credit for each remaining week, the total, and its rank
    (1 = highest remaining credit). Best first."""
    proj = player_projections(db, league_id)
    remaining = remaining_week_numbers(db, league_id)
    rows = []
    for t in db.query(Team).filter_by(league_id=league_id).order_by(Team.id):
        per_week = best_projected_lineup(db, t, proj)
        weeks = [{"week_number": n, "credit": per_week} for n in remaining]
        rows.append({"team_id": t.id, "team_name": t.name, "weekly": weeks, "per_week": per_week,
                     "remaining_credit": per_week * len(remaining)})
    rows.sort(key=lambda r: (-r["remaining_credit"], -r["per_week"], r["team_name"]))
    for i, r in enumerate(rows, 1):
        r["epa_rank"] = i
    return rows


def poll(db: Session, league_id: int) -> dict:
    """The AI voter panel's poll for the current point of the season (see the module docstring)."""
    league = db.get(League, league_id)
    if league is not None and league.scoring_system == "imported":      # no realistic scale to rank on (FS-037)
        return {"as_of_week": 0, "size": 0, "voters": 0, "remaining_weeks": [], "ranked": [], "others": [], "epa_ranking": []}
    done, _ = scored_weeks(db, league_id)
    as_of = done[-1].week_number if done else 0
    teams = team_projections(db, league_id)
    n = len(teams)
    size = poll_size(n)
    values = [t["per_week"] for t in teams]
    spread = statistics.pstdev(values) if n > 1 else 0.0
    rng = random.Random(league_id * 1000 + as_of)
    points: dict[int, int] = defaultdict(int)
    firsts: dict[int, int] = defaultdict(int)
    for _ in range(VOTERS):
        ballot = sorted(teams, key=lambda t: t["per_week"] + rng.gauss(0, VOTER_BIAS * spread), reverse=True)[:size]
        for place, t in enumerate(ballot):
            points[t["team_id"]] += size - place
            if place == 0:
                firsts[t["team_id"]] += 1
    order = sorted(teams, key=lambda t: (-points[t["team_id"]], t["epa_rank"]))
    ranked = [{"rank": i, "team_id": t["team_id"], "team_name": t["team_name"], "points": points[t["team_id"]],
               "first_place_votes": firsts[t["team_id"]], "epa_rank": t["epa_rank"],
               "remaining_credit": t["remaining_credit"]}
              for i, t in enumerate(order[:size], 1)]
    others = [{"team_id": t["team_id"], "team_name": t["team_name"], "points": points[t["team_id"]]}
              for t in order[size:] if points[t["team_id"]] > 0]
    return {"as_of_week": as_of, "size": size, "voters": VOTERS, "remaining_weeks": remaining_week_numbers(db, league_id),
            "ranked": ranked, "others": others, "epa_ranking": teams}


def poll_ranks(db: Session, league_id: int) -> dict[int, int]:
    """team id -> poll rank, for ranked teams only (what is shown next to a team's name)."""
    return {r["team_id"]: r["rank"] for r in poll(db, league_id)["ranked"]}
