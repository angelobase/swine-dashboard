'use strict';
// Scheduled-session checks on a temporary config and database:  node tools/schedule_test.js
// The prototype's MQTT messages are posted to the loopback device route, so no broker is needed (MQTT_ENABLED=0).
// Starts from an OLD-schema database to check the in-place migration keeps existing data.
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { DatabaseSync } = require('node:sqlite');

const ROOT = path.join(__dirname, '..');
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swine-sched-'));
const dbFile = path.join(dir, 'swine.db');
const PORT = 18000 + Math.floor(Math.random() * 2000);
const BASE = `http://127.0.0.1:${PORT}`;
let passed = 0; const failures = [];
const check = (name, cond, extra) => { if (cond) passed++; else failures.push(name + (extra !== undefined ? ` -> ${JSON.stringify(extra).slice(0, 300)}` : '')); };

// ---- an old-schema database with one legacy session
const old = new DatabaseSync(dbFile);
old.exec(`CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, pen_id TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
  lying_s REAL NOT NULL DEFAULT 0, alert_active INTEGER NOT NULL DEFAULT 0);
  CREATE UNIQUE INDEX one_active_session_per_pen ON sessions(pen_id) WHERE ended_at IS NULL;
  CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, pen_id TEXT NOT NULL, session_id INTEGER NOT NULL REFERENCES sessions(id),
  event_type TEXT NOT NULL CHECK (event_type IN ('Measurement','Session started','Session ended','Alert','Alert acknowledged')),
  surface_temp_c REAL, posture TEXT CHECK (posture IS NULL OR posture IN ('Lying','Not lying','Unknown')), lying_s REAL, screening_status TEXT, detail TEXT, evidence_path TEXT);
  INSERT INTO sessions (pen_id, started_at, ended_at, lying_s) VALUES ('PEN-01', '2026-10-01T00:00:00.000Z', '2026-10-01T01:00:00.000Z', 120);
  INSERT INTO records (ts, pen_id, session_id, event_type, surface_temp_c, posture, lying_s, screening_status) VALUES ('2026-10-01T00:10:00.000Z', 'PEN-01', 1, 'Measurement', 31.5, 'Lying', 60, 'Monitoring');`);
old.close();
fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify({ pens: ['PEN-01'], preview: { enabled: true } }));

const UI = { 'X-Requested-With': 'dashboard', 'Content-Type': 'application/json' };
async function call(method, p, body, headers = UI) {
  const r = await fetch(BASE + p, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await r.text();
  let json = null; try { json = JSON.parse(text); } catch {}
  return { status: r.status, json, text, headers: r.headers };
}
const device = (pen, body) => call('POST', `/api/device/${pen}`, body, { 'Content-Type': 'application/json' });
const cmds = async (pen) => (await call('GET', `/api/device/${pen}/commands`)).json.commands;
const status = async () => (await call('GET', '/api/status')).json;
const pen = async (id) => (await status()).pens.find((p) => p.pen_id === id);
const iso = (ms) => new Date(ms).toISOString();

function sessionMsg(uid, state, extra = {}) {
  const t0 = extra.t0 || Date.now();
  return { uid, state, pen: 'PEN-02', test: true, started_at: iso(t0), ends_at: iso(t0 + 300000), interval_s: 30, duration_s: 300, captures: 11,
    next_seq: 0, last_seq: -1, next_capture_at: iso(t0 + 1000), capturing: false, clock_source: 'dashboard',
    rule: { threshold_c: 36.7, consecutive_captures: 3, lookback_captures: 3 }, ended_at: null, result: null, sms: null, ...extra };
}
function sample(uid, seq, t0, pigs) {
  return { uid, seq, ts: iso(t0 + seq * 30000), ok: true, thermal_valid: true, calibrated: false, images: { rgb: `${uid}/${String(seq).padStart(3, '0')}/rgb.jpg` },
    pigs: pigs || [
      { pig_id: 1, observed: true, posture: 'Lying', lying_s: (seq + 1) * 30, surface_temp_c: 37.1, behavioral_status: seq >= 2 ? 'Abnormal' : 'Normal', thermal_status: 'Elevated' },
      { pig_id: 2, observed: true, posture: 'Not lying', lying_s: 0, surface_temp_c: 35.0, behavioral_status: 'Normal', thermal_status: 'Normal' }] };
}
const RESULT = { pigs: [
  { pig_id: 1, alert: true, longest_lying_s: 180, longest_lying_captures: 6, lying_captures: 6, final_temp_c: 37.1, behavioral_status: 'Abnormal', thermal_status: 'Elevated', reason: 'Prolonged lying and elevated final temperature' },
  { pig_id: 2, alert: false, longest_lying_s: 0, longest_lying_captures: 0, lying_captures: 0, final_temp_c: 35.0, behavioral_status: 'Normal', thermal_status: 'Normal', reason: 'Lying bout below the rule' }],
  flagged: [1], any_alert: true, rule: { threshold_c: 36.7, consecutive_captures: 3, lookback_captures: 3, interval_s: 30 } };

async function main() {
  const srv = spawn(process.execPath, ['server.js'], { cwd: ROOT, env: { ...process.env, SWINE_CONFIG: path.join(dir, 'config.json'), SWINE_DB: dbFile,
    PORT: String(PORT), HOST: '127.0.0.1', MQTT_ENABLED: '0', INGEST_TOKEN: '' }, stdio: ['ignore', 'pipe', 'pipe'] });
  let log = ''; srv.stdout.on('data', (d) => { log += d; }); srv.stderr.on('data', (d) => { log += d; });
  for (let i = 0; i < 100 && !log.includes('listening'); i++) await new Promise((r) => setTimeout(r, 100));
  try {
    // migration kept the legacy data
    const sess0 = (await call('GET', '/api/sessions')).json.sessions;
    check('legacy session kept after migration', sess0.length === 1 && sess0[0].kind === 'live' && sess0[0].measurements === 1, sess0);

    // pen with a prototype + test mode
    check('add pen', (await call('POST', '/api/pens', { pen_id: 'PEN-02', address: 'mqtt://h:1883/proto-aaa111' })).status === 200);
    check('test mode on', (await call('POST', '/api/settings/test-mode', { enabled: true })).json.test_mode === true);
    check('test mode rejects non-boolean', (await call('POST', '/api/settings/test-mode', { enabled: 'yes' })).status === 422);
    check('test mode needs UI header', (await call('POST', '/api/settings/test-mode', { enabled: true }, { 'Content-Type': 'application/json' })).status === 403);
    check('status reports test mode', (await status()).test_mode === true);

    // start -> command queued -> waiting
    const st = await call('POST', '/api/pens/PEN-02/session/start', {});
    check('start scheduled', st.status === 200 && st.json.scheduled === true && st.json.test === true, st.json);
    const c1 = await cmds('PEN-02');
    const start = c1.find((c) => c.action === 'start');
    check('start command sent', start && start.test === true && start.pen === 'PEN-02' && /^dash-/.test(start.uid) && !!Date.parse(start.server_time), c1);
    const uid = start.uid;
    check('waiting for prototype', (await pen('PEN-02')).status === 'Waiting for prototype');
    check('second start refused', (await call('POST', '/api/pens/PEN-02/session/start', {})).status === 409);

    // prototype confirms, captures arrive (one replayed twice), legacy readings refused
    const t0 = Date.now();
    let r = await device('PEN-02', { session: sessionMsg(uid, 'running', { t0 }) });
    check('session confirmed', r.status === 200, r.json);
    let pv = await pen('PEN-02');
    check('monitoring after confirm', pv.status === 'Monitoring' && pv.schedule.interval_s === 30 && pv.schedule.test === true, pv);
    for (const k of [0, 1, 2]) check(`sample ${k} stored`, (await device('PEN-02', { sample: sample(uid, k, t0) })).json.stored === 2);
    check('duplicate sample ignored', (await device('PEN-02', { sample: sample(uid, 1, t0) })).json.stored === 0);
    pv = await pen('PEN-02');
    const lastPigs = pv.schedule.last && pv.schedule.last.pigs;
    check('tile shows latest capture', pv.schedule.last.seq === 2 && lastPigs.length === 2 && lastPigs[0].behavioral_status === 'Abnormal' && lastPigs[0].thermal_status === 'Elevated', pv.schedule.last);
    check('received count', pv.schedule.received === 3);
    const ing = await call('POST', '/api/ingest', { pen_id: 'PEN-02', timestamp: iso(Date.now()), surface_temp_c: 30, posture: 'Lying' }, { 'Content-Type': 'application/json' });
    check('live reading refused during scheduled session', ing.status === 409, ing.json);
    check('sample outside session window rejected', (await device('PEN-02', { sample: { ...sample(uid, 9, t0), ts: iso(t0 - 3600000) } })).status === 422);
    check('sample for unknown session rejected', (await device('PEN-02', { sample: sample('nope-123456', 0, t0) })).status === 409);
    const noPig = await device('PEN-02', { sample: { ...sample(uid, 3, t0, []), ok: false, error: 'Missed: the prototype was not running' } });
    check('capture without pigs is still one row', noPig.json.stored === 1);

    // a gap triggers a replay request for exactly the missing captures
    await device('PEN-02', { sample: sample(uid, 6, t0) });
    await cmds('PEN-02');
    await device('PEN-02', { session: sessionMsg(uid, 'running', { t0, next_seq: 7, last_seq: 6, next_capture_at: iso(t0 + 210000) }) });
    const rp = (await cmds('PEN-02')).find((c) => c.action === 'replay');
    check('replay requests missing captures', rp && rp.uid === uid && JSON.stringify(rp.seqs) === '[4,5]', rp);

    // overdue
    await device('PEN-02', { session: sessionMsg(uid, 'running', { t0, next_seq: 7, last_seq: 6, next_capture_at: iso(Date.now() - 600000) }) });
    pv = await pen('PEN-02');
    check('overdue capture flagged', pv.status === 'Capture overdue' && pv.stale === true, pv.status);

    // finished: session ends, alert raised once, SMS stored
    const fin = sessionMsg(uid, 'finished', { t0, next_seq: 11, last_seq: 10, next_capture_at: null, ended_at: iso(Date.now()), result: RESULT,
      sms: { enabled: true, state: 'submitted', detail: 'SMS submitted (attempt 1).', time: iso(Date.now()) } });
    // the Pi reports 'evaluating' while it sends the SMS, then 'finished'
    await device('PEN-02', { session: { ...fin, state: 'evaluating', sms: { enabled: true, state: 'sending', detail: 'Sending one SMS...' } } });
    check('evaluating ends the session', (await pen('PEN-02')).last_result.state === 'evaluating');
    await device('PEN-02', { session: fin });
    await device('PEN-02', { session: fin }); // re-announced (retained) message
    check('evaluating becomes finished', (await pen('PEN-02')).last_result.state === 'finished');
    pv = await pen('PEN-02');
    check('session ended after finish', pv.session === null && pv.last_result && pv.last_result.flagged[0] === 1 && pv.last_result.sms.state === 'submitted', pv);
    check('scheduled alert shown', pv.alert && pv.alert.kind === 'scheduled' && pv.alert.pigs.length === 1 && pv.alert.pigs[0].pig_id === 1, pv.alert);
    const logRows = (await call('GET', '/api/log?pen=PEN-02&limit=500')).json.records;
    check('one Session ended record', logRows.filter((x) => x.event_type === 'Session ended').length === 1);
    const alertRows = logRows.filter((x) => x.event_type === 'Alert');
    check('one Alert record for pig 1', alertRows.length === 1 && alertRows[0].pig_id === 1 && alertRows[0].thermal_status === 'Elevated', alertRows);
    const m0 = logRows.find((x) => x.event_type === 'Measurement' && x.seq === 0 && x.pig_id === 1);
    check('measurement has methodology fields', m0 && m0.posture === 'Lying' && m0.lying_s === 30 && m0.behavioral_status === 'Normal' && m0.evidence_path === `pi/${uid}/000/rgb.jpg`, m0);
    const later = { ...fin, sms: { ...fin.sms, state: 'unconfirmed', detail: 'changed' } };
    await device('PEN-02', { session: later });
    check('later SMS update stored', (await pen('PEN-02')).last_result.sms.state === 'unconfirmed');

    // exports
    const code = (await call('GET', '/api/sessions')).json.sessions.find((s) => s.pen_id === 'PEN-02').session;
    const csv = await call('GET', `/api/sessions/${code}/export.csv`);
    check('csv filename marks TEST', /_TEST\.csv/.test(csv.headers.get('content-disposition')));
    check('csv summary line', csv.text.includes(`# ${code}: TEST session`) && csv.text.includes('flagged pigs: 1'), csv.text.slice(0, 300));
    check('csv methodology columns', csv.text.includes('Capture,Pig ID,Event,Posture,Lying (s),Surface temp (°C),Behavioral status,Thermal status'));
    check('csv capture row', csv.text.includes(`,PEN-02,${code},1,1,Measurement,Lying,30,37.1,Normal,Elevated,Captured,`), csv.text.split('\n').slice(0, 6));
    const pdf = await call('GET', `/api/sessions/${code}/export.pdf`);
    check('pdf builds', pdf.status === 200 && pdf.text.startsWith('%PDF') && pdf.text.includes('Behavior') && pdf.text.includes('TEST session'));
    const sl = (await call('GET', '/api/sessions')).json.sessions.find((s) => s.session === code);
    check('sessions list shows result', sl.kind === 'scheduled' && sl.test === true && sl.captures === 5 && sl.result.flagged[0] === 1, sl);

    // acknowledge the end-of-session alert
    check('ack ended-session alert', (await call('POST', '/api/pens/PEN-02/alert/ack', {})).status === 200);
    check('alert cleared', !(await pen('PEN-02')).alert);

    // session started on the Pi (--start) is adopted; End without a broker closes it locally; late result is still recorded
    await device('PEN-02', { session: sessionMsg('pi-cli0000001', 'running', { t0: Date.now() }) });
    pv = await pen('PEN-02');
    check('pi-started session adopted', pv.session && pv.schedule.uid === 'pi-cli0000001' && pv.status === 'Monitoring', pv);
    const end = await call('POST', '/api/pens/PEN-02/session/end', {});
    check('end without broker closes locally', end.status === 200 && !end.json.pending, end.json);
    await device('PEN-02', { session: sessionMsg('pi-cli0000001', 'ended_early', { ended_at: iso(Date.now()), result: { ...RESULT, any_alert: false, flagged: [], pigs: RESULT.pigs.map((p) => ({ ...p, alert: false })) },
      sms: { enabled: true, state: 'skipped', detail: 'Session ended early; no SMS is sent.' } }) });
    const lateEnd = (await call('GET', '/api/log?pen=PEN-02&type=Session%20ended&limit=50')).json.records.filter((x) => x.detail && x.detail.kind === 'evaluation' && x.detail.note);
    check('late evaluation recorded with note', lateEnd.length === 1, lateEnd);

    // start refused by the prototype
    await call('POST', '/api/pens/PEN-02/session/start', {});
    const uid2 = (await cmds('PEN-02')).find((c) => c.action === 'start').uid;
    // meanwhile a session finished while the dashboard was down is announced: recorded as ended, the pending start is untouched
    await device('PEN-02', { session: sessionMsg('pi-offline001', 'finished', { ended_at: iso(Date.now()), last_seq: 10, next_seq: 11, result: RESULT, sms: { enabled: true, state: 'submitted', detail: 'x' } }) });
    const rp2 = (await cmds('PEN-02')).find((c) => c.action === 'replay' && c.uid === 'pi-offline001');
    check('all captures of the offline session are requested', rp2 && rp2.seqs.length === 11 && rp2.seqs[10] === 10, rp2);
    pv = await pen('PEN-02');
    check('offline-finished session recorded without touching the pending start', pv.session && pv.status === 'Waiting for prototype', pv.status);
    const off = (await call('GET', '/api/sessions')).json.sessions.find((s) => s.state === 'finished' && s.result && s.result.flagged.length === 1 && s.session !== code);
    check('offline-finished session listed', !!off);
    await device('PEN-02', { event: { kind: 'start_refused', detail: 'still running', uid: uid2 } });
    pv = await pen('PEN-02');
    check('refused start closes the pending session', pv.session === null, pv);

    // the Pi announces a different running session while one is active: the old one is superseded
    await device('PEN-02', { session: sessionMsg('pi-aaaa000001', 'running') });
    await device('PEN-02', { session: sessionMsg('pi-bbbb000002', 'running') });
    pv = await pen('PEN-02');
    check('newer running session supersedes', pv.schedule && pv.schedule.uid === 'pi-bbbb000002', pv.schedule && pv.schedule.uid);
    const sup = (await call('GET', '/api/sessions')).json.sessions.find((s) => s.state === 'superseded');
    check('superseded session closed', !!sup && !!sup.ended_at);

    // deleted sessions are not re-created when the prototype re-announces them
    const del = await call('DELETE', `/api/sessions/${off.session}`);
    check('delete ended session', del.status === 200);
    const re = await device('PEN-02', { session: sessionMsg('pi-offline001', 'finished', { ended_at: iso(Date.now()), result: RESULT }) });
    check('deleted session not re-created', re.json.ignored === 'deleted on the dashboard', re.json);

    // a second pen without a prototype still runs the legacy live flow
    const live = await call('POST', '/api/pens/PEN-01/session/start', {});
    check('legacy pen starts a live session', live.status === 200 && !live.json.scheduled, live.json);
    const lr = await call('POST', '/api/ingest', { pen_id: 'PEN-01', timestamp: iso(Date.now()), surface_temp_c: 30.5, posture: 'Lying' }, { 'Content-Type': 'application/json' });
    check('legacy ingest still works', lr.status === 200 && lr.json.ok, lr.json);
    check('session message for a pen with a live session is refused', (await device('PEN-01', { session: sessionMsg('pi-cccc000003', 'running') })).status === 409);
  } finally {
    srv.kill();
  }
  if (log.includes('[error]')) failures.push('server logged an error: ' + log.split('\n').filter((l) => l.includes('[error]')).join(' | '));
  console.log(`${passed} passed, ${failures.length} failed`);
  for (const f of failures) console.log('  FAIL ' + f);
  fs.rmSync(dir, { recursive: true, force: true });
  process.exit(failures.length ? 1 : 0);
}
main().catch((e) => { console.error(e); process.exit(1); });
