'use strict';
// MQTT over WebSockets + dashboard login, end to end:  node tools/ws_test.js
// Starts the real server (temporary database, settings from environment variables as on Render), then acts as a
// prototype: speaks MQTT 3.1.1 byte for byte over a real WebSocket to /mqtt, so no MQTT client library is needed.
// Needs the 'aedes' and 'ws' packages (npm install).
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
let WebSocket;
try { WebSocket = require('ws'); } catch { console.error("The 'ws' package is missing. Run: npm install ws@8"); process.exit(1); }

const ROOT = path.join(__dirname, '..');
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swine-ws-'));
const PORT = 20000 + Math.floor(Math.random() * 2000);
const BASE = `http://127.0.0.1:${PORT}`;
const MQTT_PASS = 'mqtt-s3cret', USER = 'admin', PASS = 'dash-pw';
const LOGIN = 'Basic ' + Buffer.from(`${USER}:${PASS}`).toString('base64');
let passed = 0; const failures = [];
const check = (name, cond, extra) => { if (cond) passed++; else failures.push(name + (extra !== undefined ? ` -> ${JSON.stringify(extra).slice(0, 300)}` : '')); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function call(method, p, body, auth = LOGIN) {
  const headers = { 'Content-Type': 'application/json', 'X-Requested-With': 'dashboard' };
  if (auth) headers.Authorization = auth;
  const r = await fetch(BASE + p, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await r.text(); let json = null; try { json = JSON.parse(text); } catch {}
  return { status: r.status, json, text, headers: r.headers };
}

// ---- minimal MQTT 3.1.1 client over a WebSocket
const varint = (n) => { const o = []; do { let b = n % 128; n = Math.floor(n / 128); if (n > 0) b |= 128; o.push(b); } while (n > 0); return Buffer.from(o); };
const pkt = (first, body) => Buffer.concat([Buffer.from([first]), varint(body.length), body]);
const str = (s) => { const b = Buffer.from(s); return Buffer.concat([Buffer.from([b.length >> 8, b.length & 255]), b]); };
function mqttClient(url, clientId, user, pass) {
  return new Promise((resolve) => {
    const ws = new WebSocket(url, ['mqtt']);
    const c = { ws, connack: null, closed: false, inbox: [], pubacks: new Set(), nextId: 1, protocol: null };
    let buf = Buffer.alloc(0);
    ws.on('open', () => {
      c.protocol = ws.protocol;
      const flags = 0x02 | (user ? 0x80 : 0) | (pass ? 0x40 : 0); // clean session + credentials
      const body = Buffer.concat([str('MQTT'), Buffer.from([4, flags, 0, 30]), str(clientId), user ? str(user) : Buffer.alloc(0), pass ? str(pass) : Buffer.alloc(0)]);
      ws.send(pkt(0x10, body));
    });
    ws.on('message', (d) => {
      buf = Buffer.concat([buf, d]);
      for (;;) {
        if (buf.length < 2) break;
        let len = 0, mul = 1, i = 1, b;
        do { b = buf[i++]; len += (b & 127) * mul; mul *= 128; } while (b & 128 && i < buf.length);
        if (buf.length < i + len) break;
        const type = buf[0] >> 4, body = buf.subarray(i, i + len); buf = buf.subarray(i + len);
        if (type === 2) { c.connack = body[1]; resolve(c); }
        if (type === 4) c.pubacks.add(body.readUInt16BE(0));
        if (type === 3) { const n = body.readUInt16BE(0); c.inbox.push({ topic: body.subarray(2, 2 + n).toString(), payload: body.subarray(2 + n).toString() }); }
      }
    });
    ws.on('close', () => { c.closed = true; resolve(c); });
    ws.on('error', () => { c.closed = true; c.error = true; resolve(c); });
    c.publish = (topic, payload, qos = 0) => {
      const data = Buffer.isBuffer(payload) ? payload : Buffer.from(typeof payload === 'string' ? payload : JSON.stringify(payload));
      const id = qos ? c.nextId++ : null;
      ws.send(pkt(0x30 | (qos << 1), Buffer.concat([str(topic), qos ? Buffer.from([id >> 8, id & 255]) : Buffer.alloc(0), data])));
      return id;
    };
    c.subscribe = (topic) => ws.send(pkt(0x82, Buffer.concat([Buffer.from([0, 99]), str(topic), Buffer.from([1])])));
    c.close = () => { try { ws.send(Buffer.from([0xe0, 0])); ws.close(); } catch {} };
  });
}
async function until(fn, ms = 4000) { const end = Date.now() + ms; while (Date.now() < end) { const v = await fn(); if (v) return v; await sleep(100); } return null; }

async function main() {
  const env = { ...process.env, SWINE_DB: path.join(dir, 's.db'), PORT: String(PORT), HOST: '127.0.0.1', MQTT_PASS, MQTT_TCP: '0', MQTT_BEACON: '0',
    DASHBOARD_USER: USER, DASHBOARD_PASSWORD: PASS, SWINE_CONFIG_JSON: JSON.stringify({ pens: [], preview: { enabled: true } }), MQTT_ENABLED: '1', INGEST_TOKEN: '' };
  const srv = spawn(process.execPath, ['server.js'], { cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'] });
  let log = ''; srv.stdout.on('data', (d) => { log += d; }); srv.stderr.on('data', (d) => { log += d; });
  await until(() => log.includes('MQTT broker:'), 8000);
  try {
    // settings from environment variables, broker on the web port only
    check('broker reports WebSockets on /mqtt and TCP off', /TCP off; WebSockets on at path \/mqtt/.test(log), log);
    check('login reported at startup', log.includes('Login required (user "admin")'));

    // login
    check('/healthz open for health checks', (await call('GET', '/healthz', undefined, null)).status === 200);
    const anon = await call('GET', '/', undefined, null);
    check('page without login -> 401 + browser prompt', anon.status === 401 && /Basic realm/.test(anon.headers.get('www-authenticate') || ''));
    check('wrong password -> 401', (await call('GET', '/api/status', undefined, 'Basic ' + Buffer.from('admin:nope').toString('base64'))).status === 401);
    check('correct login -> 200', (await call('GET', '/api/status')).status === 200);

    // WebSocket MQTT: wrong path, wrong password, then a prototype
    const wrongPath = await mqttClient(`ws://127.0.0.1:${PORT}/other`, 'sim-x', 'swine', MQTT_PASS);
    check('other WebSocket paths refused', wrongPath.closed && wrongPath.connack === null);
    const bad = await mqttClient(`ws://127.0.0.1:${PORT}/mqtt`, 'sim-wstest01', 'swine', 'wrong');
    check('wrong MQTT password refused', bad.connack !== 0, bad.connack);
    const pi = await mqttClient(`ws://127.0.0.1:${PORT}/mqtt`, 'sim-wstest01', 'swine', MQTT_PASS);
    check('prototype connects over WebSocket', pi.connack === 0 && !pi.closed, { connack: pi.connack });
    check('mqtt subprotocol accepted', pi.protocol === 'mqtt', pi.protocol);
    pi.subscribe('swine/sim-wstest01/cmd');
    pi.publish('swine/sim-wstest01/status', 'online', 1);
    pi.publish('swine/sim-wstest01/state', { online: true, mode: 'scheduled', status: 'Ready', sms: { enabled: true, state: 'idle' } }, 1);
    const found = await until(async () => (await call('GET', '/api/discovered')).json.devices.find((d) => d.device_id === 'sim-wstest01'));
    check('prototype listed under Add a pen', !!found, found);

    // add it as a pen, start from the dashboard, the start command arrives over the WebSocket
    check('add pen', (await call('POST', '/api/pens', { pen_id: 'PEN-01', device_id: 'sim-wstest01' })).status === 200);
    pi.publish('swine/sim-wstest01/state', { online: true, mode: 'scheduled', status: 'Ready', sms: { enabled: true, state: 'idle' } }, 1);
    await sleep(300);
    const st = await call('POST', '/api/pens/PEN-01/session/start', {});
    check('start accepted (prototype seen as connected)', st.status === 200 && st.json.scheduled, st.json);
    const cmd = await until(() => pi.inbox.map((x) => JSON.parse(x.payload)).find((x) => x.action === 'start'));
    check('start command delivered over the WebSocket', cmd && /^dash-/.test(cmd.uid) && !!Date.parse(cmd.server_time), cmd);
    if (!cmd) throw new Error('no start command reached the prototype');

    // the prototype confirms and sends a capture with images
    const t0 = Date.now();
    const sess = { uid: cmd.uid, state: 'running', pen: 'PEN-01', test: false, started_at: new Date(t0).toISOString(), ends_at: new Date(t0 + 68400000).toISOString(),
      interval_s: 1800, duration_s: 68400, captures: 39, next_seq: 1, last_seq: 0, next_capture_at: new Date(t0 + 1800000).toISOString(), capturing: false,
      clock_source: 'dashboard', rule: { threshold_c: 36.7, consecutive_captures: 6, lookback_captures: 3 }, ended_at: null, result: null, sms: null };
    const sid = pi.publish('swine/sim-wstest01/session', sess, 1);
    const smp = pi.publish('swine/sim-wstest01/sample', { uid: cmd.uid, seq: 0, ts: new Date(t0).toISOString(), ok: true, calibrated: true, images: {},
      pigs: [{ pig_id: 1, observed: true, posture: 'Lying', lying_s: 1800, surface_temp_c: 37.1, behavioral_status: 'Normal', thermal_status: 'Elevated' }] }, 1);
    const jpeg = Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe0]), Buffer.alloc(2000, 7), Buffer.from([0xff, 0xd9])]);
    pi.publish('swine/sim-wstest01/preview/rgb', jpeg, 0);
    check('QoS 1 messages acknowledged', !!(await until(() => pi.pubacks.has(sid) && pi.pubacks.has(smp))));
    const pen = await until(async () => { const p = (await call('GET', '/api/status')).json.pens.find((x) => x.pen_id === 'PEN-01'); return p && p.schedule && p.schedule.last ? p : null; });
    check('session and capture recorded', pen && pen.status === 'Monitoring' && pen.schedule.last.pigs[0].surface_temp_c === 37.1, pen && pen.schedule);
    check('prototype shown online', pen && pen.device && pen.device.online === true);
    const pv = (await call('GET', '/api/preview/status')).json;
    check('capture image received over the WebSocket', pv.pens['PEN-01'] && pv.pens['PEN-01'].rgb, pv.pens);

    // End from the dashboard reaches the prototype
    const end = await call('POST', '/api/pens/PEN-01/session/end', {});
    check('End asks the connected prototype', end.status === 200 && end.json.pending === true, end.json);
    const stop = await until(() => pi.inbox.map((x) => JSON.parse(x.payload)).find((x) => x.action === 'stop'));
    check('stop command delivered over the WebSocket', stop && stop.uid === cmd.uid, stop);

    // a prototype may only publish under its own id (the broker closes the connection otherwise)
    pi.publish('swine/other-device/state', { online: true }, 0);
    await sleep(500);
    check('publishing as another device is refused', !(await call('GET', '/api/discovered')).json.devices.find((d) => d.device_id === 'other-device'));
    pi.close();
  } catch (e) {
    failures.push(`stopped early: ${e.message}. Server log:\n${log}`);
  } finally {
    srv.kill();
  }
  if (/\[error\]/.test(log)) failures.push('server logged an error: ' + log.split('\n').filter((l) => l.includes('[error]')).join(' | '));
  console.log(`${passed} passed, ${failures.length} failed`);
  for (const f of failures) console.log('  FAIL ' + f);
  fs.rmSync(dir, { recursive: true, force: true });
  process.exit(failures.length ? 1 : 0);
}
main().catch((e) => { console.error(e); process.exit(1); });
