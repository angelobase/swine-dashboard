"""Find the dashboard's MQTT broker on the local network (standard library only).

The dashboard broadcasts a small UDP beacon (see lib/beacon.js). This listens for it and returns (ip, mqtt_port).
"""
import json
import socket
import time

PORT = 18830


def find_hub(timeout=5.0, port=PORT):
    """Return (ip, mqtt_port) of the first dashboard heard within `timeout` seconds, else None."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, 'SO_REUSEPORT'):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(('', port))
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return None
            sock.settimeout(left)
            try:
                data, (ip, _port) = sock.recvfrom(1024)
            except socket.timeout:
                return None
            try:
                msg = json.loads(data.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(msg, dict) and msg.get('swine') == 1 and isinstance(msg.get('mqtt_port'), int):
                return ip, msg['mqtt_port']
    except OSError:
        return None
    finally:
        sock.close()
