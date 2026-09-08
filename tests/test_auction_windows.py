"""Round window math: dividing a Recruiting Period into per-slot rounds."""

import datetime

import pytest

from fantasy_sports.platform.auction.config import (
    MINIMUM_ROUND_SECONDS,
    AuctionConfig,
    TierConfig,
    plan_round_windows,
)

START = datetime.datetime(2026, 8, 1, 12, 0, 0)

TIERS = (
    TierConfig(code="large", display_name="Large", starting_budget=300),
    TierConfig(code="moderate", display_name="Moderate", starting_budget=225),
    TierConfig(code="small", display_name="Small", starting_budget=150),
)


def _config(**kwargs) -> AuctionConfig:
    base = dict(slots=20, tiers=TIERS, default_tier_code="moderate")
    base.update(kwargs)
    return AuctionConfig(**base)


def test_windows_tile_the_period_back_to_back():
    windows = plan_round_windows(START, slots=4, round_seconds=300)

    assert [w.round_number for w in windows] == [1, 2, 3, 4]
    assert all(w.duration_seconds == 300 for w in windows)
    # No gaps and no overlaps: each round opens exactly when the last closed.
    for earlier, later in zip(windows, windows[1:]):
        assert later.opens_at == earlier.closes_at
    assert windows[0].opens_at == START
    assert windows[-1].closes_at == START + datetime.timedelta(seconds=4 * 300)


def test_window_below_the_three_minute_floor_is_rejected():
    with pytest.raises(ValueError, match="floor"):
        plan_round_windows(START, slots=4, round_seconds=MINIMUM_ROUND_SECONDS - 1)


def test_explicit_round_seconds_below_floor_is_a_config_error():
    with pytest.raises(ValueError, match="floor"):
        _config(round_seconds=179)


def test_three_minutes_exactly_is_allowed():
    assert _config(round_seconds=180).resolved_round_seconds() == 180


def test_period_divided_by_slots_gives_the_window():
    # 20 slots across a 4-hour Recruiting Period = 12-minute rounds.
    config = _config(slots=20, recruiting_period_seconds=4 * 60 * 60)

    assert config.resolved_round_seconds() == 720
    assert config.total_period_seconds() == 4 * 60 * 60


def test_too_short_a_period_clamps_up_to_the_floor():
    # 20 slots in half an hour would be 90-second rounds; the floor wins and
    # the period stretches instead.
    config = _config(slots=20, recruiting_period_seconds=30 * 60)

    assert config.resolved_round_seconds() == MINIMUM_ROUND_SECONDS
    assert config.total_period_seconds() == 20 * MINIMUM_ROUND_SECONDS


def test_defaults_to_the_floor_when_nothing_is_configured():
    assert _config().resolved_round_seconds() == MINIMUM_ROUND_SECONDS


def test_a_sport_may_raise_its_own_floor_but_not_lower_it():
    raised = _config(minimum_round_seconds=600)
    assert raised.resolved_round_seconds() == 600
    with pytest.raises(ValueError, match="floor"):
        _config(minimum_round_seconds=600, round_seconds=300)

    with pytest.raises(ValueError, match="lower it below"):
        _config(minimum_round_seconds=60)


def test_config_is_not_football_specific():
    """A sport with two tiers and eight slots configures fine - nothing in the
    config layer assumes football's roster or tier names."""
    hockey_ish = AuctionConfig(
        slots=8,
        tiers=(
            TierConfig(code="original_six", display_name="Original Six", starting_budget=500),
            TierConfig(code="expansion", display_name="Expansion", starting_budget=350),
        ),
        default_tier_code="expansion",
        round_seconds=240,
    )

    assert hockey_ish.total_period_seconds() == 8 * 240
    assert hockey_ish.tier("original_six").starting_budget == 500


def test_tiers_must_be_distinct_and_the_default_must_exist():
    with pytest.raises(ValueError, match="distinct starting budgets"):
        AuctionConfig(
            slots=4,
            tiers=(
                TierConfig(code="a", display_name="A", starting_budget=200),
                TierConfig(code="b", display_name="B", starting_budget=200),
            ),
            default_tier_code="a",
        )

    with pytest.raises(ValueError, match="default_tier_code"):
        _config(default_tier_code="nonexistent")
