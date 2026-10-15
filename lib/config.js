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
  // Built-in MQTT broker. Prototypes connect to this computer on `port`. Empty password = open broker (trusted LAN only).
  mqtt: { enabled: true, host: '0.0.0.0', port: 1883, username: 'swine', password: '', beacon: true, discovery_port: 18830 },
  // Scheduled sessions (run by the prototype): a capture counts as overdue this long after its planned time.
  scheduled: { overdue_grace_s: 180 },
};

function load() {
  const file = process.env.SWINE_CONFIG || path.join(__dirname, '..', 'config.json');
  // config.json holds local settings (MQTT password) and is not committed to git; without it the defaults are used.
  let user = {};
  if (fs.existsSync(file)) user = JSON.parse(fs.readFileSync(file, 'utf8'));
  else console.log(`${path.basename(file)} not found: using the default settings. Copy config.example.json to config.json to change them.`);
  const c = { ...DEFAULTS, ...user, screening: { ...DEFAULTS.screening, ...(user.screening || {}) },
    preview: { ...DEFAULTS.preview, ...(user.preview || {}) }, mqtt: { ...DEFAULTS.mqtt, ...(user.mqtt || {}) },
    scheduled: { ...DEFAULTS.scheduled, ...(user.scheduled || {}) } };
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
  if (process.env.MQTT_PASS !== undefined) c.mqtt.password = process.env.MQTT_PASS;
  if (process.env.MQTT_PORT) c.mqtt.port = Number(process.env.MQTT_PORT);
  if (/^(0|off|false|no)$/i.test(process.env.MQTT_ENABLED || '')) c.mqtt.enabled = false; // e.g. MQTT_ENABLED=0 for tests
  if (/^(0|off|false|no)$/i.test(process.env.MQTT_BEACON || '')) c.mqtt.beacon = false;
  const mq = c.mqtt;
  if (typeof mq.enabled !== 'boolean') throw new Error('mqtt.enabled must be true or false');
  if (!Number.isInteger(mq.port) || mq.port < 1 || mq.port > 65535) throw new Error('mqtt.port must be an integer 1-65535');
  if (typeof mq.beacon !== 'boolean') throw new Error('mqtt.beacon must be true or false');
  if (!Number.isInteger(mq.discovery_port) || mq.discovery_port < 1 || mq.discovery_port > 65535) throw new Error('mqtt.discovery_port must be an integer 1-65535');
  if (typeof mq.password !== 'string' || typeof mq.username !== 'string') throw new Error('mqtt.username and mqtt.password must be strings');
  if (!Number.isFinite(c.scheduled.overdue_grace_s) || c.scheduled.overdue_grace_s < 0) throw new Error('scheduled.overdue_grace_s must be a number >= 0');
  c.db_path = process.env.SWINE_DB || path.join(__dirname, '..', 'data', 'swine.db');
  c.ingest_token = process.env.INGEST_TOKEN || null;
  return c;
}
module.exports = { load };
