# Instruction.md

## Context

You are a **senior full-stack developer and Raspberry Pi integration engineer** extending an existing local **Swine Monitoring Dashboard** (Node.js, zero npm dependencies, built-in `node:sqlite`). Act as an engineer improving working code, not rewriting it. Read the existing project first (`server.js`, `lib/db.js`, `lib/config.js`, `public/*`, `tools/*`, `config.json`, `README.md`) and keep its structure unless a change below requires otherwise. `guardrail.md` still applies in full and wins any conflict.

### What the system is
A local, browser-based dashboard hosted on the Raspberry Pi. It is a **client of the existing monitoring pipeline**, not another camera application. The pipeline owns camera acquisition, thermal readings, posture inference, timestamps and pen association. The dashboard receives derived results, manages pen sessions, stores records in SQLite, and shows screening status. No cloud, no native app, no user accounts, no SMS/push in required scope.

Permanent **pen IDs** (`PEN-01`) are separate from **sessions** (`SESSION-002`) for whichever animal currently occupies the pen. The system does not identify individual pigs. A dashboard alert is only seen by someone looking at the dashboard, and the screening threshold is not a validated surface-temperature fever criterion (the earlier threshold came from core temperature).

### What already exists (baseline, verified only by a smoke test on synthetic data on a PC)
- Pens from `config.json`; `Start Session` / `End Session`; one active session per pen enforced by a partial unique index; lifecycle changes and log events written in one transaction; UI confirms only after commit.
- `POST /api/ingest` (loopback only, optional token) accepting `pen_id`, `timestamp`, `surface_temp_c`, `posture`; rejects missing/future/out-of-order timestamps, unknown pens, no active session, readings before session start, out-of-range temperatures.
- Lying duration accumulated server-side from pipeline timestamps, gaps over `max_lying_gap_s` ignored.
- Screening: `Monitoring` / `Threshold reached` / `Inconclusive`; threshold is `null` by default so no alert fires; alerts need N consecutive readings, are logged with triggering values, and persist until acknowledged.
- Stale detection, Pens / Sessions / Log views, light and dark mode, `tools/simulate_pipeline.js` (synthetic), `tools/smoke_test.js`.
- **Not built:** RGB preview, thermal preview, evidence snapshots.

## Objective

Bring the dashboard to a defensible, demonstrable state by (A) fixing the weaknesses listed below, then (B) adding the optional low-rate camera preview **only if** the required workflow is verified and performance holds.

## Details

### A. Required improvements to the existing implementation

Work through these in order. Each item says what was wrong or unverified and what "done" means.

1. **Bind address default contradicts the guardrail.** The server defaults to `0.0.0.0`, which exposes it on every interface. Change the default to `127.0.0.1` and make LAN access an explicit setting (`host` in `config.json` or `HOST` env), with a startup message that states the address actually bound and warns that there is no authentication. Document how to find the Pi's LAN address. Done when a fresh `npm start` is reachable only from the same machine and the README explains how to opt in to LAN access.

2. **Pipeline integration was assumed, not inspected.** The `/api/ingest` contract was invented because the real pipeline code was not available. Before changing anything, locate the real pipeline's output (function call, queue, file, socket, or database) and record the findings in `docs/INTEGRATION.md`: how it produces temperature, posture, timestamps and pen association, its units, its output rate, and whether it runs on the same host. If the real interface differs, write the **smallest adapter** (for example a small module the pipeline calls, or a bridge script) rather than changing the pipeline's capture/inference loop. If the details are unknown, state the assumption in that file and keep the current contract. Do not invent fields the pipeline cannot supply.

3. **Lying-duration counter can lag after a restart.** It lives in memory and is saved only on the logging interval, so a crash or restart can lose up to one interval. Persist the cumulative value more safely (for example on every accepted reading in the same transaction as a light state update, or on a short fixed cadence plus on graceful shutdown) without writing every reading as a full record. After a restart, the first reading must not credit the downtime as lying time. Done when a test kills and restarts the server mid-session and the counter resumes correctly with the downtime excluded.

4. **Database failures were not tested.** Guardrails require that nothing is shown as saved if the write failed and that prior data survives a failed new session. Add tests that force a failure (for example a read-only DB file or a locked database) and verify: the API returns an error, the UI shows an actionable message, no session is created, the previous session and records are untouched, and the in-memory state does not show the unsaved reading as logged.

5. **The smoke test is untidy and thin.** `tools/smoke_test.js` contains leftover scaffolding (unused helpers, a no-op loop) and does not cover several guardrail items. Rewrite it cleanly and extend it to cover: stale detection, alert acknowledgement, alert not re-firing while one is unacknowledged, lying-duration gap handling, no inherited lying duration or alert in a new session, history preserved after end, filter validation, `409` on a second active session, rejection of cross-origin or header-less state-changing requests, and ingest refused from a non-loopback address where that can be tested. Use a temp DB; never touch the real one. `npm test` must exit non-zero on failure.

6. **Alert criteria are minimal and must stay honest.** Keep the default `surface_temp_threshold_c: null`. Keep `consecutive_readings` and optional `min_lying_duration_s`. Show the criteria beside every alert and in the log. Never ship a default numeric threshold, a confidence score, or wording such as "fever", "sick" or "unhealthy". Add a visible line on the dashboard when the threshold is not configured, so a healthy-looking `Monitoring` state is never mistaken for "checked and fine". Also state in the UI that absence of an alert does not mean an animal is well.

7. **UI was never checked on real screen sizes.** Test and fix layout at roughly 360 px (phone), 768 px (tablet), 1280 px (laptop) and the Pi's actual display resolution. Confirm status is distinguishable without color alone (text plus glyph plus border style), touch targets are at least 44 px, focus is visible, the table scrolls inside its container, and the `confirm()` on End Session is acceptable or replaced by an accessible dialog. Add a persistent "last updated" indicator and keep the offline/stale handling.

8. **Moderate hardening.** Add per-IP or global rate limiting on `/api/ingest` (a simple token bucket is enough) so a runaway pipeline cannot flood SQLite. Add a database size/retention note and a documented, **separate, confirmed** (not built unless requested) path for history clearing. Validate `evidence_path` values if the field is ever written. Keep SQL parameterized and the static file whitelist.

9. **Node version risk on the Pi.** The project needs Node 22.13 or newer for `node:sqlite`, and Raspberry Pi OS may ship an older version. Add a startup check that exits with a clear message when the version is too low and explains how to install a newer one. Record the tested Node version and Pi model in the README after testing on the device. Do not add native dependencies to work around this unless the owner asks.

10. **The verification checklist was never run.** Run the checklist at the end of `guardrail.md` on the actual Pi and record each item as passed, failed or not tested, with date, in `docs/VERIFICATION.md`. Do not claim a pass for anything not actually checked, and do not claim reachability from another device until it has been tested from that device.

### B. Optional low-rate camera preview (only after A is complete)

Build this only if items 1 to 10 are done and the owner still wants it. The preview is **observational only**.

1. **Ownership.** The dashboard never opens a camera and never starts a capture or thermal loop. The pipeline writes its most recent preview frame to a fixed local path (for example `data/preview/PEN-01-rgb.jpg`), as already-produced output. The pipeline must write atomically (write to a temp file, then rename) so the dashboard never serves a half-written image. Document this as the contract in `docs/INTEGRATION.md`. If the pipeline cannot supply a frame without extra work, say so and stop; do not add capture code.
2. **Serving.** Add `GET /api/preview/:pen/rgb` that serves the file for a pen from a **fixed, server-built path** (pen ID validated against `config.pens`; no user-supplied path segments). Respond `404` with a clear message when the file is missing, and report the frame's age from the file modification time. Mark frames older than the stale threshold as stale in the UI rather than showing them as current. Send `Cache-Control: no-store`. Cap the file size served.
3. **Rate and cost.** Preview is **off by default** (`preview.enabled: false`). When enabled, the browser requests a still image about every 1 to 2 seconds (configurable, minimum interval enforced server-side), and only for the visible tab and visible pen. Do not re-encode, resize or overlay on the server. Do not use MJPEG or other continuous streams.
4. **Presentation.** Label it "Observation view, not the monitoring clock". Show the frame time and a "stale" or "unavailable" state explicitly. Preview failure must never block session control, logging or alerts.
5. **Benchmark before keeping it.** On the Pi, measure pipeline capture FPS, inference latency and input lag with the preview off, then on at 1 Hz and 2 Hz, and record the results in `docs/PREVIEW_BENCHMARK.md`. If monitoring degrades materially, leave the feature disabled and document why. Do not raise the rate or resolution without re-measuring.
6. **Thermal preview** is a separate toggle, added only after RGB is stable, using the thermal worker's existing output. Same rules.
7. **Test support.** Extend `tools/simulate_pipeline.js` (clearly labelled synthetic) to write a generated placeholder frame atomically to the preview path so the feature can be tested on a PC without a camera. Generate it without new npm dependencies (for example a small PNG or BMP built with Node's `zlib`/`Buffer`).
8. **Evidence snapshots** remain out of scope unless separately requested. If added: save only when enabled and needed, to a local evidence directory, validate and constrain paths, and handle missing files gracefully.

### C. Working rules

- Keep it lean: no frontend framework, no new runtime dependencies without a stated reason.
- Do not make SMS, push, cloud, accounts or simultaneous camera streams part of this work.
- Do not claim authentication, encryption, diagnosis, SMS or cloud features exist. There is no authentication; state-changing routes only block accidental or cross-origin requests.
- Use synthetic or labelled test data in tests and demos. Never present simulated readings as real measurements.
- When something is unknown (pipeline interface, Pi performance, validated criteria), say so and choose the smallest safe option. Ask the owner before adding optional scope or changing contracts.

### D. Deliverables

- Updated code, tests, and `README.md` (run, LAN access, Node version, pipeline contract, config).
- `docs/INTEGRATION.md`, `docs/VERIFICATION.md`, and (if preview is built) `docs/PREVIEW_BENCHMARK.md`.
- A short change summary that lists what was changed, what was tested and where (PC or Pi), and what is still untested.

## Example

### Acceptance walk-through (defense demo)

1. `npm start` binds to the configured address and prints it. From the Pi and from a permitted phone or laptop on the same network, the dashboard loads.
2. `PEN-01` shows no session. Select **Start Session**: `SESSION-001` appears with a confirmation and a `Session started` log entry.
3. The pipeline supplies timestamped readings. The card shows the latest °C, posture, lying duration and a **current** freshness line. Stopping the pipeline turns the card **stale / Inconclusive** after the stale period.
4. With a throwaway test threshold configured, consecutive readings at or above it raise a visible alert that names the pen and session, shows the timestamp, readings and criteria, and states it is a screening result. It is logged and stays until acknowledged.
5. **End Session** records the end event. **Start Session** creates `SESSION-002` with a zero lying duration and no inherited readings or alert. `SESSION-001` remains in the Sessions and Log views.
6. If the preview was built and enabled, the observation view refreshes about once per second, shows its frame age, and the benchmark file shows monitoring performance was not materially degraded.

### Example record

| Field | Example value |
| --- | --- |
| Pen ID | `PEN-01` |
| Session ID | `SESSION-002` |
| Timestamp | `2026-10-12T06:05:00.000Z` |
| Surface temperature | Measured value in °C |
| Posture | `Lying` |
| Cumulative lying duration | Duration for `SESSION-002` only |
| Screening status | `Monitoring` |
| Event type | `Measurement` |
| Evidence path | Optional local path, or empty |

These values show structure only. They are not a prescribed threshold, clinical criterion or expected result.
