#!/usr/bin/env python3
"""Fake prototype for testing the dashboard. Runs on Windows, Linux or Mac. No cameras, modem or model needed.

It behaves like inlayo8.py on the network: it finds the dashboard (or uses --host), shows up in the "Add a pen" list,
then streams random readings, state, events and camera frames, and answers Reset / Cam commands from the dashboard.
It NEVER sends a real SMS.

Install (once):   pip install paho-mqtt            (optional, for nicer fake camera images:  pip install pillow)
Run:              python inlayo_test.py
Several fake pens at once: run it again in another window with a different id:  python inlayo_test.py --id proto-test02
Stop with Ctrl+C.
"""
import argparse
import base64
import io
import json
import math
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit('paho-mqtt is not installed. Run:  pip install paho-mqtt')

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = ImageDraw = None

# 64x48 grey JPEG used when Pillow is not installed (the dashboard only needs a valid JPEG)
FALLBACK_JPEG = base64.b64decode(
    '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABQODxIPDRQSEBIXFRQYHjIhHhwcHj0sLiQySUBMS0dARkVQWnNiUFVtVkVGZIhlbXd7gYKBTmCNl4x9lnN+gXz/2wBDARUXFx4aHjshITt8U0ZTfHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHz/wAARCAAwAEADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCrRRRQZhRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAf//Z')


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def make_frame(kind, temp, posture, tick):
    """Small random JPEG so the tile shows something moving."""
    if Image is None:
        return FALLBACK_JPEG
    w, h = 320, 240
    img = Image.new('RGB', (w, h), (28, 36, 34) if kind == 'rgb' else (10, 0, 40))
    d = ImageDraw.Draw(img)
    if kind == 'rgb':
        d.rectangle([0, 170, w, h], fill=(70, 55, 40))                       # floor
        x = 60 + int(80 * (1 + math.sin(tick / 6.0)))
        d.ellipse([x, 110, x + 110, 190], fill=(231, 160, 170))              # "pig"
    else:
        for _ in range(6):
            x, y = random.randint(20, w - 80), random.randint(20, h - 80)
            r = random.randint(25, 60)
            c = random.choice([(255, 140, 0), (255, 220, 40), (200, 40, 120)])
            d.ellipse([x, y, x + r, y + r], fill=c)
    d.text((8, 8), f'TEST {kind.upper()}  {temp:.1f} C  {posture}' if temp is not None else f'TEST {kind.upper()}', fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=60)
    return buf.getvalue()


class FakePrototype:
    def __init__(self, a):
        self.a = a
        self.device = a.id
        self.base = f'swine/{self.device}'
        self.started = time.monotonic()
        self.mode, self.camera = 'auto', 'thermal'
        self.flags = [0, 0]
        self.sms = {'enabled': False, 'state': 'disabled', 'detail': 'Test prototype: no real SMS is ever sent.', 'time': None}
        self.status = 'Test data stream'
        self.temp = a.base_temp
        self.posture = 'Not lying'
        self.next_switch = time.monotonic() + a.cycle
        self.hot_since = None
        self.connected = False
        self.last_state = None
        self.hub = None
        self.client = None
        self.stopping = False

    # ---- MQTT
    def open(self, host, port):
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.device)
        except AttributeError:  # paho-mqtt 1.x
            c = mqtt.Client(client_id=self.device)
        if self.a.user:
            c.username_pw_set(self.a.user, self.a.password)
        c.will_set(f'{self.base}/status', 'offline', qos=1, retain=True)

        def on_connect(cl, *args):
            self.connected = True
            cl.subscribe(f'{self.base}/cmd', qos=1)
            cl.publish(f'{self.base}/status', 'online', qos=1, retain=True)
            self.last_state = None
            print(f'Connected to {host}:{port} as {self.device}', flush=True)

        def on_disconnect(cl, *args):
            if self.connected and not self.stopping:
                print('Disconnected from the dashboard; retrying...', flush=True)
            self.connected = False

        c.on_connect, c.on_disconnect, c.on_message = on_connect, on_disconnect, self.on_message
        c.reconnect_delay_set(1, 15)
        c.connect_async(host, port, keepalive=30)
        c.loop_start()
        self.client, self.hub = c, (host, port)

    def pub(self, suffix, payload, qos=0, retain=False):
        if self.client is not None and self.connected:
            self.client.publish(f'{self.base}/{suffix}', payload, qos=qos, retain=retain)

    def event(self, kind, detail):
        print(f'event: {kind} - {detail}', flush=True)
        self.pub('event', json.dumps({'kind': kind, 'detail': detail, 'time': now_iso()}), qos=1)

    def on_message(self, _c, _u, msg):
        try:
            cmd = json.loads(msg.payload.decode('utf-8'))
        except ValueError:
            return
        action = cmd.get('action')
        print(f'command received: {cmd}', flush=True)
        if action == 'reset':
            self.flags, self.hot_since = [0, 0], None
            self.sms['time'] = None  # a new alert cycle can report "both flags" again
            self.event('reset', 'Flags cleared; new alert cycle')
        elif action == 'camera':
            kind = cmd.get('kind')
            if kind == 'auto':
                self.mode = 'auto'
            elif kind in ('thermal', 'yolo'):
                self.mode, self.camera = 'manual', kind
            elif kind == 'stop':
                self.mode, self.camera = 'manual', None
        self.last_state = None  # publish the new state straight away

    # ---- fake data
    def step(self, tick):
        a = self.a
        # temperature: slow random walk around base_temp, with occasional fever-like drifts upward
        drift = 0.35 if random.random() < a.fever_chance else 0.0
        self.temp += random.uniform(-0.25, 0.25) + drift + (a.base_temp - self.temp) * 0.05
        self.temp = max(30.0, min(42.0, self.temp))
        # posture: sticky (changes rarely), like a real pig
        if random.random() < 0.08:
            self.posture = random.choice(['Lying', 'Lying', 'Not lying'])
        # flags latch like the real prototype
        if self.temp >= a.threshold:
            self.hot_since = self.hot_since or time.monotonic()
            if not self.flags[0] and time.monotonic() - self.hot_since >= a.confirm:
                self.flags[0] = 1
                self.event('flag_latched', 'high temperature')
        else:
            self.hot_since = None
        if not self.flags[1] and random.random() < a.abnormal_chance:
            self.flags[1] = 1
            self.event('flag_latched', 'abnormal behavior')
        if self.flags == [1, 1] and self.sms['state'] == 'disabled' and self.sms['time'] is None:
            self.sms['time'] = now_iso()
            self.event('sms_disabled', 'Both flags latched; test prototype does not send SMS.')

    def reading(self):
        temp = round(self.temp, 1)
        if random.random() < 0.05:
            temp = None                    # sometimes no thermal value, like a camera switch
        posture = self.posture if random.random() > 0.05 else 'Unknown'
        if self.camera is None and self.mode == 'manual':
            temp, posture = None, 'Unknown'
        return {'timestamp': now_iso(), 'surface_temp_c': temp, 'posture': posture}

    def state(self):
        return {'device_id': self.device, 'online': True, 'mode': self.mode, 'camera': self.camera,
                'flags': {'high_temperature': self.flags[0], 'abnormal_behavior': self.flags[1]},
                'threshold_c': self.a.threshold, 'sms': dict(self.sms), 'status': self.status,
                'uptime_s': int(time.monotonic() - self.started)}

    # ---- main loop
    def run(self):
        a = self.a
        if a.host.lower() in ('', 'auto'):
            import hub_discovery
            print('Looking for the dashboard on the network...', flush=True)
            found = None
            while not found:
                found = hub_discovery.find_hub(5.0)
                if not found:
                    print('  not found yet (is the dashboard running? use --host <ip> to skip discovery)', flush=True)
            print(f'Found the dashboard at {found[0]}:{found[1]}', flush=True)
            self.open(*found)
        else:
            self.open(a.host, a.port)
        print(f'Streaming fake data for {self.device}. In the dashboard: press +, pick it, add the pen, press Start. Ctrl+C to stop.', flush=True)
        tick, last_reading, last_state, last_preview = 0, 0.0, 0.0, 0.0
        while True:
            now = time.monotonic()
            tick += 1
            self.step(tick)
            if self.mode == 'auto' and now >= self.next_switch:
                self.next_switch = now + a.cycle
                self.camera = 'yolo' if self.camera == 'thermal' else 'thermal'
                self.last_state = None
            if now - last_reading >= a.interval:
                last_reading = now
                r = self.reading()
                self.pub('reading', json.dumps(r), qos=1)
                if self.connected:
                    print(f"reading: {r['surface_temp_c']} C, {r['posture']}, flags {self.flags}", flush=True)
            cur = json.dumps(self.state(), sort_keys=True)
            if cur != self.last_state or now - last_state >= 10:
                last_state, self.last_state = now, cur
                self.pub('state', json.dumps(self.state()), qos=1, retain=True)
            if not a.no_preview and now - last_preview >= 1.0:
                last_preview = now
                self.pub('preview/rgb', make_frame('rgb', self.temp, self.posture, tick))
                self.pub('preview/thermal', make_frame('thermal', self.temp, self.posture, tick))
            time.sleep(0.25)

    def stop(self):
        self.stopping = True
        if self.client is not None:
            if self.connected:
                self.client.publish(f'{self.base}/status', 'offline', qos=1, retain=True).wait_for_publish(2)
            self.client.loop_stop()
            self.client.disconnect()


def main():
    p = argparse.ArgumentParser(description='Fake prototype that streams random test data to the dashboard.')
    default_id = 'proto-test' + format(uuid.getnode() & 0xFFF, '03x')
    p.add_argument('--id', default=default_id, help=f'device id (default {default_id}); use a different one per fake pen')
    p.add_argument('--host', default='auto', help='dashboard computer IP, or "auto" to find it (default)')
    p.add_argument('--port', type=int, default=1883)
    p.add_argument('--user', default='', help='MQTT username (only if the dashboard has a password), usually swine')
    p.add_argument('--password', default='', help='MQTT password')
    p.add_argument('--interval', type=float, default=2.0, help='seconds between readings (default 2)')
    p.add_argument('--threshold', type=float, default=35.0, help='high temperature threshold in C (default 35, match config.json)')
    p.add_argument('--base-temp', dest='base_temp', type=float, default=34.0, help='typical surface temperature (default 34)')
    p.add_argument('--fever-chance', dest='fever_chance', type=float, default=0.03, help='chance per step of a temperature climb (default 0.03)')
    p.add_argument('--abnormal-chance', dest='abnormal_chance', type=float, default=0.002, help='chance per step of the abnormal-behavior flag (default 0.002)')
    p.add_argument('--confirm', type=float, default=1.0, help='seconds above threshold before the flag latches (default 1)')
    p.add_argument('--cycle', type=float, default=20.0, help='seconds between thermal/webcam switches in auto mode (default 20)')
    p.add_argument('--no-preview', action='store_true', help='do not send fake camera images')
    a = p.parse_args()
    if not all(c.isalnum() or c in '-_' for c in a.id) or not 1 <= len(a.id) <= 40:
        sys.exit('--id may only contain letters, digits, - and _ (max 40).')
    if Image is None and not a.no_preview:
        print('Pillow not installed: using a plain grey camera image. For moving fake images:  pip install pillow', flush=True)
    proto = FakePrototype(a)
    try:
        proto.run()
    except KeyboardInterrupt:
        print('\nStopping...', flush=True)
    finally:
        proto.stop()


if __name__ == '__main__':
    main()
