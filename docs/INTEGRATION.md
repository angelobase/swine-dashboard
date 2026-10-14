# Pipeline integration notes

This repository does not include the source code for the existing monitoring pipeline, so the dashboard contract is documented as the smallest safe assumption. The app is designed to consume a pipeline-generated JSON payload for each reading rather than own the capture loop itself.

## Assumed interface
The dashboard expects the pipeline process to send a JSON payload over HTTP to `http://127.0.0.1:8080/api/ingest` (or the configured `HOST`/`PORT` values) from the same machine. The payload is intentionally minimal and does not add fields that the pipeline cannot be expected to provide reliably.

Example payload:

```json
{
  "pen_id": "PEN-01",
  "timestamp": "2026-10-12T06:05:00.000Z",
  "surface_temp_c": 31.4,
  "posture": "Lying"
}
```

Required fields:
- `pen_id`: stable pen identifier, from the dashboard config (`config.json`)
- `timestamp`: ISO 8601 timestamp or epoch seconds from the pipeline
- `surface_temp_c`: measured surface temperature in °C, or `null`
- `posture`: one of `"Lying"`, `"Not lying"`, or `"Unknown"`

## Optional preview interface
Preview is separate from ingest and off by default. When `preview.enabled` is true in `config.json`, the pipeline may also push its latest already-captured frame as a JPEG:

    POST http://127.0.0.1:8080/api/preview/<PEN_ID>/rgb        Content-Type: image/jpeg
    POST http://127.0.0.1:8080/api/preview/<PEN_ID>/thermal    Content-Type: image/jpeg   (optional)

- The body is the raw JPEG bytes, with no JSON wrapper and no extra fields.
- Accepted only from the Pi itself, with `X-Ingest-Token` when `INGEST_TOKEN` is set.
- Limits: JPEG only, at most `preview.max_frame_bytes` (default 262144), rate-limited (HTTP 429 when flooded).
- Suggested settings: about 480 px wide, JPEG quality 60, 1 frame per second or less.
- `tools/push_preview_example.py` is a ready-made helper. It reuses the frame the pipeline already has, runs in a background thread, and swallows errors so it cannot disturb the pipeline.
- The dashboard keeps only the newest frame per pen and camera in memory. Frames are never written to SQLite, and `evidence_path` is still reserved and unused.
- Preview frames play no part in ingest: they carry no timestamp, are never used for lying duration or screening, and do not touch the session or alert logic.

See `docs/PREVIEW.md` for setup and testing.

## What the dashboard uses
The dashboard consumes these values to:
- assign the record to the active pen session
- reject out-of-order, duplicate, or future timestamps
- compute a cumulative lying duration from consecutive pipeline timestamps
- set a screening state when configured thresholds are met
- persist a local SQLite record and alert log

## Assumptions and constraints
- The pipeline owns camera acquisition, thermal readings, posture inference, and timestamp creation.
- The dashboard does not create a second camera or inference process.
- There is no observed direct database or queue output in this repo; all output is assumed to be through the HTTP ingest route.
- The dashboard does not invent fields like confidence scores, derived fever claims, or frame metadata that the pipeline cannot supply. The optional preview accepts image bytes only and reads no metadata from them.
- The preview adds no camera access to the dashboard. It never opens a camera, decodes video, or runs a second capture or thermal loop.
- If the real pipeline uses a different contract later, the adapter should be the smallest local bridge or wrapper around the actual source rather than modifying the pipeline loop itself.

## Operational notes
- Ingest traffic is loopback-only by default. Preview pushes are loopback-only too.
- An optional `INGEST_TOKEN` may be used for extra pipeline authentication. It applies to preview pushes as well.
- Preview frames are served to anyone who can reach the dashboard and there is no login, so keep the loopback default or use LAN access only on a trusted network.
- Requests are rejected when the timestamp is invalid, the pen is unknown, the session is inactive, or the reading is outside the accepted ranges.

This contract is intentionally conservative and matches the existing dashboard implementation without inventing a richer pipeline API.

## Scheduled sessions (prototype over MQTT)
Pens added with a prototype run scheduled sessions; `/api/ingest` readings are refused for them. Messages
(base `swine/<device_id>`), all JSON:

- `session` (retained, on every change and every 30 s): `uid`, `state` (running | evaluating | finished | ended_early |
  abandoned), `test`, `started_at`, `ends_at`, `interval_s`, `duration_s`, `captures`, `next_seq`, `last_seq`,
  `next_capture_at`, `capturing`, `stopping`, `clock_source`, `rule`, `ended_at`, `result` (per pig: longest lying,
  final temperature, statuses, `alert`), `sms` (`state`, `detail`, `time`).
- `sample` (one per capture): `uid`, `seq` (0-based), `ts`, `ok`, `error`, `thermal_valid`, `calibrated`, `images`,
  `pigs` (per pig: `pig_id`, `observed`, `posture`, `lying_s`, `surface_temp_c`, `behavioral_status`, `thermal_status`).
- `cmd` (dashboard -> Pi): `{"action": "start", "uid", "pen", "test", "server_time"}`, `{"action": "stop", "uid"}`,
  `{"action": "replay", "uid", "seqs": [...]}`.

The dashboard stores one `Measurement` record per pig per capture (columns `seq`, `pig_id`, `behavioral_status`,
`thermal_status` added by migration), deduplicated, so replays are safe. Times come from the Pi, corrected at start.
The loopback route `POST /api/device/<PEN>` also accepts `{"session": ...}` and `{"sample": ...}` for testing.
