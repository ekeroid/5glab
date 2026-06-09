"""
Local segmentation detector — YOLOv8x-seg inference on CPU.

Used when MODE=local and model_mode=seg. Returns detections with
polygon masks for instance segmentation overlay.
"""

import io
import logging
import os
import time

import numpy as np
from PIL import Image
from ultralytics import YOLO

from config import CONFIDENCE_THRESHOLD

logger = logging.getLogger(__name__)

os.environ.setdefault("TORCHINDUCTOR_DISABLE", "1")

from typing import Optional
_model: Optional[YOLO] = None
_MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "yolov8x-seg.pt")


def _get_model() -> YOLO:
    global _model
    if _model is None:
        logger.info("Loading YOLOv8x-seg model (local CPU)...")
        _model = YOLO(_MODEL_PATH)
        logger.info("YOLOv8x-seg model loaded")
    return _model


def detect(jpeg_bytes: bytes) -> tuple[list[dict], dict]:
    """
    Run YOLOv8x-seg on JPEG bytes.

    Returns (detections, timing) where each detection has:
      label, confidence, bbox, mask_polygon (list of [x,y] points)
    """
    model = _get_model()
    image = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    img_array = np.array(image)

    t0 = time.perf_counter()
    results = model(img_array, verbose=False, conf=CONFIDENCE_THRESHOLD)
    infer_ms = (time.perf_counter() - t0) * 1000

    detections = []
    for r in results:
        boxes = r.boxes
        masks = r.masks
        for i in range(len(boxes)):
            conf = float(boxes.conf[i])
            cls_id = int(boxes.cls[i])
            label = model.names[cls_id]
            x1, y1, x2, y2 = boxes.xyxy[i].tolist()

            det = {
                "label": label,
                "confidence": conf,
                "bbox": [x1, y1, x2, y2],
            }

            if masks is not None and i < len(masks.xy):
                det["mask_polygon"] = masks.xy[i].tolist()

            detections.append(det)

    timing = {"total_ms": infer_ms, "inference_ms": infer_ms, "transport": "local"}
    return detections, timing
