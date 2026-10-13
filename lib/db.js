'use strict';
// SQLite via Node's built-in node:sqlite (Node >= 22.13). No native add-ons to compile on the Pi.
// Timestamp convention: ISO 8601, UTC, millisecond precision, e.g. 2026-10-12T06:05:00.000Z.
const { DatabaseSync } = require('node:sqlite');

const SCHEMA = `
CREATE TABLE IF NOT EXISTS sessions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  pen_id       TEXT NOT NULL,
  started_at   TEXT NOT NULL,
  ended_at     TEXT,
  lying_s      REAL NOT NULL DEFAULT 0,      -- last persisted cumulative lying seconds
  alert_active INTEGER NOT NULL DEFAULT 0    -- 1 = unacknowledged screening alert
);
-- Database-level guarantee: at most one active session per pen.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_session_per_pen
  ON sessions(pen_id) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS records (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  ts               TEXT NOT NULL,
  pen_id           TEXT NOT NULL,
  session_id       INTEGER NOT NULL REFERENCES sessions(id),
  event_type       TEXT NOT NULL CHECK (event_type IN
                     ('Measurement','Session started','Session ended','Alert','Alert acknowledged')),
  surface_temp_c   REAL,
  posture          TEXT CHECK (posture IS NULL OR posture IN ('Lying','Not lying','Unknown')),
  lying_s          REAL,
  screening_status TEXT,
  detail           TEXT,
  evidence_path    TEXT
);
CREATE INDEX IF NOT EXISTS records_pen_idx     ON records(pen_id, id DESC);
CREATE INDEX IF NOT EXISTS records_session_idx ON records(session_id, id DESC);
`;

function open(file) {
  const db = new DatabaseSync(file);
  db.exec('PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 3000;');
  db.exec(SCHEMA);
  return db;
}

// Runs fn inside a transaction; rolls back and rethrows on any error.
function tx(db, fn) {
  db.exec('BEGIN IMMEDIATE');
  try { const r = fn(); db.exec('COMMIT'); return r; }
  catch (e) { try { db.exec('ROLLBACK'); } catch (_) {} throw e; }
}

const sessionCode = (id) => `SESSION-${String(id).padStart(3, '0')}`;
module.exports = { open, tx, sessionCode };
