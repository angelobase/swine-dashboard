"""PLACEHOLDER pig ID assignment between scheduled captures.

Each new capture's boxes are matched to the pigs' last known positions by distance (greedy, closest first).
This is position-based only: a pig that moves across the pen between captures can swap IDs with another
pig or receive a new ID. The real ID approach (still to be decided) replaces only `assign()`.
"""
import math

MAX_SIZE_RATIO = 2.5      # same limit as StableIDMapper in yolo_worker.py
GATE_FRACTION = 0.25      # match radius as a fraction of the image diagonal


def _center(box):
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def _area(box):
    return max(box[2] - box[0], 1.0) * max(box[3] - box[1], 1.0)


def assign(previous, boxes, next_id, frame_size):
    """previous: [{'id': int, 'box': [x1,y1,x2,y2]}]; boxes: [[x1,y1,x2,y2], ...]; frame_size: (w, h).
    Returns (ids aligned with boxes, new next_id)."""
    gate = GATE_FRACTION * math.hypot(*frame_size)
    edges = []
    for i, box in enumerate(boxes):
        cx, cy = _center(box)
        for j, prev in enumerate(previous):
            px, py = _center(prev['box'])
            dist = math.hypot(cx - px, cy - py)
            ratio = _area(box) / _area(prev['box'])
            if dist <= gate and 1.0 / MAX_SIZE_RATIO <= ratio <= MAX_SIZE_RATIO:
                edges.append((dist, i, j))
    edges.sort()
    ids, used = [None] * len(boxes), set()
    for _, i, j in edges:
        if ids[i] is None and j not in used:
            ids[i] = int(previous[j]['id'])
            used.add(j)
    for i in range(len(boxes)):
        if ids[i] is None:
            ids[i], next_id = next_id, next_id + 1
    return ids, next_id
