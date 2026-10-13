# Swine Monitoring Dashboard (Node.js, zero npm dependencies)

Local, browser-based dashboard for the Raspberry Pi. It consumes results from an existing monitoring pipeline; it never opens a camera or runs inference. Screening display only, not a veterinary diagnosis.

## Run
Requires Node 22.13 or newer (`node:sqlite`). Check the Pi with `node -v`.

    npm start                      # binds to 127.0.0.1:8080 unless config.json or HOST sets another host
    npm test                       # smoke tests on a temp DB with synthetic data
    npm run simulate -- PEN-01 30  # synthetic demo readings only

### Local-only default
The default is intentionally locked to the loopback interface (`127.0.0.1`) so a fresh `npm start` is not reachable from other devices. To allow LAN access, edit `config.json` and set `"host": "0.0.0.0"` or start with `HOST=<Pi LAN address> npm start` (or `HOST=0.0.0.0` for all addresses). The Pi's LAN address can be found with `hostname -I` (Linux) or `ip addr` / `ifconfig` and then choose the address on the local network, e.g. `192.168.1.42`.

Do not port-forward the dashboard to the internet. There is no authentication or encryption; state-changing UI routes only reject accidental or cross-origin requests.

## Pipeline contract
The project expects the existing pipeline to send one JSON payload per reading to the dashboard on the same machine (loopback only):

    { "pen_id": "PEN-01", "timestamp": "2026-10-12T06:05:00.000Z", "surface_temp_c": 31.4, "posture": "Lying" }

The dashboard accepts `surface_temp_c` as a number in 0-60 °C or `null`, and accepts `posture` values `"Lying"`, `"Not lying"`, or `"Unknown"`. The pipeline may also send `timestamp` as ISO 8601 UTC or epoch seconds. The real pipeline contract is documented in `docs/INTEGRATION.md`.

Rejected values: missing or future timestamps, unknown pen, no active session, pre-session readings, out-of-order or duplicate timestamps, and invalid temperatures.

## Current project features
The project is a local Raspberry Pi dashboard for observing pen activity and screening state. It is not a camera application and it does not diagnose animals.

Current implemented features:
- Local SQLite-backed storage for pens, sessions, measurements, alerts, and lifecycle log events.
- Pen/session lifecycle controls: start session, end session, and enforce one active session per pen.
- Pipeline ingest API at `/api/ingest` for temperature and posture readings from the monitoring pipeline.
- Session and log views showing historical records, counts, and filters.
- Screening status states: `Monitoring`, `Threshold reached`, `Inconclusive`, and `No active session`.
- Alert detection using configured threshold logic with required criteria details displayed in the UI and stored in the log.
- Alert acknowledgement flow that marks a screening alert as resolved while preserving the original alert history.
- Stale detection for missing or old readings using the configured stale timeout.
- Lying duration calculation based on pipeline timestamps, with gaps ignored when they exceed `max_lying_gap_s`.
- UI state for status cards, pen summaries, and last-updated server time.
- Light and dark mode support via CSS theme variables.
- Synthetic test tooling to exercise the app without touching the real database.
- Loopback-only ingest restriction and same-origin checks for state-changing UI routes.
- Startup safety checks and default host binding that does not expose the dashboard on every interface.

## What was added or implemented in this pass
- Default bind host changed from `0.0.0.0` to `127.0.0.1` in [config.json](<f:/Side-project/WebApp PD/config.json>) and [lib/config.js](<f:/Side-project/WebApp PD/lib/config.js>), with explicit LAN opt-in documented in this README.
- Node version enforcement for `node:sqlite` compatibility using Node 22.13+ in [server.js](<f:/Side-project/WebApp PD/server.js>).
- Safer cumulative lying-duration persistence to avoid losing the counter after a restart, while keeping downtime out of the session total.
- Ingest rate limiting as a basic guard against runaway pipeline floods.
- More explicit startup warnings that state the actual bound address and the fact that there is no authentication.
- Updated smoke testing in [tools/smoke_test.js](<f:/Side-project/WebApp PD/tools/smoke_test.js>) covering the required guardrail behaviors without touching the real DB.
- New project notes in [docs/INTEGRATION.md](<f:/Side-project/WebApp PD/docs/INTEGRATION.md>) for the pipeline contract and assumptions.
- Verification status file in [docs/VERIFICATION.md](<f:/Side-project/WebApp PD/docs/VERIFICATION.md>) documenting what was actually tested and what remains untested.
- README documentation updated to describe the actual current project state, configuration, and local-only security assumptions.

## Not implemented by default
RGB/thermal preview, evidence snapshots (the `evidence_path` field is reserved), SMS/push, accounts, cloud, and a one-click history clear. Any history-clearing path must be separate, explicit, and confirmed on purpose.

## Local data and retention
The data directory is stored under `data/` and the SQLite database is local to the Pi. For capacity/retention, keep the database on the Pi's local disk and rotate or archive older records separately when needed; do not add an automatic purge path as part of the dashboard itself.

## Configuration reference
The default config is intentionally conservative:

    {
      "host": "127.0.0.1",
      "port": 8080,
      "pens": ["PEN-01"],
      "stale_after_s": 30,
      "log_interval_s": 10,
      "max_lying_gap_s": 15,
      "screening": {
        "surface_temp_threshold_c": null,
        "consecutive_readings": 3,
        "min_lying_duration_s": 0
      }
    }

Set `HOST` or `PORT` in the shell to override the values at startup. Use `SWINE_CONFIG` or `SWINE_DB` to point to a custom configuration or database file.

## Change summary
See `docs/INTEGRATION.md` and `docs/VERIFICATION.md` for the operational contract and the verification status for this build.
