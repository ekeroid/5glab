"""
Local detector — YOLOv8n inference running in-process on CPU.

Used when MODE=local. Downloads the YOLOv8n model on first run via
ultralytics and runs inference directly on JPEG frames from the camera API.
"""

import io
import logging

import numpy as np
from PIL import Image
from ultralytics import YOLO

from config import CONFIDENCE_THRESHOLD

logger = logging.getLogger(__name__)

from typing import Optional
_model: Optional[YOLO] = None


def _get_model() -> YOLO:
    global _model
    if _model is None:
        logger.info("Loading YOLOv8n model (local CPU)...")
        _model = YOLO("yolov8n.pt")
        logger.info("YOLOv8n model loaded")
    return _model


def detect(jpeg_bytes: bytes) -> list[dict]:
    """
    Run YOLOv8n on JPEG bytes, return detections.

    Returns list of {label, confidence, bbox: [x1, y1, x2, y2]}
    """
    model = _get_model()
    image = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    img_array = np.array(image)

    results = model(img_array, verbose=False, conf=CONFIDENCE_THRESHOLD)

    detections = []
    for r in results:
        boxes = r.boxes
        for i in range(len(boxes)):
            conf = float(boxes.conf[i])
            cls_id = int(boxes.cls[i])
            label = model.names[cls_id]
            x1, y1, x2, y2 = boxes.xyxy[i].tolist()
            detections.append({
                "label": label,
                "confidence": conf,
                "bbox": [x1, y1, x2, y2],
            })

    return detections
