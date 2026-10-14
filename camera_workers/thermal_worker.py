from preview_support import save_preview
#!/usr/bin/env python3
"""Optimized multi-region FLIR Lepton 3.1R temperature viewer.

Designed for Raspberry Pi 4B and FLIR Lepton 3.1R on SPI0 CE0.

The program detects separate warm regions in the thermal image, gives each
region a temporary Thermal ID, and displays a temporally smoothed surface
temperature. A thermal region is not guaranteed to be one pig: touching pigs
can merge and other warm objects can be detected. Reliable per-pig identity
requires RGB YOLO detections plus calibrated RGB-to-thermal registration.

Temperature is valid only when the Lepton is outputting TLinear radiometric
pixels. The displayed value is surface temperature, not rectal/core body
temperature.
"""

from dataclasses import dataclass
import json
import fcntl
import os
import queue
import threading
import time

import cv2
import numpy as np
import spidev


# ---------------------------------------------------------------------------
# FLIR Lepton / SPI configuration
# ---------------------------------------------------------------------------
SPI_BUS = 0
SPI_DEVICE = 0
SPI_SPEED_HZ = 18_000_000
PACKET_SIZE = 164
PACKETS_PER_SEGMENT = 60
WIDTH = 160
HEIGHT = 120
CAPTURE_TIMEOUT = 2.0
RESYNC_DELAY_SECONDS = 0.25

# ---------------------------------------------------------------------------
# Display and radiometry
# ---------------------------------------------------------------------------
DISPLAY_SCALE = 4
GUI_UPDATE_MS = 30
TLINEAR_K_PER_COUNT = 0.01

# Calibrate this against a trusted reference at the intended distance.
# corrected temperature = measured temperature + offset
TEMPERATURE_OFFSET_C = 0.0

# ---------------------------------------------------------------------------
# Warm-region detection
# ---------------------------------------------------------------------------
# A region must be warmer than the frame median by at least this amount.
MINIMUM_DELTA_ABOVE_MEDIAN_C = 1.0

# Ignore extremely small blobs and a blob covering almost the whole image.
MINIMUM_REGION_AREA_PIXELS = 100
MAXIMUM_REGION_AREA_PIXELS = int(WIDTH * HEIGHT * 0.80)

# A robust high percentile estimates warm body surface without trusting a
# single noisy maximum pixel.
REGION_TEMPERATURE_PERCENTILE = 90.0

# Tracking and temperature smoothing.
MAX_TRACK_DISTANCE_PIXELS = 28.0
MAX_MISSED_FRAMES = 12
MINIMUM_TRACK_HITS = 2
TEMPERATURE_EMA_ALPHA = 0.25

# Surface-temperature alert threshold used for the High Temperature counter.
# This is a configurable surface-temperature threshold, not a core/rectal
# body-temperature diagnosis. Adjust it for your camera/calibration setup.
HIGH_TEMPERATURE_THRESHOLD_C = 35.0

# A condition must remain detected this long before it is counted.
EVENT_CONFIRMATION_SEC = 1.0


def publish_controller_metrics(**metrics):
    """Merge non-counter status values into the shared parent UI file."""
    path = os.environ.get("PIG_STATUS_PATH")
    if not path:
        return
    try:
        with open(path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                try:
                    payload = json.load(handle)
                except (json.JSONDecodeError, ValueError):
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                payload.update(metrics)
                payload["timestamp"] = time.time()
                handle.seek(0)
                handle.truncate()
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def increment_controller_metric(field, amount=1):
    """Atomically increment a cumulative controller counter."""
    path = os.environ.get("PIG_STATUS_PATH")
    if not path:
        return 0
    try:
        with open(path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                try:
                    payload = json.load(handle)
                except (json.JSONDecodeError, ValueError):
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                total = 1  # Latch until the controller explicitly resets the cycle.
                payload[field] = total
                payload["timestamp"] = time.time()
                handle.seek(0)
                handle.truncate()
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
                return total
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, TypeError, ValueError):
        return 0



def read_controller_metric(field):
    path = os.environ.get("PIG_STATUS_PATH")
    if not path:
        return 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return max(0, int(payload.get(field, 0)))
    except (OSError, ValueError, TypeError):
        return 0

def colour_map(gray):
    """Convert an 8-bit grayscale frame to an RGB thermal colour map."""
    x = gray.astype(np.int16)
    red = np.clip(2 * x - 128, 0, 255)
    blue = np.clip(255 - 2 * x, 0, 255)
    green = 255 - np.abs(2 * x - 255)
    return np.dstack((red, green, blue)).astype(np.uint8)


def raw_to_celsius(raw):
    """Convert Kelvin x100 TLinear pixels to degrees Celsius."""
    return (
        raw.astype(np.float32) * TLINEAR_K_PER_COUNT
        - 273.15
        + TEMPERATURE_OFFSET_C
    )


def temperatures_are_plausible(celsius):
    """Reject non-TLinear data instead of displaying false temperatures."""
    median = float(np.median(celsius))
    return -80.0 <= median <= 250.0


class Lepton3:
    def __init__(self):
        self.spi = None
        self.frame_counter = 0
        self.error_counter = 0
        self.last_error = ""
        self.zero_packet = [0] * PACKET_SIZE
        self._open()

    def _open(self):
        self.spi = spidev.SpiDev()
        self.spi.open(SPI_BUS, SPI_DEVICE)
        self.spi.mode = 3
        self.spi.max_speed_hz = SPI_SPEED_HZ
        self.spi.bits_per_word = 8

    def resync(self):
        try:
            if self.spi is not None:
                self.spi.close()
        except Exception:
            pass
        time.sleep(RESYNC_DELAY_SECONDS)
        self._open()

    def read_packet(self):
        return self.spi.xfer2([0]*PACKET_SIZE)

    def capture(self, timeout=CAPTURE_TIMEOUT):
        frame = np.zeros((HEIGHT, WIDTH), dtype=np.uint16)
        expected_segment = 1
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            packets = []

            for expected_packet in range(PACKETS_PER_SEGMENT):
                packet = self.read_packet()

                if len(packet) != PACKET_SIZE:
                    packets = []
                    break
                if (packet[0] & 0x0F) == 0x0F:
                    packets = []
                    break

                packet_number = ((packet[0] & 0x0F) << 8) | packet[1]
                if packet_number != expected_packet:
                    packets = []
                    break
                packets.append(packet)

            if len(packets) != PACKETS_PER_SEGMENT:
                continue

            segment = (packets[20][0] >> 4) & 0x07
            if segment not in (1, 2, 3, 4):
                continue

            if segment != expected_segment:
                expected_segment = 1
                if segment != 1:
                    continue

            for packet_number, packet in enumerate(packets):
                row = (segment - 1) * 30 + packet_number // 2
                column = (packet_number % 2) * 80
                payload = np.frombuffer(bytes(packet[4:]), dtype=">u2")
                frame[row, column:column + 80] = payload

            expected_segment += 1
            if expected_segment == 5:
                self.frame_counter += 1
                self.last_error = ""
                return frame

        self.error_counter += 1
        self.last_error = "VoSPI synchronization timeout"
        self.resync()
        return None

    def close(self):
        try:
            if self.spi is not None:
                self.spi.close()
        except Exception:
            pass


@dataclass
class Detection:
    box: tuple
    center: tuple
    area: int
    temperature: float
    maximum: float


@dataclass
class ThermalTrack:
    box: tuple
    center: tuple
    temperature: float
    maximum: float
    hits: int = 1
    missed: int = 0


def detect_warm_regions(celsius):
    """Return filtered warm regions and their robust surface temperatures."""
    smoothed = cv2.GaussianBlur(celsius, (5, 5), 0)
    scene_median = float(np.median(smoothed))
    percentile_threshold = float(np.percentile(smoothed, 70))
    threshold = max(
        scene_median + MINIMUM_DELTA_ABOVE_MEDIAN_C,
        percentile_threshold,
    )

    mask = (smoothed >= threshold).astype(np.uint8) * 255
    kernel = np.ones((3, 3), dtype=np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    count, labels, stats, centroids = cv2.connectedComponentsWithStats(
        mask,
        connectivity=8,
    )

    detections = []
    for label_index in range(1, count):
        x, y, width, height, area = stats[label_index]
        area = int(area)
        if not (MINIMUM_REGION_AREA_PIXELS <= area <= MAXIMUM_REGION_AREA_PIXELS):
            continue

        region_values = celsius[labels == label_index]
        if region_values.size == 0:
            continue

        # Discard cooler edge pixels so flooring/background within an irregular
        # component does not pull the body-surface estimate downward.
        core_cutoff = float(np.percentile(region_values, 50))
        core_values = region_values[region_values >= core_cutoff]
        if core_values.size == 0:
            core_values = region_values

        robust_temperature = float(np.percentile(
            core_values,
            REGION_TEMPERATURE_PERCENTILE,
        ))
        robust_maximum = float(np.percentile(region_values, 99))
        center_x, center_y = centroids[label_index]

        detections.append(Detection(
            box=(int(x), int(y), int(x + width), int(y + height)),
            center=(float(center_x), float(center_y)),
            area=area,
            temperature=robust_temperature,
            maximum=robust_maximum,
        ))

    detections.sort(key=lambda item: item.area, reverse=True)
    return detections, threshold, mask


class ThermalTracker:
    """Lightweight nearest-centre tracker for separate thermal regions."""

    def __init__(self):
        self.tracks = {}
        self.next_id = 1

    def update(self, detections):
        candidates = []
        for track_id, track in self.tracks.items():
            for detection_index, detection in enumerate(detections):
                distance = float(np.hypot(
                    track.center[0] - detection.center[0],
                    track.center[1] - detection.center[1],
                ))
                if distance <= MAX_TRACK_DISTANCE_PIXELS:
                    candidates.append((distance, track_id, detection_index))

        candidates.sort()
        used_tracks = set()
        used_detections = set()

        for _, track_id, detection_index in candidates:
            if track_id in used_tracks or detection_index in used_detections:
                continue
            detection = detections[detection_index]
            track = self.tracks[track_id]
            track.box = detection.box
            track.center = detection.center
            track.temperature = (
                (1.0 - TEMPERATURE_EMA_ALPHA) * track.temperature
                + TEMPERATURE_EMA_ALPHA * detection.temperature
            )
            track.maximum = detection.maximum
            track.hits += 1
            track.missed = 0
            used_tracks.add(track_id)
            used_detections.add(detection_index)

        for track_id in list(self.tracks):
            if track_id not in used_tracks:
                self.tracks[track_id].missed += 1
                if self.tracks[track_id].missed > MAX_MISSED_FRAMES:
                    del self.tracks[track_id]

        for detection_index, detection in enumerate(detections):
            if detection_index in used_detections:
                continue
            self.tracks[self.next_id] = ThermalTrack(
                box=detection.box,
                center=detection.center,
                temperature=detection.temperature,
                maximum=detection.maximum,
            )
            self.next_id += 1

        return {
            track_id: track
            for track_id, track in self.tracks.items()
            if track.hits >= MINIMUM_TRACK_HITS and track.missed == 0
        }



def main():
    import signal
    import tempfile
    from pathlib import Path
    cv2.setNumThreads(1)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    lock = open(Path(tempfile.gettempdir()) / f'pig-lepton-{os.getuid()}.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    camera = Lepton3()
    frames = queue.Queue(maxsize=1)
    def capture():
        try:
            while not stop.is_set():
                raw = camera.capture()
                if raw is None:
                    print(camera.last_error, flush=True)
                    continue
                item = (time.time(), raw)
                try:
                    frames.get_nowait()
                except queue.Empty:
                    pass
                try:
                    frames.put_nowait(item)
                except queue.Full:
                    pass
        except Exception as error:
            print('Thermal capture failed:', repr(error), flush=True)
            stop.set()
        finally:
            camera.close()
    thread = threading.Thread(target=capture, daemon=True)
    thread.start()
    tracker = ThermalTracker()
    since = {}
    last_ts = 0
    print('Headless thermal worker started', flush=True)
    try:
        while not stop.is_set():
            try:
                stamp, raw = frames.get(timeout=0.5)
            except queue.Empty:
                if time.time() - last_ts > 3:
                    since.clear()
                    publish_controller_metrics(thermal={'valid': False, 'frame_ts': last_ts, 'hottest_c': None})
                continue
            if stamp - last_ts > 3:
                since.clear()
                tracker = ThermalTracker()
            last_ts = stamp
            celsius = raw_to_celsius(raw)
            valid = temperatures_are_plausible(celsius)
            tracks = tracker.update(detect_warm_regions(celsius)[0]) if valid else {}
            low, high = np.percentile(raw, (2, 98))
            gray = np.clip((raw.astype(np.float32)-low)*255/max(float(high-low),1),0,255).astype(np.uint8)
            display = cv2.cvtColor(colour_map(gray), cv2.COLOR_RGB2BGR)
            display = cv2.resize(display, (480,360), interpolation=cv2.INTER_NEAREST)
            active = set()
            for tid, track in tracks.items():
                x1,y1,x2,y2 = [int(v*3) for v in track.box]
                cv2.rectangle(display,(x1,y1),(x2,y2),(255,255,255),1)
                cv2.putText(display,f'Thermal {tid}: {track.temperature:.1f} C',(x1,max(12,y1)),cv2.FONT_HERSHEY_SIMPLEX,.4,(255,255,255),1)
                if track.temperature >= HIGH_TEMPERATURE_THRESHOLD_C:
                    active.add(tid)
                    since.setdefault(tid, stamp)
                    if stamp - since[tid] >= EVENT_CONFIRMATION_SEC:
                        increment_controller_metric('high_temperature')
            since = {tid: value for tid,value in since.items() if tid in active}
            publish_controller_metrics(thermal={'valid': bool(valid and tracks), 'frame_ts': stamp,
                'hottest_c': max((tr.temperature for tr in tracks.values()), default=None)})
            if not valid:
                cv2.putText(display,'Temperature unavailable',(5,20),cv2.FONT_HERSHEY_SIMPLEX,.5,(255,255,255),1)
            save_preview('thermal', display)
    finally:
        stop.set()
        thread.join(timeout=3)
        lock.close()

HIGH_TEMPERATURE_THRESHOLD_C = float(os.environ.get('PIG_HIGH_TEMP_C', '35'))
EVENT_CONFIRMATION_SEC = float(os.environ.get('PIG_CONFIRM_SEC', '1'))
if __name__ == '__main__':
    main()
