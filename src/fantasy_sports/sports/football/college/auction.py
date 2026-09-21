"""
NCAAF's default Timed Auction Draft settings.

Only the *values* live here - the mechanics are entirely in
platform/auction/. This is the file another sport copies the shape of
(sports/hockey/auction.py, etc.) rather than the engine growing a football
branch.

Program-size tiers are college football's flavour of the platform's tier
concept: a blue-blood program recruits with more money than a Group of Five
one. The NFL module will map the same tier mechanic onto whatever its own
"program size" analogue turns out to be, without new engine code.
"""

from fantasy_sports.platform.auction.config import (
    MINIMUM_ROUND_SECONDS,
    AuctionConfig,
    TierConfig,
)

NCAAF_TIERS = (
    TierConfig(code="large", display_name="Large program", starting_budget=300, sort_order=1),
    TierConfig(code="moderate", display_name="Moderate program", starting_budget=225, sort_order=2),
    TierConfig(code="small", display_name="Small program", starting_budget=150, sort_order=3),
)

# 20 slots matches the league default roster (League.draft_rounds); the API
# overrides `slots` with the league's actual setting when a session is
# created, so this is a default, not a hardcode.
NCAAF_AUCTION_DEFAULTS = AuctionConfig(
    slots=20,
    tiers=NCAAF_TIERS,
    default_tier_code="moderate",
    round_seconds=MINIMUM_ROUND_SECONDS,
    min_bid=1,
)
