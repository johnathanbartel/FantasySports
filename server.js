const http = require('http');
const fs = require('fs');
const path = require('path');
const { LeagueStore, HttpError } = require('./lib/store');

const PORT = process.env.PORT || 3000;
const PUBLIC_DIR = path.join(__dirname, 'public');

const MIME_TYPES = {
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.css': 'text/css',
};

function createServer(store = new LeagueStore()) {
  return http.createServer((req, res) => {
    const url = new URL(req.url, `http://${req.headers.host}`);

    if (url.pathname.startsWith('/api/')) {
      return handleApi(req, res, url, store);
    }

    return handleStatic(req, res, url);
  });
}

function handleStatic(req, res, url) {
  let filePath = url.pathname === '/' ? '/index.html' : url.pathname;
  filePath = path.join(PUBLIC_DIR, filePath);

  if (!filePath.startsWith(PUBLIC_DIR)) {
    res.writeHead(403);
    return res.end('Forbidden');
  }

  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      return res.end('Not found');
    }
    const ext = path.extname(filePath);
    res.writeHead(200, { 'Content-Type': MIME_TYPES[ext] || 'application/octet-stream' });
    res.end(data);
  });
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', (chunk) => {
      body += chunk;
      if (body.length > 1e6) {
        req.destroy();
        reject(new HttpError(413, 'Payload too large.'));
      }
    });
    req.on('end', () => {
      if (!body) return resolve({});
      try {
        resolve(JSON.parse(body));
      } catch {
        reject(new HttpError(400, 'Invalid JSON body.'));
      }
    });
    req.on('error', reject);
  });
}

function sendJson(res, status, payload) {
  const data = JSON.stringify(payload);
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(data);
}

async function handleApi(req, res, url, store) {
  const parts = url.pathname.split('/').filter(Boolean); // ['api', 'leagues', ...]

  try {
    if (req.method === 'POST' && parts.length === 2 && parts[1] === 'leagues') {
      const { name, leagueSize } = await readJsonBody(req);
      const league = store.createLeague(name, leagueSize);
      return sendJson(res, 201, league);
    }

    if (req.method === 'GET' && parts.length === 3 && parts[1] === 'leagues') {
      const league = store.getLeague(parts[2]);
      return sendJson(res, 200, league);
    }

    if (
      req.method === 'POST' &&
      parts.length === 4 &&
      parts[1] === 'leagues' &&
      parts[3] === 'roster-size'
    ) {
      const { rosterSize } = await readJsonBody(req);
      const league = store.setRosterSize(parts[2], rosterSize);
      return sendJson(res, 200, league);
    }

    if (
      req.method === 'POST' &&
      parts.length === 5 &&
      parts[1] === 'leagues' &&
      parts[3] === 'teams' &&
      parts[4] === 'placeholder'
    ) {
      const league = store.addPlaceholderTeam(parts[2]);
      return sendJson(res, 201, league);
    }

    return sendJson(res, 404, { error: 'Not found.' });
  } catch (err) {
    if (err instanceof HttpError) {
      return sendJson(res, err.status, { error: err.message });
    }
    console.error(err);
    return sendJson(res, 500, { error: 'Internal server error.' });
  }
}

if (require.main === module) {
  const server = createServer();
  server.listen(PORT, () => {
    console.log(`Fantasy Sports league server listening on http://localhost:${PORT}`);
  });
}

module.exports = { createServer };
