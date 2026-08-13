const test = require('node:test');
const assert = require('node:assert/strict');
const {
  LeagueStore,
  HttpError,
  DEFAULT_LEAGUE_SIZE,
  DEFAULT_ROSTER_SIZE,
  STARTER_COUNT,
} = require('../lib/store');

test('createLeague creates a league with default league size and no roster size yet', () => {
  const store = new LeagueStore();
  const league = store.createLeague('The Gridiron Gurus');

  assert.equal(league.name, 'The Gridiron Gurus');
  assert.equal(league.leagueSize, DEFAULT_LEAGUE_SIZE);
  assert.equal(league.rosterSize, null);
  assert.deepEqual(league.teams, []);
  assert.ok(league.id);
});

test('createLeague rejects empty names', () => {
  const store = new LeagueStore();
  assert.throws(() => store.createLeague(''), HttpError);
  assert.throws(() => store.createLeague('   '), HttpError);
  assert.throws(() => store.createLeague(undefined), HttpError);
});

test('createLeague accepts a custom league size', () => {
  const store = new LeagueStore();
  const league = store.createLeague('League A', 8);
  assert.equal(league.leagueSize, 8);
});

test('createLeague rejects out-of-range league sizes', () => {
  const store = new LeagueStore();
  assert.throws(() => store.createLeague('League A', 1), HttpError);
  assert.throws(() => store.createLeague('League A', 33), HttpError);
  assert.throws(() => store.createLeague('League A', 'abc'), HttpError);
});

test('setRosterSize defaults to the standard 20-player roster', () => {
  const store = new LeagueStore();
  const league = store.createLeague('League A');
  const updated = store.setRosterSize(league.id, undefined);

  assert.equal(updated.rosterSize, DEFAULT_ROSTER_SIZE);
  assert.equal(updated.benchSize, DEFAULT_ROSTER_SIZE - STARTER_COUNT);
  assert.equal(updated.draftSize, DEFAULT_ROSTER_SIZE);
});

test('setRosterSize sets a valid custom roster size and derives bench/draft/positions', () => {
  const store = new LeagueStore();
  const league = store.createLeague('League A');
  const updated = store.setRosterSize(league.id, 24);

  assert.equal(updated.rosterSize, 24);
  assert.equal(updated.benchSize, 14);
  assert.equal(updated.draftSize, 24);
  assert.equal(updated.positions.starterCount, STARTER_COUNT);
  assert.equal(updated.positions.benchSize, 14);
  assert.equal(updated.positions.starters.length, 8);

  const qb = updated.positions.starters.find((pos) => pos.code === 'QB');
  assert.equal(qb.count, 1);
});

test('setRosterSize rejects sizes below the starting lineup requirement or above the cap', () => {
  const store = new LeagueStore();
  const league = store.createLeague('League A');
  assert.throws(() => store.setRosterSize(league.id, STARTER_COUNT - 1), HttpError);
  assert.throws(() => store.setRosterSize(league.id, 41), HttpError);
  assert.throws(() => store.setRosterSize(league.id, 'abc'), HttpError);
});

test('setRosterSize throws for unknown league', () => {
  const store = new LeagueStore();
  assert.throws(() => store.setRosterSize('nope', 10), HttpError);
});

test('addPlaceholderTeam requires roster size to be set first', () => {
  const store = new LeagueStore();
  const league = store.createLeague('League A');
  assert.throws(() => store.addPlaceholderTeam(league.id), HttpError);
});

test('addPlaceholderTeam adds an incrementally-named placeholder team', () => {
  const store = new LeagueStore();
  const league = store.createLeague('League A');
  store.setRosterSize(league.id, 12);

  const afterFirst = store.addPlaceholderTeam(league.id);
  assert.equal(afterFirst.teams.length, 1);
  assert.equal(afterFirst.teams[0].name, 'Team 1');
  assert.equal(afterFirst.teams[0].isPlaceholder, true);

  const afterSecond = store.addPlaceholderTeam(league.id);
  assert.equal(afterSecond.teams.length, 2);
  assert.equal(afterSecond.teams[1].name, 'Team 2');
});

test('addPlaceholderTeam refuses to exceed the league size', () => {
  const store = new LeagueStore();
  const league = store.createLeague('League A', 2);
  store.setRosterSize(league.id, 10);

  store.addPlaceholderTeam(league.id);
  store.addPlaceholderTeam(league.id);
  assert.throws(() => store.addPlaceholderTeam(league.id), HttpError);
});

test('full end-to-end flow: create league, set roster size, add placeholder team', () => {
  const store = new LeagueStore();
  const league = store.createLeague('End To End League');
  store.setRosterSize(league.id, 10);
  const finalLeague = store.addPlaceholderTeam(league.id);

  assert.equal(finalLeague.name, 'End To End League');
  assert.equal(finalLeague.leagueSize, DEFAULT_LEAGUE_SIZE);
  assert.equal(finalLeague.rosterSize, 10);
  assert.equal(finalLeague.teams.length, 1);
  assert.equal(finalLeague.teams[0].name, 'Team 1');
});
