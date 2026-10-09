"""
NHL equivalent of football/professional/scripts/run_week_validation.py: run
every game's shots (and, when ingested, play-by-play events) through the xG
credit pipeline and compare each team's model total with its real goals.

Reads only (never writes) unless --write is passed, which replaces the
shot_credits and event_credits rows for the games it scored. Point
DATABASE_URL at a throwaway copy when using --write.

Scope: the model total is shooter + assist credit (goals in regulation and
overtime; a goal's credits sum to exactly 1.0). Real scores also include the
shootout winner's extra goal, which has no shot rows, so a -1 error on
shootout winners is expected, exactly as the football validation expects a
negative bias from unattributed kickoffs and punts. Model goals allowed is
rebuilt from goalie GSAx (xG faced - GSAx) and excludes empty-net goals.
Block and hit credit is extra credit, reported on its own lines.

Stored game scores come from the MoneyPuck shot file. When events are
present, each game is also checked against the NHL's own play-by-play: goal
events must equal the stored goals, and every goal must pair with a goal event
(its assists then come from that event).

Usage:
    DATABASE_URL=sqlite:///./copy.db python run_game_validation.py [--limit N] [--write] [--no-events]
"""

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from fantasy_sports.platform.db.models.core import Game, Player, DraftableAsset
from fantasy_sports.platform.db.session import SessionLocal
from fantasy_sports.sports.hockey.models import EventCredit, GameEvent, Shot, ShotCredit
from fantasy_sports.sports.hockey.pipeline import (
    BlockXgEstimator, EventRecord, ShotRecord, process_game,
)


def _shot_clock(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="score only the first N games")
    parser.add_argument("--write", action="store_true", help="replace credit rows for scored games")
    parser.add_argument("--no-events", action="store_true", help="ignore play-by-play events (v1 behaviour)")
    args = parser.parse_args()

    with SessionLocal() as db:
        games = db.query(Game).filter(Game.home_score.isnot(None)).order_by(Game.id)
        games = games.all()

        by_game = defaultdict(list)
        shot_ids_by_game = defaultdict(list)
        for s in db.query(Shot).order_by(Shot.id):
            by_game[s.game_id].append(ShotRecord(
                shot_id=s.id, shooting_team_id=s.shooting_team_id, goalie_team_id=s.goalie_team_id,
                shooter_asset_id=s.shooter_asset_id, goalie_asset_id=s.goalie_asset_id,
                pre_shot_xg=s.pre_shot_xg or 0.0, is_goal=bool(s.is_goal), is_empty_net=bool(s.is_empty_net),
                game_seconds=_shot_clock(s.clock),
            ))
        events_by_game = defaultdict(list)
        all_events = []
        if not args.no_events:
            for e in db.query(GameEvent).order_by(GameEvent.id):
                rec = EventRecord(
                    event_id=e.id, event_type=e.event_type, team_id=e.team_id, game_seconds=e.game_seconds,
                    actor_ref=e.actor_ref, secondary_ref=e.secondary_ref, tertiary_ref=e.tertiary_ref,
                    actor_asset_id=e.actor_asset_id, secondary_asset_id=e.secondary_asset_id,
                    tertiary_asset_id=e.tertiary_asset_id, x=e.x_coord, y=e.y_coord,
                )
                events_by_game[e.game_id].append(rec)
                all_events.append(rec)
        estimator = BlockXgEstimator.fit(all_events) if all_events else None
        scored_ids = [g.id for g in games if by_game.get(g.id)]
        if args.limit:
            scored_ids = scored_ids[: args.limit]
        scored_set = set(scored_ids)
        print(f"{len(scored_ids)} NHL games with shots ({len(games)} scored games in the database), "
              f"{sum(len(by_game[i]) for i in scored_ids)} shots, "
              f"{len(events_by_game)} games with play-by-play events\n")

        rows, def_rows = [], []
        max_goal_tie = max_goalie_tie = 0.0
        unnamed_shooter = unnamed_assist = unnamed_goalie = 0.0
        en_goals = shootout_games = 0
        shooter_assets, assist_assets, goalie_assets, blocker_assets = set(), set(), set(), set()
        xg_total = nonen_goals = 0.0
        goals_all = goals_with_assists = goals_unmatched = 0
        nhl_assist_mismatch = []
        nhl_goal_mismatch = []   # games where NHL goal events != stored goals
        event_games = 0
        block_totals = {"blocks": 0, "xg": 0.0}
        hit_total = 0
        team_goal_credit = defaultdict(float)
        asset_games = defaultdict(set)
        asset_totals = defaultdict(lambda: defaultdict(float))
        game_by_id = {g.id: g for g in games}

        for gid in scored_ids:
            game = game_by_id[gid]
            shots = by_game[gid]
            events = events_by_game.get(gid)
            res = process_game(shots, events, estimator, teams=(game.home_team_id, game.away_team_id))
            if events:
                event_games += 1
                nhl_goals = sum(1 for e in events if e.event_type == "goal")
                if nhl_goals != sum(1 for s in shots if s.is_goal):
                    nhl_goal_mismatch.append((gid, nhl_goals, sum(1 for s in shots if s.is_goal)))
                nhl_slots = sum((1 if e.secondary_ref else 0) + (1 if e.tertiary_ref else 0)
                                for e in events if e.event_type == "goal")
                if nhl_slots != res.assist_slots_credited:
                    nhl_assist_mismatch.append((gid, nhl_slots, res.assist_slots_credited))
                goals_all += res.goals_total
                goals_with_assists += res.goals_with_assists
                goals_unmatched += res.goals_unmatched
                block_totals["blocks"] += sum(res.blocks_by_team.values())
                block_totals["xg"] += sum(res.block_credit_by_team.values())
                hit_total += sum(res.hits_by_team.values())
            real_for = {game.home_team_id: game.home_score, game.away_team_id: game.away_score}
            real_against = {game.home_team_id: game.away_score, game.away_team_id: game.home_score}
            shot_goals = sum(1 for s in shots if s.is_goal)
            if shot_goals != game.home_score + game.away_score:
                shootout_games += 1
            for team, real in real_for.items():
                total = res.model_total(team)
                rows.append((team, total, real, total - real))
                allowed = res.model_goals_allowed(team)
                def_rows.append((team, allowed, real_against[team], allowed - real_against[team]))
            en_goals += sum(res.empty_net_goals_by_team.values())

            skater = sum(c.points for c in res.credits if c.role in ("shooter", "primary_assist", "secondary_assist"))
            skater += res.unnamed_shooter_points + res.unnamed_assist_points
            max_goal_tie = max(max_goal_tie, abs(skater - shot_goals))
            goalie_sum = sum(c.points for c in res.credits if c.role == "goalie") + res.unnamed_goalie_points
            max_goalie_tie = max(max_goalie_tie, abs(goalie_sum - sum(res.gsax_by_team.values())))
            unnamed_shooter += res.unnamed_shooter_points
            unnamed_assist += res.unnamed_assist_points
            unnamed_goalie += res.unnamed_goalie_points
            shooter_assets |= set(res.shooter_credit_by_asset)
            assist_assets |= set(res.assist_credit_by_asset)
            goalie_assets |= set(res.gsax_by_asset)
            blocker_assets |= set(res.block_credit_by_asset)
            xg_total += sum(res.xg_faced_by_team.values())
            nonen_goals += sum(1 for s in shots if s.is_goal and not s.is_empty_net)
            for table, key in ((res.shooter_credit_by_asset, "shooter"), (res.assist_credit_by_asset, "assist"),
                               (res.gsax_by_asset, "gsax"), (res.block_credit_by_asset, "block"),
                               (res.ixg_by_asset, "ixg")):
                for asset, pts in table.items():
                    asset_totals[asset][key] += pts
                    asset_games[asset].add(gid)
            for asset, n in res.takeaways_by_asset.items():
                asset_totals[asset]["takeaways"] += n
            for asset, n in res.giveaways_by_asset.items():
                asset_totals[asset]["giveaways"] += n

            if args.write:
                db.query(ShotCredit).filter(
                    ShotCredit.shot_id.in_([s.shot_id for s in shots])
                ).delete(synchronize_session=False)
                db.bulk_insert_mappings(ShotCredit, [
                    {"shot_id": c.shot_id, "draftable_asset_id": c.asset_id, "role": c.role, "credit_points": c.points}
                    for c in res.credits
                ])
                if events:
                    db.query(EventCredit).filter(
                        EventCredit.event_id.in_([e.event_id for e in events])
                    ).delete(synchronize_session=False)
                    db.bulk_insert_mappings(EventCredit, [
                        {"event_id": c.event_id, "draftable_asset_id": c.asset_id, "role": c.role, "credit_points": c.points}
                        for c in res.event_credits
                    ])
        if args.write:
            db.commit()
            print(f"Wrote credits for {len(scored_ids)} games.\n")

        names = {}
        if asset_totals:
            for asset_id, name, pos in db.query(DraftableAsset.id, Player.full_name, Player.position).join(
                Player, Player.id == DraftableAsset.player_id
            ).filter(DraftableAsset.id.in_(list(asset_totals))):
                names[asset_id] = (name, pos)

    errors = [r[3] for r in rows]
    exact = sum(1 for e in errors if abs(e) < 1e-9)
    minus_one = sum(1 for e in errors if abs(e + 1) < 1e-9)
    print(f"{len(rows)} team-games scored ({shootout_games} games decided by shootout).")
    print(f"Mean error (model - real):   {statistics.mean(errors):+.3f}")
    print(f"Median error:                {statistics.median(errors):+.2f}")
    print(f"Std dev of error:            {statistics.pstdev(errors):.3f}")
    print(f"Exact matches:               {exact} / {len(errors)}; {minus_one} shootout winners at -1")
    print(f"Other errors (not 0, not -1): {len(errors) - exact - minus_one}")
    corr = statistics.correlation([r[1] for r in rows], [r[2] for r in rows])
    print(f"Correlation(model, real):    {corr:.4f}")
    print(f"Mean per team-game: model {statistics.mean(r[1] for r in rows):.3f} vs real {statistics.mean(r[2] for r in rows):.3f} goals")

    rows_sorted = sorted(rows, key=lambda r: r[3])
    print("\nBiggest overcounts (model >> real):")
    for team, total, real, err in rows_sorted[-3:][::-1]:
        print(f"  team {team:<4} model={total:+6.2f}  real={real:<3}  error={err:+.2f}")
    print("Biggest undercounts (model << real):")
    for team, total, real, err in rows_sorted[:3]:
        print(f"  team {team:<4} model={total:+6.2f}  real={real:<3}  error={err:+.2f}")

    print(f"\n=== Skater credit tie-out ===")
    print(f"max per-game |shooter + assist credit - goals| = {max_goal_tie:.9f} (must be ~0)")
    print(f"Credited: {len(shooter_assets)} shooters, {len(assist_assets)} assisters; "
          f"unnamed shooter points {unnamed_shooter:.2f}, unnamed assist points {unnamed_assist:.2f}")

    if event_games:
        print(f"\n=== NHL play-by-play check ({event_games} games with events) ===")
        print(f"NHL goal events != stored goals in {len(nhl_goal_mismatch)} games {nhl_goal_mismatch[:5]}")
        print(f"Assist slots (primary+secondary) in NHL goal events vs credited: "
              f"{len(nhl_assist_mismatch)} games differ {nhl_assist_mismatch[:5]} (differences come from goals missing in the shot file)")
        print(f"Goals paired with an NHL goal event: {goals_all - goals_unmatched} / {goals_all}; "
              f"{goals_with_assists} have at least one assist "
              f"({100 * goals_with_assists / max(goals_all, 1):.1f}%; NHL: roughly 95% of goals are assisted)")
        print(f"Blocks: {block_totals['blocks']} blocked attempts, estimated xG {block_totals['xg']:.1f} "
              f"({block_totals['xg'] / max(block_totals['blocks'], 1):.4f} per block; "
              f"{block_totals['blocks'] / (2 * event_games):.1f} blocks and "
              f"{block_totals['xg'] / (2 * event_games):.3f} goals of block credit per team-game vs "
              f"{statistics.mean(r[1] for r in rows):.2f} goals scored)")
        print(f"Hits: {hit_total} ({hit_total / (2 * event_games):.1f} per team-game)")

    d_errors = [r[3] for r in def_rows]
    print(f"\n=== Goalies: {len(def_rows)} team-games, model goals allowed (xG faced - GSAx) vs real ===")
    print(f"max |goalie credits - team GSAx| = {max_goalie_tie:.9f} (must be ~0); {len(goalie_assets)} goalies credited, "
          f"unnamed GSAx {unnamed_goalie:+.2f}; empty-net goals excluded {en_goals}")
    print(f"Mean error (model - real):   {statistics.mean(d_errors):+.3f}  (negative = empty-net + shootout goals, by design)")
    print(f"Median error:                {statistics.median(d_errors):+.2f}")
    print(f"Std dev of error:            {statistics.pstdev(d_errors):.3f}")
    print(f"Correlation(model allowed, real allowed): "
          f"{statistics.correlation([r[1] for r in def_rows], [r[2] for r in def_rows]):.4f}")
    print(f"Calibration: league xG faced {xg_total:.0f} vs non-empty-net goals {nonen_goals:.0f} "
          f"(ratio {xg_total / nonen_goals:.3f})")

    def per_game(key, pos_filter, n=5, minimum_games=40):
        board = []
        for asset, totals in asset_totals.items():
            name, pos = names.get(asset, ("?", "?"))
            games_played = len(asset_games[asset])
            if games_played >= minimum_games and pos in pos_filter:
                board.append((totals.get(key, 0.0) / games_played, name, pos, games_played, totals))
        return sorted(board, reverse=True)[:n], board

    print("\n=== Realism: credit per game (goal units; fantasy scale applied separately) ===")
    for label, positions in (("Forwards (skater credit)", {"C", "LW", "RW"}), ("Defensemen (skater credit)", {"D"})):
        board = []
        for asset, totals in asset_totals.items():
            name, pos = names.get(asset, ("?", "?"))
            games_played = len(asset_games[asset])
            if games_played >= 40 and pos in positions:
                skater = totals.get("shooter", 0.0) + totals.get("assist", 0.0)
                board.append((skater / games_played, name, pos, games_played, totals))
        board.sort(reverse=True)
        if board:
            mean = statistics.mean(b[0] for b in board)
            print(f"{label}: {len(board)} players with 40+ games, mean {mean:.3f}/game; top 3: "
                  + ", ".join(f"{b[1]} {b[0]:.2f}" for b in board[:3]))
    top, board = per_game("gsax", {"G"}, n=3)
    if board:
        print(f"Goalies GSAx per game: mean {statistics.mean(b[0] for b in board):+.3f}; top 3: "
              + ", ".join(f"{b[1]} {b[0]:+.2f}" for b in top))
    if event_games:
        top, board = per_game("block", {"D", "C", "LW", "RW"}, n=3)
        print("Block credit per game, top 3: " + ", ".join(f"{b[1]} {b[0]:.3f}" for b in top))


if __name__ == "__main__":
    main()
