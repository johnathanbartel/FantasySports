"""
AI-owned teams run their own rosters and lines in non-college leagues, slowly, like a person checking in.

Once a league's draft is done, each AI team wakes up on its own schedule: a seeded, pseudo-random gap of 1 to 14 hours
between visits, different for every team and every visit. At a visit the team

  1. signs the free agent that best fills its lineup, if it has an open roster spot (platform/ai/snake_ai.py), or - with a
     one-in-four chance, and only if the gain is clear - swaps its weakest player for a clearly better free agent at the
     same position; at most ONE transaction per visit, and
  2. sets its lines: hockey teams rank each position group best-first (the pecking order); other leagues set the best
     lineup for the next week that has not started.

So a team that needs ten players takes days to fill out, and the whole league's AI activity trickles across the day
instead of landing in one burst. Nothing is scheduled: `tick` applies whatever visits have fallen due. Each visit is
stamped with the instant it fell due, visits are applied in time order, and the next visit is computed from that
instant, so running it every second or once after a week gives the same result, and running it twice changes nothing.

State lives in each team's `personality_profile["ai_roster"]` = {"next_at": iso instant, "k": visits made}; the seeds
are (league, team, visit number). No new tables.
"""

from __future__ import annotations

import datetime
import random
from types import SimpleNamespace

from sqlalchemy.orm import Session

from fantasy_sports.platform import free_agency, lineups, pecking_order, scoring
from fantasy_sports.platform.ai import snake_ai
from fantasy_sports.platform.db.models.core import League, RosterEntry, Team
from fantasy_sports.platform.draft_stats import stats_by_asset
from fantasy_sports.platform.league_features import draft_state, is_college, roster_limit

MIN_GAP_SECONDS = 1 * 3600
MAX_GAP_SECONDS = 14 * 3600
SWAP_CHANCE = 0.25            # of visits to a full roster
SWAP_MIN_GAIN = 1.3           # a free agent must score this many times the player he replaces...
SWAP_MIN_ADD = 0.5            # ...plus this many points
MAX_VISITS_PER_TICK = 400     # bound on catch-up work in one call


def gap_seconds(league_id: int, team_id: int, visit: int) -> float:
    return random.Random(f"ai-roster:{league_id}:{team_id}:{visit}").uniform(MIN_GAP_SECONDS, MAX_GAP_SECONDS)


def _state(team: Team) -> dict | None:
    return (team.personality_profile or {}).get("ai_roster")


def _save(team: Team, state: dict) -> None:
    profile = dict(team.personality_profile or {})
    profile["ai_roster"] = state
    team.personality_profile = profile      # a new dict, so the JSON column notices the change


def _at(state: dict) -> datetime.datetime:
    return datetime.datetime.fromisoformat(state["next_at"])


def next_visit_at(db: Session, league_id: int) -> datetime.datetime | None:
    """The earliest moment an AI team in the league will next make a decision (None if none are scheduled)."""
    times = [_at(s) for t in db.query(Team).filter_by(league_id=league_id, is_ai_owned=True) if (s := _state(t))]
    return min(times) if times else None


# ---- lines ------------------------------------------------------------------------------------------------------
def set_lines(db: Session, team: Team, now: datetime.datetime) -> None:
    """The team sets its own lines: best players first (stored points, then name)."""
    try:
        pecking_order.assert_hockey_team(db, team)
    except ValueError:
        _set_weekly_lineup(db, team, now)
        return
    roster = pecking_order.roster_players(db, team.id)
    if not roster:
        return
    points = stats_by_asset(db, [asset_id for asset_id, _ in roster.values()])
    by_group: dict[str, list[tuple]] = {g: [] for g in pecking_order.GROUPS}
    for pid, (asset_id, player) in roster.items():
        group = pecking_order.GROUP_BY_POSITION.get(player.position)
        if group:
            pts = points.get(asset_id, {}).get("points")
            by_group[group].append((-(pts if pts is not None else -1.0), player.full_name, pid))
    pecking_order.replace_groups(db, team, {
        g: [pid for _, _, pid in sorted(rows)[:pecking_order.DEPTH[g]]] for g, rows in by_group.items() if rows})


def _set_weekly_lineup(db: Session, team: Team, now: datetime.datetime) -> None:
    """Non-hockey leagues: the best lineup for the next week that has not started."""
    try:
        weeks = scoring.season_weeks(db, team.league_id)
        week = next((w for w in weeks if not scoring.is_locked(w, now.date())), None)
        if week is None or not lineups.roster_assets(db, team.id):
            return
        pairs = lineups.build_lineup(db, team, lineups.best_lineup(db, team))
        lineups.save_lineup(db, team, week.id, pairs)
    except ValueError:
        return


# ---- one visit --------------------------------------------------------------------------------------------------
def _visit(db: Session, league: League, team: Team, at: datetime.datetime, board: list, visit: int) -> None:
    from fantasy_sports.api.helpers import draftable_asset_position

    limit = roster_limit(db, league)
    entries = db.query(RosterEntry).filter_by(team_id=team.id).all()
    stub = SimpleNamespace(league_id=league.id)
    if len(entries) < limit and board:
        pick = snake_ai.choose_pick(db, stub, team.id, board)
        board.remove(pick)
        free_agency._sign(db, team, pick.draftable_asset_id, 0, free_agency.AI_FILL, at)
        db.flush()
    elif len(entries) >= limit and board and random.Random(f"ai-swap:{league.id}:{team.id}:{visit}").random() < SWAP_CHANCE:
        stats = stats_by_asset(db, [e.draftable_asset_id for e in entries])
        mine = [(stats[e.draftable_asset_id]["points"] or 0.0, e.draftable_asset_id,
                 draftable_asset_position(e.draftable_asset)) for e in entries]
        worst = min(mine)
        group_of = lambda p: pecking_order.GROUP_BY_POSITION.get(p, p)   # noqa: E731
        better = [a for a in board if a.position and group_of(a.position) == group_of(worst[2])
                  and (a.points or 0.0) >= worst[0] * SWAP_MIN_GAIN + SWAP_MIN_ADD]
        if better:
            pick = better[0]                                   # the board is best-first
            board.remove(pick)
            free_agency.drop_player(db, team, worst[1], free_agency.AI_FILL, at)
            free_agency._sign(db, team, pick.draftable_asset_id, 0, free_agency.AI_FILL, at)
            db.flush()
    set_lines(db, team, at)


def tick(db: Session, league_id: int, now: datetime.datetime | None = None) -> int:
    """Applies every AI visit that has fallen due. Returns how many visits were made. Flushes, does not commit; the
    league row is locked (on a database that supports it) so two requests cannot apply the same visit."""
    now = now or datetime.datetime.utcnow()
    league = db.query(League).filter(League.id == league_id).with_for_update().one_or_none()
    if league is None or is_college(db, league_id) or draft_state(db, league_id) != "complete":
        return 0
    teams = db.query(Team).filter_by(league_id=league_id, is_ai_owned=True).order_by(Team.id).all()
    for t in teams:
        if _state(t) is None:                                  # first sight: lines now, first visit a little later
            _save(t, {"k": 0, "next_at": (now + datetime.timedelta(seconds=gap_seconds(league_id, t.id, 0))).isoformat()})
            set_lines(db, t, now)
    board = None
    visits = 0
    while visits < MAX_VISITS_PER_TICK:
        due = [(_at(s), t.id, t) for t in teams if (s := _state(t)) and _at(s) <= now]
        if not due:
            break
        at, _, team = min(due, key=lambda d: (d[0], d[1]))
        if board is None:
            board = free_agency.free_agent_board(db, league_id)
        state = _state(team)
        _visit(db, league, team, at, board, state["k"])
        k = state["k"] + 1
        _save(team, {"k": k, "next_at": (at + datetime.timedelta(seconds=gap_seconds(league_id, team.id, k))).isoformat()})
        visits += 1
    db.flush()
    return visits
