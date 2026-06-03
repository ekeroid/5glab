"""
Inference sidecar — gRPC + HTTP serving.

gRPC on :50051 for fast binary inference (client-facing).
HTTP on :8080 for health checks and legacy REST.
Uses OpenCV preprocessing + Triton gRPC + shared memory.
"""

import time
import asyncio
import threading
from concurrent import futures

import cv2
import grpc
import numpy as np
import tritonclient.grpc as grpcclient
import tritonclient.utils.shared_memory as shm
from tritonclient import utils
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import infer_pb2
import infer_pb2_grpc

INPUT_SIZE = 640
MODEL_NAME = "yolov8n_trt"
INPUT_BYTE_SIZE = 1 * 3 * INPUT_SIZE * INPUT_SIZE * 4
OUTPUT_BYTE_SIZE = 1 * 84 * 8400 * 4

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
_shm_input = None
_shm_output = None
_inputs = None
_outputs = None


def _init_triton():
    global _triton, _shm_input, _shm_output, _inputs, _outputs

    _triton = grpcclient.InferenceServerClient("localhost:8001")

    _shm_input = shm.create_shared_memory_region("infer_input", "/infer_input_shm", INPUT_BYTE_SIZE)
    _shm_output = shm.create_shared_memory_region("infer_output", "/infer_output_shm", OUTPUT_BYTE_SIZE)

    try:
        _triton.unregister_system_shared_memory("infer_input")
    except Exception:
        pass
    try:
        _triton.unregister_system_shared_memory("infer_output")
    except Exception:
        pass

    _triton.register_system_shared_memory("infer_input", "/infer_input_shm", INPUT_BYTE_SIZE)
    _triton.register_system_shared_memory("infer_output", "/infer_output_shm", OUTPUT_BYTE_SIZE)

    _inputs = [grpcclient.InferInput("images", [1, 3, INPUT_SIZE, INPUT_SIZE], "FP32")]
    _inputs[0].set_shared_memory("infer_input", INPUT_BYTE_SIZE)
    _outputs = [grpcclient.InferRequestedOutput("output0")]
    _outputs[0].set_shared_memory("infer_output", OUTPUT_BYTE_SIZE)


def _run_inference(jpeg_bytes: bytes, confidence: float) -> tuple[list[dict], float]:
    """Run full inference pipeline. Returns (detections, latency_ms)."""
    start = time.perf_counter()

    img_array = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    orig_h, orig_w = img.shape[:2]
    scale_x = orig_w / INPUT_SIZE
    scale_y = orig_h / INPUT_SIZE

    img = cv2.resize(img, (INPUT_SIZE, INPUT_SIZE))
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = img.astype(np.float32) / 255.0
    tensor = np.transpose(img, (2, 0, 1))[np.newaxis].astype(np.float32)

    shm.set_shared_memory_region(_shm_input, [tensor])
    _triton.infer(MODEL_NAME, _inputs, outputs=_outputs)
    output = shm.get_contents_as_numpy(
        _shm_output, utils.triton_to_np_dtype("FP32"), [1, 84, 8400]
    )

    detections = _postprocess(output, scale_x, scale_y, confidence)
    elapsed_ms = (time.perf_counter() - start) * 1000
    return detections, elapsed_ms


# --- gRPC Service ---

class InferenceServicer(infer_pb2_grpc.InferenceServiceServicer):
    def Infer(self, request, context):
        confidence = request.confidence_threshold if request.confidence_threshold > 0 else 0.35
        detections, latency_ms = _run_inference(request.jpeg, confidence)

        response = infer_pb2.InferResponse(latency_ms=latency_ms)
        for det in detections:
            bbox = infer_pb2.BBox(
                x1=det["bbox"][0], y1=det["bbox"][1],
                x2=det["bbox"][2], y2=det["bbox"][3],
            )
            response.detections.append(infer_pb2.Detection(
                label=det["label"],
                confidence=det["confidence"],
                bbox=bbox,
            ))
        return response

    def Health(self, request, context):
        try:
            live = _triton.is_server_live() if _triton else False
        except Exception:
            live = False
        return infer_pb2.HealthResponse(
            live=live,
            status="ok" if live else "triton_unavailable",
        )


def _start_grpc_server():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    infer_pb2_grpc.add_InferenceServiceServicer_to_server(InferenceServicer(), server)
    server.add_insecure_port("[::]:50051")
    server.start()
    print("[sidecar] gRPC server started on :50051")
    server.wait_for_termination()


# --- HTTP (FastAPI) ---

app = FastAPI()


@app.on_event("startup")
async def startup():
    _init_triton()
    threading.Thread(target=_start_grpc_server, daemon=True).start()


@app.get("/health")
async def health():
    try:
        live = _triton.is_server_live() if _triton else False
    except Exception:
        live = False
    return {"status": "ok" if live else "triton_unavailable"}


@app.post("/infer")
async def infer(request: Request):
    jpeg_bytes = await request.body()
    confidence = float(request.headers.get("X-Confidence-Threshold", "0.35"))
    detections, latency_ms = _run_inference(jpeg_bytes, confidence)
    return JSONResponse({
        "detections": detections,
        "latency_ms": round(latency_ms, 1),
    })


# --- Postprocessing ---

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
