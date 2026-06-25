"""
Display — Annotates frames with bounding boxes and a status overlay.

Draws colour-coded bounding boxes per class, labels with confidence,
and a status bar showing mode (LOCAL/EDGE), latency, frame counter,
detection count, and edge instance info when in edge mode.
"""

import io
import logging
import os
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from config import OUTPUT_DIR, DISPLAY_WINDOW

logger = logging.getLogger(__name__)

_latency_history: deque = deque(maxlen=10)
_frame_counter = 0

# Deterministic colour palette seeded by class name
_colour_cache: dict[str, tuple[int, int, int]] = {}


def _get_colour(label: str) -> tuple[int, int, int]:
    """Get a deterministic BGR colour for a class label."""
    if label not in _colour_cache:
        h = hash(label) % 360
        # HSV with high saturation and value → convert to BGR
        hsv = np.array([[[h / 2, 200, 230]]], dtype=np.uint8)
        bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0][0]
        _colour_cache[label] = (int(bgr[0]), int(bgr[1]), int(bgr[2]))
    return _colour_cache[label]


def annotate(
    jpeg_bytes: bytes,
    detections: list[dict],
    mode: str,
    latency_ms: float,
    app_instance_id: str | None = None,
    proxy_endpoint: str | None = None,
) -> bytes:
    """
    Draw detections and status bar on frame, save to output dir.

    Returns annotated JPEG bytes.
    """
    global _frame_counter
    _frame_counter += 1
    _latency_history.append(latency_ms)

    # Decode JPEG (force 3-channel for grayscale images)
    img_array = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if frame.ndim == 2:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    h, w = frame.shape[:2]

    # Draw bounding boxes
    for det in detections:
        label = det["label"]
        conf = det["confidence"]
        x1, y1, x2, y2 = [int(v) for v in det["bbox"]]
        colour = _get_colour(label)

        cv2.rectangle(frame, (x1, y1), (x2, y2), colour, 2)

        text = f"{label} {conf:.0%}"
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(frame, (x1, y1 - th - 6), (x1 + tw + 4, y1), colour, -1)
        cv2.putText(frame, text, (x1 + 2, y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

    # Status bar at top
    bar_h = 36
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (w, bar_h), (40, 40, 40), -1)
    frame = cv2.addWeighted(overlay, 0.7, frame, 0.3, 0)

    # Mode badge
    if mode == "edge":
        badge_colour = (0, 180, 0)  # green
        badge_text = "EDGE"
    else:
        badge_colour = (180, 100, 0)  # blue
        badge_text = "LOCAL"

    cv2.rectangle(frame, (4, 4), (70, 30), badge_colour, -1)
    cv2.putText(frame, badge_text, (8, 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

    # Stats text
    avg_latency = sum(_latency_history) / len(_latency_history)
    timestamp = time.strftime("%H:%M:%S")
    stats = (f"{latency_ms:.0f}ms (avg {avg_latency:.0f}ms) | "
             f"#{_frame_counter} {timestamp} | "
             f"{len(detections)} det")

    if mode == "edge" and app_instance_id:
        stats += f" | inst:{app_instance_id[:8]}"

    cv2.putText(frame, stats, (80, 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (220, 220, 220), 1)

    # Save outputs
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    latest_path = out_dir / "latest.jpg"
    ts_path = out_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{_frame_counter:04d}.jpg"

    cv2.imwrite(str(latest_path), frame)
    cv2.imwrite(str(ts_path), frame)

    # Display window if enabled
    if DISPLAY_WINDOW:
        cv2.imshow("EdgeVision", frame)
        cv2.waitKey(1)

    # Encode back to JPEG
    _, buf = cv2.imencode(".jpg", frame)
    return buf.tobytes()
