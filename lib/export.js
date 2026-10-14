'use strict';
// Session export: CSV and a dependency-free PDF (text table, Helvetica, A4 landscape).
// Scheduled sessions add: capture number, pig ID, behavioral and thermal status (methodology record fields).
// Live (legacy) sessions leave those columns empty.
const HEAD = ['Time', 'Pen', 'Session', 'Capture', 'Pig ID', 'Event', 'Posture', 'Lying (s)', 'Surface temp (°C)',
  'Behavioral status', 'Thermal status', 'Status', 'Detail'];
const PDF_HEAD = ['Time', 'Pen', 'Session', 'Capture', 'Pig', 'Event', 'Posture', 'Lying (s)', 'Temp', 'Behavior', 'Thermal', 'Status', 'Detail'];

const t1 = (v) => (v == null ? '--' : Number(v).toFixed(1));
function detailText(d) {
  if (!d) return '';
  try {
    if (d.kind === 'sample') return [d.error, d.thermal_error ? `thermal: ${d.thermal_error}` : '', d.observed === false ? 'not detected in this capture' : '',
      d.capture_ok && !d.calibrated ? 'uncalibrated thermal mapping' : ''].filter(Boolean).join('; ');
    if (d.kind === 'pig_alert') return `Pig ${d.pig_id}: final ${t1(d.final_temp_c)} °C, longest lying ${Math.round(d.longest_lying_s / 60)} min` +
      (d.rule ? ` (rule >= ${d.rule.threshold_c} °C and >= ${d.rule.consecutive_captures} lying captures)` : '');
    if (d.kind === 'evaluation') {
      const r = d.result;
      const res = r ? `${r.pigs.length} pig(s), flagged: ${r.flagged.length ? r.flagged.join(', ') : 'none'}` : 'no evaluation';
      return `${res}; SMS ${d.sms ? d.sms.state : 'n/a'}${d.note ? '; ' + d.note : ''}`;
    }
    if (d.kind === 'scheduled' || d.kind === 'note') return [d.test ? 'TEST session' : '', d.interval_s ? `every ${d.interval_s} s for ${d.duration_s} s` : '', d.note || ''].filter(Boolean).join('; ');
    return `Criteria >= ${d.criteria.surface_temp_threshold_c} °C x${d.criteria.consecutive_readings}; readings ${d.triggering.surface_temp_c_readings.join(', ')} °C`;
  } catch { return ''; }
}
function rowValues(r, code) {
  return [r.ts, r.pen_id, code, r.seq == null ? '' : String(r.seq + 1), r.pig_id == null ? '' : String(r.pig_id), r.event_type, r.posture || '',
    r.lying_s == null ? '' : String(Math.round(r.lying_s)), r.surface_temp_c == null ? '' : r.surface_temp_c.toFixed(1),
    r.behavioral_status || '', r.thermal_status || '', r.screening_status || '', detailText(r.detail)];
}
// One summary line per session (also printed at the top of the PDF).
function summary(sess) {
  if (sess.kind !== 'scheduled') return '';
  const r = sess.result;
  const parts = [sess.test ? 'TEST session' : 'Scheduled session', sess.interval_s ? `capture every ${Math.round(sess.interval_s / 60 * 10) / 10} min for ${Math.round(sess.duration_s / 360) / 10} h` : '',
    `state ${sess.state || 'unknown'}`, r ? `flagged pigs: ${r.flagged.length ? r.flagged.join(', ') : 'none'}` : 'not evaluated yet',
    r && r.rule ? `rule: final temp >= ${r.rule.threshold_c} °C and lying >= ${r.rule.consecutive_captures} captures in a row` : '',
    sess.sms ? `SMS ${sess.sms.state}` : ''];
  return parts.filter(Boolean).join('; ');
}
const csvCell = (v) => {
  let s = String(v);
  if (/^[=+\-@\t\r]/.test(s)) s = "'" + s; // stop spreadsheet formula injection
  return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
};
function buildCsv(code, rows, sess = {}) {
  const lines = [];
  const sum = summary(sess);
  if (sum) lines.push(csvCell(`# ${code}: ${sum}. Screening data only, not a veterinary diagnosis.`));
  lines.push(HEAD.map(csvCell).join(','));
  for (const r of rows) lines.push(rowValues(r, code).map(csvCell).join(','));
  return '\uFEFF' + lines.join('\r\n') + '\r\n'; // BOM so Excel reads the ° sign correctly
}

const latin1 = (s) => Buffer.from(String(s).replace(/≥/g, '>=').replace(/[^\x20-\x7E\xA0-\xFF]/g, '?'), 'latin1');
const esc = (b) => latin1(b).toString('latin1').replace(/\\/g, '\\\\').replace(/\(/g, '\\(').replace(/\)/g, '\\)');
function buildPdf(sess, code, rows) {
  const W = 842, H = 595, M = 30, LH = 12;
  const X = [30, 125, 165, 220, 250, 275, 337, 375, 417, 447, 489, 531, 593];
  const MAXCH = [24, 8, 12, 4, 4, 15, 9, 9, 6, 9, 9, 15, 52];
  const perPage = Math.floor((H - 2 * M - 60) / LH);
  const pagesRows = []; for (let i = 0; i < rows.length; i += perPage) pagesRows.push(rows.slice(i, i + perPage));
  if (!pagesRows.length) pagesRows.push([]);
  const fmt = (t) => (t ? String(t).replace('T', ' ').slice(0, 19) + ' UTC' : 'active');
  const title = `${code} - ${sess.pen_id}${sess.test ? ' (TEST)' : ''}`;
  const sub = `Started ${fmt(sess.started_at)}   Ended ${fmt(sess.ended_at)}   ` +
    (sess.kind === 'scheduled' ? '' : `Lying ${Math.round(sess.lying_s)} s   `) + `Records ${rows.length}`;
  const sum = summary(sess);
  const note = 'Screening data only. Not a veterinary diagnosis.';
  const txt = (f, size, x, y, s) => `BT /${f} ${size} Tf ${x} ${y} Td (${esc(s)}) Tj ET\n`;
  const chunks = [];
  const objs = [];
  const add = (b) => { objs.push(Buffer.isBuffer(b) ? b : Buffer.from(b, 'latin1')); return objs.length; };
  add('<< /Type /Catalog /Pages 2 0 R >>');
  add(''); // pages, filled below
  add('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>');
  add('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>');
  const kids = [];
  pagesRows.forEach((pr, pi) => {
    let c = '';
    c += txt('F2', 13, M, H - M, title);
    c += txt('F1', 8, M, H - M - 14, sub);
    if (sum) c += txt('F1', 7.5, M, H - M - 25, sum.length > 190 ? sum.slice(0, 187) + '...' : sum);
    c += txt('F1', 7, M, H - M - 36, note + `   Page ${pi + 1} of ${pagesRows.length}`);
    let y = H - M - 54;
    PDF_HEAD.forEach((hd, i) => { c += txt('F2', 7.5, X[i], y, hd); });
    y -= 3; c += `0.5 w ${M} ${y} m ${W - M} ${y} l S\n`; y -= LH - 3;
    for (const r of pr) {
      rowValues(r, code).forEach((v, i) => { const s = String(v); c += txt('F1', 7.5, X[i], y, s.length > MAXCH[i] ? s.slice(0, MAXCH[i] - 1) + '...' : s); });
      y -= LH;
    }
    const content = Buffer.from(c, 'latin1');
    const cid = objs.length + 2;
    const pid = add(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${W} ${H}] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents ${cid} 0 R >>`);
    add(Buffer.concat([Buffer.from(`<< /Length ${content.length} >>\nstream\n`, 'latin1'), content, Buffer.from('\nendstream', 'latin1')]));
    kids.push(`${pid} 0 R`);
  });
  objs[1] = Buffer.from(`<< /Type /Pages /Kids [${kids.join(' ')}] /Count ${kids.length} >>`, 'latin1');
  const parts = [Buffer.from('%PDF-1.4\n', 'latin1')]; const offsets = []; let pos = parts[0].length;
  objs.forEach((o, i) => { offsets.push(pos); const b = Buffer.concat([Buffer.from(`${i + 1} 0 obj\n`, 'latin1'), o, Buffer.from('\nendobj\n', 'latin1')]); parts.push(b); pos += b.length; });
  let xref = `xref\n0 ${objs.length + 1}\n0000000000 65535 f \n`;
  offsets.forEach((o) => { xref += String(o).padStart(10, '0') + ' 00000 n \n'; });
  parts.push(Buffer.from(xref + `trailer\n<< /Size ${objs.length + 1} /Root 1 0 R >>\nstartxref\n${pos}\n%%EOF\n`, 'latin1'));
  return Buffer.concat(parts);
}
module.exports = { buildCsv, buildPdf };
