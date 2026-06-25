"""
Tenant-scoped reverse proxy — path-transparent forwarding to the user's pod.

  /proxy/{slug}/{port_name}/{path...}  →  http://{cluster_ip}:{port}/{path...}

The first path segment after the slug names the declared port (from the
tenant's componentSpec.networkInterfaces). The remainder of the path,
plus query string, headers, and body, are forwarded byte-for-byte.

The edge proxy is application-agnostic — it has no opinion about what
the user's container serves at any path.
"""

import logging
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

import k8s_manager
import tenant as tenant_mod

logger = logging.getLogger(__name__)
router = APIRouter()

_http_client: httpx.AsyncClient | None = None

HOP_BY_HOP = {
    "host", "content-length", "transfer-encoding", "connection",
    "accept-encoding", "keep-alive", "te", "trailers", "upgrade",
    "proxy-authorization", "proxy-authenticate",
}


async def init_proxy_client():
    global _http_client
    _http_client = httpx.AsyncClient(timeout=60.0)


async def close_proxy_client():
    if _http_client:
        await _http_client.aclose()


def _resolve_port(manifest: dict, port_name: str) -> int | None:
    """Look up a declared port number by name in the tenant manifest."""
    for comp in manifest.get("componentSpec", []) or []:
        for nic in comp.get("networkInterfaces", []) or []:
            if (nic.get("name") or "") == port_name:
                return int(nic["port"])
    return None


@router.api_route(
    "/proxy/{tenant_slug}/{port_name}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_request(tenant_slug: str, port_name: str, path: str, request: Request):
    """Forward to the tenant's chosen port, preserving method/headers/body/query."""
    start = time.perf_counter()

    expected_slug = tenant_mod.get_tenant_slug(request)
    if tenant_slug != expected_slug:
        logger.warning(
            f"Proxy denied: {tenant_mod.get_tenant_id(request)} "
            f"tried to access {tenant_slug} (expected {expected_slug})"
        )
        raise HTTPException(403, "Tenant mismatch")

    manifest = k8s_manager.get_tenant_manifest(tenant_slug)
    if not manifest:
        raise HTTPException(404, "No instance found for tenant")

    port = _resolve_port(manifest, port_name)
    if port is None:
        raise HTTPException(404, f"Port '{port_name}' not declared in app manifest")

    cluster_ip = k8s_manager.get_service_cluster_ip(tenant_slug)
    if not cluster_ip:
        raise HTTPException(502, f"Service not found for {tenant_slug}")

    target_url = f"http://{cluster_ip}:{port}/{path}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    body = await request.body()
    headers = {"Accept-Encoding": "identity"}
    for key, value in request.headers.items():
        if key.lower() in HOP_BY_HOP:
            continue
        headers[key] = value

    try:
        resp = await _http_client.request(
            method=request.method,
            url=target_url,
            content=body,
            headers=headers,
        )
    except httpx.ConnectError:
        raise HTTPException(502, f"Cannot connect to {cluster_ip}:{port}")
    except httpx.TimeoutException:
        raise HTTPException(504, "Upstream request timed out")

    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        f"Proxy: tenant={tenant_slug} port={port_name} {request.method} /{path} "
        f"status={resp.status_code} latency={elapsed_ms:.0f}ms"
    )

    resp_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection"}
    }
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=resp_headers,
        media_type=resp.headers.get("content-type"),
    )
