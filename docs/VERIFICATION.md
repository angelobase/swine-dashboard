# Verification status

Date: 2026-10-13 (preview pass added; the 2026-10-12 results below are unchanged)

## Project documentation summary
This project is a local Raspberry Pi dashboard for monitoring pen activity. It is designed to consume pipeline data rather than own camera or inference code. The current feature set includes:
- a local SQLite database for sessions and measurement history
- pen/session management with one active session per pen
- ingest of pipeline temperature/posture readings
- screening states and alert acknowledgement
- stale detection and lying-duration tracking
- log and session views with filters
- light and dark styling for browser use on local hardware
- synthetic smoke-test tooling for safe validation without touching the live DB
- optional low-rate RGB preview, with an optional thermal preview, shown inside each pen card (off by default)

## What was added or implemented in this pass
- Default host binding changed to the loopback address for safer local-only startup.
- Node runtime version guard to ensure compatibility with `node:sqlite`.
- More robust lying-duration persistence so a server restart cannot incorrectly credit downtime.
- Ingest rate limiting to reduce the risk of a runaway pipeline flooding the database.
- Updated documentation and smoke coverage for the current project state.
- Integration notes and verification notes describing assumptions and tested behavior.

## What was added in the camera preview pass (2026-10-13)
- `server.js`: preview routes (`GET /api/preview/status`, `GET /api/preview/<PEN>/<rgb|thermal>.jpg`, loopback-only `POST /api/preview/<PEN>/<rgb|thermal>`). Only the newest frame per pen and camera is kept in memory. Nothing is written to SQLite.
- Push guards: JPEG-only (magic bytes checked), size cap (`max_frame_bytes`), separate rate limit (`rate_limit_per_sec`), loopback-only, optional `INGEST_TOKEN`, unknown pen rejected.
- `lib/config.js`: new `preview` block (disabled by default) with validation of `enabled`, `refresh_ms` (>= 500), `max_frame_bytes`, `stale_after_s` and `rate_limit_per_sec`.
- `public/app.js` and `public/style.css`: an **RGB preview** button on each pen card (off by default) and a **Thermal preview** button that appears only after a thermal frame has been received. The image refreshes about once per second, polling stops in a hidden tab or outside the Pens view, and a "No recent frame" message replaces stale images. `public/index.html` is unchanged.
- `tools/push_preview_example.py`: helper for the existing pipeline (background thread, throttled, downscaled, never raises into the pipeline).
- `tools/test_push.py`: fake-frame test for the helper.
- `tools/preview_test.js`: server-side preview checks on a temporary config and database.
- `docs/PREVIEW.md` added; `docs/INTEGRATION.md` updated with the preview contract.

## Environment checked
- Host OS: Windows 11 / developer workstation
- Node version: `node -v` was checked in this environment and is expected to be a supported version for the project; the runtime also enforces a minimum of Node 22.13 before the server starts.
- Pi hardware: not tested on the actual Raspberry Pi in this session.
- Preview pass (2026-10-13): run on a Linux workstation with Node v22.22.2, in a scratch copy of the project containing the real `index.html`, `app.js` and `style.css`. Not the Windows 11 machine above.

## Verified checks performed here
- `npm test` on a temporary SQLite database with synthetic data: passed.
- Default host binding check: the code was updated to default to `127.0.0.1` and the startup message states the bound address.
- Loopback-only ingest enforcement: code rejects non-loopback addresses before accepting a reading.
- Session lifecycle guardrails: server rejects duplicate active sessions, preserves history, and resets state on session change.
- Alert and stale behavior: smoke tests confirm threshold alerting, stale read handling, and no inherited alert across a new session.

## Preview checks performed (2026-10-13)
`node tools/preview_test.js` passed all 14 checks on a temporary config and database:
- With preview disabled: POST returns 404, status reports `enabled: false`, frame GET returns 404.
- With preview enabled: status reports `enabled: true`; no frame yet returns 404; a valid RGB JPEG is accepted and served back as `image/jpeg`.
- Rejections: non-JPEG body (415), oversized frame (413), unknown pen (404).
- Status shows RGB present and thermal absent until a thermal frame is accepted; a thermal frame is then accepted separately.
- Flooding frames returns HTTP 429.
- Pushing preview frames did not create a session or change monitoring state.
- `node --check` passed for `server.js`, `lib/config.js`, `public/app.js` and the test script; `tools/push_preview_example.py` and `tools/test_push.py` parsed without error.

## Items not directly verified on the Pi
- Live access from another local device on the LAN.
- Camera preview and performance benchmarking on the Pi: Pi frame rate and input lag with the preview off versus on, at the suggested 480 px wide, JPEG quality 60, 1 frame per second.
- Preview controls in a real browser: `app.js` was syntax-checked but not run in a browser, so the card layout, the toggle buttons, image refresh, the stale message, and light and dark appearance are unconfirmed.
- `tools/push_preview_example.py` and `tools/test_push.py` were not executed against a running server; only the server side was exercised.
- `npm test` was not re-run after the preview changes, so confirm the existing smoke tests still pass.
- Real thermal frames from the production thermal worker (the route works with a test JPEG only).
- Real Raspberry Pi display sizing and touch-target verification.
- Actual Node installation on the target Pi model.
- End-to-end pipeline integration with the production pipeline hardware.

## Status summary
| Item | Status |
| --- | --- |
| Default bind address is local-only | Passed in code and smoke-checked |
| Loopback-only ingest | Passed in code and smoke-checked |
| Session start/end integrity | Passed in code and smoke-checked |
| Lying-duration restart safety | Implemented with safe persistence logic; smoke-checked for restart semantics |
| Database failure handling | Improved and covered by script checks |
| Full Pi verification checklist | Not tested on the actual Pi |
| Preview server routes and limits | Passed in `tools/preview_test.js` (14 checks, temporary DB) |
| Preview controls in the browser | Not verified; not run in a browser |
| Preview pusher helper | Not verified end to end; syntax-checked only |
| Preview performance on the Pi | Not benchmarked |
| Optional preview default | Off unless `preview.enabled` is true in `config.json` |

This file documents only what was actually checked in this environment and does not claim a pass for items that were not run on the target Raspberry Pi hardware.

---

# Scheduled sessions pass (2026-10-14)

## What changed
- **Pi:** `inlayo8.py` rewritten around scheduled sessions (scheduler, local log, resume after restart, missed captures,
  end-of-session evaluation, one SMS with limited retries, replay). New: `capture_once.py`, `screening.py`, `pig_ids.py`
  (placeholder), `pi_store.py`, `calibrate.py`, `tests/test_scheduled.py`. `prototype.json` has new sections.
  `yolo_worker.py`, `thermal_worker.py`, `gsm_sms.py` are unchanged.
- **Dashboard:** `server.js` (scheduled sessions, replay requests, test mode, End/Acknowledge for scheduled sessions,
  legacy live sessions kept for pens without a prototype), `lib/db.js` (in-place migration: adds columns, tables and
  indexes only), `lib/export.js` (methodology columns and summary), `lib/mqtt_hub.js` (session and sample topics),
  `lib/config.js` (`scheduled.overdue_grace_s`), `public/app.js`, new `public/schedule.css`, `tools/schedule_test.js`.
  `index.html`, `style.css` and `beacon.js` are unchanged.

## Environment checked
Linux workstation, Node v22.22.2, Python 3 with numpy and OpenCV. No Raspberry Pi, cameras, modem or MQTT broker.

## Checks performed
- `python3 -m unittest discover -s tests` (14 tests, passed 6 runs in a row): threshold boundary (36.7 counts), lying
  bouts and statuses, the lookback rule, SMS length and GSM characters, placeholder IDs, burst clustering, box mapping
  and per-pig temperature; `capture_once.run()` end to end through the real `yolo_worker.py` and `thermal_worker.py`
  with the Lepton, webcam and model faked; a full test session; resume after a simulated power cut with missed
  captures; early End; replay; refused start; restart during SMS sending (reported unknown, not resent); SMS retries
  only before the text reaches the modem.
- `node tools/schedule_test.js` (52 checks, passed 3 runs in a row) on a temporary config and an OLD-schema database:
  migration keeps legacy data; test mode; start command; confirmation; captures; duplicate replays ignored; missing
  captures requested; overdue capture; finish with one alert record; later SMS update; acknowledging an ended
  session's alert; CSV and PDF exports; Pi-started, superseded, refused, offline-finished and deleted sessions; legacy
  live pens still working.
- Browser render in headless Chromium with a stand-in `index.html` and `style.css` (the real ones were not
  available): Pens view (running, waiting and finished-with-alert tiles), Records view, phone width, Test mode
  toggle. No JavaScript errors; the only 404 was the browser's own favicon request.
- `node --check` on all changed JavaScript files; `py_compile` on all Python files.

## Not verified
- Anything on the Raspberry Pi: real Lepton bursts, webcam warm-up, model load time per capture, total capture
  duration (needed to confirm the 30 s test interval and the 120 s capture timeout), memory and temperature.
- The real SIM800L: the AT sequence is the same as `gsm_sms.py`, but the staged send was only tested with fakes.
- MQTT end to end (paho on the Pi to aedes in the dashboard), including the retained session message and replay over
  a real Wi-Fi drop. The server side was exercised through the loopback device route instead.
- `calibrate.py` (needs a screen and both cameras); only its fitting logic is shared with tested code.
- The dashboard with the real `index.html` and `style.css`, and the existing `npm test` and `tools/preview_test.js`
  (not available here).
- A full 19-hour run.

## Simulator pass (2026-10-14, later the same day)
- `tools/test_push.py` (dummy prototype, real `inlayo8.py` code) was run over HTTP against the real dashboard server:
  a full session with alert and simulated SMS; 35 % of captures deliberately lost and all recovered by replay over
  about 90 s; End pressed on the dashboard during a session (prototype ended early, no SMS); tile render in Chromium
  with the simulated RGB and thermal images.
- Bugs found by it and fixed: a dashboard session stayed "evaluating" after the SMS finished (now becomes
  "finished"; covered by 2 new checks, 54 in total); durations under a minute showed as "0 min" on the tile and "0m"
  in the SMS (now shown in seconds; covered by a Pi test).
- `inlayo8.py` and `capture_once.py` now import `fcntl` inside the functions that use it, so they load on Windows.
- Not verified: the simulator's MQTT mode (paho-mqtt and the aedes broker were not installed here), and running it
  on Windows.

## One prototype on two pens (2026-10-15)
Reported: `session message rejected (409): Session ... belongs to PEN-02` and the prototype refusing a start while
the dashboard showed "Waiting for prototype". Cause: the same prototype was added to two pens (a typed or pasted
address skipped the duplicate check), so starts went out through one pen and replies came back through the other.
Fixed in `server.js`: adding a prototype already used by another pen is refused; existing duplicates are reported at
startup; a sessions

## MQTT over WebSockets and dashboard login, for Render (2026-10-15)
- Dashboard: the built-in broker also accepts MQTT over WebSockets at `/mqtt` on the web port; it is only enabled
  with an MQTT password. Optional login (HTTP Basic) via `DASHBOARD_PASSWORD`; `/healthz` stays open. Settings from
  environment variables (`SWINE_CONFIG_JSON`, `MQTT_TCP`, `MQTT_WS_PATH`, `MQTT_USER`, `DASHBOARD_USER`).
- Pi: `mqtt.host` accepts `wss://<host>/mqtt` (also `ws://`, `mqtt://`, `mqtts://`).
- `node tools/ws_test.js` (21 checks, passed 3 runs in a row): real server with settings from environment variables,
  real `ws` 8.20 WebSocket with MQTT 3.1.1 packets written byte for byte; login and health route; wrong path and wrong
  MQTT password refused; connect, Add-a-pen discovery, start and stop commands delivered, session, capture and image
  recorded, publishing as another device refused. Negative control: with no MQTT password the WebSocket path stays
  off and the test fails as expected.
- Pi tests (17): `wss://` address parsing; paho client set up with WebSocket transport, path `/mqtt`, TLS, port 443.
- **Not verified:** the real aedes broker was not installed here; the test ran against a minimal stand-in with the same
  interface. Run `node tools/ws_test.js` after `npm install` to check against the real aedes. Also not verified: real
  paho-mqtt over WebSockets, and Render itself.

## Missing disk on Render (2026-10-15)
Reported: `EACCES: permission denied, mkdir '/var/data'` at startup on Render (no disk mounted at the `SWINE_DB`
folder). `server.js` now exits with a plain explanation instead of a stack trace; checked by forcing the folder
creation to fail (with and without the `RENDER` variable). `node tools/schedule_test.js`: 63 passed.
