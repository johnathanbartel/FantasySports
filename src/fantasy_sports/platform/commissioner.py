"""
Commissioner tools: who counts as a commissioner, and what a commissioner can do on other people's behalf.

  * Co-commissioners. leagues.commissioner_user_id is the PRIMARY commissioner. A co-commissioner (LeagueCoCommissioner) has
    every commissioner power except the ones that are about the commissioner role itself: only the primary adds or removes
    co-commissioners and deletes the league. A co-commissioner must manage a team in the league.
  * Counters. A team's transaction count is what it did (free-agent / waiver signings and completed trades) plus the
    commissioner's adjustment; its FAAB balance (free-agency money, college leagues) is computed from the budget plus the
    commissioner's adjustment. Adjustments are stored apart, so the transaction log itself is never rewritten.
  * Waiver order. A hand-set order applies from the moment it is set until the next weekly reset (platform/waivers.py).
  * Rosters. add / drop / move a player for any team, free, outside the usual rules about price and waivers. These are logged
    with reason "commissioner_edit", which is not counted as a team's own transaction and never puts a player on waivers
    (unless the commissioner asks for that).
  * League history. Archived seasons (LeagueSeason) can be corrected, and seasons from before the league joined the platform
    added.

Every one of these writes a CommissionerAction row (the audit trail the commissioner tab shows). Nothing here commits.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import (
    CommissionerAction,
    FreeAgencyTransaction,
    League,
    LeagueCoCommissioner,
    LeagueSeason,
    LineupEntry,
    RosterEntry,
    Team,
    TeamAdjustment,
    Trade,
    User,
    WaiverOrderOverride,
)

EDIT = "commissioner_edit"                     # FreeAgencyTransaction.reason for a commissioner's correction
UNCOUNTED_REASONS = ("ai_fill", EDIT)           # signings that aren't a team's own transaction
MAX_COMMISSIONERS = 4                           # primary + up to three co-commissioners


class CommissionerError(ValueError):
    """A refused commissioner action; the message is meant for the user."""


# ---- who is a commissioner ----------------------------------------------------------------------------------------
def co_commissioner_ids(db: Session, league_id: int) -> set[int]:
    return {c.user_id for c in db.query(LeagueCoCommissioner).filter_by(league_id=league_id)}


def commissioner_ids(db: Session, league: League) -> set[int]:
    """The primary commissioner and every co-commissioner."""
    return {league.commissioner_user_id} | co_commissioner_ids(db, league.id)


def is_commissioner(db: Session, league: League, user_id: int | None) -> bool:
    if user_id is None:
        return False
    return league.commissioner_user_id == user_id or db.query(LeagueCoCommissioner).filter_by(
        league_id=league.id, user_id=user_id).first() is not None


def is_primary(league: League, user_id: int | None) -> bool:
    return user_id is not None and league.commissioner_user_id == user_id


def log_action(db: Session, league_id: int, user_id: int, action: str, team_id: int | None = None,
               detail: dict | None = None, now: datetime.datetime | None = None) -> CommissionerAction:
    row = CommissionerAction(league_id=league_id, user_id=user_id, action=action, team_id=team_id, detail=detail,
                             created_at=now or datetime.datetime.utcnow())
    db.add(row)
    db.flush()
    return row


def add_co_commissioner(db: Session, league: League, actor_id: int, user_id: int,
                        now: datetime.datetime | None = None) -> LeagueCoCommissioner:
    now = now or datetime.datetime.utcnow()
    if not is_primary(league, actor_id):
        raise PermissionError("Only the league's commissioner can add a co-commissioner")
    if user_id == league.commissioner_user_id:
        raise CommissionerError("That person is already the league's commissioner")
    if db.get(User, user_id) is None:
        raise CommissionerError("User not found")
    if user_id in co_commissioner_ids(db, league.id):
        raise CommissionerError("That person is already a co-commissioner")
    if not db.query(Team).filter_by(league_id=league.id, owner_user_id=user_id).first():
        raise CommissionerError("A co-commissioner has to manage a team in this league")
    if len(commissioner_ids(db, league)) >= MAX_COMMISSIONERS:
        raise CommissionerError(f"A league can have up to {MAX_COMMISSIONERS - 1} co-commissioners")
    row = LeagueCoCommissioner(league_id=league.id, user_id=user_id, added_by_user_id=actor_id, created_at=now)
    db.add(row)
    db.flush()
    log_action(db, league.id, actor_id, "co_commissioner_added", detail={"user_id": user_id}, now=now)
    return row


def remove_co_commissioner(db: Session, league: League, actor_id: int, user_id: int,
                           now: datetime.datetime | None = None) -> None:
    """The primary removes anyone; a co-commissioner may step down themselves."""
    if not (is_primary(league, actor_id) or actor_id == user_id):
        raise PermissionError("Only the league's commissioner can remove a co-commissioner")
    row = db.query(LeagueCoCommissioner).filter_by(league_id=league.id, user_id=user_id).first()
    if row is None:
        raise CommissionerError("That person isn't a co-commissioner")
    db.delete(row)
    db.flush()
    log_action(db, league.id, actor_id, "co_commissioner_removed", detail={"user_id": user_id}, now=now)


# ---- transaction counts -------------------------------------------------------------------------------------------
def transaction_counts(db: Session, league_id: int) -> dict[int, dict]:
    """team id -> {signings, trades, adjustment, total}. Signings are a team's own free-agent and waiver adds (the AI's
    required fill and commissioner corrections don't count); trades are completed ones the team was part of."""
    team_ids = [t.id for t in db.query(Team.id).filter_by(league_id=league_id)]
    out = {t: {"signings": 0, "trades": 0, "adjustment": 0, "total": 0} for t in team_ids}
    for txn in db.query(FreeAgencyTransaction).filter(
            FreeAgencyTransaction.league_id == league_id, FreeAgencyTransaction.kind == "add",
            FreeAgencyTransaction.reason.notin_(UNCOUNTED_REASONS)):
        if txn.team_id in out:
            out[txn.team_id]["signings"] += 1
    for trade in db.query(Trade).filter_by(league_id=league_id, status="accepted"):
        for t in (trade.proposer_team_id, trade.counterparty_team_id):
            if t in out:
                out[t]["trades"] += 1
    for adj in db.query(TeamAdjustment).filter_by(league_id=league_id):
        if adj.team_id in out:
            out[adj.team_id]["adjustment"] = adj.transaction_adjustment
    for row in out.values():
        row["total"] = max(0, row["signings"] + row["trades"] + row["adjustment"])
    return out


def _adjustment_row(db: Session, team: Team, now: datetime.datetime) -> TeamAdjustment:
    row = db.query(TeamAdjustment).filter_by(team_id=team.id).first()
    if row is None:
        row = TeamAdjustment(league_id=team.league_id, team_id=team.id, transaction_adjustment=0, faab_adjustment=0, updated_at=now)
        db.add(row)
        db.flush()
    return row


def set_transaction_count(db: Session, team: Team, actor_id: int, total: int,
                          now: datetime.datetime | None = None) -> dict:
    """Makes the team's transaction count `total` by storing the difference from what it actually did."""
    now = now or datetime.datetime.utcnow()
    if total < 0 or total > 100_000:
        raise CommissionerError("A transaction count has to be between 0 and 100,000")
    counts = transaction_counts(db, team.league_id)[team.id]
    before = counts["total"]
    row = _adjustment_row(db, team, now)
    row.transaction_adjustment = total - counts["signings"] - counts["trades"]
    row.updated_at = now
    db.flush()
    log_action(db, team.league_id, actor_id, "transactions_set", team.id, {"from": before, "to": total}, now)
    return transaction_counts(db, team.league_id)[team.id]


# ---- FAAB (free-agency money) -------------------------------------------------------------------------------------
def faab_adjustment(db: Session, team_id: int) -> int:
    row = db.query(TeamAdjustment).filter_by(team_id=team_id).first()
    return row.faab_adjustment if row else 0


def set_faab_balance(db: Session, team: Team, actor_id: int, balance: int, now: datetime.datetime | None = None) -> int:
    """Makes the team's free-agency money `balance`. Only college leagues have a budget; elsewhere signings are free."""
    from fantasy_sports.platform import free_agency
    from fantasy_sports.platform.league_features import is_college

    now = now or datetime.datetime.utcnow()
    if not is_college(db, team.league_id):
        raise CommissionerError("This league has no free-agency budget, so there is no FAAB balance to set")
    if balance < 0 or balance > 10_000_000:
        raise CommissionerError("A balance has to be between 0 and 10,000,000")
    row = _adjustment_row(db, team, now)
    current = free_agency.team_money(db, team)["fa_money"]
    base = current - row.faab_adjustment
    row.faab_adjustment = balance - base
    row.updated_at = now
    db.flush()
    log_action(db, team.league_id, actor_id, "faab_set", team.id, {"from": current, "to": balance}, now)
    return balance


# ---- waiver order -------------------------------------------------------------------------------------------------
def set_waiver_order(db: Session, league: League, actor_id: int, team_ids: list[int],
                     now: datetime.datetime | None = None) -> WaiverOrderOverride:
    """The order is the full list of the league's teams, first claim first. It holds until the weekly reset."""
    now = now or datetime.datetime.utcnow()
    league_teams = {t.id for t in db.query(Team.id).filter_by(league_id=league.id)}
    if len(set(team_ids)) != len(team_ids) or set(team_ids) != league_teams:
        raise CommissionerError("The waiver order has to list every team in the league exactly once")
    row = WaiverOrderOverride(league_id=league.id, team_order=list(team_ids), set_by_user_id=actor_id, set_at=now)
    db.add(row)
    db.flush()
    log_action(db, league.id, actor_id, "waiver_order_set", detail={"order": list(team_ids)}, now=now)
    return row


def latest_waiver_override(db: Session, league_id: int, since: datetime.datetime,
                           at: datetime.datetime) -> WaiverOrderOverride | None:
    """The hand-set order in force at `at`: the latest one set since the last weekly reset."""
    return (db.query(WaiverOrderOverride)
            .filter(WaiverOrderOverride.league_id == league_id, WaiverOrderOverride.set_at >= since,
                    WaiverOrderOverride.set_at <= at)
            .order_by(WaiverOrderOverride.set_at.desc(), WaiverOrderOverride.id.desc()).first())


# ---- rosters ------------------------------------------------------------------------------------------------------
def _team_in_league(db: Session, league: League, team_id: int) -> Team:
    team = db.get(Team, team_id)
    if team is None or team.league_id != league.id:
        raise CommissionerError("That team isn't in this league")
    return team


def add_player(db: Session, league: League, actor_id: int, team_id: int, asset_id: int, ignore_roster_limit: bool = False,
               now: datetime.datetime | None = None) -> None:
    """Puts a free agent on a team's roster at no cost."""
    from fantasy_sports.platform import free_agency
    from fantasy_sports.platform.league_features import roster_limit

    now = now or datetime.datetime.utcnow()
    team = _team_in_league(db, league, team_id)
    if asset_id not in set(free_agency.pool_ids(db, league.id)):
        raise CommissionerError("That player isn't in this league's pool")
    if asset_id in free_agency.rostered_ids(db, league.id):
        raise CommissionerError("That player is already on a team's roster")
    limit = roster_limit(db, league)
    if not ignore_roster_limit and free_agency.roster_count(db, team.id) >= limit:
        raise CommissionerError(f"That roster is full ({limit}). Drop a player first, or allow going over the limit")
    free_agency._sign(db, team, asset_id, 0, EDIT, now)
    log_action(db, league.id, actor_id, "roster_add", team.id, {"asset_id": asset_id}, now)


def drop_player(db: Session, league: League, actor_id: int, team_id: int, asset_id: int, put_on_waivers: bool = False,
                now: datetime.datetime | None = None) -> None:
    """Takes a player off a team's roster (and out of its lineups). He goes to free agency at once, unless the commissioner
    asks for the usual 24 hours on waivers."""
    from fantasy_sports.platform import pecking_order

    now = now or datetime.datetime.utcnow()
    team = _team_in_league(db, league, team_id)
    entry = db.query(RosterEntry).filter_by(team_id=team.id, draftable_asset_id=asset_id).first()
    if entry is None:
        raise CommissionerError("That player isn't on this team's roster")
    db.delete(entry)
    db.query(LineupEntry).filter_by(team_id=team.id, draftable_asset_id=asset_id).delete()
    pecking_order.unrank_player(db, team, asset_id)
    db.add(FreeAgencyTransaction(league_id=league.id, team_id=team.id, draftable_asset_id=asset_id, kind="drop", price=0,
                                 reason="commissioner" if put_on_waivers else EDIT, created_at=now))
    db.flush()
    log_action(db, league.id, actor_id, "roster_drop", team.id, {"asset_id": asset_id, "waivers": put_on_waivers}, now)


def move_player(db: Session, league: League, actor_id: int, from_team_id: int, to_team_id: int, asset_id: int,
                ignore_roster_limit: bool = False, now: datetime.datetime | None = None) -> None:
    """Moves a player from one team's roster to another's, as a straight transfer (no trade, no price)."""
    from fantasy_sports.platform import free_agency, pecking_order
    from fantasy_sports.platform.league_features import roster_limit

    now = now or datetime.datetime.utcnow()
    src, dst = _team_in_league(db, league, from_team_id), _team_in_league(db, league, to_team_id)
    if src.id == dst.id:
        raise CommissionerError("Pick two different teams")
    entry = db.query(RosterEntry).filter_by(team_id=src.id, draftable_asset_id=asset_id).first()
    if entry is None:
        raise CommissionerError("That player isn't on the first team's roster")
    limit = roster_limit(db, league)
    if not ignore_roster_limit and free_agency.roster_count(db, dst.id) >= limit:
        raise CommissionerError(f"The receiving roster is full ({limit}). Drop a player first, or allow going over the limit")
    db.delete(entry)
    db.query(LineupEntry).filter_by(team_id=src.id, draftable_asset_id=asset_id).delete()
    pecking_order.unrank_player(db, src, asset_id)
    db.flush()
    db.add(RosterEntry(team_id=dst.id, draftable_asset_id=asset_id, acquired_via="commissioner"))
    db.flush()
    pecking_order.rank_new_player(db, dst, asset_id)
    log_action(db, league.id, actor_id, "roster_move", dst.id, {"asset_id": asset_id, "from_team_id": src.id}, now)


# ---- league history -----------------------------------------------------------------------------------------------
STANDINGS_KEYS = ("wins", "losses", "ties")


def _clean_standings(db: Session, league: League, rows: list[dict]) -> list[dict]:
    """Validates a replacement standings list: a name (or a team in this league), non-negative records. Ranks follow the order."""
    if not rows:
        raise CommissionerError("The standings need at least one team")
    if len(rows) > 64:
        raise CommissionerError("That's too many teams for one season")
    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=league.id)}
    out, seen = [], set()
    for i, r in enumerate(rows, start=1):
        team_id = r.get("team_id")
        if team_id is not None:
            if team_id not in names:
                raise CommissionerError(f"Team {team_id} isn't in this league")
            if team_id in seen:
                raise CommissionerError(f"{names[team_id]} is listed twice")
            seen.add(team_id)
        name = (r.get("team_name") or names.get(team_id) or "").strip()
        if not name or len(name) > 80:
            raise CommissionerError(f"Row {i} needs a team name of up to 80 characters")
        row = {"rank": i, "team_id": team_id, "team_name": name}
        for key in STANDINGS_KEYS:
            v = r.get(key, 0)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0 or v > 10_000:
                raise CommissionerError(f"Row {i}: {key} has to be a whole number from 0 to 10,000")
            row[key] = v
        out.append(row)
    return out


def _season(db: Session, league: League, season_number: int) -> LeagueSeason:
    row = db.query(LeagueSeason).filter_by(league_id=league.id, season_number=season_number).one_or_none()
    if row is None:
        raise CommissionerError("That season isn't in the league's history")
    return row


def edit_season(db: Session, league: League, actor_id: int, season_number: int, sent: set[str], champion_team_id: int | None = None,
                standings: list[dict] | None = None, note: str | None = None, label: str | None = None,
                now: datetime.datetime | None = None) -> LeagueSeason:
    """Corrects an archived season. `sent` is the set of fields the caller actually included (so null can mean "clear")."""
    now = now or datetime.datetime.utcnow()
    row = _season(db, league, season_number)
    changed: list[str] = []
    if "champion_team_id" in sent:
        if champion_team_id is not None and db.query(Team).filter_by(id=champion_team_id, league_id=league.id).first() is None:
            raise CommissionerError("The champion has to be a team in this league")
        if (league.offseason and season_number == league.season_number - 1) and row.champion_team_id != champion_team_id:
            raise CommissionerError("The champion sets the next draft's order, which is still to be played. Change it after the draft")
        row.champion_team_id = champion_team_id
        changed.append("champion")
    data = dict(row.data or {})
    if "standings" in sent:
        if league.offseason and season_number == league.season_number - 1:
            raise CommissionerError("These standings set the order of the next draft, which is still to be played. Change them after the draft")
        old = {r.get("team_id"): r for r in data.get("standings", []) if r.get("team_id") is not None}
        cleaned = _clean_standings(db, league, standings or [])
        data["standings"] = [{**old.get(r["team_id"], {}), **r} for r in cleaned]     # keep stats we don't edit (goals, ...)
        changed.append("standings")
    if "label" in sent:
        text = (label or "").strip()
        if len(text) > 40:
            raise CommissionerError("A season label is 40 characters at most")
        if text:
            data["label"] = text
        else:
            data.pop("label", None)
        changed.append("label")
    row.data = data
    if "note" in sent:
        text = (note or "").strip()
        if len(text) > 500:
            raise CommissionerError("A note is 500 characters at most")
        row.commissioner_note = text or None
        changed.append("note")
    row.edited_at = now
    db.flush()
    log_action(db, league.id, actor_id, "season_edited", detail={"season_number": season_number, "changed": changed}, now=now)
    return row


def add_past_season(db: Session, league: League, actor_id: int, season_number: int, standings: list[dict],
                    champion_team_id: int | None = None, note: str | None = None, label: str | None = None,
                    now: datetime.datetime | None = None) -> LeagueSeason:
    """Adds a season played before the league joined the platform (or one that was never archived)."""
    now = now or datetime.datetime.utcnow()
    if season_number >= league.season_number:
        raise CommissionerError(f"Past seasons are numbered below the current season ({league.season_number}); use 0, -1, ... for earlier ones")
    if season_number < -99:
        raise CommissionerError("Season numbers go down to -99")
    if db.query(LeagueSeason).filter_by(league_id=league.id, season_number=season_number).first():
        raise CommissionerError(f"Season {season_number} is already in the history. Edit it instead")
    if champion_team_id is not None and db.query(Team).filter_by(id=champion_team_id, league_id=league.id).first() is None:
        raise CommissionerError("The champion has to be a team in this league")
    text = (label or "").strip()
    if len(text) > 40:
        raise CommissionerError("A season label is 40 characters at most")
    note = (note or "").strip()
    if len(note) > 500:
        raise CommissionerError("A note is 500 characters at most")
    data = {"standings": _clean_standings(db, league, standings), "imported": True}
    if text:
        data["label"] = text
    row = LeagueSeason(league_id=league.id, season_number=season_number, league_format=league.league_format, archived_at=now,
                       champion_team_id=champion_team_id, data=data, commissioner_note=note or None, edited_at=now)
    db.add(row)
    db.flush()
    log_action(db, league.id, actor_id, "season_added", detail={"season_number": season_number}, now=now)
    return row
