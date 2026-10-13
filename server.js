'use strict';
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { open, tx, sessionCode } = require('./lib/db');
const { buildCsv, buildPdf } = require('./lib/export');
const { createHub, deviceFromAddress } = require('./lib/mqtt_hub');
const { startBeacon } = require('./lib/beacon');
const cfg = require('./lib/config').load();
const PENS = [...cfg.pens]; // known pens: config.json plus pens added from the dashboard
const nodeVersion = process.versions.node.split('.').map(Number);
if (nodeVersion[0] < 22 || (nodeVersion[0] === 22 && (nodeVersion[1] || 0) < 13)) {
  console.error(`Node.js ${process.version} is too old for node:sqlite. Please install Node 22.13 or newer (for example via nvm or the Raspberry Pi OS package manager) and restart.`);
  process.exit(1);
}

fs.mkdirSync(path.dirname(cfg.db_path), { recursive: true });
const db = open(cfg.db_path);

// ---- Pen registry: which pens are shown on the dashboard, with the prototype address and optional camera URL
// Extra tables are created at startup. If the database is read-only the server must still start
// (so database failures are reported per request, as before); the new features then report "not saved".
let extraTables = true;
try {
  db.exec("CREATE TABLE IF NOT EXISTS pen_registry (pen_id TEXT PRIMARY KEY, address TEXT NOT NULL, camera TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL)");
  db.exec('CREATE TABLE IF NOT EXISTS device_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, pen_id TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL)');
} catch (e) { extraTables = false; console.error('[warn] Could not create extra tables (read-only database?):', e.message); }
const unwritable = { all: () => [], get: () => undefined, run: () => { throw new Error('Database is not writable; the action was NOT saved.'); } };
const prep = (sql) => (extraTables ? db.prepare(sql) : unwritable);
const reg = {
  all: prep('SELECT pen_id, address, camera FROM pen_registry ORDER BY rowid'),
  get: prep('SELECT pen_id FROM pen_registry WHERE pen_id = ?'),
  ins: prep('INSERT INTO pen_registry (pen_id, address, camera, added_at) VALUES (?, ?, ?, ?)'),
  del: prep('DELETE FROM pen_registry WHERE pen_id = ?'),
};
for (const r of reg.all.all()) if (!PENS.includes(r.pen_id)) PENS.push(r.pen_id);
// device id (from the pen's prototype address) <-> pen id, for the built-in MQTT hub
let devToPen = new Map(); let penToDev = new Map();
function rebuildDeviceMap() {
  devToPen = new Map(); penToDev = new Map();
  for (const r of reg.all.all()) { const d = deviceFromAddress(r.address); if (d) { devToPen.set(d, r.pen_id); penToDev.set(r.pen_id, d); } }
}
rebuildDeviceMap();

// ---- Prototype device channel: state, events and remote commands (fed by tools/mqtt_bridge.py)
const devIns = prep('INSERT INTO device_events (ts, pen_id, kind, detail) VALUES (?, ?, ?, ?)');
const devList = prep('SELECT ts, pen_id, kind, detail FROM device_events WHERE (? = \'\' OR pen_id = ?) ORDER BY id DESC LIMIT ?');
const dev = new Map(); // pen -> { state, at, cmds }
const devGet = (pen) => { let d = dev.get(pen); if (!d) { d = { state: null, at: 0, cmds: [] }; dev.set(pen, d); } return d; };
const str = (v, n) => (typeof v === 'string' ? v.slice(0, n) : '');
function cleanState(b) {
  const o = {};
  if (typeof b.online === 'boolean') o.online = b.online;
  if (typeof b.device_id === 'string') o.device_id = str(b.device_id, 40);
  if (['auto', 'manual'].includes(b.mode)) o.mode = b.mode;
  if (b.camera === null || ['thermal', 'yolo'].includes(b.camera)) o.camera = b.camera;
  if (b.flags && typeof b.flags === 'object') o.flags = { high_temperature: b.flags.high_temperature ? 1 : 0, abnormal_behavior: b.flags.abnormal_behavior ? 1 : 0 };
  if (Number.isFinite(b.threshold_c)) o.threshold_c = b.threshold_c;
  if (Number.isFinite(b.uptime_s)) o.uptime_s = b.uptime_s;
  if (typeof b.status === 'string') o.status = str(b.status, 200);
  if (b.sms && typeof b.sms === 'object') o.sms = { enabled: !!b.sms.enabled, state: str(b.sms.state, 20), detail: str(b.sms.detail, 200), time: str(b.sms.time, 40) || null };
  return o;
}

function applyDevice(pen, b) {
  const d = devGet(pen);
  if (b.state && typeof b.state === 'object') { d.state = { ...(d.state || {}), ...cleanState(b.state) }; d.at = Date.now(); }
  if (b.event && typeof b.event === 'object') devIns.run(new Date().toISOString(), pen, str(b.event.kind, 40) || 'event', str(b.event.detail, 300));
}

const POSTURES = new Set(['Lying', 'Not lying', 'Unknown']);
const EVENT_TYPES = new Set(['Measurement', 'Session started', 'Session ended', 'Alert', 'Alert acknowledged']);
const SC = cfg.screening;
const criteria = () => ({
  surface_temp_threshold_c: SC.surface_temp_threshold_c,
  consecutive_readings: SC.consecutive_readings,
  min_lying_duration_s: SC.min_lying_duration_s,
  configured: SC.surface_temp_threshold_c !== null,
});

// ---- in-memory live state (derived from the pipeline's timestamps; rebuilt from DB after restart)
const live = new Map(); // pen_id -> { sid, ts, tsMs, temp, posture, lyingS, consecutive, temps[], lastLoggedMs }

const q = {
  active: db.prepare('SELECT * FROM sessions WHERE pen_id = ? AND ended_at IS NULL'),
  allActive: db.prepare('SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY id'),
  lastEnded: db.prepare('SELECT id FROM sessions WHERE pen_id = ? AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 1'),
  lastMeas: db.prepare("SELECT ts, surface_temp_c, posture FROM records WHERE session_id = ? AND event_type = 'Measurement' ORDER BY id DESC LIMIT 1"),
  lastAlert: db.prepare("SELECT ts, detail FROM records WHERE session_id = ? AND event_type = 'Alert' ORDER BY id DESC LIMIT 1"),
  insSession: db.prepare('INSERT INTO sessions (pen_id, started_at) VALUES (?, ?)'),
  endSession: db.prepare('UPDATE sessions SET ended_at = ?, lying_s = ? WHERE id = ?'),
  insRecord: db.prepare(`INSERT INTO records (ts, pen_id, session_id, event_type, surface_temp_c, posture, lying_s, screening_status, detail)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`),
  saveLying: db.prepare('UPDATE sessions SET lying_s = ? WHERE id = ?'),
  setAlert: db.prepare('UPDATE sessions SET alert_active = ? WHERE id = ?'),
};

for (const s of q.allActive.all()) {
  live.set(s.pen_id, { sid: s.id, ts: null, tsMs: null, temp: null, posture: null, lyingS: s.lying_s, consecutive: 0, temps: [], lastLoggedMs: 0 });
}

function getLive(sess) {
  let l = live.get(sess.pen_id);
  if (l && l.sid === sess.id) return l;
  const m = q.lastMeas.get(sess.id);
  l = { sid: sess.id, ts: m ? m.ts : null, tsMs: m ? Date.parse(m.ts) : null,
        temp: m ? m.surface_temp_c : null, posture: m ? m.posture : null,
        lyingS: sess.lying_s, consecutive: 0, temps: [], lastLoggedMs: m ? Date.parse(m.ts) : 0 };
  live.set(sess.pen_id, l);
  return l;
}

function screening(l, nowMs) {
  // Returns { status, note }. Never claims a diagnosis; never invents criteria.
  if (!l || l.ts === null) return { status: 'Inconclusive', note: 'No reading received yet for this session.' };
  const ageS = (nowMs - l.tsMs) / 1000;
  if (ageS > cfg.stale_after_s) return { status: 'Inconclusive', note: `Data stale: last reading ${Math.round(ageS)} s ago.` };
  if (l.temp === null) return { status: 'Inconclusive', note: 'Surface temperature unavailable in latest reading.' };
  if (SC.surface_temp_threshold_c === null) return { status: 'Monitoring', note: 'No screening threshold configured; showing measurements only.' };
  if (l.consecutive >= SC.consecutive_readings) return { status: 'Threshold reached', note: 'Configured screening condition met. This is not a diagnosis.' };
  return { status: 'Monitoring', note: '' };
}

function penView(pen, nowMs) {
  const s = q.active.get(pen);
  const v = { pen_id: pen, session: null, latest: null, status: 'No active session', status_note: '', stale: false, age_s: null, alert: null, last_ended_session: null };
  const e = q.lastEnded.get(pen);
  if (e) v.last_ended_session = sessionCode(e.id);
  if (!s) return v;
  const l = getLive(s);
  v.session = { code: sessionCode(s.id), started_at: s.started_at };
  if (l.ts !== null) {
    v.age_s = Math.max(0, Math.round((nowMs - l.tsMs) / 1000));
    v.stale = v.age_s > cfg.stale_after_s;
    v.latest = { ts: l.ts, surface_temp_c: l.temp, posture: l.posture, lying_s: Math.round(l.lyingS) };
  } else { v.stale = true; }
  const sc = screening(l, nowMs);
  v.status = sc.status; v.status_note = sc.note;
  const a = q.lastAlert.get(s.id);
  if (a) v.alert = { ts: a.ts, active: !!s.alert_active, detail: JSON.parse(a.detail) };
  return v;
}

// ---- HTTP helpers
const CSP = "default-src 'self'; img-src 'self' data: http: https:; frame-ancestors 'none'; base-uri 'none'";
function send(res, code, body, type = 'application/json; charset=utf-8') {
  res.writeHead(code, { 'Content-Type': type, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': CSP });
  res.end(typeof body === 'string' || Buffer.isBuffer(body) ? body : JSON.stringify(body));
}
const fail = (res, code, error) => send(res, code, { ok: false, error });
function readJson(req) {
  return new Promise((resolve, reject) => {
    let n = 0; const chunks = [];
    req.on('data', (c) => { n += c.length; if (n > 8192) { reject(Object.assign(new Error('Body too large'), { code: 413 })); req.destroy(); } else chunks.push(c); });
    req.on('end', () => { try { resolve(chunks.length ? JSON.parse(Buffer.concat(chunks)) : {}); } catch { reject(Object.assign(new Error('Invalid JSON'), { code: 400 })); } });
    req.on('error', reject);
  });
}
const isLoopback = (a) => a === '127.0.0.1' || a === '::1' || a === '::ffff:127.0.0.1';
const ingestRate = new Map();
function ingestRateAllowed(address) {
  const key = address || 'unknown';
  const now = Date.now();
  const bucket = ingestRate.get(key) || { startedAt: now, count: 0 };
  if (now - bucket.startedAt > 1000) {
    bucket.startedAt = now;
    bucket.count = 0;
  }
  bucket.count += 1;
  ingestRate.set(key, bucket);
  return bucket.count <= cfg.ingest_rate_limit_per_sec;
}
function validateEvidencePath(value) {
  if (value == null || value === '') return null;
  if (typeof value !== 'string') return null;
  const normalized = value.replace(/\\/g, '/');
  if (normalized.startsWith('/') || normalized.startsWith('../') || normalized.includes('..') || /^[A-Za-z]:\//.test(normalized)) return null;
  return normalized;
}
// State-changing UI routes: no accounts (out of scope), so block cross-site/accidental requests.
function sameOriginUi(req) {
  if (req.headers['x-requested-with'] !== 'dashboard') return false;
  const o = req.headers.origin;
  if (o) { try { if (new URL(o).host !== req.headers.host) return false; } catch { return false; } }
  return true;
}

// ---- Session lifecycle (server clock is used for lifecycle events)
function startSession(pen) {
  const now = new Date().toISOString();
  const id = tx(db, () => {
    if (q.active.get(pen)) throw Object.assign(new Error(`${pen} already has an active session.`), { code: 409 });
    const sid = Number(q.insSession.run(pen, now).lastInsertRowid);
    q.insRecord.run(now, pen, sid, 'Session started', null, null, 0, null, null);
    return sid;
  });
  live.delete(pen); // fresh counters: nothing inherited from any prior session
  return { session: sessionCode(id), started_at: now };
}
function endSession(pen) {
  const now = new Date().toISOString();
  return tx(db, () => {
    const s = q.active.get(pen);
    if (!s) throw Object.assign(new Error(`${pen} has no active session.`), { code: 409 });
    const l = live.get(pen);
    const lying = l && l.sid === s.id ? l.lyingS : s.lying_s;
    q.endSession.run(now, lying, s.id);
    q.insRecord.run(now, pen, s.id, 'Session ended', null, null, lying, null, null);
    live.delete(pen);
    return { session: sessionCode(s.id), ended_at: now };
  });
}
function ackAlert(pen) {
  const now = new Date().toISOString();
  return tx(db, () => {
    const s = q.active.get(pen);
    if (!s || !s.alert_active) throw Object.assign(new Error('No unacknowledged alert for this pen.'), { code: 409 });
    q.setAlert.run(0, s.id);
    q.insRecord.run(now, pen, s.id, 'Alert acknowledged', null, null, null, null, null);
    return { session: sessionCode(s.id) };
  });
}

// ---- Ingest from the EXISTING monitoring pipeline (loopback only)
function ingest(b) {
  const bad = (m) => Object.assign(new Error(m), { code: 422 });
  if (!PENS.includes(b.pen_id)) throw bad('Unknown pen_id.');
  if (!POSTURES.has(b.posture)) throw bad("posture must be 'Lying', 'Not lying' or 'Unknown'.");
  let tsMs = typeof b.timestamp === 'number' ? b.timestamp * 1000 : Date.parse(b.timestamp);
  if (b.timestamp === undefined || b.timestamp === null || !Number.isFinite(tsMs)) throw bad('A valid pipeline timestamp (ISO 8601 or epoch seconds) is required; none is invented.');
  if (tsMs > Date.now() + 5000) throw bad('Timestamp is in the future.');
  let temp = b.surface_temp_c;
  if (temp === undefined || temp === null) temp = null;
  else if (typeof temp !== 'number' || !Number.isFinite(temp) || temp < 0 || temp > 60) throw bad('surface_temp_c must be a number in 0-60 °C or null.');
  const sess = q.active.get(b.pen_id);
  if (!sess) throw Object.assign(new Error('No active session for this pen; reading rejected.'), { code: 409 });
  if (tsMs < Date.parse(sess.started_at)) throw bad('Reading predates the active session; rejected.');
  const l = getLive(sess);
  if (l.tsMs !== null && tsMs <= l.tsMs) throw bad('Out-of-order or duplicate timestamp; rejected.');

  // Lying duration from pipeline timestamps only. The prior interval is credited to the prior posture,
  // and gaps longer than max_lying_gap_s are not counted (unobserved time).
  let lyingS = l.lyingS;
  if (l.posture === 'Lying' && l.tsMs !== null) {
    const dt = (tsMs - l.tsMs) / 1000;
    if (dt <= cfg.max_lying_gap_s) lyingS += dt;
  }
  const ts = new Date(tsMs).toISOString();
  const temps = [...l.temps, temp].slice(-SC.consecutive_readings);
  const met = SC.surface_temp_threshold_c !== null && temp !== null
    && temp >= SC.surface_temp_threshold_c && lyingS >= SC.min_lying_duration_s;
  const consecutive = met ? l.consecutive + 1 : 0;
  const next = { sid: sess.id, ts, tsMs, temp, posture: b.posture, lyingS, consecutive, temps, lastLoggedMs: l.lastLoggedMs };
  const status = screening(next, tsMs).status;
  const fire = consecutive >= SC.consecutive_readings && !sess.alert_active;
  const due = fire || tsMs - l.lastLoggedMs >= cfg.log_interval_s * 1000;

  tx(db, () => {
    if (due) {
      q.insRecord.run(ts, b.pen_id, sess.id, 'Measurement', temp, b.posture, lyingS, status, null);
    }
    q.saveLying.run(lyingS, sess.id);
    if (fire) {
      const detail = { criteria: criteria(), triggering: { surface_temp_c_readings: temps, posture: b.posture, lying_s: Math.round(lyingS), timestamp: ts }, note: 'Screening result, not a veterinary diagnosis.' };
      q.insRecord.run(ts, b.pen_id, sess.id, 'Alert', temp, b.posture, lyingS, 'Threshold reached', JSON.stringify(detail));
      q.setAlert.run(1, sess.id);
    }
  });
  if (due) next.lastLoggedMs = tsMs;
  live.set(b.pen_id, next);
  return { ok: true, logged: due, alert_raised: fire };
}

// ---- Log queries (parameterised; filters whitelisted)
function queryLog(u) {
  const where = []; const args = [];
  const pen = u.searchParams.get('pen'); const sess = u.searchParams.get('session');
  const type = u.searchParams.get('type'); const before = u.searchParams.get('before');
  if (pen) { if (!/^[A-Za-z0-9_-]{1,32}$/.test(pen)) throw Object.assign(new Error('Bad pen id.'), { code: 400 }); where.push('r.pen_id = ?'); args.push(pen); }
  if (sess) { const m = /^SESSION-(\d{1,9})$/.exec(sess); if (!m) throw Object.assign(new Error('Bad session id.'), { code: 400 }); where.push('r.session_id = ?'); args.push(Number(m[1])); }
  if (type) { if (!EVENT_TYPES.has(type)) throw Object.assign(new Error('Bad event type.'), { code: 400 }); where.push('r.event_type = ?'); args.push(type); }
  if (before && /^\d{1,12}$/.test(before)) { where.push('r.id < ?'); args.push(Number(before)); }
  const limit = Math.min(500, Math.max(1, Number(u.searchParams.get('limit')) || 100));
  const rows = db.prepare(`SELECT r.id, r.ts, r.pen_id, r.session_id, r.event_type, r.surface_temp_c, r.posture, r.lying_s,
      r.screening_status, r.detail, r.evidence_path FROM records r ${where.length ? 'WHERE ' + where.join(' AND ') : ''}
      ORDER BY r.id DESC LIMIT ?`).all(...args, limit);
  return rows.map((r) => ({ ...r, session: sessionCode(r.session_id), detail: r.detail ? JSON.parse(r.detail) : null }));
}
function listSessions() {
  return db.prepare(`SELECT s.id, s.pen_id, s.started_at, s.ended_at, s.lying_s,
      (SELECT COUNT(*) FROM records r WHERE r.session_id = s.id AND r.event_type = 'Measurement') AS measurements,
      (SELECT COUNT(*) FROM records r WHERE r.session_id = s.id AND r.event_type = 'Alert') AS alerts
      FROM sessions s ORDER BY s.id DESC LIMIT 200`).all().map((s) => ({ ...s, session: sessionCode(s.id) }));
}

// ---- Optional camera preview (memory only; never touches the DB, never opens a camera)
// The pipeline pushes its latest frame; only the newest frame per pen/kind is kept.
const PV = cfg.preview;
const KINDS = new Set(['rgb', 'thermal']);
const frames = new Map(); // `${pen}/${kind}` -> { buf, at }
const previewRate = new Map();
function previewRateAllowed(address) {
  const key = address || 'unknown'; const now = Date.now();
  const b = previewRate.get(key) || { startedAt: now, count: 0 };
  if (now - b.startedAt > 1000) { b.startedAt = now; b.count = 0; }
  b.count += 1; previewRate.set(key, b);
  return b.count <= PV.rate_limit_per_sec;
}
function readFrame(req) {
  return new Promise((resolve, reject) => {
    const declared = Number(req.headers['content-length']);
    if (Number.isFinite(declared) && declared > PV.max_frame_bytes) { req.resume(); return reject(Object.assign(new Error(`Frame too large (max ${PV.max_frame_bytes} bytes). Downscale or lower JPEG quality in the pipeline.`), { code: 413 })); }
    let n = 0; const chunks = [];
    req.on('data', (c) => { n += c.length; if (n > PV.max_frame_bytes) { reject(Object.assign(new Error('Frame too large.'), { code: 413 })); req.destroy(); } else chunks.push(c); });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}
const isJpeg = (b) => b.length > 4 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff;
function previewStatus(now) {
  const pens = {};
  for (const pen of PENS) {
    pens[pen] = {};
    for (const k of KINDS) {
      const f = frames.get(`${pen}/${k}`);
      pens[pen][k] = f ? { age_s: Math.round((now - f.at) / 100) / 10 } : null;
    }
  }
  return { enabled: PV.enabled, refresh_ms: PV.refresh_ms, stale_after_s: PV.stale_after_s, pens };
}
function storeFrame(pen, kind, buf) {
  if (!PV.enabled || !isJpeg(buf) || buf.length > PV.max_frame_bytes) return false;
  frames.set(`${pen}/${kind}`, { buf: Buffer.from(buf), at: Date.now() });
  return true;
}
// ---- Static files (fixed whitelist; no user-controlled paths)
const STATIC = { '/': ['index.html', 'text/html; charset=utf-8'], '/app.js': ['app.js', 'text/javascript; charset=utf-8'], '/style.css': ['style.css', 'text/css; charset=utf-8'] };

const server = http.createServer(async (req, res) => {
  try {
    const u = new URL(req.url, 'http://localhost');
    const p = u.pathname; const m = req.method;
    if (m === 'GET' && STATIC[p]) return send(res, 200, fs.readFileSync(path.join(__dirname, 'public', STATIC[p][0])), STATIC[p][1]);

    if (m === 'GET' && p === '/api/status') {
      const now = Date.now();
      return send(res, 200, { server_time: new Date(now).toISOString(), stale_after_s: cfg.stale_after_s, criteria: criteria(), pens: PENS.map((x) => { const v = penView(x, now); const d = dev.get(x); v.device = d && d.state ? { ...d.state, age_s: Math.round((now - d.at) / 1000) } : null; return v; }), registry: reg.all.all() });
    }
    const exp = /^\/api\/sessions\/SESSION-(\d{1,9})\/export\.(csv|pdf)$/.exec(p);
    if (m === 'GET' && exp) { // read-only download of one session's records
      const sid = Number(exp[1]);
      const sess = db.prepare('SELECT id, pen_id, started_at, ended_at, lying_s FROM sessions WHERE id = ?').get(sid);
      if (!sess) return fail(res, 404, 'Session not found.');
      const code = sessionCode(sid);
      const rows = db.prepare('SELECT ts, pen_id, event_type, surface_temp_c, posture, lying_s, screening_status, detail FROM records WHERE session_id = ? ORDER BY id').all(sid)
        .map((r) => ({ ...r, detail: r.detail ? JSON.parse(r.detail) : null }));
      const csv = exp[2] === 'csv';
      const body = csv ? Buffer.from(buildCsv(code, rows), 'utf8') : buildPdf(sess, code, rows);
      res.writeHead(200, { 'Content-Type': csv ? 'text/csv; charset=utf-8' : 'application/pdf', 'Content-Disposition': `attachment; filename="${code}_${sess.pen_id}.${exp[2]}"`, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': CSP });
      return res.end(body);
    }
    if (m === 'GET' && p === '/api/discovered') return send(res, 200, discovered(req.headers.host));
    if (m === 'GET' && p === '/api/log') return send(res, 200, { records: queryLog(u) });
    if (m === 'GET' && p === '/api/sessions') return send(res, 200, { sessions: listSessions() });

    // ---- Optional preview routes
    if (m === 'GET' && p === '/api/preview/status') return send(res, 200, previewStatus(Date.now()));
    const pvGet = /^\/api\/preview\/([A-Za-z0-9_-]{1,32})\/(rgb|thermal)\.jpg$/.exec(p);
    if (m === 'GET' && pvGet) {
      if (!PV.enabled) return fail(res, 404, 'Preview is disabled.');
      const f = frames.get(`${pvGet[1]}/${pvGet[2]}`);
      if (!f) return fail(res, 404, 'No frame received yet.');
      return send(res, 200, f.buf, 'image/jpeg');
    }
    const pvPost = /^\/api\/preview\/([A-Za-z0-9_-]{1,32})\/(rgb|thermal)$/.exec(p);
    if (m === 'POST' && pvPost) {
      if (!PV.enabled) return fail(res, 404, 'Preview is disabled in config.json (preview.enabled).');
      const remote = req.socket.remoteAddress || 'unknown';
      if (!isLoopback(remote)) return fail(res, 403, 'Preview frames are accepted from the Pi itself only.');
      if (!previewRateAllowed(remote)) return fail(res, 429, 'Too many preview frames. Push at 1-2 frames per second.');
      if (cfg.ingest_token && req.headers['x-ingest-token'] !== cfg.ingest_token) return fail(res, 403, 'Bad ingest token.');
      if (!PENS.includes(pvPost[1])) return fail(res, 404, 'Unknown pen.');
      const buf = await readFrame(req);
      if (!isJpeg(buf)) return fail(res, 415, 'Body must be a JPEG image (Content-Type: image/jpeg).');
      storeFrame(pvPost[1], pvPost[2], buf);
      return send(res, 200, { ok: true, bytes: buf.length });
    }

    // ---- Prototype device channel
    const devPost = /^\/api\/device\/([A-Za-z0-9_-]{1,32})$/.exec(p);
    const devCmds = /^\/api\/device\/([A-Za-z0-9_-]{1,32})\/commands$/.exec(p);
    if ((m === 'POST' && devPost) || (m === 'GET' && devCmds)) {
      const remote = req.socket.remoteAddress || 'unknown';
      if (!isLoopback(remote)) return fail(res, 403, 'Device data is accepted from the dashboard host only.');
      if (cfg.ingest_token && req.headers['x-ingest-token'] !== cfg.ingest_token) return fail(res, 403, 'Bad ingest token.');
      const pen = (devPost || devCmds)[1];
      if (!PENS.includes(pen)) return fail(res, 404, 'Unknown pen.');
      const d = devGet(pen);
      if (devCmds) return send(res, 200, { ok: true, commands: d.cmds.splice(0) });
      applyDevice(pen, await readJson(req));
      return send(res, 200, { ok: true });
    }
    if (m === 'GET' && p === '/api/device/events') {
      const pen = u.searchParams.get('pen') || '';
      const lim = Math.min(200, Math.max(1, Number(u.searchParams.get('limit')) || 50));
      return send(res, 200, { events: devList.all(pen, pen, lim) });
    }
    const cmdPost = /^\/api\/pens\/([A-Za-z0-9_-]{1,32})\/command$/.exec(p);
    if (m === 'POST' && cmdPost) { // UI -> prototype (delivered by the bridge): reset flags, choose camera
      if (!sameOriginUi(req)) return fail(res, 403, 'Request rejected (missing or cross-origin request header).');
      if (!reg.get.get(cmdPost[1])) return fail(res, 404, 'Pen is not on the dashboard.');
      const b = await readJson(req);
      let cmd;
      if (b.action === 'reset') cmd = { action: 'reset' };
      else if (b.action === 'camera' && ['auto', 'thermal', 'yolo', 'stop'].includes(b.kind)) cmd = { action: 'camera', kind: b.kind };
      else return fail(res, 422, 'Unknown command.');
      if (hub) {
        const dv = penToDev.get(cmdPost[1]);
        if (!dv) return fail(res, 422, 'This pen has no valid prototype address.');
        await hub.sendCommand(dv, cmd);
      } else { const d = devGet(cmdPost[1]); d.cmds.push(cmd); if (d.cmds.length > 10) d.cmds.shift(); } // legacy: picked up by tools/mqtt_bridge.py
      return send(res, 200, { ok: true });
    }

    // ---- Pen registry (dashboard-managed pens)
    if (m === 'POST' && p === '/api/pens') {
      if (!sameOriginUi(req)) return fail(res, 403, 'Request rejected (missing or cross-origin request header).');
      const b = await readJson(req);
      const pen = typeof b.pen_id === 'string' ? b.pen_id.trim() : '';
      let address = typeof b.address === 'string' ? b.address.trim() : '';
      if (!address && typeof b.device_id === 'string' && /^[A-Za-z0-9_-]{1,40}$/.test(b.device_id)) { // picked from the discovered list
        const used = devToPen.get(b.device_id);
        if (used) return fail(res, 409, `That prototype is already used by ${used}.`);
        address = discovered(req.headers.host).devices.find((x) => x.device_id === b.device_id)?.address || `mqtt://localhost:${cfg.mqtt.port}/${b.device_id}`;
      }
      const camera = typeof b.camera === 'string' ? b.camera.trim() : '';
      if (!/^[A-Za-z0-9_-]{1,32}$/.test(pen)) return fail(res, 422, 'Pen number may use letters, digits, - and _ (max 32).');
      if (!/^\S{1,200}$/.test(address)) return fail(res, 422, 'Prototype address is required (no spaces).');
      if (camera && !/^https?:\/\/\S{1,500}$/i.test(camera)) return fail(res, 422, 'Camera URL must start with http:// or https://');
      if (reg.get.get(pen)) return fail(res, 409, `${pen} is already on the dashboard.`);
      reg.ins.run(pen, address, camera, new Date().toISOString());
      if (!PENS.includes(pen)) PENS.push(pen);
      rebuildDeviceMap();
      return send(res, 200, { ok: true, pen_id: pen });
    }
    const penDel = /^\/api\/pens\/([A-Za-z0-9_-]{1,32})$/.exec(p);
    if (m === 'DELETE' && penDel) { // removes the pen from the dashboard only; sessions and log are kept
      if (!sameOriginUi(req)) return fail(res, 403, 'Request rejected (missing or cross-origin request header).');
      if (!reg.get.get(penDel[1])) return fail(res, 404, 'Pen is not on the dashboard.');
      reg.del.run(penDel[1]);
      rebuildDeviceMap();
      return send(res, 200, { ok: true });
    }
    const sesDel = /^\/api\/sessions\/SESSION-(\d{1,9})$/.exec(p);
    if (m === 'DELETE' && sesDel) { // deletes an ended session together with all of its log records
      if (!sameOriginUi(req)) return fail(res, 403, 'Request rejected (missing or cross-origin request header).');
      const id = Number(sesDel[1]);
      const s = db.prepare('SELECT id, pen_id, ended_at FROM sessions WHERE id = ?').get(id);
      if (!s) return fail(res, 404, 'Session not found.');
      if (s.ended_at === null) return fail(res, 409, 'End the session before deleting it.');
      tx(db, () => { db.prepare('DELETE FROM records WHERE session_id = ?').run(id); db.prepare('DELETE FROM sessions WHERE id = ?').run(id); });
      const l = live.get(s.pen_id); if (l && l.sid === id) live.delete(s.pen_id);
      return send(res, 200, { ok: true });
    }

    const act = /^\/api\/pens\/([A-Za-z0-9_-]{1,32})\/(session\/start|session\/end|alert\/ack)$/.exec(p);
    if (m === 'POST' && act) {
      if (!sameOriginUi(req)) return fail(res, 403, 'Request rejected (missing or cross-origin request header).');
      const pen = act[1];
      if (!PENS.includes(pen)) return fail(res, 404, 'Unknown pen.');
      const r = act[2] === 'session/start' ? startSession(pen) : act[2] === 'session/end' ? endSession(pen) : ackAlert(pen);
      return send(res, 200, { ok: true, ...r }); // reached only after the DB commit succeeded
    }
    if (m === 'POST' && p === '/api/ingest') {
      const remote = req.socket.remoteAddress || 'unknown';
      if (!isLoopback(remote)) return fail(res, 403, 'Ingest is accepted from the Pi itself only.');
      if (!ingestRateAllowed(remote)) return fail(res, 429, 'Too many ingest requests. Please slow the pipeline output rate.');
      if (cfg.ingest_token && req.headers['x-ingest-token'] !== cfg.ingest_token) return fail(res, 403, 'Bad ingest token.');
      return send(res, 200, ingest(await readJson(req)));
    }
    return fail(res, 404, 'Not found.');
  } catch (e) {
    const code = Number.isInteger(e.code) && e.code >= 400 && e.code < 600 ? e.code : 500;
    if (code === 500) console.error('[error]', e.message); // message only: no env/secrets in logs
    return fail(res, code, code === 500 ? 'Database or server error; the action was NOT saved.' : e.message);
  }
});

server.listen(cfg.port, cfg.host, () => {
  console.log(`Swine dashboard listening on http://${cfg.host}:${cfg.port}`);
  console.log(`Bound address: ${cfg.host}:${cfg.port}. No authentication or encryption is configured; access from other devices is only enabled when HOST is set to the LAN interface or config.json host is changed explicitly.`);
  console.log(`DB: ${cfg.db_path} | screening threshold: ${SC.surface_temp_threshold_c === null ? 'NOT configured (no alerts will fire)' : SC.surface_temp_threshold_c + ' °C'}`);
});
// ---- Devices that connected but are not yet a pen (offered in the Add-pen dialog)
const seen = new Map(); // device id -> { ip, at }
function noteUnknown(dev, ip) {
  if (devToPen.has(dev)) return;
  seen.delete(dev); seen.set(dev, { ip, at: Date.now() });
  if (seen.size > 50) seen.delete(seen.keys().next().value); // bounded memory
}
function discovered(hostHeader) {
  let host = 'localhost';
  try { host = new URL('http://' + (hostHeader || 'localhost')).hostname || host; } catch {}
  const now = Date.now(); const live = new Set(hub ? hub.connectedDevices() : []);
  const devices = [...seen.entries()].filter(([d, v]) => !devToPen.has(d) && now - v.at < 60000)
    .sort((a, b) => b[1].at - a[1].at)
    .map(([d, v]) => ({ device_id: d, ip: v.ip, age_s: Math.round((now - v.at) / 1000), connected: live.has(d), address: `mqtt://${host}:${cfg.mqtt.port}/${d}` }));
  let n = 1; while (PENS.includes(`PEN-${String(n).padStart(2, '0')}`)) n++;
  return { devices, suggested_pen_id: `PEN-${String(n).padStart(2, '0')}`, broker: !!hub };
}

// ---- Built-in MQTT broker + subscriber (replaces Mosquitto and tools/mqtt_bridge.py)
let hub = null; let beacon = null;
const warnAt = new Map();
const quiet = (key, msg) => { const t = Date.now(); if (t - (warnAt.get(key) || 0) > 60000) { warnAt.set(key, t); console.log(msg); } };
if (cfg.mqtt.enabled) {
  createHub({
    mqtt: cfg.mqtt, resolve: (d) => devToPen.get(d) || null,
    onReading: (pen, r) => {
      try { ingest({ pen_id: pen, timestamp: new Date().toISOString(), surface_temp_c: r.surface_temp_c, posture: r.posture || 'Unknown' }); } // arrival time: Pi clock drift cannot reject data
      catch (e) { quiet(`${pen}/${e.code}`, `[mqtt] ${pen}: reading rejected (${e.code || 500}): ${e.message}`); }
    },
    onState: (pen, s) => applyDevice(pen, { state: s }),
    onEvent: (pen, ev) => applyDevice(pen, { event: ev }),
    onPreview: (pen, kind, buf) => storeFrame(pen, kind, buf),
    onUnknown: noteUnknown,
  }).then((h) => {
    hub = h;
    if (cfg.mqtt.beacon) beacon = startBeacon({ port: cfg.mqtt.discovery_port, mqttPort: cfg.mqtt.port, requireAuth: h.requireAuth });
    console.log(`MQTT broker listening on ${cfg.mqtt.host}:${cfg.mqtt.port} (${h.requireAuth ? `login required, user "${cfg.mqtt.username}"` : 'OPEN: no password set; use only on a trusted network'}). Do not also run tools/mqtt_bridge.py or Mosquitto.`);
  }).catch((e) => { console.error(`[error] MQTT broker is NOT running: ${e.message}. (If the port is busy, stop Mosquitto or change mqtt.port; to turn the broker off set mqtt.enabled=false or MQTT_ENABLED=0.) The dashboard itself keeps running.`); });
}
const shutdown = () => server.close(async () => { if (beacon) beacon.close(); if (hub) await hub.close(); db.close(); process.exit(0); });
process.on('SIGINT', shutdown); process.on('SIGTERM', shutdown);