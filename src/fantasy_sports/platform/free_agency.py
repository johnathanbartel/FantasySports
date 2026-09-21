"""
Free agency: how a team fills a roster spot it left open in the recruiting draft.

Teams may recruit fewer players than the roster size. The gap is filled by
signing free agents - players in the league's pool that no team in the league
rosters. Free agency opens when the recruiting draft ends. AI teams are
REQUIRED to use it to fill every open spot: the moment recruiting closes each
AI team signs, through the same transaction log a person's signings go in,
the best free agent it can afford (starting positions it still lacks first,
scarcest position first, teams taking turns), and if it can't afford any, the
cheapest available - so its roster still fills, even if that leaves it over
budget.

PROVISIONAL (the commissioner wants to see the framework before deciding): the
asking_price() rule - a free agent's asking price is his hidden recruiting price,
revealed. Everything price-related routes through that one function.

Money: a signing's price counts as roster cost (there are no refunds when a player
is dropped). "Free-agency money" = the team's Season Budget (Total) - its Roster
Cost, i.e. the same pool the season Weekly Budget is carved from. A person's
signing is refused if it costs more than that; the AI's required fill isn't.

MOCK-ONLY SHORTCUT: the AI ranks free agents by their 2024 stats (see pricing.py).
"""

from __future__ import annotations

import datetime
from collections import Counter

from sqlalchemy import or_
from sqlalchemy.orm import Session

from fantasy_sports.api.helpers import draftable_asset_position
from fantasy_sports.platform import season
from fantasy_sports.platform.ai.auction_ai import POSITION_CAP, STARTER_MIN
from fantasy_sports.platform.auction.constants import STARTING_BUDGET_BY_PROGRAM_SIZE
from fantasy_sports.platform.db.models.core import (
    DraftableAsset,
    FreeAgencyTransaction,
    League,
    LeagueUniverse,
    LineupEntry,
    MatchupRequest,
    Player,
    RecruitingCharge,
    RecruitingDraft,
    RecruitingThreshold,
    RosterEntry,
    Team,
)
from fantasy_sports.platform.recruiting import pricing

USER, COMMISSIONER, AI_FILL = "user", "commissioner", "ai_fill"


def is_open(db: Session, league_id: int) -> bool:
    """Free agency opens when the recruiting draft ends."""
    draft = db.query(RecruitingDraft).filter_by(league_id=league_id).first()
    return draft is not None and draft.status == "closed"


def pool_ids(db: Session, league_id: int) -> list[int]:
    """Every player and defense group a league can use: players no longer on an FBS roster
    (Player.active False) are out."""
    universe_ids = [lu.universe_id for lu in db.query(LeagueUniverse).filter_by(league_id=league_id)]
    return [
        a.id for a in db.query(DraftableAsset)
        .outerjoin(Player, Player.id == DraftableAsset.player_id)
        .filter(DraftableAsset.universe_id.in_(universe_ids),
                or_(DraftableAsset.player_id.is_(None), Player.active.is_(True)))
    ]


def rostered_ids(db: Session, league_id: int) -> set[int]:
    team_ids = [t.id for t in db.query(Team).filter_by(league_id=league_id)]
    return {r.draftable_asset_id for r in db.query(RosterEntry).filter(RosterEntry.team_id.in_(team_ids or [0]))}


def asking_prices(db: Session, league: League) -> dict[int, int]:
    """PROVISIONAL price rule: a free agent costs his hidden recruiting price,
    made public once recruiting ends. Players with no recruiting price (a league
    that never ran a recruiting draft) cost the average player's price."""
    fallback = pricing.ROLE_MEAN_PER_WEEK * league.draft_rounds
    draft = db.query(RecruitingDraft).filter_by(league_id=league.id).first()
    out = {}
    if draft is not None:
        out = {t.draftable_asset_id: t.threshold for t in db.query(RecruitingThreshold).filter_by(draft_id=draft.id)}
    return {a: out.get(a, fallback) for a in pool_ids(db, league.id)}


def team_money(db: Session, team: Team) -> dict:
    """The numbers behind a team's free-agency budget."""
    league = db.get(League, team.league_id)
    weekly = STARTING_BUDGET_BY_PROGRAM_SIZE[team.program_size]
    season_total = season.season_budget_total(weekly, weekly * league.draft_rounds)
    draft = db.query(RecruitingDraft).filter_by(league_id=team.league_id).first()
    recruited = 0
    if draft is not None:
        recruited = sum(c.amount for c in db.query(RecruitingCharge).filter_by(
            draft_id=draft.id, team_id=team.id, is_winner=True))
    signed = fa_spent(db, team.id)
    accepted = db.query(MatchupRequest).filter_by(league_id=team.league_id, status="accepted")
    sent = sum(r.offer_amount for r in accepted.filter_by(from_team_id=team.id))        # matchup offers this team paid
    received = sum(r.offer_amount for r in accepted.filter_by(to_team_id=team.id))      # ... and was paid
    return {
        "season_budget_total": season_total, "recruited_cost": recruited, "fa_spent": signed,
        "schedule_cash_sent": sent, "schedule_cash_received": received,
        "roster_cost": recruited + signed, "fa_money": season_total - recruited - signed - sent + received,
    }


def fa_spent(db: Session, team_id: int) -> int:
    return sum(t.price for t in db.query(FreeAgencyTransaction).filter_by(team_id=team_id, kind="add"))


def roster_count(db: Session, team_id: int) -> int:
    return db.query(RosterEntry).filter_by(team_id=team_id).count()


def _sign(db: Session, team: Team, asset_id: int, price: int, reason: str, now: datetime.datetime) -> None:
    db.add(RosterEntry(team_id=team.id, draftable_asset_id=asset_id, acquired_via="free_agency"))
    db.add(FreeAgencyTransaction(
        league_id=team.league_id, team_id=team.id, draftable_asset_id=asset_id,
        kind="add", price=price, reason=reason, created_at=now))


def add_player(
    db: Session, team: Team, asset_id: int, reason: str, now: datetime.datetime | None = None
) -> FreeAgencyTransaction:
    """A person's signing (the AI's required fill goes through ai_fill_rosters).
    Raises ValueError with a message meant for the user."""
    now = now or datetime.datetime.utcnow()
    league = db.get(League, team.league_id)
    if not is_open(db, league.id):
        raise ValueError("Free agency opens when the recruiting draft ends")
    if asset_id not in set(pool_ids(db, league.id)):
        raise ValueError("That player isn't in this league's pool")
    if asset_id in rostered_ids(db, league.id):
        raise ValueError("That player is already on a team's roster")
    if roster_count(db, team.id) >= league.draft_rounds:
        raise ValueError(f"Roster is full ({league.draft_rounds}) - drop a player first")
    price = asking_prices(db, league)[asset_id]
    money = team_money(db, team)["fa_money"]
    if price > money:
        raise ValueError(f"He costs ${price} but only ${max(0, money)} of free-agency money is left "
                         f"(Season Budget less Roster Cost)")
    _sign(db, team, asset_id, price, reason, now)
    db.flush()
    return db.query(FreeAgencyTransaction).filter_by(team_id=team.id, draftable_asset_id=asset_id).order_by(
        FreeAgencyTransaction.id.desc()).first()


def drop_player(
    db: Session, team: Team, asset_id: int, reason: str, now: datetime.datetime | None = None
) -> FreeAgencyTransaction:
    """Releases a player to the free-agent pool. No refund - the money is spent."""
    now = now or datetime.datetime.utcnow()
    if not is_open(db, team.league_id):
        raise ValueError("Free agency opens when the recruiting draft ends")
    entry = db.query(RosterEntry).filter_by(team_id=team.id, draftable_asset_id=asset_id).first()
    if entry is None:
        raise ValueError("That player isn't on this team's roster")
    db.delete(entry)
    db.query(LineupEntry).filter_by(team_id=team.id, draftable_asset_id=asset_id).delete()
    txn = FreeAgencyTransaction(
        league_id=team.league_id, team_id=team.id, draftable_asset_id=asset_id,
        kind="drop", price=0, reason=reason, created_at=now)
    db.add(txn)
    db.flush()
    return txn


def ai_fill_rosters(db: Session, league_id: int, now: datetime.datetime | None = None) -> dict[int, int]:
    """The AI's REQUIRED free-agency fill: every AI team with open roster spots
    signs free agents until its roster is full (or nobody is left). Idempotent -
    a full team is skipped. Returns team_id -> players signed."""
    now = now or datetime.datetime.utcnow()
    league = db.get(League, league_id)
    prices = asking_prices(db, league)
    value = pricing.value_by_asset(db, db.query(Team).filter_by(league_id=league_id).count(), league.draft_rounds)
    assets = {a.id: a for a in db.query(DraftableAsset).filter(DraftableAsset.id.in_(list(prices) or [0]))}
    position = {i: draftable_asset_position(a) for i, a in assets.items()}
    free = {a for a in prices if a not in rostered_ids(db, league_id)}
    signed: dict[int, int] = {}

    # Per-team state for the fill.
    state = {}
    for team in db.query(Team).filter_by(league_id=league_id, is_ai_owned=True).order_by(Team.id):
        open_spots = league.draft_rounds - roster_count(db, team.id)
        if open_spots > 0:
            state[team.id] = {
                "team": team, "open": open_spots, "money": team_money(db, team)["fa_money"],
                "have": Counter(position.get(r.draftable_asset_id) for r in db.query(RosterEntry).filter_by(team_id=team.id)),
            }

    def pick_from(st, candidates):
        affordable = [a for a in candidates if prices[a] <= st["money"]]
        pick = (max(affordable, key=lambda a: (value.get(a, 0.0), -prices[a])) if affordable
                else min(candidates, key=lambda a: (prices[a], -value.get(a, 0.0))))
        _sign(db, st["team"], pick, prices[pick], AI_FILL, now)
        st["money"] -= prices[pick]
        st["open"] -= 1
        st["have"][position[pick]] += 1
        free.discard(pick)
        signed[st["team"].id] = signed.get(st["team"].id, 0) + 1

    # Phase 1 - starting positions first, scarcest first, one team at a time in turn, so a position
    # with few free agents (only 16 DL/LB/DB groups exist) is spread across the teams that lack it
    # instead of the first teams in line taking extras.
    for pos in sorted(STARTER_MIN, key=lambda p: sum(1 for a in free if position[a] == p)):
        for st in state.values():
            while st["open"] > 0 and st["have"][pos] < STARTER_MIN[pos]:
                candidates = [a for a in free if position[a] == pos]
                if not candidates:
                    break
                pick_from(st, candidates)

    # Phase 2 - the rest of each team's open spots: best free agent it can afford within its
    # position caps; if it can't afford any, the cheapest available, so the roster still fills.
    for st in state.values():
        while st["open"] > 0 and free:
            room = [a for a in free if position[a] in POSITION_CAP and st["have"][position[a]] < POSITION_CAP[position[a]]]
            pick_from(st, room or list(free))
    db.flush()
    return signed
