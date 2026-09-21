"""
The auction over HTTP - the surface the draft board frontend actually calls.

These go through FastAPI's TestClient against a real (in-memory) database, so
they exercise routing, request/response schemas, and the engine together
rather than the engine alone.
"""

from fantasy_sports.platform.db.models.core import (
    DraftableAsset,
    DraftPick,
    League,
    LeagueUniverse,
    Player,
    RealTeam,
    RosterEntry,
    Team,
    Universe,
)


def start_auction(client, seeded_league, **overrides):
    payload = {"league_id": seeded_league.league.id, "slots": 3}
    payload.update(overrides)
    response = client.post("/auctions/start", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_start_uses_the_sports_defaults(client, seeded_league):
    session = start_auction(client, seeded_league)

    assert session["status"] == "in_progress"
    assert session["total_rounds"] == 3
    # NCAAF defaults: 3-minute rounds, Large/Moderate/Small tiers.
    assert session["round_seconds"] == 180
    assert session["minimum_round_seconds"] == 180
    assert session["recruiting_period_seconds"] == 540
    assert [t["code"] for t in session["tiers"]] == ["large", "moderate", "small"]
    assert [t["starting_budget"] for t in session["tiers"]] == [300, 225, 150]
    assert session["current_round"]["round_number"] == 1
    assert session["current_round"]["status"] == "open"
    assert 0 < session["current_round"]["seconds_remaining"] <= 180


def test_start_rejects_a_window_under_the_floor(client, seeded_league):
    response = client.post(
        "/auctions/start",
        json={"league_id": seeded_league.league.id, "slots": 3, "round_seconds": 60},
    )

    assert response.status_code == 400
    assert "floor" in response.json()["detail"]


def test_start_accepts_a_total_recruiting_period(client, seeded_league):
    session = start_auction(client, seeded_league, recruiting_period_seconds=1800)

    assert session["round_seconds"] == 600  # 1800s across 3 slots
    assert session["recruiting_period_seconds"] == 1800


def test_start_accepts_league_specific_tiers(client, seeded_league):
    session = start_auction(
        client,
        seeded_league,
        tiers=[
            {"code": "blue_blood", "display_name": "Blue blood", "starting_budget": 500},
            {"code": "mid_major", "display_name": "Mid-major", "starting_budget": 250},
        ],
        default_tier_code="mid_major",
    )

    assert [t["code"] for t in session["tiers"]] == ["blue_blood", "mid_major"]

    budgets = client.get(f"/auctions/{session['id']}/budgets").json()
    assert {b["starting_budget"] for b in budgets} == {250}


def test_budgets_reflect_the_assigned_tiers(client, seeded_league):
    alpha, bravo, charlie = seeded_league.team_ids
    session = start_auction(
        client,
        seeded_league,
        team_tiers={str(alpha): "large", str(bravo): "moderate", str(charlie): "small"},
    )

    budgets = {b["team_id"]: b for b in client.get(f"/auctions/{session['id']}/budgets").json()}
    assert budgets[alpha]["starting_budget"] == 300
    assert budgets[alpha]["tier_display_name"] == "Large program"
    assert budgets[bravo]["starting_budget"] == 225
    assert budgets[charlie]["starting_budget"] == 150
    assert all(b["spent_budget"] == 0 for b in budgets.values())
    assert budgets[alpha]["remaining_budget"] == 300


def test_board_lists_defense_groups_individually(client, seeded_league):
    session = start_auction(client, seeded_league)

    board = client.get(f"/auctions/{session['id']}/board").json()
    names = {a["display_name"] for a in board}

    # Three separate Georgia defensive lines, not one bundled "Georgia D".
    assert {"Georgia DL", "Georgia LB", "Georgia DB"} <= names
    assert "Notre Dame DL" in names
    georgia_defense = [a for a in board if a["display_name"].startswith("Georgia ")]
    assert sorted(a["position"] for a in georgia_defense if a["asset_type"] == "defense_group") == [
        "DB",
        "DL",
        "LB",
    ]


def test_no_open_bid_is_readable_by_anyone(client, seeded_league):
    """Blind means blind. While the round is open, no request - however it is
    parameterised - can surface another owner's asset or amount. There is no
    auth yet, so a "show me my own bid" parameter would be readable by
    anyone, and reading a rival's bid and beating it by one would defeat the
    whole mechanic.
    """
    alpha, bravo, _ = seeded_league.team_ids
    session = start_auction(client, seeded_league)
    auction_id = session["id"]
    beck = seeded_league.player_assets["Carson Beck"].id

    for team_id, amount in ((alpha, 60), (bravo, 45)):
        response = client.post(
            f"/auctions/{auction_id}/bids",
            json={"team_id": team_id, "draftable_asset_id": beck, "amount": amount},
        )
        assert response.status_code == 200, response.text
        # The bidder does get their own bid echoed back at submission time.
        assert response.json()["team_id"] == team_id
        assert response.json()["amount"] == amount

    blind = client.get(f"/auctions/{auction_id}/rounds/1").json()
    assert blind["status"] == "open"
    assert blind["bid_count"] == 2  # how many owners have acted, nothing more
    assert blind["bids"] == []
    assert blind["contests"] == []

    # The old leak: asking for a specific team must not open a side door.
    for probe in ({"team_id": bravo}, {"team_id": alpha}):
        peek = client.get(f"/auctions/{auction_id}/rounds/1", params=probe).json()
        assert peek["bids"] == [], f"open bids leaked via {probe}"

    # Nothing anywhere else exposes them either while the round is open.
    assert client.get(f"/auctions/{auction_id}/results").json() == []

    # After resolution, everything is revealed at once.
    client.post(f"/auctions/{auction_id}/rounds/1/resolve", params={"force": True, "seed": 1})
    revealed = client.get(f"/auctions/{auction_id}/rounds/1").json()
    assert sorted(bid["team_id"] for bid in revealed["bids"]) == sorted([alpha, bravo])
    assert sorted(bid["amount"] for bid in revealed["bids"]) == [45, 60]


def test_a_timezone_aware_start_time_is_accepted(client, seeded_league):
    """`Date.toISOString()` emits a trailing Z; that must not 500."""
    response = client.post(
        "/auctions/start",
        json={
            "league_id": seeded_league.league.id,
            "slots": 2,
            "starts_at": "2030-01-01T12:00:00Z",
        },
    )
    assert response.status_code == 200, response.text
    session = response.json()
    assert session["status"] == "pending"
    assert session["current_round"] is None

    # And reading it back does not blow up on the naive/aware comparison.
    assert client.get(f"/auctions/{session['id']}").status_code == 200


def test_a_rejected_bid_does_not_un_start_the_auction(client, db, seeded_league):
    """Opening the Recruiting Period and placing a bid are separate
    transactions, so a bad bid can't roll the period back to pending."""
    alpha = seeded_league.team_ids[0]
    session = start_auction(client, seeded_league)
    auction_id = session["id"]

    bad = client.post(
        f"/auctions/{auction_id}/bids",
        json={
            "team_id": alpha,
            "draftable_asset_id": seeded_league.player_assets["Carson Beck"].id,
            "amount": 99999,
        },
    )
    assert bad.status_code == 400

    after = client.get(f"/auctions/{auction_id}").json()
    assert after["status"] == "in_progress"
    assert after["current_round"]["round_number"] == 1
    assert after["current_round"]["status"] == "open"


def test_a_second_live_auction_for_one_league_is_refused(client, seeded_league):
    start_auction(client, seeded_league)

    second = client.post("/auctions/start", json={"league_id": seeded_league.league.id})
    assert second.status_code == 409
    assert "already has auction" in second.json()["detail"]


def test_resolve_reveals_debits_and_advances(client, db, seeded_league):
    alpha, bravo, charlie = seeded_league.team_ids
    session = start_auction(
        client,
        seeded_league,
        team_tiers={str(alpha): "large", str(bravo): "moderate", str(charlie): "small"},
    )
    auction_id = session["id"]
    beck = seeded_league.player_assets["Carson Beck"].id
    georgia_dl = seeded_league.defense_assets["Georgia DL"].id

    client.post(
        f"/auctions/{auction_id}/bids",
        json={"team_id": alpha, "draftable_asset_id": beck, "amount": 90},
    )
    client.post(
        f"/auctions/{auction_id}/bids",
        json={"team_id": bravo, "draftable_asset_id": beck, "amount": 89},
    )
    client.post(
        f"/auctions/{auction_id}/bids",
        json={"team_id": charlie, "draftable_asset_id": georgia_dl, "amount": 20},
    )

    # A round still inside its window needs the commissioner's force flag.
    too_early = client.post(f"/auctions/{auction_id}/rounds/1/resolve")
    assert too_early.status_code == 409
    assert "still open" in too_early.json()["detail"]

    revealed = client.post(
        f"/auctions/{auction_id}/rounds/1/resolve", params={"force": True, "seed": 7}
    )
    assert revealed.status_code == 200, revealed.text
    body = revealed.json()

    # Reveal: every bid in the round is now visible at once.
    assert body["status"] == "resolved"
    assert sorted(b["team_id"] for b in body["bids"]) == sorted([alpha, bravo, charlie])
    assert {b["status"] for b in body["bids"]} == {"won", "lost"}
    outbid = next(b for b in body["bids"] if b["team_id"] == bravo)
    assert outbid["lost_reason"] == "outbid"

    contests = {c["draftable_asset_id"]: c for c in body["contests"]}
    assert contests[beck]["winning_team_id"] == alpha
    assert contests[beck]["winning_amount"] == 90
    assert contests[georgia_dl]["display_name"] == "Georgia DL"
    assert contests[georgia_dl]["position"] == "DL"

    # Debit.
    budgets = {b["team_id"]: b for b in client.get(f"/auctions/{auction_id}/budgets").json()}
    assert budgets[alpha]["remaining_budget"] == 210
    assert budgets[bravo]["remaining_budget"] == 225
    assert budgets[charlie]["remaining_budget"] == 130

    # Won assets are off the board and on the winners' rosters.
    board = client.get(f"/auctions/{auction_id}/board").json()
    assert beck not in {a["draftable_asset_id"] for a in board}
    assert "Georgia LB" in {a["display_name"] for a in board}
    assert (
        db.query(RosterEntry).filter(RosterEntry.draftable_asset_id == beck).one().team_id == alpha
    )

    # Advance.
    assert body["next_round_number"] == 2
    assert body["session_complete"] is False
    assert client.get(f"/auctions/{auction_id}").json()["current_round"]["round_number"] == 2


def test_full_recruiting_period_runs_end_to_end(client, db, seeded_league):
    """Three slots, three teams, every round resolved: the whole Recruiting
    Period start to finish through the API."""
    alpha, bravo, charlie = seeded_league.team_ids
    session = start_auction(client, seeded_league)
    auction_id = session["id"]

    rounds = [
        {
            alpha: (seeded_league.player_assets["Carson Beck"].id, 50),
            bravo: (seeded_league.player_assets["Riley Leonard"].id, 40),
            charlie: (seeded_league.player_assets["Trevor Etienne"].id, 30),
        },
        {
            alpha: (seeded_league.defense_assets["Georgia DL"].id, 25),
            bravo: (seeded_league.defense_assets["Georgia LB"].id, 20),
            charlie: (seeded_league.defense_assets["Georgia DB"].id, 15),
        },
        {
            alpha: (seeded_league.defense_assets["Notre Dame DL"].id, 10),
            bravo: (seeded_league.player_assets["Jeremiyah Love"].id, 10),
        },
    ]

    for round_number, bids in enumerate(rounds, start=1):
        for team_id, (asset_id, amount) in bids.items():
            response = client.post(
                f"/auctions/{auction_id}/bids",
                json={"team_id": team_id, "draftable_asset_id": asset_id, "amount": amount},
            )
            assert response.status_code == 200, response.text
        resolved = client.post(
            f"/auctions/{auction_id}/rounds/{round_number}/resolve",
            params={"force": True, "seed": 11},
        )
        assert resolved.status_code == 200, resolved.text

    final = client.get(f"/auctions/{auction_id}").json()
    assert final["status"] == "complete"
    assert final["current_round"] is None

    results = client.get(f"/auctions/{auction_id}/results").json()
    assert len(results) == 8  # 3 + 3 + 2 assets won
    assert [r["round_number"] for r in results] == [1, 1, 1, 2, 2, 2, 3, 3]

    # Every win is a draft pick and a roster entry in the shared draft tables,
    # so the existing pick-history endpoint sees the auction's results too.
    picks = client.get(f"/drafts/{session['draft_id']}/picks").json()
    assert len(picks) == 8
    assert all(p["draftable_asset_id"] is not None for p in picks)
    assert db.query(DraftPick).filter(DraftPick.draft_id == session["draft_id"]).count() == 8
    assert db.query(RosterEntry).count() == 8

    # Spend adds up to what was actually won.
    budgets = {b["team_id"]: b for b in client.get(f"/auctions/{auction_id}/budgets").json()}
    assert budgets[alpha]["spent_budget"] == 50 + 25 + 10
    assert budgets[bravo]["spent_budget"] == 40 + 20 + 10
    assert budgets[charlie]["spent_budget"] == 30 + 15

    # And the auction is closed for business.
    late = client.post(
        f"/auctions/{auction_id}/bids",
        json={
            "team_id": alpha,
            "draftable_asset_id": seeded_league.player_assets["Jeremiyah Love"].id,
            "amount": 5,
        },
    )
    assert late.status_code == 409


def test_tie_break_is_reported_to_the_client(client, seeded_league):
    alpha, bravo, _ = seeded_league.team_ids
    session = start_auction(client, seeded_league)
    auction_id = session["id"]
    beck = seeded_league.player_assets["Carson Beck"].id

    for team_id in (alpha, bravo):
        client.post(
            f"/auctions/{auction_id}/bids",
            json={"team_id": team_id, "draftable_asset_id": beck, "amount": 70},
        )

    body = client.post(
        f"/auctions/{auction_id}/rounds/1/resolve", params={"force": True, "seed": 5}
    ).json()

    contest = body["contests"][0]
    assert contest["tie_break_used"] is True
    assert sorted(contest["tied_team_ids"]) == sorted([alpha, bravo])
    assert contest["winning_team_id"] in (alpha, bravo)
    loser = next(b for b in body["bids"] if b["team_id"] != contest["winning_team_id"])
    assert loser["lost_reason"] == "tie_break"


def test_bad_bids_are_rejected_with_useful_errors(client, seeded_league):
    alpha, _, charlie = seeded_league.team_ids
    session = start_auction(
        client, seeded_league, team_tiers={str(charlie): "small"}
    )
    auction_id = session["id"]
    beck = seeded_league.player_assets["Carson Beck"].id

    over_budget = client.post(
        f"/auctions/{auction_id}/bids",
        json={"team_id": charlie, "draftable_asset_id": beck, "amount": 999},
    )
    assert over_budget.status_code == 400
    assert "remaining budget" in over_budget.json()["detail"]

    missing_asset = client.post(
        f"/auctions/{auction_id}/bids",
        json={"team_id": alpha, "draftable_asset_id": 99999, "amount": 5},
    )
    assert missing_asset.status_code == 404

    unknown_auction = client.post(
        "/auctions/9999/bids",
        json={"team_id": alpha, "draftable_asset_id": beck, "amount": 5},
    )
    assert unknown_auction.status_code == 404


def test_snake_draft_endpoints_still_work(client, seeded_league):
    """The auction is additive: the pre-existing snake draft path is
    untouched."""
    response = client.post("/drafts/start", json={"league_id": seeded_league.league.id})
    assert response.status_code == 200, response.text
    draft = response.json()

    board = client.get(f"/drafts/{draft['id']}/board").json()
    assert len(board) == 8

    pick = client.post(
        f"/drafts/{draft['id']}/pick",
        json={"draftable_asset_id": seeded_league.player_assets["Carson Beck"].id},
    )
    assert pick.status_code == 200, pick.text
    assert pick.json()["display_name"] == "Carson Beck"

def test_a_non_football_league_runs_the_same_engine(client, db, seeded_league):
    """The engine is sport-agnostic: a hockey league with its own tiers,
    slot count and window runs an auction with no football defaults
    registered and no engine changes."""
    nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL")
    db.add(nhl)
    db.flush()
    real_team = RealTeam(universe_id=nhl.id, name="Colorado Avalanche")
    db.add(real_team)
    db.flush()
    skater = Player(
        universe_id=nhl.id,
        real_team_id=real_team.id,
        full_name="Nathan MacKinnon",
        position="C",
    )
    db.add(skater)
    db.flush()
    asset = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=skater.id)
    db.add(asset)
    db.flush()

    league = League(
        name="Puck League",
        commissioner_user_id=seeded_league.commissioner.id,
        size=2,
        draft_rounds=2,
    )
    db.add(league)
    db.flush()
    db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id))
    teams = [
        Team(league_id=league.id, name="Pucks", owner_user_id=seeded_league.commissioner.id),
        Team(league_id=league.id, name="Sticks", is_ai_owned=True),
    ]
    db.add_all(teams)
    db.commit()

    # No NHL defaults are registered, so the request supplies its own.
    bare = client.post("/auctions/start", json={"league_id": league.id})
    assert bare.status_code == 400
    assert "send `tiers`" in bare.json()["detail"]

    response = client.post(
        "/auctions/start",
        json={
            "league_id": league.id,
            "round_seconds": 240,
            "tiers": [
                {"code": "original_six", "display_name": "Original Six", "starting_budget": 500},
                {"code": "expansion", "display_name": "Expansion", "starting_budget": 350},
            ],
            "default_tier_code": "expansion",
        },
    )
    assert response.status_code == 200, response.text
    session = response.json()
    assert session["total_rounds"] == 2  # from the league, not from football
    assert session["round_seconds"] == 240

    # And a round runs exactly as it does for football.
    bid = client.post(
        f"/auctions/{session['id']}/bids",
        json={"team_id": teams[0].id, "draftable_asset_id": asset.id, "amount": 120},
    )
    assert bid.status_code == 200, bid.text
    body = client.post(
        f"/auctions/{session['id']}/rounds/1/resolve", params={"force": True, "seed": 3}
    ).json()
    assert body["contests"][0]["display_name"] == "Nathan MacKinnon"
    assert body["contests"][0]["winning_amount"] == 120
