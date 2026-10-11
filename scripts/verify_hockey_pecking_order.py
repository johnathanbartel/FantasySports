"""
Offline verification for the NHL pecking order: persistence, read/reorder APIs, and the pure
daily-starter resolver (FS-022).

Uses a throwaway SQLite database and the real HTTP API (FastAPI TestClient). Activity and goalie
confirmation are passed in as data; no external services are called.

Run from the repository root with:

    .venv-api/Scripts/python.exe scripts/verify_hockey_pecking_order.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fs022-pecking-", dir=ROOT) as temp_dir:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir) / 'pecking-order').as_posix()}"

        from fastapi.testclient import TestClient
        from sqlalchemy.exc import IntegrityError

        from fantasy_sports.api.auth import create_session
        from fantasy_sports.api.main import app
        from fantasy_sports.platform import pecking_order as po
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            DraftableAsset, League, LeagueUniverse, Player, RosterEntry, Team, TeamPeckingOrder, Universe, User,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine

        Base.metadata.create_all(bind=engine)
        client = TestClient(app)

        # ---- pure resolver -------------------------------------------------------------------
        full = {"C": [1, 2, 3, 4], "W": [11, 12, 13, 14, 15, 16, 17, 18], "D": [21, 22, 23, 24, 25, 26, 27, 28],
                "G": [31, 32, 33, 34]}
        everyone = frozenset(i for ids in full.values() for i in ids)
        r = po.resolve_starters(full, everyone, frozenset({31}))
        check(r.starters == {"C": [1], "W": [11, 12], "D": [21, 22], "G": [31]} and not r.vacancies,
              "all active: exactly 1 C, 2 W, 2 D, 1 G, no vacancies")
        r = po.resolve_starters(full, everyone - {1, 2, 11, 13, 21, 22, 23}, frozenset({31, 32}))
        check(r.starters["C"] == [3] and r.starters["W"] == [12, 14] and r.starters["D"] == [24, 25],
              "inactive players are skipped: C falls to C3, W/D pool past the top ranks")
        r = po.resolve_starters(full, everyone, frozenset({33}))
        check(r.starters["G"] == [33], "goalie is the highest-priority CONFIRMED active goalie")
        r = po.resolve_starters(full, everyone - {33}, frozenset({33}))
        check(r.starters["G"] == [] and [v.reason for v in r.vacancies] == [po.NO_CONFIRMED_GOALIE],
              "a confirmed goalie who is not scheduled is not dressed")
        r = po.resolve_starters(full, everyone, frozenset())
        check(r.starters["G"] == [] and [(v.position_group, v.reason) for v in r.vacancies] == [("G", po.NO_CONFIRMED_GOALIE)],
              "active goalies but none confirmed -> NO_CONFIRMED_GOALIE")
        r = po.resolve_starters(full, frozenset({31}), frozenset({31}))
        check({(v.position_group, v.slot, v.reason) for v in r.vacancies}
              == {("C", 1, po.NO_ACTIVE_PLAYER), ("W", 1, po.NO_ACTIVE_PLAYER), ("W", 2, po.NO_ACTIVE_PLAYER),
                  ("D", 1, po.NO_ACTIVE_PLAYER), ("D", 2, po.NO_ACTIVE_PLAYER)}, "nobody scheduled -> explicit per-slot vacancies")
        r = po.resolve_starters({"C": [], "W": [11], "D": [], "G": [31]}, frozenset({11, 31}), frozenset({31}))
        check(r.starters["W"] == [11] and {(v.position_group, v.slot, v.reason) for v in r.vacancies}
              == {("C", 1, po.NO_RANKED_PLAYERS), ("W", 2, po.INSUFFICIENT_RANKED),
                  ("D", 1, po.NO_RANKED_PLAYERS), ("D", 2, po.NO_RANKED_PLAYERS)},
              "incomplete ranking: partial W fills one slot with INSUFFICIENT_RANKED for the other")
        a = po.resolve_starters(full, everyone, frozenset({31}))
        b = po.resolve_starters({g: list(v) for g, v in full.items()}, set(everyone), {31})
        check(a == b, "resolver is deterministic")

        # ---- fixtures ------------------------------------------------------------------------
        with SessionLocal() as db:
            owner = User(email="o@example.com", display_name="Owner")
            rival = User(email="r@example.com", display_name="Rival")
            commish = User(email="c@example.com", display_name="Commish")
            db.add_all([owner, rival, commish])
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            cfb = Universe(sport="football", level="college", code="cfb", display_name="CFB", supports_individual_defense=True)
            db.add_all([nhl, cfb])
            db.flush()
            league = League(name="NHL", commissioner_user_id=commish.id, size=4, series_length_days=7)
            fb_league = League(name="CFB", commissioner_user_id=commish.id, size=4)
            db.add_all([league, fb_league])
            db.flush()
            db.add_all([LeagueUniverse(league_id=league.id, universe_id=nhl.id),
                        LeagueUniverse(league_id=fb_league.id, universe_id=cfb.id)])
            mine = Team(league_id=league.id, name="Mine", owner_user_id=owner.id, is_ai_owned=False)
            other = Team(league_id=league.id, name="Other", owner_user_id=rival.id, is_ai_owned=False)
            sparse = Team(league_id=league.id, name="Sparse", owner_user_id=owner.id, is_ai_owned=False)
            ai = Team(league_id=league.id, name="AI", is_ai_owned=True)
            fb_team = Team(league_id=fb_league.id, name="FB", owner_user_id=owner.id, is_ai_owned=False)
            db.add_all([mine, other, sparse, ai, fb_team])
            db.flush()

            counter = [0]

            def add_player(team: Team | None, position: str, name: str) -> int:
                counter[0] += 1
                p = Player(universe_id=nhl.id, full_name=name, position=position, external_ref=f"x{counter[0]}")
                db.add(p)
                db.flush()
                a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
                db.add(a)
                db.flush()
                if team is not None:
                    db.add(RosterEntry(team_id=team.id, draftable_asset_id=a.id))
                return p.id

            my = {"C": [], "W": [], "D": [], "G": []}
            for pos, group, n in [("C", "C", 5), ("LW", "W", 5), ("RW", "W", 4), ("D", "D", 9), ("G", "G", 5)]:
                for i in range(n):
                    my[group].append(add_player(mine, pos, f"Mine {pos}{i + 1}"))
            foreign_c = add_player(other, "C", "Foreign C")
            free_agent_c = add_player(None, "C", "Free Agent C")
            sparse_c = add_player(sparse, "C", "Sparse C1")
            sparse_w = add_player(sparse, "W", "Sparse W1")
            ai_c = add_player(ai, "C", "AI C1")
            db.commit()
            team_ids = dict(mine=mine.id, other=other.id, sparse=sparse.id, ai=ai.id, fb=fb_team.id)
            owner_tok = create_session(db, owner.id).token
            rival_tok = create_session(db, rival.id).token
            commish_tok = create_session(db, commish.id).token

        def hdr(tok):
            return {"Authorization": f"Bearer {tok}"}

        url = f"/teams/{team_ids['mine']}/pecking-order"

        # ---- read: starts empty, roster shown as unranked ------------------------------------
        r = client.get(url)
        body = r.json()
        check(r.status_code == 200 and [g["position_group"] for g in body["groups"]] == ["C", "W", "D", "G"],
              "GET returns the C/W/D/G groups")
        check(all(g["ranked"] == [] for g in body["groups"]) and len(body["groups"][1]["unranked"]) == 9,
              "a fresh team has nothing ranked; roster players (LW+RW as W) are listed unranked")
        check([g["max_depth"] for g in body["groups"]] == [4, 8, 8, 4] and [g["starters"] for g in body["groups"]] == [1, 2, 2, 1],
              "depth (4/8/8/4) and starter counts (1/2/2/1) are reported")
        check(client.get("/teams/99999/pecking-order").status_code == 404, "unknown team is 404")
        check(client.get(f"/teams/{team_ids['fb']}/pecking-order").status_code == 400, "non-NHL team is rejected")

        # ---- authorization -------------------------------------------------------------------
        good = {"assignments": {"C": my["C"][:4], "W": my["W"][:8], "D": my["D"][:8], "G": my["G"][:4]}}
        check(client.put(url, json=good).status_code == 401, "reordering requires a signed-in user")
        check(client.put(url, json=good, headers=hdr(rival_tok)).status_code == 403, "another team's owner cannot reorder")
        check(client.put(url, json=good, headers=hdr(commish_tok)).status_code == 403,
              "the commissioner cannot reorder a human-owned team")
        r = client.put(f"/teams/{team_ids['ai']}/pecking-order", json={"assignments": {"C": [ai_c]}}, headers=hdr(commish_tok))
        check(r.status_code == 403, "commissioner without commissioner mode cannot reorder an AI team")
        check(client.put(f"/teams/{team_ids['fb']}/pecking-order", json=good, headers=hdr(owner_tok)).status_code == 400,
              "non-NHL team update is rejected")
        with SessionLocal() as db:
            check(db.query(TeamPeckingOrder).count() == 0, "rejected requests wrote nothing")

        # ---- successful full ranking ---------------------------------------------------------
        r = client.put(url, json=good, headers=hdr(owner_tok))
        check(r.status_code == 200, "owner sets a full 24-player pecking order")
        groups = {g["position_group"]: g for g in r.json()["groups"]}
        check([len(groups[k]["ranked"]) for k in "CWDG"] == [4, 8, 8, 4], "4 C, 8 W, 8 D, 4 G ranked")
        check([p["player_id"] for p in groups["W"]["ranked"]] == my["W"][:8]
              and [p["priority"] for p in groups["W"]["ranked"]] == list(range(1, 9)), "priorities are 1..n in the order given")
        check([p["player_id"] for p in groups["C"]["unranked"]] == [my["C"][4]] and len(groups["D"]["unranked"]) == 1,
              "roster players beyond the depth stay unranked")

        # ---- reordering, atomicity -----------------------------------------------------------
        before = client.get(url).json()
        swapped = list(reversed(my["W"][:8]))
        r = client.put(url, json={"assignments": {"W": swapped}}, headers=hdr(owner_tok))
        after = {g["position_group"]: g for g in r.json()["groups"]}
        check([p["player_id"] for p in after["W"]["ranked"]] == swapped, "reordering one group re-ranks it")
        check(after["C"] == {g["position_group"]: g for g in before["groups"]}["C"] and
              after["G"] == {g["position_group"]: g for g in before["groups"]}["G"], "groups not named are untouched")
        # swap in an unranked player and drop one
        newc = [my["C"][4], my["C"][0], my["C"][1]]
        r = client.put(url, json={"assignments": {"C": newc}}, headers=hdr(owner_tok))
        check([p["player_id"] for p in r.json()["groups"][0]["ranked"]] == newc and
              [p["player_id"] for p in r.json()["groups"][0]["unranked"]] == sorted(set(my["C"]) - set(newc), key=lambda i: (f"Mine C{my['C'].index(i) + 1}", i)),
              "shorter lists and promoting an unranked player work; dropped players become unranked")
        snapshot = client.get(url).json()

        bad_cases = {
            "duplicate within a group": {"W": [my["W"][0], my["W"][0]]},
            "duplicate across groups": {"C": [my["C"][0]], "G": [my["C"][0]]},
            "missing player id": {"C": [999999]},
            "free agent not on the roster": {"C": [free_agent_c]},
            "foreign roster player": {"C": [foreign_c]},
            "ineligible position (W in C)": {"C": [my["W"][0]]},
            "ineligible position (G in D)": {"D": [my["G"][0]]},
            "too deep (5 C)": {"C": my["C"][:5]},
            "too deep (9 D)": {"D": my["D"][:9]},
            "unknown group": {"X": [my["C"][0]]},
            "empty request": {},
        }
        for label, assignments in bad_cases.items():
            r = client.put(url, json={"assignments": assignments}, headers=hdr(owner_tok))
            check(r.status_code == 400, f"rejected: {label}")
        # a request with a valid group AND an invalid group must not apply the valid part
        r = client.put(url, json={"assignments": {"W": list(reversed(swapped)), "C": [foreign_c]}}, headers=hdr(owner_tok))
        check(r.status_code == 400 and client.get(url).json() == snapshot, "invalid mixed update is atomic: nothing changed")

        # ---- database constraints ------------------------------------------------------------
        with SessionLocal() as db:
            asset_id = db.query(RosterEntry).filter_by(team_id=team_ids["mine"]).first().draftable_asset_id
            for label, row in {
                "priority beyond C depth": dict(position_group="C", priority=5),
                "priority 0": dict(position_group="W", priority=0),
                "bad group": dict(position_group="X", priority=1),
                "duplicate (team, group, priority)": dict(position_group="W", priority=1),
            }.items():
                db.add(TeamPeckingOrder(team_id=team_ids["mine"], draftable_asset_id=asset_id, **row))
                try:
                    db.flush()
                except IntegrityError:
                    db.rollback()
                    check(True, f"DB rejects {label}")
                else:
                    db.rollback()
                    raise AssertionError(f"DB accepted {label}")
            dup_asset = db.query(TeamPeckingOrder).filter_by(team_id=team_ids["mine"], position_group="G").first().draftable_asset_id
            db.add(TeamPeckingOrder(team_id=team_ids["mine"], draftable_asset_id=dup_asset, position_group="W", priority=8))
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                check(True, "DB rejects the same asset ranked twice for a team")
            else:
                db.rollback()
                raise AssertionError("DB accepted a duplicate asset")

        # ---- resolve endpoint, every position path -------------------------------------------
        client.put(url, json=good, headers=hdr(owner_tok))
        rurl = url + "/resolve"
        everyone_ids = [i for ids in my.values() for i in ids]

        def resolve(scheduled, goalies):
            r = client.post(rurl, json={"scheduled_player_ids": scheduled, "confirmed_goalie_ids": goalies})
            check(r.status_code == 200, "resolve responds 200")
            return r.json()

        out = resolve(everyone_ids, [my["G"][0]])
        counts = {g: len(v) for g, v in out["starters"].items()}
        check(counts == {"C": 1, "W": 2, "D": 2, "G": 1} and out["vacancies"] == [], "resolve: exactly 1 C, 2 W, 2 D, 1 G when eligible")
        check(out["starters"]["C"][0]["player_id"] == my["C"][0] and out["starters"]["W"][0]["priority"] == 1,
              "resolve returns the top-priority players with their priorities")
        out = resolve([i for i in everyone_ids if i not in (my["C"][0], my["C"][1], my["W"][0], my["D"][0], my["G"][0])], [my["G"][0], my["G"][1]])
        check([p["player_id"] for p in out["starters"]["C"]] == [my["C"][2]], "C vertical depth: C1 and C2 out -> C3")
        check([p["player_id"] for p in out["starters"]["W"]] == [my["W"][1], my["W"][2]], "W pooled depth: W1 out -> W2 and W3")
        check([p["player_id"] for p in out["starters"]["D"]] == [my["D"][1], my["D"][2]], "D pooled depth: D1 out -> D2 and D3")
        check([p["player_id"] for p in out["starters"]["G"]] == [my["G"][1]], "G vertical depth: confirmed G2 starts when G1 is out")
        out = resolve(everyone_ids, [my["G"][3]])
        check([p["player_id"] for p in out["starters"]["G"]] == [my["G"][3]], "an unconfirmed higher goalie is passed over for the confirmed G4")
        out = resolve(everyone_ids, [])
        check(out["starters"]["G"] == [] and out["vacancies"] == [{"position_group": "G", "slot": 1, "reason": "NO_CONFIRMED_GOALIE"}],
              "no confirmed goalie -> explicit vacancy, other positions still fill")
        out = resolve([], [])
        check(len(out["vacancies"]) == 6 and {v["reason"] for v in out["vacancies"]} == {"NO_ACTIVE_PLAYER"}, "nobody scheduled -> six vacancies")
        out = resolve([foreign_c, free_agent_c], [])
        check(out["starters"]["C"] == [], "ids that are not on this team's ranking never start")
        # reordering changes who starts
        client.put(url, json={"assignments": {"W": [my["W"][5], my["W"][4]] + my["W"][:4] + my["W"][6:8]}}, headers=hdr(owner_tok))
        out = resolve(everyone_ids, [my["G"][0]])
        check([p["player_id"] for p in out["starters"]["W"]] == [my["W"][5], my["W"][4]], "a reorder changes the resolved starters")

        # ---- incomplete rosters --------------------------------------------------------------
        surl = f"/teams/{team_ids['sparse']}/pecking-order"
        r = client.put(surl, json={"assignments": {"C": [sparse_c], "W": [sparse_w]}}, headers=hdr(owner_tok))
        check(r.status_code == 200, "a sparse roster can rank the few players it has")
        r = client.post(surl + "/resolve", json={"scheduled_player_ids": [sparse_c, sparse_w], "confirmed_goalie_ids": []}).json()
        check([p["player_id"] for p in r["starters"]["C"]] == [sparse_c] and [p["player_id"] for p in r["starters"]["W"]] == [sparse_w],
              "sparse roster starts the players it has")
        check({(v["position_group"], v["slot"], v["reason"]) for v in r["vacancies"]}
              == {("W", 2, "INSUFFICIENT_RANKED"), ("D", 1, "NO_RANKED_PLAYERS"), ("D", 2, "NO_RANKED_PLAYERS"),
                  ("G", 1, "NO_RANKED_PLAYERS")}, "sparse roster reports explicit vacancy reasons")
        r = client.post(f"/teams/{team_ids['fb']}/pecking-order/resolve", json={"scheduled_player_ids": []})
        check(r.status_code == 400, "resolve rejects a non-NHL team")

        # a player removed from the roster after ranking is ignored by resolve and the read
        with SessionLocal() as db:
            asset = db.query(DraftableAsset).filter_by(player_id=sparse_c).one()
            db.query(RosterEntry).filter_by(team_id=team_ids["sparse"], draftable_asset_id=asset.id).delete()
            db.commit()
        r = client.post(surl + "/resolve", json={"scheduled_player_ids": [sparse_c, sparse_w]}).json()
        check(r["starters"]["C"] == [] and any(v["position_group"] == "C" and v["reason"] == "NO_RANKED_PLAYERS" for v in r["vacancies"]),
              "a player who left the roster no longer starts")
        engine.dispose()  # release the SQLite file so the temp directory can be removed

    print("\nAll NHL pecking order checks passed.")


if __name__ == "__main__":
    main()
