"""Drop-in helper for the EXISTING pipeline: pushes its latest frame to the dashboard.
It reuses a frame the pipeline already captured. It never opens a camera.
Preview work runs in a background thread, so a slow dashboard cannot slow the pipeline.

Usage inside your existing loop (after you already have `frame`, a BGR numpy array):
    from push_preview_example import PreviewPusher
    rgb_push = PreviewPusher("PEN-01", "rgb")
    thermal_push = PreviewPusher("PEN-01", "thermal")   # optional; pass your colourised thermal image
    ...
    rgb_push.offer(frame)          # cheap: returns immediately, sends at most ~1 frame per second
"""
import threading, time, urllib.request
import cv2  # already part of your pipeline

class PreviewPusher:
    def __init__(self, pen_id, kind="rgb", host="127.0.0.1", port=8080,
                 interval_s=1.0, width=480, quality=60, token=None):
        self.url = f"http://{host}:{port}/api/preview/{pen_id}/{kind}"
        self.interval_s, self.width, self.quality, self.token = interval_s, width, quality, token
        self._last = 0.0
        self._slot = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def offer(self, frame):
        now = time.monotonic()
        if now - self._last < self.interval_s:
            return                              # throttle on the pipeline side
        self._last = now
        with self._lock:
            self._slot = frame.copy()           # keep only the newest frame
        self._wake.set()

    def _run(self):
        while True:
            self._wake.wait(); self._wake.clear()
            with self._lock:
                frame, self._slot = self._slot, None
            if frame is None:
                continue
            try:
                h, w = frame.shape[:2]
                if w > self.width:
                    frame = cv2.resize(frame, (self.width, int(h * self.width / w)))
                ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
                if not ok:
                    continue
                req = urllib.request.Request(self.url, data=jpg.tobytes(), method="POST",
                                             headers={"Content-Type": "image/jpeg"})
                if self.token:
                    req.add_header("X-Ingest-Token", self.token)
                urllib.request.urlopen(req, timeout=0.5).read()
            except Exception:
                pass                            # preview is optional: never raise into the pipeline
