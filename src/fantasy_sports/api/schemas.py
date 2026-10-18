"""
Pydantic request/response models. Response models set from_attributes=True
so they read directly off the SQLAlchemy ORM objects returned by db/models.
"""

from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _naive_utc(v):
    """Deadlines are stored as naive UTC: convert an aware datetime, leave a naive one as sent."""
    if v is not None and v.tzinfo is not None:
        return v.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return v


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
    # NHL only: consecutive daily games against the same opponent.
    # None lets the API apply the NHL default without changing other sports.
    series_length_days: int | None = None
    # League Settings, chosen in the wizard (the same ones the League Settings tab edits later).
    league_format: Literal["traditional_redraft", "future_pick_redraft", "keeper", "dynasty"] = "traditional_redraft"
    keeper_draft_style: Literal["rookie_resign", "combined", "separate"] = "rookie_resign"
    keeper_limit: int | None = Field(default=None, ge=0, le=100)
    keeper_deadline: datetime.datetime | None = None
    pick_trade_seasons: int = Field(default=2, ge=1, le=10)
    pick_trading_enabled: bool = True
    league_type: Literal["FREE", "PAID"] = "FREE"
    annual_dues_cents: int | None = Field(default=None, ge=1, le=10_000_000)
    trade_deadline: datetime.datetime | None = None
    # Votes a trade needs beyond half the league: 1 = half + 1 (exactly half never passes); the second is for trades that
    # include the commissioner's own team, which have no commissioner sign-off.
    trade_vote_extra_votes: int = Field(default=1, ge=1, le=20)
    commissioner_trade_vote_extra_votes: int = Field(default=2, ge=1, le=20)
    # Hockey only (rejected for other sports when changed from the defaults).
    standings_mode: Literal["points", "win_pct"] = "points"
    points_per_win: int = Field(default=3, ge=0, le=20)
    points_per_tie: int = Field(default=1, ge=0, le=20)
    playoff_teams: int | None = Field(default=None, ge=1, le=64)       # None = 33% of the teams, rounded
    tiebreakers: list[str] | None = None                                # None = the default order (platform/tiebreakers.py)
    playoff_byes: int = Field(default=0, ge=0, le=32)
    division_winners_qualify: bool = True
    division_winners_seeded_first: bool = True
    playoff_consolation: bool = False          # False = an eliminated team's season ends; True = it plays consolation matchups
    # Joining (platform/league_join.py).
    visibility: Literal["public", "private"] = "private"
    allow_ai_takeover_in_season: bool = False      # public leagues only
    unfilled_slot_policy: Literal["fill_ai", "remove"] = "fill_ai"

    _keeper_deadline = field_validator("keeper_deadline", "trade_deadline")(_naive_utc)


class LeagueSlugRequest(BaseModel):
    slug: str | None = None     # null clears the custom URL


class LeagueRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


LEAGUE_FORMATS = ("traditional_redraft", "future_pick_redraft", "keeper", "dynasty")


class LeagueSettingsRequest(BaseModel):
    """Only the fields sent are changed (keeper_limit / keeper_deadline may be sent as null to clear them)."""
    league_format: Literal["traditional_redraft", "future_pick_redraft", "keeper", "dynasty"] | None = None
    keeper_draft_style: Literal["rookie_resign", "combined", "separate"] | None = None
    keeper_limit: int | None = Field(default=None, ge=0, le=100)
    pick_trade_seasons: int | None = Field(default=None, ge=1, le=10)
    pick_trading_enabled: bool | None = None
    league_type: Literal["FREE", "PAID"] | None = None
    annual_dues_cents: int | None = Field(default=None, ge=1, le=10_000_000)    # send null to clear
    trade_deadline: datetime.datetime | None = None                          # send null to clear
    trade_vote_extra_votes: int | None = Field(default=None, ge=1, le=20)
    commissioner_trade_vote_extra_votes: int | None = Field(default=None, ge=1, le=20)
    platform_plan: Literal["AD_SUPPORTED", "AD_FREE"] | None = None          # not while a draft is in progress
    trade_review_hours: int | None = Field(default=None, ge=0, le=72)        # 0 = no review window
    trade_review_mode: Literal["none", "commissioner", "league", "both"] | None = None
    standings_mode: Literal["points", "win_pct"] | None = None
    points_per_win: int | None = Field(default=None, ge=0, le=20)
    points_per_tie: int | None = Field(default=None, ge=0, le=20)
    playoff_teams: int | None = Field(default=None, ge=1, le=64)       # send null to go back to the default
    tiebreakers: list[str] | None = None                                # send null to go back to the default order
    playoff_byes: int | None = Field(default=None, ge=0, le=32)
    division_winners_qualify: bool | None = None
    division_winners_seeded_first: bool | None = None
    playoff_consolation: bool | None = None
    visibility: Literal["public", "private"] | None = None
    allow_ai_takeover_in_season: bool | None = None
    unfilled_slot_policy: Literal["fill_ai", "remove"] | None = None
    keeper_deadline: datetime.datetime | None = None

    _deadline = field_validator("keeper_deadline", "trade_deadline")(_naive_utc)


class TeamSetupItem(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    is_ai: bool = False
    is_commissioner: bool = False
    email: str | None = None
    program_size: str = "large"
    is_open_slot: bool = False      # a human slot anyone can claim (no email); AI-run until claimed


class LeagueTeamsSetupRequest(BaseModel):
    teams: list[TeamSetupItem]


class TeamSetupResult(BaseModel):
    id: int
    name: str
    is_ai_owned: bool
    owner_user_id: int | None
    email: str | None = None
    program_size: str
    invite_url: str | None = None
    open_human_slot: bool = False


class LeagueTeamsSetupResponse(BaseModel):
    league_id: int
    teams: list[TeamSetupResult]


class TeamInviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)


class TeamInviteOut(BaseModel):
    team_id: int
    league_id: int
    email: str
    invite_url: str
    status: str = "invited"


class TeamAssignRequest(BaseModel):
    user_id: int | None = None
    email: str | None = None
    team_name: str | None = None
    is_ai: bool = False


class TeamAssignOut(BaseModel):
    team_id: int
    league_id: int
    team_name: str
    owner_user_id: int | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    is_ai_owned: bool
    invite_url: str | None = None
    status: str = "assigned"


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
    series_length_days: int | None
    league_format: str
    season_number: int
    offseason: bool
    offseason_draft_rounds: int | None
    offseason_refill_rounds: int | None
    keeper_draft_style: str
    keeper_limit: int | None
    keeper_deadline: datetime.datetime | None
    keepers_finalized: bool
    pick_trade_seasons: int
    pick_trading_enabled: bool
    league_type: str
    annual_dues_cents: int | None
    trade_deadline: datetime.datetime | None
    trade_vote_extra_votes: int
    commissioner_trade_vote_extra_votes: int
    platform_plan: str
    platform_fee_cents: int | None
    trade_review_hours: int
    trade_review_mode: str
    standings_mode: str
    points_per_win: int
    points_per_tie: int
    playoff_teams: int | None
    tiebreakers: list[str] | None
    playoff_byes: int
    division_winners_qualify: bool
    division_winners_seeded_first: bool
    playoff_consolation: bool
    visibility: str
    allow_ai_takeover_in_season: bool
    unfilled_slot_policy: str
    slug: str | None = None
    logo_url: str | None = None


class SettingVoteOut(BaseModel):
    team_id: int
    team_name: str
    vote: str | None            # None = hasn't voted


class SettingChangeOut(BaseModel):
    id: int
    league_id: int
    field: str
    old_value: str
    new_value: str
    status: str
    created_at: datetime.datetime
    resolved_at: datetime.datetime | None
    votes: list[SettingVoteOut]         # one row per person-managed team
    my_vote: str | None = None
    can_vote: bool = False


class SettingVoteRequest(BaseModel):
    vote: Literal["approve", "reject"]


class RolloverRequest(BaseModel):
    offseason_draft_rounds: int | None = Field(default=None, ge=1, le=60)   # rookie rounds: Keeper / Dynasty only
    refill_rounds: int | None = Field(default=None, ge=1, le=60)            # Keeper with a combined or separate draft style


class KeeperPlayerOut(BaseModel):
    draftable_asset_id: int
    display_name: str
    position: str | None = None
    games_played: int
    kept: bool


class KeepersOut(BaseModel):
    team_id: int
    season_number: int
    finalized: bool
    deadline: datetime.datetime | None
    cap: int
    submitted: bool
    players: list[KeeperPlayerOut]


class FuturePickOut(BaseModel):
    round: int
    pick_in_round: int | None
    overall: int | None
    original_team_id: int | None = None
    original_team_name: str | None = None
    own: bool = True


class FuturePicksOut(BaseModel):
    season_number: int
    order_set: bool
    picks: list[FuturePickOut]


class KeepersRequest(BaseModel):
    draftable_asset_ids: list[int]


class LeagueSeasonSummary(BaseModel):
    season_number: int
    league_format: str
    archived_at: datetime.datetime
    champion_team_id: int | None = None
    standings: list[dict]


class LeagueSeasonOut(LeagueSeasonSummary):
    data: dict


class DivisionOut(BaseModel):
    id: int
    conference_id: int
    name: str
    team_count: int = 0


class ConferenceOut(BaseModel):
    id: int
    program_size: str
    name: str
    team_count: int
    editable: bool = True
    divisions: list[DivisionOut] = []


class ConferenceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ConferenceUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class DivisionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class DivisionUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class DivisionSetupInput(BaseModel):
    name: str | None = None


class ConferenceSetupInput(BaseModel):
    name: str | None = None
    divisions: list[DivisionSetupInput] = []


class SetupStructureRequest(BaseModel):
    conference_count: int = Field(ge=1, le=16)
    divisions_per_conference: int = Field(ge=0, le=16)
    conferences: list[ConferenceSetupInput] | None = None
    randomize_teams: bool = True


class TeamAlignmentItem(BaseModel):
    team_id: int
    conference_id: int | None = None
    division_id: int | None = None


class LeagueAlignmentRequest(BaseModel):
    assignments: list[TeamAlignmentItem]


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
    division_id: int | None = None
    conference_name: str | None = None
    division_name: str | None = None
    season_goal: str
    owner_name: str | None = None
    owner_email: str | None = None
    is_claimed: bool = False
    invite_status: str = "ai"
    invite_url: str | None = None
    open_human_slot: bool = False
    ads_enabled: bool = True        # False only for an AD_FREE league's team whose current-season platform fee is secured


class RosterEntryOut(BaseModel):
    draftable_asset_id: int
    asset_type: str
    display_name: str  # player full_name, or "<Real Team> <Group>" for defense groups
    position: str | None = None      # QB/RB/WR/TE/PK, or DL/LB/DB for a defense group
    real_team: str | None = None     # the school
    age: int | None = None           # whole years; None when unknown (and for defense groups)
    last_season_points: float | None = None  # last season, ESPN standard scoring (None for defense groups / no stat line)
    acquired_via: str | None = None
    games: int | None = None                  # games with stored credit (None = no stored games)
    points_per_game: float | None = None
    this_season_points: float | None = None   # this season's stored points (None = none scored yet)
    this_season_games: int | None = None


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
    # Optional pick clock (commissioner only). Omit everything for the original untimed draft.
    pick_seconds: int | None = None
    overnight_start: str | None = None   # "HH:MM" local, with utc_offset_minutes
    overnight_end: str | None = None
    utc_offset_minutes: int = 0
    randomize_order: bool = False
    team_order: list[int] | None = None


class DraftOut(ORMModel):
    id: int
    league_id: int
    status: str
    started_at: datetime.datetime | None
    completed_at: datetime.datetime | None
    pick_seconds: int | None = None
    paused: bool = False
    deadline_at: datetime.datetime | None = None


class DraftOvernightOut(BaseModel):
    start: str
    end: str
    utc_offset_minutes: int


class DraftClockOut(BaseModel):
    draft_id: int
    status: str               # draft status: pending / in_progress / complete
    clock_status: str         # untimed / running / paused / overnight / complete
    mode: str                 # untimed / live / async
    pick_seconds: int | None
    paused: bool
    server_time: datetime.datetime
    deadline_at: datetime.datetime | None
    remaining_seconds: int | None   # active seconds left; frozen while paused or overnight
    overnight: DraftOvernightOut | None
    overnight_active: bool
    resumes_at: datetime.datetime | None   # when an active overnight pause ends
    pick_number: int | None
    on_clock_team_id: int | None
    # Set when an AI-owned team is on the clock: seconds until it makes its pick (the page processes the draft then).
    ai_in_seconds: float | None = None
    online_team_ids: list[int] = []
    online_user_ids: list[int] = []


class DraftPresenceRequest(BaseModel):
    team_id: int | None = None


class DraftPickOut(BaseModel):
    id: int
    round: int
    pick_number: int
    team_id: int
    draftable_asset_id: int | None
    display_name: str | None
    picked_at: datetime.datetime | None
    is_autopick: bool
    position: str | None = None  # the picked asset's position, for the draft-board grid colours
    team: str | None = None      # the picked player's real team, when known


class MakePickRequest(BaseModel):
    draftable_asset_id: int


class DraftProcessOut(BaseModel):
    processed: int
    picks: list[DraftPickOut]
    clock: DraftClockOut


class DraftBoardAsset(BaseModel):
    draftable_asset_id: int
    asset_type: str
    display_name: str
    position: str | None = None  # player position, or "DL"/"LB"/"DB" for defense groups
    # Stored stats for the sortable table; None = not available (never estimated). See platform/draft_stats.py.
    team: str | None = None
    points: float | None = None       # last season's fantasy points
    projected: float | None = None    # no projection source yet: always None
    goals: int | None = None
    assists: int | None = None
    shots: int | None = None
    blocks: int | None = None


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


class SeriesScheduleCreateRequest(BaseModel):
    start_date: datetime.date
    cycles: int = 1
    replace: bool = False
    force: bool = False
    window_rows: bool = True      # False keeps the legacy shape: one game per calendar day instead of one per window


class SeriesDailyGameOut(ORMModel):
    id: int
    series_id: int
    calendar_date: datetime.date
    calendar_date_end: datetime.date | None = None
    window_index: int | None = None
    is_matchup_day: bool
    home_team_id: int
    away_team_id: int
    home_score: int = 0
    away_score: int = 0
    home_shots: int = 0
    away_shots: int = 0
    current_period: int = 0
    seconds_remaining: int = 1200
    game_score_rating: int = 50
    status: str
    owner_watched: bool = False
    highlights_ready: bool = False   # the viewer has a simulated period of this game they have not watched yet
    first_start_at: str | None = None   # UTC ISO instant the first segment of the first period begins (wall-clock games)



class LeagueSeriesOut(ORMModel):
    id: int
    league_id: int
    team_a_id: int
    team_b_id: int
    start_date: datetime.date
    end_date: datetime.date
    total_days: int
    status: str
    daily_games: list[SeriesDailyGameOut]


class SeriesScheduleOut(BaseModel):
    league_id: int
    series_length_days: int
    series: list[LeagueSeriesOut]
    real_slate_summary: str | None = None
    real_games_total: int = 0
    real_games_final: int = 0
    real_games_live: int = 0


class TeamSeriesGameOut(BaseModel):
    daily_game_id: int
    calendar_date: datetime.date
    calendar_date_end: datetime.date | None = None
    window_index: int | None = None
    is_matchup_day: bool
    home_away: str
    status: str
    team_score: int | None = None       # this team's goals, only for a FINAL game the viewer may see
    opponent_score: int | None = None
    score_hidden: bool = False          # FINAL, but the owner hasn't watched the highlights yet (no spoilers)


class TeamSeriesOut(BaseModel):
    series_id: int
    opponent_id: int
    opponent_name: str
    start_date: datetime.date
    end_date: datetime.date
    total_days: int
    status: str
    games: list[TeamSeriesGameOut]


class TeamSeriesScheduleOut(BaseModel):
    team_id: int
    league_id: int
    series: list[TeamSeriesOut]


class PeckingOrderSetRequest(BaseModel):
    """group ("C"/"W"/"D"/"G") -> Player ids, highest priority first. Groups left out are untouched."""
    assignments: dict[str, list[int]]


class PeckingOrderPlayerOut(BaseModel):
    player_id: int
    draftable_asset_id: int
    display_name: str
    position: str | None = None
    priority: int | None = None  # None = on the roster but unranked
    games: int | None = None
    points_per_game: float | None = None
    real_team: str | None = None
    real_team_code: str | None = None
    injury_status: str | None = None   # DTD / OUT / IR / LTIR; None = healthy
    injury_note: str | None = None


class PeckingOrderGroupOut(BaseModel):
    position_group: str
    max_depth: int
    starters: int
    ranked: list[PeckingOrderPlayerOut]
    unranked: list[PeckingOrderPlayerOut]


class PeckingOrderOut(BaseModel):
    team_id: int
    groups: list[PeckingOrderGroupOut]
    real_schedules: dict[str, dict[int, list[dict]]] = {}
    injured_list: list[PeckingOrderPlayerOut] = []
    injured_list_capacity: int = 0


class InjuryAlertOut(BaseModel):
    id: int
    draftable_asset_id: int
    player_name: str
    old_status: str | None = None
    new_status: str | None = None   # None = healthy again
    note: str | None = None
    created_at: datetime.datetime


class InjuredListRequest(BaseModel):
    draftable_asset_id: int
    on: bool   # True = place on the injured list, False = activate


class PeckingOrderResolveRequest(BaseModel):
    scheduled_player_ids: list[int]
    confirmed_goalie_ids: list[int] = []


class PeckingOrderVacancyOut(BaseModel):
    position_group: str
    slot: int
    reason: str


class PeckingOrderResolvedOut(BaseModel):
    team_id: int
    starters: dict[str, list[PeckingOrderPlayerOut]]
    vacancies: list[PeckingOrderVacancyOut]


# --- NHL series standings, rivalries, Game Score ----------------------------

class SeriesStandingOut(BaseModel):
    rank: int
    team_id: int
    team_name: str
    wins: int
    losses: int
    ties: int
    goals_for: int
    goals_against: int
    goal_diff: int
    h2h_wins: int
    home_wins: int = 0
    home_losses: int = 0
    home_ties: int = 0
    away_wins: int = 0
    away_losses: int = 0
    away_ties: int = 0
    games_played: int = 0
    points: int = 0
    win_pct: float = 0.0
    seed: int | None = None             # playoff seed, None if the team is outside the playoff field
    division_winner: bool = False
    bye: bool = False                   # holds a first-round bye in the current standings
    eliminated: bool = False            # mathematically out of the playoffs (platform/elimination.py)


class SeriesStandingsOut(BaseModel):
    league_id: int
    as_of: datetime.date | None
    tiebreak_order: str
    standings_mode: str = "points"
    points_per_win: int = 3
    points_per_tie: int = 1
    playoff_teams: int = 0              # the field size in use (the league's setting, or the default)
    tiebreakers: list[str] = []         # the tiebreaker order in use
    playoff_byes: int = 0
    division_winners_qualify: bool = True
    division_winners_seeded_first: bool = True
    playoff_consolation: bool = False
    standings: list[SeriesStandingOut]


class PlayoffOddsTeamOut(BaseModel):
    team_id: int
    team_name: str
    games_played: int
    goals_for: float
    goals_against: float
    pythagorean: float                  # GF^x / (GF^x + GA^x) from this season's goals
    strength: float                     # pythagorean after padding with league-average games; what the simulation uses
    playoff_probability: float          # share of simulated seasons in the playoff field (an estimate)
    bye_probability: float
    mean_points: float                  # mean of the standings' primary number (points or win %) at the end of the season
    eliminated: bool = False            # mathematically out: the exact proof, which overrides the estimate


class PlayoffOddsOut(BaseModel):
    league_id: int
    simulations: int
    pythagorean_exponent: float
    tie_rate: float
    teams: list[PlayoffOddsTeamOut]


class RivalryOut(BaseModel):
    league_id: int
    team_low_id: int
    team_high_id: int
    series_low_wins: int
    series_high_wins: int
    series_ties: int
    daily_low_wins: int
    daily_high_wins: int
    daily_ties: int
    goals_low: int
    goals_high: int
    net_goal_diff_low: int
    intensity: float


class RivalriesOut(BaseModel):
    league_id: int
    as_of: datetime.date | None
    rivalries: list[RivalryOut]


class GameScoreOut(BaseModel):
    daily_game_id: int
    series_id: int
    calendar_date: datetime.date
    home_team_id: int
    away_team_id: int
    status: str
    score: int
    margin: float
    cutoff: float
    rivalry: float
    urgency: float
    standings_as_of: datetime.date


class GameScoresOut(BaseModel):
    league_id: int
    weights: dict[str, float]
    games: list[GameScoreOut]


# --- Progressive simulator (FS-025) -----------------------------------------

class SimTeamOut(BaseModel):
    id: int
    name: str


class SimStateOut(BaseModel):
    """The viewer's scoreboard. Everything but the lifecycle and identity fields is null while the viewer is behind
    the veil. Team names and the date are not spoilers."""
    contract_version: str
    daily_game_id: int
    league_id: int | None = None
    series_id: int | None = None
    calendar_date: datetime.date | None = None
    home_team: SimTeamOut | None = None
    away_team: SimTeamOut | None = None
    published: bool
    status: str  # SCHEDULED / LIVE / FINAL
    veiled: bool
    reveal_mode: str | None
    periods: int | None
    period_seconds: int | None
    window_seconds: int | None
    period: int | None
    seconds_remaining: int | None
    home_score: int | None
    away_score: int | None
    home_shots: int | None
    away_shots: int | None
    shown_seq: int
    cursor_seq: int
    has_more: bool
    is_complete: bool
    can_skip: bool
    provenance: dict | None
    progressive: bool = False
    published_periods: int | None = None
    period_phases: list[dict] | None = None


class SimEventOut(ORMModel):
    seq: int
    kind: str
    period: int
    game_second: int
    team_side: str | None
    home_score: int
    away_score: int
    home_shots: int
    away_shots: int
    headline: str
    detail: dict


class SimEventsOut(BaseModel):
    contract_version: str
    daily_game_id: int
    status: str
    after_seq: int
    next_seq: int  # pass as after_seq to continue; equals after_seq when nothing new
    available_through: int
    has_more: bool
    events: list[SimEventOut]


class SimProgressRequest(BaseModel):
    cursor_seq: int


class SimFinalOut(BaseModel):
    contract_version: str
    daily_game_id: int
    status: str
    home_score: int
    away_score: int
    home_shots: int
    away_shots: int
    periods: int
    event_count: int
    scoring_events: list[SimEventOut]
    provenance: dict


# --- NHL off-day banking (FS-028) -------------------------------------------

class BankedCreditOut(BaseModel):
    id: int
    league_id: int
    team_id: int
    draftable_asset_id: int
    player_name: str
    source_calendar_date: datetime.date
    credit_points: float
    applied_points: float | None
    capped: bool
    status: str  # AVAILABLE / ASSIGNED / EXPIRED
    status_reason: str | None
    assignment_mode: str | None  # MANUAL / AUTO
    assigned_daily_game_id: int | None
    assigned_at: datetime.datetime | None
    created_at: datetime.datetime
    fallback_at: datetime.datetime
    window_end: datetime.date


class BankedCreditsOut(BaseModel):
    team_id: int
    cap: float
    fallback_hours: int
    credits: list[BankedCreditOut]


class BankGameOptionOut(BaseModel):
    daily_game_id: int
    calendar_date: datetime.date
    opponent_id: int
    opponent_name: str
    home_away: str
    game_score: int
    opponent_strength: dict | None
    projected_margin: float | None
    win_probability_swing: float | None
    rank: int


class BankRecommendationOut(BaseModel):
    credit: BankedCreditOut
    recommended_game_id: int | None
    confidence: str  # FULL / PARTIAL / NEUTRAL / NONE
    basis: str
    reasons: list[str]
    inputs_available: dict
    options: list[BankGameOptionOut]


class BankAssignRequest(BaseModel):
    daily_game_id: int


class BankFallbackOut(BaseModel):
    processed_at: datetime.datetime
    assigned: list[dict]
    expired: list[int]
    waiting: int


# --- Draft broadcast room (FS-031) --------------------------------------------

class AnnouncementTeamOut(BaseModel):
    id: int
    name: str


class AnnouncementPlayerOut(BaseModel):
    asset_id: int
    name: str
    position: str | None


class AnnouncementCommentaryOut(BaseModel):
    provider: str
    text: str
    tags: list[str]
    value_label: str            # value / fair / reach / unknown
    unavailable: list[str]      # inputs that were missing, so the text does not claim them
    inputs: dict


class PickAnnouncementOut(BaseModel):
    """Versioned pick-announcement event. event_id is stable per pick, so a client can drop duplicates."""
    version: str
    event_id: str
    draft_id: int
    pick_number: int
    round: int
    team: AnnouncementTeamOut
    player: AnnouncementPlayerOut
    is_autopick: bool
    picked_at: datetime.datetime | None
    commentary: AnnouncementCommentaryOut
    spoken_text: str


class AnnouncementsOut(BaseModel):
    version: str
    draft_id: int
    provider: str
    after_pick: int
    next_pick: int              # pass as after_pick to continue; equals after_pick when nothing is new
    picks_made: int
    announcements: list[PickAnnouncementOut]


# --- Weekly salary-cap redraft (FS-033) ---------------------------------------

class RedraftEnableRequest(BaseModel):
    base_cap: int | None = None       # salary units; omitted = the neutral fixed cap


class RedraftContestCreateRequest(BaseModel):
    start_date: datetime.date
    end_date: datetime.date
    lock_at: datetime.datetime        # naive UTC


class RedraftSalariesRequest(BaseModel):
    salaries: dict[int, int]          # draftable asset id -> salary


class RedraftRosterRequest(BaseModel):
    slots: dict[str, list[int]]       # C / W / D / G -> draftable asset ids


class RedraftSlotOut(BaseModel):
    slot_code: str
    slot_index: int
    draftable_asset_id: int
    player_name: str
    position: str
    salary: int


class RedraftEntryOut(BaseModel):
    team_id: int
    status: str                       # DRAFT / SUBMITTED
    salary_total: int
    cap: int
    cap_remaining: int
    submitted_at: datetime.datetime | None
    slots: list[RedraftSlotOut]


class RedraftPairingOut(BaseModel):
    team_a_id: int
    team_b_id: int
    team_a_status: str                # NO_ENTRY / DRAFT / SUBMITTED
    team_b_status: str


class RedraftContestOut(BaseModel):
    id: int
    league_id: int
    week_index: int
    start_date: datetime.date
    end_date: datetime.date
    lock_at: datetime.datetime
    status: str                       # OPEN / LOCKED
    cap: int
    cap_policy: str
    salaries_published: int
    pairings: list[RedraftPairingOut]


class RedraftLeagueOut(BaseModel):
    league_id: int
    league_mode: str
    base_cap: int | None
    cap_policy: str | None
    contests: list[RedraftContestOut]
    note: str


class RedraftContestViewOut(BaseModel):
    contest: RedraftContestOut
    opponent_team_id: int | None
    my_entry: RedraftEntryOut | None
    opponent_entry: RedraftEntryOut | None     # null until the contest locks (rosters are private before then)


class JoinableTeamOut(BaseModel):
    id: int
    name: str
    kind: Literal["open_slot", "ai_takeover"]
    program_size: str


class JoinOptionsOut(BaseModel):
    league_id: int
    can_join: bool
    reason: str | None = None            # why not, when can_join is False
    teams: list[JoinableTeamOut]
    slot_cutoff: datetime.datetime | None = None


class PublicLeagueOut(BaseModel):
    id: int
    name: str
    universe_codes: list[str]
    size: int
    draft_type: str
    draft_scheduled_at: datetime.datetime | None
    league_type: str
    annual_dues_cents: int | None
    human_teams: int
    open_slots: int
    ai_takeovers: int
    allow_ai_takeover_in_season: bool
    draft_state: str
    slug: str | None = None
    logo_url: str | None = None


class JoinLeagueRequest(BaseModel):
    team_id: int
    team_name: str | None = Field(default=None, max_length=80)
