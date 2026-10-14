# Prototype (headless) over MQTT

Pi: `prototype/inlayo8.py` runs scheduled screening sessions (see `docs/SCHEDULED_SESSIONS.md`). It keeps its own
log, evaluates each pig at the end of the session and sends at most one SMS through the SIM800L, whether or not the
dashboard is reachable. It publishes to the MQTT broker built into the dashboard (`lib/mqtt_hub.js`).
`tools/mqtt_bridge.py` and a separate Mosquitto broker are no longer used.

## One-time setup on the Pi
Edit `prototype.json` (mqtt.password, GSM recipient/port, screening values). Copy `YOLOv8A_SD_best.onnx` next to
`inlayo8.py`. Install: `pip install paho-mqtt pyserial` (plus the existing spidev, opencv, numpy, onnxruntime).
Then run `python3 calibrate.py` once (needs a screen) to create `calibration.json`.

## Run
    python3 inlayo8.py            # ready; start the session from the dashboard
    python3 inlayo8.py --start    # start a full session now, no dashboard needed
    python3 inlayo8.py --test     # start a short test session now
    python3 inlayo8.py --abandon  # mark an unfinished session abandoned (no evaluation, no SMS)

If the Pi restarts during a session, run `python3 inlayo8.py` again: the session resumes.
In the dashboard: **+ > Add a pen**, pick the prototype from the list, then **Start**.
Set `preview.enabled: true` in `config.json` to see each capture's images on the tile.

## Files on the Pi
| File | Role |
|---|---|
| `inlayo8.py` | Session scheduler, local log, evaluation, SMS, MQTT |
| `capture_once.py` | One capture (thermal + webcam burst), run as a subprocess |
| `screening.py` | Statuses, end-of-session rule, SMS text |
| `pig_ids.py` | Placeholder position-based pig IDs |
| `pi_store.py` | Local SQLite log (`pi_data/swine_pi.db`) |
| `calibrate.py` | One-time RGB-to-thermal calibration |
| `yolo_worker.py`, `thermal_worker.py`, `gsm_sms.py` | Unchanged; their camera, model, thermal and AT code is reused |
| `camera_logs/capture.log`, `camera_logs/sms_events.jsonl` | Capture output and SMS history |

## Topics (base = swine/<device_id>)
session (retained), sample, state (retained), event, status (retained, LWT), preview/rgb, preview/thermal,
cmd (dashboard -> Pi: start, stop, replay).

## Notes
- Reset flags and the Cam selector are gone: there are no latched flags and no continuous camera.
- Old `prototype.json` keys (`high_temperature_threshold_c`, `reading_interval_s`, `mode`, `cycle_s`,
  `behavior_confirmation_s`) are ignored.
- Tests (no hardware needed): `python3 -m unittest discover -s tests -v`
