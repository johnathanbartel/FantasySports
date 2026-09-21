"""
Pydantic request/response models. Response models set from_attributes=True
so they read directly off the SQLAlchemy ORM objects returned by db/models.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Leagues ----------------------------------------------------------------

class LeagueCreateRequest(BaseModel):
    name: str
    commissioner_user_id: int
    universe_codes: list[str]  # e.g. ["ncaaf"], or ["ncaaf", "nfl"] for Dual Universe
    size: int = 12
    draft_rounds: int = 20


class LeagueOut(ORMModel):
    id: int
    name: str
    commissioner_user_id: int
    size: int
    draft_rounds: int
    salary_cap_enabled: bool


class StandingsRow(BaseModel):
    team_id: int
    team_name: str
    wins: int
    losses: int
    points_for: float
    points_against: float


# --- Teams / rosters ----------------------------------------------------------

class TeamOut(ORMModel):
    id: int
    league_id: int
    name: str
    owner_user_id: int | None
    is_ai_owned: bool


class RosterEntryOut(BaseModel):
    draftable_asset_id: int
    asset_type: str
    display_name: str  # player full_name, or "<Real Team> <Group>" for defense groups


class LineupSetRequest(BaseModel):
    week_id: int
    # slot_code -> draftable_asset_id, e.g. {"QB": 501, "RB": 502, ...}
    assignments: dict[str, int]


class LineupEntryOut(BaseModel):
    week_id: int
    roster_slot_id: int
    slot_code: str
    draftable_asset_id: int
    display_name: str


# --- Draft --------------------------------------------------------------------

class DraftStartRequest(BaseModel):
    league_id: int


class DraftOut(ORMModel):
    id: int
    league_id: int
    status: str
    started_at: datetime.datetime | None
    completed_at: datetime.datetime | None


class DraftPickOut(BaseModel):
    id: int
    round: int
    pick_number: int
    team_id: int
    draftable_asset_id: int | None
    display_name: str | None
    picked_at: datetime.datetime | None
    is_autopick: bool


class MakePickRequest(BaseModel):
    draftable_asset_id: int


class DraftBoardAsset(BaseModel):
    draftable_asset_id: int
    asset_type: str
    display_name: str
    position: str | None = None  # player position, or "DL"/"LB"/"DB" for defense groups


# --- Scoring --------------------------------------------------------------------

class PlayCreditDetail(BaseModel):
    play_id: int
    role: str
    credit_points: float
    play_text: str | None


class WeeklyScoreBreakdown(BaseModel):
    draftable_asset_id: int
    display_name: str
    week_id: int
    total_points: float
    play_credits: list[PlayCreditDetail]


class TeamWeeklyScoreOut(BaseModel):
    team_id: int
    week_id: int
    total_points: float


class MatchupOut(ORMModel):
    id: int
    league_id: int
    week_id: int
    team_a_id: int
    team_b_id: int
    team_a_score: float | None
    team_b_score: float | None
    winner_team_id: int | None


# --- Timed Auction Draft ---------------------------------------------------
#
# The auction's parameters are per-league settings, not football constants:
# every field below that has a default falls back to the sport's configured
# default (sports/football/college/auction.py for NCAAF) rather than to a
# value hardcoded in the engine.


class AuctionTierConfigIn(BaseModel):
    code: str
    display_name: str
    starting_budget: int
    sort_order: int = 0


class AuctionStartRequest(BaseModel):
    league_id: int
    # Per-slot rounds in the Recruiting Period; defaults to League.draft_rounds.
    slots: int | None = None
    # Set the per-round window directly (must respect the 3-minute floor)...
    round_seconds: int | None = None
    # ...or set the whole period and let the window fall out of it.
    recruiting_period_seconds: int | None = None
    min_bid: int | None = None
    # Omit to use the sport's tiers (Large/Moderate/Small for college football).
    tiers: list[AuctionTierConfigIn] | None = None
    default_tier_code: str | None = None
    # team_id -> tier code; unlisted teams get the default tier.
    team_tiers: dict[int, str] = {}
    starts_at: datetime.datetime | None = None


class AuctionRoundOut(BaseModel):
    round_number: int
    status: str
    opens_at: datetime.datetime
    closes_at: datetime.datetime
    resolved_at: datetime.datetime | None
    seconds_remaining: int | None  # null unless the round is open


class AuctionTierOut(BaseModel):
    code: str
    display_name: str
    starting_budget: int


class AuctionBudgetOut(BaseModel):
    team_id: int
    team_name: str
    tier_code: str
    tier_display_name: str
    starting_budget: int
    spent_budget: int
    remaining_budget: int


class AuctionSessionOut(BaseModel):
    id: int
    league_id: int
    draft_id: int
    status: str
    total_rounds: int
    round_seconds: int
    minimum_round_seconds: int
    recruiting_period_seconds: int
    min_bid: int
    starts_at: datetime.datetime | None
    completed_at: datetime.datetime | None
    current_round: AuctionRoundOut | None
    tiers: list[AuctionTierOut]


class AuctionBidRequest(BaseModel):
    team_id: int
    draftable_asset_id: int
    amount: int


class AuctionBidOut(BaseModel):
    id: int
    round_number: int
    team_id: int
    draftable_asset_id: int
    display_name: str
    amount: int
    status: str
    lost_reason: str | None
    submitted_at: datetime.datetime


class AuctionContestOut(BaseModel):
    """One asset's outcome in a resolved round."""

    round_number: int
    draftable_asset_id: int
    display_name: str
    position: str | None
    winning_team_id: int
    winning_amount: int
    bid_count: int
    tie_break_used: bool
    tied_team_ids: list[int]


class AuctionRoundRevealOut(BaseModel):
    """Post-resolution reveal: every bid in the round, plus who won what.

    `bids` is empty while the round is still open - blind means blind.
    """

    round_number: int
    status: str
    opens_at: datetime.datetime
    closes_at: datetime.datetime
    resolved_at: datetime.datetime | None
    bid_count: int
    bids: list[AuctionBidOut]
    contests: list[AuctionContestOut]
    next_round_number: int | None = None
    session_complete: bool = False
