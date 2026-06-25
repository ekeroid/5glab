"""
CPU Inference sidecar — gRPC + HTTP serving via ONNX Runtime.

Same interface as the GPU/Triton variant but runs on CPU with ONNX Runtime.
gRPC on :50051, HTTP on :8080.
"""

import os
import time
import threading
from concurrent import futures

import cv2
import grpc
import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import infer_pb2
import infer_pb2_grpc

INPUT_SIZE = 640
MODEL_DIR = os.getenv("MODEL_DIR", "/opt/models")

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

_session: ort.InferenceSession | None = None


def _init_onnx():
    global _session
    model_path = os.path.join(MODEL_DIR, "yolov8n.onnx")
    opts = ort.SessionOptions()
    opts.inter_op_num_threads = 4
    opts.intra_op_num_threads = 4
    _session = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
    print(f"[sidecar] ONNX model loaded: {model_path}")


def _preprocess(jpeg_bytes: bytes) -> tuple[np.ndarray, float, float]:
    img_array = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    orig_h, orig_w = img.shape[:2]
    scale_x = orig_w / INPUT_SIZE
    scale_y = orig_h / INPUT_SIZE

    img = cv2.resize(img, (INPUT_SIZE, INPUT_SIZE))
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = img.astype(np.float32) / 255.0
    tensor = np.transpose(img, (2, 0, 1))[np.newaxis].astype(np.float32)
    return tensor, scale_x, scale_y


def _run_inference(jpeg_bytes: bytes, confidence: float, model: str = "detect") -> tuple[list[dict], dict]:
    t0 = time.perf_counter()
    tensor, scale_x, scale_y = _preprocess(jpeg_bytes)
    t1 = time.perf_counter()

    input_name = _session.get_inputs()[0].name
    outputs = _session.run(None, {input_name: tensor})
    output = outputs[0]
    t2 = time.perf_counter()

    detections = _postprocess_detect(output, scale_x, scale_y, confidence)
    t3 = time.perf_counter()

    timing = {
        "preprocess_ms": round((t1 - t0) * 1000, 2),
        "inference_ms": round((t2 - t1) * 1000, 2),
        "postprocess_ms": round((t3 - t2) * 1000, 2),
        "total_ms": round((t3 - t0) * 1000, 2),
    }
    return detections, timing


def _postprocess_detect(output: np.ndarray, scale_x: float, scale_y: float,
                        confidence_threshold: float) -> list[dict]:
    if output.ndim == 3:
        output = output[0]
    output = output.T  # [8400, 84]

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


# --- gRPC Service ---

class InferenceServicer(infer_pb2_grpc.InferenceServiceServicer):
    def Infer(self, request, context):
        confidence = request.confidence_threshold if request.confidence_threshold > 0 else 0.35
        model = request.model if request.model else "detect"
        detections, timing = _run_inference(request.jpeg, confidence, model)

        response = infer_pb2.InferResponse(
            latency_ms=timing["total_ms"],
            preprocess_ms=timing["preprocess_ms"],
            inference_ms=timing["inference_ms"],
            postprocess_ms=timing["postprocess_ms"],
        )
        for det in detections:
            bbox = infer_pb2.BBox(
                x1=det["bbox"][0], y1=det["bbox"][1],
                x2=det["bbox"][2], y2=det["bbox"][3],
            )
            proto_det = infer_pb2.Detection(
                label=det["label"],
                confidence=det["confidence"],
                bbox=bbox,
            )
            response.detections.append(proto_det)
        return response

    def Health(self, request, context):
        live = _session is not None
        return infer_pb2.HealthResponse(
            live=live,
            status="ok" if live else "model_not_loaded",
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
    _init_onnx()
    threading.Thread(target=_start_grpc_server, daemon=True).start()


@app.get("/health")
async def health():
    live = _session is not None
    return {"status": "ok" if live else "model_not_loaded"}


@app.post("/infer")
async def infer(request: Request):
    jpeg_bytes = await request.body()
    confidence = float(request.headers.get("X-Confidence-Threshold", "0.35"))
    model = request.headers.get("X-Model", "detect")
    detections, timing = _run_inference(jpeg_bytes, confidence, model)
    return JSONResponse({
        "detections": detections,
        "latency_ms": timing["total_ms"],
        "timing": timing,
    })
