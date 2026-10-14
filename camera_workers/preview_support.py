import os
for _key in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
    os.environ[_key] = '1'
from pathlib import Path
import signal
import time
import cv2
_last_preview = 0.0
def save_preview(kind, frame):
    global _last_preview
    now = time.monotonic()
    if now - _last_preview < 1.0:
        return
    folder = Path(os.environ['PIG_PREVIEW_DIR'])
    folder.mkdir(parents=True, exist_ok=True)
    frame = cv2.resize(frame, (480, 360))
    ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
    if not ok or encoded.nbytes > 250000:
        raise RuntimeError('Preview JPEG encoding failed or exceeded size limit')
    tmp = folder / (kind + '.tmp')
    tmp.write_bytes(encoded.tobytes())
    os.replace(tmp, folder / (kind + '.jpg'))
    _last_preview = now
