"""
Application Endpoint Discovery — Returns the tenant-scoped proxy URL for inference.

After an app instance is ready, the client calls this endpoint to get
the opaque URL through which inference requests are proxied to the
tenant's Triton deployment.
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request

import tenant as tenant_mod
from config import EXTERNAL_HOSTNAME

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/application-endpoint-discovery/v0")

# Reference to app_management's instance store (set during app startup)
_instances: dict | None = None


def set_instances_ref(instances: dict):
    """Set reference to the shared instances dict from app_management."""
    global _instances
    _instances = instances


@router.get("/endpoints")
async def get_endpoint(
    request: Request,
    appInstanceId: str = Query(..., alias="appInstanceId"),
):
    """
    Discover the inference endpoint for a running app instance.

    Returns an opaque proxy URL scoped to the tenant.
    """
    tenant_ip = tenant_mod.get_tenant_id(request)
    tenant_slug = tenant_mod.get_tenant_slug(request)

    if _instances is None:
        raise HTTPException(500, "Instance store not initialized")

    if appInstanceId not in _instances:
        raise HTTPException(404, "Instance not found")

    instance = _instances[appInstanceId]
    if instance["tenant_ip"] != tenant_ip:
        raise HTTPException(403, "Instance not owned by this tenant")

    if instance["status"] != "ready":
        raise HTTPException(409, f"Instance not ready (status: {instance['status']})")

    endpoint = f"http://{EXTERNAL_HOSTNAME}/proxy/{tenant_slug}"
    grpc_endpoint = f"{EXTERNAL_HOSTNAME}:50051"
    logger.info(f"Endpoint discovery: tenant={tenant_slug}, grpc={grpc_endpoint}")

    return {
        "appInstanceId": appInstanceId,
        "endpoint": endpoint,
        "grpcEndpoint": grpc_endpoint,
    }
