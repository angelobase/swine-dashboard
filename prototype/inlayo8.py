#!/usr/bin/env python3
"""Headless pig-pen prototype: scheduled screening sessions, local log, end-of-session SMS, MQTT data stream.

A session (started from the dashboard, or with --start / --test) runs on the Pi by itself:
  - every capture interval (default 30 min), capture_once.py takes one short thermal + webcam burst and
    reports each pig's posture and surface temperature;
  - every capture is written to the local log (pi_data/swine_pi.db) BEFORE it is published, so a Wi-Fi drop
    or a dashboard restart loses nothing; the dashboard asks for missed captures with a 'replay' command;
  - after the session duration (default 19 h) the Pi evaluates every pig itself (screening.py) and sends at
    most one SMS through the SIM800L, whether or not the dashboard is reachable.
Screening only, not a diagnosis. Pig IDs are position-based placeholders (pig_ids.py).
"""
import argparse
import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import screening  # noqa: E402
from pi_store import Store  # noqa: E402

DEFAULTS = {
    "device_id": "", "advertise_host": "",
    "mqtt": {"host": "auto", "port": 1883, "username": "", "password": ""},  # "auto" = find the dashboard on the network
    "session": {"interval_s": 1800, "duration_s": 68400},                  # methodology: every 30 min for 19 h
    "test": {"interval_s": 30, "duration_s": 300, "send_sms": True},       # dashboard Test mode / --test
    "screening": {"temperature_threshold_c": 36.7, "abnormal_lying_consecutive_captures": 6,
                  "final_temperature_lookback_captures": 3},
    "capture": {"rgb_warmup_frames": 15, "rgb_frames": 5, "rgb_frame_spacing_s": 0.3, "thermal_frames": 9,
                "thermal_budget_s": 8.0, "confidence": 0.93, "timeout_s": 120},
    "thermal": {"offset_c": 0.0, "calibration_file": "calibration.json"},
    "save_images": True,
    "gsm": {"enabled": True, "recipient": "+639083082262", "port": "/dev/serial0", "attempts": 3, "retry_delay_s": 60},
}
MAX_FRAME_BYTES = 250_000  # the dashboard rejects frames over preview.max_frame_bytes (default 262144)
UID_RE = re.compile(r'^[A-Za-z0-9_-]{6,40}$')
LATE_FRACTION = 0.9        # a capture may start up to 90 % of an interval late; after that the slot is recorded as missed


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
    for name in ('session', 'test'):
        s = cfg[name]
        if not (float(s['interval_s']) > 0 and float(s['duration_s']) >= float(s['interval_s'])):
            raise ValueError(f'prototype.json "{name}": interval_s must be > 0 and duration_s >= interval_s')
    sc = cfg['screening']
    if int(sc['abnormal_lying_consecutive_captures']) < 1 or int(sc['final_temperature_lookback_captures']) < 1:
        raise ValueError('prototype.json "screening": capture counts must be at least 1')
    return cfg


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def parse_time(text):
    try:
        return datetime.fromisoformat(str(text).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------- SMS (one message, limited retries)
def send_once(message, gsm, stage):
    """Same AT sequence as gsm_sms.py. stage['body_sent'] becomes True once the text is handed to the modem:
    after that point the outcome may be unknown and the message must NOT be resent (duplicate risk)."""
    import gsm_sms as m  # needs pyserial; imported only when an SMS is actually sent
    m.validate_message(message)
    with m.serial.Serial(gsm['port'], baudrate=115200, timeout=0.2, write_timeout=3, exclusive=True) as modem:
        m.connect(modem)
        m.command(modem, 'AT+CMEE=2')
        reply = m.command(modem, 'AT+CPIN?')
        if '+CPIN: READY' not in reply:
            raise RuntimeError('SIM not ready: ' + reply.strip())
        m.wait_for_network(modem)
        m.command(modem, 'AT+CMGF=1')
        m.command(modem, 'AT+CSCS="GSM"')
        m.command(modem, 'AT+CSMP=17,167,0,0')
        try:
            m.command(modem, f'AT+CMGS="{gsm["recipient"]}"', timeout=10, prompt=True)
        except Exception:
            modem.write(b'\x1b'); modem.flush()
            raise
        stage['body_sent'] = True
        modem.write(message.encode('ascii') + b'\x1a'); modem.flush()
        response = m.read_response(modem, timeout=60)
        if '+CMGS:' not in response:
            raise RuntimeError('No SMS submission reference: ' + response.strip())


def send_with_retries(message, gsm, send=send_once, sleep=time.sleep):
    """Retries only failures that happened BEFORE the text reached the modem (no SIM, no network, no prompt)."""
    attempts = max(1, int(gsm.get('attempts', 3)))
    for attempt in range(1, attempts + 1):
        stage = {'body_sent': False}
        try:
            send(message, gsm, stage)
            return 'submitted', f'SMS submitted (attempt {attempt}). Verify receipt on the phone.'
        except Exception as error:
            if stage['body_sent']:
                return 'unconfirmed', f'SMS handed to the modem but not confirmed; not resent to avoid a duplicate. Check the phone. ({error})'
            if attempt == attempts:
                return 'failed', f'SMS not sent after {attempts} attempt(s): {error}'
            sleep(float(gsm.get('retry_delay_s', 60)))


class Prototype:
    def __init__(self, cfg, model, config_path, data_dir=None, capture_fn=None, sms_send=None):
        self.cfg, self.model, self.config_path = cfg, model, config_path
        self.device = cfg['device_id']
        self.base = f'swine/{self.device}'
        self.log_dir = HERE / 'camera_logs'; self.log_dir.mkdir(exist_ok=True)
        self.data_dir = Path(data_dir or HERE / 'pi_data')
        self.store = Store(self.data_dir / 'swine_pi.db')
        self.env = os.environ.copy()
        self.env.update(PIG_MODEL_PATH=str(model), PIG_YOLO_THREADS='3')
        self.capture_fn = capture_fn or self.run_capture
        self.sms_send = sms_send or send_once
        self.cmds, self.capture_results, self.sms_results = queue.Queue(), queue.Queue(), queue.Queue()
        self.capture_thread = self.capture_proc = None
        self.sms_thread = None
        self.stop_requested = False
        self.status = 'Ready. Start a session from the dashboard.'
        self.closing = False
        self.started = time.monotonic()
        self.client = None
        self.hub = None                      # (host, port) currently used
        self.mqtt_ok = False
        self.lost_at = time.monotonic()
        self.last_state = self.last_session = None
        self.last_state_at = self.last_session_at = 0.0
        self.sess = None
        self.resume()

    # ---- MQTT (unchanged from the previous version)
    def connect(self):
        m = self.cfg['mqtt']
        if str(m['host']).strip().lower() not in ('', 'auto'):
            self.open_client(m['host'], int(m['port']))  # fixed address typed in prototype.json
        else:
            threading.Thread(target=self.discover_loop, daemon=True).start()

    def open_client(self, host, port):
        import paho.mqtt.client as mqtt
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.device)
        except AttributeError:  # paho-mqtt 1.x
            client = mqtt.Client(client_id=self.device)
        m = self.cfg['mqtt']
        if m['username']:
            client.username_pw_set(m['username'], m['password'])
        client.will_set(f'{self.base}/status', 'offline', qos=1, retain=True)
        client.on_connect = lambda c, *a: (c.subscribe(f'{self.base}/cmd', qos=1), c.publish(f'{self.base}/status', 'online', qos=1, retain=True), setattr(self, 'last_state', None), setattr(self, 'last_session', None), setattr(self, 'mqtt_ok', True))
        client.on_disconnect = lambda c, *a: (setattr(self, 'mqtt_ok', False), setattr(self, 'lost_at', time.monotonic()))
        client.on_message = self.on_message
        client.reconnect_delay_set(1, 30)
        client.connect_async(host, port, keepalive=30)
        client.loop_start()
        self.client, self.hub = client, (host, port)
        self.lost_at = time.monotonic()

    def close_client(self):
        client, self.client, self.mqtt_ok = self.client, None, False
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
            client.loop_stop()

    def discover_loop(self):
        """mqtt.host = "auto": listen for the dashboard's beacon until found, and look again if the link stays down
        (for example the dashboard computer got a new IP address)."""
        import hub_discovery
        announced = None
        while not self.closing:
            if self.client is None or (not self.mqtt_ok and time.monotonic() - self.lost_at > 20):
                found = hub_discovery.find_hub(5.0)
                if found and found != self.hub:
                    self.close_client()
                    self.open_client(*found)
                    if found != announced:
                        announced = found
                        print(f'Found the dashboard at {found[0]}:{found[1]}', flush=True)
                elif not found and self.client is None:
                    print('Looking for the dashboard on the network...', flush=True)
            else:
                time.sleep(2.0)

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

    def event(self, kind, detail, **extra):
        self.pub('event', json.dumps({'kind': kind, 'detail': detail, 'time': iso(time.time()), **extra}), qos=1)
        print(f'[{kind}] {detail}', flush=True)

    # ---- session lifecycle
    def now(self):
        """Reported time: Pi clock corrected by the offset measured against the dashboard at session start."""
        return time.time() + (self.sess['clock_offset_s'] if self.sess else 0.0)

    def resume(self):
        s = self.store.unfinished()
        if s and s['state'] == 'evaluating':  # restarted during evaluation / SMS
            sms = s['sms'] or {}
            if sms.get('state') == 'sending':
                sms = {**sms, 'state': 'unknown', 'detail': 'The prototype restarted while sending the SMS. It may or may not '
                       'have been delivered and is not resent automatically. Check the phone.'}
            self.store.update(s['uid'], state='finished', sms=sms)
            s = None
        if s:
            print(f"Resuming session {s['uid']} at capture {s['next_seq'] + 1} of {s['last_slot'] + 1}.", flush=True)
            self.status = 'Session resumed after a restart.'
        self.sess = s or self.store.latest()

    def running(self):
        return bool(self.sess and self.sess['state'] == 'running')

    def start_session(self, uid=None, pen=None, test=False, server_time=None):
        if self.sess and self.sess['state'] in ('running', 'evaluating'):
            self.event('start_refused', f"Session {self.sess['uid']} is still {self.sess['state']}. End it first.", uid=uid)
            return False
        if uid is not None and not UID_RE.match(str(uid)):
            self.event('start_refused', 'Invalid session id from the dashboard.', uid=None)
            return False
        sched, sc = self.cfg['test' if test else 'session'], self.cfg['screening']
        raw = time.time()
        server = parse_time(server_time) if server_time else None
        offset = 0.0 if server is None or abs(server - raw) < 2.0 else server - raw
        interval, duration = float(sched['interval_s']), float(sched['duration_s'])
        s = {'uid': str(uid or 'pi-' + uuid.uuid4().hex[:12]), 'pen': str(pen or self.device)[:32], 'test': int(bool(test)),
             'started_raw': raw, 'clock_offset_s': offset, 'clock_source': 'dashboard' if server is not None else 'pi',
             'interval_s': interval, 'duration_s': duration, 'last_slot': int(math.floor(duration / interval + 1e-9)),
             'threshold_c': float(sc['temperature_threshold_c']), 'rule_n': int(sc['abnormal_lying_consecutive_captures']),
             'lookback': int(sc['final_temperature_lookback_captures']), 'state': 'running', 'next_seq': 0,
             'ids': {'next_id': 1, 'pigs': {}}, 'tracks': {}}
        self.store.create(s)
        self.sess, self.stop_requested = self.store.unfinished(), False
        if abs(offset) >= 2.0:
            self.event('clock_offset', f'Pi clock differs from the dashboard by {offset:+.1f} s; reported times are corrected.')
        self.status = 'Session running.'
        self.event('session_started', f"{'TEST ' if test else ''}session {s['uid']}: {s['last_slot'] + 1} captures every "
                   f"{interval:g} s over {duration:g} s.", uid=s['uid'])
        self.publish_session(force=True)
        return True

    def due_raw(self, seq):
        return self.sess['started_raw'] + seq * self.sess['interval_s']

    def schedule(self):
        s = self.sess
        if self.stop_requested:
            return self.finish(early=True)
        k = s['next_seq']
        if k > s['last_slot']:
            return self.finish(early=False)
        now = time.time()
        if now < self.due_raw(k):
            return
        if now > self.due_raw(k) + LATE_FRACTION * s['interval_s']:
            return self.record(k, self.due_raw(k), {'ok': False, 'error': 'Missed: the prototype was not running or was busy at this time.', 'pigs': []})
        ids = s['ids']
        previous = {'next_id': ids['next_id'], 'pigs': [{'id': int(i), 'box': v['box']} for i, v in ids['pigs'].items()]}
        caption = f"{s['pen']}  capture {k + 1}/{s['last_slot'] + 1}  {datetime.fromtimestamp(self.now()).strftime('%Y-%m-%d %H:%M')}"
        out = self.data_dir / 'sessions' / s['uid'] / f'{k:03d}'
        started = now

        def job():
            try:
                result = self.capture_fn(out, previous, caption)
            except Exception as error:
                result = {'ok': False, 'error': f'{type(error).__name__}: {error}', 'pigs': []}
            self.capture_results.put((s['uid'], k, started, out, result))
        self.status = f'Capturing {k + 1} of {s["last_slot"] + 1}...'
        self.capture_thread = threading.Thread(target=job, daemon=True)
        self.capture_thread.start()

    def run_capture(self, out, previous, caption):
        out.mkdir(parents=True, exist_ok=True)
        (out / 'previous.json').write_text(json.dumps(previous), encoding='utf-8')
        cmd = [sys.executable, '-u', str(HERE / 'capture_once.py'), '--out', str(out), '--config', str(self.config_path),
               '--model', str(self.model), '--previous', str(out / 'previous.json'), '--caption', caption]
        timeout = float(self.cfg['capture']['timeout_s'])
        with open(self.log_dir / 'capture.log', 'a', buffering=1) as log:
            log.write(f'--- {iso(time.time())} {caption}\n')
            self.capture_proc = subprocess.Popen(cmd, cwd=str(HERE), env=self.env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                self.capture_proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.kill_capture()
                return {'ok': False, 'error': f'Capture took longer than {timeout:g} s and was stopped.', 'pigs': []}
            finally:
                code, self.capture_proc = self.capture_proc.returncode, None
        try:
            return json.loads((out / 'capture.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'ok': False, 'error': f'Capture failed (exit code {code}). See camera_logs/capture.log', 'pigs': []}

    def kill_capture(self):
        proc = self.capture_proc
        if proc is not None and proc.poll() is None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                    proc.wait(timeout=3)
                    break
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    continue

    def record(self, seq, started_raw, result, out=None):
        """Turn one capture (or a missed slot) into per-pig rows, update the tracks, log it, then publish it."""
        s = self.sess
        tracks, ids = s['tracks'], s['ids']
        observed = {}
        if result.get('ok'):
            for p in result.get('pigs', []):
                observed[int(p['id'])] = p
                ids['pigs'][str(p['id'])] = {'box': p['box'], 'seq': seq}
            ids['next_id'] = max(int(result.get('next_id', ids['next_id'])), ids['next_id'])
        for pid in observed:
            tracks.setdefault(str(pid), screening.new_track(seq))
        rows = []
        for key in sorted(tracks, key=int):
            p = observed.get(int(key))
            row = screening.step(tracks[key], seq, p is not None, bool(p and p['lying']), p['temp_c'] if p else None,
                                 s['interval_s'], s['rule_n'], s['threshold_c'])
            row.update(pig_id=int(key), observed=p is not None)
            if p:
                row.update(confidence=p.get('confidence'), frames_seen=p.get('frames_seen'))
            rows.append(row)
        images = {}
        if out is not None and s and self.cfg.get('save_images', True):
            images = {k: f"{s['uid']}/{seq:03d}/{v}" for k, v in (result.get('images') or {}).items()}
        payload = {'uid': s['uid'], 'seq': seq, 'ts': iso(started_raw + s['clock_offset_s']), 'ok': bool(result.get('ok')),
                   'error': result.get('error'), 'thermal_valid': bool(result.get('thermal_valid')),
                   'thermal_error': result.get('thermal_error'), 'calibrated': bool(result.get('calibrated')),
                   'pigs': rows, 'images': images}
        self.store.add_sample(s['uid'], seq, payload, next_seq=seq + 1, tracks=tracks, ids=ids)
        s['next_seq'] = seq + 1
        self.pub('sample', json.dumps(payload), qos=1)
        if out is not None:
            self.publish_images(out, result)
            if not self.cfg.get('save_images', True):
                shutil.rmtree(out, ignore_errors=True)
        self.status = f"Capture {seq + 1} of {s['last_slot'] + 1}: " + (f"{sum(r['observed'] for r in rows)} pig(s) detected." if payload['ok'] else payload['error'] or 'failed')
        self.publish_session(force=True)

    def publish_images(self, out, result):
        for kind, name in (result.get('images') or {}).items():
            try:
                blob = (out / name).read_bytes()
                if kind in ('rgb', 'thermal') and 0 < len(blob) <= MAX_FRAME_BYTES:
                    self.pub(f'preview/{kind}', blob, qos=0)
            except OSError:
                pass

    def finish(self, early):
        s = self.sess
        result = screening.evaluate(s['tracks'], s['next_seq'] - 1, s['interval_s'], s['rule_n'], s['threshold_c'], s['lookback'])
        ended = self.now()
        gsm = self.cfg['gsm']
        sms = {'enabled': bool(gsm.get('enabled')), 'time': None}
        if early:
            sms.update(state='skipped', detail='Session ended early; no SMS is sent.')
        elif not result['any_alert']:
            sms.update(state='not_needed', detail='No pig met both conditions; no SMS.')
        elif s['test'] and not self.cfg['test'].get('send_sms', True):
            sms.update(state='skipped', detail='Test session: SMS disabled in prototype.json (test.send_sms).')
        elif not gsm.get('enabled'):
            sms.update(state='disabled', detail='SMS is disabled in prototype.json (gsm.enabled).')
        else:
            message = screening.build_sms(s['pen'], result, ended, s['duration_s'], test=bool(s['test']))
            sms.update(state='sending', detail='Sending one SMS...', message=message)
            self.store.update(s['uid'], state='evaluating', ended_at=ended, result=result, sms=sms)
            s.update(state='evaluating', ended_at=ended, result=result, sms=sms)

            def job():
                self.sms_results.put(send_with_retries(message, gsm, self.sms_send))
            self.sms_thread = threading.Thread(target=job, daemon=True)
            self.sms_thread.start()
            self.status = 'Session complete; sending the SMS.'
            self.event('session_evaluated', f"Flagged pigs: {', '.join(map(str, result['flagged']))}. Sending SMS.", uid=s['uid'])
            return self.publish_session(force=True)
        final = 'ended_early' if early else 'finished'
        self.store.update(s['uid'], state=final, ended_at=ended, result=result, sms=sms)
        s.update(state=final, ended_at=ended, result=result, sms=sms)
        self.status = 'Session ended early.' if early else 'Session complete.'
        flagged = ', '.join(map(str, result['flagged'])) or 'none'
        self.event('session_ended' if early else 'session_evaluated', f'Flagged pigs: {flagged}. {sms["detail"]}', uid=s['uid'])
        self.publish_session(force=True)

    def sms_done(self, state, detail):
        s = self.sess
        sms = {**(s['sms'] or {}), 'state': state, 'detail': detail, 'time': iso(self.now())}
        try:
            with (self.log_dir / 'sms_events.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps({'time': sms['time'], 'session': s['uid'], 'message': sms.get('message'), 'result': detail}) + '\n')
        except OSError:
            pass
        self.store.update(s['uid'], state='finished', sms=sms)
        s.update(state='finished', sms=sms)
        self.sms_thread = None
        self.status = 'Session complete.'
        self.event('sms_' + state, detail, uid=s['uid'])
        self.publish_session(force=True)

    def abandon(self):
        s = self.store.unfinished()
        if s:
            self.store.update(s['uid'], state='abandoned', ended_at=time.time() + s['clock_offset_s'])
            print(f"Session {s['uid']} marked abandoned (no evaluation, no SMS).", flush=True)
        self.sess = self.store.latest()

    # ---- commands from the dashboard
    def handle_command(self, cmd):
        action = cmd.get('action')
        if action == 'start':
            self.start_session(cmd.get('uid'), cmd.get('pen'), bool(cmd.get('test')), cmd.get('server_time'))
        elif action == 'stop':
            if self.running() and cmd.get('uid') in (None, self.sess['uid']):
                self.stop_requested = True
                self.status = 'Ending the session early...'
            else:
                self.event('stop_ignored', 'No running session with that id.', uid=cmd.get('uid'))
        elif action == 'replay':
            uid = str(cmd.get('uid', ''))
            seqs = cmd.get('seqs') if isinstance(cmd.get('seqs'), list) else None
            for payload in self.store.samples(uid, seqs=seqs, after=cmd.get('after')):
                self.pub('sample', json.dumps(payload), qos=1)
        elif action in ('reset', 'camera'):
            self.event('unsupported', 'This prototype runs scheduled sessions; Reset and camera selection no longer apply.')

    # ---- data out
    def session_view(self):
        s = self.sess
        if not s:
            return None
        off = s['clock_offset_s']
        nxt = s['next_seq']
        running = s['state'] == 'running'
        return {'uid': s['uid'], 'pen': s['pen'], 'test': bool(s['test']), 'state': s['state'],
                'started_at': iso(s['started_raw'] + off), 'ends_at': iso(s['started_raw'] + off + s['duration_s']),
                'interval_s': s['interval_s'], 'duration_s': s['duration_s'], 'captures': s['last_slot'] + 1,
                'next_seq': nxt, 'last_seq': nxt - 1, 'capturing': self.capture_thread is not None,
                'next_capture_at': iso(self.due_raw(nxt) + off) if running and nxt <= s['last_slot'] else None,
                'stopping': bool(running and self.stop_requested), 'clock_source': s['clock_source'],
                'rule': {'threshold_c': s['threshold_c'], 'consecutive_captures': s['rule_n'], 'lookback_captures': s['lookback']},
                'ended_at': iso(s['ended_at']) if s['ended_at'] else None, 'result': s['result'],
                'sms': {k: v for k, v in (s['sms'] or {}).items() if k != 'message'} or None}

    def publish_session(self, force=False):
        view = self.session_view()
        if view is None:
            return
        current = json.dumps(view, sort_keys=True)
        now = time.monotonic()
        if force or current != self.last_session or now - self.last_session_at >= 30:
            self.last_session, self.last_session_at = current, now
            self.pub('session', current, qos=1, retain=True)

    def state(self):
        s = self.sess
        sms = dict((s or {}).get('sms') or {'state': 'idle', 'detail': 'No session evaluated yet.'})
        sms.pop('message', None)
        sms['enabled'] = bool(self.cfg['gsm'].get('enabled'))
        return {'device_id': self.device, 'online': True, 'mode': 'scheduled', 'status': self.status,
                'capturing': self.capture_thread is not None, 'session_state': s['state'] if s else None,
                'threshold_c': self.cfg['screening']['temperature_threshold_c'], 'sms': sms,
                'uptime_s': int(time.monotonic() - self.started)}

    def tick(self):
        try:
            while True:
                self.handle_command(self.cmds.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                uid, seq, started, out, result = self.capture_results.get_nowait()
                self.capture_thread = None
                if self.sess and self.sess['uid'] == uid and self.sess['state'] == 'running':
                    self.record(seq, started, result, out)
        except queue.Empty:
            pass
        try:
            while True:
                self.sms_done(*self.sms_results.get_nowait())
        except queue.Empty:
            pass
        if self.running() and self.capture_thread is None:
            self.schedule()
        now = time.monotonic()
        current = json.dumps(self.state(), sort_keys=True)
        if current != self.last_state or now - self.last_state_at >= 10:
            self.last_state, self.last_state_at = current, now
            self.pub('state', current, qos=1, retain=True)
        self.publish_session()

    def run(self):
        while not self.closing:
            self.tick()
            time.sleep(0.25)

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.kill_capture()
        if self.sms_thread is not None and self.sms_thread.is_alive():
            print('SMS outcome may be unknown; check the phone before restarting.', flush=True)
        if self.client is not None:
            self.pub('status', 'offline', qos=1, retain=True)
            time.sleep(0.2)
            self.client.loop_stop()
        self.store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', type=Path, default=HERE / 'prototype.json')
    parser.add_argument('--model', type=Path, default=HERE / 'YOLOv8A_SD_best.onnx')
    parser.add_argument('--start', action='store_true', help='start a full session now (no dashboard needed)')
    parser.add_argument('--test', action='store_true', help='start a short test session now')
    parser.add_argument('--abandon', action='store_true', help='mark an unfinished session as abandoned (no evaluation, no SMS)')
    args = parser.parse_args()
    if not args.model.is_file():
        raise FileNotFoundError(f'Missing model: {args.model}')
    import fcntl  # Pi/Linux only; imported here so the module also loads on Windows (test_push.py)
    lock = open(Path(tempfile.gettempdir()) / f'pig-controller-{os.getuid()}.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('Another prototype controller is already running.')
    cfg = load_config(args.config)
    proto = Prototype(cfg, args.model.resolve(), args.config.resolve())
    if args.abandon:
        proto.abandon()
    m = cfg['mqtt']
    address = f"mqtt://{cfg['advertise_host'] or m['host']}:{m['port']}/{cfg['device_id']}"
    (HERE / 'prototype_address.txt').write_text(address + '\n', encoding='utf-8')
    print('=' * 60, flush=True)
    print(f"Prototype id: {cfg['device_id']}", flush=True)
    print('It will appear in the dashboard under "Add a pen" once it is connected. If you need to type it:', flush=True)
    print('  ' + address, flush=True)
    print('=' * 60, flush=True)
    proto.connect()
    if (args.start or args.test) and not proto.running():
        proto.start_session(test=args.test)
    elif not proto.running():
        print('Ready. Start a session from the dashboard, or restart with --start / --test.', flush=True)
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
