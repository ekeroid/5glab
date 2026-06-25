"""
Inference sidecar — runs inside the Triton container.

Accepts raw JPEG on :8080/infer, preprocesses with OpenCV (decode+resize+normalize),
calls Triton via gRPC, postprocesses, returns JSON detections.
"""

import time

import cv2
import numpy as np
import tritonclient.grpc as grpcclient
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

INPUT_SIZE = 640
MODEL_NAME = "yolov8n"

COCO_NAMES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep",
    "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv",
    "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
]

_triton: grpcclient.InferenceServerClient | None = None


@app.on_event("startup")
async def startup():
    global _triton
    _triton = grpcclient.InferenceServerClient("localhost:8001")


@app.get("/health")
async def health():
    try:
        live = _triton.is_server_live() if _triton else False
    except Exception:
        live = False
    return {"status": "ok" if live else "triton_unavailable"}


@app.post("/infer")
async def infer(request: Request):
    start = time.perf_counter()

    jpeg_bytes = await request.body()
    confidence = float(request.headers.get("X-Confidence-Threshold", "0.35"))

    # OpenCV preprocessing: decode + resize + normalize (~5ms)
    img_array = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    orig_h, orig_w = img.shape[:2]
    scale_x = orig_w / INPUT_SIZE
    scale_y = orig_h / INPUT_SIZE

    img = cv2.resize(img, (INPUT_SIZE, INPUT_SIZE))
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = img.astype(np.float32) / 255.0
    tensor = np.transpose(img, (2, 0, 1))[np.newaxis]  # (1, 3, 640, 640)

    # Triton gRPC inference
    inputs = [grpcclient.InferInput("images", [1, 3, INPUT_SIZE, INPUT_SIZE], "FP32")]
    inputs[0].set_data_from_numpy(tensor)
    outputs = [grpcclient.InferRequestedOutput("output0")]

    result = _triton.infer(MODEL_NAME, inputs, outputs=outputs)
    output = result.as_numpy("output0")

    # Postprocess
    detections = _postprocess(output, scale_x, scale_y, confidence)

    elapsed_ms = (time.perf_counter() - start) * 1000
    return JSONResponse({
        "detections": detections,
        "latency_ms": round(elapsed_ms, 1),
    })


def _postprocess(output: np.ndarray, scale_x: float, scale_y: float,
                 confidence_threshold: float) -> list[dict]:
    if output.ndim == 3:
        output = output[0]
    output = output.T

    cx, cy, w, h = output[:, 0], output[:, 1], output[:, 2], output[:, 3]
    class_scores = output[:, 4:]

    x1 = cx - w / 2
    y1 = cy - h / 2
    x2 = cx + w / 2
    y2 = cy + h / 2

    class_ids = np.argmax(class_scores, axis=1)
    confidences = np.max(class_scores, axis=1)

    mask = confidences >= confidence_threshold
    x1, y1, x2, y2 = x1[mask], y1[mask], x2[mask], y2[mask]
    class_ids = class_ids[mask]
    confidences = confidences[mask]

    if len(confidences) == 0:
        return []

    boxes = np.stack([x1, y1, x2, y2], axis=1)
    keep = _nms(boxes, confidences)
    boxes = boxes[keep]
    class_ids = class_ids[keep]
    confidences = confidences[keep]

    detections = []
    for i in range(len(boxes)):
        bx1, by1, bx2, by2 = boxes[i]
        detections.append({
            "label": COCO_NAMES[class_ids[i]] if class_ids[i] < len(COCO_NAMES) else f"class_{class_ids[i]}",
            "confidence": round(float(confidences[i]), 3),
            "bbox": [
                round(float(bx1 * scale_x), 1),
                round(float(by1 * scale_y), 1),
                round(float(bx2 * scale_x), 1),
                round(float(by2 * scale_y), 1),
            ],
        })
    return detections


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float = 0.45) -> list[int]:
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while len(order) > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[np.where(iou <= iou_threshold)[0] + 1]
    return keep
