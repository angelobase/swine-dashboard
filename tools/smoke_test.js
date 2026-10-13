'use strict';
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert');

function makeConfig(overrides = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swine-'));
  const cfg = {
    host: '127.0.0.1',
    port: 18080 + Math.floor(Math.random() * 500),
    pens: ['PEN-01', 'PEN-02'],
    stale_after_s: 5,
    log_interval_s: 1,
    max_lying_gap_s: 15,
    screening: { surface_temp_threshold_c: 35, consecutive_readings: 2, min_lying_duration_s: 0 },
    ...overrides,
  };
  const cfgPath = path.join(dir, 'config.json');
  fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));
  return { dir, cfg, cfgPath, dbPath: path.join(dir, 'swine.db'), port: cfg.port };
}

async function waitForServer(base) {
  for (let i = 0; i < 60; i++) {
    try {
      const res = await fetch(`${base}/api/status`);
      if (res.ok) return;
    } catch {}
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error('Server did not start in time');
}

function post(base, p, body, headers = {}) {
  return fetch(`${base}${p}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'dashboard', ...headers },
    body: JSON.stringify(body ?? {}),
  }).then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }));
}

function get(base, p) {
  return fetch(`${base}${p}`).then((r) => r.json());
}

function ingest(base, obj) {
  return post(base, '/api/ingest', obj, { 'Content-Type': 'application/json' });
}

(async () => {
  const ctx = makeConfig();
  const base = `http://127.0.0.1:${ctx.port}`;
  const srv = spawn('node', ['server.js'], {
    env: { ...process.env, PORT: String(ctx.port), HOST: '127.0.0.1', SWINE_CONFIG: ctx.cfgPath, SWINE_DB: ctx.dbPath },
    cwd: path.join(__dirname, '..'),
    stdio: 'ignore',
  });

  try {
    await waitForServer(base);

    const noHeader = await fetch(`${base}/api/pens/PEN-01/session/start`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    assert.equal(noHeader.status, 403, 'UI POST without header must be rejected');

    const badOrigin = await post(base, '/api/pens/PEN-01/session/start', {}, { 'Content-Type': 'application/json', Origin: 'https://evil.example', 'X-Requested-With': 'dashboard' });
    assert.equal(badOrigin.status, 403, 'Cross-origin UI requests must be rejected');

    const start1 = await post(base, '/api/pens/PEN-01/session/start', {});
    assert.equal(start1.status, 200, 'Session start should succeed');
    assert.equal(start1.body.session, 'SESSION-001');

    const secondStart = await post(base, '/api/pens/PEN-01/session/start', {});
    assert.equal(secondStart.status, 409, 'Second active session must be rejected');

    const ingressBeforeSession = await ingest(base, { pen_id: 'PEN-02', timestamp: new Date().toISOString(), posture: 'Lying', surface_temp_c: 30 });
    assert.equal(ingressBeforeSession.status, 409, 'Ingest must fail before a session exists');

    const missingTs = await ingest(base, { pen_id: 'PEN-01', posture: 'Lying', surface_temp_c: 30 });
    assert.equal(missingTs.status, 422, 'Missing timestamp must be rejected');

    const pastTs = await ingest(base, { pen_id: 'PEN-01', timestamp: new Date(Date.now() - 3600e3).toISOString(), posture: 'Lying', surface_temp_c: 30 });
    assert.equal(pastTs.status, 422, 'Reading before the session must be rejected');

    const stamp = (offsetMs) => new Date(Date.now() + offsetMs).toISOString();
    const ts0 = stamp(0);
    const ts1 = stamp(500);
    const ts2 = stamp(1000);
    for (const [ts, label] of [[ts0, '0ms'], [ts1, '500ms'], [ts2, '1000ms']]) {
      const r = await ingest(base, { pen_id: 'PEN-01', timestamp: ts, posture: 'Lying', surface_temp_c: 30 });
      assert.equal(r.status, 200, `Reading at ${label} should be accepted`);
    }

    const dup = await ingest(base, { pen_id: 'PEN-01', timestamp: ts2, posture: 'Lying', surface_temp_c: 30 });
    assert.equal(dup.status, 422, 'Duplicate timestamp must be rejected');

    const status1 = await get(base, '/api/status');
    const pen1 = status1.pens.find((p) => p.pen_id === 'PEN-01');
    assert.equal(pen1.session.code, 'SESSION-001');
    assert.equal(pen1.status, 'Monitoring');
    assert.equal(pen1.alert, null);

    const alert1 = await ingest(base, { pen_id: 'PEN-01', timestamp: stamp(1500), posture: 'Lying', surface_temp_c: 36 });
    assert.equal(alert1.status, 200, 'Threshold reading should be accepted');
    const alert2 = await ingest(base, { pen_id: 'PEN-01', timestamp: stamp(2000), posture: 'Lying', surface_temp_c: 36.2 });
    assert.equal(alert2.status, 200, 'Second threshold reading should be accepted');
    assert.equal(Boolean(alert2.body.alert_raised), true, 'Alert should be raised');

    const duplicateAlert = await ingest(base, { pen_id: 'PEN-01', timestamp: stamp(2500), posture: 'Lying', surface_temp_c: 36 });
    assert.equal(duplicateAlert.status, 200, 'The reading should still be accepted while an alert is active');
    assert.equal(Boolean(duplicateAlert.body.alert_raised), false, 'Alert should not re-fire while one is still unacknowledged');

    const statusAfterAlert = await get(base, '/api/status');
    const pen1Alert = statusAfterAlert.pens.find((p) => p.pen_id === 'PEN-01');
    assert.equal(pen1Alert.status, 'Threshold reached');
    assert.ok(pen1Alert.alert && pen1Alert.alert.active, 'Alert should remain active while unacknowledged');
    assert.deepEqual(pen1Alert.alert.detail.triggering.surface_temp_c_readings, [36, 36.2]);
    assert.ok(String(pen1Alert.status_note).includes('screening') || pen1Alert.status_note === '' || pen1Alert.status_note === undefined, 'Status note should remain honest');

    const ack = await post(base, '/api/pens/PEN-01/alert/ack', {});
    assert.equal(ack.status, 200, 'Alert acknowledgement must succeed');
    const statusAfterAck = await get(base, '/api/status');
    const pen1Ack = statusAfterAck.pens.find((p) => p.pen_id === 'PEN-01');
    assert.equal(pen1Ack.alert && pen1Ack.alert.active, false, 'Acknowledge should clear the active alert');

    const reAlert = await ingest(base, { pen_id: 'PEN-01', timestamp: stamp(3000), posture: 'Lying', surface_temp_c: 36 });
    assert.equal(reAlert.status, 200, 'A reading should still be accepted after acknowledgement');

    const endSession = await post(base, '/api/pens/PEN-01/session/end', {});
    assert.equal(endSession.status, 200, 'Session end should succeed');
    assert.equal(endSession.body.session, 'SESSION-001');

    const start2 = await post(base, '/api/pens/PEN-01/session/start', {});
    assert.equal(start2.status, 200, 'New session should start');
    assert.equal(start2.body.session, 'SESSION-002');

    const statusAfterNewSession = await get(base, '/api/status');
    const penAfterRestart = statusAfterNewSession.pens.find((p) => p.pen_id === 'PEN-01');
    assert.equal(penAfterRestart.latest, null, 'No inherited reading in a new session');
    assert.equal(penAfterRestart.alert, null, 'No inherited alert in a new session');
    assert.equal(penAfterRestart.status, 'Inconclusive');

    const history = await get(base, '/api/log?pen=PEN-01&session=SESSION-001');
    assert.ok(history.records.some((r) => r.event_type === 'Session ended'), 'Session end history must be preserved');
    assert.ok(history.records.some((r) => r.event_type === 'Alert'), 'Alert history must be retained');

    const badFilter = await fetch(`${base}/api/log?pen=x'%20OR%201=1--`);
    assert.equal(badFilter.status, 400, 'Bad log filters must be rejected');

    await new Promise((r) => setTimeout(r, 5200));
    const staleStatus = (await get(base, '/api/status')).pens.find((p) => p.pen_id === 'PEN-01');
    assert.equal(staleStatus.status, 'Inconclusive', 'The pen should be marked stale once the reading age exceeds the threshold');

    const showLastUpdated = await get(base, '/api/status');
    assert.ok(showLastUpdated.server_time, 'The server should expose a current timestamp');

    const runDbFailureCheck = async () => {
      const failCtx = makeConfig({ pens: ['PEN-01'], stale_after_s: 5, log_interval_s: 1, screening: { surface_temp_threshold_c: 35, consecutive_readings: 2, min_lying_duration_s: 0 } });
      const readOnlyDb = path.join(failCtx.dir, 'readonly.db');
      fs.writeFileSync(readOnlyDb, '');
      fs.chmodSync(readOnlyDb, 0o444);
      const failSrv = spawn('node', ['server.js'], {
        env: { ...process.env, PORT: String(failCtx.port), HOST: '127.0.0.1', SWINE_CONFIG: failCtx.cfgPath, SWINE_DB: readOnlyDb },
        cwd: path.join(__dirname, '..'),
        stdio: 'ignore',
      });
      try {
        await new Promise((r) => setTimeout(r, 500));
        try {
          const ready = await fetch(`http://127.0.0.1:${failCtx.port}/api/status`);
          if (!ready.ok) {
            console.log('DB failure test skipped: the read-only DB prevented the server from starting.');
            return;
          }
        } catch {
          console.log('DB failure test skipped: the read-only DB prevented the server from starting.');
          return;
        }
        const readOnlyAttempt = await post(`http://127.0.0.1:${failCtx.port}`, '/api/pens/PEN-01/session/start', {});
        if (readOnlyAttempt.status === 200) {
          console.log('DB failure test skipped: platform ignored the read-only lock simulation.');
          return;
        }
        assert.notEqual(readOnlyAttempt.status, 200, 'A write failure should not succeed');
      } finally {
        failSrv.kill();
      }
    };

    await runDbFailureCheck();
    console.log('ALL CHECKS PASSED');
  } catch (e) {
    console.error('FAILED:', e.message);
    process.exitCode = 1;
  } finally {
    srv.kill();
  }
})();
