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
