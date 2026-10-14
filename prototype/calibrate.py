#!/usr/bin/env python3
"""One-time RGB-to-thermal calibration. Needs a screen on the Pi (HDMI or VNC); run it with the cameras mounted
exactly as they will stay for the study. Redo it if either camera is moved.

1. Place 4 to 8 small warm objects (mugs or bottles of hot water) spread across the pen floor, including near the
   corners, where both cameras can see them.
2. Run:  python3 calibrate.py        (inlayo8.py must not be capturing at the same time)
3. The webcam image is on the left and the thermal image on the right. Click the centre of an object on the
   LEFT, then the same object on the RIGHT. Repeat for every object.
     u = undo the last click    r = take new pictures    s = save    q = quit without saving
4. calibration.json is written next to this script and used by every capture from then on.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import capture_once as co  # noqa: E402

PANEL_W, PANEL_H = 640, 480


def grab(model_cfg):
    celsius, err = co.thermal_burst(model_cfg, 0.0)
    if celsius is None:
        raise RuntimeError(f'Thermal camera: {err}')
    import yolo_worker as yw  # only for the camera settings
    cam = cv2.VideoCapture(yw.CAMERA_INDEX, cv2.CAP_V4L2)
    cam.set(cv2.CAP_PROP_FRAME_WIDTH, yw.CAMERA_WIDTH)
    cam.set(cv2.CAP_PROP_FRAME_HEIGHT, yw.CAMERA_HEIGHT)
    try:
        frame = None
        for _ in range(int(model_cfg['rgb_warmup_frames']) + 1):
            ok, f = cam.read()
            if ok:
                frame = f
    finally:
        cam.release()
    if frame is None:
        raise RuntimeError('Webcam returned no frame.')
    return frame, celsius


def thermal_view(celsius):
    lo, hi = np.percentile(celsius, (2, 98))
    gray = np.clip((celsius - lo) * 255.0 / max(float(hi - lo), 0.1), 0, 255).astype(np.uint8)
    return cv2.resize(cv2.applyColorMap(gray, cv2.COLORMAP_INFERNO), (PANEL_W, PANEL_H), interpolation=cv2.INTER_NEAREST)


def fit(rgb_pts, th_pts):
    src, dst = np.array(rgb_pts, dtype=np.float64), np.array(th_pts, dtype=np.float64)
    h, mask = cv2.findHomography(src, dst, cv2.RANSAC, 3.0) if len(src) >= 5 else cv2.findHomography(src, dst, 0)
    if h is None:
        return None, None
    proj = cv2.perspectiveTransform(src.reshape(-1, 1, 2), h).reshape(-1, 2)
    return h, float(np.sqrt(((proj - dst) ** 2).sum(axis=1)).mean())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', type=Path, default=HERE / 'prototype.json')
    ap.add_argument('--model', type=Path, default=HERE / 'YOLOv8A_SD_best.onnx')
    args = ap.parse_args()
    import os
    os.environ.setdefault('PIG_MODEL_PATH', str(args.model))
    os.environ.setdefault('PIG_PREVIEW_DIR', str(HERE / 'camera_logs' / 'preview'))
    user = json.loads(args.config.read_text(encoding='utf-8')) if args.config.is_file() else {}
    cfg = {**co.CAPTURE_DEFAULTS, **(user.get('capture') or {})}
    out = HERE / (user.get('thermal') or {}).get('calibration_file', 'calibration.json')

    frame, celsius = grab(cfg)
    rgb_size = (frame.shape[1], frame.shape[0])
    rgb_pts, th_pts = [], []

    def on_click(event, x, y, *_):
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        if x < PANEL_W and len(rgb_pts) == len(th_pts):
            rgb_pts.append((x * rgb_size[0] / PANEL_W, y * rgb_size[1] / PANEL_H))
        elif x >= PANEL_W and len(rgb_pts) == len(th_pts) + 1:
            th_pts.append(((x - PANEL_W) * co.THERMAL_SIZE[0] / PANEL_W, y * co.THERMAL_SIZE[1] / PANEL_H))

    cv2.namedWindow('calibrate')
    cv2.setMouseCallback('calibrate', on_click)
    while True:
        left = cv2.resize(frame, (PANEL_W, PANEL_H))
        right = thermal_view(celsius)
        for i, (x, y) in enumerate(rgb_pts):
            p = (int(x * PANEL_W / rgb_size[0]), int(y * PANEL_H / rgb_size[1]))
            cv2.circle(left, p, 6, (0, 255, 0), 2); cv2.putText(left, str(i + 1), (p[0] + 8, p[1]), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        for i, (x, y) in enumerate(th_pts):
            p = (int(x * PANEL_W / co.THERMAL_SIZE[0]), int(y * PANEL_H / co.THERMAL_SIZE[1]))
            cv2.circle(right, p, 6, (0, 255, 0), 2); cv2.putText(right, str(i + 1), (p[0] + 8, p[1]), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        side = 'webcam (left)' if len(rgb_pts) == len(th_pts) else 'thermal (right)'
        canvas = np.hstack([left, right])
        cv2.putText(canvas, f'Pairs: {len(th_pts)}. Next click: {side}. u undo, r retake, s save, q quit', (10, PANEL_H - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
        cv2.imshow('calibrate', canvas)
        key = cv2.waitKey(50) & 0xFF
        if key == ord('q'):
            print('Not saved.'); break
        if key == ord('u'):
            if len(rgb_pts) > len(th_pts):
                rgb_pts.pop()
            elif th_pts:
                th_pts.pop()
        if key == ord('r'):
            frame, celsius = grab(cfg); rgb_pts.clear(); th_pts.clear()
        if key == ord('s'):
            if len(th_pts) < 4 or len(rgb_pts) != len(th_pts):
                print('Need at least 4 complete pairs.'); continue
            h, err = fit(rgb_pts, th_pts)
            if h is None:
                print('Could not fit these points. Spread the objects out more and try again.'); continue
            out.write_text(json.dumps({'created': time.strftime('%Y-%m-%d %H:%M:%S'), 'rgb_size': list(rgb_size),
                                       'thermal_size': list(co.THERMAL_SIZE), 'pairs': len(th_pts),
                                       'mean_error_thermal_px': round(err, 2), 'homography': h.tolist()}, indent=2), encoding='utf-8')
            print(f'Saved {out.name}: {len(th_pts)} pairs, mean error {err:.2f} thermal pixels '
                  f'({"good" if err <= 2 else "check the clicks; redo if pigs are small in the image"}).')
            break
    cv2.destroyAllWindows()


if __name__ == '__main__':
    main()
