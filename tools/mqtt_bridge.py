#!/usr/bin/env python3
"""MQTT -> dashboard bridge. Runs on the machine that runs the dashboard (loopback).

Reads the pens added in the dashboard ("Add a pen" -> Prototype address, e.g. mqtt://192.168.4.1:1883/proto-3f9a2c),
subscribes to each prototype's topics and forwards:
  <base>/reading        -> POST /api/ingest                (timestamp = bridge arrival time, so Pi clock drift cannot reject data)
  <base>/state, /event  -> POST /api/device/<PEN>
  <base>/preview/<kind> -> POST /api/preview/<PEN>/<kind>
and delivers commands queued in the dashboard to <base>/cmd.
Environment: DASHBOARD (default http://127.0.0.1:8080), INGEST_TOKEN, MQTT_USER, MQTT_PASS.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

DASH = os.environ.get('DASHBOARD', 'http://127.0.0.1:8080').rstrip('/')
TOKEN = os.environ.get('INGEST_TOKEN', '')
USER, PASS = os.environ.get('MQTT_USER', 'swine'), os.environ.get('MQTT_PASS', '')


def parse_address(text):
    """'mqtt://host:1883/device', 'host:1883/device' or 'host/device' -> (host, port, device) or None."""
    u = urlparse(text if '://' in text else 'mqtt://' + text)
    parts = [x for x in u.path.split('/') if x]
    if not u.hostname or len(parts) != 1 or not all(c.isalnum() or c in '-_' for c in parts[0]):
        return None
    return u.hostname, u.port or 1883, parts[0]


def http(method, path, body=None, ctype='application/json'):
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    req = urllib.request.Request(DASH + path, data=data, method=method)
    req.add_header('Content-Type', ctype)
    req.add_header('X-Requested-With', 'dashboard')
    if TOKEN:
        req.add_header('X-Ingest-Token', TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b'{}')
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError):
        return 0, {}


def forward(pen, suffix, payload):
    """Pure routing of one MQTT message to the dashboard. Returns (status, response) or None."""
    try:
        if suffix == 'reading':
            r = json.loads(payload)
            body = {'pen_id': pen, 'timestamp': datetime.now(timezone.utc).isoformat(),
                    'surface_temp_c': r.get('surface_temp_c'), 'posture': r.get('posture', 'Unknown')}
            return http('POST', '/api/ingest', body)
        if suffix == 'state':
            return http('POST', f'/api/device/{pen}', {'state': json.loads(payload)})
        if suffix == 'event':
            return http('POST', f'/api/device/{pen}', {'event': json.loads(payload)})
        if suffix == 'status':
            return http('POST', f'/api/device/{pen}', {'state': {'online': payload.decode() == 'online'}})
        if suffix in ('preview/rgb', 'preview/thermal'):
            return http('POST', f'/api/preview/{pen}/{suffix.split("/")[1]}', payload, 'image/jpeg')
    except (ValueError, AttributeError):
        pass
    return None


class Link:
    """One MQTT connection per broker; subscriptions follow the dashboard's pen list."""
    def __init__(self, host, port):
        import paho.mqtt.client as mqtt
        try:
            self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'bridge-{os.getpid()}-{host}')
        except AttributeError:
            self.c = mqtt.Client(client_id=f'bridge-{os.getpid()}-{host}')
        if USER:
            self.c.username_pw_set(USER, PASS)
        self.devices = {}  # device_id -> pen_id
        self.c.on_connect = lambda c, *a: [c.subscribe(f'swine/{d}/#', qos=1) for d in list(self.devices)]
        self.c.on_message = self.on_message
        self.c.reconnect_delay_set(1, 30)
        self.c.connect_async(host, port, keepalive=30)
        self.c.loop_start()

    def on_message(self, _c, _u, msg):
        parts = msg.topic.split('/', 2)
        if len(parts) == 3 and parts[1] in self.devices:
            res = forward(self.devices[parts[1]], parts[2], msg.payload)
            if res and res[0] >= 400 and parts[2] != 'preview/rgb':
                print(f'[{parts[1]}] {parts[2]} -> HTTP {res[0]} {res[1].get("error", "")}', flush=True)

    def set_devices(self, wanted):
        for d in set(self.devices) - set(wanted):
            self.c.unsubscribe(f'swine/{d}/#'); del self.devices[d]
        for d, pen in wanted.items():
            if d not in self.devices:
                self.devices[d] = pen; self.c.subscribe(f'swine/{d}/#', qos=1)
            self.devices[d] = pen


def main():
    links, lock = {}, threading.Lock()
    print(f'Bridge running. Dashboard {DASH}', flush=True)
    last_sync = 0.0
    while True:
        now = time.monotonic()
        if now - last_sync >= 10:
            last_sync = now
            code, st = http('GET', '/api/status')
            if code == 200:
                want = {}
                for r in st.get('registry', []):
                    p = parse_address(r.get('address', ''))
                    if p:
                        want.setdefault((p[0], p[1]), {})[p[2]] = r['pen_id']
                with lock:
                    for key, devs in want.items():
                        links.setdefault(key, Link(*key)).set_devices(devs)
                    for key in set(links) - set(want):
                        links[key].set_devices({})
        with lock:  # deliver commands queued by the dashboard (Reset flags, camera selection)
            for link in links.values():
                for dev, pen in list(link.devices.items()):
                    code, res = http('GET', f'/api/device/{pen}/commands')
                    for cmd in (res.get('commands') or []) if code == 200 else []:
                        link.c.publish(f'swine/{dev}/cmd', json.dumps(cmd), qos=1)
        time.sleep(1)


if __name__ == '__main__':
    main()
