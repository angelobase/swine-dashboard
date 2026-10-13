'use strict';
const fs = require('node:fs');
const path = require('node:path');

const DEFAULTS = {
  host: '127.0.0.1', port: 8080, pens: ['PEN-01'],
  stale_after_s: 30, log_interval_s: 10, max_lying_gap_s: 15,
  ingest_rate_limit_per_sec: 20,
  screening: { surface_temp_threshold_c: null, consecutive_readings: 3, min_lying_duration_s: 0 },
  // Optional camera preview. OFF by default. The dashboard never opens a camera: the pipeline pushes frames.
  preview: { enabled: false, refresh_ms: 1000, max_frame_bytes: 262144, stale_after_s: 5, rate_limit_per_sec: 10 },
};

function load() {
  const file = process.env.SWINE_CONFIG || path.join(__dirname, '..', 'config.json');
  const user = JSON.parse(fs.readFileSync(file, 'utf8'));
  const c = { ...DEFAULTS, ...user, screening: { ...DEFAULTS.screening, ...(user.screening || {}) },
    preview: { ...DEFAULTS.preview, ...(user.preview || {}) } };
  if (process.env.HOST) c.host = process.env.HOST;
  if (process.env.PORT) c.port = Number(process.env.PORT);
  if (!Array.isArray(c.pens) || !c.pens.every((p) => /^[A-Za-z0-9_-]{1,32}$/.test(p))) throw new Error('config.pens invalid');
  if (!Number.isFinite(c.ingest_rate_limit_per_sec) || c.ingest_rate_limit_per_sec <= 0) throw new Error('ingest_rate_limit_per_sec must be a positive number');
  const t = c.screening.surface_temp_threshold_c;
  if (t !== null && !Number.isFinite(t)) throw new Error('surface_temp_threshold_c must be a number or null');
  if (!Number.isInteger(c.screening.consecutive_readings) || c.screening.consecutive_readings < 1) throw new Error('consecutive_readings must be an integer >= 1');
  const pv = c.preview;
  if (typeof pv.enabled !== 'boolean') throw new Error('preview.enabled must be true or false');
  if (!Number.isFinite(pv.refresh_ms) || pv.refresh_ms < 500) throw new Error('preview.refresh_ms must be a number >= 500');
  if (!Number.isInteger(pv.max_frame_bytes) || pv.max_frame_bytes < 1024 || pv.max_frame_bytes > 2097152) throw new Error('preview.max_frame_bytes must be an integer between 1024 and 2097152');
  if (!Number.isFinite(pv.stale_after_s) || pv.stale_after_s <= 0) throw new Error('preview.stale_after_s must be a positive number');
  if (!Number.isFinite(pv.rate_limit_per_sec) || pv.rate_limit_per_sec <= 0) throw new Error('preview.rate_limit_per_sec must be a positive number');
  c.db_path = process.env.SWINE_DB || path.join(__dirname, '..', 'data', 'swine.db');
  c.ingest_token = process.env.INGEST_TOKEN || null;
  return c;
}
module.exports = { load };
