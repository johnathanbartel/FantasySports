"""Platform lifecycle operations for leagues (deletion, cleanup)."""

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import (
    AuctionBid,
    AuctionDraft,
    AuctionRound,
    AuctionTeamBudget,
    BankedCredit,
    CommissionerAction,
    EmailOutbox,
    TeamAdjustment,
    WaiverOrderOverride,
    Conference,
    Division,
    Draft,
    DraftPick,
    FreeAgencyTransaction,
    League,
    KeeperCandidate,
    KeeperSubmission,
    LeagueCoCommissioner,
    LeagueSeason,
    LeagueSeries,
    LeagueUniverse,
    LineupEntry,
    MatchupRequest,
    RecruitingAward,
    RecruitingBid,
    RecruitingBidResult,
    RecruitingCharge,
    RecruitingDraft,
    RecruitingResultAck,
    RecruitingSettings,
    RecruitingThreshold,
    RosterEntry,
    ScheduleSettings,
    SeriesDailyGame,
    SimulatorEvent,
    SimulatorGame,
    SimulatorReveal,
    Team,
    TeamPeckingOrder,
    Trade,
    TradedPick,
    TradeItem,
    TradeVote,
    LeagueSettingChange,
    LeagueSettingVote,
    WeeklyContest,
    WeeklyContestEntry,
    WeeklyContestPairing,
    WeeklyContestSlot,
    WeeklyMatchup,
    WeeklyPlayerSalary,
    WeeklyRedraftSettings,
    WeeklyTeamScore,
)


def delete_league(db: Session, league_id: int) -> None:
    """Deletes a league and all dependent child records in strict reverse foreign key order.
    Does NOT commit the session so the caller controls transaction boundaries.
    """
    league = db.get(League, league_id)
    if league is None:
        raise ValueError(f"League {league_id} not found")

    team_ids = [r[0] for r in db.query(Team.id).filter(Team.league_id == league_id).all()]
    draft_ids = [r[0] for r in db.query(Draft.id).filter(Draft.league_id == league_id).all()]
    auction_draft_ids = [r[0] for r in db.query(AuctionDraft.id).filter(AuctionDraft.league_id == league_id).all()]
    recruiting_draft_ids = [r[0] for r in db.query(RecruitingDraft.id).filter(RecruitingDraft.league_id == league_id).all()]
    league_series_ids = [r[0] for r in db.query(LeagueSeries.id).filter(LeagueSeries.league_id == league_id).all()]
    daily_game_ids = (
        [r[0] for r in db.query(SeriesDailyGame.id).filter(SeriesDailyGame.series_id.in_(league_series_ids)).all()]
        if league_series_ids else []
    )
    simulator_game_ids = (
        [r[0] for r in db.query(SimulatorGame.id).filter(SimulatorGame.daily_game_id.in_(daily_game_ids)).all()]
        if daily_game_ids else []
    )
    auction_round_ids = (
        [r[0] for r in db.query(AuctionRound.id).filter(AuctionRound.auction_draft_id.in_(auction_draft_ids)).all()]
        if auction_draft_ids else []
    )
    weekly_contest_ids = [r[0] for r in db.query(WeeklyContest.id).filter(WeeklyContest.league_id == league_id).all()]
    contest_entry_ids = (
        [r[0] for r in db.query(WeeklyContestEntry.id).filter(WeeklyContestEntry.contest_id.in_(weekly_contest_ids)).all()]
        if weekly_contest_ids else []
    )

    # 1. Simulator events & view states
    if simulator_game_ids:
        db.query(SimulatorReveal).filter(SimulatorReveal.simulator_game_id.in_(simulator_game_ids)).delete(synchronize_session=False)
        db.query(SimulatorEvent).filter(SimulatorEvent.simulator_game_id.in_(simulator_game_ids)).delete(synchronize_session=False)
        db.query(SimulatorGame).filter(SimulatorGame.id.in_(simulator_game_ids)).delete(synchronize_session=False)

    # 2. Banking: Off-day banked credits (points to series_daily_games, leagues, teams)
    if team_ids:
        db.query(BankedCredit).filter(
            (BankedCredit.league_id == league_id) | (BankedCredit.team_id.in_(team_ids))
        ).delete(synchronize_session=False)
    else:
        db.query(BankedCredit).filter(BankedCredit.league_id == league_id).delete(synchronize_session=False)

    # 3. Daily games and series
    if league_series_ids:
        db.query(SeriesDailyGame).filter(SeriesDailyGame.series_id.in_(league_series_ids)).delete(synchronize_session=False)
        db.query(LeagueSeries).filter(LeagueSeries.id.in_(league_series_ids)).delete(synchronize_session=False)

    # 4. Weekly salary-cap redraft
    if contest_entry_ids:
        db.query(WeeklyContestSlot).filter(WeeklyContestSlot.entry_id.in_(contest_entry_ids)).delete(synchronize_session=False)
        db.query(WeeklyContestEntry).filter(WeeklyContestEntry.id.in_(contest_entry_ids)).delete(synchronize_session=False)
    if weekly_contest_ids:
        db.query(WeeklyContestPairing).filter(WeeklyContestPairing.contest_id.in_(weekly_contest_ids)).delete(synchronize_session=False)
        db.query(WeeklyPlayerSalary).filter(WeeklyPlayerSalary.contest_id.in_(weekly_contest_ids)).delete(synchronize_session=False)
        db.query(WeeklyContest).filter(WeeklyContest.id.in_(weekly_contest_ids)).delete(synchronize_session=False)
    db.query(WeeklyRedraftSettings).filter(WeeklyRedraftSettings.league_id == league_id).delete(synchronize_session=False)

    # 5. Timed auctions
    if auction_round_ids:
        db.query(AuctionBid).filter(AuctionBid.auction_round_id.in_(auction_round_ids)).delete(synchronize_session=False)
        db.query(AuctionRound).filter(AuctionRound.id.in_(auction_round_ids)).delete(synchronize_session=False)
    if auction_draft_ids:
        db.query(AuctionTeamBudget).filter(AuctionTeamBudget.auction_draft_id.in_(auction_draft_ids)).delete(synchronize_session=False)
        db.query(AuctionDraft).filter(AuctionDraft.id.in_(auction_draft_ids)).delete(synchronize_session=False)

    # 6. Recruiting draft
    if recruiting_draft_ids:
        db.query(RecruitingResultAck).filter(RecruitingResultAck.draft_id.in_(recruiting_draft_ids)).delete(synchronize_session=False)
        db.query(RecruitingBidResult).filter(RecruitingBidResult.draft_id.in_(recruiting_draft_ids)).delete(synchronize_session=False)
        db.query(RecruitingCharge).filter(RecruitingCharge.draft_id.in_(recruiting_draft_ids)).delete(synchronize_session=False)
        db.query(RecruitingAward).filter(RecruitingAward.draft_id.in_(recruiting_draft_ids)).delete(synchronize_session=False)
        db.query(RecruitingBid).filter(RecruitingBid.draft_id.in_(recruiting_draft_ids)).delete(synchronize_session=False)
        db.query(RecruitingThreshold).filter(RecruitingThreshold.draft_id.in_(recruiting_draft_ids)).delete(synchronize_session=False)
        db.query(RecruitingDraft).filter(RecruitingDraft.id.in_(recruiting_draft_ids)).delete(synchronize_session=False)
    db.query(RecruitingSettings).filter(RecruitingSettings.league_id == league_id).delete(synchronize_session=False)

    # 7. Snake drafts
    if draft_ids:
        db.query(DraftPick).filter(DraftPick.draft_id.in_(draft_ids)).delete(synchronize_session=False)
        db.query(Draft).filter(Draft.id.in_(draft_ids)).delete(synchronize_session=False)

    # 8. Matchups, requests, settings, transactions, trades
    db.query(WeeklyMatchup).filter(WeeklyMatchup.league_id == league_id).delete(synchronize_session=False)
    db.query(MatchupRequest).filter(MatchupRequest.league_id == league_id).delete(synchronize_session=False)
    db.query(ScheduleSettings).filter(ScheduleSettings.league_id == league_id).delete(synchronize_session=False)
    db.query(FreeAgencyTransaction).filter(FreeAgencyTransaction.league_id == league_id).delete(synchronize_session=False)
    trade_ids = [t.id for t in db.query(Trade).filter_by(league_id=league_id)]
    if trade_ids:
        db.query(TradeVote).filter(TradeVote.trade_id.in_(trade_ids)).delete(synchronize_session=False)
        db.query(TradeItem).filter(TradeItem.trade_id.in_(trade_ids)).delete(synchronize_session=False)
        db.query(Trade).filter(Trade.id.in_(trade_ids)).delete(synchronize_session=False)

    change_ids = [c.id for c in db.query(LeagueSettingChange).filter_by(league_id=league_id)]
    if change_ids:
        db.query(LeagueSettingVote).filter(LeagueSettingVote.change_id.in_(change_ids)).delete(synchronize_session=False)
        db.query(LeagueSettingChange).filter(LeagueSettingChange.id.in_(change_ids)).delete(synchronize_session=False)

    # Offline drafts (platform/offline_draft.py): picks, lots and claims point at teams, so they go first
    from fantasy_sports.platform.db.models.offline_draft import (
        OfflineAuctionLot, OfflineAuditLog, OfflineBidEvent, OfflineDraftPick, OfflineDraftSession, OfflineTeamClaim,
    )
    offline_ids = [s.id for s in db.query(OfflineDraftSession.id).filter_by(league_id=league_id)]
    if offline_ids:
        lot_ids = [r[0] for r in db.query(OfflineAuctionLot.id).filter(OfflineAuctionLot.session_id.in_(offline_ids))]
        if lot_ids:
            db.query(OfflineBidEvent).filter(OfflineBidEvent.lot_id.in_(lot_ids)).delete(synchronize_session=False)
        for model in (OfflineAuctionLot, OfflineDraftPick, OfflineTeamClaim, OfflineAuditLog):
            db.query(model).filter(model.session_id.in_(offline_ids)).delete(synchronize_session=False)
        db.query(OfflineDraftSession).filter(OfflineDraftSession.id.in_(offline_ids)).delete(synchronize_session=False)

    # Commissioner tools (platform/commissioner.py): these point at teams and the league, so they go before both
    for model in (CommissionerAction, TeamAdjustment, WaiverOrderOverride, LeagueCoCommissioner, EmailOutbox):
        db.query(model).filter(model.league_id == league_id).delete(synchronize_session=False)

    # 9. Team-level child records (the keeper rows and season archive point at teams, so they go first)
    db.query(TradedPick).filter(TradedPick.league_id == league_id).delete(synchronize_session=False)
    db.query(KeeperCandidate).filter(KeeperCandidate.league_id == league_id).delete(synchronize_session=False)
    db.query(KeeperSubmission).filter(KeeperSubmission.league_id == league_id).delete(synchronize_session=False)
    db.query(LeagueSeason).filter(LeagueSeason.league_id == league_id).delete(synchronize_session=False)
    if team_ids:
        db.query(TeamPeckingOrder).filter(TeamPeckingOrder.team_id.in_(team_ids)).delete(synchronize_session=False)
        db.query(LineupEntry).filter(LineupEntry.team_id.in_(team_ids)).delete(synchronize_session=False)
        db.query(RosterEntry).filter(RosterEntry.team_id.in_(team_ids)).delete(synchronize_session=False)
        db.query(WeeklyTeamScore).filter(WeeklyTeamScore.team_id.in_(team_ids)).delete(synchronize_session=False)
        db.query(Team).filter(Team.id.in_(team_ids)).delete(synchronize_session=False)

    # 10. Conferences, divisions, universes, and league
    db.query(Division).filter(Division.league_id == league_id).delete(synchronize_session=False)
    db.query(Conference).filter(Conference.league_id == league_id).delete(synchronize_session=False)
    db.query(LeagueUniverse).filter(LeagueUniverse.league_id == league_id).delete(synchronize_session=False)
    db.query(League).filter(League.id == league_id).delete(synchronize_session=False)
