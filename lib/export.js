'use strict';
// Session export: CSV and a dependency-free PDF (text table, Helvetica, A4 landscape).
const HEAD = ['Time', 'Pen', 'Session', 'Event', 'Surface temp (°C)', 'Posture', 'Lying (s)', 'Status', 'Alert detail'];

function detailText(d) {
  if (!d) return '';
  try { return `Criteria >= ${d.criteria.surface_temp_threshold_c} °C x${d.criteria.consecutive_readings}; readings ${d.triggering.surface_temp_c_readings.join(', ')} °C`; } catch { return ''; }
}
function rowValues(r, code) {
  return [r.ts, r.pen_id, code, r.event_type, r.surface_temp_c == null ? '' : r.surface_temp_c.toFixed(1), r.posture || '',
    r.lying_s == null ? '' : String(Math.round(r.lying_s)), r.screening_status || '', detailText(r.detail)];
}
const csvCell = (v) => {
  let s = String(v);
  if (/^[=+\-@\t\r]/.test(s)) s = "'" + s; // stop spreadsheet formula injection
  return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
};
function buildCsv(code, rows) {
  const lines = [HEAD.map(csvCell).join(',')];
  for (const r of rows) lines.push(rowValues(r, code).map(csvCell).join(','));
  return '\uFEFF' + lines.join('\r\n') + '\r\n'; // BOM so Excel reads the ° sign correctly
}

const latin1 = (s) => Buffer.from(String(s).replace(/≥/g, '>=').replace(/[^\x20-\x7E\xA0-\xFF]/g, '?'), 'latin1');
const esc = (b) => latin1(b).toString('latin1').replace(/\\/g, '\\\\').replace(/\(/g, '\\(').replace(/\)/g, '\\)');
function buildPdf(sess, code, rows) {
  const W = 842, H = 595, M = 30, LH = 12;
  const X = [30, 125, 175, 240, 330, 385, 440, 485, 570];
  const MAXCH = [24, 8, 12, 15, 12, 10, 9, 18, 62];
  const perPage = Math.floor((H - 2 * M - 50) / LH);
  const pagesRows = []; for (let i = 0; i < rows.length; i += perPage) pagesRows.push(rows.slice(i, i + perPage));
  if (!pagesRows.length) pagesRows.push([]);
  const fmt = (t) => (t ? String(t).replace('T', ' ').slice(0, 19) + ' UTC' : 'active');
  const title = `${code} - ${sess.pen_id}`;
  const sub = `Started ${fmt(sess.started_at)}   Ended ${fmt(sess.ended_at)}   Lying ${Math.round(sess.lying_s)} s   Records ${rows.length}`;
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
    c += txt('F1', 7, M, H - M - 26, note + `   Page ${pi + 1} of ${pagesRows.length}`);
    let y = H - M - 44;
    HEAD.forEach((hd, i) => { c += txt('F2', 7.5, X[i], y, hd.replace('Surface temp', 'Temp')); });
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
