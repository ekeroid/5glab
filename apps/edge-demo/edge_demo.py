#!/usr/bin/env python3
"""
edge_demo.py — minimal CAMARA Edge Compute walkthrough.

Demonstrates the full platform lifecycle against the LTH 5G Lab edge.
The platform is generic — any container can run. This script uses a
public nginx echo image; the only application-specific data is the
DEMO_MANIFEST dict below, which is what gets POSTed to /apps.

Lifecycle (same shape as the EdgeVision deck):

  1. Discover edge cloud zones reachable from this device.
  2. Register the app manifest (image, ports, resources, probes).
  3. Instantiate the app on a chosen zone.
  4. Poll until the instance is ready.
  5. Discover the per-port endpoint URLs.
  6. Call the container directly (host:port returned by discovery —
     no proxy on the data plane).
  7. Terminate the instance.

The CAMARA NEF-shim is on the control plane only. Once endpoints are
returned in step 5, traffic goes straight to the pod's NodePort.
"""

import argparse
import sys
import time

import requests

DEFAULT_API = "http://camara.5glab.control.lth.se"
DEFAULT_DEVICE_IP = "10.45.0.2"

# Two zones are advertised today; defaults to LTH (lowest latency for UEs on
# the 5G network). Override with --zone xerces-cloud-zone to run on the
# Ericsson OpenStack cluster (NVIDIA V100, slower cold start).
KNOWN_ZONES = ("lth-5glab-gpu-zone", "xerces-cloud-zone")
DEFAULT_ZONE = "lth-5glab-gpu-zone"

# A small, public, fast-pulling HTTP echo image — gives a different response
# every refresh, so it's obvious the request reached a real container.
DEMO_MANIFEST = {
    "appName": "edge-demo-hello",
    "appProvider": "edge-demo",
    "appSoftwareVersion": "1.0.0",
    "packageType": "CONTAINER",
    "containerSpec": {
        "image": "nginxdemos/hello:plain-text",
        "readinessProbe": {
            "http": {"path": "/", "port": 80},
            "initialDelaySeconds": 2,
            "periodSeconds": 2,
        },
    },
    "requiredResources": {
        "cpu": "100m",
        "memory": 64,
    },
    "componentSpec": [
        {
            "componentName": "web",
            "networkInterfaces": [
                {"name": "http", "port": 80, "protocol": "TCP"},
            ],
        }
    ],
}


def step(n, title):
    print(f"\n[{n}/7] {title}")


def detail(label, value):
    print(f"      {label:<10}{value}")


def discover_zones(api, device_ip, headers, preferred_zone=None):
    step(1, "Discover edge cloud zones")
    url = f"{api}/simple-edge-discovery/v0/edge-cloud-zones"
    print(f"      GET {url}?device-ip={device_ip}")
    r = requests.get(url, params={"device-ip": device_ip}, headers=headers, timeout=10)
    r.raise_for_status()
    zones = r.json().get("edgeCloudZones", [])
    print(f"      → {len(zones)} zone(s):")
    for z in zones:
        caps = z.get("capabilities", {})
        gpu = f"{caps.get('gpuCount', '?')}× {caps.get('gpuModel', 'no GPU')}" if caps.get("gpuAvailable") else "CPU only"
        marker = "  ← selected" if z["edgeCloudZoneId"] == preferred_zone else ""
        print(f"        - {z['edgeCloudZoneId']:<25} {gpu}{marker}")
    if not zones:
        sys.exit("No edge zones available for this device.")
    if preferred_zone:
        for z in zones:
            if z["edgeCloudZoneId"] == preferred_zone:
                return z
        sys.exit(f"Requested zone {preferred_zone!r} not advertised. Got: "
                 f"{[z['edgeCloudZoneId'] for z in zones]}")
    return zones[0]


def register_app(api, manifest, headers):
    step(2, "Register the app manifest")
    url = f"{api}/edge-app-management/v0/apps"
    print(f"      POST {url}")
    detail("image", manifest["containerSpec"].get("image"))
    detail("ports", [n["port"] for c in manifest["componentSpec"] for n in c["networkInterfaces"]])
    detail("resources", manifest["requiredResources"])
    r = requests.post(url, json=manifest, headers=headers, timeout=15)
    r.raise_for_status()
    app_id = r.json()["appId"]
    print(f"      → appId={app_id}")
    return app_id


def instantiate(api, app_id, zone_id, headers):
    step(3, "Instantiate the app on the zone")
    url = f"{api}/edge-app-management/v0/app-instances"
    body = {"appId": app_id, "edgeCloudZoneId": zone_id}
    print(f"      POST {url}")
    print(f"      body: {body}")
    r = requests.post(url, json=body, headers=headers, timeout=30)
    r.raise_for_status()
    inst_id = r.json()["appInstanceId"]
    print(f"      → appInstanceId={inst_id}")
    return inst_id


def wait_ready(api, inst_id, headers, timeout=180, interval=2):
    step(4, "Poll until ready")
    url = f"{api}/edge-app-management/v0/app-instances/{inst_id}"
    print(f"      GET {url}")
    deadline = time.time() + timeout
    last_phase = None
    while time.time() < deadline:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        s = r.json()
        phase = s.get("phase")
        if phase != last_phase:
            print(f"      phase={phase:<12} status={s.get('status')}  {s.get('message','')}")
            last_phase = phase
        if s.get("status") == "ready":
            return s
        if s.get("status") == "failed":
            sys.exit(f"Instance failed: {s.get('message')}")
        time.sleep(interval)
    sys.exit(f"Instance not ready within {timeout}s")


def get_endpoints(api, inst_id, headers):
    step(5, "Discover per-port endpoints")
    url = f"{api}/application-endpoint-discovery/v0/endpoints"
    print(f"      GET {url}?appInstanceId={inst_id}")
    r = requests.get(url, params={"appInstanceId": inst_id}, headers=headers, timeout=10)
    r.raise_for_status()
    eps = r.json()["endpoints"]
    for ep in eps:
        print(f"      - {ep['name']:<8} ({ep['protocol']}/{ep['containerPort']})  {ep['url']}")
    return {ep["name"]: ep for ep in eps}


def call_container(http_endpoint):
    """Step 6 — talk to the pod directly.

    The URL came from /endpoints in step 5. It is the pod's NodePort,
    reachable on the cluster's public IP. The NEF-shim is NOT on this
    path; the request goes straight to kube-proxy → pod.
    """
    step(6, "Call the container directly (data plane bypasses NEF-shim)")
    print(f"      GET {http_endpoint}/")
    # Deliberately do NOT carry the CAMARA host header here — this is a
    # different host:port and the upstream container has its own routing.
    r = requests.get(f"{http_endpoint}/", timeout=10)
    r.raise_for_status()
    body = r.text.strip().splitlines()
    for line in body[:6]:
        print(f"      | {line}")
    if len(body) > 6:
        print(f"      | ... ({len(body) - 6} more lines)")


def terminate(api, inst_id, headers):
    step(7, "Terminate the instance")
    url = f"{api}/edge-app-management/v0/app-instances/{inst_id}"
    print(f"      DELETE {url}")
    r = requests.delete(url, headers=headers, timeout=15)
    print(f"      → HTTP {r.status_code}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", default=DEFAULT_API, help=f"Base URL (default: {DEFAULT_API})")
    p.add_argument("--device-ip", default=DEFAULT_DEVICE_IP, help=f"Source device IP (default: {DEFAULT_DEVICE_IP})")
    p.add_argument("--zone", default=DEFAULT_ZONE, choices=KNOWN_ZONES,
                   help=f"Edge zone to instantiate on (default: {DEFAULT_ZONE})")
    p.add_argument("--host-header", default=None,
                   help="Override the Host header on CAMARA calls "
                        "(use with --api http://<gateway-ip> when DNS isn't set up)")
    p.add_argument("--ready-timeout", type=int, default=180,
                   help="How long to wait for the pod to become ready, in seconds (default: 180)")
    p.add_argument("--keep", action="store_true", help="Skip teardown — leave the instance running")
    args = p.parse_args()

    # CAMARA control-plane calls go through the gateway. Only that path needs
    # the Host header override when DNS isn't set up. The data-plane call in
    # step 6 talks to the pod's NodePort directly and never needs it.
    headers = {"Host": args.host_header} if args.host_header else {}

    print(f"CAMARA edge: {args.api}  device-ip={args.device_ip}  zone={args.zone}")

    inst_id = None
    try:
        zone = discover_zones(args.api, args.device_ip, headers, preferred_zone=args.zone)
        app_id = register_app(args.api, DEMO_MANIFEST, headers)
        inst_id = instantiate(args.api, app_id, zone["edgeCloudZoneId"], headers)
        wait_ready(args.api, inst_id, headers, timeout=args.ready_timeout)
        eps = get_endpoints(args.api, inst_id, headers)
        call_container(eps["http"]["url"])
        print("\n✓ Demo succeeded.")
    except requests.exceptions.ConnectionError as e:
        print(f"\nERROR: cannot reach {args.api}", file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        print("  Hint: VPN to the LTH lab up? DNS resolves the API hostname?", file=sys.stderr)
        sys.exit(2)
    except requests.exceptions.HTTPError as e:
        print(f"\nERROR: HTTP {e.response.status_code} from API", file=sys.stderr)
        print(f"  body: {e.response.text[:500]}", file=sys.stderr)
        sys.exit(2)
    finally:
        if inst_id and not args.keep:
            try:
                terminate(args.api, inst_id, headers)
            except requests.RequestException as e:
                print(f"  warning: teardown failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
