import time
import numpy as np
import cv2
from push_preview_example import PreviewPusher

rgb = PreviewPusher("PEN-01", "rgb")
thermal = PreviewPusher("PEN-01", "thermal")  # optional

for i in range(40):
    frame = np.zeros((480, 640, 3), np.uint8)
    frame[:] = (40, (i * 6) % 200, 120)
    cv2.putText(frame, f"test frame {i}", (30, 240),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
    rgb.offer(frame)

    heat = np.tile(np.linspace(0, 255, 160, dtype=np.uint8), (120, 1))
    thermal.offer(cv2.applyColorMap(heat, cv2.COLORMAP_INFERNO))

    time.sleep(0.25)   # the loop runs faster than the pusher sends

time.sleep(1.5)        # let the background thread finish the last send