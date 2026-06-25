"""
Edge Application Management — CAMARA app registration and instance lifecycle.

Implements the CAMARA Edge Application Management API:
  - POST /apps: Register an app manifest (returns appId)
  - POST /app-instances: Instantiate app on a zone (triggers k8s resource creation)
  - GET /app-instances/{id}: Poll instance status (instantiating → ready)
  - DELETE /app-instances/{id}: Terminate instance (deletes k8s resources)

Each operation is tenant-scoped via source IP.
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request

import k8s_manager
import tenant as tenant_mod

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/edge-app-management/v0")

# In-memory stores (sufficient for demo)
_apps: dict[str, dict] = {}  # appId → manifest
_instances: dict[str, dict] = {}  # appInstanceId → instance record


@router.post("/apps")
async def register_app(body: dict, request: Request):
    """Register a CAMARA app manifest."""
    tenant_slug = tenant_mod.get_tenant_slug(request)
    tenant_ip = tenant_mod.get_tenant_id(request)

    app_name = body.get("appName")
    if not app_name:
        raise HTTPException(400, "appName is required")
    if not body.get("containerSpec"):
        raise HTTPException(400, "containerSpec is required")

    app_id = str(uuid.uuid4())
    _apps[app_id] = {**body, "tenant": tenant_ip}

    logger.info(f"App registered: appId={app_id}, appName={app_name}, tenant={tenant_slug}")
    return {"appId": app_id, "appName": app_name, "status": "registered"}


@router.post("/app-instances")
async def create_instance(body: dict, request: Request):
    """Instantiate an app on an edge zone — triggers k8s resource creation."""
    tenant_slug = tenant_mod.get_tenant_slug(request)
    tenant_ip = tenant_mod.get_tenant_id(request)

    app_id = body.get("appId")
    zone_id = body.get("edgeCloudZoneId")

    if not app_id or app_id not in _apps:
        raise HTTPException(404, f"App {app_id} not found")

    manifest = _apps[app_id]
    if manifest.get("tenant") != tenant_ip:
        raise HTTPException(403, "App not owned by this tenant")

    # Create k8s resources on the requested zone
    instance_id = str(uuid.uuid4())
    k8s_manager.create_instance(tenant_slug, manifest, zone_id=zone_id or k8s_manager.ZONE_LOCAL)

    _instances[instance_id] = {
        "appInstanceId": instance_id,
        "appId": app_id,
        "edgeCloudZoneId": zone_id,
        "tenant_ip": tenant_ip,
        "tenant_slug": tenant_slug,
        "status": "instantiating",
    }

    logger.info(f"Instance created: id={instance_id}, tenant={tenant_slug}, zone={zone_id}")
    return {"appInstanceId": instance_id, "status": "instantiating"}


@router.get("/app-instances/{app_instance_id}")
async def get_instance(app_instance_id: str, request: Request):
    """Poll instance status — checks k8s pod conditions for detailed breakdown."""
    tenant_ip = tenant_mod.get_tenant_id(request)

    if app_instance_id not in _instances:
        raise HTTPException(404, "Instance not found")

    instance = _instances[app_instance_id]
    if instance["tenant_ip"] != tenant_ip:
        raise HTTPException(403, "Instance not owned by this tenant")

    detail = k8s_manager.get_instance_status_detail(instance["tenant_slug"])
    instance["status"] = detail["status"]

    return {
        "appInstanceId": app_instance_id,
        "status": detail["status"],
        "phase": detail["phase"],
        "message": detail["message"],
        "phaseOrder": detail.get("phaseOrder"),
        "createdAt": detail.get("createdAt"),
        "scheduledAt": detail.get("scheduledAt"),
        "containerStartedAt": detail.get("containerStartedAt"),
        "phaseStartedAt": detail.get("phaseStartedAt"),
    }


@router.delete("/app-instances/{app_instance_id}", status_code=204)
async def delete_instance(app_instance_id: str, request: Request):
    """Terminate an app instance — deletes all k8s resources for the tenant."""
    tenant_ip = tenant_mod.get_tenant_id(request)

    if app_instance_id not in _instances:
        raise HTTPException(404, "Instance not found")

    instance = _instances[app_instance_id]
    if instance["tenant_ip"] != tenant_ip:
        raise HTTPException(403, "Instance not owned by this tenant")

    k8s_manager.delete_instance(instance["tenant_slug"])
    del _instances[app_instance_id]

    logger.info(f"Instance terminated: id={app_instance_id}, tenant={instance['tenant_slug']}")
