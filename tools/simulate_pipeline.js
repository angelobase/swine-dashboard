'use strict';
// SYNTHETIC TEST DATA ONLY. Stands in for the real monitoring pipeline during demos/tests.
// Usage: node tools/simulate_pipeline.js PEN-01 [baseTempC] [intervalSeconds]
const [pen = 'PEN-01', base = '30', every = '1'] = process.argv.slice(2);
const url = `http://127.0.0.1:${process.env.PORT || 8080}/api/ingest`;
let i = 0;
setInterval(async () => {
  const body = { pen_id: pen, timestamp: new Date().toISOString(), surface_temp_c: Number(base) + Math.sin(i / 10) * 0.5,
    posture: Math.floor(i / 20) % 2 ? 'Not lying' : 'Lying' };
  i++;
  try { const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', ...(process.env.INGEST_TOKEN ? { 'X-Ingest-Token': process.env.INGEST_TOKEN } : {}) }, body: JSON.stringify(body) });
    if (!r.ok) console.log(r.status, (await r.json()).error); } catch (e) { console.log('server unreachable'); }
}, Number(every) * 1000);
