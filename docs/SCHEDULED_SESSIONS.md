# Scheduled screening sessions

This follows the study methodology: one RGB and one thermal capture every 30 minutes for 19 hours, a per-pig
record for every capture, and one evaluation per pig at the end. The prototype (Raspberry Pi) runs the session
and sends the SMS itself; the dashboard records and displays it. Screening only, not a diagnosis of ASF.

## How a session runs
1. **Start.** Press **Start** on the pen (or **Start test** while Test mode is on). The dashboard sends the start
   command and its clock time; the Pi confirms within a few seconds. Without a dashboard:
   `python3 inlayo8.py --start` (or `--test`).
2. **Captures.** At each planned time the Pi runs `capture_once.py`: a short thermal burst (median of about
   9 frames), then a webcam burst (warm-up frames discarded, YOLO on 5 frames). A pig must appear in at least half of
   the frames; its posture is the majority vote. Cameras and the model are released between captures.
3. **Local log.** Every capture is saved in `pi_data/swine_pi.db` before it is published. If the dashboard misses
   captures (Wi-Fi drop, dashboard restart), it asks the Pi to send them again.
4. **Restart safety.** If the Pi restarts, `inlayo8.py` resumes the session. Planned captures that passed while it
   was off are recorded as missed. A capture may start up to 90 % of an interval late before it counts as missed.
5. **End.** After the last capture (39 captures for 19 h at 30 min: at 0:00, 0:30 … 19:00) the Pi evaluates every
   pig and sends at most one SMS. Pressing **End** early stops the session: it is evaluated, but no SMS is sent.

## Per-capture record (one row per pig, also in CSV/PDF exports)
| Field | Meaning |
|---|---|
| Time, Capture, Pig ID | When, which capture (1–39), which pig |
| Posture | Lying, Not lying, or Unknown (pig not detected in this capture) |
| Lying (s) | Estimated current lying bout: consecutive lying captures x interval |
| Surface temp | That pig's surface temperature, from its own box in the thermal image |
| Behavioral status | **Normal**, **Abnormal** (bout has reached the rule), **No data** (not detected) |
| Thermal status | **Normal**, **Elevated** (at or above the threshold), **No result** (no valid reading) |

A capture in which a pig is not detected ends its lying bout: unobserved time is never counted as lying.

## End-of-session rule (prototype.json "screening")
A pig is flagged when BOTH hold:
- its longest lying bout reached `abnormal_lying_consecutive_captures` captures in a row, and
- its final temperature is at or above `temperature_threshold_c` (36.7 °C, inclusive). The final temperature is the
  most recent valid reading, but only if it came from one of the last `final_temperature_lookback_captures` (3)
  captures; otherwise it counts as No result and the pig is not flagged.

**`abnormal_lying_consecutive_captures` is a placeholder (6 = 3 hours).** Replace it with a value your study can cite:
healthy pigs spend most of the day lying, so this number decides what "prolonged lying" means.

## SMS
One message after the evaluation, listing flagged pigs hottest first (at most 160 characters, "+N more pigs" if
needed). Failures before the text reaches the modem (no SIM, no network, no prompt) are retried
(`gsm.attempts`, `gsm.retry_delay_s`). Once the text has been handed to the modem it is never resent, to avoid
duplicates; an unconfirmed result is shown as "unconfirmed". Test sessions send a real SMS starting with [TEST]
unless `test.send_sms` is false.

## Test mode
The **Test mode** button at the top of the dashboard applies to sessions started while it is on (running sessions
are not affected). Test sessions use `prototype.json` "test" (default: every 30 s for 5 minutes = 11 captures), show a
TEST chip, and are marked TEST in records, session lists and export file names.

## Pig temperatures and calibration
Run `python3 calibrate.py` once on the Pi with a screen (HDMI or VNC), with the cameras mounted as they will stay.
Place 4–8 warm objects (mugs of hot water) across the pen, click each one on the webcam image and then on the thermal
image, and press `s`. It saves `calibration.json`; redo it if a camera moves. Without it, the two images are assumed
to cover the same view, and each sample is marked "uncalibrated thermal mapping" in the Detail column.
`thermal.offset_c` adds a fixed correction after a check against a reference thermometer.

## Pig IDs (placeholder)
`pig_ids.py` matches each capture's pigs to the previous positions. Pigs that move between captures can swap IDs or
get new ones. Only `pig_ids.assign()` changes when the ID approach is decided.

## Images
With `save_images: true` each capture's annotated `rgb.jpg` and `thermal.jpg` stay on the Pi in
`pi_data/sessions/<session>/<capture>/` (about 40 pairs per session). The record's evidence path points there
(`pi/<session>/<capture>/rgb.jpg`). The dashboard tile shows the latest pair when `preview.enabled` is true in
`config.json`.

## Clock
Reported times use the Pi clock corrected by its difference from the dashboard at Start. That correction does not
survive a Pi reboot during a session without network time. For a field run, an RTC module (DS3231) or network time
on the Pi is strongly recommended. Each session records whether its clock came from the dashboard or the Pi alone.

## Testing without the Pi (tools/test_push.py)
A dummy prototype that runs the real `inlayo8.py` code with simulated pigs, images and SMS (the SMS is printed, not
sent). Its data is kept in `tools/sim_data/`, separate from the Pi's log. Short schedules by default: every 10 s for
2 minutes (Test mode: every 5 s for 1 minute), abnormal lying = 3 captures in a row.

- Like the real Pi, over MQTT (needs `pip install paho-mqtt`):  `python3 tools/test_push.py`
  It appears under **+ > Add a pen** as `sim-xxxxxx`; Start, Start test, End and Test mode work as with the Pi.
  Add `--host <dashboard IP>` if it is not found automatically, `--password <mqtt password>` if one is set.
- Like the old test_push.py, over HTTP on the dashboard computer:  `python3 tools/test_push.py --http PEN-01`
  It starts a session at once; the dashboard buttons cannot reach it (Ctrl+C stops it).

One pig is "sick" and marked "sim: sick" in the camera image, so a session normally ends with an alert for it.
`--drop 0.2` loses some captures on purpose so you can watch the dashboard fetch them again; `--sms-fail`,
`--fail-rate`, `--pigs` and `--seed` change the scenario. `python3 tools/test_push.py --help` lists everything.
