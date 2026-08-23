# EdgeVision — CAMARA Edge Compute Offloading Demo

YOLOv8 object detection offloaded to a GPU edge node via the CAMARA API.
Demonstrates the full MEC lifecycle: edge discovery, app registration,
instantiation, endpoint discovery, and dual-transport inference (gRPC + HTTP).

## Architecture

```
┌─────────────── Laptop ───────────────┐
│                                      │
│  camera-api :8081   (JPEG frames)    │
│  GUI client         (Tkinter)        │
│    ├─ LOCAL mode: CPU inference      │
│    └─ EDGE mode:                     │
│         ├─ CAMARA API (setup)        │
│         └─ gRPC or HTTP (inference)  │
│                                      │
└──────────┬───────────────────────────┘
           │  port 80 (Envoy Gateway)
           ▼
┌─── K8s cluster (edgevision ns) ──────┐
│                                      │
│  Gateway → GRPCRoute + HTTPRoute     │
│      │                               │
│      ▼                               │
│  nef-shim (FastAPI + gRPC proxy)     │
│    ├── CAMARA APIs (control plane)   │
│    ├── gRPC proxy :50051 → tenant    │
│    └── HTTP proxy /proxy/{t}/infer   │
│                                      │
│  {tenant}-infer (per-tenant, GPU)    │
│    ├── Triton Server (TRT FP16)      │
│    └── Sidecar (gRPC :50051, HTTP)   │
│                                      │
└──────────────────────────────────────┘
```

## Quick Start

```bash
# Start the demo (camera-api + GUI)
./demo.sh start

# Stop everything and tear down edge instance
./demo.sh stop

# Restart
./demo.sh restart
```

## GUI Controls

| Key | Action |
|-----|--------|
| `E` | Toggle LOCAL / EDGE mode |
| `T` | Toggle gRPC / HTTP transport (edge mode) |
| `Q` | Quit |

The status bar shows:
- Mode (LOCAL blue / EDGE green)
- Transport (gRPC / HTTP)
- Per-frame latency with breakdown bar
- Detection count and class labels

The latency breakdown bar shows:
- **Preprocess** (orange) — resize + normalize on GPU pod
- **Inference** (red) — TensorRT FP16 forward pass
- **Postprocess** (yellow) — NMS + box decoding
- **Network** (blue) — round-trip overhead

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EDGE_TRANSPORT` | `grpc` | Transport for inference: `grpc` or `http` |
| `FRAME_INTERVAL_MS` | `200` | Capture interval in milliseconds |
| `CAMERA_API_URL` | `http://localhost:8081` | Camera-api endpoint |
| `CAMARA_API_URL` | `http://camara.5glab.control.lth.se` | CAMARA API gateway |

## Performance

| Mode | Startup | Inference | Total | Hardware |
|------|---------|-----------|-------|----------|
| Local CPU | instant | ~300ms | ~350ms | Laptop CPU |
| Edge gRPC | ~10 min | ~12-18ms | ~15-25ms | NVIDIA L40S |
| Edge HTTP | ~10 min | ~12-18ms | ~20-35ms | NVIDIA L40S |

Startup is currently ~10 minutes because the pod builds TensorRT engines
on cold boot. We plan to reduce this later with improved boot-time handling
and/or persistent engine caching.

## CAMARA API Flow

The client performs these steps when entering EDGE mode:

| Step | API | Action |
|------|-----|--------|
| 1 | `GET /simple-edge-discovery/v0/edge-cloud-zones` | Discover GPU zones |
| 2 | `POST /edge-app-management/v0/apps` | Register app manifest |
| 3 | `POST /edge-app-management/v0/app-instances` | Create k8s resources |
| 4 | `GET /edge-app-management/v0/app-instances/{id}` | Poll until ready |
| 5 | `GET /application-endpoint-discovery/v0/endpoints` | Get inference endpoint |

Phase breakdown during step 4 (shown in GUI):
- **scheduling** — waiting for pod to be assigned to GPU node
- **pulling** — downloading container image
- **starting** — container initializing
- **waiting_ready** — Triton loading model, readiness probe pending
- **ready** — inference server accepting requests

## Multi-Tenancy

Tenant identity is derived from the source IP (via `x-forwarded-for` set by
Envoy). Each tenant gets isolated k8s resources named `t-{md5(ip)[:8]}`.

## Project Structure

```
edgevision/
├── demo.sh                  # Start/stop script
├── camera-api/              # JPEG frame server (webcam or test images)
├── client/                  # GUI + inference client
│   ├── gui.py               # Tkinter GUI with controls
│   ├── detector_remote.py   # gRPC + HTTP inference client
│   ├── camara.py            # CAMARA API wrapper
│   └── config.py            # Environment-based config
├── nef-shim/                # CAMARA API server + proxies
│   ├── main.py              # FastAPI app + gRPC proxy thread
│   ├── grpc_proxy.py        # Tenant-scoped gRPC forwarding
│   ├── k8s_manager.py       # K8s resource lifecycle
│   └── app_management.py    # CAMARA app instance API
└── triton-infer/            # Inference container (GPU)
    ├── Dockerfile           # Triton runtime + model assets
    ├── models/              # YOLO model files; TRT engines are built on boot
    └── sidecar/             # gRPC + HTTP inference server
```

## Building the Inference Image

```bash
cd triton-infer
docker build -t ghcr.io/ekeroid/5glab/edgevision-infer:latest .
docker push ghcr.io/ekeroid/5glab/edgevision-infer:latest
```

The image does not currently rely on a pre-built TensorRT engine; engines
are built on pod startup. This is why first EDGE startup is about 10 minutes
until boot-time caching is improved.

## Teardown

```bash
# Stop local processes
./demo.sh stop

# Remove all k8s resources
kubectl delete namespace edgevision
```
