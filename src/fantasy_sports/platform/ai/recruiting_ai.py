"""
Server-side AI for AI-owned teams in a recruiting draft.

Like platform/ai/auction_ai.py, this runs in-process against the engine -
there's no HTTP path that lets a caller act as an AI team. tick() makes each
AI team, with probability `intensity`, either raise one of its standing bids
or open a new one. Bids are placed the same way a person's are (through
engine.place_bid). Bids are only processed when a period ends, so the AI keeps
raising a standing bid through the period toward its own valuation.

The AI never sees hidden prices. It values a player at (his share of the
value model - last season's points) x (its weekly budget) x a per-team
aggression, opens at half that and creeps up toward it. Money is weekly and
accumulates, and no team can overspend (the engine rejects a bid the team
can't afford), so the AI sizes every bid to the room it has left and sits out
when it has none - a small program waits for its budget to build. It stops opening new bids once its
charges (which include money lost on bids for players it didn't get) plus expected cost of open bids reach its budget (the AI stays within
budget even though the rules allow overspending).

MOCK-ONLY SHORTCUT: player value is the demo dataset's own weekly scores
(pricing.value_by_asset): a player's worth - value over replacement, from last season's
stats or, for a player with none, his recruiting rating - the same measure the prices use.
Personality (Team.personality_profile) isn't consulted yet.
"""

from __future__ import annotations

import datetime
import random
from collections import Counter

from sqlalchemy.orm import Session

from fantasy_sports.api.helpers import draftable_asset_position
from fantasy_sports.platform.ai.auction_ai import POSITION_CAP, STARTER_MIN
from fantasy_sports.platform.auction.constants import STARTING_BUDGET_BY_PROGRAM_SIZE
from fantasy_sports.platform.db.models.core import (
    DraftableAsset,
    League,
    RecruitingAward,
    RecruitingBid,
    RecruitingCharge,
    RecruitingDraft,
    Team,
)
from fantasy_sports.platform.recruiting import engine, pricing

# What an open bid is assumed to cost when budgeting: the average player's price for the whole recruitment.
CANDIDATE_POOL = 80  # a team picks new targets from its top-N wanted players

_value_cache: dict[int, tuple[dict[int, float], float]] = {}
_position_cache: dict[int, str | None] = {}


def _aggression(team_id: int) -> float:
    return random.Random(team_id * 7919).uniform(0.7, 1.3)


def _values(db: Session, draft_id: int, teams: int, roster_size: int) -> tuple[dict[int, float], float]:
    if draft_id not in _value_cache:
        value = pricing.value_by_asset(db, teams, roster_size)
        nonzero = [v for v in value.values() if v > 0]
        _value_cache[draft_id] = (value, (sum(nonzero) / len(nonzero)) if nonzero else 1.0)
    return _value_cache[draft_id]


def _positions(db: Session, asset_ids: list[int]) -> dict[int, str | None]:
    missing = [a for a in asset_ids if a not in _position_cache]
    if missing:
        for asset in db.query(DraftableAsset).filter(DraftableAsset.id.in_(missing)):
            _position_cache[asset.id] = draftable_asset_position(asset)
    return _position_cache


def tick(
    db: Session, draft_id: int, intensity: float = 0.35, rng: random.Random | None = None,
    now: datetime.datetime | None = None,
) -> int:
    """One pass of AI bidding. Returns how many bids were placed or raised."""
    rng = rng or random.Random()
    draft = db.get(RecruitingDraft, draft_id)
    if draft is None:
        return 0
    engine.sync(db, draft.league_id, now)  # process any period that has ended
    if draft.status != "open" or engine.in_buffer(draft, now):
        return 0   # closed, or paused between weeks (no bids from anyone)
    league = db.get(League, draft.league_id)
    roster_size = league.draft_rounds
    value, mean_value = _values(
        db, draft_id, db.query(Team).filter_by(league_id=draft.league_id).count(), roster_size)

    awards = db.query(RecruitingAward).filter_by(draft_id=draft_id).all()
    charged: dict[int, int] = {}
    for c in db.query(RecruitingCharge).filter_by(draft_id=draft_id):
        charged[c.team_id] = charged.get(c.team_id, 0) + c.amount
    awarded = {a.draftable_asset_id for a in awards}
    all_ids = engine.pool_asset_ids(db, draft.league_id)
    pos = _positions(db, all_ids)

    ai_teams = [t for t in db.query(Team).filter_by(league_id=draft.league_id, is_ai_owned=True)]
    rng.shuffle(ai_teams)
    acted = 0

    for team in ai_teams:
        if rng.random() > intensity:
            continue
        if draft.status != "open":
            break
        weekly = STARTING_BUDGET_BY_PROGRAM_SIZE[team.program_size]
        available = weekly * engine.current_period(draft)
        expected_cost = pricing.ROLE_MEAN_PER_WEEK * draft.total_periods
        won = [a for a in awards if a.team_id == team.id]
        if len(won) >= roster_size:
            continue
        spent = charged.get(team.id, 0)  # includes money lost on players it didn't get
        my_bids = {
            b.draftable_asset_id: b
            for b in db.query(RecruitingBid).filter_by(draft_id=draft_id, team_id=team.id)
            if b.draftable_asset_id not in awarded
        }
        have = Counter(pos[a.draftable_asset_id] for a in won)
        unmet = {p for p, n in STARTER_MIN.items() if have[p] < n}

        def ceiling(asset_id: int) -> int:
            return max(1, round((value.get(asset_id, 0.0) / mean_value) * weekly * _aggression(team.id)))

        try:
            raisable = [b for b in my_bids.values() if b.amount < ceiling(b.draftable_asset_id)]
            if raisable and rng.random() < 0.6:
                b = rng.choice(raisable)
                step = max(1, round(ceiling(b.draftable_asset_id) * rng.uniform(0.05, 0.2)))
                new_amount = min(ceiling(b.draftable_asset_id), b.amount + step,
                                 engine.bid_room(db, draft, team.id, exclude_asset_id=b.draftable_asset_id))
                if new_amount <= b.amount:
                    continue  # can't afford to raise
                engine.place_bid(db, draft_id, team.id, b.draftable_asset_id, new_amount, now=now)
                acted += 1
            else:
                if spent + expected_cost * len(my_bids) >= available or len(won) + len(my_bids) >= roster_size:
                    continue
                candidates = [
                    a for a in all_ids
                    if a not in awarded and a not in my_bids and pos[a] in POSITION_CAP
                    and (have[pos[a]] < POSITION_CAP[pos[a]] or not unmet)
                ]
                if unmet and rng.random() < 0.6:
                    needed = [a for a in candidates if pos[a] in unmet]
                    candidates = needed or candidates
                if not candidates:
                    continue
                candidates.sort(key=lambda a: value.get(a, 0.0), reverse=True)
                top = candidates[:CANDIDATE_POOL]
                target = rng.choices(top, weights=[value.get(a, 0.0) + 1.0 for a in top])[0]
                opening = min(max(1, round(ceiling(target) * 0.5)), engine.bid_room(db, draft, team.id))
                if opening < 1:
                    continue  # no money to spare right now - wait for next week's budget
                engine.place_bid(db, draft_id, team.id, target, opening, now=now)
                acted += 1
        except ValueError:
            continue  # e.g. the roster filled between the check and the bid
    return acted
