"""
The schedule: a premade regular season that teams can renegotiate in the preseason
schedule stage, and the conference-championship week that follows it.

PREMADE SCHEDULE. Every team is given a round-robin schedule for weeks 1-9 (circle method: with n
teams, n-1 rounds in which everyone plays everyone once; a bye is added if n is odd). The premade
games are provisional - they are not agreements.

REQUESTS. While the stage is open a team can ask another team to play it in a given week instead,
optionally attaching in-game cash to sweeten the offer (it comes out of the sender's free-agency
money, and can't exceed what they have left after their other pending offers). Only teams that don't
already have an AGREEMENT that week (an accepted request) can be asked, and a team that already has
one can't ask. A team has at most one open request per week (a new one replaces the old).

  * A user team decides on requests made to it. Accepting makes the agreement: both teams' premade
    games that week are dropped, the two play each other, and their former premade opponents are
    paired up with each other (or, if only one is left, it gets a bye that week). The cash moves then,
    sender -> receiver, and never before. Any other open request that week touching either team is
    declined.
  * An AI team decides at the close of the stage: if it is the target of exactly one request that
    week it accepts it, and if several teams asked it picks one at random.
  * When the commissioner closes the stage, AI teams' requests are resolved as above and every
    request still open (to a user team that never answered) is declined. After that, nothing changes.

CONFERENCE CHAMPIONSHIPS (week 10). Derived from the regular-season standings, never stored, so a
retroactive lineup change moves them: within each conference, #1 plays #2, #3 plays #4, and so on;
in a conference with an odd number of teams the worst team gets a bye and its season is over.
"""

from __future__ import annotations

import datetime
import random
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy.orm import Session

from fantasy_sports.platform import free_agency, scoring, season
from fantasy_sports.platform.db.models.core import (
    MatchupRequest,
    ScheduleSettings,
    Team,
    Week,
    WeeklyMatchup,
)


# ------------------------------------------------------------------------- premade schedule
def round_robin_rounds(team_ids: list[int]) -> list[list[tuple[int, int]]]:
    ids = list(team_ids)
    if len(ids) % 2:
        ids.append(0)                       # 0 = bye
    n = len(ids)
    rounds = []
    for _ in range(n - 1):
        pairs = [(ids[i], ids[n - 1 - i]) for i in range(n // 2)]
        rounds.append([(a, b) for a, b in pairs if a and b])
        ids = [ids[0]] + [ids[-1]] + ids[1:-1]      # rotate everyone but the first
    return rounds


def regular_weeks(db: Session, league_id: int) -> list[Week]:
    return scoring.season_weeks(db, league_id)[:season.REGULAR_SEASON_WEEKS]


def create_schedule(
    db: Session, league_id: int, weeks: int = season.REGULAR_SEASON_WEEKS, replace: bool = False,
    rng: random.Random | None = None,
) -> int:
    """The premade schedule: matchups for the first `weeks` weeks. Returns how many were created.
    Refuses if the league already has matchups unless replace=True (which also clears requests)."""
    season_weeks = scoring.season_weeks(db, league_id)
    if not season_weeks:
        raise ValueError("This league's season has no weeks yet")
    weeks = min(weeks, season.REGULAR_SEASON_WEEKS)
    existing = db.query(WeeklyMatchup).filter_by(league_id=league_id)
    if existing.count():
        if not replace:
            raise ValueError("This league already has a schedule - replace it to regenerate")
        existing.delete()
        db.query(MatchupRequest).filter_by(league_id=league_id).delete()
        settings = db.query(ScheduleSettings).filter_by(league_id=league_id).first()
        if settings:
            settings.status, settings.closed_at = "open", None
    team_ids = [t.id for t in db.query(Team).filter_by(league_id=league_id).order_by(Team.id)]
    if len(team_ids) < 2:
        raise ValueError("A schedule needs at least 2 teams")
    (rng or random.Random()).shuffle(team_ids)
    rounds = round_robin_rounds(team_ids)
    created = 0
    for i, week in enumerate(season_weeks[:weeks]):
        for a, b in rounds[i % len(rounds)]:
            db.add(WeeklyMatchup(league_id=league_id, week_id=week.id, team_a_id=a, team_b_id=b))
            created += 1
    db.flush()
    return created


# ------------------------------------------------------------------------------- the stage
def stage_status(db: Session, league_id: int) -> str:
    s = db.query(ScheduleSettings).filter_by(league_id=league_id).first()
    return s.status if s else "open"


def _matchup(db: Session, league_id: int, week_id: int, team_id: int) -> WeeklyMatchup | None:
    return db.query(WeeklyMatchup).filter(
        WeeklyMatchup.league_id == league_id, WeeklyMatchup.week_id == week_id,
        (WeeklyMatchup.team_a_id == team_id) | (WeeklyMatchup.team_b_id == team_id)).first()


def agreed_teams(db: Session, league_id: int, week_id: int) -> set[int]:
    """Teams that already have an agreement (an accepted request) for the week."""
    out: set[int] = set()
    for r in db.query(MatchupRequest).filter_by(league_id=league_id, week_id=week_id, status="accepted"):
        out.update((r.from_team_id, r.to_team_id))
    return out


def pending_offer_total(db: Session, team_id: int, exclude_request_id: int | None = None) -> int:
    return sum(r.offer_amount for r in db.query(MatchupRequest).filter_by(from_team_id=team_id, status="pending")
               if r.id != exclude_request_id)


def cash_available(db: Session, team: Team, exclude_request_id: int | None = None) -> int:
    """Free-agency money not already promised in other pending offers."""
    return max(0, free_agency.team_money(db, team)["fa_money"] - pending_offer_total(db, team.id, exclude_request_id))


def available_opponents(db: Session, team: Team, week_id: int) -> list[Team]:
    """Who this team could ask that week: every other team without an agreement. Empty if the team
    already has an agreement that week."""
    taken = agreed_teams(db, team.league_id, week_id)
    if team.id in taken:
        return []
    return [t for t in db.query(Team).filter_by(league_id=team.league_id).order_by(Team.name)
            if t.id != team.id and t.id not in taken]


def _check_open(db: Session, league_id: int, week: Week | None) -> None:
    if stage_status(db, league_id) != "open":
        raise ValueError("The schedule stage is closed")
    if week is None or week.id not in {w.id for w in regular_weeks(db, league_id)}:
        raise ValueError("Matchups can only be arranged for the regular-season weeks (1-9)")
    if scoring.is_locked(week):
        raise ValueError("That week has already started")


def request_matchup(
    db: Session, from_team: Team, week_id: int, to_team_id: int, offer_amount: int = 0,
    now: datetime.datetime | None = None,
) -> MatchupRequest:
    """Raises ValueError with a message meant for the user."""
    now = now or datetime.datetime.utcnow()
    _check_open(db, from_team.league_id, db.get(Week, week_id))
    to_team = db.get(Team, to_team_id)
    if to_team is None or to_team.league_id != from_team.league_id or to_team.id == from_team.id:
        raise ValueError("Pick another team in this league")
    if offer_amount < 0:
        raise ValueError("An offer can't be negative")
    taken = agreed_teams(db, from_team.league_id, week_id)
    if from_team.id in taken:
        raise ValueError("You already have an agreement for that week")
    if to_team.id in taken:
        raise ValueError(f"{to_team.name} already has an agreement for that week")
    old = db.query(MatchupRequest).filter_by(from_team_id=from_team.id, week_id=week_id, status="pending").first()
    room = cash_available(db, from_team, exclude_request_id=old.id if old else None)
    if offer_amount > room:
        raise ValueError(f"You can offer at most ${room} (your free-agency money less your other open offers)")
    if old is not None:
        old.status, old.resolved_at = "withdrawn", now       # a new request for the week replaces the old one
    req = MatchupRequest(league_id=from_team.league_id, week_id=week_id, from_team_id=from_team.id,
                         to_team_id=to_team.id, offer_amount=offer_amount, status="pending", created_at=now)
    db.add(req)
    db.flush()
    return req


def withdraw_request(db: Session, req: MatchupRequest, now: datetime.datetime | None = None) -> None:
    if req.status != "pending":
        raise ValueError("That request has already been answered")
    req.status, req.resolved_at = "withdrawn", now or datetime.datetime.utcnow()


def _agree(db: Session, req: MatchupRequest, now: datetime.datetime) -> None:
    """Makes the agreement: rewires the premade schedule, moves the cash (by marking it accepted) and
    declines every other open request that week touching either team."""
    a, b = req.from_team_id, req.to_team_id
    mine, theirs = _matchup(db, req.league_id, req.week_id, a), _matchup(db, req.league_id, req.week_id, b)
    if mine is None or mine is not theirs:               # not already paired with each other
        orphans = []
        for m in {x.id: x for x in (mine, theirs) if x is not None}.values():
            orphans.extend(t for t in (m.team_a_id, m.team_b_id) if t not in (a, b))
            db.delete(m)
        db.flush()
        db.add(WeeklyMatchup(league_id=req.league_id, week_id=req.week_id, team_a_id=a, team_b_id=b))
        if len(orphans) == 2:                            # their old opponents play each other
            db.add(WeeklyMatchup(league_id=req.league_id, week_id=req.week_id,
                                 team_a_id=orphans[0], team_b_id=orphans[1]))
    req.status, req.resolved_at = "accepted", now
    db.flush()
    for other in db.query(MatchupRequest).filter_by(league_id=req.league_id, week_id=req.week_id, status="pending"):
        if other.id != req.id and {other.from_team_id, other.to_team_id} & {a, b}:
            other.status, other.resolved_at = "declined", now
    db.flush()


def _acceptable(db: Session, req: MatchupRequest) -> str | None:
    """Why an open request can't be accepted right now, or None."""
    taken = agreed_teams(db, req.league_id, req.week_id)
    if req.from_team_id in taken or req.to_team_id in taken:
        return "One of the teams already has an agreement for that week"
    sender = db.get(Team, req.from_team_id)
    if req.offer_amount > free_agency.team_money(db, sender)["fa_money"]:
        return f"{sender.name} no longer has ${req.offer_amount} of free-agency money"
    return None


def respond(db: Session, req: MatchupRequest, accept: bool, now: datetime.datetime | None = None) -> None:
    now = now or datetime.datetime.utcnow()
    if req.status != "pending":
        raise ValueError("That request has already been answered")
    _check_open(db, req.league_id, db.get(Week, req.week_id))
    if not accept:
        req.status, req.resolved_at = "declined", now
        return
    problem = _acceptable(db, req)
    if problem:
        raise ValueError(problem)
    _agree(db, req, now)


def close_stage(db: Session, league_id: int, rng: random.Random | None = None, now: datetime.datetime | None = None) -> dict:
    """AI teams answer the requests made to them (one -> accept, several -> pick one at random), every
    other open request is declined, and the stage closes for good."""
    rng = rng or random.Random()
    now = now or datetime.datetime.utcnow()
    accepted = 0
    ai_ids = {t.id for t in db.query(Team).filter_by(league_id=league_id, is_ai_owned=True)}
    by_target: dict[tuple[int, int], list[MatchupRequest]] = defaultdict(list)     # (week, AI team) -> requests
    for r in db.query(MatchupRequest).filter_by(league_id=league_id, status="pending").order_by(MatchupRequest.id):
        if r.to_team_id in ai_ids:
            by_target[(r.week_id, r.to_team_id)].append(r)
    for key in sorted(by_target, key=lambda k: rng.random()):
        options = [r for r in by_target[key] if r.status == "pending" and _acceptable(db, r) is None]
        if options:
            _agree(db, rng.choice(options), now)
            accepted += 1
    declined = 0
    for r in db.query(MatchupRequest).filter_by(league_id=league_id, status="pending"):
        r.status, r.resolved_at = "declined", now
        declined += 1
    settings = db.query(ScheduleSettings).filter_by(league_id=league_id).first()
    if settings is None:
        settings = ScheduleSettings(league_id=league_id)
        db.add(settings)
    settings.status, settings.closed_at = "closed", now
    db.flush()
    return {"accepted": accepted, "declined": declined}


# ------------------------------------------------------------------ conference championships
def pair_conference(ordered_team_ids: list[int]) -> tuple[list[tuple[int, int]], int | None]:
    """#1 v #2, #3 v #4, ...; with an odd number the last (worst) team gets a bye. Returns (games, bye team)."""
    pairs = [(ordered_team_ids[i], ordered_team_ids[i + 1]) for i in range(0, len(ordered_team_ids) - 1, 2)]
    bye = ordered_team_ids[-1] if len(ordered_team_ids) % 2 else None
    return pairs, bye


@dataclass
class Championship:
    conference_id: int | None
    team_a_id: int
    team_b_id: int


def championship_bracket(db: Session, league_id: int, regular_standings: list[dict]) -> tuple[list[Championship], list[int]]:
    """From the regular-season standings (best first): each conference's pairings and its bye team."""
    by_conf: dict[int | None, list[int]] = defaultdict(list)
    conf_of = {t.id: t.conference_id for t in db.query(Team).filter_by(league_id=league_id)}
    for row in regular_standings:
        by_conf[conf_of.get(row["team_id"])].append(row["team_id"])
    games, byes = [], []
    for conf_id in sorted(by_conf, key=lambda c: (c is None, c or 0)):
        pairs, bye = pair_conference(by_conf[conf_id])
        games.extend(Championship(conf_id, a, b) for a, b in pairs)
        if bye is not None:
            byes.append(bye)
    return games, byes
