# EdgeVision — How it actually works

A current, code-grounded description of the LTH EdgeVision demo: how the API
is wired, how an app gets deployed, how a frame turns into detections, and
where the implementation has drifted from the EdgeVision-Overview slide deck.

Last verified against running system on 2026-06-23 (commit-equivalent: GUI
running on 5G UE, two clusters: LTH `5glab-kubeconfig`, Xerces `zone2-kubeconfig`).

---

## 1. Components and where they run

```
┌────────────────────────────────── Laptop (5G UE) ───────────────────────────────────┐
│                                                                                     │
│   camera-api  :8081                       client/gui.py  (Tkinter)                  │
│   (FastAPI, MJPEG-ish /frame)             ├─ Local detector (CPU, ultralytics)      │
│                                           ├─ Remote detector  (gRPC | HTTP)         │
│                                           ├─ CAMARA client    (camara.py)           │
│                                           └─ Latency collector / display            │
│                                                                                     │
└─────────────────┬───────────────────────────────────────────────┬───────────────────┘
                  │ CAMARA control plane                          │ data plane
                  │ http://camara.5glab.control.lth.se            │ direct to NodePort
                  ▼                                               ▼
┌─── LTH cluster (kubeconfig: 5glab-kubeconfig) ──────┐    ┌── Xerces cluster ─────┐
│                                                     │    │   (OpenStack VMs)     │
│  ns: edgevision                                     │    │                       │
│  ┌─────────────────┐    ┌──────────────────────┐    │    │  ns: edgevision       │
│  │ nef-shim         │    │ t-<slug>-app         │   │    │  ┌──────────────────┐ │
│  │ (FastAPI, K8s    │──▶ │ (Triton + sidecar,   │   │    │  │ t-<slug>-app     │ │
│  │  api client to   │    │  GPU pod, L40S)      │   │    │  │ (same image,     │ │
│  │  BOTH clusters)  │    └──────────────────────┘    │    │  │  V100)           │ │
│  └─────────────────┘             :8080  :50051       │    │  └──────────────────┘ │
│   reachable from:                NodePort (kube-proxy)    │    :8080 :50051        │
│   - laptop (CAMARA)                                  │    │    via 129.192.83.16  │
│   - both kube-apiservers (control)                   │    │    (floating IP)      │
└──────────────────────────────────────────────────────┘    └───────────────────────┘
```

Single nef-shim. It holds two Kubernetes API clients — one for LTH (in-cluster
config since nef-shim runs on the LTH cluster), one for Xerces (kubeconfig file
mounted at `/etc/edgevision/zone2-kubeconfig.yaml`). The `zone_id` chosen at
instantiation time picks which client to use.

Inference is **not** proxied through nef-shim. The client connects directly
to the pod's NodePort on the cluster's public ingress IP (LTH gateway for
zone1, OpenStack floating IP for zone2).

---

## 2. The CAMARA API as implemented

OpenAPI spec at `http://camara.5glab.control.lth.se/openapi.json`. Endpoints
that actually exist today:

```
GET    /simple-edge-discovery/v0/edge-cloud-zones
POST   /edge-app-management/v0/apps
POST   /edge-app-management/v0/app-instances
GET    /edge-app-management/v0/app-instances/{id}
DELETE /edge-app-management/v0/app-instances/{id}
GET    /application-endpoint-discovery/v0/endpoints
*      /proxy/{tenant}/{port_name}/{path…}   ← HTTP-only edge proxy, used by edge-demo/
GET    /health
```

**Tenancy** is by source IP. nef-shim reads `x-forwarded-for` (Envoy sets it)
and computes `tenant_slug = "t-" + md5(source_ip)[:8]`. Every register /
instantiate / endpoints / delete call is scoped to that slug. Two laptops from
two NATs get two isolated tenants automatically.

**Storage** is in-memory only. Restarting the nef-shim deployment forgets all
`appId` and instance ids, but the underlying k8s Deployments and Services stay
up; cleanup must then be done with kubectl directly.

---

## 3. The 4-step lifecycle, step by step

What `client/gui.py` does in EDGE mode (entered by pressing `E`). All steps
hit the LTH-side nef-shim regardless of which zone is picked.

### Step 1 — Discover zones
```
GET /simple-edge-discovery/v0/edge-cloud-zones?device-ip=<laptop-IP>
```
Returns a static two-element list (no real discovery yet): `lth-5glab-gpu-zone`
(2× NVIDIA L40S) and `xerces-cloud-zone` (1× NVIDIA Tesla V100). Client picks
one.

### Step 2 — Register the app manifest
```
POST /edge-app-management/v0/apps
{
  "appName": "edgevision-yolov8",
  "containerSpec": {
    "imageRegistry": "ghcr.io/ekeroid/5glab",
    "imageName":     "edgevision-infer",
    "imageTag":      "latest",
    "readinessProbe": {"http": {"path": "/health", "port": 8080},
                       "initialDelaySeconds": 30,  "periodSeconds": 5},
    "livenessProbe":  {"http": {"path": "/health", "port": 8080},
                       "initialDelaySeconds": 1200, "periodSeconds": 15,
                       "failureThreshold": 5}
  },
  "requiredResources": {"cpu": 2, "memory": 8192, "gpu": 1},
  "componentSpec": [{
    "componentName": "infer",
    "networkInterfaces": [
      {"name": "http",  "port": 8080,  "protocol": "TCP"},
      {"name": "grpc",  "port": 50051, "protocol": "TCP"}
    ]
  }]
}
```
Returns `appId`. The manifest stays in nef-shim's memory; nothing is created
on Kubernetes yet.

The 1200 s liveness grace is calibrated for the slowest case: a cold Xerces
V100 pod that has to export ONNX and build TensorRT engines from scratch
(~12-14 min). On L40S the same path is ~3 min.

### Step 3 — Instantiate on a zone
```
POST /edge-app-management/v0/app-instances
{ "appId": "<from step 2>", "edgeCloudZoneId": "lth-5glab-gpu-zone" | "xerces-cloud-zone" }
```
nef-shim translates the manifest into Kubernetes objects on the chosen cluster:

- **Deployment** `t-<slug>-app` (1 replica)
  - container image from `containerSpec`
  - `runtimeClassName: nvidia` if `gpu >= 1`
  - tolerates `nvidia.com/gpu=true:NoSchedule`
  - selects nodes with `nvidia.com/gpu.present=true`
  - `imagePullSecrets: [ghcr-secret]`
- **Service** `t-<slug>-app`
  - `type: NodePort`
  - one port per `networkInterfaces` entry, name preserved

Returns instance id and `{ "status": "instantiating" }`.

### Step 4 — Poll until ready
```
GET /edge-app-management/v0/app-instances/{id}    ← every 2 s
```
nef-shim queries the pod status on the right cluster and synthesises a
phase: `scheduling | pulling | starting | running | ready | failed`. The
client GUI shows this in the bottom-left progress strip.

`ready` ≡ container's HTTP readiness probe passes (sidecar's `GET /health`
returns 200). At that point Triton has both TRT engines loaded and the
sidecar's gRPC and HTTP servers are listening.

Failure modes the GUI surfaces verbatim:
- `failed: Container crash loop: …` — pod is in `CrashLoopBackOff`.
- `failed: <reason>` — pod is in Pod-level Failed state.
- Long stays in `pulling`/`starting` mean a slow ghcr.io pull or a slow
  cold TRT build.

### Step 5 — Endpoint discovery
```
GET /application-endpoint-discovery/v0/endpoints?appInstanceId=<id>
```
Returns one entry per declared `networkInterface`. Current (post-fix) shape
is **symmetric across both zones, no proxy**:

```json
{
  "appInstanceId": "...",
  "endpoints": [
    { "name": "http", "protocol": "TCP", "containerPort": 8080,
      "url": "http://<zone_host>:<nodePort>" },
    { "name": "grpc", "protocol": "TCP", "containerPort": 50051,
      "url": "<zone_host>:<nodePort>" }
  ]
}
```
`<zone_host>` is `camara.5glab.control.lth.se` for LTH and `129.192.83.16`
for Xerces. NodePorts are whatever kube-proxy assigned (30000-32767).

### Step 6 — Inference (the steady state)
The client opens its connections **directly to the pod**:

- **gRPC** path: persistent HTTP/2 channel to `<host>:<nodePort>`. Calls
  `edgevision.InferenceService/Infer` with `InferRequest{jpeg, model, confidence_threshold}`.
- **HTTP** fallback: `POST <url>/infer`, body = raw JPEG bytes,
  `X-Model: detect|seg`, `X-Confidence-Threshold: 0.25`, response is JSON.

Sidecar (inside pod):
1. JPEG decode + resize to 640×640
2. Shared-memory write into Triton's input region
3. `infer()` on the named model (`yolov8n_trt` or `yolov8xseg_trt`)
4. SHM read back of output tensors
5. NMS + mask decode
6. Return `InferResponse{detections[], latency_ms, preprocess_ms, inference_ms, postprocess_ms}`

The sidecar talks to Triton through Triton's gRPC port on localhost, with the
input/output tensors in shared memory so the JPEG bytes are decoded once and
the inference output is read back without an extra serialization hop.

### Step 7 — Teardown
```
DELETE /edge-app-management/v0/app-instances/{id}
DELETE /edge-app-management/v0/app-instances/all       ← shortcut for "this tenant's instance"
```
nef-shim deletes the Deployment and Service on the right cluster. The pod
terminates within a few seconds.

---

## 4. The container — what `edgevision-infer:latest` actually contains

Build context: `triton-infer/`. Resulting image (`ghcr.io/ekeroid/5glab/edgevision-infer:latest`):

| Layer | What it is |
|---|---|
| `nvcr.io/nvidia/tritonserver:23.10-py3` | Base. TensorRT 8.6, supports SM 70 (Volta / V100) through SM 89 (Ada / L40S). |
| apt: `libgl1`, `libglib2.0-0`, `libsm6`, … | OpenCV runtime deps. |
| pip: `fastapi`, `uvicorn`, `tritonclient[grpc]`, `opencv-python-headless`, `grpcio`, `grpcio-tools`, `ultralytics`, `onnx`, `onnxruntime`, `onnxslim` | Sidecar runtime + export tooling. |
| `models/yolov8n_trt/config.pbtxt` + `yolov8xseg_trt/config.pbtxt` | Triton model repository skeleton. **No `.plan` files baked in** — they're built on first boot on the actual GPU. |
| `yolov8n.pt`, `yolov8x-seg.pt` | Ultralytics weights. |
| `sidecar/main.py` + generated `infer_pb2*.py` | gRPC/HTTP server. |
| `export_model.py` | `.pt → ONNX → TRT FP16 engine` pipeline. |
| `entrypoint.sh` | Orchestrates engine builds + Triton + sidecar startup. |

### First-boot timeline

| Step | L40S | V100 |
|---|---|---|
| Build `yolov8n` engine | ~10-30 s | ~5 min |
| Build `yolov8x-seg` engine | ~3-5 min | ~6-8 min |
| Triton load both models | ~10 s | ~10 s |
| **Total before `ready`** | **~3-5 min** | **~12-14 min** |

The engines are GPU-arch-specific. The cache lives in pod-local emptyDir, so
deleting and recreating a pod (same node) rebuilds. **A PersistentVolume here
would eliminate cold-build pain** — open work.

---

## 5. Where the implementation differs from the slide deck

The Keynote deck is one slide ahead of, and one slide behind, the code in
different places. Concrete diffs:

| Topic | Slide deck says | Code actually does |
|---|---|---|
| **HTTP endpoint URL** | "NGINX Ingress (HTTP proxy)" / `host/proxy/<slug>/<name>` | Direct `http://<host>:<nodePort>` — same shape as gRPC. No path-prefix proxy on the inference path; the `/proxy/...` route still exists in nef-shim but is only used by `edge-demo/`. |
| **NEF-Shim role at runtime** | "Remote Detector → NEF-Shim → Triton" line in the System Architecture diagram | NEF-shim is **control plane only**. Once endpoints are returned, the client talks straight to the pod's NodePort. Inference traffic never enters nef-shim. |
| **Edge ingress** | "NGINX Ingress" | The cluster's edge ingress is **Envoy Gateway** (Gateway API). NGINX is not in the picture. |
| **Application manifest network interfaces** | `8000 http-inference`, `8002 metrics`, `componentName: triton` | `8080 http`, `50051 grpc`, `componentName: infer` (the sidecar surface, not Triton's raw API). |
| **"NEF-Shim manages Triton GPU deployments"** | Implied single cluster | Multi-cluster: nef-shim sits on LTH but instantiates on either LTH or Xerces. Zone selection comes from the client's `edgeCloudZoneId`. |
| **"Sidecar: JPEG→Tensor→JSON"** | One row in System Architecture | Sidecar is two endpoints (gRPC `InferenceService/Infer` + HTTP `POST /infer`), both backed by the same SHM→Triton path. gRPC also has `Health`. |
| **Edge zones** | LTH only | Two zones registered (`lth-5glab-gpu-zone`, `xerces-cloud-zone`). The deck does mention "Xerces" once on the Edge Discovery slide but the System Architecture diagram only shows the LTH side. |
| **Number of models** | "yolov8n, yolov8x-seg on L40S" | Same — both models, both architectures. Models are loaded by Triton with `--model-control-mode=explicit --load-model=yolov8n_trt --load-model=yolov8xseg_trt`. |
| **Liveness probe** | Not shown | 1200 s initial delay (raised from 600 s during V100 bring-up; on L40S anything ≥ 600 s would have worked). |
| **Engine portability** | "Subsequent: engines cached, ready in seconds" | True per-node, but not per-pod-recreation: emptyDir is pod-scoped. New pod ≠ engine cache hit. |
| **CAMARA spec alignment slide** | "App Package: pre-onboarded by id" — labeled "Differs" | Still differs — we use an inline `containerSpec`, not an `appPackageSource`. Slide is accurate. |
| **Sequence diagram caption** | `kubectl apply deploy+svc` | Code calls `apps_v1.create_namespaced_deployment()` and `core_v1.create_namespaced_service()` via the python kubernetes client — not `kubectl` shelling out, but the semantics are identical. |
| **Latency breakdown** | Marked "(Placeholder)" — 5G UL 3.2 ms, inference 4.8 ms, total ~13 ms | The "Runtime Properties" slide that comes later has real numbers (gRPC end-to-end 55 ms avg / P95 73 ms over 5G SA). The placeholder slide is aspirational; the runtime-properties slide is correct. |

### What the deck doesn't mention but is true today

- **Tenant isolation by source-IP hash.** No auth, no tokens; a different NAT
  → a different tenant slug → a different deployment, automatically. The
  catch: VPN reconnect can change the source IP, creating an orphan deployment
  in the previous slug's name that won't be reachable by the new slug.
- **Per-zone network reachability gotchas.** Xerces NodePorts (30000-32767)
  needed the OpenStack security group `k8` opened on TCP for the public path
  to work; LTH's gateway accepts NodePort traffic by default.
- **No persistent state.** nef-shim restart drops the in-memory `_apps` and
  `_instances` dicts. The k8s objects survive, but they become orphans because
  CAMARA has no "list" endpoint to discover them. Cleanup needs kubectl.
- **gRPC over VPN reality.** Direct NodePort gRPC works fine over 5G but is
  routinely blocked when the laptop is on the corporate VPN (it MITMs through
  an HTTP proxy on `192.168.65.1:3128` that closes long-lived TCP streams).
  This is the real reason for the "gRPC = primary, HTTP = fallback" framing
  in the deck.

---

## 6. Quick reference — file map

```
edgevision/
├── demo.sh                       # start/stop wrapper (activates .venv, launches camera-api + GUI)
├── camera-api/main.py            # FastAPI MJPEG-ish frame server (:8081)
├── client/
│   ├── gui.py                    # Tkinter UI, mode/transport/model toggles, lifecycle progress strip
│   ├── camara.py                 # CAMARA client + APP_MANIFEST literal
│   ├── detector_remote.py        # gRPC + HTTP transports
│   ├── detector_local.py         # CPU ultralytics fallback
│   ├── display.py                # latency bars, status badges, histogram
│   ├── modem.py                  # Quectel CPE radio metrics poller (SINR / RSRP / PCI)
│   └── infer_pb2*.py             # generated gRPC stubs
├── nef-shim/
│   ├── main.py                   # FastAPI app + lifecycle
│   ├── discovery.py              # static zone list (LTH + Xerces)
│   ├── app_management.py         # register + instantiate + status + delete
│   ├── endpoint_discovery.py     # returns host:port for both zones, both protocols
│   ├── proxy.py                  # HTTP-only path-prefix proxy (used by edge-demo/ only)
│   ├── k8s_manager.py            # multi-cluster k8s client + manifest→Deployment+Service
│   ├── tenant.py                 # source-IP → tenant slug (md5[:8])
│   └── config.py                 # env-driven config (EXTERNAL_HOSTNAME, ZONE2_KUBECONFIG, ZONE2_EXTERNAL_IP)
├── triton-infer/
│   ├── Dockerfile                # tritonserver:23.10-py3 base (TRT 8.6, supports V100+L40S)
│   ├── entrypoint.sh             # build engines if missing, start Triton, start sidecar
│   ├── export_model.py           # .pt → ONNX → TRT FP16 plan
│   ├── sidecar/main.py           # gRPC :50051 + HTTP :8080 inference server
│   ├── proto/infer.proto         # InferenceService.{Infer,Health}
│   ├── models/yolov8n_trt/       # config.pbtxt only (plans built per-GPU at first boot)
│   ├── models/yolov8xseg_trt/    # same
│   └── yolov8n.pt, yolov8x-seg.pt
├── k8s/                          # nef-shim Deployment + Service + Gateway + HTTPRoute manifests
├── zone2-kubeconfig.yaml         # ⚠ lives one dir up in repo: ../zone2-kubeconfig.yaml
└── EdgeVision-Overview.{key,pdf} # slide deck (drifted, see §5)
```

---

## 7. Running the demo end-to-end (current procedure)

```bash
cd edgevision

# One-time per Python version: ensure tkinter is available
brew install python-tk@3.14      # or matching version of Homebrew Python

./demo.sh start                  # starts camera-api + GUI

# Inside the GUI:
#   E — toggle LOCAL ↔ EDGE
#   T — toggle gRPC ↔ HTTP
#   M — toggle YOLOv8n (detect) ↔ YOLOv8x-seg (segment)
#   Q — quit
```

First time you enter EDGE on a fresh zone: pod build takes 3-5 min on L40S,
12-14 min on V100. Subsequent enters on the same node hit the engine cache
and are ready in ~10-20 s.

`./demo.sh stop` tears down the local processes and calls
`DELETE /app-instances/all` (per-tenant). Orphan deployments from a previous
laptop IP must be cleaned with `kubectl delete deploy,svc -l tenant=t-<slug>`
on the right cluster.
