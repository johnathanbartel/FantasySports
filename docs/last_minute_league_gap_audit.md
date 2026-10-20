# Last Minute League: gap audit (FS-037)

The owner's 10-team NBA keeper league ("Last Minute League", ESPN league 395243481, season 2027, settings read from ESPN's
mSettings view on 2026-10-19). Its rookie draft runs on Sleeper. Goal: the platform replaces both.

`scripts/seed_last_minute_league.py` builds the league in a throwaway DB and prints a `SKIP` line for every Missing row below.
No ESPN, Sleeper or other external service was called for this audit; ESPN values come from the ticket.

## Settings

| Setting (ESPN) | Status | What backs it / what is missing |
|---|---|---|
| Head-to-head points, 10 teams, one division | Supported | `LeagueCreateRequest.size`, NBA window-sport schedule (`series_schedule.py`), `standings_mode='win_pct'` for non-hockey |
| Scoring: PTS .5, 3PM .5, REB 1, AST 1, STL 2, BLK 2, TO -1, DD 1, TD 2 | Supported (new) | `imported_scoring.py` (`create_formula`, `score_stat_line`), `leagues.scoring_system='imported'`, `imported_scoring_items`. Needs box scores to produce real scores (see below) |
| Lineup slots PG SG SF PF C G F UTIL x3 (10 starters) | Supported (new) | `nba_reference.apply_league_layout` and `RosterSlotDefinition.league_id`; `lineups.league_slots` |
| BENCH 12 | Supported (new) | same |
| IR 3 | Partly supported | The slot exists, is never auto-filled and is not a roster spot (`league_features.roster_spots`). Missing: only an injured player may enter IR, and the IR spot frees a roster spot for a pickup. Injury data: `injuries.py`, `injury_sync.py` (NHL feed) |
| 22 non-IR roster spots | Supported (new) | `draft_rounds` = 22 from the layout |
| Lineup lock daily, per player at his own game start | Missing | `game_lock.py` locks a whole team's players for the window when its first game starts; it has no per-player kickoff lock for basketball |
| Roster lock at the first game of the scoring period | Missing | Not modelled |
| Undroppable list | Missing | No setting, table or check in `free_agency.py` / `waivers.py`. Needs a per-league (or commissioner-set) list of player ids that `drop` refuses |
| Keepers: 22 (the whole active roster) | Supported | `league_format='keeper'`, `keeper_limit` (`routers/leagues.py`, `season_rollover.py`) |
| Offseason: rookie draft only | Partly supported | `keeper_draft_style`, `offseason_draft_rounds`, `season_rollover.py`. Sleeper rules (rounds, order, pick trading) are TBD |
| Rookie-draft pick trading | Supported | `pick_trading.py` (`pick_trading_enabled`, `pick_trade_seasons`); whether Sleeper allows it is TBD |
| ESPN draft: offline, order set by hand | Supported | `offline_draft.py` + `draft_order_mode='manual'` (`draft_order.py`) |
| 17 one-week regular-season matchups | Partly supported | NBA series schedule is a round robin of 7-day series (`series_schedule.plan_series`): 10 teams give 9 series per cycle, not 17 weeks. A fixed week count or a repeat-until-N option is missing |
| 6-team playoffs | Supported | `playoff_teams=6` (`playoffs.py`); 6 teams get 2 byes automatically (standard bracket) |
| Playoff seeding by total points scored, no reseeding | Missing | Seeds follow the standings order (win % then `tiebreakers.py`). No "seed by points for" primary sort, and no switch for no-reseeding |
| Playoff rounds last 1, 1 and 2 weeks | Missing | `playoffs.py` decides who is in and who has a bye; it does not play rounds or give them lengths |
| Consolation ladder | Partly supported | `playoff_consolation` flag exists (consolation games never change elimination); the ladder itself is not played out |
| Regular-season matchup tie goes to more 3PM | Missing | `tiebreakers.py` keys are fixed (head_to_head, division_record, wins, goal_differential, goals_for, goals_against, coin_flip) and break *standings* ties; there is no per-matchup tiebreak and no stat tiebreak |
| Playoff ties: no tiebreaker | Partly supported | Nothing resolves a tied playoff game today either; the missing rule would be "a tie advances the higher seed" or similar, and the ESPN behaviour is not stated |
| Waivers WAIVERS_TRADITIONAL, 24 h, processed Sunday 8 AM | Partly supported | `acquisition_type='waivers'`, `waiver_hours=24`, `waiver_processing_days=[6]` (`waivers.py`). Processing time is midnight Pacific, not 8 AM |
| waiverOrderReset false (rolling order) | Missing | `waivers.py` orders by reverse goal differential and resets weekly. A rolling "winner goes to the back, never resets" order does not exist |
| No FAAB | Supported | `acquisition_type='waivers'` |
| Acquisition limit 7 per matchup | Supported | `acquisition_limit=7`, `acquisition_limit_period='series'` (`acquisition_limits.py`); for NBA the series is the 7-day matchup |
| Trade deadline Mon 2027-02-15 9:00 AM Pacific | Supported | `trade_deadline` (stored as naive UTC: 17:00) |
| Veto: 5 votes kill a trade | Partly supported | See below |
| 24-hour revision window | Supported | `trade_review_hours=24`, `trade_review_mode='league'` (`trade_review.py`) |
| No limit on trade count | Supported | No trade-count cap exists |

Counts (28 rows): Supported 14 (4 of them new in FS-037), Partly supported 7, Missing 7.

## The specific questions

**Veto count.** `trade_review.veto_majority` needs `n // 2 + 1` vetoes where `n` is the person-managed teams outside the trade.
In a 10-team league with two teams in the trade and the rest people, n = 8, so 5 vetoes, which matches ESPN here. It stops
matching when an AI team or a vacant team is in the league (n shrinks: 7 -> 4) and for trades that change n. A fixed 5 would
need a setting (`trade_veto_votes`, null = majority). Recommendation: only add it if the owner needs "5, always"; for a full
10-person league majority already equals 5.

**Tiebreak by a stat (3PM).** Missing. `tiebreakers.TIEBREAKERS` is a fixed key list and the standings use it for *season* ties.
ESPN's rule is per matchup: when the two teams' weekly totals tie, compare one stat. Needs (1) a matchup-level tiebreak
setting {stat_code}, (2) the weekly stat totals per team, which come from the same box scores as scoring. Without box scores a
stat tiebreak cannot be evaluated.

**Playoff round lengths 1/1/2 and seeding by total points.** Missing on both counts. `playoffs.py` returns the field, seeds and
byes only. Needs a playoff schedule table (round number, weeks, matchups) and a "seed by points for" standings mode; an
imported league also needs "no reseeding" (fixed bracket). The two-week final is a matchup that sums two periods.

**Per-matchup acquisition limit and waivers.** Per matchup is supported through the `series` period for NBA. Waiver type is
supported; processing time-of-day (8 AM) and the non-resetting order are not (see rows above).

**Undroppable list.** Missing; see row.

## What an ESPN adapter needs

Out of scope for FS-037; not built. Read-only, using ESPN's unofficial fantasy API (`lm-api-reads.fantasy.espn.com`, views
`mSettings`, `mTeam`, `mRoster`, `mMatchup`, `kona_player_info`, `mDraftDetail`, `mTransactions2`), which needs the league's
`espn_s2` and `SWID` cookies for a private league. Credentials must never be stored; use them once in a request and discard.

1. Map settings: `scoringSettings.scoringItems` -> `imported_scoring.create_formula`; `rosterSettings.lineupSlotCounts` ->
   `apply_league_layout`; schedule, playoff, waiver, trade and keeper settings -> the existing league columns. Unmapped
   stat ids already raise.
2. Map players: ESPN player id to our `Player`/`DraftableAsset`. For NBA no crosswalk is named in `league_import_research.md`
   (the nflverse crosswalk is football only); candidates are a name + team + position match with a manual review queue.
3. Import managers, teams, rosters (with keeper flags), the transaction history and matchup history into the Legacy era
   (`league_import_research.md` section 4).
4. Cookie handling and terms-of-use review: the ESPN API is unofficial.

## What NBA box-score ingestion needs

Per-player, per-game stat lines in the shape `imported_scoring.score_stat_line` takes (PTS, REB, AST, STL, BLK, TO, 3PM, plus
FGM/FGA/FTM/FTA if a later league scores them), keyed to a `DraftableAsset`, a game and a time. Needs: an NBA schedule
(Games with start times, used for per-player locks), real teams and players (the NBA `Universe` has none yet), a stats table
(`weekly_player_stats` or per-game rows), and `imported_scoring.week_stat_lines` pointed at it. Candidate sources, named only:
the NBA's stats site (`stats.nba.com` JSON endpoints, unofficial), the `nba_api` Python package, Basketball-Reference,
BALLDONTLIE (has a free tier and API key), and SportsDataIO / Sportradar (paid, licensed). Licensing is the deciding factor
for a commercial product; none was installed or called.

Also unknown: Sleeper's own API for the rookie draft is described as NFL-only in `league_import_research.md`, so importing an
NBA rookie draft from Sleeper may not be possible; the platform's own rookie draft is the replacement.

## Open questions for the owner

- Sleeper rookie draft: number of rounds, how the order is set (lottery, reverse standings, manual), whether picks trade.
- Does the league need a fixed veto count of 5, or does "majority of the others" do?
- ESPN tie rules for a *tied playoff game* (the setting is empty; confirm what ESPN actually does).
- Is the 8 AM Sunday waiver time important, or is midnight Pacific fine?

## Recommended order of follow-up tickets

1. NBA data foundation: real teams, players, schedule, and box-score ingestion (everything else depends on it).
2. Wire `week_stat_lines` to the stored box scores, so imported leagues produce real weekly scores, standings and matchups.
3. Matchup tiebreak by stat, "seed by points for" and a no-reseed option (small, contained, needed to settle the season).
4. Playoff schedule with round lengths (1/1/2 weeks), a two-week final, and the consolation ladder.
5. Per-player lineup lock and the roster lock (needs the schedule from step 1).
6. Waiver order modes (rolling, never resets) and processing time of day, plus the undroppable list.
7. IR rules (injured-only entry, frees a roster spot).
8. Optional fixed veto-vote count and a fixed regular-season length.
9. ESPN import adapter (after 1 to 4, so an imported league can actually be played), then the Sleeper question.
