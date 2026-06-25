"""
NEF-shim — CAMARA Edge Compute control plane.

Provides a container runtime to tenants:
  - Simple Edge Discovery (zone capabilities)
  - Edge Application Management (register, instantiate, terminate)
  - Application Endpoint Discovery (per-port reachable URLs)
  - Tenant-scoped path-transparent HTTP reverse proxy

The edge runs whatever container the tenant declares in their app manifest.
No application semantics live here.
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

app.include_router(discovery_router)
app.include_router(app_management_router)
app.include_router(endpoint_discovery_router)
app.include_router(proxy_router)


@app.on_event("startup")
async def startup():
    k8s_manager.init()
    set_instances_ref(_instances)
    await init_proxy_client()
    logger.info(f"NEF-shim started (namespace={config.CAMARA_NAMESPACE})")


@app.on_event("shutdown")
async def shutdown():
    await close_proxy_client()
    logger.info("NEF-shim stopped")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"→ {request.method} {request.url.path} from {request.client.host}")
    return await call_next(request)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "nef-shim"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
