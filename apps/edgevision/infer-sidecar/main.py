"""
Inference sidecar — runs inside the Triton container for direct DALI + TRT access.

Accepts raw JPEG on :8080/infer, preprocesses with NVIDIA DALI (GPU decode+resize+normalize,
~2.5ms), calls Triton TensorRT via gRPC + system shared memory (~2.5ms), postprocesses,
returns JSON detections. Full pipeline: ~7ms.
"""

import io
import time

import numpy as np
import nvidia.dali as dali
import nvidia.dali.fn as fn
from nvidia.dali import pipeline_def
import tritonclient.grpc as grpcclient
import tritonclient.utils.shared_memory as shm
from tritonclient import utils
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

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
_dali_pipe = None


@pipeline_def(batch_size=1, num_threads=2, device_id=0, prefetch_queue_depth=1)
def _build_dali_pipe():
    encoded = fn.external_source(name="jpeg_in", device="cpu", dtype=dali.types.UINT8)
    decoded = fn.decoders.image(encoded, device="mixed", output_type=dali.types.RGB)
    resized = fn.resize(decoded, resize_x=640, resize_y=640, interp_type=dali.types.INTERP_LINEAR)
    normalized = fn.crop_mirror_normalize(
        resized,
        dtype=dali.types.FLOAT,
        output_layout="CHW",
        mean=[0.0, 0.0, 0.0],
        std=[255.0, 255.0, 255.0],
    )
    reshaped = fn.reshape(normalized, shape=[1, 3, 640, 640])
    return reshaped


@app.on_event("startup")
async def startup():
    global _triton, _shm_input, _shm_output, _inputs, _outputs, _dali_pipe

    # DALI pipeline
    _dali_pipe = _build_dali_pipe()
    _dali_pipe.build()

    # Triton gRPC + shared memory
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


@app.on_event("shutdown")
async def shutdown():
    if _triton:
        try:
            _triton.unregister_system_shared_memory("infer_input")
            _triton.unregister_system_shared_memory("infer_output")
        except Exception:
            pass
    if _shm_input:
        shm.destroy_shared_memory_region(_shm_input)
    if _shm_output:
        shm.destroy_shared_memory_region(_shm_output)


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

    # DALI GPU preprocessing: JPEG decode + resize + normalize (~2.5ms)
    jpeg_np = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    _dali_pipe.feed_input("jpeg_in", [jpeg_np])
    (dali_out,) = _dali_pipe.run()
    tensor = dali_out.as_cpu().as_array()

    orig_h, orig_w = _get_jpeg_dimensions(jpeg_np)
    scale_x = orig_w / INPUT_SIZE
    scale_y = orig_h / INPUT_SIZE

    # TRT inference via shared memory (~2.5ms)
    shm.set_shared_memory_region(_shm_input, [tensor])
    _triton.infer(MODEL_NAME, _inputs, outputs=_outputs)
    output = shm.get_contents_as_numpy(
        _shm_output, utils.triton_to_np_dtype("FP32"), [1, 84, 8400]
    )

    # Postprocess (<1ms)
    detections = _postprocess(output, scale_x, scale_y, confidence)

    elapsed_ms = (time.perf_counter() - start) * 1000
    return JSONResponse({
        "detections": detections,
        "latency_ms": round(elapsed_ms, 1),
    })


def _get_jpeg_dimensions(jpeg_np: np.ndarray) -> tuple[int, int]:
    """Fast JPEG dimension extraction from SOF0 marker."""
    data = bytes(jpeg_np[:1024])
    i = 2
    while i < len(data) - 8:
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker in (0xC0, 0xC2):
            h = (data[i + 5] << 8) | data[i + 6]
            w = (data[i + 7] << 8) | data[i + 8]
            return h, w
        length = (data[i + 2] << 8) | data[i + 3]
        i += 2 + length
    return 720, 1280


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
