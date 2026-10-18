"""Trade eligibility for FREE and PAID leagues (CFG-06) - the one place the rules live.

Every trade path asks this module before a trade is proposed and again immediately before it executes. It never trusts
the client: payment, elimination, ownership, votes and the commissioner's identity are all read from the database.

Rules (see the CFG-06 matrix):
  * Past the league's trade deadline, or with an asset its sender doesn't own: blocked.
  * FREE traditional redraft league: an eliminated, person-managed team is locked from every transaction (trades, signings,
    drops, waiver claims) - see elimination.transactions_locked(). Nothing else in this list applies to it.
  * Draft picks need pick trading on in the league (and the pick inside the tradeable horizon).
  * Eliminated team: current players alone can't be traded. It must receive net positive future draft value.
      - receives picks and sends none: net positive by definition;
      - receives picks and sends picks: needs a valuation, and none is approved - goes to a league vote until
        set_future_value_evaluator() installs one (the AI's pick valuation in pick_trading.py is not an approved policy).
  * PAID league: an eliminated team needs next season's dues secured; any team sending picks needs the dues secured for each
    season whose picks it sends. A season is never counted twice. Only confirmed ('paid') records count. AI-owned teams
    have nobody to pay, so they owe nothing (open policy question).
  * FREE league: future picks can't be traded at all (FREE_LEAGUE_PICK_TRADING_LOCKED).
  * AD_FREE league (FS-036): a person-managed team sending picks needs each pick season's platform fee (the deposit) secured,
    once per team per season; in a PAID + AD_FREE league that is on top of the dues. See platform_fees.py.
  * FREE league, eliminated team involved: majority league approval AND the commissioner's sign-off
    (ELIMINATED_FREE_LEAGUE_TRADE). Neither can stand in for the other. If the commissioner's own team is in the trade there is
    no sign-off: the league alone decides, at the higher commissioner-trade threshold (votes_needed).
  * Votes needed: teams // 2 + league.trade_vote_extra_votes (default 1); with the commissioner's team in the trade,
    + league.commissioner_trade_vote_extra_votes (default 2).
  * Eliminated team sending picks while no valuation policy is installed: not blocked - the league votes (majority only,
    no commissioner sign-off; in a FREE league the vote plus sign-off above applies). Dues rules still apply first.
  * Everything else follows the ordinary flow (trades.py): the counterparty accepts, or the league votes if an AI is involved.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy.orm import Session

from fantasy_sports.platform import dues, elimination, pick_trading, platform_fees
from fantasy_sports.platform.db.models.core import (
    League, PlatformSeasonFee, RosterEntry, SeasonDues, Team, Trade, TradeApproval, TradeVote,
)

log = logging.getLogger(__name__)

PickRef = tuple[int, int, int]

# Statuses
ELIGIBLE_FOR_REVIEW = "ELIGIBLE_FOR_REVIEW"
AWAITING_PAYMENT = "AWAITING_PAYMENT"
AWAITING_LEAGUE_VOTE = "AWAITING_LEAGUE_VOTE"
AWAITING_COMMISSIONER_APPROVAL = "AWAITING_COMMISSIONER_APPROVAL"
BLOCKED = "BLOCKED"
APPROVED_FOR_EXECUTION = "APPROVED_FOR_EXECUTION"
EXECUTED = "EXECUTED"
EXECUTABLE = (ELIGIBLE_FOR_REVIEW, APPROVED_FOR_EXECUTION)

# Approval requirements
ELIMINATED_FREE_LEAGUE_TRADE = "ELIMINATED_FREE_LEAGUE_TRADE"
STANDARD_TRADE_APPROVAL = "STANDARD_TRADE_APPROVAL"
ELIMINATED_PICK_TRADE_VOTE = "ELIMINATED_PICK_TRADE_VOTE"   # league majority only; no commissioner sign-off

# Net future draft value: a valuation of (incoming picks, outgoing picks, outgoing players) for one team; True = net positive.
FutureValueEvaluator = Callable[[Session, League, Team, list[PickRef], list[PickRef], list[int]], bool]
_future_value_evaluator: FutureValueEvaluator | None = None


def set_future_value_evaluator(fn: FutureValueEvaluator | None) -> None:
    """Integration point for the (not yet approved) future draft pick valuation policy."""
    global _future_value_evaluator
    _future_value_evaluator = fn


@dataclass
class EligibilityResult:
    status: str = ELIGIBLE_FOR_REVIEW
    approval_requirements: list[str] = field(default_factory=list)
    payment_requirements: list[dict] = field(default_factory=list)
    blocking_codes: list[str] = field(default_factory=list)
    explanations: list[str] = field(default_factory=list)
    missing_prerequisites: list[str] = field(default_factory=list)

    @property
    def executable(self) -> bool:
        return self.status in EXECUTABLE

    def block(self, code: str, why: str) -> None:
        self.blocking_codes.append(code)
        self.explanations.append(why)

    def message(self) -> str:
        return " ".join(self.explanations) or self.status

    def as_dict(self) -> dict:
        return {"status": self.status, "approval_requirements": self.approval_requirements,
                "payment_requirements": self.payment_requirements, "blocking_codes": self.blocking_codes,
                "explanations": self.explanations, "missing_prerequisites": self.missing_prerequisites}


def _ordinal_round(rnd: int) -> str:
    return f"round {rnd}"


def _evaluate_proposal(db: Session, league: League, a: Team, b: Team, a_players: list[int], b_players: list[int],
                       a_picks: list[PickRef], b_picks: list[PickRef], now: datetime.datetime) -> EligibilityResult:
    """The rules that depend only on the trade's contents. a sends a_*, b sends b_*."""
    res = EligibilityResult()

    if league.trade_deadline is not None and now > league.trade_deadline:
        res.block("TRADE_DEADLINE_PASSED", "This trade cannot proceed because the league trade deadline has passed.")
        res.status = BLOCKED
        return res

    # Free traditional redraft: an eliminated team makes no transactions at all.
    for team in (a, b):
        if elimination.transactions_locked(league, team):
            res.block("TRANSACTIONS_LOCKED", f"{team.name}: {elimination.TRANSACTIONS_LOCKED_MESSAGE}")
    if res.blocking_codes:
        res.status = BLOCKED
        return res

    # Ownership: every asset must belong to the franchise sending it.
    for team, players, picks in ((a, a_players, a_picks), (b, b_players, b_picks)):
        roster = {r.draftable_asset_id for r in db.query(RosterEntry).filter_by(team_id=team.id)}
        for aid in players:
            if aid not in roster:
                res.block("ASSET_NOT_OWNED", f"Player {aid} is not on {team.name}'s roster.")
        for season, rnd, original in picks:
            if pick_trading.current_owner(db, league.id, season, rnd, original) != team.id:
                res.block("ASSET_NOT_OWNED", f"{team.name} doesn't own the {_ordinal_round(rnd)} pick of season {season}.")
    if len(set(a_picks)) != len(a_picks) or len(set(b_picks)) != len(b_picks):
        res.block("DUPLICATE_ASSET", "A draft pick is listed twice.")

    # Future picks need the feature on, and the pick inside the horizon.
    if a_picks or b_picks:
        if not pick_trading.enabled(league):
            res.block("FUTURE_PICK_TRADING_DISABLED", "Draft pick trading is turned off in this league.")
        else:
            for season, rnd, original in [*a_picks, *b_picks]:
                why = pick_trading.pick_available(db, league, season, rnd, original)
                if why:
                    res.block("FUTURE_PICK_UNAVAILABLE", why)
        # FS-036: free leagues have no dues behind them, so they can't trade future picks at all.
        if league.league_type == "FREE":
            res.block("FREE_LEAGUE_PICK_TRADING_LOCKED", "Draft pick trading needs league dues. Free leagues can't trade picks.")
    if res.blocking_codes:
        res.status = BLOCKED
        return res

    has_picks = bool(a_picks or b_picks)
    sides = ((a, a_players, a_picks, b_picks), (b, b_players, b_picks, a_picks))
    eliminated = [t for t, *_ in sides if elimination.is_eliminated(league, t)]
    needs_vote = False       # an eliminated team sends picks and no approved valuation exists: the league decides

    # An eliminated team must come away with net positive future draft value.
    for team, players, out_picks, in_picks in sides:
        if team not in eliminated:
            continue
        if not in_picks:
            res.block("ELIMINATED_NO_FUTURE_VALUE",
                      "Eliminated teams cannot complete player-for-player trades without receiving net positive future "
                      f"draft value. {team.name} has been mathematically eliminated.")
        elif out_picks:
            positive = (_future_value_evaluator(db, league, team, list(in_picks), list(out_picks), list(players))
                        if _future_value_evaluator else None)
            if positive is None:
                needs_vote = True
                res.explanations.append("This trade sends future picks from an eliminated team, and no approved draft-pick "
                                        "valuation exists, so the league votes on it.")
            elif not positive:
                res.block("ELIMINATED_NO_FUTURE_VALUE",
                          f"{team.name} is eliminated and would not receive net positive future draft value.")
    if res.blocking_codes:
        res.status = BLOCKED
        return res

    # Dues in a paid league.
    if league.league_type == "PAID" and (has_picks or eliminated):
        needed: dict[int, set[int]] = {}
        for team, _players, out_picks, _in in sides:
            if team.is_ai_owned:
                continue
            seasons = {season for season, _r, _o in out_picks}
            if team in eliminated:
                seasons.add(league.season_number + 1)
            if seasons:
                needed[team.id] = seasons
        if needed and not league.annual_dues_cents:
            res.block("DUES_NOT_CONFIGURED", "This is a paid league but the annual dues amount hasn't been set.")
            res.status = BLOCKED
            return res
        names = {t.id: t.name for t in (a, b)}
        for team_id, seasons in needed.items():
            for season in sorted(seasons):
                if dues.is_secured(db, league, team_id, season):
                    continue
                remaining = league.annual_dues_cents - dues.secured_cents(db, team_id, season)
                is_elim_season = season == league.season_number + 1 and any(t.id == team_id for t in eliminated)
                if is_elim_season:
                    why = (f"Your team has been mathematically eliminated. Future-asset trading requires payment of next "
                           f"season's league dues ({season}) for {names[team_id]}.")
                else:
                    why = f"{names[team_id]} must secure {season} league dues before trading away a {season} draft pick."
                res.payment_requirements.append({"kind": "league_dues", "team_id": team_id, "team_name": names[team_id],
                                                 "season_number": season, "amount_cents": remaining})
                res.missing_prerequisites.append(f"{names[team_id]}: {season} dues")
                res.explanations.append(why)
        if not res.payment_requirements:
            for team_id, seasons in needed.items():
                if any(t.id == team_id for t in eliminated):
                    res.explanations.append(f"{names[team_id]}'s {league.season_number + 1} league dues are secured. "
                                            "You are eligible to submit future-asset trades.")

    # FS-036: in an AD_FREE league a person-managed team sending picks needs each pick season's platform fee (the deposit)
    # secured, once per team per season. This sits beside the dues gate: a PAID + AD_FREE league needs both.
    if league.platform_plan == platform_fees.AD_FREE and has_picks:
        if not league.platform_fee_cents:
            res.block("PLATFORM_FEE_NOT_CONFIGURED", "This league is ad-free but the platform fee hasn't been set.")
            res.status = BLOCKED
            return res
        names = {t.id: t.name for t in (a, b)}
        for team, _players, out_picks, _in in sides:
            if team.is_ai_owned:
                continue
            for season in sorted({season for season, _r, _o in out_picks}):
                if platform_fees.is_secured(db, league, team.id, season):
                    continue
                remaining = league.platform_fee_cents - platform_fees.secured_cents(db, team.id, season)
                res.payment_requirements.append({"kind": "platform_fee", "team_id": team.id, "team_name": team.name,
                                                 "season_number": season, "amount_cents": remaining})
                res.missing_prerequisites.append(f"{team.name}: {season} platform deposit")
                res.explanations.append(f"{names[team.id]} must pay the {season} platform deposit before trading a {season} "
                                        "draft pick. It's charged once per season and counts toward that season's fee.")

    if res.payment_requirements:
        res.status = AWAITING_PAYMENT
        return res

    # Approvals.
    if eliminated and league.league_type == "FREE":
        res.approval_requirements.append(ELIMINATED_FREE_LEAGUE_TRADE)
        if commissioner_involved(league, a, b):
            res.explanations.append("The commissioner's team is in this trade, so there is no commissioner sign-off. It needs "
                                    f"{votes_needed(db, league, a, b)} league votes instead.")
        else:
            res.explanations.append("This free-league trade requires majority league approval and commissioner sign-off.")
    elif needs_vote:
        res.approval_requirements.append(ELIMINATED_PICK_TRADE_VOTE)
    else:
        res.approval_requirements.append(STANDARD_TRADE_APPROVAL)
    res.status = ELIGIBLE_FOR_REVIEW
    return res


def _trade_parts(trade: Trade):
    p, c = trade.proposer_team, trade.counterparty_team
    players = lambda tid: [it.draftable_asset_id for it in trade.items if it.sender_team_id == tid and it.draftable_asset_id is not None]
    picks = lambda tid: [(it.pick_season, it.pick_round, it.pick_original_team_id) for it in trade.items
                         if it.sender_team_id == tid and it.draftable_asset_id is None]
    return p, c, players(p.id), players(c.id), picks(p.id), picks(c.id)


def evaluate_proposal(db: Session, league: League, proposer: Team, counterparty: Team, send_assets: list[int],
                      receive_assets: list[int], send_picks: list[PickRef], receive_picks: list[PickRef],
                      now: datetime.datetime | None = None) -> EligibilityResult:
    """Eligibility of a trade that hasn't been submitted yet (the manager sees this before submitting)."""
    return _evaluate_proposal(db, league, proposer, counterparty, list(send_assets), list(receive_assets),
                              list(send_picks), list(receive_picks), now or datetime.datetime.utcnow())


def commissioner_approval(db: Session, trade_id: int) -> TradeApproval | None:
    return db.query(TradeApproval).filter_by(trade_id=trade_id, approval_type=ELIMINATED_FREE_LEAGUE_TRADE).one_or_none()


def commissioner_involved(league: League, *teams: Team) -> bool:
    """True when the commissioner's own team is on either side of the trade."""
    return any(t.owner_user_id is not None and t.owner_user_id == league.commissioner_user_id for t in teams)


def votes_needed(db: Session, league: League, *teams: Team) -> int:
    """Approvals a trade needs from the league: half the teams plus the league's configured extra votes (default 1, so exactly
    half never passes), or plus the commissioner-trade extra (default 2) when the commissioner's team is in the trade."""
    total = db.query(Team).filter_by(league_id=league.id).count()
    extra = (league.commissioner_trade_vote_extra_votes if commissioner_involved(league, *teams)
             else league.trade_vote_extra_votes)
    return min(total, total // 2 + max(1, extra))


def majority_reached(db: Session, trade: Trade) -> bool:
    league = db.get(League, trade.league_id)
    needed = votes_needed(db, league, trade.proposer_team, trade.counterparty_team)
    return db.query(TradeVote).filter_by(trade_id=trade.id, vote="approve").count() >= needed


def evaluate_trade(db: Session, trade: Trade, now: datetime.datetime | None = None) -> EligibilityResult:
    """Eligibility of a submitted trade right now, including the votes and sign-offs recorded so far."""
    now = now or datetime.datetime.utcnow()
    if trade.status == "accepted":
        return EligibilityResult(status=EXECUTED, explanations=["This trade has been executed."])
    if trade.status not in ("proposed", "voting", "in_review"):
        res = EligibilityResult(status=BLOCKED)
        res.block("TRADE_CLOSED", f"This trade is {trade.status}." + (f" {trade.failure_reason}" if trade.failure_reason else ""))
        return res
    league = db.get(League, trade.league_id)
    if league is None:
        res = EligibilityResult(status=BLOCKED)
        res.block("LEAGUE_NOT_FOUND", "League not found.")
        return res
    p, c, p_players, c_players, p_picks, c_picks = _trade_parts(trade)
    res = _evaluate_proposal(db, league, p, c, p_players, c_players, p_picks, c_picks, now)
    if res.status != ELIGIBLE_FOR_REVIEW:
        return res
    if ELIMINATED_PICK_TRADE_VOTE in res.approval_requirements:
        if majority_reached(db, trade):
            res.status = APPROVED_FOR_EXECUTION
        else:
            res.missing_prerequisites.append("majority league approval")
            res.status = AWAITING_LEAGUE_VOTE
        return res
    if ELIMINATED_FREE_LEAGUE_TRADE not in res.approval_requirements:
        return res

    if commissioner_involved(league, p, c):
        # The commissioner can't sign off a trade their own team is in; the league's higher vote threshold replaces it.
        if majority_reached(db, trade):
            res.status = APPROVED_FOR_EXECUTION
        else:
            res.missing_prerequisites.append("majority league approval")
            res.status = AWAITING_LEAGUE_VOTE
        return res
    approval = commissioner_approval(db, trade.id)
    if approval is not None and approval.decision == "reject":
        res.status = BLOCKED
        res.block("COMMISSIONER_REJECTED", "The commissioner rejected this trade.")
        return res
    voted = majority_reached(db, trade)
    signed = approval is not None
    if not voted:
        res.missing_prerequisites.append("majority league approval")
    if not signed:
        res.missing_prerequisites.append("commissioner sign-off")
    res.status = (AWAITING_LEAGUE_VOTE if not voted else AWAITING_COMMISSIONER_APPROVAL if not signed
                  else APPROVED_FOR_EXECUTION)
    return res


def commit_backing_records(db: Session, trade: Trade) -> list:
    """FS-036: marks every paid dues and platform-fee record that backed this trade as committed to it, so it can't be refunded
    afterwards. Backing = a person-managed sender's pick seasons (dues in a PAID league, platform fee in an AD_FREE league) and,
    in a PAID league, an eliminated team's next-season dues. Only records with no earlier trade are touched: the first trade
    that used a record keeps it. Returns the rows marked; the caller flushes."""
    league = db.get(League, trade.league_id)
    p, c, _pp, _cp, p_picks, c_picks = _trade_parts(trade)
    marked = []
    for team, picks in ((p, p_picks), (c, c_picks)):
        if team.is_ai_owned:
            continue
        seasons = {season for season, _r, _o in picks}
        dues_seasons = set(seasons)
        if league.league_type == "PAID" and elimination.is_eliminated(league, team):
            dues_seasons.add(league.season_number + 1)
        if league.league_type == "PAID":
            for season in dues_seasons:
                marked += [r for r in db.query(SeasonDues).filter_by(team_id=team.id, season_number=season, status="paid")
                           if r.committed_by_trade_id is None]
        if league.platform_plan == platform_fees.AD_FREE:
            for season in seasons:
                marked += [r for r in db.query(PlatformSeasonFee).filter_by(team_id=team.id, season_number=season, status="paid")
                           if r.committed_by_trade_id is None]
    for row in marked:
        row.committed_by_trade_id = trade.id
    return marked


def validate_trade_eligibility(db: Session, trade_id: int, league_id: int, acting_user_id: int | None,
                               now: datetime.datetime | None = None) -> EligibilityResult:
    """The authoritative check. Only the commissioner or a manager in the league may ask about a trade."""
    trade = db.get(Trade, trade_id)
    league = db.get(League, league_id)
    res = EligibilityResult(status=BLOCKED)
    if trade is None or league is None or trade.league_id != league_id:
        res.block("TRADE_NOT_FOUND", "Trade not found in this league.")
        return res
    member = acting_user_id is not None and (
        league.commissioner_user_id == acting_user_id
        or db.query(Team).filter_by(league_id=league_id, owner_user_id=acting_user_id).first() is not None)
    if not member:
        res.block("NOT_AUTHORIZED", "You are not a member of this league.")
        return res
    return evaluate_trade(db, trade, now)
