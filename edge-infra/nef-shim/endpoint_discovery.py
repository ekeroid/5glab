"""
Application Endpoint Discovery — returns the externally reachable URL for
every port the tenant declared in their app manifest.

Every port — HTTP, gRPC, anything else — is exposed via NodePort at
{ext_host}:{nodePort}. HTTP/HTTPS ports get a scheme prefix so clients
can use the URL directly. Symmetric across zones: the only zone-specific
piece is which public host to point at.

The client picks endpoints by name. The edge has no opinion about what
the user does with them. Inference traffic goes straight to the pod's
NodePort — it does NOT pass through the nef-shim.
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request

import k8s_manager
import tenant as tenant_mod
from config import EXTERNAL_HOSTNAME, ZONE2_EXTERNAL_IP

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/application-endpoint-discovery/v0")

_instances: dict | None = None


def set_instances_ref(instances: dict):
    """Set reference to the shared instances dict from app_management."""
    global _instances
    _instances = instances


def _is_http(name: str, protocol: str) -> bool:
    """Heuristic: name starts with http OR is the well-known 'http' name."""
    if (protocol or "").upper() != "TCP":
        return False
    n = (name or "").lower()
    return n == "http" or n.startswith("http-") or n.startswith("https") or n == "https"


@router.get("/endpoints")
async def get_endpoint(
    request: Request,
    appInstanceId: str = Query(..., alias="appInstanceId"),
):
    """
    Return one endpoint entry per port declared in the tenant's manifest.

    Response shape:
      {
        "appInstanceId": "...",
        "endpoints": [
          {"name": "http", "protocol": "TCP", "url": "http://host/proxy/<slug>/http"},
          {"name": "grpc", "protocol": "TCP", "url": "host:nodePort"}
        ]
      }
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

    zone_id = k8s_manager.get_tenant_zone(tenant_slug)
    ext_host = ZONE2_EXTERNAL_IP if zone_id == k8s_manager.ZONE_XERCES else EXTERNAL_HOSTNAME

    manifest = k8s_manager.get_tenant_manifest(tenant_slug) or {}
    node_ports = k8s_manager.get_service_node_ports(tenant_slug)

    endpoints = []
    for comp in manifest.get("componentSpec", []) or []:
        for nic in comp.get("networkInterfaces", []) or []:
            name = nic.get("name") or f"port-{nic.get('port')}"
            protocol = nic.get("protocol", "TCP")
            np = node_ports.get(name)
            if not np:
                url = ""
            elif _is_http(name, protocol):
                url = f"http://{ext_host}:{np}"
            else:
                url = f"{ext_host}:{np}"
            endpoints.append({
                "name": name,
                "protocol": protocol,
                "containerPort": nic.get("port"),
                "url": url,
            })

    logger.info(f"Endpoint discovery: tenant={tenant_slug}, {len(endpoints)} endpoint(s)")

    return {
        "appInstanceId": appInstanceId,
        "endpoints": endpoints,
    }
