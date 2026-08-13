const MIN_ROSTER_SIZE = 1;
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

class LeagueStore {
  constructor() {
    this.leagues = new Map();
  }

  createLeague(name) {
    if (typeof name !== 'string' || name.trim().length === 0) {
      throw new HttpError(400, 'League name is required.');
    }

    const league = {
      id: randomId(),
      name: name.trim(),
      rosterSize: null,
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
    const size = Number(rosterSize);

    if (!Number.isInteger(size) || size < MIN_ROSTER_SIZE || size > MAX_ROSTER_SIZE) {
      throw new HttpError(
        400,
        `Roster size must be an integer between ${MIN_ROSTER_SIZE} and ${MAX_ROSTER_SIZE}.`
      );
    }

    league.rosterSize = size;
    return league;
  }

  addPlaceholderTeam(id) {
    const league = this.getLeague(id);

    if (league.rosterSize === null) {
      throw new HttpError(400, 'Roster size must be set before adding a team.');
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

module.exports = { LeagueStore, HttpError, MIN_ROSTER_SIZE, MAX_ROSTER_SIZE };
