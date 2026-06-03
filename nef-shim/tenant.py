"""
Tenant identity — Derives tenant ID from the source IP of each request.

All k8s resources for a tenant use a deterministic slug derived from
the source IP via MD5 hash. This enables multi-tenant isolation without
authentication — suitable for a lab/demo environment.
"""

import hashlib

from fastapi import Request


def get_tenant_id(request: Request) -> str:
    """Get the raw tenant identifier (source IP via x-forwarded-for or peer)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host


def get_tenant_slug(request: Request) -> str:
    """Get the k8s-safe tenant slug: t-{md5[:8]} of the source IP."""
    tenant_ip = get_tenant_id(request)
    return "t-" + hashlib.md5(tenant_ip.encode()).hexdigest()[:8]
