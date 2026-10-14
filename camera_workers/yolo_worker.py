from preview_support import save_preview
#!/usr/bin/env python3
"""Responsive pig detector/tracker with model-based laying timer.

Designed for Raspberry Pi CPU deployment. Uses ONNX Runtime directly.
The ONNX model must contain the behavior classes used by the project.
Each tracked Pig ID has an independent laying timer.
"""

from dataclasses import dataclass
from pathlib import Path
import json
import fcntl
import json
import os
import queue
import threading
import time

import cv2
import numpy as np
import onnxruntime as ort


MODEL_PATH = Path(os.environ["PIG_MODEL_PATH"])
CAMERA_INDEX = 0
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480

CONFIDENCE_THRESHOLD = 0.93
NMS_IOU_THRESHOLD = 0.45
MINIMUM_BOX_AREA = 1500

TRACK_IOU_THRESHOLD = 0.12
TRACK_CENTER_DISTANCE = 1.25
TRACK_MAX_MISSED_INFERENCES = 12
TRACK_VISIBLE_MISSES = 3
TRACK_TRAIL_LENGTH = 20
TRACK_MINIMUM_HITS = 2
COUNT_CONFIRMATION_UPDATES = 2

# Persistent stable-ID layer.
MAX_LOST_SEC = 10.0
BASE_GATE_PX = 150.0
SPEED_PX_PER_SEC = 120.0
MAX_GATE_PX = 700.0
MAX_SIZE_RATIO = 2.5


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
GAP_RESET_SEC = 2.0

# Names are read from the ONNX metadata when available. These aliases let the
# timer recognize common labels without requiring an exact spelling.
LAYING_ALIASES = {
    "laying", "lying", "lie", "laid", "down", "lying_down", "laying_down"
}

# In the current model, the abnormal-behavior result is the laying/lying
# behavior already used by the timer. Keep this mapping here so it is easy
# to extend if the ONNX model later adds more abnormal classes.
ABNORMAL_BEHAVIOR_ALIASES = set(LAYING_ALIASES)

# A condition must remain detected this long before it is counted.
EVENT_CONFIRMATION_SEC = 1.0


def normalize_class_name(name):
    return str(name).strip().lower().replace("-", "_").replace(" ", "_")


def is_laying_class(name):
    normalized = normalize_class_name(name)
    return normalized in LAYING_ALIASES or "lying" in normalized or "laying" in normalized


def is_abnormal_behavior_class(name):
    normalized = normalize_class_name(name)
    return (
        normalized in ABNORMAL_BEHAVIOR_ALIASES
        or is_laying_class(normalized)
    )


def stable_colour(track_id):
    generator = np.random.default_rng(track_id * 1009)
    colour = generator.integers(70, 256, size=3, dtype=np.uint8)
    return tuple(int(value) for value in colour)


def letterbox(image, target_width, target_height):
    height, width = image.shape[:2]
    scale = min(target_width / width, target_height / height)
    resized_width = int(round(width * scale))
    resized_height = int(round(height * scale))
    resized = cv2.resize(image, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
    pad_x = (target_width - resized_width) // 2
    pad_y = (target_height - resized_height) // 2
    canvas = np.full((target_height, target_width, 3), 114, dtype=np.uint8)
    canvas[pad_y:pad_y + resized_height, pad_x:pad_x + resized_width] = resized
    return canvas, scale, pad_x, pad_y


def box_iou(first, second):
    ax1, ay1, ax2, ay2 = first
    bx1, by1, bx2, by2 = second
    intersection_width = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    intersection_height = max(0.0, min(ay2, by2) - max(ay1, by1))
    intersection = intersection_width * intersection_height
    first_area = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    second_area = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = first_area + second_area - intersection
    return intersection / union if union > 0.0 else 0.0


def normalized_center_distance(first, second):
    first_center = np.array([(first[0] + first[2]) / 2.0, (first[1] + first[3]) / 2.0])
    second_center = np.array([(second[0] + second[2]) / 2.0, (second[1] + second[3]) / 2.0])
    first_size = max(first[2] - first[0], first[3] - first[1])
    second_size = max(second[2] - second[0], second[3] - second[1])
    reference_size = max((first_size + second_size) / 2.0, 1.0)
    return float(np.linalg.norm(first_center - second_center) / reference_size)


@dataclass
class Track:
    box: np.ndarray
    confidence: float
    class_id: int
    class_name: str
    missed: int
    trail: list
    hits: int
    laying_since: float | None = None
    laying_seconds: float = 0.0


class StableIDMapper:
    """Re-attach raw tracker IDs to persistent pig IDs after tracker resets."""

    def __init__(self, fps_hint=10.0):
        self.fps_hint = max(float(fps_hint), 1.0)
        self.raw_to_stable = {}
        self.last = {}  # stable_id -> center/w/h/monotonic time
        self.next_id = 1

    @staticmethod
    def _geom(det):
        x1, y1, x2, y2 = [float(v) for v in det["box"]]
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0), max(x2 - x1, 1.0), max(y2 - y1, 1.0)

    def _cost(self, det, sid, now):
        (cx, cy), w, h = self._geom(det)
        prev = self.last[sid]
        px, py = prev["center"]
        dt = max(0.0, now - prev["time"])
        gate = min(max(BASE_GATE_PX, 0.8 * max(w, h)) + SPEED_PX_PER_SEC * dt, MAX_GATE_PX)
        distance = float(np.hypot(cx - px, cy - py))
        if distance > gate:
            return None
        ratio = (w * h) / max(prev["w"] * prev["h"], 1.0)
        if ratio > MAX_SIZE_RATIO or ratio < 1.0 / MAX_SIZE_RATIO:
            return None
        size_delta = abs(np.log(max(ratio, 1e-6))) / np.log(MAX_SIZE_RATIO)
        return distance / gate + 0.15 * size_delta

    def update(self, detections, now):
        claimed = set()
        pending = []
        for det in detections:
            raw = int(det["track_id"])
            sid = self.raw_to_stable.get(raw)
            if raw != -1 and sid is not None and sid not in claimed:
                claimed.add(sid)
                det["raw_id"] = raw
                det["track_id"] = sid
            else:
                pending.append(det)

        candidates = [sid for sid, info in self.last.items()
                      if sid not in claimed and 0.0 <= now - info["time"] <= MAX_LOST_SEC]
        matches = {}
        if pending and candidates:
            inf = 1e6
            cost = np.full((len(pending), len(candidates)), inf, dtype=np.float64)
            for i, det in enumerate(pending):
                for j, sid in enumerate(candidates):
                    value = self._cost(det, sid, now)
                    if value is not None:
                        cost[i, j] = value
            # Greedy minimum-cost one-to-one assignment keeps this deployment SciPy-free.
            # For the small number of pigs expected here, this is lightweight and stable.
            edges = []
            for row in range(cost.shape[0]):
                for col in range(cost.shape[1]):
                    if cost[row, col] < inf:
                        edges.append((float(cost[row, col]), row, col))
            edges.sort(key=lambda item: item[0])
            used_rows = set()
            used_cols = set()
            for _, row, col in edges:
                if row in used_rows or col in used_cols:
                    continue
                matches[row] = candidates[col]
                used_rows.add(row)
                used_cols.add(col)

        for i, det in enumerate(pending):
            raw = int(det["track_id"])
            sid = matches.get(i)
            if sid is None:
                sid = self.next_id
                self.next_id += 1
            self.raw_to_stable[raw] = sid
            claimed.add(sid)
            det["raw_id"] = raw
            det["track_id"] = sid

        for det in detections:
            sid = int(det["track_id"])
            if sid == -1:
                continue
            (cx, cy), w, h = self._geom(det)
            previous = self.last.get(sid)
            det["gap_seconds"] = max(0.0, now - previous["time"]) if previous else None
            self.last[sid] = {"center": (cx, cy), "w": w, "h": h, "time": now}
        return detections


class IoUTracker:
    def __init__(self):
        self.tracks = {}
        self.next_id = 1

    def _set_behavior(self, track, class_id, class_name, now):
        was_laying = is_laying_class(track.class_name)
        now_laying = is_laying_class(class_name)
        track.class_id = class_id
        track.class_name = class_name
        if now_laying:
            if not was_laying or track.laying_since is None:
                track.laying_since = now
            track.laying_seconds = max(0.0, now - track.laying_since)
        else:
            track.laying_since = None
            track.laying_seconds = 0.0

    def update(self, detections, now):
        # detections contains (box_xyxy, confidence, class_id, class_name).
        candidates = []
        for track_id, track in self.tracks.items():
            for detection_index, (box, _, _, _) in enumerate(detections):
                overlap = box_iou(track.box, box)
                center_distance = normalized_center_distance(track.box, box)
                if overlap >= TRACK_IOU_THRESHOLD or center_distance <= TRACK_CENTER_DISTANCE:
                    score = overlap + 0.25 / (1.0 + center_distance)
                    candidates.append((score, track_id, detection_index))

        candidates.sort(reverse=True)
        used_tracks = set()
        used_detections = set()

        for _, track_id, detection_index in candidates:
            if track_id in used_tracks or detection_index in used_detections:
                continue
            box, confidence, class_id, class_name = detections[detection_index]
            track = self.tracks[track_id]
            track.box = box
            track.confidence = confidence
            track.missed = 0
            track.hits += 1
            center = (int((box[0] + box[2]) / 2), int((box[1] + box[3]) / 2))
            track.trail.append(center)
            track.trail = track.trail[-TRACK_TRAIL_LENGTH:]
            self._set_behavior(track, class_id, class_name, now)
            used_tracks.add(track_id)
            used_detections.add(detection_index)

        for track_id, track in list(self.tracks.items()):
            if track_id not in used_tracks:
                track.missed += 1
                if track.missed > TRACK_MAX_MISSED_INFERENCES:
                    del self.tracks[track_id]

        for detection_index, (box, confidence, class_id, class_name) in enumerate(detections):
            if detection_index in used_detections:
                continue
            center = (int((box[0] + box[2]) / 2), int((box[1] + box[3]) / 2))
            laying_since = now if is_laying_class(class_name) else None
            self.tracks[self.next_id] = Track(
                box=box,
                confidence=confidence,
                class_id=class_id,
                class_name=class_name,
                missed=0,
                trail=[center],
                hits=1,
                laying_since=laying_since,
                laying_seconds=0.0,
            )
            self.next_id += 1

        visible = {}
        for track_id, track in self.tracks.items():
            if track.hits >= TRACK_MINIMUM_HITS and track.missed <= TRACK_VISIBLE_MISSES:
                if is_laying_class(track.class_name) and track.laying_since is not None:
                    track.laying_seconds = max(0.0, now - track.laying_since)
                visible[track_id] = track
        return visible


class YoloOnnxDetector:
    def __init__(self, model_path, intra_threads=4):
        if not model_path.exists():
            raise FileNotFoundError(
                f"Model not found: {model_path}\n"
                "Place best.onnx beside this file, or use --model PATH."
            )

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.intra_op_num_threads = int(os.environ.get("PIG_YOLO_THREADS", "2"))
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        options.inter_op_num_threads = 1

        self.session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        model_input = self.session.get_inputs()[0]
        self.input_name = model_input.name
        shape = model_input.shape
        if not isinstance(shape[2], int) or not isinstance(shape[3], int):
            raise RuntimeError(f"Unsupported dynamic model input shape: {shape}")
        self.input_height = int(shape[2])
        self.input_width = int(shape[3])
        self.output_names = [output.name for output in self.session.get_outputs()]
        self.class_names = self._load_class_names()

        print(f"Loaded: {model_path}")
        print(f"Actual ONNX input: {self.input_width}x{self.input_height}")
        print(f"Model classes: {self.class_names}")
        laying = [name for name in self.class_names if is_laying_class(name)]
        if laying:
            print(f"Laying class(es): {laying}")
        else:
            print("WARNING: no laying class was found in ONNX class names.")

    def _load_class_names(self):
        metadata = self.session.get_modelmeta().custom_metadata_map or {}
        raw_names = metadata.get("names") or metadata.get("classes") or metadata.get("class_names")
        if raw_names:
            try:
                parsed = json.loads(raw_names)
                if isinstance(parsed, dict):
                    return [str(parsed[str(i)] if str(i) in parsed else parsed.get(i, i)) for i in range(len(parsed))]
                if isinstance(parsed, list):
                    return [str(x) for x in parsed]
            except Exception:
                pass
            if "," in raw_names:
                return [part.strip() for part in raw_names.split(",")]

        env_names = os.environ.get("PIG_CLASS_NAMES", "").strip()
        if env_names:
            return [part.strip() for part in env_names.split(",") if part.strip()]

        # Infer class count from standard YOLO segmentation output
        # ([1, 4 + classes + 32, anchors]) when metadata is absent.
        try:
            output_shape = self.session.get_outputs()[0].shape
            dimensions = [int(d) for d in output_shape if isinstance(d, int)]
            candidate_dim = next((d for d in dimensions if 36 < d < 1000), None)
            inferred_count = candidate_dim - 36 if candidate_dim is not None else 1
            if inferred_count == 1:
                return ["pig"]
            if inferred_count == 2:
                return ["standing", "laying"]
            return [f"class_{i}" for i in range(max(1, inferred_count))]
        except Exception:
            return ["standing", "laying"]

    def detect(self, frame):
        prepared, scale, pad_x, pad_y = letterbox(frame, self.input_width, self.input_height)
        rgb = cv2.cvtColor(prepared, cv2.COLOR_BGR2RGB)
        tensor = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None].astype(np.float32) / 255.0)

        started = time.perf_counter()
        outputs = self.session.run(self.output_names, {self.input_name: tensor})
        inference_ms = (time.perf_counter() - started) * 1000.0
        prediction = np.squeeze(outputs[0])
        if prediction.ndim != 2:
            raise RuntimeError(f"Unexpected detection output: {outputs[0].shape}")
        if prediction.shape[0] < prediction.shape[1]:
            prediction = prediction.T

        class_count = len(self.class_names)
        if prediction.shape[1] < 4 + class_count:
            raise RuntimeError(
                f"Model output has {prediction.shape[1]} values per candidate, "
                f"but {class_count} classes were configured. Check PIG_CLASS_NAMES."
            )

        boxes_xywh = prediction[:, :4]
        class_scores = prediction[:, 4:4 + class_count]
        class_ids = np.argmax(class_scores, axis=1)
        scores = class_scores[np.arange(len(class_scores)), class_ids]
        selected = scores >= CONFIDENCE_THRESHOLD
        boxes_xywh = boxes_xywh[selected]
        scores = scores[selected]
        class_ids = class_ids[selected]

        frame_height, frame_width = frame.shape[:2]
        nms_boxes = []
        converted_boxes = []
        converted_scores = []
        converted_classes = []

        for box, score, class_id in zip(boxes_xywh, scores, class_ids):
            center_x, center_y, width, height = (float(value) for value in box)
            x1 = (center_x - width / 2.0 - pad_x) / scale
            y1 = (center_y - height / 2.0 - pad_y) / scale
            x2 = (center_x + width / 2.0 - pad_x) / scale
            y2 = (center_y + height / 2.0 - pad_y) / scale
            x1 = float(np.clip(x1, 0, frame_width - 1))
            y1 = float(np.clip(y1, 0, frame_height - 1))
            x2 = float(np.clip(x2, 0, frame_width - 1))
            y2 = float(np.clip(y2, 0, frame_height - 1))
            box_width = x2 - x1
            box_height = y2 - y1
            if box_width * box_height < MINIMUM_BOX_AREA:
                continue
            nms_boxes.append([int(x1), int(y1), int(box_width), int(box_height)])
            converted_boxes.append(np.array([x1, y1, x2, y2], dtype=np.float32))
            converted_scores.append(float(score))
            converted_classes.append(int(class_id))

        if not nms_boxes:
            return [], inference_ms

        # NMS per class so standing/laying detections do not suppress each other.
        kept_global = []
        for class_id in sorted(set(converted_classes)):
            indices = [i for i, value in enumerate(converted_classes) if value == class_id]
            class_boxes = [nms_boxes[i] for i in indices]
            class_scores_for_nms = [converted_scores[i] for i in indices]
            kept = cv2.dnn.NMSBoxes(
                class_boxes, class_scores_for_nms,
                CONFIDENCE_THRESHOLD, NMS_IOU_THRESHOLD,
            )
            for local_index in np.asarray(kept).reshape(-1) if len(kept) else []:
                kept_global.append(indices[int(local_index)])

        detections = [
            (
                converted_boxes[i],
                converted_scores[i],
                converted_classes[i],
                self.class_names[converted_classes[i]] if converted_classes[i] < len(self.class_names) else f"class_{converted_classes[i]}",
            )
            for i in kept_global
        ]
        return detections, inference_ms


def put_outlined_text(frame, text, position, scale, colour, thickness=1):
    cv2.putText(frame, text, position, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(frame, text, position, cv2.FONT_HERSHEY_SIMPLEX, scale, colour, thickness, cv2.LINE_AA)


def draw_tracks(frame, tracks):
    for track_id, track in tracks.items():
        x1, y1, x2, y2 = (int(value) for value in track.box)
        colour = stable_colour(track_id)
        cv2.rectangle(frame, (x1, y1), (x2, y2), colour, 2)

        if is_abnormal_behavior_class(track.class_name):
            behavior_text = f"ABNORMAL: {track.class_name.upper()}  {track.laying_seconds:.2f}s"
        else:
            behavior_text = track.class_name.upper()

        put_outlined_text(
            frame,
            f"Pig ID {track_id}  {track.confidence:.2f}",
            (x1, max(22, y1 - 28)),
            0.52,
            colour,
            2,
        )
        put_outlined_text(
            frame,
            behavior_text,
            (x1, max(44, y1 - 7)),
            0.50,
            colour,
            2,
        )


def replace_queue_item(target_queue, value):
    try:
        target_queue.get_nowait()
    except queue.Empty:
        pass
    try:
        target_queue.put_nowait(value)
    except queue.Full:
        pass


def inference_worker(detector, input_queue, output_queue, stop_event):
    raw_tracker = IoUTracker()
    stable_mapper = StableIDMapper(fps_hint=10.0)
    behavior_state = {}

    while not stop_event.is_set():
        try:
            frame = input_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        try:
            now = time.perf_counter()
            detections, inference_ms = detector.detect(frame)
            raw_tracks = raw_tracker.update(detections, now)
            mapper_detections = []
            for raw_id, track in raw_tracks.items():
                mapper_detections.append({
                    "track_id": raw_id, "box": track.box,
                    "confidence": track.confidence, "class_id": track.class_id,
                    "class_name": track.class_name, "hits": track.hits,
                    "trail": list(track.trail), "missed": track.missed,
                })

            stable_detections = stable_mapper.update(mapper_detections, now)
            snapshot = {}
            for det in stable_detections:
                stable_id = int(det["track_id"])
                class_name = det["class_name"]
                previous = behavior_state.get(stable_id)
                gap = det.get("gap_seconds")
                is_laying = is_laying_class(class_name)

                if previous is None or (gap is not None and gap > GAP_RESET_SEC):
                    laying_since = now if is_laying else None
                elif is_laying:
                    laying_since = previous["laying_since"] or now
                else:
                    laying_since = None

                laying_seconds = max(0.0, now - laying_since) if laying_since is not None and is_laying else 0.0
                behavior_state[stable_id] = {"laying_since": laying_since, "last_seen": now}
                box = np.asarray(det["box"], dtype=np.float32)
                snapshot[stable_id] = Track(
                    box=box.copy(), confidence=float(det["confidence"]),
                    class_id=int(det["class_id"]), class_name=class_name, missed=0,
                    trail=list(det["trail"]), hits=int(det["hits"]),
                    laying_since=laying_since, laying_seconds=laying_seconds,
                )

            cutoff = now - MAX_LOST_SEC
            for stable_id, state in list(behavior_state.items()):
                if state["last_seen"] < cutoff and stable_id not in snapshot:
                    del behavior_state[stable_id]

            replace_queue_item(output_queue, (snapshot, inference_ms, time.perf_counter(), None))
        except Exception as error:
            replace_queue_item(output_queue, ({}, 0.0, time.perf_counter(), str(error)))
            stop_event.set()


def main():
    cv2.setUseOptimized(True)
    cv2.setNumThreads(1)

    detector = YoloOnnxDetector(MODEL_PATH, intra_threads=int(os.environ.get("PIG_YOLO_THREADS", "1")))
    camera = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not camera.isOpened():
        raise RuntimeError(f"Could not open /dev/video{CAMERA_INDEX}")

    input_queue = queue.Queue(maxsize=1)
    output_queue = queue.Queue(maxsize=1)
    stop_event = threading.Event()
    worker = threading.Thread(target=inference_worker, args=(detector, input_queue, output_queue, stop_event), daemon=True)
    worker.start()

    latest_tracks = {}
    latest_inference_ms = 0.0
    latest_result_time = 0.0
    last_error = None
    stable_count = 0
    pending_count = None
    pending_count_updates = 0
    submitted_first_frame = False
    previous_display_time = time.perf_counter()
    display_fps = 0.0
    counted_abnormal_ids = set()
    abnormal_since = {}

    import signal
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    print("Headless webcam worker started", flush=True)

    try:
        while not stop_event.is_set():
            loop_started = time.monotonic()
            ok, frame = camera.read()
            if not ok or frame is None:
                time.sleep(0.02)
                continue

            if input_queue.empty():
                replace_queue_item(input_queue, frame.copy())
                submitted_first_frame = True

            try:
                while True:
                    latest_tracks, latest_inference_ms, latest_result_time, last_error = output_queue.get_nowait()
                    candidate_count = len(latest_tracks)
                    if candidate_count == stable_count:
                        pending_count = None
                        pending_count_updates = 0
                    elif candidate_count == pending_count:
                        pending_count_updates += 1
                    else:
                        pending_count = candidate_count
                        pending_count_updates = 1
                    if pending_count_updates >= COUNT_CONFIRMATION_UPDATES:
                        stable_count = candidate_count
                        pending_count = None
                        pending_count_updates = 0
            except queue.Empty:
                pass

            if last_error or not latest_result_time or time.perf_counter() - latest_result_time > 3.0:
                latest_tracks = {}
                abnormal_since.clear()
                counted_abnormal_ids.clear()
            display = frame.copy()
            draw_tracks(display, latest_tracks)

            now = time.perf_counter()
            instant_fps = 1.0 / max(now - previous_display_time, 1e-6)
            previous_display_time = now
            display_fps = instant_fps if display_fps == 0.0 else 0.9 * display_fps + 0.1 * instant_fps

            detection_fps = 1000.0 / latest_inference_ms if latest_inference_ms > 0.0 else 0.0
            result_age_ms = (now - latest_result_time) * 1000.0 if latest_result_time > 0.0 else 0.0
            laying_count = sum(1 for track in latest_tracks.values() if is_laying_class(track.class_name))
            abnormal_behavior_count = sum(
                1 for track in latest_tracks.values()
                if is_abnormal_behavior_class(track.class_name)
            )

            current_abnormal_ids = set()
            for stable_id, track in latest_tracks.items():
                if not is_abnormal_behavior_class(track.class_name):
                    abnormal_since.pop(stable_id, None)
                    counted_abnormal_ids.discard(stable_id)
                    continue
                current_abnormal_ids.add(stable_id)
                if stable_id not in abnormal_since:
                    abnormal_since[stable_id] = now
                if (
                    stable_id not in counted_abnormal_ids
                    and now - abnormal_since[stable_id] >= EVENT_CONFIRMATION_SEC
                ):
                    increment_controller_metric("abnormal_behavior", 1)
                    counted_abnormal_ids.add(stable_id)

            for stale_id in set(abnormal_since) - current_abnormal_ids:
                abnormal_since.pop(stale_id, None)
            for stale_id in set(counted_abnormal_ids) - current_abnormal_ids:
                counted_abnormal_ids.discard(stale_id)

            status = (
                f"Pigs: {stable_count} | Abnormal Behavior: {abnormal_behavior_count} | "
                f"Model: {detector.input_width}px | "
                f"Detection: {detection_fps:.1f} FPS ({latest_inference_ms:.0f} ms) | "
                f"Display: {display_fps:.1f} FPS"
            )
            put_outlined_text(display, status, (10, 25), 0.46, (255, 255, 255), 1)
            put_outlined_text(
                display,
                f"Abnormal Behavior: {abnormal_behavior_count}",
                (10, 50),
                0.55,
                (0, 0, 255) if abnormal_behavior_count else (0, 255, 0),
                2,
            )
            # Thermal data is produced by the separate FLIR worker, so it is
            # intentionally not fabricated or inferred from RGB detections.
            # The thermal window displays its own High Temperature counter.

            if result_age_ms > 750.0 and latest_inference_ms > 0.0:
                put_outlined_text(display, f"Boxes are {result_age_ms:.0f} ms old", (10, 48), 0.44, (0, 200, 255), 1)

            if not submitted_first_frame or latest_inference_ms == 0.0:
                put_outlined_text(display, "Loading first detection...", (10, 70), 0.50, (0, 255, 255), 2)

            if last_error:
                put_outlined_text(display, "MODEL ERROR - check terminal/log", (10, 92), 0.45, (0, 0, 255), 2)

            publish_controller_metrics(yolo={
                'frame_ts': time.time() - max(0.0, time.perf_counter() - latest_result_time) if latest_result_time else 0,
                'pigs': len(latest_tracks), 'lying': laying_count,
                'model_error': last_error or (None if latest_result_time else 'Waiting for inference')})
            save_preview('rgb', display)
            stop_event.wait(max(0.0, 0.1 - (time.monotonic() - loop_started)))

        if last_error:
            raise RuntimeError(last_error)
    finally:
        stop_event.set()
        camera.release()
        worker.join(timeout=2)



EVENT_CONFIRMATION_SEC = float(os.environ.get("PIG_CONFIRM_SEC", "1"))
if __name__ == "__main__":
    main()
