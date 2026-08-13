const test = require('node:test');
const assert = require('node:assert/strict');
const { createServer } = require('../server');

async function withServer(fn) {
  const server = createServer();
  await new Promise((resolve) => server.listen(0, resolve));
  const { port } = server.address();
  const baseUrl = `http://localhost:${port}`;
  try {
    await fn(baseUrl);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

test('end-to-end HTTP flow: create league, set roster size, add placeholder team', async () => {
  await withServer(async (baseUrl) => {
    const createRes = await fetch(`${baseUrl}/api/leagues`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'Web League' }),
    });
    assert.equal(createRes.status, 201);
    const league = await createRes.json();
    assert.equal(league.name, 'Web League');
    assert.equal(league.leagueSize, 12);

    const rosterRes = await fetch(`${baseUrl}/api/leagues/${league.id}/roster-size`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rosterSize: 16 }),
    });
    assert.equal(rosterRes.status, 200);
    const afterRoster = await rosterRes.json();
    assert.equal(afterRoster.rosterSize, 16);
    assert.equal(afterRoster.benchSize, 6);
    assert.equal(afterRoster.draftSize, 16);
    assert.equal(afterRoster.positions.starterCount, 10);

    const teamRes = await fetch(`${baseUrl}/api/leagues/${league.id}/teams/placeholder`, {
      method: 'POST',
    });
    assert.equal(teamRes.status, 201);
    const finalLeague = await teamRes.json();
    assert.equal(finalLeague.teams.length, 1);
    assert.equal(finalLeague.teams[0].name, 'Team 1');

    const getRes = await fetch(`${baseUrl}/api/leagues/${league.id}`);
    assert.equal(getRes.status, 200);
    const fetched = await getRes.json();
    assert.deepEqual(fetched, finalLeague);
  });
});

test('rejects adding a placeholder team before roster size is set', async () => {
  await withServer(async (baseUrl) => {
    const createRes = await fetch(`${baseUrl}/api/leagues`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'No Roster League' }),
    });
    const league = await createRes.json();

    const teamRes = await fetch(`${baseUrl}/api/leagues/${league.id}/teams/placeholder`, {
      method: 'POST',
    });
    assert.equal(teamRes.status, 400);
  });
});

test('honors a custom league size and enforces it when adding teams', async () => {
  await withServer(async (baseUrl) => {
    const createRes = await fetch(`${baseUrl}/api/leagues`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'Small League', leagueSize: 2 }),
    });
    const league = await createRes.json();
    assert.equal(league.leagueSize, 2);

    await fetch(`${baseUrl}/api/leagues/${league.id}/roster-size`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rosterSize: 10 }),
    });

    const firstTeamRes = await fetch(`${baseUrl}/api/leagues/${league.id}/teams/placeholder`, {
      method: 'POST',
    });
    assert.equal(firstTeamRes.status, 201);

    const secondTeamRes = await fetch(`${baseUrl}/api/leagues/${league.id}/teams/placeholder`, {
      method: 'POST',
    });
    assert.equal(secondTeamRes.status, 201);

    const thirdTeamRes = await fetch(`${baseUrl}/api/leagues/${league.id}/teams/placeholder`, {
      method: 'POST',
    });
    assert.equal(thirdTeamRes.status, 400);
  });
});

test('serves the frontend index page', async () => {
  await withServer(async (baseUrl) => {
    const res = await fetch(`${baseUrl}/`);
    assert.equal(res.status, 200);
    const html = await res.text();
    assert.match(html, /Create a League/);
  });
});
