# EdgeVision — CAMARA Edge Compute Offloading Demo

YOLOv8 object detection offloaded to a GPU edge node via the CAMARA API.
Demonstrates the full MEC lifecycle: edge discovery, app registration,
instantiation, endpoint discovery, inference via proxy, and teardown.

## Architecture

```
[laptop]
  camera-api :8081  ──GET /frame──────────────────────┐
  client                                               │
    │                                                  │
    ├──GET /simple-edge-discovery──────────────────┐   │
    ├──POST /edge-app-management/apps              │   │
    ├──POST /edge-app-management/app-instances     │   │
    ├──GET  /edge-app-management/app-instances/{id}│   │
    └──GET  /application-endpoint-discovery        │   │
                                                   ▼   ▼
[k8s cluster: edgevision namespace]
  Gateway → HTTPRoute → nef-shim
    ├── discovery.py
    ├── app_management.py  ──creates──► {tenant}-triton Deployment (GPU)
    ├── endpoint_discovery.py           {tenant}-triton Service
    └── proxy.py  ◄──POST /proxy/{tenant}/v2/models/yolov8n/infer
                      (data plane, proxied to ClusterIP Triton)
```

## Prerequisites

- Docker & Docker Compose
- kubectl with access to the lab k8s cluster
- Python 3.12+ (for local model export)
- GPU node with `nvidia.com/gpu.present=true` label in the cluster
- Envoy Gateway controller installed (GatewayClass: `eg`)

## Quick Start

### Phase 1: Download demo images

```bash
./scripts/setup_demo_images.sh
```

### Phase 2: Deploy NEF-shim to cluster

```bash
./scripts/deploy_nef.sh
```

### Phase 3: Run the client

**Local mode** (CPU inference on laptop):
```bash
docker compose up
```

**Edge mode** (GPU inference via CAMARA API):
```bash
docker compose -f docker-compose.yml -f docker-compose.edge.yml up
```

## How Multi-Tenancy Works

Tenant identity is derived from the source IP of each request. When a client
calls the CAMARA APIs, the NEF-shim:

1. Extracts the client IP from the request
2. Computes a deterministic slug: `t-{md5(ip)[:8]}`
3. Names all k8s resources with this slug (PVC, Job, Deployment, Service)
4. Scopes the proxy endpoint to this slug

This allows multiple clients to each get their own isolated Triton GPU
instance without any authentication tokens.

## CAMARA Flow Walkthrough

The client performs these steps at startup in edge mode:

| Step | API Call | What Happens |
|------|----------|--------------|
| 1 | `GET /simple-edge-discovery/v0/edge-cloud-zones` | Discover available GPU zones |
| 2 | `POST /edge-app-management/v0/apps` | Register app manifest (Triton + YOLOv8) |
| 3 | `POST /edge-app-management/v0/app-instances` | Trigger k8s resource creation |
| 4 | `GET /edge-app-management/v0/app-instances/{id}` | Poll until ready (~2-5 min) |
| 5 | `GET /application-endpoint-discovery/v0/endpoints` | Get proxy URL for inference |

After setup, each frame is sent via:
```
POST /proxy/{tenant-slug}/v2/models/yolov8n/infer
```

## Monitoring Output

Watch annotated frames in real-time:
```bash
# macOS
open output/latest.jpg    # refreshes every FRAME_INTERVAL_MS

# or watch the directory
ls -lt output/ | head
```

The status bar shows mode (LOCAL blue / EDGE green), per-frame latency,
rolling average, detection count, and edge instance ID.

## Expected Performance

| Mode | Discovery | Inference | Total | Hardware |
|------|-----------|-----------|-------|----------|
| Local CPU | n/a | ~200-400ms | ~250-450ms | Laptop CPU |
| Edge GPU | ~5ms | ~15-30ms | ~20-40ms | NVIDIA L40S (k8s) |

## Teardown

```bash
# Stop client
docker compose down

# Remove all k8s resources (including tenant Triton instances)
kubectl delete namespace edgevision
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| GatewayClass mismatch | `kubectl get gatewayclass` — must be `eg` |
| Model loader stuck | `kubectl logs job/{tenant}-model-loader -n edgevision` |
| Triton not scheduling | `kubectl describe pod -l app=edgevision -n edgevision` — check GPU node |
| Proxy returning 502 | Instance not ready yet — poll status first |
| DNS not resolving | Use port-forward: see deploy_nef.sh output |
