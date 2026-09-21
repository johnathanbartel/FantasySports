"""
Instant simulation of a whole recruiting draft (commissioner mode).

It is NOT a shortcut around the rules: every week runs through the same engine
a live draft uses - the AI teams place and raise real bids (recruiting_ai.tick),
the week's bids are processed exactly as at a real week's end (thresholds,
highest bidder, everyone charged their bid, results snapshots, roster entries),
and the final week settles what's left at the deadline. The only thing skipped
is the clock: the time left in each week, and the buffer after it, are
fast-forwarded (the draft's start time is shifted back by that much so its
timeline stays consistent).

Only for a league with no human-owned teams - a person's bids can't be
simulated for them.
"""

from __future__ import annotations

import datetime
import random

from sqlalchemy.orm import Session

from fantasy_sports.platform.ai import recruiting_ai
from fantasy_sports.platform.db.models.core import RecruitingDraft, Team
from fantasy_sports.platform.recruiting import engine

TICKS_PER_WEEK = 8


def simulate_draft(
    db: Session, league_id: int, rng: random.Random | None = None, ticks_per_week: int = TICKS_PER_WEEK,
) -> RecruitingDraft:
    if db.query(Team).filter(Team.league_id == league_id, Team.is_ai_owned.is_(False)).count():
        raise ValueError("This league has human-owned teams - only an all-AI league can be simulated")
    draft = engine.get_draft(db, league_id)
    if draft is None:
        draft = engine.open_draft(db, league_id)
    rng = rng or random.Random()

    while draft.status == "open":
        now = datetime.datetime.utcnow()
        if engine.in_buffer(draft, now):
            # skip the rest of the buffer: pull the whole timeline earlier
            draft.started_at -= engine.period_start(draft, draft.periods_processed + 1) - now
        for _ in range(ticks_per_week):
            recruiting_ai.tick(db, draft.id, intensity=1.0, rng=rng)
        engine.process_period_now(db, draft.id)
        db.flush()
    return draft
