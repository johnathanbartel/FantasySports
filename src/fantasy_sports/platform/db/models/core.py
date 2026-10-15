"""
Sport-agnostic core schema: users, leagues, teams, rosters, draft, and the
real-world calendar/matchup mechanics that every sport on the platform
shares. Football-specific tables (raw play-by-play, credit attribution
output) live in db/models/football.py - see that module's docstring for
why the split.

"universe" = a specific league context (college football, NFL, ...), not
just "sport" - football and baseball are sports, but college football and
NFL are two different universes of the same sport. A league can belong to
more than one universe (Dual Universe mode) via league_universes; the
crossover mechanics themselves aren't built yet, this just avoids the
schema blocking it later.
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.team_tiers import SEASON_GOAL_BY_PROGRAM_SIZE


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    # Auth mechanism TBD - this is a hook, not an implementation.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())


class UserSession(Base):
    """Dev-grade login session: an opaque bearer token that identifies a
    user. There is no password check yet (users.password_hash is only a hook),
    so anyone who can POST /sessions can become any user - this closes the
    "team_id in the request body" trust gap (acting as a team you don't own),
    not real authentication. Swap the login step for real credentials later
    without touching the ownership checks in api/auth.py."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())


class Universe(Base):
    __tablename__ = "universes"

    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[str] = mapped_column(String(30))  # "football", "baseball", ...
    level: Mapped[str] = mapped_column(String(30))  # "college", "pro"
    code: Mapped[str] = mapped_column(String(20), unique=True)  # "ncaaf", "nfl"
    display_name: Mapped[str] = mapped_column(String(60))
    # Whether this universe's data source identifies individual defenders on
    # a play (nflfastR: sack_player_name, interception_player_name, tackle
    # columns, ...). CFBD has none of that for NCAAF, so "individual" is a
    # league_universes.defense_mode option only where this is True.
    supports_individual_defense: Mapped[bool] = mapped_column(Boolean, default=False)


class League(Base):
    __tablename__ = "leagues"

    __table_args__ = (
        CheckConstraint(
            "program_size_mode IN ('team_choice', 'dynamic')", name="ck_league_program_size_mode"
        ),
        CheckConstraint("draft_type IN ('recruit', 'snake')", name="ck_league_draft_type"),
        CheckConstraint(
            "series_length_days IS NULL OR series_length_days IN (5, 7, 10)",
            name="ck_league_series_length_days",
        ),
        CheckConstraint("league_mode IN ('standard', 'weekly_redraft')", name="ck_league_mode"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    commissioner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    size: Mapped[int] = mapped_column(Integer, default=12)
    draft_rounds: Mapped[int] = mapped_column(Integer, default=20)
    # Placeholder only - no salary-cap logic is implemented anywhere yet.
    salary_cap_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # "team_choice": teams pick their own program_size (see Team below).
    # "dynamic": every team starts "small" and is promoted/relegated by
    # season performance - stubbed only. The column/CHECK accepts it so a
    # future migration isn't needed to add the value, but the API layer
    # (create_league) currently rejects actually setting it, since dynamic
    # mode needs a multi-season/season-rollover concept this schema doesn't
    # have yet.
    program_size_mode: Mapped[str] = mapped_column(String(20), default="team_choice")
    # "recruit": a timed sealed-bid auction (AuctionDraft) - user-facing name
    # is "recruiting period", started on demand once the commissioner is
    # ready. "snake": a traditional position-by-position snake draft
    # (Draft/DraftPick), run at a scheduled time instead. Set once at league
    # creation (the create-league wizard) and used there to decide which
    # screen a newly created team lands on next.
    draft_type: Mapped[str] = mapped_column(String(20), default="snake")
    # Only meaningful for draft_type="snake" - lets the roster page tell a
    # team when to expect the live draft. Nullable because a commissioner
    # may not have picked a time yet, and because "recruit" leagues start
    # their recruiting period on demand instead of at a fixed time.
    draft_scheduled_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    # NHL leagues play repeated daily games against the same opponent. Null
    # keeps football and other universes on their existing weekly format.
    series_length_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # "standard" = the league's normal format; "weekly_redraft" = weekly salary-cap head-to-head rosters (FS-033).
    league_mode: Mapped[str] = mapped_column(String(20), default="standard", server_default="standard")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    universes: Mapped[list["LeagueUniverse"]] = relationship(back_populates="league")
    teams: Mapped[list["Team"]] = relationship(back_populates="league")
    series: Mapped[list["LeagueSeries"]] = relationship(back_populates="league")


class LeagueUniverse(Base):
    """A normal league has one row here; a Dual Universe league has two."""

    __tablename__ = "league_universes"
    __table_args__ = (
        UniqueConstraint("league_id", "universe_id"),
        CheckConstraint(
            "defense_mode IN ('group', 'individual', 'team')", name="ck_league_universe_defense_mode"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"))
    # A league's choice, per universe, of how defense is drafted/scored:
    # "group" (DL/LB/DB units), "team" (one whole-defense asset per team) or
    # "individual" (named defenders). "team" and "group" are always legal;
    # "individual" is only a legal choice when universe.supports_individual_defense is
    # True - enforced at the API layer (create_league), not by a DB
    # constraint, since that check spans two tables.
    defense_mode: Mapped[str] = mapped_column(String(20), default="group")

    league: Mapped["League"] = relationship(back_populates="universes")
    universe: Mapped["Universe"] = relationship()


class Conference(Base):
    """Groups a league's teams by program_size tier. Every league starts
    with one conference per tier (a baseline of len(PROGRAM_SIZES), i.e. 3
    today); growing conference count is only ever done one full tier-set at
    a time (platform/conferences/service.py::expand_conferences), so a
    league's total conference count is always a multiple of the number of
    tiers in use.

    Per-league tier subsetting (a league that only uses 2 of the 3 tiers,
    so conferences would only ever grow 2 at a time) isn't implemented yet
    - every league currently gets all 3 tiers."""

    __tablename__ = "conferences"
    __table_args__ = (
        CheckConstraint("program_size IN ('large', 'moderate', 'small')", name="ck_conference_program_size"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    program_size: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(80))

    league: Mapped["League"] = relationship()
    divisions: Mapped[list["Division"]] = relationship(
        back_populates="conference", cascade="all, delete-orphan", order_by="Division.id"
    )


class Division(Base):
    """A division within a conference (e.g. Atlantic Division within Eastern Conference)."""

    __tablename__ = "divisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    conference_id: Mapped[int] = mapped_column(ForeignKey("conferences.id"))
    name: Mapped[str] = mapped_column(String(80))

    league: Mapped["League"] = relationship()
    conference: Mapped["Conference"] = relationship(back_populates="divisions")
    teams: Mapped[list["Team"]] = relationship(back_populates="division")


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (
        CheckConstraint(
            "(owner_user_id IS NOT NULL AND is_ai_owned IS FALSE) OR "
            "(owner_user_id IS NULL AND is_ai_owned IS TRUE)",
            name="ck_team_owner_xor_ai",
        ),
        CheckConstraint(
            "program_size IN ('large', 'moderate', 'small')", name="ck_team_program_size"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    name: Mapped[str] = mapped_column(String(80))
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_ai_owned: Mapped[bool] = mapped_column(Boolean, default=False)
    # Sets this team's starting auction-draft budget (see
    # auction.constants.STARTING_BUDGET_BY_PROGRAM_SIZE), its conference
    # (see conference_id below), and its season_goal. Default "small"
    # matches the eventual dynamic-mode rule (stubbed only - see
    # League.program_size_mode) that a program starts small and has to
    # build up over multiple seasons.
    program_size: Mapped[str] = mapped_column(String(20), default="small")
    # Assigned at team creation (api/routers/teams.py::create_team) by
    # platform/conferences/service.py::assign_team_to_conference, to the
    # least-populated conference matching program_size. Nullable because
    # teams inserted directly (as every team is today - there's no
    # create-team endpoint's callers yet outside scripts/tests) predate
    # this column and never get one.
    conference_id: Mapped[int | None] = mapped_column(ForeignKey("conferences.id"), nullable=True)
    division_id: Mapped[int | None] = mapped_column(ForeignKey("divisions.id"), nullable=True)
    # Trait system design TBD - just a JSON hook for now, e.g.
    # {"aggression": 0.8, "reactive_to_trends": true}.
    personality_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    league: Mapped["League"] = relationship(back_populates="teams")
    conference: Mapped["Conference | None"] = relationship()
    division: Mapped["Division | None"] = relationship(back_populates="teams")

    @property
    def season_goal(self) -> str:
        """Fixed per-tier goal label (see platform/team_tiers.py) - purely
        informational for now, not fed into scoring. The commissioner's
        stated intent is that reaching/missing it could move next season's
        budget or tier; that needs season-rollover, which doesn't exist
        yet."""
        return SEASON_GOAL_BY_PROGRAM_SIZE[self.program_size]


class RealTeam(Base):
    """A real-world team (Georgia, Kansas City Chiefs, ...), not a fantasy team."""

    __tablename__ = "real_teams"
    __table_args__ = (UniqueConstraint("universe_id", "external_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"))
    name: Mapped[str] = mapped_column(String(80))
    conference: Mapped[str | None] = mapped_column(String(60), nullable=True)
    classification: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "fbs"/"fcs"
    external_ref: Mapped[str | None] = mapped_column(String(60), nullable=True)


class Player(Base):
    __tablename__ = "players"
    __table_args__ = (UniqueConstraint("universe_id", "external_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"))
    real_team_id: Mapped[int | None] = mapped_column(ForeignKey("real_teams.id"), nullable=True)
    full_name: Mapped[str] = mapped_column(String(100))
    position: Mapped[str] = mapped_column(String(10))  # QB/RB/WR/TE/K/...
    # Source id (CFBD player id / nflfastR gsis_id) for future ingestion joins.
    external_ref: Mapped[str | None] = mapped_column(String(60), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Date of birth (hockey: from the NHL roster sync); NULL = unknown. Age is derived, see platform/player_age.py.
    birth_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    # Real-world injury report (hockey): NULL = healthy; else DTD / OUT / IR / LTIR (platform/injuries.py).
    injury_status: Mapped[str | None] = mapped_column(String(10), nullable=True)
    injury_note: Mapped[str | None] = mapped_column(String(200), nullable=True)   # e.g. "Lower body"
    injury_updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)


class DefenseGroup(Base):
    """A draftable team-defense asset - e.g. "Georgia DL" (a position group) or,
    where a universe offers whole-team defense, "Georgia DEF" (position_group
    "DEF": the entire defense). Scores what that team's unit does regardless of
    which players are on it. Individual defenders are `players` instead."""

    __tablename__ = "defense_groups"
    __table_args__ = (
        UniqueConstraint("real_team_id", "position_group"),
        CheckConstraint("position_group IN ('DL','LB','DB','DEF')", name="ck_defense_group_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    real_team_id: Mapped[int] = mapped_column(ForeignKey("real_teams.id"))
    position_group: Mapped[str] = mapped_column(String(3))  # "DL"/"LB"/"DB", or "DEF" for a whole defense

    real_team: Mapped["RealTeam"] = relationship()


class DraftableAsset(Base):
    """Single join target for anything that can be drafted/rostered/started -
    a player or a defense group. Keeps draft_picks/roster_entries/
    lineup_entries from each needing two nullable FKs, and lets the draft
    pool freely mix players and defense groups (see the DL/LB/DB same-pool
    recommendation in docs/data_contract.md)."""

    __tablename__ = "draftable_assets"
    __table_args__ = (
        CheckConstraint(
            "(asset_type = 'player' AND player_id IS NOT NULL AND defense_group_id IS NULL) OR "
            "(asset_type = 'defense_group' AND defense_group_id IS NOT NULL AND player_id IS NULL)",
            name="ck_draftable_asset_type_xor",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"))
    asset_type: Mapped[str] = mapped_column(String(20))  # "player"/"defense_group"
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    defense_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("defense_groups.id"), nullable=True
    )

    universe: Mapped["Universe"] = relationship()
    player: Mapped["Player | None"] = relationship()
    defense_group: Mapped["DefenseGroup | None"] = relationship()


class RosterSlotDefinition(Base):
    """Defines roster *structure* per universe/sport as data, not hardcoded
    columns - a different sport gets its own rows here without touching
    this table's shape."""

    __tablename__ = "roster_slot_definitions"
    __table_args__ = (
        CheckConstraint(
            "defense_mode IS NULL OR defense_mode IN ('group', 'individual', 'team')",
            name="ck_roster_slot_definition_defense_mode",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"))
    slot_code: Mapped[str] = mapped_column(String(20))  # "QB","RB","FLEX","DL","BENCH",...
    eligible_positions: Mapped[list] = mapped_column(JSON)  # e.g. ["RB","WR","TE"] for FLEX
    slot_count: Mapped[int] = mapped_column(Integer)
    is_starter: Mapped[bool] = mapped_column(Boolean, default=True)
    # NULL applies to every league on this universe regardless of
    # defense_mode (all offensive slots, BENCH). Only set on slot rows that
    # are specific to one defense_mode - e.g. a universe offering both
    # modes has one set of DL/LB/DB slots tagged "group" and a different
    # set of individual-position slots tagged "individual"; a league only
    # sees the slots matching its own league_universes.defense_mode plus
    # every NULL row.
    defense_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/in_progress/complete
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    # Pick clock (platform/draft_clock.py). pick_seconds NULL = untimed (the original behaviour). All instants are
    # naive UTC. deadline_at is the authoritative end of the current pick; it is NULL while paused or untimed.
    pick_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overnight_start_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)  # local minute of day
    overnight_end_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    utc_offset_minutes: Mapped[int] = mapped_column(Integer, default=0)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    paused_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    remaining_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)  # active seconds left, while paused
    pick_started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    deadline_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)


class DraftPick(Base):
    """Pick slots (round, pick_number, team_id) are pre-generated in snake
    order when the draft is created; draftable_asset_id is filled in as
    picks are made."""

    __tablename__ = "draft_picks"
    __table_args__ = (UniqueConstraint("draft_id", "pick_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id"))
    round: Mapped[int] = mapped_column(Integer)
    pick_number: Mapped[int] = mapped_column(Integer)  # overall pick, 1-indexed
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    draftable_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("draftable_assets.id"), nullable=True
    )
    picked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    is_autopick: Mapped[bool] = mapped_column(Boolean, default=False)

    draftable_asset: Mapped["DraftableAsset | None"] = relationship()


class AuctionDraft(Base):
    """A timed, sealed-bid auction draft. Teams take turns nominating a
    draftable asset; everyone gets one hidden bid within a fixed time
    window (round_seconds); the highest bid wins and is debited from that
    team's budget - see platform/auction/engine.py for the round-resolution
    rules this table supports.

    Separate from Draft/DraftPick (snake-shaped: fixed team-per-slot picks
    pre-generated up front) because an auction has no fixed pick order and
    price is a first-class concept neither of those tables has room for.
    """

    __tablename__ = "auction_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/in_progress/complete
    round_seconds: Mapped[int] = mapped_column(Integer, default=30)
    # Nomination turn order, as a list of team_id - e.g. [3, 1, 2]. Rotates
    # via next_nominator_index regardless of who wins each round's bid.
    nomination_order: Mapped[list] = mapped_column(JSON)
    next_nominator_index: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)


class AuctionTeamBudget(Base):
    """One row per team per auction draft - the budget the engine debits
    against. starting_budget is snapshotted from Team.program_size at
    auction start, so a program_size change mid-auction can't rewrite a
    budget that's already being spent."""

    __tablename__ = "auction_team_budgets"
    __table_args__ = (UniqueConstraint("auction_draft_id", "team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    auction_draft_id: Mapped[int] = mapped_column(ForeignKey("auction_drafts.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    starting_budget: Mapped[int] = mapped_column(Integer)
    remaining_budget: Mapped[int] = mapped_column(Integer)
    assets_won: Mapped[int] = mapped_column(Integer, default=0)


class AuctionRound(Base):
    """One nominated asset up for bid. opens_at/closes_at is the round's
    timed sealed-bid window (opens_at + round_seconds); the engine rejects
    bids placed after closes_at even if the round hasn't been resolved yet.

    status: "open" (bidding window running) -> "resolved" (had at least one
    bid, winner recorded) or "unsold" (window closed with no bids at all -
    the asset returns to the board for a future nomination).
    """

    __tablename__ = "auction_rounds"
    __table_args__ = (UniqueConstraint("auction_draft_id", "round_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    auction_draft_id: Mapped[int] = mapped_column(ForeignKey("auction_drafts.id"))
    round_number: Mapped[int] = mapped_column(Integer)  # sequential across the whole auction
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    nominated_by_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    opens_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    closes_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="open")
    winning_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    winning_bid_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # True when the winner was chosen by random tie-break among two or more
    # equal top bids, rather than being the only top bidder.
    was_tie_broken_randomly: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    draftable_asset: Mapped["DraftableAsset"] = relationship()


class AuctionBid(Base):
    """A team's single sealed bid for a round - hidden from other teams
    until the round resolves (bid -> reveal -> debit -> next round). A team
    may overwrite its own bid as many times as it likes while the round is
    still open; only the latest amount counts."""

    __tablename__ = "auction_bids"
    __table_args__ = (UniqueConstraint("auction_round_id", "team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    auction_round_id: Mapped[int] = mapped_column(ForeignKey("auction_rounds.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    amount: Mapped[int] = mapped_column(Integer)
    submitted_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())


class RosterEntry(Base):
    """The season-long 20-man roster a team owns."""

    __tablename__ = "roster_entries"
    __table_args__ = (UniqueConstraint("team_id", "draftable_asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    acquired_via: Mapped[str] = mapped_column(String(20), default="draft")
    acquired_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    # On the team's injured list: off the lines, doesn't play or score, and doesn't count toward the roster limit.
    on_injured_list: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    draftable_asset: Mapped["DraftableAsset"] = relationship()


class Week(Base):
    """Maps a fantasy week to each universe's real calendar - needed
    independently for college/NFL, and for Dual Universe later."""

    __tablename__ = "weeks"
    __table_args__ = (UniqueConstraint("universe_id", "season_year", "week_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"))
    season_year: Mapped[int] = mapped_column(Integer)
    week_number: Mapped[int] = mapped_column(Integer)
    start_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    end_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    is_playoffs: Mapped[bool] = mapped_column(Boolean, default=False)


class LineupEntry(Base):
    """The week-by-week starting lineup - separate from roster_entries so
    'set lineup' is its own weekly action distinct from roster ownership."""

    __tablename__ = "lineup_entries"
    __table_args__ = (UniqueConstraint("team_id", "week_id", "draftable_asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    week_id: Mapped[int] = mapped_column(ForeignKey("weeks.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    roster_slot_id: Mapped[int] = mapped_column(ForeignKey("roster_slot_definitions.id"))

    draftable_asset: Mapped["DraftableAsset"] = relationship()
    roster_slot: Mapped["RosterSlotDefinition"] = relationship()


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True)
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"))
    week_id: Mapped[int] = mapped_column(ForeignKey("weeks.id"))
    home_team_id: Mapped[int] = mapped_column(ForeignKey("real_teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("real_teams.id"))
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kickoff_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")


class WeeklyMatchup(Base):
    __tablename__ = "weekly_matchups"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    week_id: Mapped[int] = mapped_column(ForeignKey("weeks.id"))
    team_a_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    team_b_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    team_a_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    team_b_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    winner_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True)


class LeagueSeries(Base):
    """A multi-day fantasy matchup between the same two league teams."""

    __tablename__ = "league_series"
    __table_args__ = (
        UniqueConstraint(
            "league_id", "team_a_id", "team_b_id", "start_date",
            name="uq_league_series_teams_start",
        ),
        CheckConstraint("team_a_id <> team_b_id", name="ck_league_series_distinct_teams"),
        CheckConstraint("end_date >= start_date", name="ck_league_series_date_order"),
        CheckConstraint("total_days IN (5, 7, 10)", name="ck_league_series_total_days"),
        CheckConstraint("team_a_wins >= 0", name="ck_league_series_team_a_wins"),
        CheckConstraint("team_b_wins >= 0", name="ck_league_series_team_b_wins"),
        CheckConstraint(
            "team_a_wins + team_b_wins <= total_days",
            name="ck_league_series_wins_within_total",
        ),
        CheckConstraint(
            "status IN ('SCHEDULED', 'IN_PROGRESS', 'FINAL')",
            name="ck_league_series_status",
        ),
        Index("ix_league_series_league_start_date", "league_id", "start_date"),
        Index("ix_league_series_team_a_id", "team_a_id"),
        Index("ix_league_series_team_b_id", "team_b_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    team_a_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    team_b_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    start_date: Mapped[datetime.date] = mapped_column()
    end_date: Mapped[datetime.date] = mapped_column()
    total_days: Mapped[int] = mapped_column(Integer, default=7)
    team_a_wins: Mapped[int] = mapped_column(Integer, default=0)
    team_b_wins: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="SCHEDULED")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    league: Mapped["League"] = relationship(back_populates="series")
    team_a: Mapped["Team"] = relationship(foreign_keys=[team_a_id])
    team_b: Mapped["Team"] = relationship(foreign_keys=[team_b_id])
    daily_games: Mapped[list["SeriesDailyGame"]] = relationship(back_populates="series")


class SeriesDailyGame(Base):
    """One independently scored calendar day within a league series."""

    __tablename__ = "series_daily_games"
    __table_args__ = (
        UniqueConstraint("series_id", "calendar_date", name="uq_series_daily_game_date"),
        UniqueConstraint(
            "calendar_date", "home_team_id", "away_team_id",
            name="uq_series_daily_game_teams_date",
        ),
        CheckConstraint("home_team_id <> away_team_id", name="ck_series_daily_game_distinct_teams"),
        CheckConstraint("window_index IS NULL OR window_index IN (1, 2, 3)", name="ck_series_daily_game_window_index"),
        CheckConstraint(
            "game_score_rating BETWEEN 0 AND 100",
            name="ck_series_daily_game_score_rating",
        ),
        CheckConstraint("home_score >= 0", name="ck_series_daily_game_home_score"),
        CheckConstraint("away_score >= 0", name="ck_series_daily_game_away_score"),
        CheckConstraint(
            "current_period BETWEEN 0 AND 3",
            name="ck_series_daily_game_current_period",
        ),
        CheckConstraint(
            "seconds_remaining BETWEEN 0 AND 1200",
            name="ck_series_daily_game_seconds_remaining",
        ),
        CheckConstraint(
            "status IN ('SCHEDULED', 'LIVE_SIMULATING', 'FINAL')",
            name="ck_series_daily_game_status",
        ),
        Index("ix_series_daily_games_calendar_status", "calendar_date", "status"),
        Index("ix_series_daily_games_home_team_id", "home_team_id"),
        Index("ix_series_daily_games_away_team_id", "away_team_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("league_series.id"))
    calendar_date: Mapped[datetime.date] = mapped_column()
    # NHL window rows (docs/hockey_spec.md 5 and 10.1): one row per fantasy window - 1 Sun-Tue, 2 Wed-Thu, 3 Fri-Sat -
    # running from calendar_date to calendar_date_end. Both are NULL on legacy one-row-per-day games.
    calendar_date_end: Mapped[datetime.date | None] = mapped_column(nullable=True)
    window_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_matchup_day: Mapped[bool] = mapped_column(Boolean, default=True)
    game_score_rating: Mapped[int] = mapped_column(Integer, default=50)
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    home_score: Mapped[int] = mapped_column(Integer, default=0)
    away_score: Mapped[int] = mapped_column(Integer, default=0)
    current_period: Mapped[int] = mapped_column(Integer, default=0)
    seconds_remaining: Mapped[int] = mapped_column(Integer, default=1200)
    status: Mapped[str] = mapped_column(String(20), default="SCHEDULED")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    series: Mapped["LeagueSeries"] = relationship(back_populates="daily_games")
    home_team: Mapped["Team"] = relationship(foreign_keys=[home_team_id])
    away_team: Mapped["Team"] = relationship(foreign_keys=[away_team_id])


class TeamPeckingOrder(Base):
    """One ranked slot in an NHL team's depth chart (docs/hockey_spec.md section 4). C and G rank
    4 deep; W and D rank 8 deep (pooled: the top two active dress). Ranks are 1-based; a team may
    rank fewer players than the maximum, and a roster player may be left unranked."""

    __tablename__ = "team_pecking_order"
    __table_args__ = (
        UniqueConstraint("team_id", "position_group", "priority", name="uq_pecking_order_team_group_priority"),
        UniqueConstraint("team_id", "draftable_asset_id", name="uq_pecking_order_team_asset"),
        CheckConstraint("position_group IN ('C', 'W', 'D', 'G')", name="ck_pecking_order_position_group"),
        CheckConstraint(
            "(position_group IN ('C', 'G') AND priority BETWEEN 1 AND 4) OR "
            "(position_group IN ('W', 'D') AND priority BETWEEN 1 AND 8)",
            name="ck_pecking_order_priority_range",
        ),
        Index("ix_pecking_order_team_id", "team_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    position_group: Mapped[str] = mapped_column(String(1))
    priority: Mapped[int] = mapped_column(Integer)
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    draftable_asset: Mapped["DraftableAsset"] = relationship()


class WeeklyTeamScore(Base):
    """Sum of a fantasy team's active lineup's player scores for a week -
    feeds weekly_matchups. Standings are NOT a stored table; they're
    derived on read from weekly_matchups (see docs/data_contract.md)."""

    __tablename__ = "weekly_team_scores"
    __table_args__ = (UniqueConstraint("team_id", "week_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    week_id: Mapped[int] = mapped_column(ForeignKey("weeks.id"))
    total_points: Mapped[float] = mapped_column(Float, default=0.0)


class RecruitingSettings(Base):
    """Commissioner-chosen settings for a league's recruiting draft. Kept in
    its own table (not columns on leagues) so they can be added to databases
    that predate them without an ALTER."""

    __tablename__ = "recruiting_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), unique=True)
    # How long each recruiting period lasts. The draft has one period per
    # roster spot (League.draft_rounds), so a 20-man roster means 20 periods.
    period_seconds: Mapped[int] = mapped_column(Integer)
    # The pause between periods: results are shown and no bids can be placed.
    buffer_seconds: Mapped[int] = mapped_column(Integer, default=20)


class RecruitingDraft(Base):
    """A league's recruiting draft (draft_type="recruit"): a series of
    fixed-length periods - the recruiting equivalent of rounds. During any
    period every team can bid on any player at any time; bids are processed
    only when a period ends (see platform/recruiting/engine.py). Separate from
    AuctionDraft, the one-player-at-a-time timed auction."""

    __tablename__ = "recruiting_drafts"
    __table_args__ = (UniqueConstraint("league_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open / closed
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    period_seconds: Mapped[int] = mapped_column(Integer)
    buffer_seconds: Mapped[int] = mapped_column(Integer, default=20)
    total_periods: Mapped[int] = mapped_column(Integer)
    # Periods whose bids have already been processed (0..total_periods).
    periods_processed: Mapped[int] = mapped_column(Integer, default=0)


class RecruitingThreshold(Base):
    """A player's hidden price for one recruiting draft. Never exposed by the
    API before the player is awarded."""

    __tablename__ = "recruiting_thresholds"
    __table_args__ = (UniqueConstraint("draft_id", "draftable_asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("recruiting_drafts.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    threshold: Mapped[int] = mapped_column(Integer)
    is_elite: Mapped[bool] = mapped_column(Boolean, default=False)


class RecruitingBid(Base):
    """A team's standing bid on one player. Persists across periods (and can
    be raised, lowered or retracted) until the player is awarded or the draft
    ends."""

    __tablename__ = "recruiting_bids"
    __table_args__ = (UniqueConstraint("draft_id", "team_id", "draftable_asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("recruiting_drafts.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    amount: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime)


class RecruitingAward(Base):
    """A player awarded to a team when a period's bids were processed.
    reason: "threshold" (the sum of bids met the hidden price) or "deadline"
    (the final period ended; the highest bidder takes the player regardless
    of price). price is what the winner bid, and is charged against budget."""

    __tablename__ = "recruiting_awards"
    __table_args__ = (UniqueConstraint("draft_id", "draftable_asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("recruiting_drafts.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    period_number: Mapped[int] = mapped_column(Integer)
    price: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(20))
    was_tie_broken_randomly: Mapped[bool] = mapped_column(Boolean, default=False)
    awarded_at: Mapped[datetime.datetime] = mapped_column(DateTime)

    draftable_asset: Mapped["DraftableAsset"] = relationship()


class RecruitingCharge(Base):
    """Money a team spends when a player is awarded. The money bid on a player
    goes to the player: EVERY team with a standing bid on him is charged its
    bid when he is awarded, whether or not it won him. So a team that bid $5
    on a player who went to someone else loses that $5. is_winner marks the
    team that got the player. A team's spend is the sum of its charges; bids
    on players who are never awarded are never charged."""

    __tablename__ = "recruiting_charges"
    __table_args__ = (UniqueConstraint("draft_id", "team_id", "draftable_asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("recruiting_drafts.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    period_number: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer)
    is_winner: Mapped[bool] = mapped_column(Boolean)


class RecruitingBidResult(Base):
    """What happened to one team's bid on one player when a period was
    processed - a snapshot taken at that moment, so it stays accurate after the
    bid is changed. completion_pct is the total of all (eligible) bids on the
    player divided by his hidden price, as a percent; it is shown to a team
    only in its own end-of-period results screen, never elsewhere.
    outcome: "won" / "lost" (someone else won him - the bid is still charged)
    / "pending" (his price wasn't met; the bid carries into the next period)."""

    __tablename__ = "recruiting_bid_results"
    __table_args__ = (UniqueConstraint("draft_id", "period_number", "team_id", "draftable_asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("recruiting_drafts.id"))
    period_number: Mapped[int] = mapped_column(Integer)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    bid_amount: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(10))
    completion_pct: Mapped[float] = mapped_column(Float)
    charged: Mapped[int] = mapped_column(Integer)


class RecruitingResultAck(Base):
    """A team has closed the results screen for a period. Until it has, the
    screen keeps coming back (even after a page reload)."""

    __tablename__ = "recruiting_result_acks"
    __table_args__ = (UniqueConstraint("draft_id", "team_id", "period_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("recruiting_drafts.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    period_number: Mapped[int] = mapped_column(Integer)


class UserEntitlement(Base):
    """A paid/premium feature switched on for a user (see platform/entitlements.py).
    Today's only feature is "commissioner_mode"."""

    __tablename__ = "user_entitlements"
    __table_args__ = (UniqueConstraint("user_id", "feature"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    feature: Mapped[str] = mapped_column(String(40))
    granted_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())


class FreeAgencyTransaction(Base):
    """A free-agency signing or release (see platform/free_agency.py). The
    league-wide Transactions log is these rows plus the recruiting awards.
    kind: "add" / "drop". reason: "user" (the owner did it), "commissioner"
    (a commissioner-mode commissioner acted for an AI team) or "ai_fill" (the
    AI's required fill of open roster spots). price is what the signing cost
    (0 for a drop)."""

    __tablename__ = "free_agency_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    kind: Mapped[str] = mapped_column(String(10))
    price: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime)

    draftable_asset: Mapped["DraftableAsset"] = relationship()


class InjuryAlert(Base):
    """A change in the injury status of a player on a team's roster, for that team's manager to see (created by
    platform/injury_sync.py). new_status None = healthy again. read_at is set when the manager dismisses it."""

    __tablename__ = "injury_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    player_name: Mapped[str] = mapped_column(String(100))
    old_status: Mapped[str | None] = mapped_column(String(10), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(10), nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    read_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)


class Trade(Base):
    """A trade proposal between two teams in a league.
    status:
      'proposed': direct trade waiting for counterparty human manager to accept/decline.
      'voting': trade involves an AI team, waiting for league majority approval.
      'accepted': trade was agreed upon and executed (players swapped).
      'declined': counterparty declined the trade.
      'rejected': trade was voted down by the league.
      'cancelled': proposer withdrew the trade.
    """

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), index=True)
    proposer_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    counterparty_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    league: Mapped["League"] = relationship(foreign_keys=[league_id])
    proposer_team: Mapped["Team"] = relationship(foreign_keys=[proposer_team_id])
    counterparty_team: Mapped["Team"] = relationship(foreign_keys=[counterparty_team_id])
    items: Mapped[list["TradeItem"]] = relationship(back_populates="trade", cascade="all, delete-orphan")
    votes: Mapped[list["TradeVote"]] = relationship(back_populates="trade", cascade="all, delete-orphan")


class TradeItem(Base):
    """An asset included in a trade proposal and which team is sending it."""

    __tablename__ = "trade_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), index=True)
    sender_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))

    trade: Mapped["Trade"] = relationship(back_populates="items")
    sender_team: Mapped["Team"] = relationship(foreign_keys=[sender_team_id])
    draftable_asset: Mapped["DraftableAsset"] = relationship(foreign_keys=[draftable_asset_id])


class TradeVote(Base):
    """A vote cast by a team in the league on an AI-involved trade requiring league majority approval."""

    __tablename__ = "trade_votes"
    __table_args__ = (
        UniqueConstraint("trade_id", "team_id", name="uq_trade_vote_team"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    vote: Mapped[str] = mapped_column(String(10))   # "approve" or "reject"
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime)

    trade: Mapped["Trade"] = relationship(back_populates="votes")
    team: Mapped["Team"] = relationship(foreign_keys=[team_id])



class ScheduleSettings(Base):
    """The preseason schedule stage of a league (platform/schedule.py): "open" while teams
    may still request different opponents, "closed" once the commissioner has closed it
    (pending requests to AI teams are resolved then, everything else pending is declined).
    A league without a row is "open"."""

    __tablename__ = "schedule_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), unique=True)
    status: Mapped[str] = mapped_column(String(10), default="open")
    closed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)


class MatchupRequest(Base):
    """One team asking another to play it in a given week, optionally sweetened with in-game
    cash. status: "pending" / "accepted" / "declined" / "withdrawn". The cash only moves if the
    request is accepted (an accepted request's offer_amount is paid from the sender to the
    receiver - see free_agency.team_money)."""

    __tablename__ = "matchup_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), index=True)
    week_id: Mapped[int] = mapped_column(ForeignKey("weeks.id"))
    from_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    to_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    offer_amount: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10), default="pending")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)


class SimulatorGame(Base):
    """The published progressive-simulation record for one series daily game (platform/simulator/). One
    row per daily game; its events are immutable once written. `status` is SCHEDULED until started, LIVE
    while the real-time window runs, FINAL after it."""

    __tablename__ = "simulator_games"
    __table_args__ = (
        CheckConstraint("status IN ('SCHEDULED', 'LIVE', 'FINAL')", name="ck_simulator_game_status"),
        CheckConstraint("periods >= 1 AND period_seconds >= 1 AND window_seconds >= 1", name="ck_simulator_game_shape"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    daily_game_id: Mapped[int] = mapped_column(ForeignKey("series_daily_games.id"), unique=True)
    contract_version: Mapped[str] = mapped_column(String(10))
    sport: Mapped[str] = mapped_column(String(30))
    periods: Mapped[int] = mapped_column(Integer)
    period_seconds: Mapped[int] = mapped_column(Integer)
    window_seconds: Mapped[int] = mapped_column(Integer)
    event_count: Mapped[int] = mapped_column(Integer)
    script_digest: Mapped[str] = mapped_column(String(64))
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    # Period-by-period publishing (NHL window games): how many periods have been published so far. NULL means the
    # whole script was published at once. Published events are never changed; later periods are only appended.
    published_periods: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="SCHEDULED")
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    finalized_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    daily_game: Mapped["SeriesDailyGame"] = relationship()
    events: Mapped[list["SimulatorEvent"]] = relationship(back_populates="game", order_by="SimulatorEvent.seq")


class SimulatorEvent(Base):
    """One immutable event of a published script. Totals are running totals after the event. Updates and
    deletes through the ORM are refused (see the listeners below)."""

    __tablename__ = "simulator_events"
    __table_args__ = (
        UniqueConstraint("simulator_game_id", "seq", name="uq_simulator_event_seq"),
        CheckConstraint("seq >= 1", name="ck_simulator_event_seq"),
        CheckConstraint(
            "home_score >= 0 AND away_score >= 0 AND home_shots >= 0 AND away_shots >= 0",
            name="ck_simulator_event_totals",
        ),
        CheckConstraint("team_side IS NULL OR team_side IN ('home', 'away')", name="ck_simulator_event_side"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    simulator_game_id: Mapped[int] = mapped_column(ForeignKey("simulator_games.id"))
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    period: Mapped[int] = mapped_column(Integer)
    game_second: Mapped[int] = mapped_column(Integer)
    team_side: Mapped[str | None] = mapped_column(String(4), nullable=True)
    home_score: Mapped[int] = mapped_column(Integer)
    away_score: Mapped[int] = mapped_column(Integer)
    home_shots: Mapped[int] = mapped_column(Integer)
    away_shots: Mapped[int] = mapped_column(Integer)
    headline: Mapped[str] = mapped_column(String(200))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)

    game: Mapped["SimulatorGame"] = relationship(back_populates="events")


@event.listens_for(SimulatorEvent, "before_update")
def _refuse_event_update(mapper, connection, target):
    raise ValueError("Simulator events are immutable")


@event.listens_for(SimulatorEvent, "before_delete")
def _refuse_event_delete(mapper, connection, target):
    raise ValueError("Simulator events are immutable")


class SimulatorReveal(Base):
    """How far one user has opened one simulated game. No row = still behind the spoiler veil.
    mode WATCHING: following the game; cursor_seq is the last event they have been shown (it only ever
    moves forward). mode SKIPPED: jumped straight to the box score; everything is open (terminal)."""

    __tablename__ = "simulator_reveals"
    __table_args__ = (
        UniqueConstraint("simulator_game_id", "user_id", name="uq_simulator_reveal_game_user"),
        CheckConstraint("mode IN ('WATCHING', 'SKIPPED')", name="ck_simulator_reveal_mode"),
        CheckConstraint("cursor_seq >= 0", name="ck_simulator_reveal_cursor"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    simulator_game_id: Mapped[int] = mapped_column(ForeignKey("simulator_games.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    mode: Mapped[str] = mapped_column(String(10))
    cursor_seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class BankedCredit(Base):
    """A rostered NHL player's off-day credit, banked for a later series game (docs/hockey_spec.md 5.2;
    rules in platform/banking.py). The points are scored upstream (FS-026) and only stored here; a credit
    is assigned to at most one game, and a game takes at most one credit per team."""

    __tablename__ = "off_day_banked_credits"
    __table_args__ = (
        UniqueConstraint("league_id", "source_key", name="uq_banked_credit_source"),
        UniqueConstraint("assigned_daily_game_id", "team_id", name="uq_banked_credit_game_team"),
        CheckConstraint("credit_points > 0", name="ck_banked_credit_points"),
        CheckConstraint(
            "applied_points IS NULL OR (applied_points > 0 AND applied_points <= 1.5)",
            name="ck_banked_credit_applied",
        ),
        CheckConstraint("status IN ('AVAILABLE', 'ASSIGNED', 'EXPIRED')", name="ck_banked_credit_status"),
        CheckConstraint(
            "assignment_mode IS NULL OR assignment_mode IN ('MANUAL', 'AUTO')", name="ck_banked_credit_mode"
        ),
        CheckConstraint(
            "(status = 'ASSIGNED' AND assigned_daily_game_id IS NOT NULL AND applied_points IS NOT NULL) OR "
            "(status <> 'ASSIGNED' AND assigned_daily_game_id IS NULL AND applied_points IS NULL)",
            name="ck_banked_credit_assignment",
        ),
        Index("ix_banked_credit_team_status", "team_id", "status"),
        Index("ix_banked_credit_league_status", "league_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    source_calendar_date: Mapped[datetime.date] = mapped_column()
    source_key: Mapped[str] = mapped_column(String(120))
    credit_points: Mapped[float] = mapped_column(Float)
    applied_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="AVAILABLE")
    status_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    assigned_daily_game_id: Mapped[int | None] = mapped_column(ForeignKey("series_daily_games.id"), nullable=True)
    assignment_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    assigned_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    expired_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    draftable_asset: Mapped["DraftableAsset"] = relationship()
    assigned_game: Mapped["SeriesDailyGame | None"] = relationship()


class WeeklyRedraftSettings(Base):
    """Settings of a league in weekly-redraft mode (leagues.league_mode = 'weekly_redraft'; platform/weekly_redraft.py).
    cap_policy FIXED is the only implemented policy: the prior-finish cap adjustment is an open product decision."""

    __tablename__ = "weekly_redraft_settings"
    __table_args__ = (
        CheckConstraint("base_cap > 0", name="ck_weekly_settings_cap"),
        CheckConstraint("cap_policy IN ('FIXED')", name="ck_weekly_settings_policy"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), unique=True)
    base_cap: Mapped[int] = mapped_column(Integer)
    cap_policy: Mapped[str] = mapped_column(String(10), default="FIXED")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())


class WeeklyContest(Base):
    """One week's private head-to-head round for a weekly-redraft league."""

    __tablename__ = "weekly_contests"
    __table_args__ = (
        UniqueConstraint("league_id", "week_index", name="uq_weekly_contest_week"),
        UniqueConstraint("league_id", "start_date", name="uq_weekly_contest_start"),
        CheckConstraint("end_date >= start_date", name="ck_weekly_contest_dates"),
        CheckConstraint("cap_applied > 0", name="ck_weekly_contest_cap"),
        CheckConstraint("status IN ('OPEN', 'LOCKED')", name="ck_weekly_contest_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    week_index: Mapped[int] = mapped_column(Integer)
    start_date: Mapped[datetime.date] = mapped_column()
    end_date: Mapped[datetime.date] = mapped_column()
    lock_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    cap_applied: Mapped[int] = mapped_column(Integer)   # the cap in force, snapshotted when the contest was created
    status: Mapped[str] = mapped_column(String(10), default="OPEN")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())


class WeeklyContestPairing(Base):
    __tablename__ = "weekly_contest_pairings"
    __table_args__ = (
        UniqueConstraint("contest_id", "team_a_id", name="uq_weekly_pairing_a"),
        UniqueConstraint("contest_id", "team_b_id", name="uq_weekly_pairing_b"),
        CheckConstraint("team_a_id <> team_b_id", name="ck_weekly_pairing_distinct"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contest_id: Mapped[int] = mapped_column(ForeignKey("weekly_contests.id"))
    team_a_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    team_b_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))


class WeeklyPlayerSalary(Base):
    """A player's price for one contest. Prices are an input (nothing here projects or invents them)."""

    __tablename__ = "weekly_player_salaries"
    __table_args__ = (
        UniqueConstraint("contest_id", "draftable_asset_id", name="uq_weekly_salary_asset"),
        CheckConstraint("salary > 0", name="ck_weekly_salary_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contest_id: Mapped[int] = mapped_column(ForeignKey("weekly_contests.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    salary: Mapped[int] = mapped_column(Integer)


class WeeklyContestEntry(Base):
    """A team's roster for a contest. DRAFT entries can be replaced until the lock; SUBMITTED entries are final and
    carry a snapshot of the salaries and cap they were checked against."""

    __tablename__ = "weekly_contest_entries"
    __table_args__ = (
        UniqueConstraint("contest_id", "team_id", name="uq_weekly_entry_team"),
        CheckConstraint("status IN ('DRAFT', 'SUBMITTED')", name="ck_weekly_entry_status"),
        CheckConstraint("salary_total >= 0 AND salary_total <= cap", name="ck_weekly_entry_cap"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contest_id: Mapped[int] = mapped_column(ForeignKey("weekly_contests.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    status: Mapped[str] = mapped_column(String(10), default="DRAFT")
    salary_total: Mapped[int] = mapped_column(Integer, default=0)
    cap: Mapped[int] = mapped_column(Integer)
    submitted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    slots: Mapped[list["WeeklyContestSlot"]] = relationship(order_by="WeeklyContestSlot.id")


class WeeklyContestSlot(Base):
    __tablename__ = "weekly_contest_slots"
    __table_args__ = (
        UniqueConstraint("entry_id", "slot_code", "slot_index", name="uq_weekly_slot_position"),
        UniqueConstraint("entry_id", "draftable_asset_id", name="uq_weekly_slot_asset"),
        CheckConstraint("salary > 0", name="ck_weekly_slot_salary"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("weekly_contest_entries.id"))
    slot_code: Mapped[str] = mapped_column(String(2))   # C / W / D / G
    slot_index: Mapped[int] = mapped_column(Integer, default=1)
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    salary: Mapped[int] = mapped_column(Integer)

    draftable_asset: Mapped["DraftableAsset"] = relationship()
