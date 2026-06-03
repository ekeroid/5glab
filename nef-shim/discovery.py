"""
Simple Edge Discovery — CAMARA edge zone discovery endpoint.

Returns available edge cloud zones with their capabilities.
This is the entry point for clients to discover what compute
resources are available at the network edge.
"""

import logging

from fastapi import APIRouter, Query

from config import EXTERNAL_HOSTNAME

logger = logging.getLogger(__name__)
router = APIRouter()

EDGE_ZONES = {
    "edgeCloudZones": [
        {
            "edgeCloudZoneId": "lth-5glab-gpu-zone",
            "edgeCloudZoneName": "LTH 5G Lab GPU Edge",
            "edgeCloudProvider": "lth-kubernetes",
            "status": "active",
            "capabilities": {
                "gpuAvailable": True,
                "gpuModel": "NVIDIA L40S",
                "gpuCount": 2,
                "cpuCores": 128,
                "memoryGB": 768,
            },
            "location": {
                "latitude": 55.7115,
                "longitude": 13.2108,
            },
        }
    ]
}


@router.get("/simple-edge-discovery/v0/edge-cloud-zones")
async def discover_zones(device_ip: str = Query(alias="device-ip", default="0.0.0.0")):
    """Discover available edge cloud zones for a given device IP."""
    logger.info(f"Edge discovery request for device-ip={device_ip}")
    return EDGE_ZONES
