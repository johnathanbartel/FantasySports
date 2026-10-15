# Competitor Feature Parity: ESPN, Yahoo, CBS

*Researched October 15, 2026. Football focus; applies to every sport we add.*

## Verdict

Matching the big three on **league operations, draft, roster/lineup, transactions, standings/playoffs, communication, and history** is the right bar, and it's achievable. "Everything they have" is not. About a quarter of their surface area is scoring customization, betting, cash games, and TV/editorial programming. The first two conflict with Principle 1, and the last two aren't platform features. Those are listed under **Excluded** below so they don't come back as backlog items.

## 2026 market changes that matter to us

- **NFL Fantasy is retired. ESPN is now the "Official Fantasy Game of the NFL"** and imports NFL Fantasy leagues (settings, members, schedules, history). New for 2026: Knockout Leagues, Fantasy Achievements, Gridiron Gauntlet, and an NFL Playoff Predictor.
- **Yahoo relaunched college fantasy football on July 9, 2026**, with all Power 4 teams plus Notre Dame. It adds a new **"Offense" (OFF) roster slot**: a whole team's offense with flat scoring (TD 3, first 300 yds 1.5, each additional 50 yds 0.25, FG 1, win +1, loss −1, turnover −2). **This is the direct competitor for our first sport.** Their unit-level offense slot approaches our idea of crediting the drive, but it uses flat bonuses. That makes our EPA attribution easy to explain as the difference.
- **Yahoo rebuilt its draft client** for web and mobile. The new client adds real-time Draft Grades, a Draft Board tab, light/dark mode, layout options that put either stats or social first, shareable recaps, and a pre-draft **Draft Tab** with a checklist, countdown, mock drafts, and commissioner reminders.
- **Elimination formats are now standard.** ESPN has Knockout and Yahoo has Death Leagues: the lowest score each week is eliminated, and that team's roster goes to FAAB waivers.

## Status legend

**Ours:** ✅ in code · 🟡 partial · ⬜ missing · ❓ unclear from code scan (Claude Code should verify)
**Priority:** **P0** = needed to run a full season in our own leagues without manual commissioner work · **P1** = parity polish · **P2** = premium-tier parity (park as spec) · **X** = excluded

Status comes from a scan of API routes and module names, not from running the app.

---

## 1. League creation and settings

| Feature | ESPN | Yahoo | CBS | Ours | Pri |
|---|---|---|---|---|---|
| Create private league / invite / join | ✓ | ✓ | ✓ | ✅ | — |
| Public leagues (matchmaking with strangers) | ✓ | ✓ | ✓ | ⬜ | X (internal-first) |
| League size options | ✓ | ✓ | ✓ | ✅ | — |
| Divisions / conferences | ✓ | ✓ | ✓ | ✅ (beyond parity) | — |
| Roster slot configuration | ✓ | ✓ | ✓ (Commish tier) | ✅ | — |
| Player pool options (e.g., FBS subset) | — | Fixed P4 + ND | ✓ | ❓ | P1 |
| League logo, name, custom URL | ✓ | ✓ | ✓ | ❓ | P1 |
| Auto-renew / reactivate for next season | ✓ (Auto-Reactivate) | ✓ | ✓ | ⬜ | P0 (before season 2) |
| Import league from another platform | ✓ (NFL Fantasy import) | — | — | ⬜ | P2 |
| League dues tracking | — | ✓ | ✓ (prize) | ⬜ | P1 (tracking only, no money handling) |

## 2. Commissioner tools

| Feature | ESPN | Yahoo | CBS | Ours | Pri |
|---|---|---|---|---|---|
| Commissioner dashboard | ✓ (LM Tools) | ✓ | ✓ | 🟡 (commissioner.html) | P0 |
| Edit any roster / lineup on behalf of a team | ✓ | ✓ | ✓ (Override) | ❓ | P0 |
| Edit waiver priority, transaction counts, FAAB balances | ✓ | ✓ | ✓ | ⬜ | P0 |
| Assign co-commissioner | ✓ | ✓ | ✓ | ❓ | P1 |
| Lock / transfer / remove teams | ✓ | ✓ | ✓ | 🟡 (assign) | P1 |
| Edit schedule and divisions | ✓ | ✓ | ✓ | ✅ | — |
| Edit league history | — | ✓ | ✓ | ⬜ | P1 |
| Auto-email the league when settings change | ✓ | ✓ | ✓ (league email) | ⬜ | P1 |
| Commissioner note / league announcements | ✓ | ✓ | ✓ | ❓ | P1 |
| Pre-draft reminders checklist for commish | — | ✓ (Draft Tab) | — | ⬜ | P1 |

## 3. Draft

| Feature | ESPN | Yahoo | CBS | Ours | Pri |
|---|---|---|---|---|---|
| Live snake draft | ✓ | ✓ | ✓ | ✅ | — |
| Auction draft | ✓ | ✓ | ✓ (Commish) | ✅ (blind per-round, beyond parity) | — |
| Autopick / auto-draft draft type | ✓ | ✓ | ✓ | ✅ | — |
| Offline draft (commish enters results) | ✓ | ✓ | ✓ | ⬜ | P1 |
| Pause / resume / reset draft | ✓ | ✓ | ✓ | 🟡 (no reset seen) | P1 |
| Set or randomize draft order | ✓ | ✓ | ✓ | ❓ | P0 |
| Personal pre-draft rankings / draft queue | ✓ | ✓ | ✓ | ❓ | P0 |
| Mock drafts vs. AI | ✓ | ✓ | ✓ | 🟡 (AI drafters exist; no user-facing mock lobby seen) | P1 |
| Draft board grid view | ✓ | ✓ | ✓ | ✅ | — |
| Player cards in draft room (stats, news) | ✓ | ✓ | ✓ | ❓ | P1 |
| Draft chat | ✓ | ✓ | ✓ | ⬜ | P1 |
| Real-time draft grades + post-draft grades | ✓ | ✓ | ✓ | ⬜ | P1 |
| Shareable draft recap | ✓ | ✓ | — | 🟡 (broadcast/announcements) | P1 |
| Pick suggestions ("Draft Scout") | ✓ | Plus | ✓ | ⬜ | P2 |
| Keeper draft rounds / keeper costs | ✓ | ✓ | ✓ | ⬜ | P0 (before season 2) |
| Trade draft picks | — | ✓ (in-draft) | ✓ (future picks) | ⬜ | P1 |
| Pre-draft hub (countdown, checklist, mocks) | — | ✓ | — | ⬜ | P1 |

## 4. Roster and lineup

| Feature | ESPN | Yahoo | CBS | Ours | Pri |
|---|---|---|---|---|---|
| Set lineup / bench swap | ✓ | ✓ | ✓ | ✅ | — |
| Auto-set optimal lineup | ✓ | Plus (Assistant GM) | ✓ | ✅ | — |
| Per-game lineup lock (player locks at his kickoff) | ✓ | ✓ | ✓ | ❓ | P0 |
| IR slots | ✓ | ✓ | ✓ | ✅ (injured list) | — |
| Injury alerts / status tags | ✓ | ✓ | ✓ | ✅ | — |
| Set lineups for future weeks | ✓ | ✓ | ✓ | ❓ | P1 |
| Bye-week / empty-slot warnings | ✓ | ✓ | ✓ | ❓ | P0 |
| Watchlist | ✓ | ✓ | ✓ | ⬜ | P1 |

## 5. Waivers and free agency

| Feature | ESPN | Yahoo | CBS | Ours | Pri |
|---|---|---|---|---|---|
| Add / drop free agents | ✓ | ✓ | ✓ | ✅ | — |
| Standard waivers (rolling or reverse-standings priority) | ✓ | ✓ | ✓ | ⬜ | **P0** |
| FAAB blind bidding with tiebreakers | ✓ | ✓ | ✓ | ⬜ (auction engine is reusable) | **P0** |
| Continuous waivers (no game-time adds) | ✓ | ✓ | ✓ | ⬜ | P1 |
| Configurable waiver period and processing days | ✓ | ✓ | ✓ | ⬜ | P0 |
| Acquisition limits (per week / season) | ✓ | ✓ | ✓ | ⬜ | P1 |
| Trending adds/drops, recommended pickups | ✓ | ✓ | ✓ | ⬜ | P1 |
| Full transaction log | ✓ | ✓ | ✓ | ✅ | — |

## 6. Trades

| Feature | ESPN | Yahoo | CBS | Ours | Pri |
|---|---|---|---|---|---|
| Propose / accept / reject / counter / cancel | ✓ | ✓ | ✓ | 🟡 (no counter seen) | P0 |
| Review: commissioner, league vote, or none | ✓ | ✓ | ✓ | 🟡 (vote exists) | P0 |
| Review period length | ✓ | ✓ | ✓ | ❓ | P0 |
| Trade deadline | ✓ | ✓ | ✓ | ❓ | **P0** |
| Trade block / "players I'll move" | ✓ | ✓ | ✓ | ⬜ | P1 |
| Trade analyzer / evaluator | ✓ | Plus (Trade Hub) | ✓ | ⬜ | P2 (must use *our* EPA values) |

## 7. Scoring, matchups, and live experience

| Feature | ESPN | Yahoo | CBS | Ours | Pri |
|---|---|---|---|---|---|
| Live in-game scoring | ✓ | ✓ | ✓ | ⬜ (final/historical data only, by decision) | P1, revisit after season 1 |
| Live in-game projections / win probability | ✓ | ✓ | — | ⬜ | P2 |
| Matchup view, swipe between league matchups | ✓ | ✓ | ✓ | ✅ (matchups) | — |
| Player-level score breakdown | ✓ | ✓ | ✓ | ✅ (week breakdown, beyond parity with EPA attribution) | — |
| Weekly projections | ✓ | ✓ | ✓ | ⬜ | P1 (needs EPA-based projection model) |
| Head-to-head points format | ✓ | ✓ | ✓ | ✅ | — |
| Customizable scoring / presets (PPR etc.) | ✓ | ✓ | ✓ | — | **X** |

## 8. Schedule, standings, and playoffs

| Feature | ESPN | Yahoo | CBS | Ours | Pri |
|---|---|---|---|---|---|
| Auto-generated schedule | ✓ | ✓ | ✓ | ✅ | — |
| Standings with tiebreakers | ✓ | ✓ | ✓ | ✅ / ❓ tiebreak rules | P0 |
| Playoff team count, weeks, and seeding options (division winners vs. overall) | ✓ | ✓ | ✓ | 🟡 | **P0** |
| Byes for top seeds | ✓ | ✓ | ✓ | ❓ | P0 |
| Consolation / toilet bowl bracket | ✓ | ✓ | ✓ | ⬜ | P1 |
| Playoff matchup tiebreakers | ✓ | ✓ | ✓ | ❓ | P0 |
| Power rankings | ✓ | ✓ | ✓ | ✅ (rankings) | — |

## 9. League formats

| Format | ESPN | Yahoo | CBS | Ours | Pri |
|---|---|---|---|---|---|
| Redraft | ✓ | ✓ | ✓ | ✅ | — |
| Keeper | ✓ | ✓ | ✓ | ⬜ | P0 (season 2) |
| Dynasty / contracts / future picks | — | — | ✓ | ⬜ | P2 |
| Elimination (Knockout / Death League) | ✓ | ✓ | — | ⬜ | P1. Cheap once FAAB exists; eliminated rosters go to FAAB |
| Salary-cap / weekly redraft | — | — | ✓ (salary cap draft) | ✅ (weekly redraft) | — |
| Season-long global contest (Gridiron Gauntlet) | ✓ | — | ✓ (grand prize) | ⬜ | X (internal-first) |

## 10. Communication and social

| Feature | ESPN | Yahoo | CBS | Ours | Pri |
|---|---|---|---|---|---|
| League chat (text, images, GIFs, DMs) | ✓ | ✓ (private leagues) | ✓ | ⬜ | **P0**, or deliberately rely on the group text |
| Message board / league posts with media | — | — | ✓ | ⬜ | P2 |
| Polls | — | — | ✓ | ⬜ | P2 |
| Automated weekly league recap posted to chat | ✓ | ✓ (Tuesdays: top performers, busts, bench points) | — | 🟡 (AI commentary exists) | P1. Strong fit with our EPA story and AI teams |
| Trash-talk reactions | ✓ | ✓ | ✓ | ⬜ | P2 |

## 11. Notifications

| Feature | ESPN | Yahoo | CBS | Ours | Pri |
|---|---|---|---|---|---|
| Email: draft reminders, trade offers, waiver results | ✓ | ✓ | ✓ | ⬜ | **P0** |
| Push notifications (mobile) | ✓ | ✓ | ✓ | ⬜ | P1 (web push or PWA) |
| Starter inactive / injury alerts | ✓ | ✓ | ✓ | 🟡 (in-app) | P0 (email) |

## 12. History, records, and achievements

| Feature | ESPN | Yahoo | CBS | Ours | Pri |
|---|---|---|---|---|---|
| Champions list / trophy room | ✓ | ✓ | ✓ | ✅ (championships) | — |
| Record book (high scores, best/worst games, season records) | ✓ | ✓ (2026) | ✓ | 🟡 (leaders, rivalries) | P1 |
| Head-to-head rivalry history | ✓ | ✓ | ✓ | ✅ | — |
| Achievements / badges | ✓ (2026) | — | — | ⬜ | P2 |

## 13. Research and player data

| Feature | ESPN | Yahoo | CBS | Ours | Pri |
|---|---|---|---|---|---|
| Player cards: game logs, career, bio, depth chart | ✓ | ✓ | ✓ | ❓ | P1 |
| Sortable stats / player search and filters | ✓ | ✓ | ✓ | 🟡 (stats endpoint) | P0 |
| Rankings and projections | ✓ | ✓ | ✓ | ✅ rankings / ⬜ projections | P1 |
| Player comparison tool | ✓ | Plus | ✓ | ⬜ | P2 |
| News feed tied to my roster | ✓ | ✓ | ✓ | ⬜ | P2 (needs a licensed news source) |

## 14. Platform and UX

| Feature | ESPN | Yahoo | CBS | Ours | Pri |
|---|---|---|---|---|---|
| Native iOS/Android apps | ✓ | ✓ | ✓ | ⬜ | P1 as a **mobile-responsive web app / PWA**; native is X for now |
| Personalized home dashboard ("to-dos today") | ✓ | ✓ | ✓ | ✅ (dashboard) | — |
| Light/dark mode | ✓ | ✓ | — | ❓ | P1 |
| Multi-sport accounts, team switcher | ✓ | ✓ | ✓ | ✅ (architecture) | — |
| Help center / rules page per league | ✓ | ✓ | ✓ | ⬜ | P0: a **scoring explainer** is required because EPA scoring is unfamiliar |

---

## Excluded from parity

| Their feature | Why we skip it |
|---|---|
| Customizable scoring, PPR/standard presets, custom stat categories | Principle 1: realistic-only scoring |
| Team D/ST and Yahoo's flat-scored "Offense" unit | We use DL/LB/DB groups with EPA attribution. Note Yahoo's OFF slot as a talking point, not a feature to copy. |
| Betting integration (ESPN BET "Find a Bet") | Not our product. Adds regulatory surface. |
| Cash prize leagues, paid DFS, global grand prizes | Internal-first, and these bring legal and payments overhead |
| TV shows, podcasts, analyst columns, draft kits | Media operations, not platform features. Our version is AI-generated recaps and the rumor mill (parked). |
| Pick'em, survivor, brackets, playoff predictor | Separate products. Park as their own spec if wanted. |
| Native mobile apps (for now) | A PWA covers our own leagues |

---

## Recommended build order

**Gate 1: run a full season with no manual commissioner work (P0).** This list closes the gaps a 12-team friend league will hit by week 3:

1. **Waivers + FAAB.** The biggest gap. Reuse the blind-auction engine for FAAB processing.
2. **Trade deadline, review period, and counter-offers.**
3. **Per-game lineup lock**, plus bye/empty-slot warnings.
4. **Playoffs**: team count, seeding, byes, and tiebreakers.
5. **Email notifications**: draft, trades, waivers, inactive starters.
6. **Draft queue/rankings and draft-order controls.**
7. **League chat**, or a written decision to rely on the existing group chat.
8. **Scoring explainer page.**

**Gate 2: parity polish (P1).** Elimination format (cheap once FAAB exists), weekly AI league recap, draft grades and recap, record book, consolation bracket, PWA/mobile pass, player cards.

**Gate 3: premium parity (P2), parked as specs.** Trade analyzer on EPA values, pick suggestions, projections-driven tools, achievements, dynasty/contracts.

**Before season 2:** auto-renew and keepers.

---

## Sources

- [ESPN named Official Fantasy Game of the NFL; 2026 features (Cord Cutters News)](https://cordcuttersnews.com/the-nfl-is-phasing-out-its-fantasy-app-and-handing-fantasy-football-to-espn-heres-what-players-need-to-know/)
- [ESPN Knockout Leagues (ESPN Press Room)](https://espnpressroom.com/us/press-releases/2026/07/espn-fantasy-football-introduces-knockout-leagues-format/)
- [ESPN 2025 app redesign features (SVG)](https://www.sportsvideo.org/2025/08/12/espn-fantasy-football-30th-anniversary-new-design-new-features-all-new-fantasy-app-for-2025/)
- [ESPN Waivers Overview](https://support.espn.com/hc/en-us/articles/360000041152-Waivers-Overview) · [ESPN Keeper settings](https://support.espn.com/hc/en-us/articles/360000070552) · [ESPN Pre-draft LM abilities](https://support.espn.com/hc/en-us/articles/46880183627796-Pre-Draft-League-Manager-Abilities)
- [ESPN Fantasy app (App Store)](https://apps.apple.com/us/app/espn-fantasy-sports-more/id555376968)
- [Yahoo college fantasy football launch (Saturday Down South)](https://www.saturdaydownsouth.com/news/college-football/yahoo-launches-college-fantasy-football-featuring-power-4-teams-and-notre-dame/)
- [Yahoo "Offense" roster spot (FantasyNerds)](https://www.fantasynerds.com/news/story/2026/08/24/yahoo-fantasy-introduces-new-offense-roster-spot-for-nfl-and-college-football-1604229)
- [Yahoo draft client rebuild (FantasyNerds)](https://fantasynerds.com/news/story/2026/08/20/yahoo-fantasy-overhauls-draft-experience-for-speed-and-usability-1601336) · [Yahoo Draft Tab](https://www.fantasynerds.com/news/story/2026/08/15/yahoo-fantasy-football-unveils-new-draft-tab-for-pre-draft-preparation-1597138) · [Yahoo Record Book / pre-draft rankings](https://fantasynerds.com/news/story/2026/08/16/yahoo-fantasy-app-adds-record-book-pre-draft-rankings-and-draft-ordering-1597865)
- [Yahoo commissioner tools](https://help.yahoo.com/kb/SLN7834.html) · [Yahoo playoff settings](https://help.yahoo.com/kb/SLN6528.html) · [Yahoo Death Leagues](https://help.yahoo.com/kb/death-sln37116.html) · [Yahoo Weekly League Recap](https://help.yahoo.com/kb/SLN37123.html) · [Yahoo Fantasy Plus](https://help.yahoo.com/kb/SLN35629.html) · [Yahoo app overview](https://help.yahoo.com/kb/SLN22574.html)
- [CBS feature chart by tier](https://www.cbssports.com/fantasy/football/games/chart) (may date from 2015) · [CBS fantasy games](https://cbssports.com/fantasy/games) · [CBS Commissioner pricing (FantasyPros)](https://fantasypros.com/reviews/league-management/cbs-sports)

Rows without a dedicated source (draft queue, IR slots, trade block, consolation brackets, etc.) are long-standing baseline features on all three platforms. They were not individually re-verified for 2026.
