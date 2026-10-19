"""Features that depend on the kind of league. Only college leagues (College Football, College Basketball) have a
rankings poll and let teams set their own schedules; pro leagues (NFL, NHL...) do neither, for now."""

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import LeagueUniverse, Universe


def is_college(db: Session, league_id: int) -> bool:
    return db.query(LeagueUniverse).join(Universe, Universe.id == LeagueUniverse.universe_id).filter(
        LeagueUniverse.league_id == league_id, Universe.level == "college").count() > 0


def draft_state(db: Session, league_id: int) -> str:
    """not_started / in_progress / complete across snake, auction and recruiting drafts."""
    import datetime
    from fantasy_sports.platform.db.models.core import AuctionDraft, Draft, RecruitingDraft, DraftPick, League
    from fantasy_sports.platform import auto_schedule, draft_clock

    league = db.get(League, league_id)
    draft_type = getattr(league, "draft_type", "snake") if league else "snake"

    if draft_type == "snake":
        drafts = db.query(Draft).filter(Draft.league_id == league_id)
        if league:
            drafts = drafts.filter(Draft.season_number == league.season_number)   # a rolled-over league starts fresh
        drafts = drafts.all()
        for d in drafts:
            if d.status == "in_progress":
                open_pick = db.query(DraftPick.id).filter(
                    DraftPick.draft_id == d.id, DraftPick.draftable_asset_id.is_(None)
                ).first()
                if open_pick is None and db.query(DraftPick.id).filter(DraftPick.draft_id == d.id).first() is not None:
                    # Every pick has been made! Close the draft now.
                    d.status = "complete"
                    if not d.completed_at:
                        d.completed_at = datetime.datetime.utcnow()
                    draft_clock.clear(d)
                    if league and not league.is_mock_draft:      # a mock draft has no season to roll over or schedule
                        from fantasy_sports.platform import season_rollover
                        season_rollover.after_draft_complete(db, league)
                        auto_schedule.ensure_schedule(db, league)
                    try:
                        db.commit()
                    except Exception:
                        pass
        # Close any errant recruiting or auction drafts for this snake league
        for ad in db.query(AuctionDraft).filter(AuctionDraft.league_id == league_id, AuctionDraft.status != "closed").all():
            ad.status = "closed"
        for rd in db.query(RecruitingDraft).filter(RecruitingDraft.league_id == league_id, RecruitingDraft.status != "closed").all():
            rd.status = "closed"
        try:
            db.commit()
        except Exception:
            pass

        statuses = [d.status for d in drafts]
        if not statuses:
            return "not_started"
        if all(s in ("complete", "closed") for s in statuses):
            # Between the drafts of an offseason (keepers still to pick, or a refill draft after the rookie draft).
            return "not_started" if league and league.offseason else "complete"
        return "in_progress"
    else:
        statuses = [d.status for d in db.query(AuctionDraft).filter(AuctionDraft.league_id == league_id)]
        statuses += [d.status for d in db.query(RecruitingDraft).filter(RecruitingDraft.league_id == league_id)]
        if not statuses:
            return "not_started"
        return "complete" if all(s in ("complete", "closed") for s in statuses) else "in_progress"


def roster_spots(db: Session, league_id: int) -> int:
    """How many players a team's roster holds: every lineup slot (starters and bench). 0 if the league has no slots."""
    from fantasy_sports.platform import lineups
    try:
        return sum(s.slot_count for s in lineups.league_slots(db, league_id))
    except ValueError:
        return 0


def roster_limit(db: Session, league) -> int:
    """The most players a team can hold. Outside college leagues it is the number of lineup slots (24 for hockey), which
    wins over the league's stored draft_rounds so a league created under an older roster size follows the current one.
    College leagues keep draft_rounds (they may recruit fewer and fill through free agency)."""
    if not is_college(db, league.id):
        spots = roster_spots(db, league.id)
        if spots:
            if league.draft_rounds != spots:
                league.draft_rounds = spots
            return spots
    return league.draft_rounds
