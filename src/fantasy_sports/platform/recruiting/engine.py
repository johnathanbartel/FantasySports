"""
Recruiting-draft engine: a series of fixed-length periods - shown to users as
WEEKS - one per roster spot (20 for a 20-man roster), each as long as the
commissioner sets. During any period every team can bid on
any player at any time; there are no nominations or turn order.

How bids become awards - only when a period ENDS:
  * Every player has a hidden price (RecruitingThreshold, see pricing.py).
  * A team keeps at most one standing bid per player and can raise, lower or
    retract it whenever the draft is open. Standing bids carry over from
    period to period until the player is awarded.
  * When a period ends, every player whose bids SUM to at least his hidden
    price is awarded to the highest bidder (ties broken by random choice among
    the tied teams). The money bid on a player goes to the player: EVERY team
    that had a bid on him is charged its own bid, winner and losers alike (a
    team that bid $5 on a player who went to someone else loses that $5). The
    hidden price is only the trigger, never what anyone pays.
  * Players whose price isn't met stay on the board with their standing bids.
  * When the FINAL period ends, the highest bidder gets every player who has
    any bids, regardless of price (every bidder charged their bid, same as
    above), and the draft closes. It also closes early if every roster is full.
  * Money works like the season: a team's program-size budget ($300 / $200 /
    $100) is its WEEKLY budget, and it accumulates - skip week 1 as a Large
    program and you have $600 in week 2. A player's hidden price is his cost for
    the whole recruitment (avg $10 x the number of weeks), and what a team
    actually pays is its own bid. A team can NOT overspend in any week: a bid
    is rejected if it, plus what the team has already been charged and its other
    standing bids, would exceed the money accrued so far (weekly budget x the
    current week). Standing bids therefore act as commitments - they are
    exactly what would be charged if every one of them were awarded - and a
    later week only ever adds money, so no award can push a team over. Roster size still caps how many players a
    team can win (League.draft_rounds); a full team's bids can't win.

Between periods there is a short BUFFER (20 seconds by default): the period's
bids have just been processed, teams are shown their results, and NO bid can be
placed, changed or retracted until the next period starts. Timeline for period
n: it accepts bids from period_start(n) to period_end(n), is processed at
period_end(n), then the buffer runs before period n+1 begins.

Each processing also snapshots every team's per-bid result (won / lost /
carried over, and the player's completion % = total bids / hidden price) into
RecruitingBidResult, which the API shows only to the bidding team.

Periods advance lazily: every read/write goes through sync(), which processes
any period that has ended since the last call, so there's no background
worker to keep alive. (In a real deployment a scheduler would call sync()
too, so results land on time even if nobody is looking.)

Domain-rule violations raise ValueError; the API router turns them into
HTTPExceptions, same convention as platform/auction/engine.py.
"""

from __future__ import annotations

import datetime
import random

from sqlalchemy import or_
from sqlalchemy.orm import Session

from fantasy_sports.platform.auction.constants import STARTING_BUDGET_BY_PROGRAM_SIZE
from fantasy_sports.platform.defense_modes import in_pool, league_modes
from fantasy_sports.platform.league_fill import assert_league_full
from fantasy_sports.platform.db.models.core import (
    DraftableAsset,
    League,
    LeagueUniverse,
    Player,
    RecruitingAward,
    RecruitingBid,
    RecruitingBidResult,
    RecruitingCharge,
    RecruitingDraft,
    RecruitingSettings,
    RecruitingThreshold,
    RosterEntry,
    Team,
)
from fantasy_sports.platform import free_agency, season
from fantasy_sports.platform.recruiting import pricing

RNG = random.SystemRandom()
DEFAULT_PERIOD_SECONDS = 86400
DEFAULT_BUFFER_SECONDS = 20


def pool_asset_ids(db: Session, league_id: int) -> list[int]:
    """Every player and defense group in the league's universe(s) - one shared
    pool, defense groups bid on exactly like players."""
    modes = league_modes(db, league_id)
    return [
        a.id for a in db.query(DraftableAsset)
        .outerjoin(Player, Player.id == DraftableAsset.player_id)
        .filter(DraftableAsset.universe_id.in_(list(modes) or [0]),
                or_(DraftableAsset.player_id.is_(None), Player.active.is_(True)))
        if in_pool(a, modes)
    ]


def buffer_length(db: Session, league_id: int) -> int:
    settings = db.query(RecruitingSettings).filter_by(league_id=league_id).first()
    return settings.buffer_seconds if settings else DEFAULT_BUFFER_SECONDS


def get_draft(db: Session, league_id: int) -> RecruitingDraft | None:
    return db.query(RecruitingDraft).filter_by(league_id=league_id).first()


def period_length(db: Session, league_id: int) -> int:
    settings = db.query(RecruitingSettings).filter_by(league_id=league_id).first()
    return settings.period_seconds if settings else DEFAULT_PERIOD_SECONDS


def period_length_with_buffer(draft: RecruitingDraft) -> int:
    return draft.period_seconds + draft.buffer_seconds


def period_start(draft: RecruitingDraft, period_number: int) -> datetime.datetime:
    """When period n opens for bids (period 1 opens the moment the draft does)."""
    return draft.started_at + datetime.timedelta(seconds=period_length_with_buffer(draft) * (period_number - 1))


def period_end(draft: RecruitingDraft, period_number: int) -> datetime.datetime:
    """When period n stops taking bids and its bids are processed."""
    return period_start(draft, period_number) + datetime.timedelta(seconds=draft.period_seconds)


def in_buffer(draft: RecruitingDraft, now: datetime.datetime | None = None) -> bool:
    """True between a processed period and the start of the next one: no bids."""
    now = now or datetime.datetime.utcnow()
    k = draft.periods_processed
    return draft.status == "open" and 1 <= k < draft.total_periods and now < period_start(draft, k + 1)


def current_period(draft: RecruitingDraft) -> int:
    """The period bids are currently collected for (1-based); after the draft
    closes, the last period."""
    return min(draft.periods_processed + 1, draft.total_periods)


def open_draft(
    db: Session, league_id: int, now: datetime.datetime | None = None, rng: random.Random | None = None
) -> RecruitingDraft:
    """Opens period 1 and draws every player's hidden price."""
    now = now or datetime.datetime.utcnow()
    league = db.get(League, league_id)
    if league is None:
        raise ValueError("League not found")
    if get_draft(db, league_id) is not None:
        raise ValueError("This league's recruiting draft has already started")
    if db.query(Team).filter(Team.league_id == league_id).count() == 0:
        raise ValueError("League has no teams yet")
    assert_league_full(db, league)

    draft = RecruitingDraft(
        league_id=league_id, status="open", started_at=now,
        period_seconds=period_length(db, league_id), buffer_seconds=buffer_length(db, league_id),
        total_periods=league.draft_rounds,
    )
    db.add(draft)
    db.flush()

    asset_ids = pool_asset_ids(db, league_id)
    teams = db.query(Team).filter(Team.league_id == league_id).count()
    worth, drafted, groups = pricing.price_inputs(db, asset_ids, teams, league.draft_rounds)
    thresholds = pricing.generate_thresholds(
        asset_ids, worth, rng or random.Random(), league.draft_rounds, drafted, groups)
    for asset_id, (threshold, is_elite) in thresholds.items():
        db.add(RecruitingThreshold(
            draft_id=draft.id, draftable_asset_id=asset_id, threshold=threshold, is_elite=is_elite,
        ))
    db.flush()
    return draft


def sync(db: Session, league_id: int, now: datetime.datetime | None = None) -> RecruitingDraft | None:
    """Brings the league's recruiting draft up to date: opens it if its
    scheduled start (League.draft_scheduled_at) has passed, then processes
    every period that has ended. Returns the draft, or None if not started."""
    now = now or datetime.datetime.utcnow()
    draft = get_draft(db, league_id)
    if draft is None:
        league = db.get(League, league_id)
        scheduled = league.draft_scheduled_at if league else None
        if scheduled is None or now < scheduled.replace(tzinfo=None):
            return None
        try:
            assert_league_full(db, league)
        except ValueError:
            return None   # an unfilled league waits: its scheduled start doesn't open the draft
        draft = open_draft(db, league_id, now)
    while draft.status == "open" and period_end(draft, draft.periods_processed + 1) <= now:
        _process_period(db, draft, at=period_end(draft, draft.periods_processed + 1))
    return draft


def roster_counts(db: Session, draft: RecruitingDraft) -> dict[int, int]:
    counts = {t.id: 0 for t in db.query(Team).filter(Team.league_id == draft.league_id)}
    for a in db.query(RecruitingAward).filter_by(draft_id=draft.id):
        counts[a.team_id] += 1
    return counts


def team_finances(db: Session, draft: RecruitingDraft) -> dict[int, dict]:
    """Per team: the weekly budget, how many weeks of it have accrued so far
    (the current week counts), what is available (weekly x weeks), total
    charged (bids on players who were awarded - won or lost), how much of that
    was lost on players it didn't get, and how much is committed to standing
    bids (private to the team - the router only shows it to its owner)."""
    spent: dict[int, int] = {}
    lost: dict[int, int] = {}
    roster_cost: dict[int, int] = {}   # what each team paid for the players on its roster
    for team_id in {t.id for t in db.query(Team).filter(Team.league_id == draft.league_id)}:
        signed = free_agency.fa_spent(db, team_id)   # free-agent signings are part of roster cost too
        if signed:
            roster_cost[team_id] = signed
    for c in db.query(RecruitingCharge).filter_by(draft_id=draft.id):
        spent[c.team_id] = spent.get(c.team_id, 0) + c.amount
        if c.is_winner:
            roster_cost[c.team_id] = roster_cost.get(c.team_id, 0) + c.amount
        else:
            lost[c.team_id] = lost.get(c.team_id, 0) + c.amount
    counts = roster_counts(db, draft)
    # Weeks of budget accrued: the current week counts while the draft is open; once
    # it has closed, only the weeks that actually ran (it can close early if every
    # roster fills).
    weeks = current_period(draft) if draft.status == "open" else draft.periods_processed
    taken = {a.draftable_asset_id for a in db.query(RecruitingAward).filter_by(draft_id=draft.id)}
    committed: dict[int, int] = {}
    for b in db.query(RecruitingBid).filter_by(draft_id=draft.id):
        if b.draftable_asset_id not in taken:
            committed[b.team_id] = committed.get(b.team_id, 0) + b.amount
    out = {}
    for t in db.query(Team).filter(Team.league_id == draft.league_id):
        weekly = STARTING_BUDGET_BY_PROGRAM_SIZE[t.program_size]
        available = weekly * weeks
        s = spent.get(t.id, 0)
        out[t.id] = {
            "weekly_budget": weekly, "weeks_funded": weeks, "available": available, "spent": s,
            "lost_on_bids": lost.get(t.id, 0), "remaining": available - s,
            "committed": committed.get(t.id, 0), "roster_count": counts[t.id],
            # The budget panel: recruiting money over the whole draft, and season money
            # (weekly x the season's weeks + the recruiting total) less what the roster cost.
            "recruiting_budget_total": weekly * draft.total_periods,
            "roster_cost": roster_cost.get(t.id, 0),
            "season_budget_total": season.season_budget_total(weekly, weekly * draft.total_periods),
            "season_weekly_budget": season.season_weekly_budget(
                season.season_budget_total(weekly, weekly * draft.total_periods), roster_cost.get(t.id, 0)),
        }
    return out


def bid_room(db: Session, draft: RecruitingDraft, team_id: int, exclude_asset_id: int | None = None) -> int:
    """How much more this team may commit right now: the money accrued so far
    (weekly budget x current week) minus what it has been charged minus its
    other standing bids on players who are still up for grabs. Pass
    exclude_asset_id to get the most it could bid on that one player (its
    existing bid on him doesn't count against the room)."""
    team = db.get(Team, team_id)
    available = STARTING_BUDGET_BY_PROGRAM_SIZE[team.program_size] * current_period(draft)
    spent = sum(c.amount for c in db.query(RecruitingCharge).filter_by(draft_id=draft.id, team_id=team_id))
    taken = {a.draftable_asset_id for a in db.query(RecruitingAward).filter_by(draft_id=draft.id)}
    committed = sum(
        b.amount for b in db.query(RecruitingBid).filter_by(draft_id=draft.id, team_id=team_id)
        if b.draftable_asset_id not in taken and b.draftable_asset_id != exclude_asset_id
    )
    return available - spent - committed


def place_bid(
    db: Session, draft_id: int, team_id: int, asset_id: int, amount: int,
    now: datetime.datetime | None = None,
) -> None:
    """Sets (or with amount=0 retracts) a team's standing bid on a player. It
    is only processed when the current period ends."""
    now = now or datetime.datetime.utcnow()
    draft = db.get(RecruitingDraft, draft_id)
    if draft is None:
        raise ValueError("Recruiting draft not found")
    sync(db, draft.league_id, now)
    if draft.status != "open":
        raise ValueError("The recruiting draft is closed")
    if in_buffer(draft, now):
        wait = max(1, round((period_start(draft, draft.periods_processed + 1) - now).total_seconds()))
        raise ValueError(
            f"Bidding is paused between weeks - week {draft.periods_processed + 1} opens in {wait}s. "
            f"Bids can't be placed, changed or retracted until then."
        )
    if amount < 0:
        raise ValueError("Bid can't be negative")

    team = db.get(Team, team_id)
    if team is None or team.league_id != draft.league_id:
        raise ValueError("Team is not part of this recruiting draft")
    if db.query(RecruitingThreshold).filter_by(draft_id=draft_id, draftable_asset_id=asset_id).first() is None:
        raise ValueError("That player isn't in this league's pool")
    if db.query(RecruitingAward).filter_by(draft_id=draft_id, draftable_asset_id=asset_id).first():
        raise ValueError("That player has already been recruited")

    league = db.get(League, draft.league_id)
    if roster_counts(db, draft)[team_id] >= league.draft_rounds:
        raise ValueError("Roster is already full - this team can't bid again")

    room = bid_room(db, draft, team_id, exclude_asset_id=asset_id)
    if amount > room:
        raise ValueError(
            f"That bid is more than you can afford: you can put at most ${max(0, room)} on this player right now "
            f"(your budget accrues weekly, and money already spent or committed to your other bids is unavailable)"
        )

    existing = db.query(RecruitingBid).filter_by(
        draft_id=draft_id, team_id=team_id, draftable_asset_id=asset_id).first()
    if amount == 0:
        if existing is not None:
            db.delete(existing)
    elif existing is not None:
        existing.amount, existing.updated_at = amount, now
    else:
        db.add(RecruitingBid(draft_id=draft_id, team_id=team_id, draftable_asset_id=asset_id,
                             amount=amount, updated_at=now))
    db.flush()


def _process_period(db: Session, draft: RecruitingDraft, at: datetime.datetime) -> None:
    """Processes the current period's bids. Players are settled highest-top-bid
    first (random among equals) so a team nearing a full roster spends its last
    spots on what it bid most for. Only teams with a free roster spot can win
    or count toward a price."""
    league = db.get(League, draft.league_id)
    period_number = draft.periods_processed + 1
    final = period_number == draft.total_periods
    counts = roster_counts(db, draft)
    thresholds = {t.draftable_asset_id: t.threshold
                  for t in db.query(RecruitingThreshold).filter_by(draft_id=draft.id)}
    taken = {a.draftable_asset_id for a in db.query(RecruitingAward).filter_by(draft_id=draft.id)}

    by_asset: dict[int, list[RecruitingBid]] = {}
    for b in db.query(RecruitingBid).filter_by(draft_id=draft.id):
        if b.draftable_asset_id not in taken:
            by_asset.setdefault(b.draftable_asset_id, []).append(b)

    order = sorted(by_asset, key=lambda a: (max(b.amount for b in by_asset[a]), RNG.random()), reverse=True)
    for asset_id in order:
        eligible = [b for b in by_asset[asset_id] if counts[b.team_id] < league.draft_rounds]
        if not eligible:
            continue
        total = sum(b.amount for b in eligible)
        completion = round(100.0 * total / thresholds[asset_id], 1)
        met = total >= thresholds[asset_id]
        if not (met or final):
            for b in eligible:   # his price wasn't met: the bid carries over, uncharged
                db.add(RecruitingBidResult(
                    draft_id=draft.id, period_number=period_number, team_id=b.team_id,
                    draftable_asset_id=asset_id, bid_amount=b.amount, outcome="pending",
                    completion_pct=completion, charged=0))
            continue
        top = max(b.amount for b in eligible)
        tied = [b for b in eligible if b.amount == top]
        winner = tied[0] if len(tied) == 1 else RNG.choice(tied)
        db.add(RecruitingAward(
            draft_id=draft.id, draftable_asset_id=asset_id, team_id=winner.team_id,
            period_number=period_number, price=winner.amount,
            reason="threshold" if met else "deadline",
            was_tie_broken_randomly=len(tied) > 1, awarded_at=at,
        ))
        db.add(RosterEntry(team_id=winner.team_id, draftable_asset_id=asset_id, acquired_via="recruiting"))
        counts[winner.team_id] += 1
        for b in eligible:  # everyone who bid on him pays their bid
            db.add(RecruitingCharge(
                draft_id=draft.id, team_id=b.team_id, draftable_asset_id=asset_id,
                period_number=period_number, amount=b.amount, is_winner=b.team_id == winner.team_id,
            ))
            db.add(RecruitingBidResult(
                draft_id=draft.id, period_number=period_number, team_id=b.team_id,
                draftable_asset_id=asset_id, bid_amount=b.amount,
                outcome="won" if b.team_id == winner.team_id else "lost",
                completion_pct=completion, charged=b.amount))

    draft.periods_processed = period_number
    if final or all(n >= league.draft_rounds for n in counts.values()):
        draft.status, draft.ended_at = "closed", at
    db.flush()
    if draft.status == "closed":
        # Teams may recruit fewer players than the roster size; the AI's open spots are
        # filled through free agency, which opens now (see platform/free_agency.py).
        free_agency.ai_fill_rosters(db, draft.league_id, at)


def process_period_now(db: Session, draft_id: int, now: datetime.datetime | None = None) -> RecruitingDraft:
    """Commissioner override: ends the current period immediately and processes
    its bids. The remaining time in the period is skipped (the schedule
    shifts earlier by that amount), so later periods keep their full length."""
    now = now or datetime.datetime.utcnow()
    draft = db.get(RecruitingDraft, draft_id)
    if draft is None:
        raise ValueError("Recruiting draft not found")
    sync(db, draft.league_id, now)
    if draft.status != "open":
        raise ValueError("The recruiting draft is already closed")
    if in_buffer(draft, now):
        raise ValueError("Between weeks - wait for the next week to open before processing it")
    remaining = period_end(draft, draft.periods_processed + 1) - now
    draft.started_at -= remaining
    _process_period(db, draft, at=now)
    return draft


def end_draft(db: Session, draft_id: int, now: datetime.datetime | None = None) -> RecruitingDraft:
    """Commissioner override: ends the whole draft now. The period in progress
    is processed as the final one - unmet players go to their highest bidder."""
    now = now or datetime.datetime.utcnow()
    draft = db.get(RecruitingDraft, draft_id)
    if draft is None:
        raise ValueError("Recruiting draft not found")
    sync(db, draft.league_id, now)
    if draft.status != "open":
        raise ValueError("The recruiting draft is already closed")
    draft.total_periods = draft.periods_processed + 1
    remaining = period_end(draft, draft.periods_processed + 1) - now
    draft.started_at -= remaining
    _process_period(db, draft, at=now)
    return draft
