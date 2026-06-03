"""
Tenant-scoped reverse proxy — Forwards inference requests to Triton sidecar.

The /proxy/{slug}/infer endpoint accepts raw JPEG (~200KB), forwards it to the
infer-sidecar co-located with Triton in the same pod. Preprocessing, GPU
inference, and postprocessing all happen on localhost — no large tensor transfer.
"""

import logging
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

import k8s_manager
import tenant as tenant_mod

logger = logging.getLogger(__name__)
router = APIRouter()

_http_client: httpx.AsyncClient | None = None


async def init_proxy_client():
    """Initialize the async HTTP client for proxying."""
    global _http_client
    _http_client = httpx.AsyncClient(timeout=60.0)


async def close_proxy_client():
    """Close the async HTTP client."""
    if _http_client:
        await _http_client.aclose()


@router.post("/proxy/{tenant_slug}/infer")
async def infer_jpeg(tenant_slug: str, request: Request):
    """
    High-level inference endpoint: accepts JPEG, returns JSON detections.

    Forwards raw JPEG (~200KB) to the infer-sidecar co-located with Triton.
    Preprocessing + GPU inference + postprocessing all happen on localhost
    inside the Triton pod — no 4.8MB tensor over the pod network.
    """
    expected_slug = tenant_mod.get_tenant_slug(request)
    if tenant_slug != expected_slug:
        raise HTTPException(403, "Tenant mismatch")

    cluster_ip = k8s_manager.get_service_cluster_ip(tenant_slug)
    if not cluster_ip:
        raise HTTPException(502, f"Triton service not found for {tenant_slug}")

    jpeg_bytes = await request.body()
    confidence = request.headers.get("X-Confidence-Threshold", "0.35")

    try:
        resp = await _http_client.post(
            f"http://{cluster_ip}:8000/infer",
            content=jpeg_bytes,
            headers={
                "Content-Type": "image/jpeg",
                "X-Confidence-Threshold": confidence,
            },
        )
    except httpx.ConnectError:
        raise HTTPException(502, f"Cannot connect to inference sidecar at {cluster_ip}:8000")
    except httpx.TimeoutException:
        raise HTTPException(504, "Inference request timed out")

    if resp.status_code != 200:
        raise HTTPException(502, f"Sidecar error: {resp.text[:200]}")

    return JSONResponse(resp.json())


@router.api_route("/proxy/{tenant_slug}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_request(tenant_slug: str, path: str, request: Request):
    """Forward raw request to the tenant's Triton service."""
    start = time.perf_counter()

    expected_slug = tenant_mod.get_tenant_slug(request)
    if tenant_slug != expected_slug:
        logger.warning(f"Proxy denied: {tenant_mod.get_tenant_id(request)} "
                       f"tried to access {tenant_slug} (expected {expected_slug})")
        raise HTTPException(403, "Tenant mismatch")

    cluster_ip = k8s_manager.get_service_cluster_ip(tenant_slug)
    if not cluster_ip:
        raise HTTPException(502, f"Triton service not found for {tenant_slug}")

    target_url = f"http://{cluster_ip}:8001/{path}"

    body = await request.body()
    headers = {"Accept-Encoding": "identity"}
    for key, value in request.headers.items():
        if key in ("host", "content-length", "transfer-encoding", "connection",
                   "accept-encoding"):
            continue
        if key == "inference-header-content-length":
            headers["Inference-Header-Content-Length"] = value
        else:
            headers[key] = value

    try:
        resp = await _http_client.request(
            method=request.method,
            url=target_url,
            content=body,
            headers=headers,
        )
    except httpx.ConnectError:
        raise HTTPException(502, f"Cannot connect to Triton at {cluster_ip}:8001")
    except httpx.TimeoutException:
        raise HTTPException(504, "Triton request timed out")

    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(f"Proxy: tenant={tenant_slug} path=/{path} "
                f"status={resp.status_code} latency={elapsed_ms:.0f}ms")

    resp_headers = {}
    for key, value in resp.headers.items():
        if key in ("content-length", "content-encoding", "transfer-encoding", "connection"):
            continue
        resp_headers[key] = value

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=resp_headers,
    )
