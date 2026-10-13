'use strict';
// End-to-end test of the built-in MQTT hub. Starts the server, simulates a Pi with the `mqtt` package.
const { spawn } = require('node:child_process');
const path = require('node:path'); const os = require('node:os'); const fs = require('node:fs');
const mqtt = require('mqtt');
const root = path.join(__dirname, '..');
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swine-'));
const cfgFile = path.join(dir, 'config.json');
fs.writeFileSync(cfgFile, JSON.stringify({ port: 18080, pens: [], preview: { enabled: true }, screening: { surface_temp_threshold_c: 35, consecutive_readings: 3 }, mqtt: { port: 11883 } }));
let fails = 0; const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) fails++; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const api = async (method, p, body, headers = {}) => { const r = await fetch('http://127.0.0.1:18080' + p, { method, headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'dashboard', ...headers }, body: body ? JSON.stringify(body) : undefined }); return { s: r.status, j: await r.json().catch(() => ({})), r }; };
const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 0x10, 0x4a, 0x46, 0x49, 0x46, 0, 1, 1, 0, 0, 1, 0, 1, 0, 0, 0xff, 0xd9]);

async function run(env, label, fn) {
  const srv = spawn('node', ['server.js'], { cwd: root, env: { ...process.env, SWINE_CONFIG: cfgFile, SWINE_DB: path.join(dir, label + '.db'), ...env } });
  let out = ''; srv.stdout.on('data', (d) => (out += d)); srv.stderr.on('data', (d) => (out += d));
  // wait (up to 20 s) until the server reports both the web app and the broker, instead of a fixed delay
  for (let i = 0; i < 100 && !(/Swine dashboard listening/.test(out) && /MQTT broker (listening|is NOT running)/.test(out)) && srv.exitCode === null; i++) await sleep(200);
  if (/MQTT broker is NOT running/.test(out) && !/port 11883/.test(out)) console.log('\nSERVER OUTPUT:\n' + out + '\n');
  try { await fn(out); } finally { srv.kill('SIGTERM'); await sleep(400); }
  return out;
}
const connect = (id, extra = {}) => new Promise((res, rej) => { const c = mqtt.connect('mqtt://127.0.0.1:11883', { clientId: id, reconnectPeriod: 0, ...extra }); c.once('connect', () => res(c)); c.once('error', rej); setTimeout(() => rej(new Error('timeout')), 3000); });

(async () => {
  // ---- open broker
  await run({}, 'open', async (out) => {
    ok(/MQTT broker listening on 0\.0\.0\.0:11883 \(OPEN/.test(out), 'broker starts and warns when no password is set');
    const add = await api('POST', '/api/pens', { pen_id: 'PEN-1', address: 'mqtt://192.168.1.10:1883/proto-aaa111' });
    ok(add.s === 200, 'pen added with a prototype address');
    const pi = await connect('proto-aaa111');
    const cmds = []; await pi.subscribeAsync('swine/proto-aaa111/cmd'); pi.on('message', (t, m) => cmds.push(JSON.parse(m)));
    pi.publish('swine/proto-aaa111/status', 'online', { qos: 1 });
    pi.publish('swine/proto-aaa111/state', JSON.stringify({ device_id: 'proto-aaa111', online: true, flags: { high_temperature: 1, abnormal_behavior: 0 }, threshold_c: 35 }), { qos: 1 });
    pi.publish('swine/proto-aaa111/event', JSON.stringify({ kind: 'flag_latched', detail: 'high temperature' }), { qos: 1 });
    await sleep(300);
    let st = (await api('GET', '/api/status')).j.pens[0];
    ok(st.device && st.device.online === true && st.device.flags.high_temperature === 1, 'state from Pi appears in /api/status');
    ok((await api('GET', '/api/device/events?pen=PEN-1')).j.events.length === 1, 'event from Pi is stored');

    pi.publish('swine/proto-aaa111/reading', JSON.stringify({ surface_temp_c: 36.5, posture: 'Lying' }), { qos: 1 });
    await sleep(300);
    ok((await api('GET', '/api/log?pen=PEN-1&type=Measurement')).j.records.length === 0, 'reading without an active session is rejected (no records)');

    ok((await api('POST', '/api/pens/PEN-1/session/start')).s === 200, 'session started');
    for (let i = 0; i < 3; i++) { pi.publish('swine/proto-aaa111/reading', JSON.stringify({ surface_temp_c: 36.5 + i * 0.1, posture: 'Lying' }), { qos: 1 }); await sleep(250); }
    st = (await api('GET', '/api/status')).j.pens[0];
    ok(st.latest && st.latest.surface_temp_c === 36.7 && st.latest.posture === 'Lying', 'reading flows Pi -> broker -> ingest (latest value shown)');
    ok(st.status === 'Threshold reached' && st.alert && st.alert.active, 'screening alert fires after 3 consecutive readings');

    pi.publish('swine/proto-aaa111/preview/rgb', JPEG, { qos: 0 });
    pi.publish('swine/proto-aaa111/preview/thermal', Buffer.from('not a jpeg'), { qos: 0 });
    await sleep(300);
    ok((await fetch('http://127.0.0.1:18080/api/preview/PEN-1/rgb.jpg')).status === 200, 'JPEG preview frame stored');
    ok((await fetch('http://127.0.0.1:18080/api/preview/PEN-1/thermal.jpg')).status === 404, 'non-JPEG preview frame dropped');

    ok((await api('POST', '/api/pens/PEN-1/command', { action: 'reset' })).s === 200, 'reset command accepted');
    ok((await api('POST', '/api/pens/PEN-1/command', { action: 'camera', kind: 'thermal' })).s === 200, 'camera command accepted');
    await sleep(300);
    ok(cmds.length === 2 && cmds[0].action === 'reset' && cmds[1].kind === 'thermal', 'both commands delivered to the Pi over MQTT');

    // ---- ACL / spoofing
    const spy = await connect('proto-zzz999');
    const before = (await api('GET', '/api/status')).j.pens[0].latest.ts;
    spy.publish('swine/proto-aaa111/reading', JSON.stringify({ surface_temp_c: 50, posture: 'Lying' }), { qos: 1 });
    await sleep(300);
    ok((await api('GET', '/api/status')).j.pens[0].latest.ts === before, 'another device cannot publish as proto-aaa111');
    const unk = await connect('proto-unknown');
    unk.publish('swine/proto-unknown/reading', JSON.stringify({ surface_temp_c: 40, posture: 'Lying' }), { qos: 1 });
    await sleep(300);
    ok((await api('GET', '/api/status')).j.pens.length === 1, 'unregistered device is ignored');
    const got = []; spy.on('message', (t) => got.push(t));
    spy.subscribe('swine/#'); await sleep(200); // denied by the ACL; no suback is awaited
    await api('POST', '/api/pens/PEN-1/command', { action: 'reset' }); await sleep(300);
    ok(got.length === 0, 'another device cannot subscribe to swine/# or read commands');

    // ---- discovery: an unregistered device is offered, then adopted by id
    const newPi = await connect('proto-new777');
    newPi.publish('swine/proto-new777/status', 'online', { qos: 1 }); await sleep(300);
    let disc = (await api('GET', '/api/discovered')).j;
    ok(disc.devices.some((d) => d.device_id === 'proto-new777' && d.connected), 'unregistered device shows up in /api/discovered');
    ok(!disc.devices.some((d) => d.device_id === 'proto-aaa111'), 'registered device is not offered again');
    ok(disc.suggested_pen_id === 'PEN-01', 'suggests a free pen number');
    ok((await api('POST', '/api/pens', { pen_id: 'PEN-2', device_id: 'proto-new777' })).s === 200, 'adopt by device_id (no address typed)');
    ok((await api('POST', '/api/pens', { pen_id: 'PEN-3', device_id: 'proto-new777' })).s === 409, 'same prototype cannot be adopted twice');
    ok(!(await api('GET', '/api/discovered')).j.devices.some((d) => d.device_id === 'proto-new777'), 'adopted device leaves the discovered list');
    newPi.end(true);

    // ---- pen removal stops acceptance
    await api('DELETE', '/api/pens/PEN-1');
    pi.publish('swine/proto-aaa111/reading', JSON.stringify({ surface_temp_c: 38, posture: 'Lying' }), { qos: 1 }); await sleep(300);
    ok((await api('GET', '/api/status')).j.pens[0].device !== undefined, 'removing a pen does not crash the hub');
    pi.end(true); spy.end(true); unk.end(true);
  });

  // ---- password mode
  await run({ MQTT_PASS: 's3cret' }, 'auth', async (out) => {
    ok(/login required, user "swine"/.test(out), 'password mode reported at startup');
    let denied = false; try { await connect('proto-x', { username: 'swine', password: 'wrong' }); } catch { denied = true; }
    ok(denied, 'wrong password is refused');
    denied = false; try { await connect('proto-x'); } catch { denied = true; }
    ok(denied, 'anonymous connection is refused when a password is set');
    const good = await connect('proto-x', { username: 'swine', password: 's3cret' });
    ok(good.connected, 'correct password is accepted'); good.end(true);
  });

  // ---- clean failure when the port is taken
  const net = require('node:net'); const blocker = net.createServer().listen(11883, '0.0.0.0');
  await sleep(200);
  const busyOut = await run({}, 'busy2', async () => {});
  ok(/MQTT broker is NOT running/.test(busyOut), 'port-in-use gives a clear message instead of crashing');
  blocker.close();
  console.log(fails ? `\n${fails} FAILED` : '\nALL PASSED'); process.exit(fails ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });