// Node checks for web/static/draft-clock-logic.js, run by scripts/verify_async_snake_draft.py.
const C = require(process.argv[2]);
const ok = (c, m) => { if (!c) { console.error('FAIL ' + m); process.exit(1); } console.log('ok  ' + m); };
ok(C.formatRemaining(45) === '0:45' && C.formatRemaining(754) === '12:34' && C.formatRemaining(4500) === '1h 15m' && C.formatRemaining(190000) === '2d 4h' && C.formatRemaining(null) === '—', 'countdown formatting');
const run = {clock_status: 'running', remaining_seconds: 60, mode: 'live'};
ok(C.liveRemaining(run, 10000 + 20500, 10000) === 40 && C.liveRemaining(run, 10000 + 99000, 10000) === 0, 'a running clock counts down and stops at zero');
ok(C.liveRemaining({clock_status: 'paused', remaining_seconds: 200}, 1e9, 0) === 200 && C.liveRemaining({clock_status: 'overnight', remaining_seconds: 1800}, 1e9, 0) === 1800, 'paused and overnight clocks stay frozen');
ok(C.shouldProcess(run, 10000 + 61000, 10000) && !C.shouldProcess(run, 10000 + 30000, 10000) && !C.shouldProcess({clock_status: 'paused', remaining_seconds: 0}, 1, 0), 'only an expired running clock asks the server to process');
ok(/Paused/.test(C.statusLine({clock_status: 'paused'})) && C.statusLine({clock_status: 'overnight', overnight: {end: '06:00', utc_offset_minutes: -300}}).includes('06:00 (UTC-05:00)'), 'status lines name the pause and the resume time');
ok(C.pollDelayMs({clock_status: 'untimed'}) === 0 && C.pollDelayMs({clock_status: 'running', mode: 'async'}) === 30000 && C.pollDelayMs(run) === 5000, 'poll cadence by mode');
ok(Object.keys(C.startBody({mode: 'none'}).body).length === 0, 'no clock sends nothing extra');
ok(C.startBody({mode: 'slow', value: '8', overnight: true, from: '22:00', to: '06:00', offsetMinutes: -300}).body.pick_seconds === 28800, 'slow mode converts hours to seconds');
ok(C.startBody({mode: 'live', value: '3'}).error && C.startBody({mode: 'live', value: '60', overnight: true, from: '22:00', to: '22:00'}).error && C.startBody({mode: 'live', value: ''}).error, 'bad forms are refused before sending');
