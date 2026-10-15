// Retro UI runtime, shared by every page. Load in <head> (before first paint) so the sport theme
// applies without a flash. Provides: per-sport theming, click/whistle sounds, pixel sprites and
// team crests. No audio or image files: sounds are synthesized, sprites are drawn from text.
const Retro = (() => {
  const THEME_BY_UNIVERSE = { nhl: 'hockey', mlb: 'baseball', nfl: 'football', ncaaf: 'football', nba: 'basketball' };
  const SPRITE_BY_THEME = { hockey: 'puck', baseball: 'baseball', football: 'football', basketball: 'basketball', hub: 'star' };
  const root = document.documentElement;
  const store = {
    get: k => { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} },
    sget: k => { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
    sset: (k, v) => { try { sessionStorage.setItem(k, v); } catch (e) {} },
  };

  // ---------- Theme ----------
  const params = new URLSearchParams(location.search);
  const leagueId = params.get('league_id');
  const setTheme = t => root.setAttribute('data-theme', t || 'hub');
  // Immediately apply the last known theme for this league (no flash), then confirm from the API.
  setTheme(params.get('theme') || (leagueId && store.sget('fm-theme-' + leagueId)) || 'hub');

  async function detectTheme() {
    if (!leagueId || params.get('theme') || store.sget('fm-theme-' + leagueId)) return;  // a league's sport never changes
    try {
      const s = JSON.parse(store.get('fsc_session') || 'null');
      const res = await fetch(`/leagues/${encodeURIComponent(leagueId)}/overview`, s && s.token ? { headers: { Authorization: 'Bearer ' + s.token } } : {});
      if (!res.ok) return;
      const ov = await res.json();
      const code = (ov.universe_codes || []).find(c => THEME_BY_UNIVERSE[c]);
      const theme = THEME_BY_UNIVERSE[code] || 'hub';
      store.sset('fm-theme-' + leagueId, theme);
      setTheme(theme);
    } catch (e) { /* stay on whatever theme is applied */ }
  }

  // ---------- Sound ----------
  let audio = null, soundOn = store.get('fm-sound') !== 'off';
  const ac = () => audio || (audio = new (window.AudioContext || window.webkitAudioContext)());
  function click() {
    if (!soundOn) return;
    try {
      const c = ac(), t = c.currentTime;
      const n = Math.floor(c.sampleRate * 0.012), buf = c.createBuffer(1, n, c.sampleRate), d = buf.getChannelData(0);
      for (let i = 0; i < n; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / n, 3);
      const src = c.createBufferSource(); src.buffer = buf;
      const hp = c.createBiquadFilter(); hp.type = 'highpass'; hp.frequency.value = 2400;
      const g1 = c.createGain(); g1.gain.value = 0.5; src.connect(hp).connect(g1).connect(c.destination); src.start(t);
      const o = c.createOscillator(), g2 = c.createGain(); o.type = 'square';
      o.frequency.setValueAtTime(1900, t); o.frequency.exponentialRampToValueAtTime(700, t + 0.03);
      g2.gain.setValueAtTime(0.07, t); g2.gain.exponentialRampToValueAtTime(0.0001, t + 0.04);
      o.connect(g2).connect(c.destination); o.start(t); o.stop(t + 0.05);
    } catch (e) {}
  }
  function blast(c, t, dur) {
    const o = c.createOscillator(), o2 = c.createOscillator(), lfo = c.createOscillator(), lfoG = c.createGain(), g = c.createGain();
    o.type = 'sine'; o2.type = 'sine'; o.frequency.value = 2850; o2.frequency.value = 3010;
    lfo.frequency.value = 32; lfoG.gain.value = 140; lfo.connect(lfoG); lfoG.connect(o.frequency); lfoG.connect(o2.frequency);
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.16, t + 0.015);
    g.gain.setValueAtTime(0.16, t + dur - 0.04); g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g); o2.connect(g); g.connect(c.destination);
    const n = Math.floor(c.sampleRate * dur), buf = c.createBuffer(1, n, c.sampleRate), d = buf.getChannelData(0);
    for (let i = 0; i < n; i++) d[i] = Math.random() * 2 - 1;
    const ns = c.createBufferSource(); ns.buffer = buf;
    const bp = c.createBiquadFilter(); bp.type = 'bandpass'; bp.frequency.value = 3200; bp.Q.value = 3;
    const ng = c.createGain(); ng.gain.setValueAtTime(0.0001, t); ng.gain.exponentialRampToValueAtTime(0.05, t + 0.02); ng.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    ns.connect(bp).connect(ng).connect(c.destination);
    [o, o2, lfo, ns].forEach(x => x.start(t)); [o, o2, lfo].forEach(x => x.stop(t + dur + 0.02));
  }
  function whistle() {
    if (!soundOn) return;
    try { const c = ac(), t = c.currentTime; blast(c, t, 0.16); blast(c, t + 0.24, 0.5); } catch (e) {}
  }

  // Whistle for the "big" actions; everything else gets the tick.
  const WHISTLE_TEXT = /^\s*(start (the )?(draft|auction|recruiting)|open draft board|draft\b|lock|submit|place bid|bid\b|nominate|confirm|make (the )?pick|propose|accept|enter draft)/i;
  const CLICKABLE = 'button, .btn, a[href], .tab, .chip, [role="tab"], [role="button"], select, summary, tbody tr, .card-hover, [data-click]';
  document.addEventListener('pointerdown', e => {
    const el = e.target.closest(CLICKABLE);
    if (!el || el.disabled || el.closest('[data-nosound]')) return;
    const big = el.hasAttribute('data-whistle') || (el.matches('button, .btn') && WHISTLE_TEXT.test(el.textContent || ''));
    big ? whistle() : click();
    if (el.matches('.btn')) { el.classList.add('pressed'); setTimeout(() => el.classList.remove('pressed'), 90); }
  }, true);

  function paintSoundBtn(b) { b.textContent = 'Sound: ' + (soundOn ? 'On' : 'Off'); b.setAttribute('aria-pressed', soundOn); }
  function mountSoundToggle() {
    const nav = document.querySelector('.nav'); if (!nav || nav.querySelector('.retro-sound')) return;
    const b = document.createElement('button'); b.type = 'button'; b.className = 'retro-sound'; b.title = 'Toggle sound effects';
    b.setAttribute('data-nosound', ''); paintSoundBtn(b);
    b.addEventListener('click', () => { soundOn = !soundOn; store.set('fm-sound', soundOn ? 'on' : 'off'); paintSoundBtn(b); click(); });
    nav.appendChild(b);
  }

  // ---------- Pixel art ----------
  const PAL = { K: '#14102a', W: '#ffffff', R: '#e0202e', B: '#8b4513', O: '#ff7a1a', D: '#2b2b3b', L: '#7a7a90', Y: '#ffd23f', P: '#ff2e93', C: '#00e5ff', G: '#2fb457' };
  const SPR = {
    puck: ['..KKKKKK..', '.KLLLLLLK.', 'KDDDDDDDDK', '.KKKKKKKK.'],
    baseball: ['..KKKK..', '.KWWWWK.', 'KWRWWRWK', 'KWRWWRWK', 'KWRWWRWK', 'KWRWWRWK', '.KWWWWK.', '..KKKK..'],
    football: ['..KKKKKK..', '.KBBBBBBK.', 'KBBBWWBBBK', 'KBBBBBBBBK', '.KBBBBBBK.', '..KKKKKK..'],
    basketball: ['..KKKK..', '.KOOKOK.', 'KOOOKOOK', 'KKKKKKKK', 'KOOOKOOK', 'KOOOKOOK', '.KOOKOK.', '..KKKK..'],
    star: ['...YY...', '...YY...', 'YYYYYYYY', '.YYYYYY.', '..YYYY..', '.YYYYYY.', '.YY..YY.', 'Y......Y'],
    bolt: ['....YY..', '...YY...', '..YY....', '.YYYYYY.', '....YY..', '...YY...', '..YY....', '.Y......'],
    whistle: ['..KKKKKK.', 'KKLLLLLLK', 'KLLKKLLLK', 'KLLKKLLLK', 'KKLLLLLK.', '.KKKKKK..'],
    trophy: ['YYYYYYYY', 'Y.YYYY.Y', 'Y.YYYY.Y', '.YYYYYY.', '..YYYY..', '...YY...', '..YYYY..', '..YYYY..'],
  };
  function sprite(name, px = 4) {
    const rows = SPR[name]; if (!rows) return '';
    const w = rows[0].length, h = rows.length; let r = '';
    rows.forEach((row, y) => [...row].forEach((ch, x) => { if (PAL[ch]) r += `<rect x="${x}" y="${y}" width="1" height="1" fill="${PAL[ch]}"/>`; }));
    return `<svg class="sprite" width="${w * px}" height="${h * px}" viewBox="0 0 ${w} ${h}" shape-rendering="crispEdges" aria-hidden="true">${r}</svg>`;
  }
  const rng = seed => { let s = 0; for (const c of String(seed)) s = (s * 31 + c.charCodeAt(0)) >>> 0; return () => (s = (s * 1664525 + 1013904223) >>> 0) / 4294967296; };
  // Seeded, mirrored 7x7 crest: every team name gets a stable, unique pixel logo.
  function badge(seed, px = 5) {
    const r = rng(seed), hues = [350, 25, 48, 140, 190, 220, 280, 320];
    const h1 = hues[Math.floor(r() * hues.length)], h2 = hues[Math.floor(r() * hues.length)];
    const c1 = `hsl(${h1} 85% 52%)`, c2 = `hsl(${h2} 85% ${h1 === h2 ? 85 : 68}%)`; let cells = '';
    for (let y = 0; y < 7; y++) for (let x = 0; x < 4; x++) {
      const v = r(); if (v < .38) continue; const col = v < .72 ? c1 : c2;
      cells += `<rect x="${x}" y="${y}" width="1" height="1" fill="${col}"/><rect x="${6 - x}" y="${y}" width="1" height="1" fill="${col}"/>`;
    }
    return `<svg class="sprite" width="${9 * px}" height="${9 * px}" viewBox="-1 -1 9 9" shape-rendering="crispEdges" aria-hidden="true"><rect x="-1" y="-1" width="9" height="9" fill="#14102a"/><rect x="0" y="0" width="7" height="7" fill="#fff" fill-opacity=".92"/>${cells}</svg>`;
  }
  // <span data-sprite="puck" data-px="3"></span> and <span data-badge="Team Name"></span> fill themselves in.
  function hydrate(scope = document) {
    scope.querySelectorAll('[data-sprite]:not([data-done])').forEach(el => { el.innerHTML = sprite(el.dataset.sprite, +el.dataset.px || 4); el.dataset.done = 1; });
    scope.querySelectorAll('[data-badge]:not([data-done])').forEach(el => { el.innerHTML = badge(el.dataset.badge, +el.dataset.px || 4); el.dataset.done = 1; });
  }

  document.addEventListener('DOMContentLoaded', () => { mountSoundToggle(); hydrate(); detectTheme(); });
  // Sport ball/puck sprite for a list of universe codes (nhl, nfl, ncaaf, ...).
  const sportSprite = (codes, px = 4) => { const c = (codes || []).find(x => THEME_BY_UNIVERSE[x]); return sprite(SPRITE_BY_THEME[THEME_BY_UNIVERSE[c]] || 'star', px); };
  return { setTheme, sportSprite, click, whistle, sprite, badge, hydrate, THEME_BY_UNIVERSE };
})();
