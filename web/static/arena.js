// Retro arena: a full-screen pixel-art rink / court / field / diamond fixed behind the page, with a
// very rough looping sim on it (passing and shooting, throwing, pitching and hitting). Pure canvas, no assets.
// It follows <html data-theme> (set by retro.js), so each sport page gets its own arena; hub gets none.
// Decorative only: pointer-events:none, paused when the tab is hidden, static if reduced motion is requested.
const Arena = (() => {
  'use strict';
  const H = 160;                                   // internal pixel height; width follows the window's aspect ratio
  const SPORTS = ['hockey', 'basketball', 'football', 'baseball'];
  const COLORS = { hockey: ['#e0202e', '#1d4fd8'], basketball: ['#d8202e', '#1b5fd1'], football: ['#ffffff', '#ffd23f'], baseball: ['#ffffff', '#c8472b'] };
  const store = {
    get: k => { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} },
  };
  let enabled = store.get('fm-arena') !== 'off';
  const reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  const rnd = (a, b) => a + Math.random() * (b - a), pick = a => a[Math.floor(Math.random() * a.length)];
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v)), hyp = Math.hypot, lerp = (a, b, k) => a + (b - a) * k;
  let cv = null, ctx = null, bgc = null, W = 0, theme = null, sim = null, raf = 0, last = 0, T = 0, flash = 0;
  let parts = [];

  // ---------- pixel drawing helpers ----------
  const R = (c, x, y, w, h, col) => { c.fillStyle = col; c.fillRect(Math.round(x), Math.round(y), w, h); };
  function ring(c, cx, cy, r, col, keep) {
    c.fillStyle = col; cx = Math.round(cx); cy = Math.round(cy);
    let x = r, y = 0, e = 1 - r;
    while (x >= y) {
      for (const [px, py] of [[x, y], [y, x], [-y, x], [-x, y], [-x, -y], [-y, -x], [y, -x], [x, -y]]) {
        const X = cx + px, Y = cy + py; if (!keep || keep(X, Y)) c.fillRect(X, Y, 1, 1);
      }
      y++; if (e < 0) e += 2 * y + 1; else { x--; e += 2 * (y - x) + 1; }
    }
  }
  function disc(c, cx, cy, r, col, keep) {
    c.fillStyle = col; cx = Math.round(cx); cy = Math.round(cy);
    for (let dy = -r; dy <= r; dy++) {
      const dx = Math.floor(Math.sqrt(r * r - dy * dy));
      if (!keep) c.fillRect(cx - dx, cy + dy, dx * 2 + 1, 1);
      else for (let x = -dx; x <= dx; x++) if (keep(cx + x, cy + dy)) c.fillRect(cx + x, cy + dy, 1, 1);
    }
  }
  function rrect(c, x, y, w, h, r, col) {          // rectangle with pixel-stepped rounded corners
    c.fillStyle = col;
    for (let j = 0; j < h; j++) {
      const d = j < r ? r - j : (j >= h - r ? j - (h - r - 1) : 0);
      const inset = d > 0 ? r - Math.floor(Math.sqrt(Math.max(0, r * r - (d - .5) * (d - .5)))) : 0;
      c.fillRect(x + inset, y + j, w - 2 * inset, 1);
    }
  }
  const lineH = (c, x, y, w, col) => R(c, x, y, w, 1, col), lineV = (c, x, y, h, col) => R(c, x, y, 1, h, col);

  // ---------- surfaces (drawn once per resize onto an offscreen canvas) ----------
  function drawHockey(c) {
    const m = 5, x0 = m, y0 = m, x1 = W - m, y1 = H - m, RW = x1 - x0, RH = y1 - y0, cx = (x0 + x1) / 2 | 0, cy = (y0 + y1) / 2 | 0;
    const gL = x0 + Math.round(RW * .065), gR = x1 - Math.round(RW * .065);
    R(c, 0, 0, W, H, '#15234d');
    rrect(c, x0 - 2, y0 - 2, RW + 4, RH + 4, 12, '#2c3e73');
    rrect(c, x0, y0, RW, RH, 10, '#f1faff');
    for (let i = 0; i < W * 1.2; i++) R(c, rnd(x0 + 8, x1 - 10), rnd(y0 + 3, y1 - 3), (Math.random() * 3 | 0) + 1, 1, '#e1eff9');
    const bl = x0 + Math.round(RW * .36), br = x0 + Math.round(RW * .64);
    R(c, bl - 1, y0, 3, RH, '#1d4fd8'); R(c, br - 1, y0, 3, RH, '#1d4fd8');
    R(c, cx - 1, y0, 2, RH, '#d8212f'); for (let y = y0; y < y1; y += 4) R(c, cx - 1, y, 2, 1, '#f6cfd3');
    lineV(c, gL, y0, RH, '#d8212f'); lineV(c, gR, y0, RH, '#d8212f');
    const rc = Math.round(H * .15); ring(c, cx, cy, rc, '#1d4fd8'); disc(c, cx, cy, 2, '#1d4fd8');
    const fr = Math.round(H * .1);
    for (const fx of [x0 + Math.round(RW * .2), x0 + Math.round(RW * .8)]) for (const fy of [cy - Math.round(RH * .27), cy + Math.round(RH * .27)]) { ring(c, fx, fy, fr, '#d8212f'); disc(c, fx, fy, 2, '#d8212f'); }
    disc(c, gL, cy, 10, '#bfe3ff', (x) => x >= gL); disc(c, gR, cy, 10, '#bfe3ff', (x) => x <= gR);
    ring(c, gL, cy, 10, '#d8212f', (x) => x >= gL); ring(c, gR, cy, 10, '#d8212f', (x) => x <= gR);
    R(c, gL - 4, cy - 5, 4, 10, '#c3ccdb'); R(c, gR + 1, cy - 5, 4, 10, '#c3ccdb');
    return { x0, y0, x1, y1, RW, RH, cx, cy, gL: gL + 3, gR: gR - 3 };
  }
  function drawBasketball(c) {
    const m = 9, x0 = m, y0 = m, x1 = W - m, y1 = H - m, RW = x1 - x0, RH = y1 - y0, cx = (x0 + x1) / 2 | 0, cy = (y0 + y1) / 2 | 0;
    for (let x = 0, i = 0; x < W; x += 9, i++) {
      R(c, x, 0, 9, H, i % 2 ? '#d49a58' : '#cb9250'); R(c, x, 0, 1, H, '#b9803f');
      for (let y = (i * 37) % 26; y < H; y += 26) R(c, x, y, 9, 1, '#b9803f');
    }
    c.fillStyle = 'rgba(60,25,5,.22)'; c.fillRect(0, 0, W, y0); c.fillRect(0, y1, W, H - y1); c.fillRect(0, y0, x0, RH); c.fillRect(x1, y0, W - x1, RH);
    const L = '#fff6e0';
    const pw = Math.round(RW * .17), ph = Math.round(RH * .38), hx = x0 + Math.round(RW * .05);
    R(c, x0, cy - ph / 2, pw, ph, '#2f5fc0'); R(c, x1 - pw, cy - ph / 2, pw, ph, '#2f5fc0');
    lineH(c, x0, y0, RW, L); lineH(c, x0, y1, RW, L); lineV(c, x0, y0, RH + 1, L); lineV(c, x1, y0, RH + 1, L); lineV(c, cx, y0, RH, L);
    ring(c, cx, cy, Math.round(H * .13), L); disc(c, cx, cy, 2, L);
    for (const s of [0, 1]) {
      const bx = s ? x1 : x0, dir = s ? -1 : 1, hxx = s ? x1 - (hx - x0) : hx, ftx = bx + dir * pw;
      lineV(c, ftx, cy - ph / 2, ph + 1, L); lineH(c, Math.min(bx, ftx), cy - ph / 2, pw, L); lineH(c, Math.min(bx, ftx), cy + ph / 2, pw, L);
      ring(c, ftx, cy, Math.round(ph * .42), L, X => (X - ftx) * dir > 0);
      const R3 = Math.round(RH * .47), cut = Math.round(R3 * .37);
      ring(c, hxx, cy, R3, L, X => (X - hxx) * dir > cut);
      const ly = Math.round(R3 * .93); const sx = dir > 0 ? bx : hxx + dir * cut;
      lineH(c, Math.min(sx, hxx + dir * cut), cy - ly, Math.abs(hxx + dir * cut - bx), L); lineH(c, Math.min(sx, hxx + dir * cut), cy + ly, Math.abs(hxx + dir * cut - bx), L);
      R(c, hxx - dir * 3, cy - 4, 1, 9, '#e9e9e9'); disc(c, hxx, cy, 2, '#ff6a13');
    }
    return { x0, y0, x1, y1, RW, RH, cx, cy, hL: hx, hR: x1 - (hx - x0) };
  }
  function drawFootball(c) {
    const m = 6, x0 = 4, y0 = m, x1 = W - 4, y1 = H - m, RW = x1 - x0, RH = y1 - y0, cx = (x0 + x1) / 2 | 0, cy = (y0 + y1) / 2 | 0;
    const ez = Math.round(RW * .085), fx0 = x0 + ez, fx1 = x1 - ez, playW = fx1 - fx0;
    R(c, 0, 0, W, H, '#1c5e2f');
    const seg = playW / 10;
    for (let i = 0; i < 10; i++) R(c, fx0 + Math.round(i * seg), y0, Math.ceil(seg), RH, i % 2 ? '#2b9144' : '#2f9d4a');
    R(c, x0, y0, ez, RH, '#1d7a38'); R(c, fx1, y0, ez, RH, '#1d7a38');
    for (let y = y0 + 2; y < y1; y += 4) { R(c, x0 + 1, y, ez - 2, 1, '#23863f'); R(c, fx1 + 1, y, ez - 2, 1, '#23863f'); }
    const W_ = '#f4fff4';
    lineH(c, x0, y0, RW, W_); lineH(c, x0, y1, RW, W_); lineV(c, x0, y0, RH + 1, W_); lineV(c, x1, y0, RH + 1, W_);
    for (let i = 0; i <= 10; i++) { const lx = fx0 + Math.round(i * seg); R(c, lx, y0, i === 0 || i === 10 ? 2 : 1, RH, W_); }
    for (let i = 0; i < 50; i++) { const lx = fx0 + Math.round(i * playW / 50); R(c, lx, y0 + Math.round(RH * .36), 1, 2, W_); R(c, lx, y0 + Math.round(RH * .62), 1, 2, W_); }
    ring(c, cx, cy, 9, W_); disc(c, cx, cy, 4, '#c99a00');
    for (const gx of [x0 - 1, x1]) { R(c, gx, cy - 7, 2, 15, '#ffd23f'); }
    return { x0, y0, x1, y1, RW, RH, cx, cy, ez, fx0, fx1, playW };
  }
  function drawBaseball(c) {
    const b = Math.round(H * .4), hx = Math.round(W / 2), hy = Math.round(H * .9), Rw = Math.round(H * .88);
    const home = { x: hx, y: hy }, first = { x: hx + Math.round(b * .7071), y: hy - Math.round(b * .7071) }, third = { x: hx - Math.round(b * .7071), y: first.y };
    const second = { x: hx, y: hy - Math.round(b * 1.4142) }, mound = { x: hx, y: hy - Math.round(b * .95) };
    R(c, 0, 0, W, H, '#232d49');
    for (let i = 0; i < W * 2; i++) R(c, rnd(0, W), rnd(0, H * .9), 1, 1, pick(['#e0202e', '#ffd23f', '#4a5a85', '#f4f1e4', '#1d4fd8']));
    const img = c.getImageData(0, 0, W, H), d = img.data;
    const setPx = (x, y, hex) => { const i = (y * W + x) * 4; d[i] = parseInt(hex.slice(1, 3), 16); d[i + 1] = parseInt(hex.slice(3, 5), 16); d[i + 2] = parseInt(hex.slice(5, 7), 16); d[i + 3] = 255; };
    const dcx = hx, dcy = hy - Math.round(b * .78), dirtR = Math.round(b * 0.9), dmx = hx, dmy = hy - Math.round(b * .7071), inner = Math.round(b * .7071) - 4;
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      const dh = hyp(x - hx, y - hy);
      if (dh > Rw) continue;
      let col = ((dh / 9) | 0) % 2 ? '#3aa655' : '#34994f';
      if (dh > Rw - 5) col = '#b8935a';
      else if (hyp(x - dcx, y - dcy) <= dirtR) col = (Math.abs(x - dmx) + Math.abs(y - dmy) < inner) ? (((dh / 9) | 0) % 2 ? '#3aa655' : '#34994f') : '#c8955a';
      setPx(x, y, col);
    }
    c.putImageData(img, 0, 0);
    ring(c, hx, hy, Rw, '#17452a', (x, y) => y < hy + 1); ring(c, hx, hy, Rw + 1, '#17452a', (x, y) => y < hy + 1);
    for (let i = 4; i < Rw; i++) { const o = Math.round(i * .7071); R(c, hx + o, hy - o, 1, 1, '#f6fff6'); R(c, hx - o, hy - o, 1, 1, '#f6fff6'); }
    disc(c, mound.x, mound.y, 4, '#b98249'); R(c, mound.x - 1, mound.y, 3, 1, '#f6fff6');
    disc(c, hx, hy, 5, '#c8955a', (x, y) => y >= hy - 2 || true);
    for (const p of [first, second, third]) R(c, p.x - 1, p.y - 1, 3, 3, '#ffffff');
    R(c, hx - 1, hy, 3, 2, '#ffffff');
    return { b, home, first, second, third, mound, Rw, cx: hx, cy: hy - b };
  }

  // ---------- sprites ----------
  const INK = '#1a1030', SKIN = '#f3c9a0';
  function person(c, p, col, o) {
    const x = Math.round(p.x), y = Math.round(p.y), step = p.moving ? (Math.floor(T * 9 + (p.seed || 0)) % 2) : 0;
    R(c, x - 2, y, 5, 1, 'rgba(0,0,0,.28)');
    R(c, x - 3, y - 10, 7, 10, INK);
    R(c, x - 1, y - 9, 3, 3, SKIN); R(c, x - 1, y - 9, 3, 1, o && o.hair || INK);
    R(c, x - 2, y - 6, 5, 3, col);
    R(c, x - 2, y - 3, 2, 2 - step, '#2b2b3b'); R(c, x + 1, y - 3, 2, 1 + step, '#2b2b3b');
    if (o && o.stick) R(c, p.face > 0 ? x + 3 : x - 5, y - 3, 3, 1, '#8b5a2b');
    if (o && o.armUp) R(c, x + (p.face > 0 ? 3 : -3), y - 11, 1, 4, col);
    if (o && o.bat) { const sw = o.swing > 0; R(c, x + 3, y - (sw ? 5 : 11), 1, 6, '#c8955a'); if (sw) R(c, x + 3, y - 5, 4, 1, '#c8955a'); }
  }
  function ballDraw(c, b, col, sz) {
    R(c, b.x - 1, b.y + 1, 3, 1, 'rgba(0,0,0,.3)');
    R(c, b.x - sz / 2, b.y - (b.z || 0) - sz / 2, sz, sz, col);
  }
  function moveToward(p, tx, ty, speed, dt) {
    const dx = tx - p.x, dy = ty - p.y, d = hyp(dx, dy);
    if (d < .6) { p.moving = false; return; }
    const s = Math.min(speed * dt, d); p.x += dx / d * s; p.y += dy / d * s; p.moving = true; if (Math.abs(dx) > .4) p.face = dx > 0 ? 1 : -1;
  }
  function confetti(x, y, n, cols) {
    for (let i = 0; i < n; i++) parts.push({ x, y, vx: rnd(-50, 50), vy: rnd(-80, -20), l: rnd(.5, 1.1), col: pick(cols) });
    flash = .3;
  }

  // ---------- sims ----------
  function invasion(kind, G, colors) {                // hockey and basketball: carry, pass, shoot, steal
    const hockey = kind === 'hockey', n = 5, pace = hockey ? 46 : 36, passSpd = hockey ? 150 : 120, shotSpd = 200, range = (hockey ? .3 : .27) * G.RW;
    const goalX = [hockey ? G.gR : G.hR, hockey ? G.gL : G.hL], dirs = [1, -1];
    const teams = [[], []], goalies = [null, null], all = [];
    const mk = (t, i) => ({ team: t, idx: i, x: 0, y: 0, face: dirs[t], moving: false, timer: rnd(.3, 1), seed: rnd(0, 6.28) });
    for (let t = 0; t < 2; t++) for (let i = 0; i < n; i++) { const p = mk(t, i); teams[t].push(p); all.push(p); }
    if (hockey) for (let t = 0; t < 2; t++) { const g = mk(t, -1); g.goalie = true; g.x = t === 0 ? G.gL + 4 : G.gR - 4; g.y = G.cy; goalies[t] = g; }
    const ball = { x: G.cx, y: G.cy, z: 0, mode: 'carried', owner: null, lastTeam: 0, t: 0, dur: 1, peak: 0, sx: 0, sy: 0, ex: 0, ey: 0, target: null, shooter: 0 };
    let pause = .5, pend = null, tt = 0;
    function formation(off) {
      for (let t = 0; t < 2; t++) teams[t].forEach((p, i) => { p.x = G.cx - dirs[t] * (hockey ? rnd(10, 60) : rnd(8, 80)); p.y = G.y0 + (i + .5) / n * G.RH + rnd(-3, 3); p.face = dirs[t]; });
      if (hockey) goalies.forEach((g, t) => { g.y = G.cy; });
      take(teams[off][2]); ball.x = G.cx; ball.y = G.cy;
    }
    function take(p) { ball.owner = p; ball.mode = 'carried'; ball.lastTeam = p.team; p.timer = rnd(.45, .9); }
    function pass(from, to) { ball.mode = 'pass'; ball.owner = null; ball.lastTeam = from.team; ball.target = to; }
    function shoot(p) {
      ball.mode = 'shot'; ball.owner = null; ball.lastTeam = p.team; ball.shooter = p.team; ball.t = 0;
      ball.sx = ball.x; ball.sy = ball.y; ball.ex = goalX[p.team]; ball.ey = hockey ? G.cy + rnd(-.13, .13) * G.RH : G.cy;
      ball.dur = hockey ? Math.max(.25, hyp(ball.ex - ball.sx, ball.ey - ball.sy) / shotSpd) : .9; ball.peak = hockey ? 0 : rnd(14, 22);
    }
    function goal(t) { confetti(ball.ex, ball.ey, 26, colors.concat(['#ffd23f'])); pause = 1.1; pend = 1 - t; ball.mode = 'dead'; }
    function resolve() {
      const t = ball.shooter;
      if (hockey) { const g = goalies[1 - t]; if (Math.abs(g.y - ball.ey) < 4.5 && Math.random() < .85) { take(g); g.timer = .5; } else goal(t); }
      else if (Math.random() < .42) goal(t); else { ball.mode = 'loose'; ball.t = .5; ball.z = 0; ball.owner = null; }
    }
    function nearestOpp(p) { let best = null, bd = 1e9; for (const q of teams[1 - p.team]) { const d = hyp(q.x - p.x, q.y - p.y); if (d < bd) { bd = d; best = q; } } return [best, bd]; }
    formation(0);
    return {
      update(dt) {
        tt += dt;
        if (pause > 0) { pause -= dt; if (pause <= 0 && pend !== null) { formation(pend); pend = null; } return; }
        const o = ball.owner, poss = o ? o.team : ball.lastTeam, loose = ball.mode === 'loose';
        let chaser = null; if (o) chaser = nearestOpp(o)[0];
        for (let t = 0; t < 2; t++) for (const p of teams[t]) {
          p.moving = false;
          if (loose) { moveToward(p, ball.x, ball.y, pace * 1.1, dt); continue; }
          if (p === o) moveToward(p, goalX[t] - dirs[t] * (hockey ? 14 : 20), G.cy + Math.sin(tt * 1.1 + p.seed) * G.RH * .28, pace, dt);
          else if (t === poss) moveToward(p, clamp(ball.x + dirs[t] * (10 + p.idx * 9), G.x0 + 8, G.x1 - 8), G.y0 + (p.idx + .5) / n * G.RH + Math.sin(tt * .9 + p.seed) * 4, pace * .92, dt);
          else if (p === chaser) moveToward(p, ball.x, ball.y, pace * .95, dt);
          else { const mk_ = teams[1 - t][p.idx]; moveToward(p, mk_.x * .8 + goalX[1 - t] * .2, mk_.y, pace * .85, dt); }
        }
        if (hockey) goalies.forEach((g, t) => {
          const shotAtMe = ball.mode === 'shot' && ball.shooter !== t;
          const ty = shotAtMe ? ball.ey : clamp(lerp(G.cy, ball.y, .35), G.cy - G.RH * .2, G.cy + G.RH * .2);
          g.moving = false; moveToward(g, g.x, ty, 44, dt);
        });
        if (ball.mode === 'carried' && o) {
          ball.x = o.x + o.face * 2; ball.y = o.y - 1; ball.z = 0;
          o.timer -= dt;
          if (o.timer <= 0) {
            o.timer = rnd(.5, 1.1);
            if (o.goalie) pass(o, pick(teams[o.team]));
            else if (Math.abs(o.x - goalX[o.team]) < range && Math.random() < .7) shoot(o);
            else if (Math.random() < .55) { const c = teams[o.team].filter(q => q !== o && (q.x - o.x) * dirs[o.team] > -8); if (c.length) pass(o, pick(c)); }
          }
          const [d, dd] = nearestOpp(o); if (d && dd < 4 && Math.random() < dt * 1.3) take(d);
        } else if (ball.mode === 'pass') {
          const tg = ball.target, dx = tg.x - ball.x, dy = tg.y - ball.y, d = hyp(dx, dy), step = passSpd * dt;
          if (d <= step + 1) take(tg); else { ball.x += dx / d * step; ball.y += dy / d * step; }
          for (const q of teams[1 - ball.lastTeam]) if (ball.mode === 'pass' && hyp(q.x - ball.x, q.y - ball.y) < 3 && Math.random() < dt * 7) take(q);
        } else if (ball.mode === 'shot') {
          ball.t += dt / ball.dur; const k = Math.min(1, ball.t);
          ball.x = lerp(ball.sx, ball.ex, k); ball.y = lerp(ball.sy, ball.ey, k); ball.z = Math.sin(Math.PI * k) * ball.peak;
          if (k >= 1) resolve();
        } else if (loose) {
          ball.t -= dt; if (ball.t <= 0) { let best = null, bd = 1e9; for (const p of all) { const d = hyp(p.x - ball.x, p.y - ball.y); if (d < bd) { bd = d; best = p; } } take(best); }
        }
      },
      draw(c) {
        const list = all.concat(goalies.filter(Boolean)).sort((a, b) => a.y - b.y);
        for (const p of list) person(c, p, colors[p.team], { stick: hockey, hair: p.goalie ? '#ffffff' : null });
        ballDraw(c, ball, hockey ? '#101018' : '#ff7a1a', hockey ? 2 : 3);
      },
    };
  }

  function football(G, colors) {
    const oc = colors[0], dc = colors[1], cy = G.cy;
    const mk = (role, i, t) => ({ role, i, t, x: 0, y: 0, face: t ? -1 : 1, moving: false, seed: rnd(0, 6) });
    const O = ['OL', 'OL', 'OL', 'OL', 'OL', 'QB', 'RB', 'WR', 'WR', 'TE'].map((r, i) => mk(r, i, 0));
    const D = ['DL', 'DL', 'DL', 'LB', 'LB', 'DB', 'DB'].map((r, i) => mk(r, i, 1));
    const all = O.concat(D), by = r => O.filter(p => p.role === r), QB = by('QB')[0], RB = by('RB')[0], WRs = by('WR').concat(by('TE'));
    const ball = { x: 0, y: 0, z: 0, owner: null, mode: 'carried', t: 0, dur: 1, sx: 0, sy: 0, ex: 0, ey: 0, recv: null };
    let LOS = G.fx0 + G.playW * .2, phase = 'set', pt = 0, kind = 'pass', ytimer = 0;
    const slot = (p) => {
      const ol = by('OL').indexOf(p), dl = D.filter(q => q.role === 'DL').indexOf(p), lb = D.filter(q => q.role === 'LB').indexOf(p), db = D.filter(q => q.role === 'DB').indexOf(p);
      switch (p.role) {
        case 'OL': return [LOS - 2, cy + (ol - 2) * 6];
        case 'QB': return [LOS - 9, cy];
        case 'RB': return [LOS - 14, cy + 9];
        case 'WR': return [LOS - 1, p.i === 7 ? G.y0 + 14 : G.y1 - 14];
        case 'TE': return [LOS - 2, cy + 16];
        case 'DL': return [LOS + 4, cy + (dl - 1) * 7];
        case 'LB': return [LOS + 14, cy + (lb ? 10 : -10)];
        default: return [LOS + 26, db ? G.y1 - 14 : G.y0 + 14];
      }
    };
    const nearest = (list, x, y) => list.reduce((b, p) => (!b || hyp(p.x - x, p.y - y) < hyp(b.x - x, b.y - y) ? p : b), null);
    function snapTo() { for (const p of all) { const [x, y] = slot(p); p.x = x + rnd(-4, 4); p.y = y; p.face = p.t ? -1 : 1; } ball.owner = QB; ball.mode = 'carried'; }
    snapTo();
    return {
      update(dt) {
        pt += dt; for (const p of all) p.moving = false;
        if (phase === 'set') {
          for (const p of all) { const [x, y] = slot(p); moveToward(p, x, y, 60, dt); }
          if (pt > .9) { phase = 'play'; pt = 0; kind = Math.random() < .65 ? 'pass' : 'run'; ball.owner = QB; ball.mode = 'carried'; }
        } else if (phase === 'play') {
          if (kind === 'pass') {
            for (const p of O) {
              if (p.role === 'OL') moveToward(p, LOS - 2 - Math.min(pt, 1) * 3, p.y, 12, dt);
              else if (p.role === 'QB') moveToward(p, LOS - 9 - Math.min(pt / .6, 1) * 7, cy, 30, dt);
              else moveToward(p, LOS + 14 + pt * 34, slot(p)[1] + Math.sin(pt * 2 + p.seed) * 8, 50, dt);
            }
            for (const p of D) {
              if (p.role === 'DL') moveToward(p, QB.x, QB.y, 20, dt);
              else { const w = nearest(WRs, p.x, p.y); moveToward(p, w.x + 4, w.y, 40, dt); }
            }
            if (ball.mode === 'carried') { ball.x = QB.x + 2; ball.y = QB.y - 1; }
            if (pt >= 1.4 && ball.mode === 'carried') {
              const w = pick(WRs); ball.mode = 'air'; ball.owner = null; ball.recv = w; ball.t = 0; ball.sx = QB.x; ball.sy = QB.y;
              ball.ex = w.x + 7; ball.ey = w.y; ball.dur = clamp(hyp(ball.ex - ball.sx, ball.ey - ball.sy) / 100, .45, .9);
            }
            if (ball.mode === 'air') {
              ball.t += dt / ball.dur; const k = Math.min(1, ball.t);
              ball.x = lerp(ball.sx, ball.ex, k); ball.y = lerp(ball.sy, ball.ey, k); ball.z = Math.sin(Math.PI * k) * 20;
              if (k >= 1) {
                ball.z = 0;
                if (Math.random() < .68) { ball.owner = ball.recv; ball.mode = 'carried'; phase = 'yac'; ytimer = rnd(.5, 1.1); pt = 0; }
                else { ball.mode = 'dead'; phase = 'dead'; pt = 0; }
              }
            }
          } else {
            if (pt < .5) { for (const p of O) if (p.role === 'OL') moveToward(p, LOS + 2, p.y, 22, dt); moveToward(RB, LOS - 4, RB.y - 3, 40, dt); }
            if (pt >= .5 && ball.owner !== RB) { ball.owner = RB; }
            if (pt >= .5) {
              for (const p of O) { if (p.role === 'OL') moveToward(p, LOS + 6, p.y, 22, dt); else if (p.role !== 'RB') moveToward(p, LOS + 8, p.y, 30, dt); }
              moveToward(RB, LOS + 12 + (pt - .5) * 52, cy + Math.sin(pt * 3) * 6, 52, dt);
            }
            if (pt < .5) { ball.x = QB.x + 2; ball.y = QB.y - 1; }
            for (const p of D) { if (p.role === 'DL') moveToward(p, RB.x, RB.y, 34, dt); else moveToward(p, RB.x + 4, RB.y, 38, dt); }
            if (pt >= 1.7) { ball.owner = RB; phase = 'tackle'; pt = 0; }
          }
        } else if (phase === 'yac') {
          const c = ball.owner; moveToward(c, c.x + 30, c.y, 55, dt);
          for (const p of D) moveToward(p, c.x + 3, c.y, 50, dt);
          for (const p of O) if (p !== c) moveToward(p, c.x - 6 - (p.i % 4) * 4, p.y, 35, dt);
          ytimer -= dt; if (ytimer <= 0) { phase = 'tackle'; pt = 0; }
        } else if (phase === 'tackle') {
          const c = ball.owner; for (const p of D) moveToward(p, c.x + 1, c.y, 40, dt);
          if (pt > .8) {
            LOS = Math.max(LOS, c.x);
            if (LOS >= G.fx1 - 3) { confetti(G.fx1 + 4, cy, 28, [oc, '#ffd23f', '#ff2e93']); LOS = G.fx0 + G.playW * rnd(.12, .3); }
            phase = 'set'; pt = 0; ball.owner = QB; ball.mode = 'carried';
          }
        } else if (phase === 'dead') {
          for (const p of all) moveToward(p, p.x + (p.t ? -1 : 1), p.y, 6, dt);
          if (pt > .8) { phase = 'set'; pt = 0; ball.owner = QB; ball.mode = 'carried'; }
        }
        if (ball.mode === 'carried' && ball.owner) { ball.x = ball.owner.x + ball.owner.face * 2; ball.y = ball.owner.y - 1; ball.z = 0; }
      },
      draw(c) {
        for (const p of all.slice().sort((a, b) => a.y - b.y)) person(c, p, p.t ? dc : oc, { hair: p.t ? '#6b4b00' : '#c0392b' });
        ballDraw(c, ball, '#8b4513', 3);
      },
    };
  }

  function baseball(G, colors) {
    const fc = colors[0], bc = colors[1], { home, first, second, third, mound, Rw } = G;
    const polar = (a, d) => ({ x: home.x + Math.sin(a * Math.PI / 180) * d, y: home.y - Math.cos(a * Math.PI / 180) * d });
    const mkF = (name, pos) => ({ name, x: pos.x, y: pos.y, hx: pos.x, hy: pos.y, face: 1, moving: false, seed: rnd(0, 6) });
    const P = mkF('P', mound), C = mkF('C', { x: home.x, y: home.y + 7 });
    const F = {
      B1: mkF('B1', { x: first.x + 3, y: first.y - 2 }), B2: mkF('B2', { x: (first.x + second.x) / 2 + 3, y: (first.y + second.y) / 2 + 3 }),
      SS: mkF('SS', { x: (third.x + second.x) / 2 - 3, y: (third.y + second.y) / 2 + 3 }), B3: mkF('B3', { x: third.x - 3, y: third.y - 2 }),
      LF: mkF('LF', polar(-32, Rw * .72)), CF: mkF('CF', polar(0, Rw * .8)), RF: mkF('RF', polar(32, Rw * .72)),
    };
    const fielders = [P, C].concat(Object.values(F));
    const batter = { x: home.x - 8, y: home.y, face: 1, moving: false, seed: 1, swing: 0 };
    const runner = { x: 0, y: 0, face: 1, moving: false, seed: 2, active: false, path: [], t: 0, dur: 1 };
    const ball = { x: P.x, y: P.y, z: 3, vis: true };
    let ph = 'idle', pt = 0, fl = null, chase = null, chaser = null, afterWait = 1.1, hr = false;
    const base = () => { runner.active = false; batter.x = home.x - 8; batter.y = home.y; batter.swing = 0; hr = false; chaser = null; chase = null; ball.vis = true; ball.x = P.x; ball.y = P.y; ball.z = 3; fl = null; ph = 'idle'; pt = 0; };
    function fly(ex, ey, dur, peak, onEnd) { fl = { sx: ball.x, sy: ball.y, ex, ey, dur: Math.max(.15, dur), peak, t: 0, onEnd }; ph = 'fly'; }
    function runBases(pts, dur) { runner.active = true; runner.path = pts; runner.t = 0; runner.dur = dur; runner.x = pts[0].x; runner.y = pts[0].y; }
    const nearestF = (x, y) => fielders.slice(2).reduce((b, p) => (!b || hyp(p.x - x, p.y - y) < hyp(b.x - x, b.y - y) ? p : b), null);
    function after(w) { ph = 'after'; pt = 0; afterWait = w; }
    function throwTo(from, target, runnerOut) {
      ball.x = from.x; ball.y = from.y;
      fly(target.x, target.y, hyp(target.x - from.x, target.y - from.y) / 150 + .05, 3, () => after(1.2));
    }
    function inPlay() {
      batter.swing = .3; const kind = pick(['ground', 'ground', 'fly', 'fly', 'line', 'hr']);
      runBases([home, first], rnd(.9, 1.3));
      if (kind === 'ground') {
        const f = pick([F.B1, F.B2, F.SS, F.B3]), T_ = { x: f.hx + rnd(-6, 6), y: f.hy + rnd(-4, 4) }; chaser = f; chase = T_;
        fly(T_.x, T_.y, hyp(T_.x - home.x, T_.y - home.y) / 120, 0, () => { chase = null; throwTo(f, first); });
      } else if (kind === 'fly') {
        const f = pick([F.LF, F.CF, F.RF]), T_ = { x: f.hx + rnd(-10, 10), y: f.hy + rnd(-8, 8) }; chaser = f; chase = T_;
        fly(T_.x, T_.y, 1.4, 34, () => { runner.active = false; after(1.1); });
      } else if (kind === 'line') {
        const a = rnd(-28, 28), T_ = polar(a, Rw * .56), f = nearestF(T_.x, T_.y); chaser = f; chase = T_;
        fly(T_.x, T_.y, .6, 8, () => { throwTo(f, second); });
      } else {
        hr = true; const T_ = polar(rnd(-22, 22), Rw + 8); runBases([home, first, second, third, home], 4.2);
        const f = nearestF(T_.x, T_.y); chaser = f; chase = { x: T_.x, y: T_.y + 6 };
        fly(T_.x, T_.y, 1.9, 52, () => { ball.vis = false; confetti(T_.x, T_.y + 4, 30, [fc, bc, '#ffd23f']); after(3.4); });
      }
    }
    function atPlate() {
      const r = Math.random();
      const catchIt = () => { fly(C.x, C.y, .12, 0, () => fly(P.x, P.y, .7, 5, () => base())); };
      ball.x = home.x; ball.y = home.y - 1;
      if (r < .42) catchIt();
      else if (r < .58) { ball.x += rnd(-5, 5); catchIt(); }
      else if (r < .7) { batter.swing = .3; catchIt(); }
      else if (r < .77) { batter.swing = .3; fly(home.x + rnd(-34, 34), home.y + 28, .9, 30, () => base()); }
      else inPlay();
    }
    return {
      update(dt) {
        pt += dt; batter.moving = false; if (batter.swing > 0) batter.swing -= dt;
        for (const p of fielders) { p.moving = false; if (p === chaser && chase) moveToward(p, chase.x, chase.y, 60, dt); else moveToward(p, p.hx, p.hy, 20, dt); }
        if (runner.active) {
          runner.t += dt; const k = Math.min(1, runner.t / runner.dur), segs = runner.path.length - 1, f = k * segs, i = Math.min(segs - 1, Math.floor(f)), kk = f - i;
          const a = runner.path[i], b = runner.path[i + 1]; runner.x = lerp(a.x, b.x, kk); runner.y = lerp(a.y, b.y, kk); runner.moving = k < 1; runner.face = b.x >= a.x ? 1 : -1;
          if (k >= 1 && !hr) runner.moving = false;
        }
        if (ph === 'idle') { ball.x = P.x; ball.y = P.y - 3; ball.z = 3; if (pt > .9) { ph = 'windup'; pt = 0; } }
        else if (ph === 'windup') { ball.x = P.x; ball.y = P.y - 3; ball.z = 8; if (pt > .5) { ph = 'pitch'; pt = 0; } }
        else if (ph === 'pitch') {
          const k = Math.min(1, pt / .5); ball.x = lerp(P.x, home.x, k); ball.y = lerp(P.y, home.y - 1, k); ball.z = 5 - k * 3;
          if (k >= 1) atPlate();
        } else if (ph === 'fly' && fl) {
          fl.t += dt / fl.dur; const k = Math.min(1, fl.t);
          ball.x = lerp(fl.sx, fl.ex, k); ball.y = lerp(fl.sy, fl.ey, k); ball.z = Math.sin(Math.PI * k) * fl.peak;
          if (k >= 1) { const cb = fl.onEnd; fl = null; ball.z = 0; cb && cb(); }
        } else if (ph === 'after') { if (pt > afterWait) base(); }
      },
      draw(c) {
        const list = [{ p: P, col: fc, o: { armUp: ph === 'windup' } }, { p: C, col: fc }];
        for (const k in F) list.push({ p: F[k], col: fc });
        if (runner.active) list.push({ p: runner, col: bc }); else list.push({ p: batter, col: bc, o: { bat: true, swing: batter.swing } });
        for (const e of list.sort((a, b) => a.p.y - b.p.y)) person(c, e.p, e.col, e.o);
        if (ball.vis) ballDraw(c, ball, '#ffffff', 2);
      },
    };
  }

  // ---------- lifecycle ----------
  const SURFACE = { hockey: drawHockey, basketball: drawBasketball, football: drawFootball, baseball: drawBaseball };
  function build() {
    if (!theme || !SURFACE[theme]) return;
    W = clamp(Math.round(H * innerWidth / Math.max(1, innerHeight)), 140, 420);
    cv.width = W; cv.height = H; bgc = document.createElement('canvas'); bgc.width = W; bgc.height = H;
    const bctx = bgc.getContext('2d', { willReadFrequently: true }); bctx.imageSmoothingEnabled = false;
    const G = SURFACE[theme](bctx);
    parts = []; flash = 0;
    sim = theme === 'hockey' || theme === 'basketball' ? invasion(theme, G, COLORS[theme]) : theme === 'football' ? football(G, COLORS[theme]) : baseball(G, COLORS[theme]);
    render();
  }
  function render() {
    ctx.imageSmoothingEnabled = false; ctx.drawImage(bgc, 0, 0); sim.draw(ctx);
    for (const p of parts) R(ctx, p.x, p.y, 2, 2, p.col);
    if (flash > 0) { ctx.fillStyle = 'rgba(255,255,255,' + (flash * 1.1).toFixed(2) + ')'; ctx.fillRect(0, 0, W, H); }
  }
  function frame(ts) {
    raf = requestAnimationFrame(frame);
    if (document.hidden || ts - last < 40) return;
    const dt = Math.min(.08, (ts - last) / 1000); last = ts; T += dt;
    sim.update(dt);
    for (const p of parts) { p.vy += 160 * dt; p.x += p.vx * dt; p.y += p.vy * dt; p.l -= dt; }
    parts = parts.filter(p => p.l > 0); if (flash > 0) flash -= dt;
    render();
  }
  function stop() { if (raf) cancelAnimationFrame(raf); raf = 0; }
  function sync() {
    stop();
    const t = document.documentElement.getAttribute('data-theme');
    const on = enabled && SURFACE[t];
    if (!cv) { cv = document.createElement('canvas'); cv.id = 'retro-arena'; cv.setAttribute('aria-hidden', 'true');
      cv.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;z-index:-1;pointer-events:none;image-rendering:pixelated;display:none';
      document.body.insertBefore(cv, document.body.firstChild); ctx = cv.getContext('2d'); }
    cv.style.display = on ? 'block' : 'none';
    if (!on) return;
    if (t !== theme || !sim) { theme = t; build(); } else theme = t;
    if (!reduce) { last = performance.now(); raf = requestAnimationFrame(frame); }
  }
  function setEnabled(v) { enabled = !!v; store.set('fm-arena', enabled ? 'on' : 'off'); sync(); }
  function init() {
    sync();
    new MutationObserver(() => sync()).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    let rt = 0; addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(() => { if (cv && cv.style.display !== 'none') { stop(); build(); if (!reduce) { last = performance.now(); raf = requestAnimationFrame(frame); } } }, 200); });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) last = performance.now(); });
  }
  if (document.body) init(); else document.addEventListener('DOMContentLoaded', init);
  return { setEnabled, isEnabled: () => enabled };
})();
