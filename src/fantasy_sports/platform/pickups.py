"""
Trending and recommended pickups for the Transactions tab.

TRENDING (every sport). The share of the other leagues in the system that signed or won this player in the last TREND_DAYS
days. A league counts once however many of its teams (or how many times) added him, so one busy league can't make a
player trend. The denominator is every other league that has the player's universe in its pool, mock drafts excluded
(they make no real transactions); the league being looked at is left out of both parts, so the number reads "other leagues
are adding him". The AI's required roster fill is not a decision and doesn't count; commissioner moves for an AI team don't
either. With few leagues in the system the numbers are small and jumpy - the counts ("2 of 5 leagues") are returned beside
the percentage so the page can show them.

RECOMMENDED (NHL, where projections exist). The same engine as the draft's pick suggestions (platform/draft_grades.py):
every free agent is projected (platform/hockey_projection.py) and `rank_suggestions` ranks them by projected points above a
baseline. In a draft the baseline is what the board expects at the pick; here it is what the team already gets in that
position group: the projected points of its weakest starter there (1 C, 2 W, 2 D, 1 G), or 0 for a starting slot it has not
filled. So "+40" is the projected points the pickup adds over the player he would start in place of, and only upgrades are
listed. A slot the team has not filled is flagged (`fills_open_slot`) because every free agent there is a pure gain. Players
on waivers are included and marked, since a claim is still a way to get them. Nothing here signs anyone.

Pure functions first, then the database wrappers. No scoring or attribution code is touched.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from fantasy_sports.platform import free_agency, waivers
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, FreeAgencyTransaction, League, LeagueUniverse, RosterEntry, Team,
)
from fantasy_sports.platform.draft_grades import _names, rank_suggestions, roster_group_counts
from fantasy_sports.platform.hockey_projection import STARTERS, Projection, project_pool

TREND_DAYS = 7
TRENDING_LIMIT = 10
RECOMMENDED_LIMIT = 5
COUNTED_REASONS = (free_agency.USER, waivers.WAIVER)     # a person's own signing, or a claim they won


# ---- pure -----------------------------------------------------------------------------------------------------------

def trend_percent(adding: int, total: int) -> float:
    """Leagues adding as a percentage of leagues that could, one decimal; 0 when there are no leagues."""
    return round(100.0 * adding / total, 1) if total > 0 else 0.0


def starter_baselines(owned: list[Projection]) -> dict[str, float]:
    """Per position group, the projected points of the team's weakest starter there (0.0 for a starting slot it has not
    filled). A pickup is worth the points he adds over this."""
    by_group: dict[str, list[float]] = {g: [] for g in STARTERS}
    for p in owned:
        if p.group in by_group:
            by_group[p.group].append(p.points)
    out = {}
    for g, slots in STARTERS.items():
        pts = sorted(by_group[g], reverse=True)
        out[g] = pts[slots - 1] if len(pts) >= slots else 0.0
    return out


def recommend(candidates: list[tuple[int, Projection]], baselines: dict[str, float], counts: dict[str, int],
              limit: int = RECOMMENDED_LIMIT) -> list[dict]:
    """The best `limit` free agents across all groups by projected points above the group's baseline; upgrades only."""
    rows = []
    for g, baseline in baselines.items():
        for r in rank_suggestions(candidates, [g], baseline, limit):
            if r["value"] > 0:
                r["fills_open_slot"] = counts.get(g, 0) < STARTERS[g]
                rows.append(r)
    rows.sort(key=lambda r: (-r["value"], -r["projected_points"], r["asset_id"]))
    return rows[:limit]


# ---- database wrappers ----------------------------------------------------------------------------------------------

def _free_agents(db: Session, league_id: int, now: datetime.datetime) -> tuple[set[int], dict[int, datetime.datetime]]:
    """(free-agent asset ids including those on waivers, asset id -> waiver end) for a league."""
    taken = free_agency.rostered_ids(db, league_id)
    free = {a for a in free_agency.pool_ids(db, league_id) if a not in taken}
    return free, waivers.waiver_until(db, league_id, now)


def trending(db: Session, league_id: int, now: datetime.datetime | None = None, days: int = TREND_DAYS,
             limit: int = TRENDING_LIMIT) -> dict:
    """The league's current free agents that other leagues have been adding, most-added first. Never raises for a league
    with nothing to compare against - it says why there is nothing to show."""
    now = now or datetime.datetime.utcnow()
    mine = {lu.universe_id for lu in db.query(LeagueUniverse).filter_by(league_id=league_id)}
    others = {l.id: l for l in db.query(League).filter(League.id != league_id, League.is_mock_draft.is_(False))}
    universe_leagues: dict[int, int] = {}
    comparable: set[int] = set()
    for lu in db.query(LeagueUniverse).filter(LeagueUniverse.league_id.in_(list(others) or [0]),
                                              LeagueUniverse.universe_id.in_(list(mine) or [0])):
        universe_leagues[lu.universe_id] = universe_leagues.get(lu.universe_id, 0) + 1
        comparable.add(lu.league_id)
    base = {"days": days, "other_leagues": len(comparable), "players": []}
    if not universe_leagues:
        return {"available": False, "reason": "No other leagues to compare with yet", **base}
    since = now - datetime.timedelta(days=days)
    rows = (db.query(FreeAgencyTransaction.draftable_asset_id, FreeAgencyTransaction.league_id)
            .filter(FreeAgencyTransaction.kind == "add", FreeAgencyTransaction.reason.in_(COUNTED_REASONS),
                    FreeAgencyTransaction.created_at >= since, FreeAgencyTransaction.league_id.in_(list(comparable)))
            .distinct().all())
    adding: dict[int, int] = {}
    for asset_id, _league in rows:
        adding[asset_id] = adding.get(asset_id, 0) + 1
    free, on_waivers = _free_agents(db, league_id, now)
    universe_of = {a.id: a.universe_id for a in db.query(DraftableAsset).filter(DraftableAsset.id.in_([x for x in adding if x in free] or [0]))}
    info = _names(db, set(universe_of))
    players = []
    for asset_id, universe_id in universe_of.items():
        total = universe_leagues.get(universe_id, 0)
        if total <= 0:
            continue
        players.append({"asset_id": asset_id, **info.get(asset_id, {}), "leagues_adding": adding[asset_id],
                        "leagues_total": total, "percent": trend_percent(adding[asset_id], total),
                        "waiver_until": on_waivers.get(asset_id)})
    players.sort(key=lambda p: (-p["percent"], -p["leagues_adding"], p.get("name") or "", p["asset_id"]))
    if not players:
        return {"available": True, "reason": f"No free agent here was added in another league in the last {days} days", **base}
    return {"available": True, "reason": None, **base, "players": players[:limit]}


def recommended(db: Session, team: Team, projections: dict[int, Projection] | None = None, now: datetime.datetime | None = None,
                limit: int = RECOMMENDED_LIMIT) -> dict:
    """The free agents (and players on waivers) who would most raise `team`'s projected lineup. Never raises for missing data
    or closed free agency - it says why there is nothing to recommend."""
    now = now or datetime.datetime.utcnow()
    if not free_agency.is_open(db, team.league_id):
        return {"available": False, "reason": "Free agency opens when the draft ends", "recommendations": []}
    projections = project_pool(db) if projections is None else projections
    if not projections:
        return {"available": False, "reason": "No projections yet: no NHL history has been stored", "recommendations": []}
    owned = {e.draftable_asset_id for e in db.query(RosterEntry).filter_by(team_id=team.id)}
    counts = roster_group_counts(db, owned, projections)
    baselines = starter_baselines([projections[a] for a in owned if a in projections])
    free, on_waivers = _free_agents(db, team.league_id, now)
    out = recommend([(a, projections[a]) for a in free if a in projections], baselines, counts, limit)
    info = _names(db, {r["asset_id"] for r in out})
    for r in out:
        r.update(info.get(r["asset_id"], {}))
        r["baseline_points"] = round(baselines[r["group"]], 1)
        r["waiver_until"] = on_waivers.get(r["asset_id"])
    return {"available": True, "reason": None if out else "Nobody in free agency would raise this lineup",
            "team_id": team.id, "roster_counts": counts, "recommendations": out}
