"""
CAMARA control plane client — Edge Discovery, App Management, Endpoint Discovery.

Implements the full CAMARA MEC lifecycle: discover edge zones, register an app
manifest, instantiate it on a zone (triggering GPU Triton deployment), poll
readiness, discover the proxy endpoint, and terminate on shutdown.

Every API call is logged in full for demo visibility.
"""

import logging
import socket

import requests

from config import CAMARA_API_URL, CAMARA_HOST_HEADER, CAMARA_PROXY_BASE_URL, EDGE_TRANSPORT

logger = logging.getLogger(__name__)

# Optional Host header override (for port-forward through gateway)
_HEADERS = {"Host": CAMARA_HOST_HEADER} if CAMARA_HOST_HEADER else {}

# CAMARA App Manifest for YOLOv8 Triton deployment
APP_MANIFEST = {
    "appName": "edgevision-yolov8",
    "appProvider": "lth-frtn90",
    "appSoftwareVersion": "1.0.0",
    "packageType": "CONTAINER",
    "containerSpec": {
        "imageRegistry": "ghcr.io/ekeroid/5glab",
        "imageName": "edgevision-infer",
        "imageTag": "latest",
    },
    "requiredResources": {
        "cpu": 4,
        "memory": 8192,
        "gpu": 1,
    },
    "componentSpec": [
        {
            "componentName": "triton",
            "networkInterfaces": [
                {"port": 8000, "protocol": "TCP", "name": "http-inference"},
                {"port": 8002, "protocol": "TCP", "name": "metrics"},
            ],
        }
    ],
}


def _get_local_ip() -> str:
    """Get the local IP address used for outbound connections."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "10.45.0.2"


def discover_zone() -> dict:
    """GET /simple-edge-discovery/v0/edge-cloud-zones — discover nearest edge zone."""
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

    zone = zones[0]
    logger.info(f"[CAMARA] Discovered zone: {zone.get('edgeCloudZoneName')}")
    logger.info(f"[CAMARA]   Capabilities: {zone.get('capabilities')}")
    return zone


def register_app(manifest: dict = None) -> str:
    """POST /edge-app-management/v0/apps — register app manifest, get appId."""
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


def get_instance_status(app_instance_id: str) -> str:
    """GET /edge-app-management/v0/app-instances/{id} — poll instance status."""
    url = f"{CAMARA_API_URL}/edge-app-management/v0/app-instances/{app_instance_id}"

    resp = requests.get(url, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    status = data.get("status", "unknown")
    logger.info(f"[CAMARA] Instance {app_instance_id[:8]}... status: {status}")
    return status


def get_endpoint(app_instance_id: str) -> str:
    """GET /application-endpoint-discovery/v0/endpoints — get inference endpoint."""
    url = f"{CAMARA_API_URL}/application-endpoint-discovery/v0/endpoints"
    params = {"appInstanceId": app_instance_id}

    logger.info(f"[CAMARA] GET {url} ?appInstanceId={app_instance_id[:8]}...")
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if EDGE_TRANSPORT == "grpc":
        endpoint = data.get("grpcEndpoint", data.get("endpoint"))
        logger.info(f"[CAMARA] gRPC endpoint: {endpoint}")
    else:
        endpoint = data.get("httpEndpoint", data.get("endpoint"))
        logger.info(f"[CAMARA] HTTP endpoint: {endpoint}")

    return endpoint


def terminate_app(app_instance_id: str):
    """DELETE /edge-app-management/v0/app-instances/{id} — teardown instance."""
    url = f"{CAMARA_API_URL}/edge-app-management/v0/app-instances/{app_instance_id}"

    logger.info(f"[CAMARA] DELETE {url}")
    resp = requests.delete(url, headers=_HEADERS, timeout=30)
    if resp.status_code == 204:
        logger.info(f"[CAMARA] Instance {app_instance_id[:8]}... terminated")
    else:
        logger.warning(f"[CAMARA] Terminate returned {resp.status_code}: {resp.text}")
