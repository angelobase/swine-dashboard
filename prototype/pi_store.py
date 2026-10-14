"""Local log on the Pi: every scheduled session and every capture, written before anything is published.

The dashboard can be down, restarted or out of Wi-Fi range for the whole run; nothing is lost, and the
dashboard asks for whatever it missed when it reconnects (MQTT 'replay' command).
All access happens from the controller's main thread.
"""
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  uid TEXT PRIMARY KEY, pen TEXT NOT NULL, test INTEGER NOT NULL,
  started_raw REAL NOT NULL,          -- Pi clock (time.time()) at start; used for scheduling
  clock_offset_s REAL NOT NULL,       -- added to Pi clock for every reported time (from the dashboard at start)
  clock_source TEXT NOT NULL,         -- 'dashboard' or 'pi'
  interval_s REAL NOT NULL, duration_s REAL NOT NULL, last_slot INTEGER NOT NULL,
  threshold_c REAL NOT NULL, rule_n INTEGER NOT NULL, lookback INTEGER NOT NULL,
  state TEXT NOT NULL,                -- running | evaluating | finished | ended_early | abandoned
  next_seq INTEGER NOT NULL DEFAULT 0,
  ended_at REAL, result TEXT, sms TEXT, ids TEXT NOT NULL, tracks TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
  uid TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (uid, seq)
);
"""
JSON_FIELDS = ('result', 'sms', 'ids', 'tracks')


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;' + SCHEMA)

    def _row(self, row):
        if row is None:
            return None
        s = dict(row)
        for k in JSON_FIELDS:
            s[k] = json.loads(s[k]) if s[k] else None
        return s

    def create(self, s):
        cols = ['uid', 'pen', 'test', 'started_raw', 'clock_offset_s', 'clock_source', 'interval_s', 'duration_s',
                'last_slot', 'threshold_c', 'rule_n', 'lookback', 'state', 'next_seq', 'ids', 'tracks']
        vals = [json.dumps(s[c]) if c in JSON_FIELDS else s[c] for c in cols]
        self.db.execute(f"INSERT INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)

    def unfinished(self):
        """The session the controller must resume: still capturing, or evaluated but not finished."""
        return self._row(self.db.execute(
            "SELECT * FROM sessions WHERE state IN ('running','evaluating') ORDER BY rowid DESC LIMIT 1").fetchone())

    def latest(self):
        return self._row(self.db.execute('SELECT * FROM sessions ORDER BY rowid DESC LIMIT 1').fetchone())

    def update(self, uid, **fields):
        sets = ', '.join(f'{k} = ?' for k in fields)
        vals = [json.dumps(v) if k in JSON_FIELDS else v for k, v in fields.items()]
        self.db.execute(f'UPDATE sessions SET {sets} WHERE uid = ?', vals + [uid])

    def add_sample(self, uid, seq, payload, **session_fields):
        """Sample and session progress in ONE transaction, so a power cut cannot leave them out of step."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT OR REPLACE INTO samples (uid, seq, payload) VALUES (?, ?, ?)', (uid, seq, json.dumps(payload)))
            if session_fields:
                self.update(uid, **session_fields)
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def samples(self, uid, seqs=None, after=None, limit=200):
        if seqs is not None:
            seqs = [int(x) for x in seqs][:limit]
            rows = self.db.execute(f"SELECT payload FROM samples WHERE uid = ? AND seq IN ({','.join('?' * len(seqs))}) ORDER BY seq",
                                   [uid] + seqs).fetchall() if seqs else []
        else:
            rows = self.db.execute('SELECT payload FROM samples WHERE uid = ? AND seq > ? ORDER BY seq LIMIT ?',
                                   (uid, -1 if after is None else int(after), limit)).fetchall()
        return [json.loads(r['payload']) for r in rows]

    def close(self):
        self.db.close()
