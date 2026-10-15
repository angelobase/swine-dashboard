#!/usr/bin/env python3
"""Dummy prototype for testing the web app without the Raspberry Pi, cameras or SIM800L.

It runs the REAL prototype code (inlayo8.py scheduler, local log, evaluation, SMS text, replay) and replaces only
the hardware: pigs, webcam and thermal images are generated, and the SMS is printed instead of sent.
Its data is kept in tools/sim_data/, never in the Pi's pi_data/.

Two ways to connect:

  MQTT (like the real Pi; needs  pip install paho-mqtt):
      python3 tools/test_push.py
    It appears in the dashboard under "+ > Add a pen" as sim-xxxxxx. Add it, then use Start, Start test, End and the
    Test mode button exactly as with the Pi. Options: --host <dashboard IP> if auto-discovery does not find it,
    --password <mqtt password> if config.json sets one.
    Dashboard on Render:  python3 tools/test_push.py --host wss://<your-app>.onrender.com/mqtt --password <MQTT_PASS>

  HTTP (like the old test_push.py; run it on the dashboard computer, no MQTT needed):
      python3 tools/test_push.py --http PEN-01
    It starts a session on PEN-01 right away (the pen must exist on the dashboard). Start/End/Test mode on the
    dashboard cannot reach it in this mode; press Ctrl+C to stop, and End on the tile to close the session.

Short schedules so a session finishes in minutes (change with the options below):
  full session  every 10 s for 2 min (13 captures)   test session  every 5 s for 1 min (13 captures)
  "abnormal lying" = lying in 3 captures in a row; threshold 36.7 C (as in prototype.json).
One pig is "sick" (lies most of the time, 37.0-37.6 C) and is marked "sim: sick" in the simulated camera image,
so a session normally ends with an alert for it. It starts as Pig 1, but the placeholder IDs (pig_ids.py) can swap
when pigs move close together, exactly as on the real Pi; the mark shows which pig it really is.

Useful options:
  --pigs 3            number of pigs                   --fail-rate 0.05   share of captures that fail
  --drop 0.0          share of captures "lost" on the network, to watch the dashboard ask for them again
  --sms-fail          make the simulated SMS fail      --keep             resume the previous simulated session
  --interval/--duration and --test-interval/--test-duration (seconds)    --seed N for repeatable pigs
"""
import argparse
import json
import os
import random
import shutil
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
for candidate in (HERE, HERE.parent / 'prototype'):
    if (candidate / 'inlayo8.py').is_file():
        PROTO = candidate
        break
else:
    sys.exit('Cannot find inlayo8.py. Keep test_push.py in tools/ next to the prototype/ folder, or inside prototype/.')

# Each prototype file must contain its own code; a file pasted into the wrong name breaks the import.
EXPECTED = {'inlayo8.py': 'class Prototype', 'pi_store.py': 'class Store', 'screening.py': 'def evaluate(',
            'pig_ids.py': 'def assign(', 'capture_once.py': 'def annotate_rgb(', 'yolo_worker.py': 'class YoloOnnxDetector',
            'thermal_worker.py': 'class Lepton3', 'gsm_sms.py': 'def read_response(', 'preview_support.py': 'def save_preview('}
problems = []
for name, marker in EXPECTED.items():
    f = PROTO / name
    if not f.is_file():
        problems.append(f'{name}: missing')
        continue
    text = f.read_text(encoding='utf-8', errors='replace')
    if marker not in text:
        owner = next((n for n, m in EXPECTED.items() if n != name and m in text), None)
        problems.append(f'{name}: wrong content ({f.stat().st_size} bytes)' + (f'; it contains the code of {owner}' if owner else ''))
if problems:
    sys.exit(f'Some files in {PROTO} are missing or have the wrong content:\n  ' + '\n  '.join(problems) +
             '\nExtract the update zip and copy its whole prototype folder over yours (yolo_worker.py, thermal_worker.py,'
             '\ngsm_sms.py and preview_support.py are your own files from the Pi).')
sys.path.insert(0, str(PROTO))
import capture_once as co  # noqa: E402  (image drawing and JPEG writing; no camera is opened)
import inlayo8  # noqa: E402
import pig_ids  # noqa: E402

if not hasattr(inlayo8, 'Prototype') or not hasattr(inlayo8, 'send_with_retries'):
    found = Path(getattr(inlayo8, '__file__', None) or '(not a file)')
    size = found.stat().st_size if found.is_file() else 0
    sys.exit(f'The inlayo8.py that was loaded is not the scheduled-session version:\n  {found}  ({size} bytes)\n'
             f'Copy prototype/inlayo8.py from the update into {PROTO}, then run this again.')

FRAME_W, FRAME_H = 640, 480


# ---------------------------------------------------------------- simulated pen
class Pen:
    """A few pigs that lie down, get up and move a little between captures. Pig 1 is 'sick'."""

    def __init__(self, count, rng):
        self.rng = rng
        self.pigs = []
        cols = max(1, min(count, 3))
        for i in range(count):  # start spread out over the floor
            x = 120 + (i % cols) * (FRAME_W - 240) / max(cols - 1, 1) + rng.uniform(-20, 20)
            y = 190 + (i // cols) * 150 + rng.uniform(-20, 20)
            self.pigs.append({'sick': i == 0, 'x': x, 'y': min(y, FRAME_H - 70), 'lying': i == 0,
                              'temp': 37.2 if i == 0 else rng.uniform(35.0, 36.0)})

    def step(self):
        r = self.rng
        for p in self.pigs:
            if p['sick']:
                p['lying'] = r.random() < 0.9
                p['temp'] = r.uniform(37.0, 37.6)
            else:
                if r.random() < 0.35:
                    p['lying'] = not p['lying']
                p['temp'] = r.uniform(35.0, 36.3)
            move = 6 if p['lying'] else (60 if r.random() < 0.5 else 8)
            p['x'] = min(max(p['x'] + r.uniform(-move, move), 100), FRAME_W - 100)
            p['y'] = min(max(p['y'] + r.uniform(-move, move), 140), FRAME_H - 60)

    def box(self, p):
        w, h = (190, 95) if p['lying'] else (140, 80)
        return [p['x'] - w / 2, p['y'] - h / 2, p['x'] + w / 2, p['y'] + h / 2]


def draw_scene(boxes, sick_box=None):
    img = np.zeros((FRAME_H, FRAME_W, 3), np.uint8)
    img[:] = (78, 92, 104)
    cv2.rectangle(img, (0, 120), (FRAME_W, FRAME_H), (64, 74, 84), -1)       # pen floor
    for x in range(0, FRAME_W, 80):
        cv2.line(img, (x, 0), (x, 120), (60, 70, 80), 3)                      # fence
    for b in boxes:
        c = (int((b[0] + b[2]) / 2), int((b[1] + b[3]) / 2))
        cv2.ellipse(img, c, (int((b[2] - b[0]) / 2), int((b[3] - b[1]) / 2)), 0, 0, 360, (165, 175, 222), -1)
    if sick_box is not None:  # simulator-only marker, so ID swaps are visible
        co.text(img, 'sim: sick', (int(sick_box[0]) + 4, int(sick_box[3]) + 16), 0.45, (120, 200, 255))
    return img


def draw_thermal(pigs, caption):
    celsius = np.full((120, 160), 24.0, np.float32) + np.random.normal(0, 0.3, (120, 160)).astype(np.float32)
    for p in pigs:
        if p['temp_c'] is not None:
            x1, y1, x2, y2 = (int(v / 4) for v in p['box'])
            cv2.ellipse(celsius, ((x1 + x2) // 2, (y1 + y2) // 2), ((x2 - x1) // 2, (y2 - y1) // 2), 0, 0, 360, p['temp_c'], -1)
    gray = np.clip((celsius - 22.0) * 255.0 / 17.0, 0, 255).astype(np.uint8)
    img = cv2.resize(cv2.applyColorMap(gray, cv2.COLORMAP_INFERNO), (480, 360), interpolation=cv2.INTER_NEAREST)
    for p in pigs:
        x1, y1, x2, y2 = (int(v * 0.75) for v in p['box'])
        cv2.rectangle(img, (x1, y1), (x2, y2), (255, 255, 255), 1)
        co.text(img, f"Pig {p['id']}: {'--' if p['temp_c'] is None else format(p['temp_c'], '.1f')} C", (x1, max(14, y1 - 4)), 0.42)
    co.text(img, caption + '  (SIMULATED)', (8, 352), 0.45)
    return img


def make_capture(pen, args, rng):
    """Same contract as capture_once.py: returns the capture dict and writes rgb.jpg / thermal.jpg into `out`."""
    def capture(out, previous, caption):
        out.mkdir(parents=True, exist_ok=True)
        time.sleep(min(1.0, args.interval / 4))  # a real capture takes several seconds
        if rng.random() < args.fail_rate:
            return {'ok': False, 'error': 'Simulated capture failure (webcam returned no frames).', 'pigs': []}
        first = not previous['pigs']
        pen.step()
        seen = [p for p in pen.pigs if first or rng.random() >= 0.08]              # occasional missed detection
        boxes = [pen.box(p) for p in seen]
        ids, next_id = pig_ids.assign(previous['pigs'], boxes, previous['next_id'], (FRAME_W, FRAME_H))  # same placeholder IDs as the Pi
        pigs = []
        for p, box, pid in zip(seen, boxes, ids):
            temp = None if rng.random() < 0.05 else round(p['temp'], 2)             # occasional "No result"
            pigs.append({'id': pid, 'box': box, 'lying': p['lying'], 'class_name': 'laying' if p['lying'] else 'standing',
                         'confidence': round(rng.uniform(0.93, 0.99), 3), 'frames_seen': 5, 'temp_c': temp, 'thermal_rect': None})
        pigs.sort(key=lambda p: p['id'])
        sick = next((pen.box(p) for p in seen if p['sick']), None)
        images = {'rgb': co.write_jpeg(out / 'rgb.jpg', co.annotate_rgb(draw_scene(boxes, sick), pigs, caption + '  (SIMULATED)')),
                  'thermal': co.write_jpeg(out / 'thermal.jpg', draw_thermal(pigs, caption))}
        return {'ok': True, 'pigs': pigs, 'next_id': next_id, 'thermal_valid': True, 'thermal_error': None,
                'calibrated': True, 'frame_size': [FRAME_W, FRAME_H], 'images': {k: v for k, v in images.items() if v}}
    return capture


def make_sms(args):
    def send(message, gsm, stage):
        print('\n--- SIMULATED SMS (not sent) to ' + str(gsm.get('recipient')) + ' ---\n' + message + '\n---', flush=True)
        time.sleep(2)
        if args.sms_fail:
            raise RuntimeError('Simulated: not registered on the network')
        stage['body_sent'] = True
    return send


# ---------------------------------------------------------------- HTTP transport (no MQTT)
class HttpClient:
    """Stands in for the MQTT client: posts each message to the dashboard's loopback routes."""

    def __init__(self, base, pen):
        self.base, self.pen = base.rstrip('/'), pen
        self.token = os.environ.get('INGEST_TOKEN', '')
        self.warned = set()

    def _post(self, path, data, ctype):
        req = urllib.request.Request(self.base + path, data=data, method='POST')
        req.add_header('Content-Type', ctype)
        if self.token:
            req.add_header('X-Ingest-Token', self.token)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            return 0, str(e).encode()

    def publish(self, topic, payload, qos=0, retain=False):
        suffix = topic.split('/', 2)[2]
        if suffix.startswith('preview/'):
            code, body = self._post(f'/api/preview/{self.pen}/{suffix.split("/")[1]}', payload, 'image/jpeg')
        else:
            data = {'state': {'online': payload == 'online'}} if suffix == 'status' else {suffix: json.loads(payload)}
            code, body = self._post(f'/api/device/{self.pen}', json.dumps(data).encode(), 'application/json')
        if code != 200 and (suffix, code) not in self.warned:
            self.warned.add((suffix, code))
            hint = ' (is preview.enabled true in config.json?)' if suffix.startswith('preview/') and code == 404 else ''
            print(f'[http] {suffix} -> {code or "no connection"}: {body.decode(errors="replace")[:200]}{hint}', flush=True)

    def commands(self):
        """Commands the dashboard queued for this pen (only when its MQTT broker is off)."""
        try:
            req = urllib.request.Request(f'{self.base}/api/device/{self.pen}/commands')
            if self.token:
                req.add_header('X-Ingest-Token', self.token)
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read()).get('commands') or []
        except (urllib.error.URLError, OSError, ValueError):
            return []

    def loop_stop(self):
        pass


class SimPrototype(inlayo8.Prototype):
    drop = 0.0

    def pub(self, suffix, payload, qos=0, retain=False):
        if suffix == 'sample' and self.drop and random.random() < self.drop:
            print(f"[sim] capture {json.loads(payload)['seq'] + 1} 'lost' on the network; the dashboard should ask for it again.", flush=True)
            return
        super().pub(suffix, payload, qos, retain)


def build_config(args):
    cfg = json.loads(json.dumps(inlayo8.DEFAULTS))
    user_file = PROTO / 'prototype.json'
    if user_file.is_file():  # read-only: the real prototype.json is never written
        user = json.loads(user_file.read_text(encoding='utf-8'))
        cfg['mqtt'].update(user.get('mqtt') or {})
        cfg['screening'].update(user.get('screening') or {})
        cfg['gsm'].update({k: v for k, v in (user.get('gsm') or {}).items() if k in ('recipient',)})
    cfg['device_id'] = args.device or 'sim-' + format(uuid.getnode() & 0xFFFFFF, '06x')
    if args.host:
        cfg['mqtt']['host'] = args.host
    if args.password is not None:
        cfg['mqtt']['password'] = args.password
    cfg['session'] = {'interval_s': args.interval, 'duration_s': args.duration}
    cfg['test'] = {'interval_s': args.test_interval, 'duration_s': args.test_duration, 'send_sms': True}
    cfg['screening']['abnormal_lying_consecutive_captures'] = args.rule
    cfg['gsm'].update(enabled=True, attempts=2, retry_delay_s=3)
    cfg['gsm']['recipient'] = cfg['gsm'].get('recipient') or '(simulated phone)'  # nothing is sent
    cfg['save_images'] = True
    return cfg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--http', metavar='PEN', help='post to the dashboard over HTTP as this pen instead of using MQTT')
    ap.add_argument('--dashboard', default='http://127.0.0.1:8080', help='dashboard address for --http')
    ap.add_argument('--host', help='dashboard for MQTT: an IP address, or wss://<app>.onrender.com/mqtt (default: find it on the local network)')
    ap.add_argument('--password', help='MQTT password (default: from prototype.json)')
    ap.add_argument('--device', help='device id (default: sim-<from this computer>)')
    ap.add_argument('--start', action='store_true', help='MQTT mode: start a full session now')
    ap.add_argument('--test', action='store_true', help='start a test session (HTTP mode starts a full one otherwise)')
    ap.add_argument('--pigs', type=int, default=3)
    ap.add_argument('--interval', type=float, default=10)
    ap.add_argument('--duration', type=float, default=120)
    ap.add_argument('--test-interval', type=float, default=5)
    ap.add_argument('--test-duration', type=float, default=60)
    ap.add_argument('--rule', type=int, default=3, help='lying captures in a row that count as abnormal')
    ap.add_argument('--fail-rate', type=float, default=0.05)
    ap.add_argument('--drop', type=float, default=0.0)
    ap.add_argument('--sms-fail', action='store_true')
    ap.add_argument('--keep', action='store_true', help='keep tools/sim_data and resume an unfinished simulated session')
    ap.add_argument('--seed', type=int)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed if args.seed is not None else None)
    data_dir = HERE / 'sim_data'
    if not args.keep:
        shutil.rmtree(data_dir, ignore_errors=True)
    cfg = build_config(args)
    proto = SimPrototype(cfg, Path('simulated.onnx'), PROTO / 'prototype.json', data_dir=data_dir,
                         capture_fn=make_capture(Pen(args.pigs, rng), args, rng), sms_send=make_sms(args))
    proto.drop = args.drop
    print('=' * 60, flush=True)
    if args.http:
        proto.base = f'swine/{cfg["device_id"]}'
        proto.client = HttpClient(args.dashboard, args.http)
        proto.client.publish(f'{proto.base}/status', 'online')
        if not proto.running():
            proto.start_session(pen=args.http, test=args.test)

        def poll():
            while not proto.closing:
                for cmd in proto.client.commands():
                    proto.cmds.put(cmd)
                time.sleep(1)
        threading.Thread(target=poll, daemon=True).start()
        print(f'Simulating {args.http} over HTTP at {args.dashboard}. Ctrl+C to stop.', flush=True)
    else:
        try:
            import paho.mqtt.client  # noqa: F401
        except ImportError:
            sys.exit('MQTT mode needs paho-mqtt:  pip install paho-mqtt   (or use --http PEN-01)')
        if str(cfg['mqtt']['host']).lower() in ('', 'auto') and not (PROTO / 'hub_discovery.py').is_file():
            sys.exit('hub_discovery.py was not found next to inlayo8.py. Pass the dashboard address: --host <IP>')
        proto.connect()
        print(f"Simulated prototype id: {cfg['device_id']}", flush=True)
        print('In the dashboard: + > Add a pen, pick it from the list, then press Start (or turn on Test mode first).', flush=True)
        if args.start or args.test:
            proto.start_session(test=args.test)
    s = cfg['test'] if args.test else cfg['session']
    print(f"Schedule: every {s['interval_s']:g} s for {s['duration_s']:g} s. The 'sick' pig is marked in the camera image. Data: {data_dir}", flush=True)
    print('=' * 60, flush=True)
    signal.signal(signal.SIGINT, lambda *_: setattr(proto, 'closing', True))
    signal.signal(signal.SIGTERM, lambda *_: setattr(proto, 'closing', True))
    try:
        proto.run()
    finally:
        proto.closing = False
        proto.shutdown()


if __name__ == '__main__':
    main()
