# Installation, testing and deployment

The system has two parts:

- **Prototype (Raspberry Pi 4B):** `prototype/inlayo8.py` runs the 19-hour session, takes a capture every 30 minutes, evaluates each pig and sends the SMS.
- **Web app (dashboard computer):** `server.js` shows and records the data. It also runs the MQTT broker that the Pi connects to.

Both must be on the same network. Screening only, not a diagnosis.

---

## 1. Raspberry Pi (prototype)

### 1.1 Requirements

- Raspberry Pi 4B with **64-bit** Raspberry Pi OS (onnxruntime is not available for 32-bit).
- FLIR Lepton 3.1R on SPI0 CE0, USB webcam on `/dev/video0`, SIM800L on the serial pins (`/dev/serial0`).
- Python 3 (included with Raspberry Pi OS).

### 1.2 Enable the hardware interfaces (once)

```bash
sudo raspi-config
#   Interface Options > SPI          > Yes
#   Interface Options > Serial Port  > login shell: No, serial hardware: Yes
sudo usermod -aG spi,dialout,video $USER
sudo reboot
```

### 1.3 Install the Python libraries (once)

Raspberry Pi OS blocks system-wide `pip install`. Install the system packages with `apt`, then onnxruntime into a virtual environment that can also see them:

```bash
sudo apt update
sudo apt install -y python3-opencv python3-numpy python3-spidev python3-serial python3-paho-mqtt
cd ~/swine/prototype                                # wherever the prototype folder is
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install onnxruntime
```

Run `source .venv/bin/activate` in every new terminal before the commands below. To check that everything imports:

```bash
python3 -c "import cv2, numpy, spidev, serial, paho.mqtt.client, onnxruntime; print('all libraries OK')"
```

| Library | Used for |
|---|---|
| opencv, numpy | webcam frames, images, thermal processing |
| onnxruntime | the YOLOv8A-SD model |
| spidev | reading the FLIR Lepton |
| pyserial (`serial`) | the SIM800L GSM module |
| paho-mqtt | sending data to the dashboard |

### 1.4 Files in the `prototype` folder

`inlayo8.py`, `capture_once.py`, `screening.py`, `pig_ids.py`, `pi_store.py`, `calibrate.py`, `yolo_worker.py`, `thermal_worker.py`, `gsm_sms.py`, `preview_support.py`, `hub_discovery.py`, `prototype.json`, the `tests/` folder and the model **`YOLOv8A_SD_best.onnx`**.

### 1.5 Configure `prototype.json` (once)

If `prototype.json` does not exist yet (fresh clone from GitHub), create it from the example first:

```bash
cp prototype.example.json prototype.json
```

| Setting | Set it to |
|---|---|
| `mqtt.host` | `auto` (find the dashboard on the local network), or `wss://<your-app>.onrender.com/mqtt` for a dashboard on Render (section 5) |
| `mqtt.password` | the same password as `mqtt.password` in the dashboard's `config.json` (on Render: the `MQTT_PASS` variable) |
| `gsm.recipient` | the phone number that receives the alert, e.g. `+639...` |
| `screening.abnormal_lying_consecutive_captures` | the number of captures your study cites (**6 is a placeholder**) |
| `screening.temperature_threshold_c` | `36.7` |
| `device_id` | leave empty the first time; it is created automatically. Keep it afterwards. |

`session` (1800 s interval, 68400 s = 19 h) and `test` (30 s for 300 s) are the real and test schedules.

### 1.6 Calibrate the cameras (once, and again if a camera moves)

This needs a screen on the Pi (HDMI or VNC), and `inlayo8.py` must not be running.

```bash
python3 calibrate.py
```

Place 4 to 8 warm objects (mugs of hot water) across the pen. Click each object on the webcam image (left), then on the thermal image (right), and press `s`. This creates `calibration.json`.

### 1.7 Testing on the Pi

Run these in order. Each step checks more of the real hardware.

**a) Automated tests** (no hardware needed; takes a few seconds):

```bash
python3 -m unittest discover -s tests -v
```

Expected result: `OK`.

**b) One real capture** (cameras and model, no dashboard needed):

```bash
time python3 capture_once.py --out /tmp/cap1
```

Then open `/tmp/cap1/rgb.jpg` and `/tmp/cap1/thermal.jpg`, and read `/tmp/cap1/capture.json`. It should say `"ok": true`, `"thermal_valid": true`, and list the detected pigs with temperatures. The `time` result shows how long a capture takes. It must be well under `capture.timeout_s` (120 s) and under the test interval (30 s). If it is longer, raise `test.interval_s` in `prototype.json`.

**c) A short test session** (everything, including a real SMS starting with `[TEST]`):

From the dashboard, turn on **Test mode** and press **Start test** on the pen (see 2.5). Or, without the dashboard:

```bash
python3 inlayo8.py --test
```

It takes 11 captures over 5 minutes, evaluates the pigs and sends the SMS if a pig is flagged. Check the phone. Captures are logged in `camera_logs/capture.log`, and SMS attempts in `camera_logs/sms_events.jsonl`.

Note: `python3 gsm_sms.py` on its own does not send anything (the file only contains functions). Test the SMS with a test session.

### 1.8 Deployment on the Pi (the 19-hour run)

A 19-hour run must survive a closed SSH window. Start it inside `tmux`:

```bash
sudo apt install -y tmux                      # once
tmux new -s swine
cd ~/swine/prototype && source .venv/bin/activate
python3 inlayo8.py
```

To leave it running, press `Ctrl+B`, then `D`. To come back later: `tmux attach -t swine`.

Then press **Start** on the pen in the dashboard, with Test mode **off**. If the dashboard is not available, start the session from the Pi instead:

```bash
python3 inlayo8.py --start
```

If the Pi restarts or loses power, run `python3 inlayo8.py` again. The session resumes from `pi_data/swine_pi.db`; captures missed while it was off are recorded as missed.

| Command | What it does |
|---|---|
| `python3 inlayo8.py` | ready; the session is started from the dashboard |
| `python3 inlayo8.py --start` | start a full 19-hour session now |
| `python3 inlayo8.py --test` | start a short test session now |
| `python3 inlayo8.py --abandon` | close an unfinished session with no evaluation and no SMS |
| `Ctrl+C` | stop the program (a running session resumes on the next start) |

**Clock:** reported times use the Pi clock, corrected by the dashboard's clock at Start. Check `timedatectl` before a run. For field runs without internet, a DS3231 RTC module is strongly recommended.

---

## 2. Web app (dashboard computer)

### 2.1 Requirements

- **Node.js 22.13 or newer** (the built-in SQLite needs it): <https://nodejs.org>. Check with `node -v`.
- Optional, only for the dummy prototype `tools/test_push.py`: Python 3 with numpy, opencv-python and paho-mqtt.

### 2.2 Install (once)

From the web app folder (the one with `server.js` and `package.json`):

```bash
npm install
```

If the server later reports that `aedes` is not installed (the built-in MQTT broker), install it:

```bash
npm install aedes@1.2.0
```

Optional, for the dummy prototype:

```bash
pip install numpy opencv-python paho-mqtt       # Windows: py -m pip install numpy opencv-python paho-mqtt
```

### 2.3 Configure `config.json` (once)

If `config.json` does not exist yet (fresh clone from GitHub), create it from the example: `cp config.example.json config.json` (Windows: `copy config.example.json config.json`). Without it the dashboard starts with default settings.

Minimal example:

```json
{
  "pens": [],
  "preview": { "enabled": true },
  "mqtt": { "password": "same-as-prototype.json" }
}
```

- `preview.enabled: true` shows each capture's images on the pen tiles.
- `mqtt.password` must match `prototype.json`. An empty password leaves the broker open, so use that only on a trusted network.
- Pens with a prototype are added from the dashboard (**+ > Add a pen**), not in `config.json`.

**Firewall:** allow Node.js through the firewall (Windows asks the first time). The Pi connects to this computer on TCP port **1883**, and finds it automatically through a UDP broadcast on port **18830**.

### 2.4 Testing the web app

These tests use a temporary database and never touch `data/swine.db`.

```bash
node tools/schedule_test.js        # scheduled sessions: expected "63 passed, 0 failed"
npm test                           # the existing smoke tests
node tools/preview_test.js         # camera image routes
```

**Dummy prototype** (see the web app working without the Pi):

```bash
python3 tools/test_push.py                    # acts like a real Pi over MQTT; add "sim-xxxxxx" as a pen, then Start
python3 tools/test_push.py --http PEN-01      # no MQTT; posts straight to the dashboard as PEN-01
python3 tools/test_push.py --help             # all options
```

On Windows, use `py` instead of `python3`. Sessions are short (every 10 s for 2 minutes). One pig is "sick", so a session usually ends with an alert. The SMS is printed, not sent.

### 2.5 Deployment (the 19-hour run)

**Back up `data/swine.db` before the first start with this update.** The database is upgraded in place.

Viewed only on this computer:

```bash
npm start
```

Viewed from other devices on the network (phone, tablet):

```bash
HOST=0.0.0.0 npm start                         # Linux / macOS
$env:HOST="0.0.0.0"; npm start                 # Windows PowerShell
set HOST=0.0.0.0 && npm start                  # Windows Command Prompt
```

Open <http://localhost:8080> (or `http://<computer-IP>:8080` from another device). The dashboard has no login, so only use `HOST=0.0.0.0` on a network you trust.

**First connection:**

1. Start the dashboard, then start `python3 inlayo8.py` on the Pi. The Pi prints `Found the dashboard at ...`.
2. In the dashboard, open **+ > Add a pen**, pick the prototype from the list and give it a pen number. Each prototype can be added to **one** pen only.

**A session:**

1. Check that the **Test mode** button at the top reads **off**. Test mode on = short test session marked TEST.
2. Press **Start** on the pen. The tile shows "Waiting for prototype", then "Next capture in ...".
3. The dashboard can be closed or restarted during the run. The Pi keeps its own log, and the dashboard fetches missed captures when it reconnects.
4. After 19 hours the tile shows the result. If a pig was flagged, an alert appears; press **Acknowledge** once handled.
5. **Records** shows every capture per pig. Export the session as CSV or PDF from the sessions list.

Pressing **End** early ends the session on the Pi. The pigs are evaluated, but no SMS is sent.

---

## 3. Before a field run (checklist)

1. Run `python3 -m unittest discover -s tests` on the Pi and `node tools/schedule_test.js` on the dashboard computer. Both pass.
2. `calibration.json` exists, created with the cameras in their final position.
3. `prototype.json`: correct phone number, the study's lying-rule value, and the same MQTT password as `config.json`.
4. One complete Test mode session, with the `[TEST]` SMS received on the phone.
5. `data/swine.db` backed up; Pi clock correct (`timedatectl`).
6. `inlayo8.py` running inside `tmux`; Test mode **off**; **Start** pressed.

---

## 4. Deploying with GitHub

### 4.1 What GitHub is used for

GitHub stores the code. Each computer gets the code from GitHub and runs it **locally**: the dashboard computer runs the web app, and the Pi runs the prototype.

The web app cannot be hosted on GitHub Pages. (For a cloud host such as Render, see section 5.) In general:

- **GitHub Pages only serves static pages.** The web app needs a running Node.js server and a database file.
- **The Pi finds the dashboard by a broadcast on the local network** and connects on TCP port 1883. Neither works across the internet, and the farm network may have no internet at all.
- **The dashboard has no login**, so putting it online would expose the study data.

### 4.2 Before the first push

- **Make the repository private.** It contains your code and model and is linked to your study.
- **Never commit secrets or data.** The included `.gitignore` keeps these out of git: `config.json` and `prototype/prototype.json` (MQTT password and phone number), `data/` (the database), `prototype/pi_data/` (the Pi's log and images), `calibration.json`, and `node_modules/`. The repository gets `config.example.json` and `prototype/prototype.example.json` instead.
- **Remove the phone number from `gsm_sms.py`.** `RECIPIENT = "+639083082262"` is written in that file, which is committed. Replace it with `""`; the prototype takes the number from `prototype.json`.
- **Check the model size.** GitHub refuses files over 100 MB. If `YOLOv8A_SD_best.onnx` is larger, use Git LFS, or keep it out of git (see the last line of `.gitignore`) and copy it to the Pi by hand.
- **Check `package.json`.** It should list `aedes` under `dependencies`. If not, run `npm install aedes@1.2.0` once, which adds it. Commit both `package.json` and `package-lock.json`.

### 4.3 Upload the project (once, on the computer that has the project)

Create an empty **private** repository on github.com (no README), then in the project folder:

```bash
git init
git add .
git status                 # config.json, prototype/prototype.json and data/ must NOT be listed
git commit -m "Scheduled screening sessions"
git branch -M main
git remote add origin https://github.com/<your-account>/<repository>.git
git push -u origin main
```

When git asks for a password, use a GitHub **personal access token** (github.com > Settings > Developer settings > Personal access tokens), not your account password.

### 4.4 Install the web app from GitHub (dashboard computer)

```bash
git clone https://github.com/<your-account>/<repository>.git swine
cd swine
npm ci                           # or: npm install (if there is no package-lock.json)
cp config.example.json config.json   # Windows: copy config.example.json config.json
```

Edit `config.json` (section 2.3), then start it as in 2.5.

### 4.5 Install the prototype from GitHub (Raspberry Pi)

```bash
sudo apt install -y git
git clone https://github.com/<your-account>/<repository>.git ~/swine
cd ~/swine/prototype
cp prototype.example.json prototype.json
```

Then follow sections 1.2 to 1.8: libraries, `prototype.json`, calibration, tests and the run. If the model is not in the repository, copy `YOLOv8A_SD_best.onnx` into `~/swine/prototype/` (for example with a USB stick, or `scp` from another computer).

### 4.6 Updating after a change

Push changes from the computer where you edit the code (`git add .`, `git commit -m "..."`, `git push`). Then on each computer:

```bash
git pull
npm ci                     # dashboard computer, only if package.json changed
```

Restart the dashboard, or `inlayo8.py` on the Pi. **Do not update the Pi during a 19-hour run.** Wait until the session has finished.

Your local files (`config.json`, `prototype.json`, `calibration.json`, the databases) are not touched by `git pull`.

### 4.7 Automatic tests on GitHub

`.github/workflows/tests.yml` runs `node tools/schedule_test.js` and the prototype tests on GitHub after every push. No hardware or secrets are needed. Results appear in the repository's **Actions** tab; a red mark means a change broke something, so do not deploy that version.

---

## 5. Hosting the web app on Render

On Render, the Pi connects with **MQTT over secure WebSockets** (`wss://<your-app>.onrender.com/mqtt`), because Render only forwards its HTTPS port. The dashboard asks for a login, because it is reachable from the internet.

### 5.1 Before deploying (on your computer)

```bash
npm install ws@8                 # MQTT over WebSockets; adds it to package.json
npm install aedes@1.2.0          # only if package.json does not list aedes yet
node tools/ws_test.js            # expected: "21 passed, 0 failed"
```

Commit `package.json` and `package-lock.json`, and push to GitHub.

### 5.2 Render settings

Create a **Web Service** from your GitHub repository.

| Setting | Value |
|---|---|
| Build command | `npm ci` (or `npm install` without a `package-lock.json`) |
| Start command | `npm start` |
| Instance type | a paid instance: the free type has no persistent disk and sleeps after 15 minutes |
| Disk | mount path `/var/data`, 1 GB is plenty |
| Health check path | `/healthz` |

**Environment variables:**

| Variable | Value |
|---|---|
| `HOST` | `0.0.0.0` |
| `NODE_VERSION` | `22.13.0` or newer (or set `engines` in `package.json`) |
| `SWINE_DB` | `/var/data/swine.db` (the database on the disk; without it, data is lost on every restart) |
| `MQTT_PASS` | a long random password. Required: without it MQTT over WebSockets stays off |
| `MQTT_TCP` | `0` (port 1883 is not reachable on Render) |
| `MQTT_BEACON` | `0` (local-network discovery does not work on Render) |
| `DASHBOARD_USER` | the login name for the dashboard, e.g. `admin` |
| `DASHBOARD_PASSWORD` | a strong password. Without it, anyone with the link can see and change the data |
| `SWINE_CONFIG_JSON` | `{"pens":[],"preview":{"enabled":true}}` (replaces `config.json`, which is not on Render) |

After deploying, the log should show `Login required (user "admin")` and `MQTT broker: TCP off; WebSockets on at path /mqtt`. Open `https://<your-app>.onrender.com`; the browser asks for the login.

### 5.3 Connect the Pi

In `prototype/prototype.json` on the Pi:

```json
"mqtt": { "host": "wss://<your-app>.onrender.com/mqtt", "port": 1883, "username": "swine", "password": "<same as MQTT_PASS>" }
```

(`port` is ignored for `wss://` addresses, which use 443.) The Pi needs internet access. Run `python3 inlayo8.py`, then in the dashboard use **+ > Add a pen** and pick the prototype from the list.

### 5.4 Test with the simulator (no Pi)

```bash
python tools/test_push.py --host wss://<your-app>.onrender.com/mqtt --password <MQTT_PASS>
```

It needs `paho-mqtt`. `--http` mode does not work with Render: it only reaches a dashboard on the same computer.

### 5.5 Good to know

- Render closes WebSocket connections when it restarts or redeploys the service. The Pi reconnects by itself and the dashboard fetches any captures it missed, so redeploying during a session loses no data. Avoid it anyway during a 19-hour run.
- Keep the service at **one instance**: the MQTT broker runs inside it (a disk already limits it to one).
- The Pi still sends the SMS itself, even if Render or the farm's internet is down.
