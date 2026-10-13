#!/usr/bin/env python3
"""Headless pig-pen prototype: thermal + webcam workers, latched flags, SMS, and an MQTT data stream.

No window is opened. The prototype publishes to its own MQTT broker (see setup_network.sh) and the
dashboard host's tools/mqtt_bridge.py forwards the data to the web application.

Flags are session-level recorded events, NOT matched per-pig diagnoses. Thermal regions and webcam
pigs are not matched to each other, so the reading's temperature is the hottest stable thermal region.
"""
import argparse
import fcntl
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULTS = {
    "device_id": "", "advertise_host": "",
    "mqtt": {"host": "127.0.0.1", "port": 1883, "username": "", "password": ""},
    "high_temperature_threshold_c": 35.0, "behavior_confirmation_s": 1.0,
    "reading_interval_s": 5, "mode": "auto", "cycle_s": 20,
    "gsm": {"enabled": True, "recipient": "+639083082262", "port": "/dev/serial0"},
}
MAX_FRAME_BYTES = 250_000  # the dashboard rejects frames over preview.max_frame_bytes (default 262144)


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    if path.is_file():
        user = json.loads(path.read_text(encoding='utf-8'))
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    if not cfg['device_id']:  # generate once, keep it forever so the dashboard address stays valid
        cfg['device_id'] = 'proto-' + format(uuid.getnode() & 0xFFFFFF, '06x')
        try:
            path.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
        except OSError:
            pass
    return cfg


def own_ip(cfg):
    if cfg['advertise_host']:
        return cfg['advertise_host']
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('10.255.255.255', 1))  # no packet is sent; selects the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'


def read_status(path):
    try:
        with path.open('r', encoding='utf-8') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None  # never erase a latched flag on a transient read failure


def should_send(flags, attempted):
    return flags == [1, 1] and not attempted


def build_alert(timestamp):
    return ("ALERT - recorded events\nHigh temperature: 1\nAbnormal behavior: 1\n"
            f"Same pig not verified\nTime: {timestamp}")


def sms_job(message, result_queue, log_path, gsm):
    try:
        import gsm_sms as module  # needs pyserial; imported only when an SMS is actually sent
        module.validate_message(message)
        with module.serial.Serial(gsm['port'], baudrate=115200, timeout=0.2, write_timeout=3, exclusive=True) as modem:
            module.connect(modem)
            module.command(modem, 'AT+CMEE=2')
            reply = module.command(modem, 'AT+CPIN?')
            if '+CPIN: READY' not in reply:
                raise RuntimeError('SIM not ready: ' + reply.strip())
            module.wait_for_network(modem)
            module.send_sms(modem, message, gsm['recipient'])
        state, result = 'submitted', 'SMS submitted. Verify receipt on the phone.'
    except Exception as error:
        state, result = 'failed', 'SMS failed or unconfirmed: ' + str(error)
    try:
        with log_path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'time': datetime.now().isoformat(), 'message': message, 'result': result}) + '\n')
    except OSError:
        result += ' (Could not write SMS log.)'
    result_queue.put((state, result))


class Prototype:
    def __init__(self, cfg, model):
        self.cfg, self.model = cfg, model
        self.device = cfg['device_id']
        self.base = f'swine/{self.device}'
        self.log_dir = HERE / 'camera_logs'; self.log_dir.mkdir(exist_ok=True)
        self.preview_dir = self.log_dir / 'preview'; self.preview_dir.mkdir(exist_ok=True)
        self.metrics = self.log_dir / f'latched_metrics_{os.getpid()}_{time.time_ns()}.json'
        self.metrics.write_text('{}', encoding='utf-8')
        self.env = os.environ.copy()
        self.env.update(PIG_STATUS_PATH=str(self.metrics), PIG_MODEL_PATH=str(model), PIG_YOLO_THREADS='1',
                        PIG_PREVIEW_DIR=str(self.preview_dir), PIG_HIGH_TEMP_C=str(cfg['high_temperature_threshold_c']),
                        PIG_CONFIRM_SEC=str(cfg['behavior_confirmation_s']))
        self.proc = self.log = self.kind = None
        self.mode = cfg['mode'] if cfg['mode'] in ('auto', 'manual') else 'auto'
        self.next_switch = 0.0
        self.flags = [0, 0]
        self.attempted = False
        self.sms = {'enabled': bool(cfg['gsm']['enabled']), 'state': 'waiting', 'detail': 'SMS waits for BOTH flags = 1.', 'time': None}
        self.sms_thread = None
        self.results = queue.Queue()
        self.cmds = queue.Queue()
        self.status = 'No camera running'
        self.closing = False
        self.preview_sent = {'rgb': 0.0, 'thermal': 0.0}
        self.last_state = None
        self.started = time.monotonic()
        self.client = None

    # ---- MQTT
    def connect(self):
        import paho.mqtt.client as mqtt
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.device)
        except AttributeError:  # paho-mqtt 1.x
            client = mqtt.Client(client_id=self.device)
        m = self.cfg['mqtt']
        if m['username']:
            client.username_pw_set(m['username'], m['password'])
        client.will_set(f'{self.base}/status', 'offline', qos=1, retain=True)
        client.on_connect = lambda c, *a: (c.subscribe(f'{self.base}/cmd', qos=1), c.publish(f'{self.base}/status', 'online', qos=1, retain=True), setattr(self, 'last_state', None))
        client.on_message = self.on_message
        client.reconnect_delay_set(1, 30)
        client.connect_async(m['host'], int(m['port']), keepalive=30)
        client.loop_start()
        self.client = client

    def pub(self, suffix, payload, qos=0, retain=False):
        if self.client is not None:
            self.client.publish(f'{self.base}/{suffix}', payload, qos=qos, retain=retain)

    def on_message(self, _client, _userdata, msg):
        try:
            cmd = json.loads(msg.payload.decode('utf-8'))
            if isinstance(cmd, dict):
                self.cmds.put(cmd)
        except (ValueError, UnicodeDecodeError):
            pass

    def event(self, kind, detail):
        self.pub('event', json.dumps({'kind': kind, 'detail': detail, 'time': datetime.now(timezone.utc).isoformat()}), qos=1)

    # ---- camera control (one camera at a time, as in the original controller)
    def collect_flags(self):
        data = read_status(self.metrics)
        if data:
            for i, key in enumerate(('high_temperature', 'abnormal_behavior')):
                if int(data.get(key, 0) or 0) > 0 and not self.flags[i]:
                    self.flags[i] = 1
                    self.event('flag_latched', key.replace('_', ' '))
        return data

    def stop_camera(self):
        if self.proc is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.proc.pid, sig)
                except ProcessLookupError:
                    pass
                try:
                    self.proc.wait(timeout=3 if sig == signal.SIGTERM else 2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if self.proc.poll() is None:
                self.status = 'Camera has not stopped; not starting another reader.'
                return False
        if self.log is not None:
            self.log.close()
        self.proc = self.log = self.kind = None
        self.collect_flags()
        self.status = 'Camera stopped; flags retained.'
        return True

    def start_camera(self, kind):
        if self.closing or (self.kind == kind and self.proc is not None and self.proc.poll() is None):
            return
        if not self.stop_camera():
            return
        try:
            script = HERE / ('thermal_worker.py' if kind == 'thermal' else 'yolo_worker.py')
            self.log = open(self.log_dir / f'{kind}.log', 'w', buffering=1)
            self.proc = subprocess.Popen([sys.executable, '-u', str(script)], env=self.env, cwd=str(HERE),
                                         stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
            self.kind = kind
            self.status = f'{kind} camera active'
        except Exception as error:
            if self.log is not None:
                self.log.close(); self.log = None
            self.status = str(error)

    def reset_cycle(self):
        if self.sms_thread is not None and self.sms_thread.is_alive():
            self.event('reset_refused', 'Wait for the current SMS attempt to finish before Reset.')
            return
        if not self.stop_camera():
            return
        self.metrics.write_text('{}', encoding='utf-8')
        self.flags = [0, 0]; self.attempted = False
        self.sms.update(state='waiting', detail='Reset complete. SMS waits for BOTH flags = 1.', time=None)
        self.event('reset', 'Flags cleared; new alert cycle')
        if self.mode == 'auto':
            self.next_switch = 0.0

    def handle_command(self, cmd):
        action = cmd.get('action')
        if action == 'reset':
            self.reset_cycle()
        elif action == 'camera':
            kind = cmd.get('kind')
            if kind == 'auto':
                self.mode, self.next_switch = 'auto', 0.0
            elif kind in ('thermal', 'yolo'):
                self.mode = 'manual'; self.start_camera(kind)
            elif kind == 'stop':
                self.mode = 'manual'; self.stop_camera()

    # ---- data out
    def reading(self, data):
        fresh = max(30.0, 2 * float(self.cfg['cycle_s']) + 10.0)
        now = time.time()
        th = (data or {}).get('thermal') or {}
        yo = (data or {}).get('yolo') or {}
        temp = th.get('hottest_c') if th.get('valid') and now - th.get('frame_ts', 0) <= fresh else None
        posture = 'Unknown'
        if yo and now - yo.get('frame_ts', 0) <= fresh and not yo.get('model_error') and yo.get('pigs', 0) > 0:
            posture = 'Lying' if yo.get('lying', 0) > 0 else 'Not lying'
        return {'timestamp': datetime.now(timezone.utc).isoformat(), 'surface_temp_c': temp, 'posture': posture}

    def publish_previews(self):
        for kind, name in (('rgb', 'rgb.jpg'), ('thermal', 'thermal.jpg')):
            path = self.preview_dir / name
            try:
                mtime = path.stat().st_mtime
                if mtime > self.preview_sent[kind]:
                    blob = path.read_bytes()
                    self.preview_sent[kind] = mtime
                    if 0 < len(blob) <= MAX_FRAME_BYTES:
                        self.pub(f'preview/{kind}', blob, qos=0)
            except OSError:
                pass

    def state(self):
        return {'device_id': self.device, 'online': True, 'mode': self.mode, 'camera': self.kind,
                'flags': {'high_temperature': self.flags[0], 'abnormal_behavior': self.flags[1]},
                'threshold_c': self.cfg['high_temperature_threshold_c'], 'sms': dict(self.sms),
                'status': self.status, 'uptime_s': int(time.monotonic() - self.started)}

    def run(self):
        last_reading = last_state = 0.0
        interval = max(1.0, float(self.cfg['reading_interval_s']))
        self.start_camera('thermal') if self.mode == 'auto' else None
        self.next_switch = time.monotonic() + float(self.cfg['cycle_s'])
        while not self.closing:
            data = self.collect_flags()
            try:
                while True:
                    state, detail = self.results.get_nowait()
                    self.sms.update(state=state, detail=detail, time=datetime.now(timezone.utc).isoformat())
                    self.event('sms_' + state, detail)
            except queue.Empty:
                pass
            try:
                while True:
                    self.handle_command(self.cmds.get_nowait())
            except queue.Empty:
                pass
            if should_send(self.flags, self.attempted):
                self.attempted = True  # latch BEFORE dispatch; no automatic retry on an uncertain outcome
                if self.sms['enabled']:
                    self.sms.update(state='sending', detail='BOTH flags latched. Sending one SMS...')
                    message = build_alert(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                    self.sms_thread = threading.Thread(target=sms_job, args=(message, self.results, self.log_dir / 'sms_events.jsonl', self.cfg['gsm']), daemon=True)
                    self.sms_thread.start()
                else:
                    self.sms.update(state='disabled', detail='Both flags latched; SMS is disabled in prototype.json.')
                    self.event('sms_disabled', self.sms['detail'])
            if self.proc is not None and self.proc.poll() is not None:
                self.status = f'{self.kind} stopped. Check camera_logs/{self.kind}.log'
            now = time.monotonic()
            if self.mode == 'auto' and now >= self.next_switch:
                self.next_switch = now + float(self.cfg['cycle_s'])
                self.start_camera('yolo' if self.kind == 'thermal' else 'thermal')
            if now - last_reading >= interval:
                last_reading = now
                self.pub('reading', json.dumps(self.reading(data)), qos=1)
            current = json.dumps(self.state(), sort_keys=True)
            if current != self.last_state or now - last_state >= 10:
                last_state, self.last_state = now, current
                self.pub('state', current, qos=1, retain=True)
            self.publish_previews()
            time.sleep(0.25)

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.stop_camera()
        if self.sms_thread is not None and self.sms_thread.is_alive():
            print('SMS outcome may be unknown; check phone before restarting.', flush=True)
        if self.client is not None:
            self.pub('status', 'offline', qos=1, retain=True)
            time.sleep(0.2)
            self.client.loop_stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=HERE / 'prototype.json')
    parser.add_argument('--model', type=Path, default=HERE / 'YOLOv8A_SD_best.onnx')
    args = parser.parse_args()
    if not args.model.is_file():
        raise FileNotFoundError(f'Missing model: {args.model}')
    lock = open(Path(tempfile.gettempdir()) / f'pig-controller-{os.getuid()}.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('Another prototype controller is already running.')
    cfg = load_config(args.config)
    proto = Prototype(cfg, args.model.resolve())
    m = cfg['mqtt']
    address = f"mqtt://{own_ip(cfg)}:{m['port']}/{cfg['device_id']}"
    (HERE / 'prototype_address.txt').write_text(address + '\n', encoding='utf-8')
    print('=' * 60, flush=True)
    print('Prototype address (paste into "Add a pen" in the dashboard):', flush=True)
    print('  ' + address, flush=True)
    print('=' * 60, flush=True)
    proto.connect()
    signal.signal(signal.SIGTERM, lambda *_: setattr(proto, 'closing', True))
    signal.signal(signal.SIGINT, lambda *_: setattr(proto, 'closing', True))
    try:
        proto.run()
    finally:
        proto.closing = False
        proto.shutdown()
        lock.close()


if __name__ == '__main__':
    main()
