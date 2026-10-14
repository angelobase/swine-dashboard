#!/usr/bin/env python3
"""One scheduled capture, run by inlayo8.py as a subprocess every capture interval.

1. Thermal burst: a few Lepton frames, combined with a per-pixel median (less noise than one frame).
2. Webcam burst: discard warm-up frames while auto-exposure settles, then run YOLO on a few frames.
   A pig must appear in at least half of those frames; its posture is the majority vote.
3. Each pig's box is mapped into the thermal image (calibration.json from calibrate.py) and its surface
   temperature is taken from the warm pixels inside it, the same way thermal_worker.py measures a region.
4. Pig IDs come from pig_ids.assign() (placeholder, position-based).

Writes <out>/capture.json and the annotated rgb.jpg / thermal.jpg, then exits, so the cameras, the model and
their memory are released between captures. Camera, model and thermal code are reused from yolo_worker.py
and thermal_worker.py.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pig_ids  # noqa: E402

CAPTURE_DEFAULTS = {'rgb_warmup_frames': 15, 'rgb_frames': 5, 'rgb_frame_spacing_s': 0.3,
                    'thermal_frames': 9, 'thermal_budget_s': 8.0, 'confidence': 0.93}
THERMAL_SIZE = (160, 120)
MIN_WARM_PIXELS = 6
MAX_JPEG_BYTES = 240_000


# ---------------------------------------------------------------- pure helpers (unit-tested without hardware)
def box_iou(a, b):
    iw = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    ih = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = iw * ih
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def cluster_detections(frames, is_lying, min_iou=0.3):
    """frames: one list per burst frame of (box, confidence, class_id, class_name).
    Groups the same pig across frames (pigs barely move within a 2-second burst).
    Keeps a pig only if seen in at least half of the frames. Posture = majority vote; a tie is decided by the
    most confident detection."""
    clusters = []
    for dets in frames:
        pairs = sorted(((box_iou(c['boxes'][-1], d[0]), di, ci) for di, d in enumerate(dets)
                        for ci, c in enumerate(clusters)), reverse=True)
        used_d, used_c = set(), set()
        for iou, di, ci in pairs:
            if iou < min_iou or di in used_d or ci in used_c:
                continue
            used_d.add(di); used_c.add(ci)
            clusters[ci]['boxes'].append([float(v) for v in dets[di][0]]); clusters[ci]['dets'].append(dets[di])
        for di, d in enumerate(dets):
            if di not in used_d:
                clusters.append({'boxes': [[float(v) for v in d[0]]], 'dets': [d]})
    need = max(1, (len(frames) + 1) // 2)
    pigs = []
    for c in clusters:
        if len(c['dets']) < need:
            continue
        votes = sum(1 for d in c['dets'] if is_lying(d[3]))
        best = max(c['dets'], key=lambda d: d[1])
        lying = votes * 2 > len(c['dets']) or (votes * 2 == len(c['dets']) and is_lying(best[3]))
        pigs.append({'box': [float(v) for v in np.median(np.array(c['boxes']), axis=0)], 'lying': bool(lying),
                     'class_name': str(best[3]), 'confidence': round(float(best[1]), 3), 'frames_seen': len(c['dets'])})
    return pigs


def load_calibration(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        m = np.array(data['homography'], dtype=np.float64)
        return m if m.shape == (3, 3) and np.isfinite(m).all() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def map_box(box, homography, rgb_size):
    """RGB box -> integer rectangle in the 160x120 thermal image, or None if it falls outside.
    Without a calibration the two images are assumed to cover the same view (proportional scaling)."""
    x1, y1, x2, y2 = box
    if homography is None:
        sx, sy = THERMAL_SIZE[0] / rgb_size[0], THERMAL_SIZE[1] / rgb_size[1]
        pts = np.array([[x1 * sx, y1 * sy], [x2 * sx, y2 * sy]])
    else:
        corners = np.array([[[x1, y1]], [[x2, y1]], [[x2, y2]], [[x1, y2]]], dtype=np.float64)
        pts = cv2.perspectiveTransform(corners, homography).reshape(-1, 2)
    a = np.clip(np.floor(pts.min(axis=0)), 0, [THERMAL_SIZE[0], THERMAL_SIZE[1]]).astype(int)
    b = np.clip(np.ceil(pts.max(axis=0)), 0, [THERMAL_SIZE[0], THERMAL_SIZE[1]]).astype(int)
    if b[0] - a[0] < 2 or b[1] - a[1] < 2:
        return None
    return [int(a[0]), int(a[1]), int(b[0]), int(b[1])]


def pig_temperature(celsius, rect, delta_c=1.0, percentile=90.0):
    """Same method as thermal_worker.detect_warm_regions: keep pixels warmer than the scene median by delta_c,
    keep the warmer half of those, report their 90th percentile. None when too few warm pixels."""
    if celsius is None or rect is None:
        return None
    x1, y1, x2, y2 = rect
    region = celsius[y1:y2, x1:x2]
    warm = region[region >= float(np.median(celsius)) + delta_c]
    if warm.size < MIN_WARM_PIXELS:
        return None
    core = warm[warm >= np.percentile(warm, 50)]
    value = float(np.percentile(core if core.size else warm, percentile))
    return round(value, 2) if 0.0 <= value <= 60.0 else None


# ---------------------------------------------------------------- hardware
def thermal_burst(cfg, offset_c):
    import fcntl  # Pi/Linux only
    import thermal_worker as tw
    lock = open(Path(tempfile.gettempdir()) / f'pig-lepton-{os.getuid()}.lock', 'w')
    deadline = time.monotonic() + 5.0
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() > deadline:
                lock.close()
                raise RuntimeError('The thermal camera is in use by another program (thermal_worker.py?).')
            time.sleep(0.2)
    camera = tw.Lepton3()
    try:
        frames, end = [], time.monotonic() + float(cfg['thermal_budget_s'])
        while len(frames) < int(cfg['thermal_frames']) and time.monotonic() < end:
            raw = camera.capture()
            if raw is None:
                continue
            celsius = tw.raw_to_celsius(raw)
            if tw.temperatures_are_plausible(celsius):
                frames.append(celsius)
        if not frames:
            return None, camera.last_error or 'No valid radiometric (TLinear) frame.'
        return (np.median(np.stack(frames), axis=0) + float(offset_c)).astype(np.float32), None
    finally:
        camera.close()
        lock.close()


def rgb_burst(cfg, model_path):
    os.environ['PIG_MODEL_PATH'] = str(model_path)
    import yolo_worker as yw
    yw.CONFIDENCE_THRESHOLD = float(cfg['confidence'])
    detector = yw.YoloOnnxDetector(Path(model_path))  # load the model before opening the camera
    camera = cv2.VideoCapture(yw.CAMERA_INDEX, cv2.CAP_V4L2)
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, yw.CAMERA_WIDTH)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, yw.CAMERA_HEIGHT)
    camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not camera.isOpened():
        raise RuntimeError(f'Could not open /dev/video{yw.CAMERA_INDEX}')
    try:
        for _ in range(int(cfg['rgb_warmup_frames'])):
            camera.read()
        frames, last = [], None
        for _ in range(int(cfg['rgb_frames'])):
            ok, frame = camera.read()
            if ok and frame is not None:
                detections, _ = detector.detect(frame)
                frames.append(detections)
                last = frame
            time.sleep(float(cfg['rgb_frame_spacing_s']))
        if last is None:
            raise RuntimeError('The webcam returned no frames.')
        return last, frames, yw.is_laying_class
    finally:
        camera.release()


# ---------------------------------------------------------------- images
def colour(pid):
    rng = np.random.default_rng(int(pid) * 1009)
    return tuple(int(v) for v in rng.integers(70, 256, size=3))


def text(img, s, org, scale=0.5, col=(255, 255, 255)):
    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, col, 1, cv2.LINE_AA)


def label(p):
    t = 'no temp' if p['temp_c'] is None else f"{p['temp_c']:.1f} C"
    return f"Pig {p['id']} {'lying' if p['lying'] else 'not lying'} {t}"


def annotate_rgb(frame, pigs, caption):
    img = frame.copy()
    for p in pigs:
        x1, y1, x2, y2 = (int(v) for v in p['box'])
        cv2.rectangle(img, (x1, y1), (x2, y2), colour(p['id']), 2)
        text(img, label(p), (x1, max(16, y1 - 6)), 0.5, colour(p['id']))
    text(img, caption, (8, 20), 0.5)
    return img


def annotate_thermal(celsius, pigs, caption):
    if celsius is None:
        img = np.zeros((360, 480, 3), dtype=np.uint8)
        text(img, 'Temperature unavailable', (10, 40), 0.6)
    else:
        import thermal_worker as tw
        lo, hi = np.percentile(celsius, (2, 98))
        gray = np.clip((celsius - lo) * 255.0 / max(float(hi - lo), 0.1), 0, 255).astype(np.uint8)
        img = cv2.resize(cv2.cvtColor(tw.colour_map(gray), cv2.COLOR_RGB2BGR), (480, 360), interpolation=cv2.INTER_NEAREST)
        for p in pigs:
            if p['thermal_rect']:
                x1, y1, x2, y2 = (v * 3 for v in p['thermal_rect'])
                cv2.rectangle(img, (x1, y1), (x2, y2), (255, 255, 255), 1)
                text(img, f"Pig {p['id']}: {'--' if p['temp_c'] is None else format(p['temp_c'], '.1f')} C", (x1, max(14, y1 - 4)), 0.42)
    text(img, caption, (8, 352), 0.45)
    return img


def write_jpeg(path, img):
    for quality in (80, 70, 60, 50, 40):
        ok, enc = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if ok and enc.nbytes <= MAX_JPEG_BYTES:
            tmp = path.with_suffix('.tmp')
            tmp.write_bytes(enc.tobytes())
            os.replace(tmp, path)
            return path.name
    return None


# ---------------------------------------------------------------- main
def run(out, cfg, model, previous, calibration, offset_c, caption):
    result = {'ok': False, 'error': None, 'thermal_error': None, 'started': time.time(), 'pigs': [],
              'thermal_valid': False, 'calibrated': calibration is not None, 'images': {}, 'next_id': previous['next_id']}
    try:
        celsius, result['thermal_error'] = thermal_burst(cfg, offset_c)
    except Exception as error:  # a thermal failure still leaves postures usable
        celsius, result['thermal_error'] = None, f'{type(error).__name__}: {error}'
    result['thermal_valid'] = celsius is not None
    frame, frames, is_lying = rgb_burst(cfg, model)
    h, w = frame.shape[:2]
    pigs = cluster_detections(frames, is_lying)
    ids, result['next_id'] = pig_ids.assign(previous['pigs'], [p['box'] for p in pigs], previous['next_id'], (w, h))
    for pig, pid in zip(pigs, ids):
        pig['id'] = pid
        pig['thermal_rect'] = map_box(pig['box'], calibration, (w, h)) if celsius is not None else None
        pig['temp_c'] = pig_temperature(celsius, pig['thermal_rect'])
    pigs.sort(key=lambda p: p['id'])
    result.update(pigs=pigs, frame_size=[w, h], burst_frames=len(frames), ok=True)
    result['images'] = {k: v for k, v in (('rgb', write_jpeg(out / 'rgb.jpg', annotate_rgb(frame, pigs, caption))),
                                          ('thermal', write_jpeg(out / 'thermal.jpg', annotate_thermal(celsius, pigs, caption)))) if v}
    return result


def main():
    ap = argparse.ArgumentParser(description='One scheduled capture (run by inlayo8.py).')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--config', type=Path, default=HERE / 'prototype.json')
    ap.add_argument('--model', type=Path, default=HERE / 'YOLOv8A_SD_best.onnx')
    ap.add_argument('--previous', type=Path, help='JSON {"next_id": n, "pigs": [{"id", "box"}]}')
    ap.add_argument('--caption', default='')
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('PIG_PREVIEW_DIR', str(args.out))  # required by preview_support on import; unused here
    os.environ.setdefault('PIG_YOLO_THREADS', '3')          # nothing else runs during a capture
    user = json.loads(args.config.read_text(encoding='utf-8')) if args.config.is_file() else {}
    cfg = {**CAPTURE_DEFAULTS, **(user.get('capture') or {})}
    thermal = user.get('thermal') or {}
    calibration = load_calibration(HERE / thermal.get('calibration_file', 'calibration.json'))
    previous = {'next_id': 1, 'pigs': []}
    if args.previous and args.previous.is_file():
        previous.update(json.loads(args.previous.read_text(encoding='utf-8')))
    try:
        result = run(args.out, cfg, args.model, previous, calibration, float(thermal.get('offset_c', 0.0)), args.caption)
    except Exception as error:
        result = {'ok': False, 'error': f'{type(error).__name__}: {error}', 'pigs': [], 'next_id': previous['next_id']}
    result['finished'] = time.time()
    tmp = args.out / 'capture.tmp'
    tmp.write_text(json.dumps(result), encoding='utf-8')
    os.replace(tmp, args.out / 'capture.json')
    print(json.dumps({k: result.get(k) for k in ('ok', 'error', 'thermal_error')}), flush=True)


if __name__ == '__main__':
    main()
