'use strict';
// Preview smoke test. Temp DB + temp config, so the real database is never touched.
// Run from the project root:  node tools/preview_test.js
const { spawn } = require('node:child_process');
const fs = require('node:fs'); const os = require('node:os'); const path = require('node:path');
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swine-pv-'));
const cfgFile = path.join(dir, 'config.json');
const PORT = 18090; const base = `http://127.0.0.1:${PORT}`;
const root = path.join(__dirname, '..');
let failures = 0;
const check = (name, ok) => { console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}`); if (!ok) failures++; };
const jpeg = (n = 2000) => Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe0]), Buffer.alloc(n, 1), Buffer.from([0xff, 0xd9])]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function run(previewEnabled) {
  fs.writeFileSync(cfgFile, JSON.stringify({ port: PORT, pens: ['PEN-01'], preview: { enabled: previewEnabled, max_frame_bytes: 10000, rate_limit_per_sec: 5 } }));
  const p = spawn('node', ['server.js'], { cwd: root, env: { ...process.env, SWINE_CONFIG: cfgFile, SWINE_DB: path.join(dir, `t${previewEnabled}.db`) }, stdio: 'ignore' });
  for (let i = 0; i < 30; i++) { try { await fetch(`${base}/api/status`); break; } catch { await sleep(200); } }
  return p;
}
const post = (url, body, type = 'image/jpeg') => fetch(base + url, { method: 'POST', headers: { 'Content-Type': type }, body });

(async () => {
  let p = await run(false);
  check('disabled: POST rejected', (await post('/api/preview/PEN-01/rgb', jpeg())).status === 404);
  check('disabled: status says enabled=false', (await (await fetch(`${base}/api/preview/status`)).json()).enabled === false);
  check('disabled: frame GET returns 404', (await fetch(`${base}/api/preview/PEN-01/rgb.jpg`)).status === 404);
  p.kill(); await sleep(300);

  p = await run(true);
  check('enabled: status says enabled=true', (await (await fetch(`${base}/api/preview/status`)).json()).enabled === true);
  check('no frame yet -> 404', (await fetch(`${base}/api/preview/PEN-01/rgb.jpg`)).status === 404);
  check('valid RGB frame accepted', (await post('/api/preview/PEN-01/rgb', jpeg())).status === 200);
  const got = await fetch(`${base}/api/preview/PEN-01/rgb.jpg`);
  check('frame served back as image/jpeg', got.status === 200 && got.headers.get('content-type') === 'image/jpeg');
  check('non-JPEG rejected', (await post('/api/preview/PEN-01/rgb', Buffer.from('not an image'))).status === 415);
  check('oversized frame rejected', (await post('/api/preview/PEN-01/rgb', jpeg(20000))).status === 413);
  check('unknown pen rejected', (await post('/api/preview/PEN-99/rgb', jpeg())).status === 404);
  const st = await (await fetch(`${base}/api/preview/status`)).json();
  check('status: rgb present, thermal absent', !!st.pens['PEN-01'].rgb && st.pens['PEN-01'].thermal === null);
  check('thermal frame accepted separately', (await post('/api/preview/PEN-01/thermal', jpeg())).status === 200);
  await sleep(1100);
  let codes = [];
  for (let i = 0; i < 8; i++) codes.push((await post('/api/preview/PEN-01/rgb', jpeg())).status);
  check('rate limit returns 429 on flood', codes.includes(429));
  // Preview must not alter monitoring state.
  const s2 = await (await fetch(`${base}/api/status`)).json();
  check('monitoring state unaffected (no session created)', s2.pens[0].status === 'No active session');
  p.kill();
  fs.rmSync(dir, { recursive: true, force: true });
  console.log(failures ? `\n${failures} check(s) failed` : '\nAll preview checks passed');
  process.exit(failures ? 1 : 0);
})();
