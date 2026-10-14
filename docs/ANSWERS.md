# ==========================================
# Persistent pig IDs
# ==========================================
# Layer 1: tracker tuned to remember lost pigs longer.
# Layer 2: StableIDMapper re-attaches a lost pig's old ID when the
#          tracker hands out a new one nearby.

import numpy as np
from scipy.optimize import linear_sum_assignment

# Tracking uses a lower confidence than plain detection so the tracker can
# keep following a pig whose score dips (it only ever extends existing tracks).
TRACK_CONF = 0.10

# Re-identification settings
MAX_LOST_SEC = 10.0      # how long a missing pig's ID is reserved
BASE_GATE_PX = 150       # minimum search radius around last known position
SPEED_PX_PER_SEC = 120   # extra radius allowed per second of absence
MAX_GATE_PX = 700        # hard cap on search radius
MAX_SIZE_RATIO = 2.5     # box area may not change by more than this factor
GAP_RESET_SEC = 2.0      # after a longer absence, restart the motionless timer

TRACKER_CFG = "/content/botsort_pigs.yaml"

with open(TRACKER_CFG, "w") as f:
    f.write(
        "tracker_type: botsort\n"
        "track_high_thresh: 0.25\n"
        "track_low_thresh: 0.1\n"
        "new_track_thresh: 0.25\n"
        "track_buffer: 300\n"
        "match_thresh: 0.8\n"
        "fuse_score: True\n"
        "gmc_method: sparseOptFlow\n"
        "proximity_thresh: 0.5\n"
        "appearance_thresh: 0.8\n"
        "with_reid: False\n"
        "model: auto\n"
    )

class StableIDMapper:
    """Maps raw tracker IDs to stable pig IDs that survive tracker resets."""

    def __init__(self, fps):
        self.fps = fps
        self.max_lost_frames = int(MAX_LOST_SEC * fps)
        self.raw_to_stable = {}
        self.last = {}        # stable_id -> {"center", "w", "h", "frame"}
        self.next_id = 1

    @staticmethod
    def _geom(det):
        x1, y1, x2, y2 = det["box"]
        return det["center"], max(x2 - x1, 1), max(y2 - y1, 1)

    def _cost(self, det, sid, frame_idx):
        (cx, cy), w, h = self._geom(det)
        prev = self.last[sid]
        px, py = prev["center"]
        dt = (frame_idx - prev["frame"]) / self.fps

        gate = min(
            max(BASE_GATE_PX, 0.8 * max(w, h)) + SPEED_PX_PER_SEC * dt,
            MAX_GATE_PX,
        )
        dist = float(np.hypot(cx - px, cy - py))
        if dist > gate:
            return None

        ratio = (w * h) / max(prev["w"] * prev["h"], 1)
        if ratio > MAX_SIZE_RATIO or ratio < 1.0 / MAX_SIZE_RATIO:
            return None

        return dist / gate  # 0 = same spot, 1 = edge of search radius

    def update(self, detections, frame_idx):
        claimed = set()
        pending = []

        # 1) detections whose raw tracker ID is already mapped
        for det in detections:
            raw = det["track_id"]
            if raw == -1:
                continue
            sid = self.raw_to_stable.get(raw)
            if sid is not None and sid not in claimed:
                claimed.add(sid)
                det["raw_id"] = raw
                det["track_id"] = sid
            else:
                pending.append(det)

        # 2) new raw IDs: try to give them back the ID of a recently lost pig
        candidates = [
            sid for sid, info in self.last.items()
            if sid not in claimed
            and 0 < frame_idx - info["frame"] <= self.max_lost_frames
        ]

        matches = {}
        if pending and candidates:
            INF = 1e6
            cost = np.full((len(pending), len(candidates)), INF)
            for i, det in enumerate(pending):
                for j, sid in enumerate(candidates):
                    c = self._cost(det, sid, frame_idx)
                    if c is not None:
                        cost[i, j] = c

            rows, cols = linear_sum_assignment(cost)
            for r, c in zip(rows, cols):
                if cost[r, c] < INF:
                    matches[r] = candidates[c]

        for i, det in enumerate(pending):
            raw = det["track_id"]
            sid = matches.get(i)
            if sid is None:
                sid = self.next_id
                self.next_id += 1
            self.raw_to_stable[raw] = sid
            claimed.add(sid)
            det["raw_id"] = raw
            det["track_id"] = sid

        # 3) record where every visible pig is, and how long it was gone
        for det in detections:
            sid = det["track_id"]
            if sid == -1:
                continue
            (cx, cy), w, h = self._geom(det)
            prev = self.last.get(sid)
            det["gap"] = (frame_idx - prev["frame"]) if prev else 1
            self.last[sid] = {
                "center": (cx, cy), "w": w, "h": h, "frame": frame_idx
            }

        return detections

Pigs per camera - refer to StableIDMapper given above
About abnormal lying - the model already detects the behavior of the pig
The temperature rule - this would probably mean 36.7 or above. I think best approach here is to flag no result then refer to the previous result.
Who evaluates and sends the SMS - based from the inlayo7.py raspi evaluates its own 19 hours because there may be a time when an interruption happens the prototype might get disconnected to the Web Application.
Statuses - Can you elaborate this one I cannot seem to understand.
Thermal-to-RGB matching - they are mounted side by side
Test mode - can you add that to the Web App add a button on the top of main dashboard for test mode.