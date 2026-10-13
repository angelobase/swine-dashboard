# Optional camera preview

Off by default. The dashboard never opens a camera and never runs inference. The pipeline pushes its
latest already-captured frame; the dashboard keeps only the newest frame per pen and camera in memory.

## Enable
In `config.json`:

    "preview": { "enabled": true, "refresh_ms": 1000, "max_frame_bytes": 262144, "stale_after_s": 5, "rate_limit_per_sec": 10 }

Restart `npm start`. The preview controls are built into `public/app.js` and `public/style.css`;
`public/index.html` is unchanged.

## Endpoints
| Route | Who | Purpose |
|---|---|---|
| `POST /api/preview/<PEN>/rgb` | pipeline, loopback only | Send a JPEG (`Content-Type: image/jpeg`) |
| `POST /api/preview/<PEN>/thermal` | pipeline, loopback only | Same, for the thermal view |
| `GET /api/preview/<PEN>/rgb.jpg` | browser | Latest frame (404 if none) |
| `GET /api/preview/status` | browser | Enabled flag, refresh rate, frame age per pen |

Pushes use the same `INGEST_TOKEN` as ingest when one is set. Frames must be JPEG, at most
`max_frame_bytes`, and rate-limited (HTTP 429 when flooded).

## Behaviour
- Each pen card has an **RGB preview** toggle, off by default. The browser polls only while it is on, the tab is visible and the Pens view is open.
- **Thermal preview** appears only after the pipeline has pushed a thermal frame.
- If frames stop for more than `stale_after_s`, the card shows "No recent frame" instead of an old image.
- Nothing is written to SQLite. Lying duration and screening still use ingest timestamps only.

## Pipeline side
See `tools/push_preview_example.py`. Start with 480 px wide, JPEG quality 60, one frame per second, then benchmark on the Pi.

## Test
`node tools/preview_test.js` (temporary DB and config).

## Note
Preview frames are served to anyone who can reach the dashboard. Keep the default loopback binding, or use LAN access only on a network you trust.
