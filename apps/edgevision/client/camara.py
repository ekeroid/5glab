"""
CAMARA control plane client — Edge Discovery, App Management, Endpoint Discovery.

Implements the full CAMARA MEC lifecycle: discover edge zones, register an app
manifest, instantiate it on a zone, poll readiness, discover per-port endpoints,
and terminate on shutdown.

The edgevision-infer image listens on:
  - 8080/TCP  HTTP — /infer, /health
  - 50051/TCP gRPC — InferenceService

These ports are declared in APP_MANIFEST.componentSpec.networkInterfaces.
After /endpoints, we resolve the URLs by port name.
"""

import logging
import socket

import requests

from config import CAMARA_API_URL, CAMARA_HOST_HEADER, EDGE_TRANSPORT

logger = logging.getLogger(__name__)

_HEADERS = {"Host": CAMARA_HOST_HEADER} if CAMARA_HOST_HEADER else {}

# CAMARA App Manifest for the edgevision-infer container.
APP_MANIFEST = {
    "appName": "edgevision-yolov8",
    "appProvider": "lth-frtn90",
    "appSoftwareVersion": "1.0.0",
    "packageType": "CONTAINER",
    "containerSpec": {
        "imageRegistry": "ghcr.io/ekeroid/5glab",
        "imageName": "edgevision-infer",
        "imageTag": "latest",
        "readinessProbe": {
            "http": {"path": "/health", "port": 8080},
            "initialDelaySeconds": 30,
            "periodSeconds": 5,
        },
        "livenessProbe": {
            "http": {"path": "/health", "port": 8080},
            "initialDelaySeconds": 1200,
            "periodSeconds": 15,
            "failureThreshold": 5,
        },
    },
    "requiredResources": {
        "cpu": 2,
        "memory": 8192,
        "gpu": 1,
    },
    "componentSpec": [
        {
            "componentName": "infer",
            "networkInterfaces": [
                {"name": "http", "port": 8080, "protocol": "TCP"},
                {"name": "grpc", "port": 50051, "protocol": "TCP"},
            ],
        }
    ],
}


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "10.45.0.2"


def discover_zone() -> dict:
    """GET /simple-edge-discovery/v0/edge-cloud-zones — first zone."""
    zones = discover_zones()
    zone = zones[0]
    logger.info(f"[CAMARA] Discovered zone: {zone.get('edgeCloudZoneName')}")
    logger.info(f"[CAMARA]   Capabilities: {zone.get('capabilities')}")
    return zone


def discover_zones() -> list[dict]:
    """GET /simple-edge-discovery/v0/edge-cloud-zones — all zones."""
    local_ip = _get_local_ip()
    url = f"{CAMARA_API_URL}/simple-edge-discovery/v0/edge-cloud-zones"
    params = {"device-ip": local_ip}

    logger.info(f"[CAMARA] GET {url} ?device-ip={local_ip}")
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    logger.info(f"[CAMARA] Response: {resp.status_code}")

    zones = data.get("edgeCloudZones", [])
    if not zones:
        raise RuntimeError("No edge cloud zones discovered")

    logger.info(f"[CAMARA] Found {len(zones)} zone(s)")
    return zones


def register_app(manifest: dict | None = None) -> str:
    """POST /edge-app-management/v0/apps — register manifest, get appId."""
    if manifest is None:
        manifest = APP_MANIFEST

    url = f"{CAMARA_API_URL}/edge-app-management/v0/apps"
    logger.info(f"[CAMARA] POST {url}")
    logger.info(f"[CAMARA]   Manifest: appName={manifest['appName']}, "
                f"gpu={manifest['requiredResources']['gpu']}")

    resp = requests.post(url, json=manifest, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    app_id = data["appId"]
    logger.info(f"[CAMARA] Registered app: appId={app_id}, status={data.get('status')}")
    return app_id


def instantiate_app(app_id: str, zone_id: str) -> str:
    """POST /edge-app-management/v0/app-instances — create instance on zone."""
    url = f"{CAMARA_API_URL}/edge-app-management/v0/app-instances"
    body = {"appId": app_id, "edgeCloudZoneId": zone_id}

    logger.info(f"[CAMARA] POST {url}")
    logger.info(f"[CAMARA]   appId={app_id}, zoneId={zone_id}")

    resp = requests.post(url, json=body, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    instance_id = data["appInstanceId"]
    logger.info(f"[CAMARA] Instance created: appInstanceId={instance_id}, "
                f"status={data.get('status')}")
    return instance_id


def get_instance_status(app_instance_id: str) -> dict:
    """GET /edge-app-management/v0/app-instances/{id}."""
    url = f"{CAMARA_API_URL}/edge-app-management/v0/app-instances/{app_instance_id}"

    resp = requests.get(url, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    status = data.get("status", "unknown")
    phase = data.get("phase", "")
    message = data.get("message", "")
    logger.info(f"[CAMARA] Instance {app_instance_id[:8]}... status={status} phase={phase}")
    return {"status": status, "phase": phase, "message": message}


def get_endpoints(app_instance_id: str) -> dict[str, dict]:
    """
    GET /application-endpoint-discovery/v0/endpoints.

    Returns {port_name: {url, protocol, containerPort}}.
    """
    url = f"{CAMARA_API_URL}/application-endpoint-discovery/v0/endpoints"
    params = {"appInstanceId": app_instance_id}

    logger.info(f"[CAMARA] GET {url} ?appInstanceId={app_instance_id[:8]}...")
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    endpoints = {ep["name"]: ep for ep in data.get("endpoints", [])}
    for name, ep in endpoints.items():
        logger.info(f"[CAMARA]   {name}: {ep.get('url')}")
    return endpoints


def get_endpoint(app_instance_id: str) -> str:
    """
    Return the URL for the transport selected via EDGE_TRANSPORT.

    Backwards-compatible single-URL helper for the existing client loop.
    """
    endpoints = get_endpoints(app_instance_id)
    name = "grpc" if EDGE_TRANSPORT == "grpc" else "http"
    if name not in endpoints:
        raise RuntimeError(f"Endpoint '{name}' not advertised by edge "
                           f"(got: {list(endpoints)})")
    return endpoints[name]["url"]


def terminate_app(app_instance_id: str):
    """DELETE /edge-app-management/v0/app-instances/{id}."""
    url = f"{CAMARA_API_URL}/edge-app-management/v0/app-instances/{app_instance_id}"

    logger.info(f"[CAMARA] DELETE {url}")
    resp = requests.delete(url, headers=_HEADERS, timeout=30)
    if resp.status_code == 204:
        logger.info(f"[CAMARA] Instance {app_instance_id[:8]}... terminated")
    else:
        logger.warning(f"[CAMARA] Terminate returned {resp.status_code}: {resp.text}")
