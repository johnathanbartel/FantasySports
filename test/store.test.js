const test = require('node:test');
const assert = require('node:assert/strict');
const { LeagueStore, HttpError } = require('../lib/store');

test('createLeague creates a league with no roster size and no teams', () => {
  const store = new LeagueStore();
  const league = store.createLeague('The Gridiron Gurus');

  assert.equal(league.name, 'The Gridiron Gurus');
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

test('setRosterSize sets a valid roster size', () => {
  const store = new LeagueStore();
  const league = store.createLeague('League A');
  const updated = store.setRosterSize(league.id, 15);
  assert.equal(updated.rosterSize, 15);
});

test('setRosterSize rejects invalid sizes', () => {
  const store = new LeagueStore();
  const league = store.createLeague('League A');
  assert.throws(() => store.setRosterSize(league.id, 0), HttpError);
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

test('full end-to-end flow: create league, set roster size, add placeholder team', () => {
  const store = new LeagueStore();
  const league = store.createLeague('End To End League');
  store.setRosterSize(league.id, 10);
  const finalLeague = store.addPlaceholderTeam(league.id);

  assert.equal(finalLeague.name, 'End To End League');
  assert.equal(finalLeague.rosterSize, 10);
  assert.equal(finalLeague.teams.length, 1);
  assert.equal(finalLeague.teams[0].name, 'Team 1');
});
