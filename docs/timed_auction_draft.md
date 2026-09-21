# Timed Auction Draft — round resolution

The platform's replacement for the snake draft. A **Recruiting Period** is
divided into per-slot **rounds**; in each round every owner submits **one
blind bid** from the revealed pool; when the round's window closes all bids
are revealed at once, winners are debited, and the auction advances to the
next slot.

Built and validated on college football first, but nothing in the engine is
football-shaped — round count, window length, budget tiers, and minimum bid
are all stored per session, so another sport supplies values, not code.

Code map:

- `src/fantasy_sports/platform/auction/config.py` — `AuctionConfig`,
  `TierConfig`, and the round-window math (`plan_round_windows`).
- `src/fantasy_sports/platform/auction/engine.py` — bidding and round
  resolution.
- `src/fantasy_sports/platform/db/models/auction.py` — the six new tables.
- `src/fantasy_sports/api/routers/auction.py` — the HTTP surface.
- `src/fantasy_sports/sports/football/college/auction.py` — NCAAF's default
  values (and the file another sport copies the shape of).

## The cycle

Each round runs `bid → reveal → debit → advance`, and `resolve_round()`
performs the last three as one transaction.

**Bid.** While a round's window is open, each owner may submit one bid: one
`draftable_assets` row, one integer amount. Re-submitting replaces that
owner's bid rather than adding a second — a bid nobody else can see is not
information anyone could have acted on, so changing it costs nothing. A bid
is rejected if it is below the league's minimum, above the owner's remaining
budget, on an asset already won in this session, or on an asset outside the
league's universe(s). One-bid-per-owner-per-round is also enforced by a
unique constraint on `auction_bids`.

**Reveal.** Bids are invisible until the round resolves. While a round is
open, `GET /auctions/{id}/rounds/{n}` returns a bid *count* and nothing else —
no asset, no amount, not even for the caller's own bid. That last part is
deliberate: with no auth in the application yet, any "show me my own bid"
parameter is a parameter every other owner can point at you, and reading a
rival's blind bid and beating it by one is exactly what this mechanic exists
to prevent. An owner sees their own bid in the response to the bid they
submitted, and the board remembers it client-side for the rest of the round;
serving it on request can come back once there is an authenticated user.
Once the round resolves, the same endpoint returns every bid at once.

**Debit.** Before amounts are compared, bids that cannot be honoured are
screened out and cost their owner nothing: the asset was already won earlier
in the auction, the bidder already rosters it, or the bid no longer fits
their remaining budget. The survivors are grouped by asset. Each asset goes
to its highest bidder, whose budget is debited by exactly their bid — losing
bidders pay nothing. A round therefore has as many outcomes as it had
distinct assets bid on: a twelve-team league can produce twelve separate
winners in one round, or one winner and eleven losers if everyone piles onto
the same player. Each win is written into the *existing* draft tables — a
`draft_picks` row plus a `roster_entries` row (`acquired_via="auction"`) — so
the already-built draft board, pick history, and roster endpoints see auction
results with no special-casing, and the won asset drops out of the pool by
the same query that removes a snake pick.

**Advance.** The round is marked resolved and the next round opens *at the
moment of resolution*, with every later round's window sliding along behind
it so the period stays gapless. After the last slot the session and its
backing draft are both marked `complete`.

## Round window math

A Recruiting Period is `slots × round_seconds`, where `slots` defaults to the
league's `draft_rounds`. The window can be set either way round:

- **Set the window directly** (`round_seconds`). Below the floor is a
  configuration error — a too-short window is a mistake worth surfacing, not
  something to silently correct.
- **Set the total period** (`recruiting_period_seconds`) and let the window
  fall out of it (`period / slots`). Here the floor *clamps* instead: a
  commissioner asking for 20 slots in 30 minutes gets 20 three-minute rounds
  and a longer period rather than an error.

**The floor is 3 minutes** (`MINIMUM_ROUND_SECONDS = 180`), platform-wide. A
sport may raise its own floor; `AuctionConfig` refuses to lower it.

Windows tile back to back — round N opens exactly when round N−1 closes — and
are stored as absolute timestamps, so a round resolved early simply moves the
tail rather than leaving a hole.

## Budget tiers

Owner budgets are tiered by program size. For college football the tiers are
Large / Moderate / Small with distinct starting budgets (300 / 225 / 150 —
see `sports/football/college/auction.py`), and a league can override the
tier list entirely at `POST /auctions/start`.

A sport that hasn't registered defaults yet (anything but NCAAF today) can still run an auction - the start request just has to send its own `tiers`.

Tiers are copied onto the session at creation, and each team's starting
budget is copied onto its `auction_budgets` row, so editing a league's tiers
next season cannot retroactively rewrite what a past auction actually ran
with. `AuctionConfig` rejects two tiers sharing a starting budget — tiers
exist to be different sizes.

## Tie-break: random "player choice"

When the top bid on an asset is tied, the contested player or defense group
**chooses an owner at random**. The tied owners are sorted by team id and one
is drawn from an injected `random.Random`, so a seeded run is reproducible —
which is what makes the mechanic testable and a disputed draft auditable.
The winner is debited; the others pay nothing and their bids are recorded
with `lost_reason="tie_break"` rather than `"outbid"`, so the reveal can
honestly show that they lost a coin flip and not an auction. The result row
also stores `tie_break_used` and the full list of tied team ids.

## Defensive position bidding: individually, per group

**Each defensive position group is its own auction line — never bundled by
real team.** Georgia DL, Georgia LB, and Georgia DB are three independent
assets that three different owners can win at three different prices, and
winning one does not sweep up the others.

This decision was already recorded before this engine was built, in
[`docs/data_contract.md`](data_contract.md) under *"Decision: Timed Auction
Draft defensive position bidding granularity"*, which builds on the earlier
*"Decision: DL/LB/DB draft pool"* in the same file. It was applied here, not
re-decided.

Concretely it means the engine needs **no bundling logic at all**: the atomic
bidding unit is one `draftable_assets` row, and for defense that row is
already one `defense_groups` row (one real team, one of DL/LB/DB). Defense
groups therefore travel through bid, reveal, debit, and tie-break on exactly
the same code path as a quarterback. Covered by
`tests/test_auction_engine.py::test_defense_groups_are_independent_bid_lines`
and `::test_two_owners_can_contest_a_single_defense_group`.

## Endpoints

| Action | Endpoint |
|---|---|
| Open a Recruiting Period | `POST /auctions/start` |
| Session state, current round, clock | `GET /auctions/{id}` |
| The revealed pool | `GET /auctions/{id}/board` |
| Every team's tier and money left | `GET /auctions/{id}/budgets` |
| Submit one blind bid | `POST /auctions/{id}/bids` |
| A round (blind while open, full reveal after) | `GET /auctions/{id}/rounds/{n}` |
| Reveal → debit → advance | `POST /auctions/{id}/rounds/{n}/resolve` |
| Everything won so far | `GET /auctions/{id}/results` |

`resolve` takes `force=true` (the commissioner's "everyone has bid, move on"
shortcut) and an optional `seed` for a reproducible tie-break. Without
`force`, a round can only be resolved after its window has expired.

The live UI is `web/static/auction-board.html`, linked from the existing
draft board.

## Known limits

**No bidder identity.** Any caller can bid as any team, exactly as any caller
can make the next snake pick — the "no auth yet" posture noted in
`docs/data_contract.md`. Blind bids are not *readable* by anyone, but they
are overwritable by an impostor until there are real users.

**No locking around resolution.** Two simultaneous `resolve` calls both pass
the "is this round open?" check. Budgets are still safe — one bid per owner
per round means a debit can only run once — and the uniqueness constraints on
`draft_picks` and `auction_round_results` stop the second writer, but it
fails as a 500 rather than a clean 409. A bid arriving in the same instant a
round resolves can also end up stranded in `submitted`: never charged, never
shown as a contest. Single-process SQLite makes this hard to hit today and
the snake path has the same shape, but a status-guarded UPDATE (or
`SELECT ... FOR UPDATE`) is the real fix once this runs on Postgres.

**One live auction per league, but not one draft.** Starting a second auction
while one is open is refused. A league that already ran a *snake* draft can
still open an auction over the same pool, because `GET /drafts/{id}/board`
scopes "already gone" per draft — pre-existing behaviour the demo seed relies
on. An owner cannot re-buy someone they already roster (the bid is refused,
and screened again at reveal), but two *different* teams can end up holding
the same asset across two drafts. Worth resolving before any league runs both
for real.

## Not in scope

There is no "reserve enough budget to fill your remaining slots" rule: an
owner may spend down to zero and simply win nothing in later rounds. Dual
Universe, Relegation, AI coaches, and AI drafting are explicitly out of scope
for this engine.

## Verification

```powershell
.venv-api\Scripts\python.exe -m pytest                       # unit + API tests
.venv-api\Scripts\python.exe scripts/auction_smoke_test.py   # full period, end to end
```
