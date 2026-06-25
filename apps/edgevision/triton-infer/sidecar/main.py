"""
Inference sidecar — gRPC + HTTP serving.

gRPC on :50051 for fast binary inference (client-facing).
HTTP on :8080 for health checks and legacy REST.
Supports yolov8n (detection) and yolov8x-seg (segmentation).
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

# Detection model
DET_MODEL = "yolov8n_trt"
DET_OUTPUT_SHAPE = [1, 84, 8400]
DET_OUTPUT_SIZE = 1 * 84 * 8400 * 4

# Segmentation model
SEG_MODEL = "yolov8xseg_trt"
SEG_OUTPUT0_SHAPE = [1, 116, 8400]
SEG_OUTPUT0_SIZE = 1 * 116 * 8400 * 4
SEG_OUTPUT1_SHAPE = [1, 32, 160, 160]
SEG_OUTPUT1_SIZE = 1 * 32 * 160 * 160 * 4

INPUT_BYTE_SIZE = 1 * 3 * INPUT_SIZE * INPUT_SIZE * 4

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

# Shared memory regions for detection
_shm_input = None
_shm_det_output = None
_det_inputs = None
_det_outputs = None

# Shared memory regions for segmentation
_shm_seg_output0 = None
_shm_seg_output1 = None
_seg_inputs = None
_seg_outputs = None


def _init_triton():
    global _triton
    global _shm_input, _shm_det_output, _det_inputs, _det_outputs
    global _shm_seg_output0, _shm_seg_output1, _seg_inputs, _seg_outputs

    _triton = grpcclient.InferenceServerClient("localhost:8001")

    # Shared input (same for both models)
    _shm_input = shm.create_shared_memory_region("infer_input", "/infer_input_shm", INPUT_BYTE_SIZE)

    # Detection output
    _shm_det_output = shm.create_shared_memory_region("det_output", "/det_output_shm", DET_OUTPUT_SIZE)

    # Segmentation outputs
    _shm_seg_output0 = shm.create_shared_memory_region("seg_output0", "/seg_output0_shm", SEG_OUTPUT0_SIZE)
    _shm_seg_output1 = shm.create_shared_memory_region("seg_output1", "/seg_output1_shm", SEG_OUTPUT1_SIZE)

    # Unregister any prior
    for name in ["infer_input", "det_output", "seg_output0", "seg_output1"]:
        try:
            _triton.unregister_system_shared_memory(name)
        except Exception:
            pass

    _triton.register_system_shared_memory("infer_input", "/infer_input_shm", INPUT_BYTE_SIZE)
    _triton.register_system_shared_memory("det_output", "/det_output_shm", DET_OUTPUT_SIZE)
    _triton.register_system_shared_memory("seg_output0", "/seg_output0_shm", SEG_OUTPUT0_SIZE)
    _triton.register_system_shared_memory("seg_output1", "/seg_output1_shm", SEG_OUTPUT1_SIZE)

    # Detection inputs/outputs
    _det_inputs = [grpcclient.InferInput("images", [1, 3, INPUT_SIZE, INPUT_SIZE], "FP32")]
    _det_inputs[0].set_shared_memory("infer_input", INPUT_BYTE_SIZE)
    _det_outputs = [grpcclient.InferRequestedOutput("output0")]
    _det_outputs[0].set_shared_memory("det_output", DET_OUTPUT_SIZE)

    # Segmentation inputs/outputs
    _seg_inputs = [grpcclient.InferInput("images", [1, 3, INPUT_SIZE, INPUT_SIZE], "FP32")]
    _seg_inputs[0].set_shared_memory("infer_input", INPUT_BYTE_SIZE)
    _seg_outputs = [
        grpcclient.InferRequestedOutput("output0"),
        grpcclient.InferRequestedOutput("output1"),
    ]
    _seg_outputs[0].set_shared_memory("seg_output0", SEG_OUTPUT0_SIZE)
    _seg_outputs[1].set_shared_memory("seg_output1", SEG_OUTPUT1_SIZE)


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

    shm.set_shared_memory_region(_shm_input, [tensor])
    return tensor, scale_x, scale_y


def _run_detection(jpeg_bytes: bytes, confidence: float) -> tuple[list[dict], dict]:
    t0 = time.perf_counter()
    _, scale_x, scale_y = _preprocess(jpeg_bytes)
    t1 = time.perf_counter()

    _triton.infer(DET_MODEL, _det_inputs, outputs=_det_outputs)
    output = shm.get_contents_as_numpy(
        _shm_det_output, utils.triton_to_np_dtype("FP32"), DET_OUTPUT_SHAPE
    )
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


def _run_segmentation(jpeg_bytes: bytes, confidence: float) -> tuple[list[dict], dict]:
    t0 = time.perf_counter()
    _, scale_x, scale_y = _preprocess(jpeg_bytes)
    t1 = time.perf_counter()

    _triton.infer(SEG_MODEL, _seg_inputs, outputs=_seg_outputs)
    output0 = shm.get_contents_as_numpy(
        _shm_seg_output0, utils.triton_to_np_dtype("FP32"), SEG_OUTPUT0_SHAPE
    )
    output1 = shm.get_contents_as_numpy(
        _shm_seg_output1, utils.triton_to_np_dtype("FP32"), SEG_OUTPUT1_SHAPE
    )
    t2 = time.perf_counter()

    detections = _postprocess_seg(output0, output1, scale_x, scale_y, confidence)
    t3 = time.perf_counter()

    timing = {
        "preprocess_ms": round((t1 - t0) * 1000, 2),
        "inference_ms": round((t2 - t1) * 1000, 2),
        "postprocess_ms": round((t3 - t2) * 1000, 2),
        "total_ms": round((t3 - t0) * 1000, 2),
    }
    return detections, timing


def _run_inference(jpeg_bytes: bytes, confidence: float, model: str = "detect") -> tuple[list[dict], dict]:
    if model == "seg":
        return _run_segmentation(jpeg_bytes, confidence)
    return _run_detection(jpeg_bytes, confidence)


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
            if "mask_polygon" in det:
                for pt in det["mask_polygon"]:
                    proto_det.mask_polygon.append(infer_pb2.Point(x=pt[0], y=pt[1]))
            response.detections.append(proto_det)
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
    model = request.headers.get("X-Model", "detect")
    detections, timing = _run_inference(jpeg_bytes, confidence, model)
    return JSONResponse({
        "detections": detections,
        "latency_ms": timing["total_ms"],
        "timing": timing,
    })


# --- Postprocessing: Detection ---

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


# --- Postprocessing: Segmentation ---

def _postprocess_seg(output0: np.ndarray, output1: np.ndarray,
                     scale_x: float, scale_y: float,
                     confidence_threshold: float) -> list[dict]:
    # output0: [1, 116, 8400] — bbox(4) + classes(80) + mask_coeffs(32)
    # output1: [1, 32, 160, 160] — prototype masks
    if output0.ndim == 3:
        output0 = output0[0]
    if output1.ndim == 4:
        output1 = output1[0]  # [32, 160, 160]

    output0 = output0.T  # [8400, 116]

    cx, cy, w, h = output0[:, 0], output0[:, 1], output0[:, 2], output0[:, 3]
    class_scores = output0[:, 4:84]
    mask_coeffs = output0[:, 84:]  # [8400, 32]

    x1 = cx - w / 2
    y1 = cy - h / 2
    x2 = cx + w / 2
    y2 = cy + h / 2

    class_ids = np.argmax(class_scores, axis=1)
    confidences = np.max(class_scores, axis=1)

    mask = confidences >= confidence_threshold
    x1_f, y1_f, x2_f, y2_f = x1[mask], y1[mask], x2[mask], y2[mask]
    class_ids_f = class_ids[mask]
    confidences_f = confidences[mask]
    mask_coeffs_f = mask_coeffs[mask]

    if len(confidences_f) == 0:
        return []

    boxes = np.stack([x1_f, y1_f, x2_f, y2_f], axis=1)
    keep = _nms(boxes, confidences_f)
    boxes = boxes[keep]
    class_ids_f = class_ids_f[keep]
    confidences_f = confidences_f[keep]
    mask_coeffs_f = mask_coeffs_f[keep]

    # Compute instance masks from prototypes
    # mask_coeffs_f: [N, 32], output1: [32, 160, 160]
    proto_h, proto_w = output1.shape[1], output1.shape[2]
    protos = output1.reshape(32, -1)  # [32, 25600]
    masks = (mask_coeffs_f @ protos).reshape(-1, proto_h, proto_w)  # [N, 160, 160]
    masks = 1 / (1 + np.exp(-masks))  # sigmoid

    detections = []
    for i in range(len(boxes)):
        bx1, by1, bx2, by2 = boxes[i]

        # Crop mask to bbox (in 160x160 space)
        bx1_m = max(0, int(bx1 * proto_w / INPUT_SIZE))
        by1_m = max(0, int(by1 * proto_h / INPUT_SIZE))
        bx2_m = min(proto_w, int(bx2 * proto_w / INPUT_SIZE))
        by2_m = min(proto_h, int(by2 * proto_h / INPUT_SIZE))

        instance_mask = masks[i].copy()
        # Zero out everything outside bbox
        instance_mask[:by1_m, :] = 0
        instance_mask[by2_m:, :] = 0
        instance_mask[:, :bx1_m] = 0
        instance_mask[:, bx2_m:] = 0

        # Threshold and find contour
        binary = (instance_mask > 0.5).astype(np.uint8)
        # Upscale to original image space
        binary_full = cv2.resize(binary, (INPUT_SIZE, INPUT_SIZE), interpolation=cv2.INTER_NEAREST)

        contours, _ = cv2.findContours(binary_full, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        polygon = []
        if contours:
            largest = max(contours, key=cv2.contourArea)
            # Simplify polygon to reduce size
            epsilon = 0.005 * cv2.arcLength(largest, True)
            approx = cv2.approxPolyDP(largest, epsilon, True)
            polygon = [[round(float(pt[0][0] * scale_x), 1),
                        round(float(pt[0][1] * scale_y), 1)] for pt in approx]

        det = {
            "label": COCO_NAMES[class_ids_f[i]] if class_ids_f[i] < len(COCO_NAMES) else f"class_{class_ids_f[i]}",
            "confidence": round(float(confidences_f[i]), 3),
            "bbox": [
                round(float(bx1 * scale_x), 1),
                round(float(by1 * scale_y), 1),
                round(float(bx2 * scale_x), 1),
                round(float(by2 * scale_y), 1),
            ],
        }
        if polygon and len(polygon) > 2:
            det["mask_polygon"] = polygon
        detections.append(det)

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
