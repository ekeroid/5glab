"""
Application Endpoint Discovery — Returns the gRPC endpoint for inference.

After an app instance is ready, the client calls this endpoint to get
the gRPC target (host:port) for direct inference communication.
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request

import k8s_manager
import tenant as tenant_mod
from config import EXTERNAL_HOSTNAME

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/application-endpoint-discovery/v0")

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

    Returns the gRPC target (host:nodePort) for the tenant's inference server.
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

    # gRPC: direct NodePort (only works on 5G, blocked on VPN)
    grpc_nodeport = k8s_manager.get_service_grpc_nodeport(tenant_slug)
    if grpc_nodeport:
        grpc_endpoint = f"{EXTERNAL_HOSTNAME}:{grpc_nodeport}"
    else:
        grpc_endpoint = ""

    http_endpoint = f"http://{EXTERNAL_HOSTNAME}/proxy/{tenant_slug}"

    logger.info(f"Endpoint discovery: tenant={tenant_slug}, grpc={grpc_endpoint}, http={http_endpoint}")

    return {
        "appInstanceId": appInstanceId,
        "grpcEndpoint": grpc_endpoint,
        "httpEndpoint": http_endpoint,
    }
