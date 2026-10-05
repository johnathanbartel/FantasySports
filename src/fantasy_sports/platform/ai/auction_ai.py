"""
Server-side AI for AI-owned teams in a timed auction (recruiting period).

Runs entirely in-process against the auction engine - there is no HTTP path
that lets a caller "act as" an AI team (see api/auth.py). tick() is the one
entry point: it resolves an expired round, has every AI team without a bid
place one, or nominates when an AI team is on the clock, and reports what it
did.

MOCK-ONLY SHORTCUT: assets are priced off the demo dataset's own weekly
scores (WeeklyPlayerScore totals), i.e. the AI "sees the future". That is
fine for exercising the auction end-to-end and is NOT a real drafting
strategy - swap value_by_asset() for a projection model before anything is
real. Personality (Team.personality_profile) isn't consulted yet; each AI's
aggression is a fixed pseudo-random draw seeded by team id.

Partial rosters are allowed by design: the AI only buys what it wants and
never tries to force a full roster.
"""

from __future__ import annotations

import datetime
import random
from collections import Counter, defaultdict

from sqlalchemy.orm import Session

from fantasy_sports.api.helpers import draftable_asset_position
from fantasy_sports.platform.auction import engine
from fantasy_sports.platform.auction.constants import MINIMUM_BID
from fantasy_sports.platform.db.models.core import (
    AuctionBid,
    AuctionDraft,
    AuctionRound,
    AuctionTeamBudget,
    DraftableAsset,
    League,
    RosterEntry,
    Team,
)
from fantasy_sports.sports.football.models import WeeklyPlayerScore

# Per-position roster caps an AI will fill, and the starter minimums it
# chases first (same shape as scripts/run_mock_auction_draft.py's bots).
POSITION_CAP = {"QB": 2, "RB": 5, "WR": 5, "TE": 3, "PK": 2, "K": 2, "DL": 2, "LB": 2, "DB": 2}
STARTER_MIN = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "PK": 1, "K": 1, "DL": 1, "LB": 1, "DB": 1}


def _aggression(team_id: int) -> float:
    return random.Random(team_id * 7919).uniform(0.7, 1.3)


# asset id -> prior value. When set (the NFL mock run sets it from the previous season's credit), the AI prices
# from this instead of the database's own weekly scores, so the draft has no look-ahead.
PRIOR_VALUE: dict[int, float] | None = None


def value_by_asset(db: Session) -> dict[int, float]:
    if PRIOR_VALUE is not None:
        return defaultdict(float, PRIOR_VALUE)
    value: dict[int, float] = defaultdict(float)
    for s in db.query(WeeklyPlayerScore):
        value[s.draftable_asset_id] += s.points
    return value


class _TeamState:
    def __init__(self, team: Team, budget: AuctionTeamBudget, positions: Counter, roster_size: int):
        self.team = team
        self.budget = budget
        self.pos = positions
        self.roster_size = roster_size

    @property
    def slots_left(self) -> int:
        return self.roster_size - self.budget.assets_won

    def unmet_starters(self) -> set[str]:
        return {p for p, n in STARTER_MIN.items() if self.pos[p] < n}

    def wants(self, position: str | None) -> bool:
        if self.slots_left <= 0 or position not in POSITION_CAP:
            return False
        if self.pos[position] < POSITION_CAP[position]:
            return True
        # Past the caps, a team with every starter minimum met takes any
        # position - otherwise the endgame stalls on what the pool has left.
        return not self.unmet_starters()

    def bid_amount(self, asset_value: float, mean_value: float, position: str | None) -> int | None:
        ceiling = self.budget.remaining_budget - (self.slots_left - 1) * MINIMUM_BID
        if ceiling < MINIMUM_BID:
            return None
        fair = self.budget.remaining_budget / self.slots_left
        raw = (asset_value / mean_value) * fair * _aggression(self.team.id)
        if position in self.unmet_starters():
            raw *= 1.25
        return max(MINIMUM_BID, min(ceiling, int(round(raw))))


def _states(db: Session, auction: AuctionDraft, ai_only: bool) -> dict[int, _TeamState]:
    league = db.get(League, auction.league_id)
    teams = {t.id: t for t in db.query(Team).filter(Team.league_id == auction.league_id)}
    positions: dict[int, Counter] = defaultdict(Counter)
    for e in db.query(RosterEntry).filter(RosterEntry.team_id.in_(list(teams))):
        positions[e.team_id][draftable_asset_position(e.draftable_asset)] += 1
    out = {}
    for b in db.query(AuctionTeamBudget).filter(AuctionTeamBudget.auction_draft_id == auction.id):
        team = teams[b.team_id]
        if ai_only and not team.is_ai_owned:
            continue
        out[team.id] = _TeamState(team, b, positions[team.id], league.draft_rounds)
    return out


def _open_round(db: Session, auction_id: int) -> AuctionRound | None:
    return (
        db.query(AuctionRound)
        .filter(AuctionRound.auction_draft_id == auction_id, AuctionRound.status == "open")
        .first()
    )


def tick(db: Session, auction_id: int, now: datetime.datetime | None = None) -> list[str]:
    """One pass of AI activity. Returns human-readable action strings."""
    now = now or datetime.datetime.utcnow()
    actions: list[str] = []
    auction = db.get(AuctionDraft, auction_id)
    value = value_by_asset(db)
    nonzero = [v for v in value.values() if v > 0]
    mean_value = (sum(nonzero) / len(nonzero)) if nonzero else 1.0

    # Bounded loop: resolving a round can put another AI team on the clock,
    # whose nomination we handle in the same pass.
    for _ in range(3):
        db.refresh(auction)
        if auction.status != "in_progress":
            break
        round_ = _open_round(db, auction_id)

        if round_ is not None and engine.is_round_expired(round_, now):
            engine.resolve_round(db, round_.id, now=now)
            actions.append(f"resolved round {round_.round_number}")
            continue

        if round_ is not None:
            position = draftable_asset_position(round_.draftable_asset)
            already = {b.team_id for b in db.query(AuctionBid).filter_by(auction_round_id=round_.id)}
            for tid, st in _states(db, auction, ai_only=True).items():
                if tid in already or not st.wants(position):
                    continue
                amount = st.bid_amount(value.get(round_.draftable_asset_id, 0.0), mean_value, position)
                if amount is None:
                    continue
                engine.place_bid(db, round_.id, tid, amount, now=now)
                actions.append(f"team {tid} bid on round {round_.round_number}")
            break

        # No open round: nominate if an AI team is on the clock.
        on_clock = auction.nomination_order[auction.next_nominator_index]
        states = _states(db, auction, ai_only=False)
        st = states[on_clock]
        if not st.team.is_ai_owned:
            break
        board = engine.available_assets(db, auction)
        if not board:
            break
        need = st.unmet_starters()
        anyone_wants = lambda pos: any(s.wants(pos) for s in states.values())  # noqa: E731
        ranked = sorted(board, key=lambda a: value.get(a.id, 0.0), reverse=True)
        pick = (
            next((a for a in ranked if draftable_asset_position(a) in need), None)
            or next((a for a in ranked if st.wants(draftable_asset_position(a))), None)
            or next((a for a in ranked if anyone_wants(draftable_asset_position(a))), None)
            or ranked[0]
        )
        round_ = engine.open_round(db, auction_id, pick.id, on_clock)
        actions.append(f"team {on_clock} nominated for round {round_.round_number}")
        # Loop again so the AI teams bid on their own nomination at once.
    return actions
