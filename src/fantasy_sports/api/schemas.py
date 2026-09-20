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
    # universe_code -> "group"/"individual", e.g. {"nfl": "individual"}.
    # Any universe_code not present here defaults to "group". Rejected at
    # creation if a universe doesn't support the requested mode (see
    # Universe.supports_individual_defense).
    defense_modes: dict[str, str] = {}
    # "team_choice" (default) or "dynamic". "dynamic" is rejected at
    # creation for now - see League.program_size_mode.
    program_size_mode: str = "team_choice"
    # "recruit" (timed sealed-bid auction, shown to teams as a "recruiting
    # period") or "snake" (default - a traditional snake draft run at a
    # scheduled time). Drives which screen the create-league wizard sends a
    # newly created team to next.
    draft_type: str = "snake"
    # Only used when draft_type="snake" - when the commissioner plans to run
    # the live draft, so the roster page can tell teams when to expect it.
    draft_scheduled_at: datetime.datetime | None = None


class LeagueOut(ORMModel):
    id: int
    name: str
    commissioner_user_id: int
    size: int
    draft_rounds: int
    salary_cap_enabled: bool
    program_size_mode: str
    draft_type: str
    draft_scheduled_at: datetime.datetime | None


class ConferenceOut(BaseModel):
    id: int
    program_size: str
    name: str
    team_count: int


class StandingsRow(BaseModel):
    team_id: int
    team_name: str
    wins: int
    losses: int
    points_for: float
    points_against: float


# --- Teams / rosters ----------------------------------------------------------

class TeamCreateRequest(BaseModel):
    league_id: int
    name: str
    owner_user_id: int | None = None
    is_ai_owned: bool = False
    program_size: str = "small"  # "large"/"moderate"/"small" - see platform/team_tiers.py


class TeamOut(ORMModel):
    id: int
    league_id: int
    name: str
    owner_user_id: int | None
    is_ai_owned: bool
    program_size: str
    conference_id: int | None
    season_goal: str


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


# --- Auction draft --------------------------------------------------------------

class AuctionStartRequest(BaseModel):
    league_id: int
    round_seconds: int = 30


class AuctionDraftOut(ORMModel):
    id: int
    league_id: int
    status: str
    round_seconds: int
    nomination_order: list[int]
    next_nominator_index: int
    started_at: datetime.datetime | None
    completed_at: datetime.datetime | None


class AuctionBudgetOut(BaseModel):
    team_id: int
    starting_budget: int
    remaining_budget: int
    assets_won: int


class NominateRequest(BaseModel):
    draftable_asset_id: int
    nominated_by_team_id: int


class AuctionRoundOut(BaseModel):
    id: int
    round_number: int
    draftable_asset_id: int
    display_name: str
    nominated_by_team_id: int
    opens_at: datetime.datetime
    closes_at: datetime.datetime
    seconds_remaining: float
    status: str
    winning_team_id: int | None
    winning_bid_amount: int | None
    was_tie_broken_randomly: bool


class PlaceBidRequest(BaseModel):
    team_id: int
    amount: int


class AuctionBidOut(BaseModel):
    team_id: int
    amount: int
    submitted_at: datetime.datetime


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
