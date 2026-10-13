# Swine Monitoring Dashboard + Prototype

A local web app that shows many pens on one CCTV-style wall. Each pen is watched by a Raspberry Pi prototype
(thermal camera, webcam, GSM modem) that sends its data over MQTT. Screening results only: **not a veterinary diagnosis.**

```
 Pi (pen 1) --\
 Pi (pen 2) ---+--> Wi-Fi router --> web app computer:  MQTT broker -> tools/mqtt_bridge.py -> dashboard (127.0.0.1:8080)
 Pi (pen 3) --/
```

The web app computer is the center. Every Pi connects to its one broker. Only the dashboard computer needs a fixed IP.

## What is in this project

| Path | What it is | Runs on |
|---|---|---|
| `server.js`, `lib/`, `public/` | The web app (Node + SQLite) | Web app computer |
| `config.json` | Web app settings (screening threshold, preview, stale time) | Web app computer |
| `tools/mqtt_bridge.py` | Forwards MQTT data to the web app | Web app computer |
| `tools/` tests | `npm test`, `node tools/preview_test.js` | Web app computer |
| `prototype/` | Headless prototype: `inlayo8.py`, workers, GSM, `prototype.json` | Each Pi |
| `deploy/` | Optional systemd files to start things at boot (Linux) | Linux machines |
| `data/swine.db` | The database. Do not commit it. | Web app computer |

## Requirements

- Web app computer: Node 22.13 or newer, Python 3 with `pip install paho-mqtt`, the Mosquitto broker.
- Each Pi: your existing hardware setup (FLIR Lepton on SPI, webcam, SIM800L on `/dev/serial0`), SPI and serial enabled in `raspi-config`, the model file `YOLOv8A_SD_best.onnx`.
- One router or Wi-Fi network that the web app computer and every Pi can join.

## 1. Set up the web app (once)

1. Reserve a fixed IP for the web app computer in your router. Below it is called `BROKER_IP`.
2. In `config.json`, turn on the camera preview and set the screening rule:
   ```json
   "preview": { "enabled": true, "refresh_ms": 1000, "max_frame_bytes": 262144, "stale_after_s": 5, "rate_limit_per_sec": 10 },
   "screening": { "surface_temp_threshold_c": 35.0, "consecutive_readings": 3, "min_lying_duration_s": 0 }
   ```
   - `surface_temp_threshold_c`: reading at or above this counts. `null` means no alerts fire.
   - `consecutive_readings`: readings in a row needed for "Threshold reached".
   - `min_lying_duration_s`: how long the pig must be lying; `0` turns this off.
3. Run `npm test` and `node tools/preview_test.js`.
4. Start it with `npm start`, then open `http://127.0.0.1:8080`. You see empty containers with a **+**.

## 2. Set up the MQTT broker (once, on the web app computer)

- **Linux / Pi OS:** `sudo ./prototype/setup_broker.sh <mqtt-password>`
- **Windows:**
  1. Install Mosquitto from mosquitto.org.
  2. In `C:\Program Files\mosquitto\mosquitto.conf` add:
     ```
     listener 1883
     allow_anonymous false
     password_file C:\Program Files\mosquitto\swine.passwd
     ```
  3. Create the user (as administrator):
     `"C:\Program Files\mosquitto\mosquitto_passwd.exe" -c -b "C:\Program Files\mosquitto\swine.passwd" swine <mqtt-password>`
  4. Restart the "Mosquitto Broker" service.
  5. Allow the port: `netsh advfirewall firewall add rule name="MQTT" dir=in action=allow protocol=TCP localport=1883`

The username is `swine`. All Pis and the bridge use the same MQTT password.

## 3. Set up each prototype Pi (repeat per pen)

1. Join the Pi to the same Wi-Fi/router as the web app computer.
2. Copy the `prototype/` folder to the Pi and put `YOLOv8A_SD_best.onnx` inside it.
3. Run `./setup_pi.sh` (installs `paho-mqtt` and `pyserial`). Install your existing packages (spidev, opencv, numpy, onnxruntime) if they are missing.
4. Edit `prototype.json`:
   | Setting | Meaning |
   |---|---|
   | `mqtt.host` | `BROKER_IP` |
   | `mqtt.password` | the MQTT password |
   | `high_temperature_threshold_c` | surface temperature that latches the High Temperature flag (default 35.0) |
   | `behavior_confirmation_s` | seconds a condition must last before it counts |
   | `reading_interval_s` | seconds between readings sent to the web app |
   | `mode`, `cycle_s` | `auto` alternates thermal and webcam every `cycle_s` seconds |
   | `gsm.recipient`, `gsm.port` | phone number for alerts, modem serial port |
   | `gsm.enabled` | `false` turns the SMS off |
5. Run `python3 inlayo8.py`. It prints, and saves to `prototype_address.txt`, an address like
   `mqtt://BROKER_IP:1883/proto-3f9a2c`. Each Pi gets its own `device_id` automatically. Do not copy one Pi's `prototype.json` to another after it has run.

## 4. Start the bridge (on the web app computer)

- Linux / Mac: `MQTT_PASS=<mqtt-password> python3 tools/mqtt_bridge.py`
- Windows PowerShell: `$env:MQTT_PASS="<mqtt-password>"; python tools/mqtt_bridge.py`

Leave it running. If you use `INGEST_TOKEN`, set that variable the same way (and for the web app). The bridge reads the pen list from the dashboard, so new pens are picked up within about 10 seconds.

## 5. Add a pen and start monitoring

1. In the dashboard press the **+** in an empty container.
2. Enter the pig/pen number (letters, digits, `-`, `_`) and paste the Pi's address. Leave the camera URL empty to use the prototype's own camera frames.
3. Press **Start** on the tile. Readings are accepted only while a session is active.

## Using the dashboard

**Pens page (CCTV wall):**
- The grid adapts to the screen: 3×2 on a 1080p desktop, 4×2 on very wide screens, 2×2 or 2×3 on tablets, 2 pens on a phone. More pens than fit are split into pages (‹ ›, or the left/right arrow keys). The ⛶ button toggles full screen.
- A tile shows the live camera image, temperature, posture, lying time, session and status. Borders: green active, dashed amber stale, pulsing red unacknowledged alert.
- Chips show whether the prototype is online, the two latched flags (High temp, Abnormal behavior), and the SMS state.
- **Cam** chooses Auto / Thermal / Webcam / Off. **Reset flags** starts a new alert cycle. **Thermal** switches the image view when a thermal frame exists. **Start / End** control the session. ✕ removes the tile (data is kept).
- The prototype sends one SMS per cycle, when both flags are 1. It is never retried, so check the phone.

**Sessions & Log page:** two separate scrolling panels.
- Sessions (top): each ended session has **CSV**, **PDF** and **Delete**. Delete also removes that session's log records. Active sessions cannot be deleted.
- Log (bottom): filter by pen, session and event. The pen filter lists only pens found in the database.

## Start automatically (Linux)

Copy `deploy/swine-*.service` to `/etc/systemd/system/`, set the password in `/etc/swine/swine.env`, and run
`sudo systemctl enable --now swine-dashboard swine-bridge` on the web app computer and `swine-prototype` on each Pi. See `deploy/INSTALL.txt`. Edit the user and folder paths first if yours differ.

## Security notes

- The web app has **no login**. It listens on `127.0.0.1` only. To open it from other devices set `HOST=0.0.0.0`, and only on a network you trust.
- Readings, camera frames and device data are accepted only from the web app computer itself (the bridge).
- The broker accepts connections from your whole network and uses one shared password. Keep it on a trusted network.
- Never commit `data/swine.db`, `prototype.json` with real passwords, or `swine.env`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Tile says **Prototype offline** | Bridge not running, MQTT password mismatch, `mqtt.host` wrong, or port 1883 blocked |
| No readings in the log | No active session for that pen (the bridge shows HTTP 409) |
| No camera image | `preview.enabled` is not true, server not restarted, or no frame yet |
| "No screening threshold is configured" | Set `screening.surface_temp_threshold_c` in `config.json`, then restart |
| Download saves `export.json` or delete shows "Not found" | An old server is still running; stop it and start the new one |
| Prototype exits at start | Another controller is running, model file missing, or camera busy; see `camera_logs/*.log` |

## Known limitations

- The prototype code was not run on a real Pi, and the broker and SMS steps were not tested on real hardware. Test the cameras, the SIM800L and the Wi-Fi connection first.
- The reading's temperature is the hottest stable thermal region, and posture is "Lying" if any tracked pig is lying. Thermal regions and pigs are not matched to each other.
- Only one camera runs at a time on a Pi, so one of the two values can be briefly missing during a switch.
- The prototype's threshold (`prototype.json`) and the dashboard's (`config.json`) are independent. Set both to the same value, or they can disagree.
- `npm test` may print "DB failure test skipped". That check was not confirmed after the pen-registry changes.
