const DEFAULT_LEAGUE_SIZE = 12;
const MIN_LEAGUE_SIZE = 2;
const MAX_LEAGUE_SIZE = 32;

// Standard starting lineup. Fixed shape — not customizable per league.
const STARTER_POSITIONS = [
  { code: 'QB', label: 'Quarterback', count: 1 },
  { code: 'RB', label: 'Running Back', count: 2 },
  { code: 'WR', label: 'Wide Receiver', count: 2 },
  { code: 'TE', label: 'Tight End', count: 1 },
  { code: 'FLEX', label: 'Flex', count: 1 },
  { code: 'DL', label: 'Defensive Line', count: 1 },
  { code: 'LB', label: 'Linebacker', count: 1 },
  { code: 'DB', label: 'Defensive Back', count: 1 },
];
const STARTER_COUNT = STARTER_POSITIONS.reduce((sum, pos) => sum + pos.count, 0); // 10

const DEFAULT_ROSTER_SIZE = 20; // 10 starters + 10 bench
const MIN_ROSTER_SIZE = STARTER_COUNT; // must fit the full starting lineup
const MAX_ROSTER_SIZE = 40;

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function randomId() {
  return Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
}

function coerceInt(value, fallback) {
  if (value === undefined || value === null || value === '') {
    return fallback;
  }
  return Number(value);
}

class LeagueStore {
  constructor() {
    this.leagues = new Map();
  }

  createLeague(name, leagueSize) {
    if (typeof name !== 'string' || name.trim().length === 0) {
      throw new HttpError(400, 'League name is required.');
    }

    const size = coerceInt(leagueSize, DEFAULT_LEAGUE_SIZE);
    if (!Number.isInteger(size) || size < MIN_LEAGUE_SIZE || size > MAX_LEAGUE_SIZE) {
      throw new HttpError(
        400,
        `League size must be an integer between ${MIN_LEAGUE_SIZE} and ${MAX_LEAGUE_SIZE}.`
      );
    }

    const league = {
      id: randomId(),
      name: name.trim(),
      leagueSize: size,
      rosterSize: null,
      benchSize: null,
      draftSize: null,
      positions: null,
      teams: [],
      createdAt: new Date().toISOString(),
    };

    this.leagues.set(league.id, league);
    return league;
  }

  getLeague(id) {
    const league = this.leagues.get(id);
    if (!league) {
      throw new HttpError(404, 'League not found.');
    }
    return league;
  }

  setRosterSize(id, rosterSize) {
    const league = this.getLeague(id);
    const size = coerceInt(rosterSize, DEFAULT_ROSTER_SIZE);

    if (!Number.isInteger(size) || size < MIN_ROSTER_SIZE || size > MAX_ROSTER_SIZE) {
      throw new HttpError(
        400,
        `Roster size must be an integer between ${MIN_ROSTER_SIZE} (enough for the full starting ` +
          `lineup) and ${MAX_ROSTER_SIZE}.`
      );
    }

    league.rosterSize = size;
    league.benchSize = size - STARTER_COUNT;
    league.draftSize = size; // draft length mirrors roster size, one pick per roster spot
    league.positions = {
      starters: STARTER_POSITIONS.map((pos) => ({ ...pos })),
      starterCount: STARTER_COUNT,
      benchSize: league.benchSize,
    };

    return league;
  }

  addPlaceholderTeam(id) {
    const league = this.getLeague(id);

    if (league.rosterSize === null) {
      throw new HttpError(400, 'Roster size must be set before adding a team.');
    }

    if (league.teams.length >= league.leagueSize) {
      throw new HttpError(
        400,
        `League is full (${league.teams.length} of ${league.leagueSize} teams).`
      );
    }

    const team = {
      id: randomId(),
      name: `Team ${league.teams.length + 1}`,
      isPlaceholder: true,
    };

    league.teams.push(team);
    return league;
  }
}

module.exports = {
  LeagueStore,
  HttpError,
  DEFAULT_LEAGUE_SIZE,
  MIN_LEAGUE_SIZE,
  MAX_LEAGUE_SIZE,
  DEFAULT_ROSTER_SIZE,
  MIN_ROSTER_SIZE,
  MAX_ROSTER_SIZE,
  STARTER_POSITIONS,
  STARTER_COUNT,
};
