"""Offline, in-memory smoke test for waivers (platform/waivers.py): the 24-hour hold after a drop, claims resolved in
waiver order, the order itself (reverse goal differential once a week is fully complete, reverse first-round draft
order before that), the move to the back after a won claim, and the weekly reset at midnight Pacific.

Run from the repository root:

    .venv-api/Scripts/python.exe scripts/seed_waiver_smoke_test.py
"""

import datetime
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fantasy_sports.platform import free_agency, waivers
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    Draft, DraftPick, DraftableAsset, League, LeagueSeries, LeagueUniverse, Player, RealTeam, RosterEntry,
    SeriesDailyGame, Team, Universe, User, WaiverClaim,
)

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
D = datetime.datetime


def check(cond: bool, label: str) -> None:
    print(("ok    " if cond else "FAIL  ") + label)
    if not cond:
        raise SystemExit(1)


def seed(db):
    nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL")
    commissioner = User(email="waiver-commissioner@example.com", display_name="Commissioner")
    db.add_all([nhl, commissioner])
    db.flush()
    league = League(name="Waiver League", commissioner_user_id=commissioner.id, size=4, draft_rounds=3,
                    draft_type="snake", series_length_days=7)
    db.add(league)
    db.flush()
    db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id, defense_mode="group"))
    teams = [Team(league_id=league.id, name=f"Team {i}", is_ai_owned=True, program_size="small") for i in range(1, 5)]
    school = RealTeam(universe_id=nhl.id, name="Boston", external_ref="BOS")
    db.add_all(teams + [school])
    db.flush()
    assets = []
    for i in range(1, 9):
        player = Player(universe_id=nhl.id, real_team_id=school.id, full_name=f"Skater {i}", position="C")
        db.add(player)
        db.flush()
        asset = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=player.id)
        db.add(asset)
        assets.append(asset)
    db.flush()
    draft = Draft(league_id=league.id, status="complete")
    db.add(draft)
    db.flush()
    for n, team in enumerate(teams, start=1):                              # round 1 order: team 1 first ... team 4 last
        db.add(DraftPick(draft_id=draft.id, round=1, pick_number=n, team_id=team.id))
    db.add(RosterEntry(team_id=teams[0].id, draftable_asset_id=assets[0].id, acquired_via="draft"))
    db.commit()
    return league.id, [t.id for t in teams], [a.id for a in assets]


def add_week(db, league_id, teams, sunday):
    """One fully final week: team 1 beat team 2 5-1, team 4 beat team 3 3-2. Goal differential: +4, -4, -1, +1."""
    for (home, away, hs, as_) in ((teams[0], teams[1], 5, 1), (teams[3], teams[2], 3, 2)):
        series = LeagueSeries(league_id=league_id, team_a_id=home, team_b_id=away, start_date=sunday,
                              end_date=sunday + datetime.timedelta(days=6), total_days=7, status="FINAL")
        db.add(series)
        db.flush()
        db.add(SeriesDailyGame(series_id=series.id, calendar_date=sunday + datetime.timedelta(days=2), home_team_id=home,
                               away_team_id=away, home_score=hs, away_score=as_, status="FINAL"))
    db.commit()


def main() -> None:
    Base.metadata.create_all(bind=engine)
    with Session() as db:
        league_id, t, a = seed(db)
        team = lambda i: db.get(Team, t[i])    # noqa: E731

        # ---- the weekly reset: Sunday 00:00 Pacific
        wed = D(2026, 10, 14, 15, 0)
        reset = waivers.last_reset(wed)
        local = waivers._to_local(reset)
        check(local.weekday() == 6 and local.hour == 0 and local.minute == 0 and local.date() == datetime.date(2026, 10, 11),
              f"reset is Sunday 00:00 Pacific ({local})")
        check(waivers._to_local(waivers.next_reset(wed)).date() == datetime.date(2026, 10, 18), "next reset is the following Sunday")
        just_before = waivers._to_utc(D(2026, 10, 18, 0, 0)) - datetime.timedelta(minutes=1)
        check(waivers.last_reset(just_before) == reset, "Saturday 23:59 Pacific is still last week's order")
        check(waivers.last_reset(just_before + datetime.timedelta(minutes=2)) == waivers.next_reset(wed), "midnight rolls it over")

        # ---- order before any week is complete: reverse of the first round of the draft
        order = waivers.waiver_order(db, league_id, D(2026, 10, 10, 20, 0))
        check(order.basis == waivers.BASIS_DRAFT and order.teams == [t[3], t[2], t[1], t[0]], "no complete week: reverse draft order")

        # ---- a drop goes on waivers for 24 hours
        t0 = D(2026, 10, 14, 12, 0)
        free_agency.drop_player(db, team(0), a[0], free_agency.USER, t0)
        db.commit()
        check(waivers.waiver_until(db, league_id, t0 + datetime.timedelta(hours=1)) == {a[0]: t0 + datetime.timedelta(hours=24)},
              "dropped player is on waivers until drop + 24h")
        try:
            free_agency.add_player(db, team(1), a[0], free_agency.USER, t0 + datetime.timedelta(hours=1))
            check(False, "signing a waiver player is refused")
        except ValueError as e:
            check("waivers" in str(e), "signing a waiver player is refused")
        check(waivers.waiver_until(db, league_id, t0 + datetime.timedelta(hours=24)) == {}, "off waivers after 24h")

        # ---- claims go to the team first in the waiver order (draft fallback: team 3 ahead of team 2)
        waivers.place_claim(db, team(1), a[0], t0 + datetime.timedelta(hours=2))
        waivers.place_claim(db, team(2), a[0], t0 + datetime.timedelta(hours=3))
        try:
            waivers.place_claim(db, team(2), a[0], t0 + datetime.timedelta(hours=3))
            check(False, "a duplicate claim is refused")
        except ValueError:
            check(True, "a duplicate claim is refused")
        db.commit()
        check(waivers.process(db, league_id, t0 + datetime.timedelta(hours=23)) == 0, "nothing resolves before 24h")
        check(waivers.process(db, league_id, t0 + datetime.timedelta(hours=25)) == 1, "claim resolves after 24h")
        db.commit()
        claims = {c.team_id: c for c in db.query(WaiverClaim)}
        check(claims[t[2]].status == "won" and claims[t[1]].status == "lost", "team 3 (first in order) won, team 2 lost")
        check(db.query(RosterEntry).filter_by(team_id=t[2], draftable_asset_id=a[0]).count() == 1, "winner has the player")
        check(waivers.process(db, league_id, t0 + datetime.timedelta(hours=30)) == 0, "processing again changes nothing")
        after = waivers.waiver_order(db, league_id, t0 + datetime.timedelta(hours=26))
        check(after.teams == [t[3], t[1], t[0], t[2]] and after.moved_back == [t[2]], "winner moves to the back of the order")

        # ---- a drop nobody claims becomes a free agent
        t1 = D(2026, 10, 14, 18, 0)
        free_agency.drop_player(db, team(2), a[0], free_agency.USER, t1)
        db.commit()
        check(waivers.process(db, league_id, t1 + datetime.timedelta(hours=25)) == 0, "unclaimed player clears")
        free_agency.add_player(db, team(1), a[0], free_agency.USER, t1 + datetime.timedelta(hours=25))
        check(db.query(RosterEntry).filter_by(team_id=t[1], draftable_asset_id=a[0]).count() == 1, "a cleared player can be signed")

        # ---- a full week of games: order becomes reverse goal differential (+4, -4, -1, +1 -> team 2, 3, 4, 1)
        add_week(db, league_id, t, datetime.date(2026, 10, 4))
        order = waivers.waiver_order(db, league_id, D(2026, 10, 14, 15, 0))
        check(order.basis == waivers.BASIS_GOAL_DIFF and order.teams == [t[1], t[2], t[3], t[0]],
              f"complete week: worst goal differential first ({order.teams})")
        check(order.goal_diff == {t[0]: 4, t[1]: -4, t[2]: -1, t[3]: 1}, "goal differentials come from the week's games")
        before_reset = waivers.waiver_order(db, league_id, D(2026, 10, 10, 20, 0))
        check(before_reset.basis == waivers.BASIS_DRAFT, "the order only changes at the reset, not when the week ends")

        # ---- an incomplete week is not used
        series = LeagueSeries(league_id=league_id, team_a_id=t[0], team_b_id=t[2], start_date=datetime.date(2026, 10, 11),
                              end_date=datetime.date(2026, 10, 17), total_days=7)
        db.add(series)
        db.flush()
        db.add(SeriesDailyGame(series_id=series.id, calendar_date=datetime.date(2026, 10, 12), home_team_id=t[0],
                               away_team_id=t[2], home_score=9, away_score=0, status="SCHEDULED"))
        db.commit()
        order = waivers.waiver_order(db, league_id, D(2026, 10, 19, 15, 0))
        check(order.basis == waivers.BASIS_GOAL_DIFF and order.as_of == datetime.date(2026, 10, 10),
              "a week with an unfinished game is skipped for the last complete one")

        # ---- post-draft waivers: off by default, then every undrafted player is held for 24h after the draft ends
        league = db.get(League, league_id)
        draft = db.query(Draft).filter_by(league_id=league_id).first()
        ended = D(2026, 11, 1, 12, 0)
        draft.completed_at = ended
        db.commit()
        rostered = free_agency.rostered_ids(db, league_id)
        free = [x for x in a if x not in rostered]
        check(league.post_draft_waivers is False, "post-draft waivers default to off")
        check(waivers.waiver_until(db, league_id, ended + datetime.timedelta(hours=1)) == {}, "off: undrafted players are free agents")
        league.post_draft_waivers = True
        db.commit()
        until = waivers.waiver_until(db, league_id, ended + datetime.timedelta(hours=1))
        check(set(until) == set(free) and set(until.values()) == {ended + datetime.timedelta(hours=24)},
              "on: every undrafted player is on waivers until draft end + 24h")
        check(waivers.waiver_until(db, league_id, ended + datetime.timedelta(hours=24)) == {}, "post-draft waivers end after 24h")
        try:
            free_agency.add_player(db, team(1), free[0], free_agency.USER, ended + datetime.timedelta(hours=1))
            check(False, "signing an undrafted player is refused during the window")
        except ValueError as e:
            check("waivers" in str(e), "signing an undrafted player is refused during the window")
        waivers.place_claim(db, team(1), free[0], ended + datetime.timedelta(hours=2))
        waivers.place_claim(db, team(3), free[0], ended + datetime.timedelta(hours=3))
        db.commit()
        check(waivers.process(db, league_id, ended + datetime.timedelta(hours=25)) == 1, "post-draft claim resolves after 24h")
        db.commit()
        wins = {c.team_id: c.status for c in db.query(WaiverClaim).filter_by(draftable_asset_id=free[0])}
        check(wins.get(t[1]) == "won" and wins.get(t[3]) == "lost", "the team first in the waiver order (worst goal differential) wins the claim")
        check(free_agency.add_player(db, team(0), free[1], free_agency.USER, ended + datetime.timedelta(hours=25)) is not None,
              "unclaimed undrafted players are free agents once the window ends")
        # ---- configurable waiver period and processing days
        check(league.waiver_hours == 24 and league.waiver_processing_days is None, "waiver settings default to 24 hours, no processing days")
        league.waiver_hours, league.waiver_processing_days = 48, None
        check(waivers.window_end(league, ended) == ended + datetime.timedelta(hours=48), "waiver_hours sets the length of the hold")
        league.waiver_hours, league.waiver_processing_days = 24, [2]            # Wednesday
        wed = D(2026, 11, 4, 8, 0)                                              # Wednesday 00:00 PST
        check(waivers.window_end(league, ended) == wed, "with processing days the hold runs on to the next midnight Pacific of a checked day")
        check(waivers.window_end(league, wed - datetime.timedelta(hours=24)) == wed, "a hold ending exactly at a processing midnight is over then")
        league.waiver_processing_days = [2, 6]
        check(waivers.window_end(league, D(2026, 11, 5, 12, 0)) == D(2026, 11, 8, 8, 0), "the next checked day after Wednesday is Sunday")
        league.waiver_processing_days = [2]
        db.commit()
        until = waivers.waiver_until(db, league_id, ended + datetime.timedelta(hours=30))
        check(until.get(free[2]) == wed, "post-draft players stay on waivers past the 24 hours until the processing day")
        waivers.place_claim(db, team(3), free[2], ended + datetime.timedelta(hours=30))
        db.commit()
        check(waivers.process(db, league_id, wed - datetime.timedelta(minutes=1)) == 0, "the claim waits for the processing day")
        league.waiver_processing_days = [3]
        db.flush()
        waivers.retime_pending(db, league_id, ended + datetime.timedelta(hours=31))
        db.commit()
        claim = db.query(WaiverClaim).filter_by(draftable_asset_id=free[2], status="pending").one()
        check(claim.available_at == D(2026, 11, 5, 8, 0), "changing the processing days re-times claims already placed")
        check(waivers.process(db, league_id, wed + datetime.timedelta(hours=1)) == 0, "the old processing day no longer resolves it")
        check(waivers.process(db, league_id, D(2026, 11, 5, 8, 1)) == 1, "the claim resolves on the new processing day")
        db.commit()
        league.waiver_hours, league.waiver_processing_days = 0, None
        db.commit()
        check(waivers.waiver_until(db, league_id, ended + datetime.timedelta(hours=1)) == {}, "0 hours: no one is held on waivers")
    print("\nAll waiver checks passed.")


if __name__ == "__main__":
    main()
