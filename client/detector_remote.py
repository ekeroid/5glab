"""
Remote detector — YOLOv8n inference via edge proxy (gRPC or HTTP).

Used when MODE=edge. Sends raw JPEG to the NEF proxy which forwards
to the Triton sidecar. Transport is selected by EDGE_TRANSPORT config.

Returns a timing dict with keys: total_ms, preprocess_ms, inference_ms,
postprocess_ms, network_ms (computed client-side).
"""

import logging
import time
import sys
import os

import requests

sys.path.insert(0, os.path.dirname(__file__))

from config import CONFIDENCE_THRESHOLD, EDGE_TRANSPORT

logger = logging.getLogger(__name__)

# --- HTTP transport ---

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _detect_http(jpeg_bytes: bytes, endpoint_url: str, max_retries: int, model: str = "detect") -> tuple[list[dict], dict]:
    session = _get_session()
    infer_url = f"{endpoint_url}/infer"

    last_error = None
    for attempt in range(max_retries):
        try:
            t_start = time.perf_counter()
            resp = session.post(
                infer_url,
                data=jpeg_bytes,
                headers={
                    "Content-Type": "image/jpeg",
                    "X-Confidence-Threshold": str(CONFIDENCE_THRESHOLD),
                    "X-Model": model,
                },
                timeout=15,
            )
            t_end = time.perf_counter()
            resp.raise_for_status()
            data = resp.json()

            e2e_ms = (t_end - t_start) * 1000
            server_timing = data.get("timing", {})
            server_total = server_timing.get("total_ms", data.get("latency_ms", 0))

            timing = {
                "total_ms": e2e_ms,
                "server_ms": server_total,
                "preprocess_ms": server_timing.get("preprocess_ms", 0),
                "inference_ms": server_timing.get("inference_ms", 0),
                "postprocess_ms": server_timing.get("postprocess_ms", 0),
                "network_ms": max(0, e2e_ms - server_total),
                "transport": "http",
            }
            return data.get("detections", []), timing
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = e
            logger.warning(f"HTTP inference attempt {attempt + 1}/{max_retries}: {type(e).__name__}")
        except Exception as e:
            last_error = e
            logger.warning(f"HTTP inference attempt {attempt + 1}/{max_retries} failed: {e}")
        if attempt < max_retries - 1:
            time.sleep(1)

    raise RuntimeError(f"HTTP inference failed after {max_retries} attempts: {last_error}")


# --- gRPC transport ---

_channel = None
_stub = None


def _get_stub(grpc_target: str):
    global _channel, _stub
    if _stub is None:
        import grpc
        import infer_pb2_grpc
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


def _reset_channel():
    global _channel, _stub
    if _channel:
        try:
            _channel.close()
        except Exception:
            pass
    _channel = None
    _stub = None


def _detect_grpc(jpeg_bytes: bytes, grpc_target: str, max_retries: int, model: str = "detect") -> tuple[list[dict], dict]:
    import grpc
    import infer_pb2

    stub = _get_stub(grpc_target)
    request = infer_pb2.InferRequest(
        jpeg=jpeg_bytes,
        confidence_threshold=CONFIDENCE_THRESHOLD,
        model=model,
    )

    last_error = None
    for attempt in range(max_retries):
        try:
            t_start = time.perf_counter()
            response = stub.Infer(request, timeout=15.0)
            t_end = time.perf_counter()

            detections = []
            for det in response.detections:
                d = {
                    "label": det.label,
                    "confidence": det.confidence,
                    "bbox": [det.bbox.x1, det.bbox.y1, det.bbox.x2, det.bbox.y2],
                }
                if det.mask_polygon:
                    d["mask_polygon"] = [[pt.x, pt.y] for pt in det.mask_polygon]
                detections.append(d)

            e2e_ms = (t_end - t_start) * 1000
            server_total = response.latency_ms

            timing = {
                "total_ms": e2e_ms,
                "server_ms": server_total,
                "preprocess_ms": response.preprocess_ms,
                "inference_ms": response.inference_ms,
                "postprocess_ms": response.postprocess_ms,
                "network_ms": max(0, e2e_ms - server_total),
                "transport": "grpc",
            }
            return detections, timing
        except grpc.RpcError as e:
            last_error = e
            logger.warning(f"gRPC inference attempt {attempt + 1}/{max_retries}: {e.code()} {e.details()}")
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


# --- Public API ---

def detect(jpeg_bytes: bytes, endpoint: str, max_retries: int = 3, model: str = "detect") -> tuple[list[dict], dict]:
    """
    Run inference via the configured transport.

    Returns (detections, timing_dict).
    timing_dict keys: total_ms, server_ms, preprocess_ms, inference_ms,
                      postprocess_ms, network_ms, transport.
    """
    if EDGE_TRANSPORT == "grpc":
        return _detect_grpc(jpeg_bytes, endpoint, max_retries, model)
    return _detect_http(jpeg_bytes, endpoint, max_retries, model)


def health_check(endpoint: str) -> bool:
    """Check if the inference server is healthy."""
    if EDGE_TRANSPORT == "grpc":
        try:
            import grpc
            import infer_pb2
            stub = _get_stub(endpoint)
            response = stub.Health(infer_pb2.HealthRequest(), timeout=3.0)
            return response.live
        except Exception:
            return False
    else:
        try:
            session = _get_session()
            resp = session.get(f"{endpoint}/health", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False
