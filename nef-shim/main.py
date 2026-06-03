"""
NEF-shim — CAMARA Network Exposure Function for edge compute orchestration.

FastAPI application running inside the k8s cluster that implements:
  - Simple Edge Discovery (zone capabilities)
  - Edge Application Management (register, instantiate, terminate)
  - Application Endpoint Discovery (proxy URL for inference)
  - Tenant-scoped reverse proxy to Triton Inference Server

Manages per-tenant k8s resources (Deployments, Services, Jobs, PVCs)
using a ServiceAccount with cluster-scoped permissions.
"""

import logging

from fastapi import FastAPI, Request

import config
import k8s_manager
from discovery import router as discovery_router
from app_management import router as app_management_router, _instances
from endpoint_discovery import router as endpoint_discovery_router, set_instances_ref
from proxy import router as proxy_router, init_proxy_client, close_proxy_client

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("nef-shim")

app = FastAPI(title="NEF-shim — CAMARA Edge Compute")

# Mount routers
app.include_router(discovery_router)
app.include_router(app_management_router)
app.include_router(endpoint_discovery_router)
app.include_router(proxy_router)


@app.on_event("startup")
async def startup():
    """Initialize k8s client and proxy HTTP client."""
    k8s_manager.init()
    set_instances_ref(_instances)
    await init_proxy_client()
    logger.info(f"NEF-shim started (namespace={config.CAMARA_NAMESPACE})")


@app.on_event("shutdown")
async def shutdown():
    """Clean up proxy client."""
    await close_proxy_client()
    logger.info("NEF-shim stopped")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all incoming requests for demo visibility."""
    logger.info(f"→ {request.method} {request.url.path} from {request.client.host}")
    response = await call_next(request)
    return response


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "nef-shim"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
