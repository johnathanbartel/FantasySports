"""
Round resolution engine: budgets across tiers, the bid -> reveal -> debit ->
advance cycle, round timing enforcement, and the random "player choice"
tie-break (seeded, so it is deterministic here).
"""

import datetime
import random

import pytest

from fantasy_sports.platform.auction.config import AuctionConfig, TierConfig
from fantasy_sports.platform.auction.engine import (
    AuctionError,
    create_session,
    player_choice,
    resolve_round,
    submit_bid,
)
from fantasy_sports.platform.db.models.auction import (
    AuctionBid,
    AuctionBudget,
    AuctionRound,
    AuctionRoundResult,
)
from fantasy_sports.platform.db.models.core import DraftPick, RosterEntry

START = datetime.datetime(2026, 8, 1, 12, 0, 0)

TIERS = (
    TierConfig(code="large", display_name="Large program", starting_budget=300, sort_order=1),
    TierConfig(code="moderate", display_name="Moderate program", starting_budget=225, sort_order=2),
    TierConfig(code="small", display_name="Small program", starting_budget=150, sort_order=3),
)


def make_config(**kwargs) -> AuctionConfig:
    base = dict(slots=3, tiers=TIERS, default_tier_code="moderate", round_seconds=180)
    base.update(kwargs)
    return AuctionConfig(**base)


def start_session(db, fixture, *, config=None, tier_by_index=None, starts_at=START, now=START):
    """Open a Recruiting Period for the seeded league.

    `tier_by_index` maps team position (0/1/2) to a tier code so tests can say
    "Alpha is a large program" without carrying database ids around.
    """
    team_tier_codes = {}
    if tier_by_index:
        for index, code in tier_by_index.items():
            team_tier_codes[fixture.team_ids[index]] = code

    session = create_session(
        db,
        league_id=fixture.league.id,
        config=config or make_config(),
        team_tier_codes=team_tier_codes,
        starts_at=starts_at,
        now=now,
    )
    db.commit()
    return session


def budget_for(db, session, team_id) -> AuctionBudget:
    return (
        db.query(AuctionBudget)
        .filter(
            AuctionBudget.auction_session_id == session.id,
            AuctionBudget.team_id == team_id,
        )
        .one()
    )


# --- Budgets and tiers ------------------------------------------------------


def test_each_tier_gets_its_own_starting_budget(db, seeded_league):
    session = start_session(
        db,
        seeded_league,
        tier_by_index={0: "large", 1: "moderate", 2: "small"},
    )

    starting = [budget_for(db, session, tid).starting_budget for tid in seeded_league.team_ids]
    assert starting == [300, 225, 150]
    assert all(
        budget_for(db, session, tid).spent_budget == 0 for tid in seeded_league.team_ids
    )


def test_unlisted_teams_fall_back_to_the_default_tier(db, seeded_league):
    session = start_session(db, seeded_league, tier_by_index={0: "large"})

    assert budget_for(db, session, seeded_league.team_ids[0]).starting_budget == 300
    # The other two were never assigned, so they get the "moderate" default.
    assert budget_for(db, session, seeded_league.team_ids[1]).starting_budget == 225
    assert budget_for(db, session, seeded_league.team_ids[2]).starting_budget == 225


def test_winner_is_debited_exactly_their_bid_and_losers_pay_nothing(db, seeded_league):
    alpha, bravo, charlie = seeded_league.team_ids
    session = start_session(
        db, seeded_league, tier_by_index={0: "large", 1: "moderate", 2: "small"}
    )
    beck = seeded_league.player_assets["Carson Beck"].id
    leonard = seeded_league.player_assets["Riley Leonard"].id

    submit_bid(db, session, team_id=alpha, draftable_asset_id=beck, amount=95, now=START)
    submit_bid(db, session, team_id=bravo, draftable_asset_id=beck, amount=60, now=START)
    submit_bid(db, session, team_id=charlie, draftable_asset_id=leonard, amount=40, now=START)

    resolve_round(db, session, rng=random.Random(1), now=START, force=True)
    db.commit()

    assert budget_for(db, session, alpha).spent_budget == 95
    assert budget_for(db, session, alpha).remaining_budget == 300 - 95
    # Bravo was outbid, so it is not charged at all.
    assert budget_for(db, session, bravo).spent_budget == 0
    assert budget_for(db, session, bravo).remaining_budget == 225
    # Charlie's uncontested bid still costs exactly what it bid.
    assert budget_for(db, session, charlie).spent_budget == 40
    assert budget_for(db, session, charlie).remaining_budget == 150 - 40


def test_a_bid_above_remaining_budget_is_rejected(db, seeded_league):
    charlie = seeded_league.team_ids[2]
    session = start_session(db, seeded_league, tier_by_index={2: "small"})
    beck = seeded_league.player_assets["Carson Beck"].id

    with pytest.raises(AuctionError, match="exceeds"):
        submit_bid(db, session, team_id=charlie, draftable_asset_id=beck, amount=151, now=START)

    # And the same amount is fine for a team in a bigger-budget tier.
    submit_bid(
        db, session, team_id=seeded_league.team_ids[0], draftable_asset_id=beck, amount=151,
        now=START,
    )


def test_spending_accumulates_across_rounds(db, seeded_league):
    alpha = seeded_league.team_ids[0]
    session = start_session(db, seeded_league, tier_by_index={0: "large"})

    submit_bid(
        db, session, team_id=alpha,
        draftable_asset_id=seeded_league.player_assets["Carson Beck"].id, amount=100, now=START,
    )
    resolve_round(db, session, rng=random.Random(1), now=START, force=True)

    submit_bid(
        db, session, team_id=alpha,
        draftable_asset_id=seeded_league.player_assets["Riley Leonard"].id, amount=45,
        now=START + datetime.timedelta(seconds=10),
    )
    resolve_round(
        db, session, rng=random.Random(1), now=START + datetime.timedelta(seconds=20), force=True
    )
    db.commit()

    assert budget_for(db, session, alpha).spent_budget == 145
    assert budget_for(db, session, alpha).remaining_budget == 155


# --- Round timing -----------------------------------------------------------


def test_session_creation_plans_every_round_window(db, seeded_league):
    session = start_session(db, seeded_league, config=make_config(slots=3, round_seconds=300))

    rounds = (
        db.query(AuctionRound)
        .filter(AuctionRound.auction_session_id == session.id)
        .order_by(AuctionRound.round_number)
        .all()
    )
    assert [r.round_number for r in rounds] == [1, 2, 3]
    assert rounds[0].status == "open"
    assert [r.status for r in rounds[1:]] == ["pending", "pending"]
    assert rounds[0].opens_at == START
    assert rounds[0].closes_at == START + datetime.timedelta(seconds=300)
    assert rounds[1].opens_at == rounds[0].closes_at


def test_bidding_after_the_window_closes_is_refused(db, seeded_league):
    session = start_session(db, seeded_league, config=make_config(round_seconds=180))
    late = START + datetime.timedelta(seconds=181)

    with pytest.raises(AuctionError, match="closed"):
        submit_bid(
            db, session, team_id=seeded_league.team_ids[0],
            draftable_asset_id=seeded_league.player_assets["Carson Beck"].id,
            amount=10, now=late,
        )


def test_a_round_cannot_be_resolved_before_its_window_expires(db, seeded_league):
    session = start_session(db, seeded_league, config=make_config(round_seconds=180))

    with pytest.raises(AuctionError, match="still open"):
        resolve_round(db, session, rng=random.Random(1), now=START + datetime.timedelta(seconds=5))

    # Once the window has expired it resolves without the commissioner
    # override.
    resolution = resolve_round(
        db, session, rng=random.Random(1), now=START + datetime.timedelta(seconds=181)
    )
    assert resolution.round_number == 1


def test_advancing_early_slides_the_remaining_windows(db, seeded_league):
    session = start_session(db, seeded_league, config=make_config(slots=3, round_seconds=300))
    resolved_at = START + datetime.timedelta(seconds=42)

    resolve_round(db, session, rng=random.Random(1), now=resolved_at, force=True)
    db.commit()

    rounds = (
        db.query(AuctionRound)
        .filter(AuctionRound.auction_session_id == session.id)
        .order_by(AuctionRound.round_number)
        .all()
    )
    assert rounds[0].status == "resolved"
    assert rounds[1].status == "open"
    # Round 2 starts when round 1 actually finished, not at its planned time.
    assert rounds[1].opens_at == resolved_at
    assert rounds[1].closes_at == resolved_at + datetime.timedelta(seconds=300)
    # ...and round 3 slides along behind it.
    assert rounds[2].opens_at == rounds[1].closes_at


def test_a_scheduled_period_does_not_accept_bids_before_it_opens(db, seeded_league):
    session = start_session(
        db,
        seeded_league,
        starts_at=START + datetime.timedelta(hours=1),
        now=START,
    )
    assert session.status == "pending"

    with pytest.raises(AuctionError, match="not started"):
        submit_bid(
            db, session, team_id=seeded_league.team_ids[0],
            draftable_asset_id=seeded_league.player_assets["Carson Beck"].id,
            amount=10, now=START,
        )

    # Once the start time arrives, round 1 opens on the first bid.
    bid = submit_bid(
        db, session, team_id=seeded_league.team_ids[0],
        draftable_asset_id=seeded_league.player_assets["Carson Beck"].id,
        amount=10, now=START + datetime.timedelta(hours=1, seconds=1),
    )
    assert bid.amount == 10
    assert session.status == "in_progress"


# --- The full bid -> reveal -> debit -> advance cycle -----------------------


def test_full_cycle_debits_rosters_and_advances(db, seeded_league):
    alpha, bravo, charlie = seeded_league.team_ids
    session = start_session(
        db, seeded_league, tier_by_index={0: "large", 1: "moderate", 2: "small"}
    )
    beck = seeded_league.player_assets["Carson Beck"].id
    leonard = seeded_league.player_assets["Riley Leonard"].id

    # Bid: one blind bid each, from the revealed pool.
    submit_bid(db, session, team_id=alpha, draftable_asset_id=beck, amount=80, now=START)
    submit_bid(db, session, team_id=bravo, draftable_asset_id=beck, amount=79, now=START)
    submit_bid(db, session, team_id=charlie, draftable_asset_id=leonard, amount=30, now=START)

    # Reveal + debit + advance.
    resolution = resolve_round(db, session, rng=random.Random(7), now=START, force=True)
    db.commit()

    # Two assets were bid on, so the round produced two contests.
    by_asset = {c.draftable_asset_id: c for c in resolution.contests}
    assert set(by_asset) == {beck, leonard}
    assert by_asset[beck].winning_team_id == alpha
    assert by_asset[beck].winning_amount == 80
    assert by_asset[beck].bid_count == 2
    assert by_asset[beck].tie_break_used is False
    assert by_asset[leonard].winning_team_id == charlie

    # Debit.
    assert budget_for(db, session, alpha).remaining_budget == 220
    assert budget_for(db, session, charlie).remaining_budget == 120
    assert budget_for(db, session, bravo).remaining_budget == 225

    # Losing bid is recorded as outbid, not silently dropped.
    losing = db.get(AuctionBid, resolution.losing_bid_ids[0])
    assert losing.team_id == bravo
    assert losing.status == "lost"
    assert losing.lost_reason == "outbid"

    # Won assets land in the existing draft/roster tables.
    picks = db.query(DraftPick).filter(DraftPick.draft_id == session.draft_id).all()
    assert {(p.team_id, p.draftable_asset_id) for p in picks} == {(alpha, beck), (charlie, leonard)}
    assert all(p.round == 1 for p in picks)
    # Highest price picks first.
    assert sorted(picks, key=lambda p: p.pick_number)[0].draftable_asset_id == beck
    assert {(r.team_id, r.draftable_asset_id) for r in db.query(RosterEntry).all()} == {
        (alpha, beck),
        (charlie, leonard),
    }
    assert all(r.acquired_via == "auction" for r in db.query(RosterEntry).all())

    # Advance.
    assert resolution.next_round_number == 2
    assert resolution.session_complete is False
    assert session.current_round_number == 2


def test_a_won_asset_leaves_the_pool(db, seeded_league):
    alpha, bravo, _ = seeded_league.team_ids
    session = start_session(db, seeded_league)
    beck = seeded_league.player_assets["Carson Beck"].id

    submit_bid(db, session, team_id=alpha, draftable_asset_id=beck, amount=50, now=START)
    resolve_round(db, session, rng=random.Random(1), now=START, force=True)
    db.commit()

    with pytest.raises(AuctionError, match="already been won"):
        submit_bid(
            db, session, team_id=bravo, draftable_asset_id=beck, amount=60,
            now=START + datetime.timedelta(seconds=1),
        )


def test_one_bid_per_owner_per_round_and_resubmission_replaces_it(db, seeded_league):
    alpha = seeded_league.team_ids[0]
    session = start_session(db, seeded_league)
    beck = seeded_league.player_assets["Carson Beck"].id
    leonard = seeded_league.player_assets["Riley Leonard"].id

    submit_bid(db, session, team_id=alpha, draftable_asset_id=beck, amount=50, now=START)
    submit_bid(
        db, session, team_id=alpha, draftable_asset_id=leonard, amount=70,
        now=START + datetime.timedelta(seconds=1),
    )
    db.commit()

    bids = db.query(AuctionBid).filter(AuctionBid.auction_session_id == session.id).all()
    assert len(bids) == 1
    assert bids[0].draftable_asset_id == leonard
    assert bids[0].amount == 70


def test_resolving_the_final_round_completes_the_session(db, seeded_league):
    alpha = seeded_league.team_ids[0]
    session = start_session(db, seeded_league, config=make_config(slots=2))
    assets = [
        seeded_league.player_assets["Carson Beck"].id,
        seeded_league.player_assets["Riley Leonard"].id,
    ]

    for index, asset_id in enumerate(assets):
        now = START + datetime.timedelta(seconds=index)
        submit_bid(db, session, team_id=alpha, draftable_asset_id=asset_id, amount=10, now=now)
        resolution = resolve_round(db, session, rng=random.Random(1), now=now, force=True)
    db.commit()

    assert resolution.session_complete is True
    assert resolution.next_round_number is None
    assert session.status == "complete"
    assert session.current_round_number is None
    assert session.completed_at is not None
    with pytest.raises(AuctionError, match="already finished"):
        resolve_round(db, session, rng=random.Random(1), now=START, force=True)


def test_a_round_with_no_bids_still_advances(db, seeded_league):
    session = start_session(db, seeded_league)

    resolution = resolve_round(db, session, rng=random.Random(1), now=START, force=True)

    assert resolution.contests == []
    assert resolution.next_round_number == 2


# --- Tie-break: random "player choice" --------------------------------------


def test_tie_break_picks_one_tied_owner_and_only_charges_them(db, seeded_league):
    alpha, bravo, charlie = seeded_league.team_ids
    session = start_session(
        db, seeded_league, tier_by_index={0: "large", 1: "moderate", 2: "small"}
    )
    beck = seeded_league.player_assets["Carson Beck"].id

    # All three owners bid exactly 75 on the same player.
    for team_id in (alpha, bravo, charlie):
        submit_bid(db, session, team_id=team_id, draftable_asset_id=beck, amount=75, now=START)

    resolution = resolve_round(db, session, rng=random.Random(2024), now=START, force=True)
    db.commit()

    contest = resolution.contests[0]
    assert contest.tie_break_used is True
    assert contest.tied_team_ids == sorted([alpha, bravo, charlie])
    assert contest.winning_team_id in (alpha, bravo, charlie)
    assert contest.winning_amount == 75

    # Exactly one owner paid; the other two paid nothing and are marked as
    # having lost the coin flip rather than having been outbid.
    spent = {tid: budget_for(db, session, tid).spent_budget for tid in seeded_league.team_ids}
    assert sorted(spent.values()) == [0, 0, 75]
    assert spent[contest.winning_team_id] == 75

    losers = [db.get(AuctionBid, bid_id) for bid_id in resolution.losing_bid_ids]
    assert {bid.lost_reason for bid in losers} == {"tie_break"}

    stored = db.query(AuctionRoundResult).one()
    assert stored.tie_break_used is True
    assert stored.tied_team_ids == sorted([alpha, bravo, charlie])


def test_tie_break_is_deterministic_for_a_given_seed(db, seeded_league):
    """Same seed, same winner - which is what makes the mechanic testable and
    a disputed draft auditable."""
    alpha, bravo, _ = seeded_league.team_ids
    winners = []
    for _ in range(3):
        session = start_session(db, seeded_league)
        beck = seeded_league.player_assets["Carson Beck"].id
        submit_bid(db, session, team_id=alpha, draftable_asset_id=beck, amount=50, now=START)
        submit_bid(db, session, team_id=bravo, draftable_asset_id=beck, amount=50, now=START)
        resolution = resolve_round(db, session, rng=random.Random(99), now=START, force=True)
        winners.append(resolution.contests[0].winning_team_id)
        db.rollback()

    assert len(set(winners)) == 1


def test_player_choice_is_uniform_over_the_tied_owners():
    """Over many seeds every tied owner wins sometimes, and roughly equally -
    the tie-break is a genuine coin flip, not a disguised "lowest team id".
    """

    class FakeBid:
        def __init__(self, team_id):
            self.team_id = team_id

    tied = [FakeBid(30), FakeBid(10), FakeBid(20)]
    counts = {10: 0, 20: 0, 30: 0}
    for seed in range(600):
        counts[player_choice(random.Random(seed), tied).team_id] += 1

    assert all(count > 120 for count in counts.values()), counts
    assert sum(counts.values()) == 600


def test_bid_ordering_from_the_database_does_not_change_the_seeded_winner():
    """The candidates are sorted by team id before the draw, so however the
    database hands back the rows, a given seed picks the same owner."""

    class FakeBid:
        def __init__(self, team_id):
            self.team_id = team_id

    ascending = [FakeBid(1), FakeBid(2), FakeBid(3)]
    descending = [FakeBid(3), FakeBid(2), FakeBid(1)]

    for seed in range(50):
        assert (
            player_choice(random.Random(seed), ascending).team_id
            == player_choice(random.Random(seed), descending).team_id
        )


# --- Defensive position groups (docs/data_contract.md decision) -------------


def test_defense_groups_are_independent_bid_lines(db, seeded_league):
    """Each DL/LB/DB group is its own auction line, never bundled by real team.

    Decision recorded in docs/data_contract.md, "Decision: Timed Auction Draft
    defensive position bidding granularity".
    """
    alpha, bravo, charlie = seeded_league.team_ids
    session = start_session(db, seeded_league)
    georgia_dl = seeded_league.defense_assets["Georgia DL"].id
    georgia_lb = seeded_league.defense_assets["Georgia LB"].id
    notre_dame_dl = seeded_league.defense_assets["Notre Dame DL"].id

    # Three owners, three separate defensive lines - including two groups from
    # the same real team going to two different owners.
    submit_bid(db, session, team_id=alpha, draftable_asset_id=georgia_dl, amount=40, now=START)
    submit_bid(db, session, team_id=bravo, draftable_asset_id=georgia_lb, amount=35, now=START)
    submit_bid(db, session, team_id=charlie, draftable_asset_id=notre_dame_dl, amount=30, now=START)

    resolution = resolve_round(db, session, rng=random.Random(1), now=START, force=True)
    db.commit()

    winners = {c.draftable_asset_id: c.winning_team_id for c in resolution.contests}
    assert winners == {georgia_dl: alpha, georgia_lb: bravo, notre_dame_dl: charlie}

    # Winning Georgia's DL does not sweep up Georgia's other groups: DB is
    # still available to bid on in the next round.
    georgia_db = seeded_league.defense_assets["Georgia DB"].id
    bid = submit_bid(
        db, session, team_id=alpha, draftable_asset_id=georgia_db, amount=25,
        now=START + datetime.timedelta(seconds=1),
    )
    assert bid.draftable_asset_id == georgia_db


def test_two_owners_can_contest_a_single_defense_group(db, seeded_league):
    """A defense group is bid on exactly like a player - one asset, one price."""
    alpha, bravo, _ = seeded_league.team_ids
    session = start_session(db, seeded_league)
    georgia_dl = seeded_league.defense_assets["Georgia DL"].id

    submit_bid(db, session, team_id=alpha, draftable_asset_id=georgia_dl, amount=44, now=START)
    submit_bid(db, session, team_id=bravo, draftable_asset_id=georgia_dl, amount=45, now=START)

    resolution = resolve_round(db, session, rng=random.Random(1), now=START, force=True)
    db.commit()

    assert len(resolution.contests) == 1
    assert resolution.contests[0].winning_team_id == bravo
    assert resolution.contests[0].winning_amount == 45
    assert budget_for(db, session, bravo).spent_budget == 45
    assert budget_for(db, session, alpha).spent_budget == 0

def test_re_winning_an_already_rostered_asset_does_not_duplicate_the_roster(db, seeded_league):
    """A league can run a second draft over the same pool (the snake path
    allows it), so a winner may already hold the asset. roster_entries is
    unique on (team, asset) - re-winning must not blow up on that."""
    alpha = seeded_league.team_ids[0]
    beck = seeded_league.player_assets["Carson Beck"].id
    db.add(RosterEntry(team_id=alpha, draftable_asset_id=beck, acquired_via="draft"))
    db.commit()

    session = start_session(db, seeded_league)
    submit_bid(db, session, team_id=alpha, draftable_asset_id=beck, amount=25, now=START)
    resolve_round(db, session, rng=random.Random(1), now=START, force=True)
    db.commit()

    entries = (
        db.query(RosterEntry)
        .filter(RosterEntry.team_id == alpha, RosterEntry.draftable_asset_id == beck)
        .all()
    )
    assert len(entries) == 1
    # The bid still cost real money and still produced a draft pick.
    assert budget_for(db, session, alpha).spent_budget == 25
    assert (
        db.query(DraftPick)
        .filter(DraftPick.draft_id == session.draft_id, DraftPick.draftable_asset_id == beck)
        .count()
        == 1
    )
