'use strict';
const $ = (s) => document.querySelector(s);
const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) { if (k === 'class') e.className = v; else if (k.startsWith('on')) e.addEventListener(k.slice(2), v); else if (v !== false && v != null) e.setAttribute(k, v === true ? '' : v); }
  for (const c of kids.flat()) if (c != null) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
};
const fmtTs = (iso) => iso ? new Date(iso).toLocaleString() : '—';
const fmtTime = (iso) => iso ? new Date(iso).toLocaleTimeString() : '—';
const fmtDur = (s) => { if (s == null) return '—'; s = Math.round(s); const hh = Math.floor(s / 3600), mm = Math.floor(s % 3600 / 60); return `${hh ? hh + ' h ' : ''}${mm} min ${s % 60} s`; };
const fmtTemp = (t) => (t == null ? 'Unavailable' : `${t.toFixed(1)} °C`);
const fmtHm = (s) => { if (s == null) return '—'; if (s < 60) return `${Math.round(s)} s`; const m = Math.round(s / 60), hh = Math.floor(m / 60); return hh ? `${hh} h ${m % 60} min` : `${m} min`; };
const fmtClock = (ms) => { ms = Math.max(0, Math.round(ms / 1000)); const hh = Math.floor(ms / 3600), mm = Math.floor(ms % 3600 / 60), ss = ms % 60; return (hh ? hh + ':' + String(mm).padStart(2, '0') : mm) + ':' + String(ss).padStart(2, '0'); };
let skew = 0; // server clock minus browser clock, so countdowns follow the server
const serverNow = () => Date.now() + skew;
const STATE_LABEL = { starting: 'Waiting for prototype', running: 'Running', stopping: 'Ending', evaluating: 'Finished (SMS pending)', finished: 'Finished',
  ended_early: 'Ended early', abandoned: 'Abandoned', refused: 'Refused by prototype', superseded: 'Replaced by a newer session', cancelled: 'Cancelled', ended_on_dashboard: 'Ended on dashboard' };
document.head.append(h('link', { rel: 'stylesheet', href: '/schedule.css' }));

// ---- Test mode (global; applies to sessions started from now on)
let testMode = false;
const tmBtn = h('button', { type: 'button', class: 'tm-btn', 'aria-pressed': 'false', title: 'Sessions started while this is on use the short test schedule set in prototype.json and are marked TEST. Running sessions are not affected.',
  onclick: async () => {
    try { const r = await api('/api/settings/test-mode', { method: 'POST', body: JSON.stringify({ enabled: !testMode }) }); setTestMode(r.test_mode); toast(r.test_mode ? 'Test mode on. New sessions are short test runs.' : 'Test mode off. New sessions use the full schedule.'); }
    catch (e) { toast(`Test mode not changed: ${e.message}`, true); }
  } }, 'Test mode: off');
const tmBanner = h('div', { class: 'test-banner', role: 'status', hidden: true }, 'Test mode is on. Sessions you start now use the short test schedule from prototype.json and are marked TEST in records and exports.');
$('#fs').before(tmBtn);
$('#pens').before(tmBanner);
function setTestMode(on) {
  if (on === testMode && tmBtn.dataset.ready) return;
  testMode = !!on; tmBtn.dataset.ready = '1';
  tmBtn.setAttribute('aria-pressed', String(testMode)); tmBtn.textContent = `Test mode: ${testMode ? 'on' : 'off'}`;
  tmBanner.hidden = !testMode; lastKey = ''; renderWall();
}

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { 'X-Requested-With': 'dashboard', ...(opts.body ? { 'Content-Type': 'application/json' } : {}) } });
  const body = await res.json().catch(() => ({ ok: false, error: 'Unreadable server response.' }));
  if (!res.ok) throw new Error(body.error || `Request failed (${res.status})`);
  return body;
}
let toastTimer;
function toast(msg, err = false) {
  const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (err ? ' err' : ''); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, err ? 8000 : 4000);
}

// ---- Camera preview (always on for every visible tile when preview.enabled is true in config.json)
// Displays frames the pipeline already pushed. Never opens a camera. Not used for any measurement.
const pv = { enabled: false, refresh_ms: 1000, stale_after_s: 5, pens: {} };
const tiles = new Map(); // pen -> persistent tile (so the 2 s status refresh does not reset the image)
function pvShow(u, text) { u.img.hidden = true; u.msg.hidden = false; u.msg.textContent = text; }
function pvRefresh(u) {
  if (document.hidden || $('#view-pens').hidden || !u.visible) return;
  if (u.cam && u.kind === 'rgb') { // camera URL entered when the pen was added: show it directly
    const snap = /\.jpe?g(\?|$)|snapshot/i.test(u.cam);
    if (snap || u.img.dataset.src !== u.cam) { u.img.dataset.src = u.cam; u.img.src = snap ? u.cam + (u.cam.includes('?') ? '&' : '?') + 't=' + Date.now() : u.cam; }
    return;
  }
  if (!pv.enabled) return pvShow(u, u.sched ? 'Capture images appear here when preview.enabled is true in config.json.' : 'Camera preview is turned off in config.json (preview.enabled).');
  const info = (pv.pens[u.pen] || {})[u.kind];
  if (!info) return pvShow(u, u.sched ? `No ${u.kind === 'thermal' ? 'thermal ' : ''}capture image yet. Images appear after each capture.`
    : u.kind === 'thermal' ? 'No thermal frame received from the pipeline.' : 'No recent frame (none received from the pipeline yet).');
  if (info.age_s > pv.stale_after_s && !u.sched) return pvShow(u, `No recent frame (last one ${Math.round(info.age_s)} s ago). Check that the pipeline is pushing frames.`);
  if (u.sched && u.img.dataset.age !== undefined && info.age_s >= Number(u.img.dataset.age)) { u.img.dataset.age = info.age_s; return; } // same capture: no reload
  u.img.dataset.age = info.age_s;
  u.img.src = `/api/preview/${encodeURIComponent(u.pen)}/${u.kind}.jpg?t=${Date.now()}`;
}
let pvTimer;
function pvSchedule() { clearInterval(pvTimer); pvTimer = setInterval(async () => { if (document.hidden || $('#view-pens').hidden || ![...tiles.values()].some((u) => u.visible)) return; // nothing displayed: no polling
    await pvStatus(); tiles.forEach(pvRefresh); }, pv.refresh_ms); }
async function pvStatus() {
  try {
    const s = await api('/api/preview/status');
    const changed = s.refresh_ms !== pv.refresh_ms;
    pv.enabled = s.enabled; pv.refresh_ms = s.refresh_ms; pv.stale_after_s = s.stale_after_s; pv.pens = s.pens;
    if (changed) pvSchedule();
    for (const u of tiles.values()) {
      const has = !!(s.pens[u.pen] && s.pens[u.pen].thermal);
      u.th.hidden = (!has && u.kind !== 'thermal') || !!u.cam; // thermal option only once pushed frames exist
    }
  } catch { /* keep last known preview state */ }
}

// ---- Prototype controls (reset flags / camera choice are delivered to the device through the MQTT bridge)
async function sendCmd(pen, body, msg) {
  try { await api(`/api/pens/${encodeURIComponent(pen)}/command`, { method: 'POST', body: JSON.stringify(body) }); toast(msg); }
  catch (e) { toast(`Not sent: ${e.message}`, true); }
}
const CAMS = ['auto', 'thermal', 'yolo', 'stop'], CAM_LABEL = { auto: 'Auto', thermal: 'Thermal', yolo: 'Webcam', stop: 'Off' };
function devChips(d, test) {
  if (!d) return h('div', { class: 'sub extra' }, 'Prototype: no data yet');
  const off = d.online === false || d.age_s > 30, f = d.flags || {}, s = d.sms || {};
  if (d.mode === 'scheduled') return h('div', { class: 'chips' },
    h('span', { class: 'chip ' + (off ? 'bad' : 'ok'), title: d.status || '' }, off ? 'Prototype offline' : 'Prototype online'),
    test ? h('span', { class: 'chip test' }, 'TEST') : null,
    s.state && s.state !== 'idle' ? h('span', { class: 'chip' + (['failed', 'unconfirmed', 'unknown'].includes(s.state) ? ' bad' : ''), title: s.detail || '' }, `SMS ${s.state.replace('_', ' ')}`) : null);
  return h('div', { class: 'chips' },
    h('span', { class: 'chip ' + (off ? 'bad' : 'ok'), title: d.status || '' }, off ? 'Prototype offline' : 'Prototype online'),
    h('span', { class: 'chip' + (f.high_temperature ? ' bad' : ''), title: `Latched when a surface temperature stays at or above ${d.threshold_c ?? '?'} °C` }, `High temp ${f.high_temperature ? 1 : 0}`),
    h('span', { class: 'chip' + (f.abnormal_behavior ? ' bad' : '') }, `Abnormal behavior ${f.abnormal_behavior ? 1 : 0}`),
    s.state ? h('span', { class: 'chip' + (s.state === 'failed' ? ' bad' : ''), title: s.detail || '' }, `SMS ${s.state}`) : null);
}
function devButtons(p, d, pen) {
  if (!d || d.mode === 'scheduled') return []; // scheduled prototypes: no camera selection or flag reset
  const cur = d.mode === 'auto' ? 'auto' : (d.camera || 'stop');
  return [
    h('button', { type: 'button', disabled: !connected, title: 'Choose which camera the prototype reads',
      onclick: () => { const nx = CAMS[(CAMS.indexOf(cur) + 1) % CAMS.length]; sendCmd(pen, { action: 'camera', kind: nx }, `Camera: ${CAM_LABEL[nx]} (sent to prototype).`); } }, `Cam: ${CAM_LABEL[cur]}`),
    h('button', { type: 'button', disabled: !connected, title: 'Clear the latched flags and start a new alert cycle',
      onclick: () => { if (confirm(`Reset the latched flags on ${pen}? This starts a new alert cycle.`)) sendCmd(pen, { action: 'reset' }, 'Reset sent to prototype.'); } }, 'Reset flags')];
}

// ---- Scheduled session content of a tile
function pigTable(pigs) {
  if (!pigs.length) return h('div', { class: 'sub' }, 'No pigs detected in the last capture.');
  const rows = [...pigs].sort((a, b) => (b.observed - a.observed) || a.pig_id - b.pig_id);
  const shown = rows.slice(0, 4);
  return h('table', { class: 'pigs' },
    h('tr', {}, ...['Pig', 'Posture', 'Lying', 'Temp', 'Behavior', 'Thermal'].map((x) => h('th', { scope: 'col' }, x))),
    ...shown.map((r) => h('tr', { class: r.behavioral_status === 'Abnormal' || r.thermal_status === 'Elevated' ? 'warn' : (!r.observed ? 'dim' : '') },
      h('td', {}, String(r.pig_id)), h('td', {}, r.observed ? r.posture : 'Not seen'), h('td', {}, r.observed ? fmtHm(r.lying_s) : '—'),
      h('td', {}, r.surface_temp_c == null ? '—' : `${r.surface_temp_c.toFixed(1)} °C`), h('td', {}, r.behavioral_status), h('td', {}, r.thermal_status))),
    rows.length > 4 ? h('tr', {}, h('td', { colspan: '6' }, `${rows.length - 4} more in Records`)) : null);
}
function ruleText(rule) {
  return rule && rule.threshold_c != null ? `Alert rule: final temperature ≥ ${rule.threshold_c} °C and lying in ${rule.consecutive_captures}+ captures in a row` : '';
}
function schedContent(p) {
  const v = p.schedule, now = serverNow();
  const next = v.next_capture_at ? Date.parse(v.next_capture_at) : null;
  const head = v.waiting ? 'Waiting for the prototype…' : v.stopping ? 'Ending the session…' : v.capturing ? 'Capturing now…'
    : next ? (next > now ? `Next capture in ${fmtClock(next - now)}` : 'Capture due') : 'Running';
  const done = Math.min(v.next_seq || 0, v.captures || 0);
  const left = v.ends_at ? Math.max(0, Date.parse(v.ends_at) - now) : null;
  const L = v.last;
  return [
    h('div', { class: 'temp' }, head),
    v.waiting ? null : h('div', { class: 'sub' }, `${v.captures ? `${done} of ${v.captures} captures` : ''}${left != null ? ` · ${fmtHm(left / 1000)} left` : ''}`,
      h('span', { class: 'extra' }, v.interval_s ? ` · every ${fmtHm(v.interval_s)}` : '')),
    L ? h('div', { class: 'sub' + (L.status === 'Capture failed' ? ' stale' : '') }, `Last capture ${fmtTime(L.ts)}` + (L.status === 'Capture failed' ? ` failed: ${L.error || 'no data'}` : '')) : (v.waiting ? null : h('div', { class: 'sub' }, 'No capture yet.')),
    L && L.status !== 'Capture failed' ? pigTable(L.pigs) : null,
    v.rule ? h('div', { class: 'sub extra' }, ruleText(v.rule)) : null];
}
function lastResultContent(r) {
  const state = STATE_LABEL[r.state] || r.state;
  const flagged = r.flagged && r.flagged.length ? `${r.flagged.length} of ${r.pigs} pig${r.pigs === 1 ? '' : 's'} flagged (Pig ${r.flagged.join(', ')})` : (r.pigs != null ? `No pig flagged (${r.pigs} tracked)` : 'Not evaluated');
  return [h('div', { class: 'sub' }, `Last session ${r.session}${r.test ? ' (test)' : ''}: ${state}. ${flagged}.`),
    r.sms && r.sms.state ? h('div', { class: 'sub extra', title: r.sms.detail || '' }, `SMS: ${r.sms.detail || r.sms.state}`) : null];
}

// ---- Tile (one CCTV cell per pen)
function getTile(pen) {
  let u = tiles.get(pen); if (u) return u;
  u = { pen, kind: 'rgb', visible: false };
  u.img = h('img', { alt: `${pen} camera preview`, hidden: true });
  u.msg = h('p', { class: 'pv-msg' }, 'Waiting for a frame…');
  u.img.addEventListener('load', () => { u.img.hidden = false; u.msg.hidden = true; });
  u.img.addEventListener('error', () => pvShow(u, u.cam && u.kind === 'rgb' ? 'Camera not reachable at the address you entered.' : 'No recent frame available.'));
  u.th = h('button', { type: 'button', 'aria-pressed': 'false', hidden: true, title: 'Switch between RGB and thermal view',
    onclick: () => { u.kind = u.kind === 'thermal' ? 'rgb' : 'thermal'; u.th.setAttribute('aria-pressed', String(u.kind === 'thermal')); u.img.hidden = true; delete u.img.dataset.age; pvShow(u, 'Waiting for a frame…'); pvRefresh(u); } }, 'Thermal');
  u.top = h('div', { class: 'ov ov-top' });
  u.rm = h('button', { type: 'button', class: 'rm', title: 'Remove from this dashboard (recorded data is kept)', 'aria-label': `Remove ${pen} from dashboard`,
    onclick: () => { if (confirm(`Remove ${pen} from this dashboard? Its sessions and log are kept.`)) removePen(pen); } }, '✕');
  u.alertEl = h('div', { hidden: true });
  u.bot = h('div', { class: 'ov ov-bot' });
  u.root = h('article', { class: 'tile' }, h('div', { class: 'pv-frame' }, u.img, u.msg), u.top, u.alertEl, u.bot);
  tiles.set(pen, u); return u;
}
function badge(status, note) {
  const map = { 'Monitoring': ['b-monitoring', '● '], 'Inconclusive': ['b-inconclusive', '? '], 'Threshold reached': ['b-threshold', '▲ '], 'No active session': ['b-none', '○ '] };
  const [cls, glyph] = map[status] || ['b-none', ''];
  return h('span', { class: 'badge ' + cls, title: note || null }, glyph + status);
}
function updateTile(p, cfg) {
  const u = getTile(p.pen_id);
  if (u.cam !== (cfg.camera || '')) { u.cam = cfg.camera || ''; u.img.removeAttribute('data-src'); }
  const active = !!p.session, L = p.latest, alerting = p.alert && p.alert.active, sched = !!p.schedule;
  u.sched = !!(p.device && p.device.mode === 'scheduled') || sched;
  u.root.className = 'tile' + (active ? ' active' : '') + (active && p.stale ? ' stale' : '') + (alerting ? ' alert' : '') + (!connected ? ' dim' : '');
  u.top.replaceChildren(
    h('div', {}, h('span', { class: 'pid' }, h('span', { class: 'rec', 'aria-hidden': 'true' }), p.pen_id),
      h('span', { class: 'sess' }, (active ? `${p.session.code} · started ${fmtTs(p.session.started_at)}` : (p.last_ended_session ? `Last: ${p.last_ended_session} (ended)` : 'No sessions yet')) + (cfg.address ? ` · ${cfg.address}` : ''))),
    h('div', {}, badge(p.status, p.status_note), u.rm));
  if (p.alert && p.alert.kind === 'scheduled') {
    const a = p.alert, sms = a.sms || {};
    u.alertEl.className = 'alertbox'; u.alertEl.hidden = false; u.alertEl.setAttribute('role', 'alert');
    u.alertEl.replaceChildren(
      h('div', {}, h('strong', {}, `End-of-session alert${a.test ? ' (test)' : ''}`),
        h('span', { class: 'detail' }, ` · ${a.session} · ${a.pigs.map((x) => `Pig ${x.pig_id}: ${fmtTemp(x.final_temp_c)}, lying ${fmtHm(x.longest_lying_s)}`).join('; ')}` +
          `${sms.state ? ` · SMS ${sms.state.replace('_', ' ')}` : ''} · screening only, not a diagnosis`)),
      h('button', { type: 'button', onclick: () => act(p.pen_id, 'alert/ack', 'Alert acknowledged.') }, 'Acknowledge'));
  } else if (p.alert) {
    const d = p.alert.detail, t = d.triggering;
    u.alertEl.className = 'alertbox' + (p.alert.active ? '' : ' old'); u.alertEl.hidden = false; u.alertEl.setAttribute('role', 'alert');
    u.alertEl.replaceChildren(
      h('div', {}, h('strong', {}, p.alert.active ? 'Screening alert (unacknowledged)' : 'Earlier alert (acknowledged)'),
        h('span', { class: 'detail' }, ` · ${fmtTime(p.alert.ts)} · ${t.surface_temp_c_readings.map(fmtTemp).join(', ')} · lying ${fmtDur(t.lying_s)} · screening only, not a diagnosis`)),
      p.alert.active ? h('button', { type: 'button', onclick: () => act(p.pen_id, 'alert/ack', 'Alert acknowledged.') }, 'Acknowledge') : null);
  } else { u.alertEl.hidden = true; u.alertEl.replaceChildren(); }
  const fresh = !active || sched ? null : !L ? h('div', { class: 'sub stale' }, 'No reading received yet.')
    : h('div', { class: 'sub' + (p.stale ? ' stale' : '') }, p.stale ? `STALE: last reading ${fmtTime(L.ts)} (${p.age_s} s ago)` : `Reading ${fmtTime(L.ts)} (${p.age_s} s ago)`);
  u.bot.replaceChildren(
    h('div', {},
      ...(sched ? schedContent(p) : []),
      active && !sched ? h('div', { class: 'temp' }, L ? fmtTemp(L.surface_temp_c) : 'No reading') : null,
      active && !sched ? h('div', { class: 'sub' }, `Posture: ${L ? L.posture : 'Unknown'}`, h('span', { class: 'extra' }, ` · Lying ${L ? fmtDur(L.lying_s) : '—'}`)) : null,
      fresh && h('div', { class: 'extra sub' + (p.stale ? ' stale' : '') }, fresh.textContent),
      !active && p.last_result ? lastResultContent(p.last_result) : null,
      devChips(p.device, sched && p.schedule.test)),
    h('div', { class: 'acts' }, ...devButtons(p, p.device, p.pen_id), u.th,
      h('button', { type: 'button', class: 'primary', disabled: active || !connected, onclick: () => act(p.pen_id, 'session/start', null) },
        active ? 'Active' : (testMode && cfg.address ? 'Start test' : 'Start')),
      h('button', { type: 'button', disabled: !active || !connected, onclick: () => {
        const msg = sched ? (p.schedule.waiting ? `Cancel the start of ${p.session.code} on ${p.pen_id}?`
          : p.schedule.stopping ? `The prototype has not confirmed yet. Close ${p.session.code} on the dashboard only? A result it sends later is still recorded.`
          : `End ${p.session.code} on ${p.pen_id} early? The prototype evaluates what it has captured and sends no SMS. History is kept.`)
          : `End ${p.session.code} on ${p.pen_id}? History is kept.`;
        if (confirm(msg)) act(p.pen_id, 'session/end', null); } }, 'End')));
  return u;
}

// ---- Dynamic wall layout + add-pen containers
let added = []; // pens shown on the dashboard; comes from the server registry
let pensData = [], page = 0, perPage = 6, lastKey = '';
function gridFor() {
  const w = window.innerWidth, hgt = window.innerHeight;
  if (w < 600) return { cols: 1, rows: 2 };                       // phone portrait: 2 pens
  if (hgt < 500) return { cols: 2, rows: 1 };                     // phone landscape
  if (w < 1100) return { cols: 2, rows: hgt > w ? 3 : 2 };        // tablet
  if (w >= 2400) return { cols: 4, rows: 2 };                     // ultra-wide / 4K
  return { cols: 3, rows: 2 };                                    // 1080p desktop: 6 pens
}
function emptyCell() {
  return h('div', { class: 'tile empty' }, h('button', { type: 'button', class: 'add-btn', 'aria-label': 'Add a pen', onclick: openAdd }, '+', h('small', {}, 'Add pen'), h('small', { class: 'found-note' }, foundText())));
}
function renderWall() {
  const { cols, rows } = gridFor();
  perPage = cols * rows;
  const pages = Math.max(1, Math.ceil((added.length + 1) / perPage)); // always room for one more "+"
  page = Math.min(page, pages - 1);
  const wall = $('#pens');
  wall.style.setProperty('--cols', cols); wall.style.setProperty('--rows', rows);
  const byId = new Map(pensData.map((p) => [p.pen_id, p]));
  const slice = added.slice(page * perPage, (page + 1) * perPage);
  const ids = new Set(slice.map((c) => c.pen));
  tiles.forEach((u, pen) => { u.visible = ids.has(pen); });
  const els = slice.map((c) => updateTile(byId.get(c.pen) || { pen_id: c.pen, status: 'No active session', status_note: 'Not found on the server.', session: null, latest: null, alert: null, stale: false }, c).root);
  const key = `${cols}x${rows}|${page}|` + slice.map((c) => c.pen).join('|');
  if (key !== lastKey) { while (els.length < perPage) els.push(emptyCell()); wall.replaceChildren(...els); lastKey = key; }
  slice.forEach((c) => pvRefresh(tiles.get(c.pen)));
  $('#pager').hidden = pages < 2 || document.body.dataset.view !== 'pens';
  $('#pg-label').textContent = `${page + 1} / ${pages}`;
}
async function removePen(pen) {
  try { await api(`/api/pens/${encodeURIComponent(pen)}`, { method: 'DELETE' }); tiles.delete(pen); lastKey = ''; toast(`${pen} removed from the dashboard.`); }
  catch (e) { toast(`Not removed: ${e.message}`, true); }
  refresh();
}
// ---- Prototypes that connected to the built-in broker but are not a pen yet
let found = [], suggestedPen = 'PEN-01', pickedDevice = '', foundTimer;
const foundText = () => (found.length ? `${found.length} new prototype${found.length > 1 ? 's' : ''} found` : '');
async function pollFound() {
  try { const r = await api('/api/discovered'); found = r.devices; suggestedPen = r.suggested_pen_id; } catch { return; } // old server: feature simply absent
  document.querySelectorAll('.found-note').forEach((el) => { el.textContent = foundText(); });
  paintFound();
}
function paintFound() {
  const box = $('#a-found'); if (!$('#add-dlg').open) return;
  if (pickedDevice && !found.some((d) => d.device_id === pickedDevice)) pickedDevice = '';
  box.replaceChildren(...(found.length ? found.map((d) => h('button', { type: 'button', 'aria-pressed': String(d.device_id === pickedDevice), onclick: () => pickDevice(d) },
    h('span', {}, d.device_id), h('span', { class: 'muted' }, `${d.ip || 'unknown IP'} · seen ${d.age_s} s ago`)))
    : [h('p', { class: 'none' }, 'None yet. Power on the Pi and wait about 20 seconds, or type its address below.')]));
}
function pickDevice(d) {
  pickedDevice = d.device_id; $('#a-addr').value = d.address;
  if (!$('#a-pen').value.trim()) $('#a-pen').value = suggestedPen;
  paintFound(); $('#a-pen').focus(); $('#a-pen').select();
}
function openAdd() {
  $('#add-form').reset(); pickedDevice = ''; $('#add-dlg').showModal(); $('#a-pen').focus();
  paintFound(); pollFound(); clearInterval(foundTimer); foundTimer = setInterval(pollFound, 1500);
}
$('#add-dlg').addEventListener('close', () => clearInterval(foundTimer));
$('#a-cancel').addEventListener('click', () => $('#add-dlg').close());
$('#add-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = JSON.stringify({ pen_id: $('#a-pen').value.trim(), address: $('#a-addr').value.trim(), camera: $('#a-cam').value.trim() });
  try {
    const r = await api('/api/pens', { method: 'POST', body });
    $('#add-dlg').close(); toast(`${r.pen_id} added.`);
    await refresh(); page = Math.floor((added.length - 1) / perPage); lastKey = ''; renderWall();
  } catch (err) { toast(`Not added: ${err.message}`, true); }
});
addEventListener('resize', () => renderWall());
$('#pg-prev').addEventListener('click', () => { const n = Math.ceil((added.length + 1) / perPage); page = (page - 1 + n) % n; renderWall(); });
$('#pg-next').addEventListener('click', () => { const n = Math.ceil((added.length + 1) / perPage); page = (page + 1) % n; renderWall(); });
$('#fs').addEventListener('click', () => { if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(() => toast('Full screen is not available in this browser.', true)); });

// ---- Pens view data
let connected = false;
async function act(pen, action, okMsg) {
  try {
    const r = await api(`/api/pens/${encodeURIComponent(pen)}/${action}`, { method: 'POST', body: '{}' });
    toast(okMsg || (action === 'session/start' ? (r.scheduled ? `${r.session}${r.test ? ' (test)' : ''} sent to the prototype on ${pen}. Waiting for it to confirm.` : `${r.session} started on ${pen}.`)
      : r.pending ? `Asked the prototype to end ${r.session}.` : `${r.session} ended on ${pen}. Its records are kept.`));
  } catch (e) { toast(`Not saved: ${e.message}`, true); }
  refresh();
}
async function refresh() {
  try {
    const s = await api('/api/status');
    connected = true; $('#conn').className = 'conn'; $('#conn').textContent = `Connected · server time ${fmtTs(s.server_time)}`;
    skew = Date.parse(s.server_time) - Date.now();
    setTestMode(!!s.test_mode);
    pensData = s.pens; added = s.registry.map((r) => ({ pen: r.pen_id, address: r.address, camera: r.camera }));
    if (added.length && !document.hidden) await pvStatus(); // preview status only when a pen is displayed
    pollFound();
    renderWall();
    const c = s.criteria;
    const proto = added.length ? 'Pens with a prototype are evaluated pig by pig by the prototype when the session ends (rule shown on each pen). Screening only, not a diagnosis. ' : '';
    $('#criteria').textContent = proto + (proto && !c.configured ? '' : c.configured ? `Screening criteria: surface temp ≥ ${c.surface_temp_threshold_c} °C for ${c.consecutive_readings} consecutive readings${c.min_lying_duration_s ? `, lying ≥ ${c.min_lying_duration_s} s` : ''}. Readings older than ${s.stale_after_s} s are marked stale.`
      : `No screening threshold is configured, so no alerts can fire. Readings older than ${s.stale_after_s} s are marked stale.`);
  } catch (e) {
    connected = false; $('#conn').className = 'conn bad'; $('#conn').textContent = 'Cannot reach the server. Values shown are NOT current.';
    document.querySelectorAll('.tile').forEach((c) => c.classList.add('dim'));
    document.querySelectorAll('.tile .acts button:not([aria-pressed])').forEach((b) => { b.disabled = true; });
  }
}

// ---- Sessions & log views
async function loadSessions() {
  const { sessions } = await api('/api/sessions');
  const sel = $('#f-pen'), keep = sel.value; // pen filter lists only pens that exist in the database
  const pens = [...new Set([...added.map((c) => c.pen), ...sessions.map((s) => s.pen_id)])].sort();
  sel.replaceChildren(h('option', { value: '' }, 'All'), ...pens.map((x) => h('option', {}, x)));
  sel.value = pens.includes(keep) ? keep : '';
  const sessResult = (s) => s.kind !== 'scheduled' ? `Lying ${fmtDur(s.lying_s)}`
    : s.result ? (s.result.flagged.length ? `Flagged: pig ${s.result.flagged.join(', ')}` : `No alert (${s.result.pigs} pigs)`) : (STATE_LABEL[s.state] || s.state || '—');
  $('#sessions-table').replaceChildren(h('tr', {}, ...['Session', 'Pen', 'Type', 'Started', 'Ended', 'Result', 'Logged', 'Alerts', ''].map((x) => h('th', {}, x))),
    ...sessions.map((s) => h('tr', {}, h('td', {}, s.session), h('td', {}, s.pen_id), h('td', {}, s.kind === 'scheduled' ? (s.test ? 'Test' : 'Scheduled') : 'Live'),
      h('td', {}, fmtTs(s.started_at)), h('td', {}, s.ended_at ? fmtTs(s.ended_at) : (STATE_LABEL[s.state] || 'Active')), h('td', {}, sessResult(s)),
      h('td', {}, s.kind === 'scheduled' ? `${s.captures} captures` : `${s.measurements} readings`), h('td', {}, s.alerts),
      h('td', { class: 'acts-cell' }, h('a', { class: 'btn', href: `/api/sessions/${encodeURIComponent(s.session)}/export.csv`, download: '', title: 'Download this session as CSV' }, 'CSV'), h('a', { class: 'btn', href: `/api/sessions/${encodeURIComponent(s.session)}/export.pdf`, download: '', title: 'Download this session as PDF' }, 'PDF'), h('button', { type: 'button', class: 'danger', disabled: !s.ended_at, title: s.ended_at ? 'Delete this session and its log records' : 'End the session before deleting it', onclick: () => delSession(s.session) }, 'Delete')))));
}
async function delSession(code) {
  if (!confirm(`Delete ${code} and ALL of its log records? This cannot be undone.`)) return;
  try { await api(`/api/sessions/${encodeURIComponent(code)}`, { method: 'DELETE' }); toast(`${code} and its log records were deleted.`); }
  catch (e) { toast(`Not deleted: ${e.message}`, true); }
  loadSessions().catch((e) => toast(e.message, true)); loadLog(); refresh();
}
function detailText(d) {
  if (!d) return '';
  try {
    if (d.kind === 'sample') return [d.error, d.thermal_error && `thermal: ${d.thermal_error}`, d.observed === false && 'not detected in this capture',
      d.capture_ok && !d.calibrated && 'uncalibrated thermal mapping'].filter(Boolean).join('; ');
    if (d.kind === 'pig_alert') return `Pig ${d.pig_id}: final ${fmtTemp(d.final_temp_c)}, longest lying ${fmtHm(d.longest_lying_s)}`;
    if (d.kind === 'evaluation') { const r = d.result; return (r ? `${r.pigs.length} pig(s), flagged: ${r.flagged.length ? r.flagged.join(', ') : 'none'}` : 'Not evaluated') + (d.sms ? `; SMS ${d.sms.state}` : '') + (d.note ? `; ${d.note}` : ''); }
    if (d.kind === 'scheduled' || d.kind === 'note') return [d.test && 'Test session', d.note].filter(Boolean).join('; ');
    return `Criteria ≥ ${d.criteria.surface_temp_threshold_c} °C ×${d.criteria.consecutive_readings}; readings ${d.triggering.surface_temp_c_readings.join(', ')} °C`;
  } catch { return ''; }
}
let oldest = null;
async function loadLog(append = false) {
  const p = new URLSearchParams({ limit: '100' });
  if ($('#f-pen').value) p.set('pen', $('#f-pen').value);
  if ($('#f-session').value.trim()) p.set('session', $('#f-session').value.trim().toUpperCase());
  if ($('#f-type').value) p.set('type', $('#f-type').value);
  if (append && oldest) p.set('before', oldest);
  try {
    const { records } = await api('/api/log?' + p);
    const rows = records.map((r) => h('tr', { class: r.event_type === 'Alert' ? 'ev-alert' : '' },
      h('td', {}, fmtTs(r.ts)), h('td', {}, r.pen_id), h('td', {}, r.session), h('td', {}, r.seq == null ? '—' : String(r.seq + 1)),
      h('td', {}, r.pig_id == null ? '—' : String(r.pig_id)), h('td', {}, r.event_type),
      h('td', {}, r.surface_temp_c == null ? '—' : fmtTemp(r.surface_temp_c)), h('td', {}, r.posture || '—'),
      h('td', {}, r.lying_s == null ? '—' : fmtDur(r.lying_s)), h('td', {}, r.behavioral_status || '—'), h('td', {}, r.thermal_status || '—'),
      h('td', {}, r.screening_status || '—'), h('td', { class: 'wrap' }, detailText(r.detail))));
    const t = $('#log-table');
    if (!append) t.replaceChildren(h('tr', {}, ...['Time', 'Pen', 'Session', 'Capture', 'Pig', 'Event', 'Surface temp', 'Posture', 'Lying', 'Behavior', 'Thermal', 'Status', 'Detail'].map((x) => h('th', {}, x))));
    t.append(...rows);
    if (records.length) oldest = records[records.length - 1].id;
    $('#more').hidden = records.length < 100;
  } catch (e) { toast(`Could not load log: ${e.message}`, true); }
}

document.querySelectorAll('.tab').forEach((b) => b.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach((x) => x.removeAttribute('aria-current')); b.setAttribute('aria-current', 'page');
  document.body.dataset.view = b.dataset.view;
  for (const v of ['pens', 'records']) $('#view-' + v).hidden = v !== b.dataset.view;
  if (b.dataset.view === 'pens') renderWall(); else { $('#pager').hidden = true; loadSessions().catch((e) => toast(e.message, true)); loadLog(); }
}));
$('#log-filters').addEventListener('submit', (e) => { e.preventDefault(); loadLog(); });
$('#more').addEventListener('click', () => loadLog(true));
document.addEventListener('keydown', (e) => { if ($('#view-pens').hidden || /INPUT|SELECT|TEXTAREA/.test(e.target.tagName)) return; if (e.key === 'ArrowRight') $('#pg-next').click(); if (e.key === 'ArrowLeft') $('#pg-prev').click(); });
pvSchedule();
renderWall();
let tick = 0;
refresh();
setInterval(() => { // 2 s while pens are shown, 10 s when none are; paused in a hidden tab
  if (document.hidden) return;
  tick += 1;
  if (added.length || tick % 5 === 0) refresh();
}, 2000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
// // UI refresh only; never used as a clock for monitoring calculations
