"""
Remote detector — YOLOv8n inference via gRPC to edge inference server.

Used when MODE=edge. Sends raw JPEG over gRPC binary stream.
Preprocessing (resize, normalize, NCHW) and Triton communication happen
server-side — only ~200KB JPEG goes over the wire, not a 5MB tensor.
"""

import logging
import time
import sys
import os

import grpc

sys.path.insert(0, os.path.dirname(__file__))
import infer_pb2
import infer_pb2_grpc

from config import CONFIDENCE_THRESHOLD

logger = logging.getLogger(__name__)

_channel: grpc.Channel | None = None
_stub: infer_pb2_grpc.InferenceServiceStub | None = None


def _get_stub(grpc_target: str) -> infer_pb2_grpc.InferenceServiceStub:
    global _channel, _stub
    if _stub is None:
        _channel = grpc.insecure_channel(
            grpc_target,
            options=[
                ("grpc.keepalive_time_ms", 10000),
                ("grpc.keepalive_timeout_ms", 5000),
                ("grpc.max_receive_message_length", 64 * 1024 * 1024),
            ],
        )
        _stub = infer_pb2_grpc.InferenceServiceStub(_channel)
    return _stub


def detect(jpeg_bytes: bytes, triton_url: str, max_retries: int = 3) -> tuple[list[dict], float]:
    """
    Run YOLOv8n inference via gRPC.

    triton_url: gRPC target (e.g. "localhost:50051" or "130.235.32.171:50051")
    Returns (detections, server_latency_ms).
    """
    stub = _get_stub(triton_url)

    request = infer_pb2.InferRequest(
        jpeg=jpeg_bytes,
        confidence_threshold=CONFIDENCE_THRESHOLD,
    )

    last_error = None
    for attempt in range(max_retries):
        try:
            response = stub.Infer(request, timeout=10.0)
            detections = []
            for det in response.detections:
                detections.append({
                    "label": det.label,
                    "confidence": det.confidence,
                    "bbox": [det.bbox.x1, det.bbox.y1, det.bbox.x2, det.bbox.y2],
                })
            return detections, response.latency_ms
        except grpc.RpcError as e:
            last_error = e
            logger.warning(f"gRPC inference attempt {attempt + 1}/{max_retries} failed: {e.code()} {e.details()}")
            if attempt < max_retries - 1:
                time.sleep(1)
                _reset_channel()
        except Exception as e:
            last_error = e
            logger.warning(f"gRPC inference attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
                _reset_channel()

    raise RuntimeError(f"gRPC inference failed after {max_retries} attempts: {last_error}")


def health_check(grpc_target: str) -> bool:
    """Check if the inference server is healthy via gRPC."""
    try:
        stub = _get_stub(grpc_target)
        response = stub.Health(infer_pb2.HealthRequest(), timeout=3.0)
        return response.live
    except Exception:
        return False


def _reset_channel():
    global _channel, _stub
    if _channel:
        try:
            _channel.close()
        except Exception:
            pass
    _channel = None
    _stub = None
