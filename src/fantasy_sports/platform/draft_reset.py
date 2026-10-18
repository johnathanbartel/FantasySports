"""Draft reset: undo a draft back to the start or back to a chosen point, for all three draft types.

  * snake      keep = how many picks stay (0 = from pick 1). The later picks go back on the board.
  * auction    keep = how many rounds stay. Later rounds, their bids and their sales are undone and budgets refunded.
  * recruiting keep = how many periods stay. Later awards, charges and results are undone; the draft reopens and the next
               period starts at once. Standing bids stay unless the whole draft is reset.

Who can do it:
  * While the draft is running (snake or auction in progress, recruiting open) the commissioner resets it alone.
  * Once it is complete, leagues.draft_reset_policy decides: 'unanimous' (default) puts it to every person-managed team (one
    rejection ends it, like platform/setting_changes.py), 'commissioner' lets the commissioner do it alone, 'off' refuses.
  * A completed draft is only reset while nothing has been built on it: no free-agency or waiver moves by people, no executed
    trades, no scored week or played game. Drafts of an offseason (season 2+, rookie/refill/combined) aren't reset once complete.

What a reset touches is only what the draft wrote: the picks / rounds / awards, the roster entries they created (and the
lineup and hockey-order rows for those players), auction budgets, and the AI's required roster fill that follows a draft.
Pick order and the owner of every slot (traded picks included), league settings and teams are never changed. Does not commit.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from fantasy_sports.platform import commissioner, draft_clock, notifications, pecking_order, setting_changes
from fantasy_sports.platform.db.models.core import (
    AuctionBid, AuctionDraft, AuctionRound, AuctionTeamBudget, Draft, DraftPick, DraftResetRequest, DraftResetVote,
    FreeAgencyTransaction, League, LeagueSeries, LineupEntry, RecruitingAward, RecruitingBid, RecruitingBidResult,
    RecruitingCharge, RecruitingDraft, RecruitingResultAck, RosterEntry, SeriesDailyGame, Team, Trade, WaiverClaim,
    WeeklyMatchup, WeeklyTeamScore,
)

log = logging.getLogger(__name__)

KINDS = ("snake", "auction", "recruiting")
POLICIES = ("unanimous", "commissioner", "off")
AI_FILL = "ai_fill"      # platform/free_agency.AI_FILL


class ResetError(ValueError):
    """A refused reset; the message is meant for the user."""


# ---- finding and describing a draft ---------------------------------------------------------------------------------
def get_draft(db: Session, league_id: int, kind: str, draft_id: int):
    model = {"snake": Draft, "auction": AuctionDraft, "recruiting": RecruitingDraft}.get(kind)
    if model is None:
        raise ResetError(f"Draft kind must be one of {', '.join(KINDS)}")
    draft = db.get(model, draft_id)
    if draft is None or draft.league_id != league_id:
        raise ResetError("Draft not found in this league")
    return draft


def is_complete(kind: str, draft) -> bool:
    return draft.status == {"snake": "complete", "auction": "complete", "recruiting": "closed"}[kind]


def is_running(kind: str, draft) -> bool:
    return draft.status == {"snake": "in_progress", "auction": "in_progress", "recruiting": "open"}[kind]


def done_units(db: Session, kind: str, draft) -> int:
    """Picks made (snake), rounds held (auction) or periods processed (recruiting): the most a reset can take back."""
    if kind == "snake":
        return db.query(DraftPick).filter(DraftPick.draft_id == draft.id, DraftPick.draftable_asset_id.isnot(None)).count()
    if kind == "auction":
        return db.query(AuctionRound).filter_by(auction_draft_id=draft.id).count()
    return draft.periods_processed


def total_units(db: Session, kind: str, draft) -> int:
    if kind == "snake":
        return db.query(DraftPick).filter_by(draft_id=draft.id).count()
    if kind == "auction":
        return done_units(db, kind, draft)
    return draft.total_periods


def blocking_activity(db: Session, league: League) -> str | None:
    """Why a completed draft can't be reset any more: something has been built on its rosters. None if nothing has."""
    team_ids = [t.id for t in db.query(Team.id).filter_by(league_id=league.id)] or [0]
    if db.query(FreeAgencyTransaction).filter(FreeAgencyTransaction.league_id == league.id,
                                              FreeAgencyTransaction.reason != AI_FILL).first():
        return "free-agency moves have been made since the draft"
    if db.query(WaiverClaim).filter_by(league_id=league.id).first():
        return "waiver claims have been made since the draft"
    if db.query(Trade).filter(Trade.league_id == league.id, Trade.status.in_(("accepted", "in_review"))).first():
        return "a trade has been made since the draft"
    if (db.query(WeeklyMatchup).filter(WeeklyMatchup.league_id == league.id, WeeklyMatchup.winner_team_id.isnot(None)).first()
            or db.query(WeeklyTeamScore).filter(WeeklyTeamScore.team_id.in_(team_ids)).first()):
        return "games have been scored since the draft"
    if (db.query(SeriesDailyGame).join(LeagueSeries, SeriesDailyGame.series_id == LeagueSeries.id)
            .filter(LeagueSeries.league_id == league.id, SeriesDailyGame.status != "SCHEDULED").first()):
        return "games have been played since the draft"
    return None


def mode_for(league: League, kind: str, draft) -> str:
    """How a reset of this draft goes: 'now' (the commissioner alone), 'vote' (every manager must approve) or 'off'."""
    if not is_complete(kind, draft):
        return "now"
    return {"unanimous": "vote", "commissioner": "now", "off": "off"}[league.draft_reset_policy]


def describe(db: Session, league: League, kind: str, draft) -> dict:
    """What the Reset button needs: how far the draft is, how a reset would go, and why it can't if it can't."""
    done, mode, reason = done_units(db, kind, draft), mode_for(league, kind, draft), None
    if done == 0:
        reason = "Nothing has been drafted yet"
    elif is_complete(kind, draft):
        if mode == "off":
            reason = "This league doesn't allow a completed draft to be reset"
        elif kind == "snake" and (draft.season_number > 1 or draft.kind != "standard"):
            reason = "An offseason draft can't be reset once it is complete"
        else:
            reason = (f"A completed draft can't be reset: {why}" if (why := blocking_activity(db, league)) else None)
    elif not is_running(kind, draft):
        reason = "This draft isn't running"
    return {"kind": kind, "draft_id": draft.id, "status": draft.status, "done": done, "total": total_units(db, kind, draft),
            "mode": mode, "can_reset": reason is None, "reason": reason}


def validate(db: Session, league: League, kind: str, draft, keep: int) -> None:
    info = describe(db, league, kind, draft)
    if not info["can_reset"]:
        raise ResetError(info["reason"])
    if not 0 <= keep < info["done"]:
        raise ResetError(f"Keep between 0 and {info['done'] - 1} (only {info['done']} done so far)")


# ---- the reset itself -----------------------------------------------------------------------------------------------
def _remove_player(db: Session, team_id: int, asset_id: int, acquired_via: str) -> None:
    """Takes a player the draft added back off the roster, his lineup spots and his hockey line order."""
    team = db.get(Team, team_id)
    if team is not None:
        pecking_order.unrank_player(db, team, asset_id)
    db.query(RosterEntry).filter_by(team_id=team_id, draftable_asset_id=asset_id, acquired_via=acquired_via).delete()
    db.query(LineupEntry).filter_by(team_id=team_id, draftable_asset_id=asset_id).delete()
    db.flush()


def _undo_ai_fill(db: Session, league: League) -> None:
    """The AI's required roster fill that follows a draft (platform/free_agency.ai_fill_rosters) goes with it."""
    rows = db.query(FreeAgencyTransaction).filter_by(league_id=league.id, reason=AI_FILL).all()
    for row in rows:
        if row.kind == "add":
            _remove_player(db, row.team_id, row.draftable_asset_id, "free_agency")
        db.delete(row)
    db.flush()


def _reset_snake(db: Session, league: League, draft: Draft, keep: int, now: datetime.datetime) -> None:
    if is_complete("snake", draft):
        _undo_ai_fill(db, league)
    for pick in db.query(DraftPick).filter(DraftPick.draft_id == draft.id, DraftPick.pick_number > keep,
                                           DraftPick.draftable_asset_id.isnot(None)):
        _remove_player(db, pick.team_id, pick.draftable_asset_id, "draft")
        pick.draftable_asset_id, pick.picked_at, pick.is_autopick = None, None, False
    draft.status, draft.completed_at = "in_progress", None
    draft_clock.clear(draft)
    if draft_clock.is_timed(draft):
        # Back on the clock but frozen at a full pick, so nobody is caught out: the commissioner resumes when ready.
        draft.paused, draft.paused_at, draft.remaining_seconds, draft.pick_started_at = True, now, draft.pick_seconds, now
    else:
        draft_clock.begin_pick(draft, now)
    db.flush()


def _reset_auction(db: Session, league: League, draft: AuctionDraft, keep: int) -> None:
    cut = (db.query(AuctionRound).filter(AuctionRound.auction_draft_id == draft.id, AuctionRound.round_number > keep)
           .order_by(AuctionRound.round_number).all())
    budgets = {b.team_id: b for b in db.query(AuctionTeamBudget).filter_by(auction_draft_id=draft.id)}
    for rnd in cut:
        if rnd.status == "resolved" and rnd.winning_team_id is not None:
            _remove_player(db, rnd.winning_team_id, rnd.draftable_asset_id, "auction")
            if (b := budgets.get(rnd.winning_team_id)) is not None:
                b.remaining_budget += rnd.winning_bid_amount or 0
                b.assets_won = max(0, b.assets_won - 1)
        db.query(AuctionBid).filter_by(auction_round_id=rnd.id).delete()
        db.delete(rnd)
    db.flush()
    # The first undone round was started by the team now on turn to nominate.
    order = list(draft.nomination_order)
    draft.next_nominator_index = order.index(cut[0].nominated_by_team_id) if cut[0].nominated_by_team_id in order else 0
    draft.status, draft.completed_at = "in_progress", None
    db.flush()


def _reset_recruiting(db: Session, league: League, draft: RecruitingDraft, keep: int, now: datetime.datetime) -> None:
    if is_complete("recruiting", draft):
        _undo_ai_fill(db, league)
    cut = db.query(RecruitingAward).filter(RecruitingAward.draft_id == draft.id, RecruitingAward.period_number > keep).all()
    for award in cut:
        _remove_player(db, award.team_id, award.draftable_asset_id, "recruiting")
        db.delete(award)
    for model in (RecruitingCharge, RecruitingBidResult, RecruitingResultAck):
        db.query(model).filter(model.draft_id == draft.id, model.period_number > keep).delete()
    if keep == 0:
        db.query(RecruitingBid).filter_by(draft_id=draft.id).delete()
    draft.status, draft.ended_at = "open", None
    draft.total_periods, draft.periods_processed = league.draft_rounds, keep
    # Period keep + 1 opens now: the schedule is counted back from here.
    draft.started_at = now - datetime.timedelta(seconds=(draft.period_seconds + draft.buffer_seconds) * keep)
    db.flush()


def apply(db: Session, league: League, kind: str, draft, keep: int, actor_id: int | None,
          now: datetime.datetime | None = None) -> None:
    """Does the reset. Checks everything again (a vote can sit open while things change)."""
    now = now or datetime.datetime.utcnow()
    validate(db, league, kind, draft, keep)
    done = done_units(db, kind, draft)
    if kind == "snake":
        _reset_snake(db, league, draft, keep, now)
    elif kind == "auction":
        _reset_auction(db, league, draft, keep)
    else:
        _reset_recruiting(db, league, draft, keep, now)
    unit = {"snake": "pick", "auction": "round", "recruiting": "period"}[kind]
    where = f"back to the start" if keep == 0 else f"back to after {unit} {keep}"
    if actor_id is not None:
        commissioner.log_action(db, league.id, actor_id, "draft_reset", None,
                                {"kind": kind, "draft_id": draft.id, "keep": keep, "was_done": done}, now)
    notifications.queue_message(
        db, league, "draft_reset", f"{league.name}: the {kind} draft was reset",
        f"The {kind} draft in {league.name} was reset {where} ({done - keep} {unit}s undone). "
        + ("It is paused until the commissioner resumes it." if kind == "snake" and draft_clock.is_timed(draft)
           else "Check the draft page for where it stands."), now)
    log.info("league %s %s draft %s reset to keep=%s (was %s) by user %s", league.id, kind, draft.id, keep, done, actor_id)


# ---- requests and votes ---------------------------------------------------------------------------------------------
def open_request(db: Session, league_id: int) -> DraftResetRequest | None:
    return db.query(DraftResetRequest).filter_by(league_id=league_id, status="voting").first()


def request_reset(db: Session, league: League, kind: str, draft, keep: int, user_id: int,
                  now: datetime.datetime | None = None) -> DraftResetRequest:
    """A commissioner asks for a reset. It happens at once while the draft runs (or under the 'commissioner' policy), else it
    goes to a vote of every person-managed team; the commissioner's own team approves by asking."""
    now = now or datetime.datetime.utcnow()
    if not commissioner.is_commissioner(db, league, user_id):
        raise PermissionError("Only the commissioner can reset a draft")
    validate(db, league, kind, draft, keep)
    if open_request(db, league.id) is not None:
        raise ResetError("A vote to reset a draft is already open")
    req = DraftResetRequest(league_id=league.id, draft_kind=kind, draft_id=draft.id, keep=keep, status="voting",
                            proposed_by_user_id=user_id, created_at=now)
    db.add(req)
    db.flush()
    if mode_for(league, kind, draft) == "now":
        apply(db, league, kind, draft, keep, user_id, now)
        req.status, req.resolved_at = "applied", now
        db.flush()
        return req
    for team in setting_changes.voters(db, league.id):
        if team.owner_user_id == user_id:
            db.add(DraftResetVote(request_id=req.id, team_id=team.id, user_id=user_id, vote="approve", created_at=now))
    db.flush()
    notifications.queue_message(
        db, league, "draft_reset_vote", f"{league.name}: vote on resetting the draft",
        f"The commissioner asked to reset the {kind} draft in {league.name}"
        + ("" if keep == 0 else f" back to {keep} done") + ". It only happens if every manager approves. Vote on the draft page.", now)
    _settle(db, league, req, now)
    return req


def vote(db: Session, req: DraftResetRequest, user_id: int, decision: str, now: datetime.datetime | None = None) -> DraftResetRequest:
    now = now or datetime.datetime.utcnow()
    if req.status != "voting":
        raise ResetError(f"This vote is closed ({req.status})")
    if decision not in ("approve", "reject"):
        raise ResetError("Vote must be 'approve' or 'reject'")
    league = db.get(League, req.league_id)
    team = next((t for t in setting_changes.voters(db, league.id) if t.owner_user_id == user_id), None)
    if team is None:
        raise PermissionError("Only a manager in this league can vote on this")
    existing = db.query(DraftResetVote).filter_by(request_id=req.id, team_id=team.id).first()
    if existing:
        existing.vote, existing.created_at = decision, now
    else:
        db.add(DraftResetVote(request_id=req.id, team_id=team.id, user_id=user_id, vote=decision, created_at=now))
    db.flush()
    _settle(db, league, req, now)
    return req


def cancel(db: Session, req: DraftResetRequest, user_id: int, now: datetime.datetime | None = None) -> DraftResetRequest:
    now = now or datetime.datetime.utcnow()
    league = db.get(League, req.league_id)
    if not commissioner.is_commissioner(db, league, user_id):
        raise PermissionError("Only the commissioner can withdraw a reset request")
    if req.status != "voting":
        raise ResetError(f"This vote is closed ({req.status})")
    req.status, req.resolved_at = "cancelled", now
    db.flush()
    return req


def _settle(db: Session, league: League, req: DraftResetRequest, now: datetime.datetime) -> None:
    """Applies the reset once every voter has approved; any rejection ends it. Voters are read fresh. If the reset is refused
    at that point (something was built on the draft while the vote was open) the request is marked failed."""
    votes = {v.team_id: v.vote for v in req.votes}
    teams = setting_changes.voters(db, league.id)
    if any(votes.get(t.id) == "reject" for t in teams):
        req.status, req.resolved_at = "rejected", now
    elif not teams or all(votes.get(t.id) == "approve" for t in teams):
        try:
            apply(db, league, req.draft_kind, get_draft(db, league.id, req.draft_kind, req.draft_id), req.keep,
                  req.proposed_by_user_id, now)
            req.status = "applied"
        except ResetError as e:
            req.status, req.failure_reason = "failed", str(e)[:300]
        req.resolved_at = now
    db.flush()
