# Swine Monitoring Dashboard + Prototype

A local web app that shows many pens on one CCTV-style wall. Each pen is watched by a Raspberry Pi prototype
(thermal camera, webcam, GSM modem) that sends its data over MQTT. Screening results only: **not a veterinary diagnosis.**

```
 Pi (pen 1) --\
 Pi (pen 2) ---+--> Wi-Fi router --> web app computer:  ONE program (npm start)
 Pi (pen 3) --/                                          ├─ built-in MQTT broker (port 1883)
                                                         └─ dashboard (http://127.0.0.1:8080)
```

The web app computer is the center. `npm start` runs the dashboard **and** the MQTT broker in one process. There is no
Mosquitto to install, no bridge script to run and no fixed IP to reserve. Each Pi finds the dashboard by itself, and you
add it to the wall by picking it from a list.

## What is in this project

| Path | What it is | Runs on |
|---|---|---|
| `server.js`, `lib/`, `public/` | The web app (Node + SQLite) and the built-in MQTT broker | Web app computer |
| `lib/mqtt_hub.js` | The built-in broker and message handling | Web app computer |
| `lib/beacon.js` | Announces the dashboard on the network so Pis can find it | Web app computer |
| `config.json` | Web app settings (screening threshold, preview, stale time, MQTT) | Web app computer |
| `tools/` tests | `npm test`, `node tools/hub_test.js`, `node tools/preview_test.js` | Web app computer |
| `tools/mqtt_bridge.py` | **Legacy.** Only needed if you turn the built-in broker off and use Mosquitto | Web app computer |
| `prototype/` | Headless prototype: `inlayo8.py`, `hub_discovery.py`, workers, GSM, `prototype.json` | Each Pi |
| `deploy/` | Optional systemd files to start things at boot (Linux) | Linux machines |
| `data/swine.db` | The database. Do not commit it. | Web app computer |

## Requirements

- **Web app computer:** Node 22.13 or newer, then one 'npm install' in the project folder (it also installs the built-in MQTT broker, aedes). Python and Mosquitto are no longer needed on this machine.
- **Each Pi:** Python 3 with `paho-mqtt` and `pyserial`, plus your existing hardware setup (FLIR Lepton on SPI, webcam,
  SIM800L on `/dev/serial0`), SPI and serial enabled in `raspi-config`, and the model file `YOLOv8A_SD_best.onnx`.
- **One router or Wi-Fi network** that the web app computer and every Pi can join. A guest network or a router with
  "client isolation" blocks automatic discovery (see Troubleshooting).

## 1. Set up the web app (once)

1. In the project folder run:
   ```
   npm install
   npm install aedes@1.2.0
   ```
   `aedes` is the built-in MQTT broker. For the optional broker test also run `npm install -D mqtt`.
2. In `config.json`, turn on the camera preview and set the screening rule:
   ```json
   "preview": { "enabled": true, "refresh_ms": 1000, "max_frame_bytes": 262144, "stale_after_s": 5, "rate_limit_per_sec": 10 },
   "screening": { "surface_temp_threshold_c": 35.0, "consecutive_readings": 3, "min_lying_duration_s": 0 }
   ```
   - `surface_temp_threshold_c`: reading at or above this counts. `null` means no alerts fire.
   - `consecutive_readings`: readings in a row needed for "Threshold reached".
   - `min_lying_duration_s`: how long the pig must be lying; `0` turns this off.
3. Run the checks:
   ```
   npm test
   node tools/hub_test.js
   node tools/preview_test.js
   ```
   `hub_test.js` ends with `ALL PASSED` when the broker works. `npm test` may print "DB failure test skipped"; that is expected.
4. Start it with `npm start`. The console should show both lines:
   ```
   Swine dashboard listening on http://127.0.0.1:8080
   MQTT broker listening on 0.0.0.0:1883 (...)
   ```
5. Open `http://127.0.0.1:8080`. You see empty containers with a **+**.
6. **Windows only:** the first time, Windows Firewall asks about Node.js. Click **Allow** on private networks. This
   opens the MQTT port (TCP 1883) for the Pis and lets the network announcement (UDP 18830) go out.

If the broker line is missing or says `NOT running`, the dashboard still works but no Pi can connect. The message says why
(for example `aedes` not installed, or port 1883 already used by an old Mosquitto service: stop that service).

## 2. Set up each prototype Pi (repeat per pen)

1. Join the Pi to the same Wi-Fi/router as the web app computer.
2. Copy the `prototype/` folder to the Pi and put `YOLOv8A_SD_best.onnx` inside it. Make sure `hub_discovery.py` is in
   the same folder as `inlayo8.py`.
3. Run `./setup_pi.sh` (installs `paho-mqtt` and `pyserial`). Install your existing packages (spidev, opencv, numpy,
   onnxruntime) if they are missing.
4. Edit `prototype.json`:
   | Setting | Meaning |
   |---|---|
   | `mqtt.host` | `"auto"` (default) finds the dashboard by itself. Or type the dashboard computer's IP to skip discovery. |
   | `mqtt.username`, `mqtt.password` | Only if you set an MQTT password on the dashboard (see Optional settings). Leave empty otherwise. |
   | `high_temperature_threshold_c` | surface temperature that latches the High Temperature flag (default 35.0) |
   | `behavior_confirmation_s` | seconds a condition must last before it counts |
   | `reading_interval_s` | seconds between readings sent to the web app |
   | `mode`, `cycle_s` | `auto` alternates thermal and webcam every `cycle_s` seconds |
   | `gsm.recipient`, `gsm.port` | phone number for alerts, modem serial port |
   | `gsm.enabled` | `false` turns the SMS off |
5. Run `python3 inlayo8.py`. It prints `Found the dashboard at <ip>:1883` and its own id (like `proto-3f9a2c`).
   Each Pi gets its own `device_id` automatically. Do not copy one Pi's `prototype.json` to another after it has run.

The dashboard may be started before or after the Pi. The Pi keeps looking until it finds it, and looks again if the
connection stays down (for example the dashboard computer got a new IP address).

## 3. Add a pen and start monitoring

1. Start the Pi first, wait a few seconds. The empty **+** tile shows **"1 new prototype found"**.
2. Press the **+**. Under "Prototypes found on your network", pick the Pi. The pen number and address fill in
   (you can change the pen number: letters, digits, `-`, `_`). Leave the camera URL empty to use the prototype's own camera frames.
3. Press **Add pen**, then **Start** on the tile. Readings are accepted only while a session is active.

If the list stays empty, you can still type the address by hand (`mqtt://<dashboard-ip>:1883/<pi id>`), as in older versions.

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

## Optional settings

All are optional. The defaults work on a trusted home or farm network.

**`mqtt` block in `config.json`:**

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` turns the built-in broker off (then use Mosquitto and `tools/mqtt_bridge.py` as in older versions) |
| `port` | `1883` | Broker port |
| `username`, `password` | `swine`, empty | Empty password = open broker. Set a password to require a login. |
| `beacon` | `true` | Announce the dashboard on the network so Pis can find it. `false` means every Pi needs `mqtt.host` set by hand. |
| `discovery_port` | `18830` | UDP port for the announcement |

**Environment variables:**

| Variable | Effect |
|---|---|
| `MQTT_PASS` | MQTT password (overrides `config.json`). Then set the same `mqtt.username` / `mqtt.password` in each Pi's `prototype.json`. |
| `MQTT_PORT` | Broker port |
| `MQTT_ENABLED=0` | Built-in broker off (useful for tests) |
| `MQTT_BEACON=0` | Network announcement off |
| `HOST`, `PORT` | Dashboard address and port (default `127.0.0.1:8080`) |
| `SWINE_CONFIG`, `SWINE_DB` | Use another config file or database file |

Windows PowerShell example: `$env:MQTT_PASS="mypassword"; npm start`
Linux / Mac: `MQTT_PASS=mypassword npm start`

## Start automatically (Linux)

Copy `deploy/swine-*.service` to `/etc/systemd/system/`, set any password in `/etc/swine/swine.env`, and run
`sudo systemctl enable --now swine-dashboard` on the web app computer and `swine-prototype` on each Pi. See `deploy/INSTALL.txt`.
Edit the user and folder paths first if yours differ. The `swine-bridge` service is no longer needed.
(These files were not changed in this version. Check that `swine-dashboard.service` loads `swine.env` if you use `MQTT_PASS`.)

## Security notes

- The dashboard web page has **no login**. It listens on `127.0.0.1` only. To open it from other devices set `HOST=0.0.0.0`, and only on a network you trust. The Pis do **not** need this: they talk to the broker, not the web page.
- The **MQTT broker listens on your whole network** (`0.0.0.0:1883`). With no password, anyone on that network can connect. Set `MQTT_PASS` if others share your Wi-Fi.
- Even without a password, each device may only publish under its own id and listen to its own command topic, and data from devices you have not added as a pen is ignored.
- With a password set, a Pi using `mqtt.host: "auto"` sends it to whichever computer answers the network announcement. Fine on a trusted network; on a shared one, type the dashboard IP in `mqtt.host` instead.
- All Pis share one MQTT password. Per-device credentials are not implemented yet.
- Never commit `data/swine.db`, `prototype.json` with real passwords, or `swine.env`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Console says `MQTT broker is NOT running: ... aedes ...` | Run `npm install aedes@1.2.0` in the project folder, then restart |
| Console says the broker could not start because the port is busy | An old Mosquitto service or another program uses port 1883. Stop it, or change `mqtt.port` |
| `Could not load ./lib/...` at startup | A file is missing from `lib/`. Copy the whole folder: `beacon.js`, `config.js`, `db.js`, `export.js`, `mqtt_hub.js` |
| Pi prints "Looking for the dashboard on the network..." forever | Dashboard not running, Windows Firewall blocked Node (allow it), Pi on a different or guest network, or router client isolation. Set `mqtt.host` to the dashboard IP |
| Pi is not in the **+** list | It is not connected yet (wait ~20 s), it is already added as a pen, or the MQTT password does not match |
| Tile says **Prototype offline** | Pi not running or not connected, MQTT password mismatch, or `mqtt.host` wrong |
| No readings in the log | No active session for that pen. Press **Start** |
| No camera image | `preview.enabled` is not true, server not restarted, or no frame yet |
| "No screening threshold is configured" | Set `screening.surface_temp_threshold_c` in `config.json`, then restart |
| Download saves `export.json` or delete shows "Not found" | An old server is still running; stop it and start the new one |
| Prototype exits at start | Another controller is running, model file missing, or camera busy; see `camera_logs/*.log` |
| `hub_test.js` fails to connect | `aedes` or `mqtt` not installed; the test prints the server's message |

## Known limitations

- The prototype code has **not been run on a real Pi**, and SMS has not been tested on real hardware. Test the cameras, the SIM800L and the Wi-Fi connection first. The broker, discovery and pen-adding steps were tested on one computer only.
- The reading's temperature is the hottest stable thermal region, and posture is "Lying" if any tracked pig is lying. Thermal regions and pigs are not matched to each other.
- Only one camera runs at a time on a Pi, so one of the two values can be briefly missing during a switch.
- The prototype's threshold (`prototype.json`) and the dashboard's (`config.json`) are still independent. Set both to the same value, or they can disagree.
- Automatic discovery needs a network that allows UDP broadcasts. The beacon only reaches the local network, not other subnets.
- The built-in broker keeps its data in memory. After a dashboard restart, Pi status reappears within about 10 seconds.
- Do not run Mosquitto, `mqtt_bridge.py` and the built-in broker together.
