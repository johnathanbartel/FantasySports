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
    # None (the default) = the roster size the universe defines for the league's defense mode
    draft_rounds: int | None = None
    # universe_code -> "group"/"individual", e.g. {"nfl": "individual"}.
    # Any universe_code not present here defaults to "group". Rejected at
    # creation if a universe doesn't support the requested mode (see
    # Universe.supports_individual_defense).
    defense_modes: dict[str, str] = {}
    # "team_choice" (default) or "dynamic". "dynamic" is rejected at
    # creation for now - see League.program_size_mode.
    program_size_mode: str = "team_choice"
    # "recruit" (a series of recruiting periods where any team can bid on any
    # player - shown to teams as the "recruiting draft") or "snake" (default - a traditional snake draft run at a
    # scheduled time). Drives which screen the create-league wizard sends a
    # newly created team to next.
    draft_type: str = "snake"
    # When the draft starts: the snake draft's scheduled time, or when the
    # first recruiting period opens.
    draft_scheduled_at: datetime.datetime | None = None
    # Only used when draft_type="recruit": how long each recruiting period
    # lasts, in seconds (the draft runs one period per roster spot).
    recruiting_period_seconds: int = 86400
    # The pause between periods (results screen, no bidding). 20 seconds by
    # design; only settable so tests don't have to wait it out.
    recruiting_buffer_seconds: int = 20


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
    ties: int = 0
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
    position: str | None = None      # QB/RB/WR/TE/PK, or DL/LB/DB for a defense group
    real_team: str | None = None     # the school
    last_season_points: float | None = None  # last season, ESPN standard scoring (None for defense groups / no stat line)
    acquired_via: str | None = None


class RosterSlotOut(BaseModel):
    slot_code: str
    eligible_positions: list[str]
    slot_count: int
    is_starter: bool


class WeekOut(BaseModel):
    id: int
    season_year: int
    week_number: int
    start_date: datetime.date | None = None
    locked: bool = False       # the week has started, so only a commissioner-mode commissioner can change lineups
    scored: bool = False       # the week's real games are in


class StarterScoreOut(BaseModel):
    slot_code: str
    draftable_asset_id: int
    display_name: str
    position: str | None
    real_team: str | None
    played: bool
    points: float


class TeamWeekBreakdownOut(BaseModel):
    team_id: int
    team_name: str
    week_id: int
    total_points: float        # fantasy points (the fitted scale applied to raw_credit)
    raw_credit: float          # the starters' summed EPA credit
    starters: list[StarterScoreOut]


class MatchupResultOut(BaseModel):
    matchup_id: int | None      # None for a conference-championship game (derived from the standings)
    championship: bool = False
    week_id: int
    team_a_id: int
    team_a_name: str
    team_b_id: int
    team_b_name: str
    scored: bool
    team_a_score: float | None
    team_b_score: float | None
    winner_team_id: int | None


class ScheduleRequest(BaseModel):
    weeks: int = 9         # the premade regular season is weeks 1-9
    replace: bool = False


class SandboxLeagueRequest(BaseModel):
    name: str = "Commissioner Mode League"
    size: int = 12
    draft_rounds: int = 20
    recruiting_period_seconds: int = 86400


class LineupSetRequest(BaseModel):
    week_id: int
    # slot_code -> draftable_asset_id, or a list of ids for a slot that holds several
    # players, e.g. {"QB": 501, "RB": [502, 503], "BENCH": [504, 505]}
    assignments: dict[str, int | list[int]]


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
