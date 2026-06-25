# edge-demo — what a 5G edge looks like, in 150 lines

A tiny Python script that does **the whole life of an edge-compute
workload**: ask the 5G network where its edge servers are, ship a
container there, wait for it to start, get a URL, use it, and tear it
down. About 8 seconds end-to-end. No prior knowledge required.

If you've never deployed anything to a cluster, never used 5G as a
platform, and have only heard of CAMARA in passing — read on. This
README explains every concept it touches.

> **TL;DR for the impatient:**
> ```sh
> pip install -r requirements.txt
> python3 edge_demo.py
> ```
> If that works, jump to [Adapting the demo](#adapting-the-demo--change-the-manifest-not-the-script).

---

## Why does this exist?

5G networks aren't just radio anymore. The same operator that gives
your phone an IP also runs **edge data centres** — small computer
rooms close to the radio mast — so apps can run *near the user*
instead of in some cloud thousands of kilometres away.

For an app developer that's great: low latency, lots of compute, no
infrastructure to build. But how does an app *find* those edge
servers, *deploy* something to one, and *use* it? You need an API.

**CAMARA** is the open-source project that defines this API. This
script is a working demo of that API talking to a real 5G lab.

---

## The cast of characters

If any of the words below are new, here's the 30-second version of
each:

- **Container** — a self-contained bundle of an app plus everything it
  needs to run. Like a tiny preinstalled computer in a file. Docker is
  the most common way to build them.
- **Kubernetes (k8s)** — a system for running containers across many
  machines. It schedules them, restarts them, gives them IPs, makes
  groups of them reachable from outside. The lab uses two of these,
  one per "edge zone".
- **5G Standalone (5G SA)** — modern 5G with no LTE involved. The lab
  runs a real 5G SA core called **Open5GS**.
- **UE** — User Equipment. The thing with a SIM card: phone, 5G modem,
  the laptop currently behind a 5G CPE, etc.
- **UPF** — User Plane Function. The 5G component that routes UE
  traffic into the rest of the world (or into a private network like
  this lab).
- **Edge cloud zone** — one cluster of computers near the user that
  CAMARA can deploy to. This lab has two: a local one with NVIDIA L40S
  GPUs, and a remote one in Ericsson's Xerces OpenStack with a V100.
- **CAMARA** — Open API standard (Linux Foundation + GSMA) that gives
  apps a way to talk to the network. The bit we care about is the
  **Edge Compute** family: "discover zones", "run my container", "give
  me a URL".
- **NEF / NEF-shim** — Network Exposure Function. In real 5G this is a
  big stateful piece; in this lab a small FastAPI service (`nef-shim`)
  speaks the CAMARA API and translates each call into Kubernetes
  operations.
- **NodePort** — a way Kubernetes exposes a service. Picks a TCP port
  in the 30000–32767 range and forwards it to the container. **This
  demo's "data plane" — the actual app traffic — uses NodePorts.**
- **Manifest** — a JSON document that describes a container to run:
  image, ports, CPU/RAM, GPU count, health-check probe. The script
  posts one in step 2.

---

## What this script does, in pictures

```
Your laptop / UE                              5G lab
─────────────────                             ──────────────────────────

 edge_demo.py  ──────── HTTP+JSON over 5G ──────►  NEF-shim
                                                   (talks the CAMARA API,
                                                    runs your container
                                                    on Kubernetes)
                                                          │
                                                          ▼ k8s API
                                                   Container scheduled,
                                                   started, given a URL.
                                                          │
 edge_demo.py  ─────────────────────────────────►  Your container
              (data plane — direct TCP to the              (here: nginx,
               NodePort URL, no proxies in between)         could be
                                                            anything)
```

Crucial point: **only the lifecycle calls (steps 1–5, 7) go through
the NEF-shim**. Once you've got a URL (step 5), inference / traffic /
business / whatever happens between the script and your container
directly. The NEF-shim doesn't sit on the hot path.

---

## The 7 steps the script walks

| # | What it does | API call | What happens on the cluster |
|---|---|---|---|
| 1 | Find available edge zones | `GET /simple-edge-discovery/v0/edge-cloud-zones?device-ip=…` | Returns LTH + Xerces zones with their GPU specs |
| 2 | Register the container manifest | `POST /edge-app-management/v0/apps` | NEF-shim remembers the manifest, returns `appId` |
| 3 | Instantiate on a chosen zone | `POST /edge-app-management/v0/app-instances` | NEF-shim creates a k8s Deployment + Service |
| 4 | Poll until ready | `GET /edge-app-management/v0/app-instances/{id}` | Container pulls, starts, readiness probe passes |
| 5 | Get the URL to talk to | `GET /application-endpoint-discovery/v0/endpoints?appInstanceId=…` | Returns `host:port` for each port in your manifest |
| 6 | **Use the container directly** | `GET <url>/...` (or any TCP) | Direct kube-proxy → pod, **no NEF-shim involved** |
| 7 | Tear it down | `DELETE /edge-app-management/v0/app-instances/{id}` | NEF-shim deletes Deployment + Service |

### As a sequence diagram

```
 Client            NEF-shim           K8s API         Pod
   │                  │                  │             │
   │ GET zones        │                  │             │
   │─────────────────►│                  │             │
   │◄── 200 zones[]   │                  │             │
   │                  │                  │             │
   │ POST /apps       │                  │             │
   │─────────────────►│                  │             │
   │◄── 200 {appId}   │                  │             │
   │                  │                  │             │
   │ POST /app-instances                 │             │
   │─────────────────►│                  │             │
   │                  │ create Deploy+Svc│             │
   │                  │─────────────────►│             │
   │◄── 202 {appInstanceId, status}      │             │
   │                  │                  │ schedule    │
   │                  │                  │ + pull img  │
   │                  │                  │────────────►│ init
   │ GET /…/{id}  (poll every 2s)        │             │
   │─────────────────►│                  │             │
   │                  │ watch pod        │             │
   │                  │─────────────────►│             │
   │                  │                  │◄── ready ───│
   │◄── 200 {status: ready}              │             │
   │                  │                  │             │
   │ GET /endpoints   │                  │             │
   │─────────────────►│                  │             │
   │◄── 200 [{name, url=host:port}, …]   │             │
   │                  │                  │             │
   │           ── data plane direct to NodePort ──────►│
   │═════════════════════════════════════════════════►│
   │◄═════════════════════════════════════════════════│
   │                  │                  │             │
   │ DELETE /app-instances/{id}          │             │
   │─────────────────►│                  │             │
   │                  │ delete Deploy+Svc│             │
   │                  │─────────────────►│             │
   │◄── 204 No Content│                  │             │
```

`══►` is the data-plane hop your app cares about; it bypasses the
NEF-shim entirely.

---

## What gets deployed (the demo container)

The script runs **`nginxdemos/hello`** — a tiny public HTTP echo
server that returns a small text page with the pod IP, hostname,
request URI, and a per-request ID. We picked it because:

- It's public and tiny (image pull takes seconds).
- The response **changes every refresh** (per-request ID + pod IP).
  That makes it obvious the traffic really reached a fresh container
  and isn't being served from some cache somewhere.

Nothing in the script is application-specific. Want to run something
else? **Change the manifest, not the script** (see below).

---

## The manifest, explained

The **manifest** is the only application-specific data in the whole
demo. Everything else is API plumbing. Here it is verbatim from
[`edge_demo.py`](edge_demo.py):

```python
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
```

This dict becomes the body of `POST /edge-app-management/v0/apps`
(step 2). The NEF-shim stores it, returns an `appId`, and later
translates it into a Kubernetes **Deployment + Service** when you
instantiate it (step 3).

### Top-level fields

| Field | Required | Purpose |
|---|---|---|
| `appName` | yes | A human label. Shows up in logs. Two different apps can share a name; they'll get different `appId`s. |
| `appProvider` | yes | Free-text provider identifier. Useful when many teams share a NEF. |
| `appSoftwareVersion` | yes | Free-text version. Bump when you change the image. |
| `packageType` | yes | Always `"CONTAINER"`. The platform supports nothing else (no VMs, no functions). |
| `containerSpec` | yes | What runs in each pod. See below. |
| `requiredResources` | yes | CPU / memory / GPU per pod. See below. |
| `componentSpec` | yes | What ports the container exposes. See below. |

### `containerSpec` — what's inside the pod

| Field | Required | Purpose |
|---|---|---|
| `image` | yes | Full image reference, e.g. `nginxdemos/hello:plain-text` or `ghcr.io/you/yourapp:v1.2`. |
| `imageRegistry` + `imageName` + `imageTag` | alt | Three-field form. Same result; use whichever you find readable. |
| `imagePullPolicy` | no | `Always` (default) / `IfNotPresent` / `Never`. |
| `command` | no | List of strings; overrides the image's ENTRYPOINT. Example: `["python", "-m", "http.server", "9000"]`. |
| `args` | no | List of strings; overrides the image's CMD. |
| `env` | no | Environment variables. Dict (`{"K": "v"}`) or list (`[{"name":"K","value":"v"}]`). |
| `readinessProbe` | recommended | The NEF-shim won't return `status=ready` until this passes. See below. |
| `livenessProbe` | no | k8s restarts the container if this fails too many times. Use a generous `initialDelaySeconds` for slow-starting apps. |

### Probes

Both probes have the same shape:

```python
"readinessProbe": {
    "http": {"path": "/health", "port": 8080},   # required
    "initialDelaySeconds": 2,                    # grace before first probe
    "periodSeconds": 2,                          # gap between probes
    "failureThreshold": 3,                       # consecutive misses to fail
}
```

- `readinessProbe` answers "can it serve traffic *yet*?" — k8s won't
  route requests to it until this passes. Step 4 of the demo polls
  the readiness probe; the pod is considered `ready` when this
  returns 200.
- `livenessProbe` answers "is it still working?" — if it fails too
  many times in a row, k8s kills the container. Useful for catching
  deadlocked apps. For a slow-starting app (e.g. one that builds a
  TensorRT engine), set `initialDelaySeconds` long enough that the
  liveness probe doesn't fire mid-init.

If you omit the readiness probe, the platform will mark the pod
`ready` as soon as the container starts — which is wrong for most
real apps.

### `requiredResources`

| Field | Form | Example | Meaning |
|---|---|---|---|
| `cpu` | string or int | `"100m"` or `2` | CPU request. `100m` = 0.1 of one core. Whole numbers = whole cores. |
| `memory` | int (MiB) | `64` | RAM request in MiB. |
| `gpu` | int | `0` or `1` | Number of GPU slots requested. **One slot is one MPS partition, not a whole physical GPU.** LTH GPUs share 4 ways, Xerces 2 ways. |

Setting `gpu: 1` makes the platform:
- Schedule onto a GPU-equipped node only
- Add `runtimeClassName: nvidia` and the `nvidia.com/gpu` resource
  request
- Tolerate the `nvidia.com/gpu=true:NoSchedule` taint on GPU nodes
- Make `nvidia-smi` work inside the container (if the image has
  CUDA libraries available — `nvcr.io/nvidia/...` and
  `pytorch/pytorch:*-cuda*` are good starting bases)

### `componentSpec` — your ports

```python
"componentSpec": [
    {
        "componentName": "web",                        # free-text label
        "networkInterfaces": [
            {"name": "http",  "port": 80,    "protocol": "TCP"},
            {"name": "grpc",  "port": 50051, "protocol": "TCP"},
        ],
    }
],
```

- One `componentSpec` entry per pod-template (we always use one — the
  spec allows more but the NEF-shim collapses them).
- Each `networkInterfaces` entry becomes one **NodePort** on the
  Service. The platform picks a free port in `30000–32767` and tells
  you about it in step 5.
- **`name` matters.** It's echoed back in
  `/endpoints` (step 5), and your client looks it up by name:
  `endpoints["http"]["url"]`, `endpoints["grpc"]["url"]`. Use names
  that are stable across deploys.
- `port` is the port your container actually listens on. The external
  NodePort is different and unpredictable.
- `protocol` is `TCP` for everything (the platform supports UDP too
  but no demo uses it yet).

### What the platform fills in for you

You **don't** specify, and shouldn't:

- The Deployment / Service names — derived from your tenant slug.
- The namespace — currently fixed to `edgevision`.
- `replicas` — always 1. Scaling out is not in scope for v0 of the
  API.
- The NodePort numbers — picked by Kubernetes, returned in step 5.
- `imagePullSecrets` — the platform attaches `ghcr-secret`
  automatically for `ghcr.io` images.
- Node selection / GPU taint tolerations — added automatically when
  `gpu >= 1`.

### What's not yet supported in the manifest

| Real k8s feature | Status in v0 of the API |
|---|---|
| Volumes / PersistentVolumeClaims | ❌ no — emptyDir for pod lifetime only |
| ConfigMaps | ❌ no — bake configs into the image or use `env` |
| Secrets | ❌ no — see above |
| Multiple containers per pod | ❌ no — one pod = one container |
| Init containers | ❌ no — use `command` instead |
| Custom `imagePullSecrets` | ❌ no — only `ghcr-secret` is wired in |
| Service mesh / sidecars | ❌ no |
| HorizontalPodAutoscaler | ❌ no — fixed 1 replica |
| Ingress / TLS | ❌ no — your container is on the public internet via NodePort, run TLS in-pod if you need it |

If any of these are blockers for what you want to build, talk to me
before you start.

---

## Prerequisites

You need:

1. **Network reachability to the 5G lab control plane.** Three
   common cases:
   - **5G UE** (e.g. laptop behind a Teltonika CPE — you'll see a
     `10.x.x.x` PDU address). **All ports on both zones are
     reachable.** This is the canonical client.
   - **LTH VPN / Eduroam**. The control plane (port 80) works, so
     CAMARA calls succeed. **LTH NodePorts are blocked by the
     campus firewall**, so step 6 won't reach LTH-zone pods. Use
     `--zone xerces-cloud-zone` — Xerces is open on all ports.
   - **Public internet** (no VPN). The LTH side is unreachable
     entirely. Use `--zone xerces-cloud-zone`.
2. **Python 3.9 or newer** and the `requests` library:
   ```sh
   pip install -r requirements.txt
   ```
3. **DNS for `camara.5glab.control.lth.se`** (resolves through LTH
   network) or use `--api http://130.235.32.171 --host-header
   camara.5glab.control.lth.se` to bypass DNS.

---

## Running it

```sh
python3 edge_demo.py
```

A normal cold run takes **4–8 seconds end-to-end** (most of it is the
node pulling the image on first deploy; subsequent runs hit the
node-local image cache and finish in ~3 s).

### Flags

| flag | purpose |
|---|---|
| `--api URL`              | Override the CAMARA control plane base URL. |
| `--device-ip IP`         | "Device IP" passed to discovery. The platform uses this to decide which zone is "near" the UE. Default `10.45.0.2`. |
| `--zone ZONE`            | `lth-5glab-gpu-zone` (default) or `xerces-cloud-zone`. |
| `--host-header HOST`     | Send a `Host:` header on every CAMARA call. Use when DNS isn't set up. |
| `--ready-timeout N`      | Seconds to wait for the pod to become ready. Default 180. Bump for a cold V100 build (which can take ~14 min if the engines aren't cached on the node). |
| `--keep`                 | Skip teardown — leave the instance running so you can `curl` it yourself afterwards. |

### Example: pick a specific zone

```sh
python3 edge_demo.py --zone xerces-cloud-zone
```

From a 5G UE either zone works. From the VPN or the public internet,
use Xerces.

### Example: DNS not set up

```sh
python3 edge_demo.py \
  --api http://130.235.32.171 \
  --host-header camara.5glab.control.lth.se
```

---

## What you'll see in the output

A successful run looks like this (with a few interesting bits flagged
with `←`):

```
CAMARA edge: http://camara.5glab.control.lth.se  device-ip=10.45.0.2  zone=lth-5glab-gpu-zone

[1/7] Discover edge cloud zones
      → 2 zone(s):
        - lth-5glab-gpu-zone        2× NVIDIA L40S  ← selected
        - xerces-cloud-zone         1× NVIDIA Tesla V100

[2/7] Register the app manifest
      → appId=544b3b36-f3bc-41b7-824a-c1255a1b03a6      ← random per run

[3/7] Instantiate the app on the zone
      → appInstanceId=eeb1d18f-848f-429b-a41d-d5a8a53b71d4

[4/7] Poll until ready
      phase=starting     status=instantiating ...        ← container being created
      phase=running      status=instantiating ...        ← readiness probe pending
      phase=ready        status=ready  Container ready   ← we can talk to it now

[5/7] Discover per-port endpoints
      - http     (TCP/80)  http://camara.5glab.control.lth.se:32628
                                                ^^^^^
                                                random NodePort in 30000-32767

[6/7] Call the container directly (data plane bypasses NEF-shim)
      GET http://camara.5glab.control.lth.se:32628/
      | Server address: 10.244.225.184:80     ← pod's internal IP
      | Server name: t-400b6ace-app-…-vrf5p   ← container hostname
      | Request ID: d5b4e1eb0ea93cab…         ← changes every request

✓ Demo succeeded.

[7/7] Terminate the instance
      → HTTP 204
```

---

## API reference (six endpoints, that's all)

The full OpenAPI spec is at `<api>/openapi.json`.

| Method  | Path                                                             | Purpose |
|---|---|---|
| `GET`   | `/simple-edge-discovery/v0/edge-cloud-zones?device-ip=…`         | List reachable zones. |
| `POST`  | `/edge-app-management/v0/apps`                                   | Register a manifest, get `appId`. |
| `POST`  | `/edge-app-management/v0/app-instances`                          | Instantiate on a zone. |
| `GET`   | `/edge-app-management/v0/app-instances/{id}`                     | Poll instance status. |
| `DELETE`| `/edge-app-management/v0/app-instances/{id}`                     | Terminate. |
| `GET`   | `/application-endpoint-discovery/v0/endpoints?appInstanceId=…`   | List per-port URLs. |

### What `/endpoints` returns

```json
{
  "appInstanceId": "<id>",
  "endpoints": [
    {"name": "http", "protocol": "TCP", "containerPort": 80,
     "url": "http://camara.5glab.control.lth.se:32628"}
  ]
}
```

The URL is **`<reachable-host>:<NodePort>`** — symmetric between
zones, no path-prefix proxies. For Xerces the host is
`129.192.83.16` (the cluster's public floating IP); the rest of the
shape is the same.

### Multi-port apps

Declare more `networkInterfaces` in the manifest, get one URL per
declared port back. Names are preserved, so a client looks up
`endpoints["grpc"]` etc.

---

## How tenant isolation works

Multiple users can hit this API at the same time. They never see each
other's containers. How:

1. NEF-shim takes the **source IP** of every CAMARA call (via
   `X-Forwarded-For` set by the gateway).
2. Hashes it with MD5 and takes 8 hex chars: `t-660b0222`.
3. Uses that prefix on every k8s resource it creates: `t-660b0222-app`
   Deployment, `t-660b0222-app` Service.
4. Every subsequent call from the same source IP gets the same slug,
   so the same person sees their own resources.

This means: **switching networks (5G → VPN → WiFi) changes your IP →
gives you a new tenant slug → your old resources look like someone
else's.** This is fine for the demo. In production a real CAMARA
deployment would use OAuth tokens scoped to a customer.

---

## It really is just HTTP

Same lifecycle from `bash` + `curl` + `jq`, no Python needed:

```sh
API=http://camara.5glab.control.lth.se

# 1. discover
curl -s $API/simple-edge-discovery/v0/edge-cloud-zones \
  --get --data-urlencode device-ip=10.45.0.2 | jq

# 2. register
APP=$(curl -s -X POST $API/edge-app-management/v0/apps \
  -H 'Content-Type: application/json' \
  -d '{"appName":"hello",
       "containerSpec":{"image":"nginxdemos/hello:plain-text",
         "readinessProbe":{"http":{"path":"/","port":80},
                           "initialDelaySeconds":2,"periodSeconds":2}},
       "requiredResources":{"cpu":"100m","memory":64},
       "componentSpec":[{"componentName":"web",
         "networkInterfaces":[{"name":"http","port":80,"protocol":"TCP"}]}]}' \
  | jq -r .appId)

# 3. instantiate
INST=$(curl -s -X POST $API/edge-app-management/v0/app-instances \
  -H 'Content-Type: application/json' \
  -d "{\"appId\":\"$APP\",\"edgeCloudZoneId\":\"lth-5glab-gpu-zone\"}" \
  | jq -r .appInstanceId)

# 4. poll
until curl -s $API/edge-app-management/v0/app-instances/$INST \
       | jq -e '.status == "ready"' > /dev/null; do sleep 2; done

# 5. endpoints
URL=$(curl -s "$API/application-endpoint-discovery/v0/endpoints?appInstanceId=$INST" \
       | jq -r '.endpoints[] | select(.name=="http") | .url')

# 6. talk to the pod directly
curl -s $URL/

# 7. teardown
curl -s -X DELETE $API/edge-app-management/v0/app-instances/$INST
```

---

## Adapting the demo — change the manifest, not the script

The whole point of the API is that **you don't change the script; you
change the manifest**. Edit `DEMO_MANIFEST` at the top of
`edge_demo.py`:

### Run a Python HTTP echo server on a custom port

```python
"containerSpec": {
    "image": "python:3.12-slim",
    "command": ["python", "-m", "http.server", "9000"],
},
"componentSpec": [{
    "componentName": "echo",
    "networkInterfaces": [{"name": "http", "port": 9000, "protocol": "TCP"}],
}],
```

### Run a multi-port app

```python
"componentSpec": [{
    "componentName": "myapp",
    "networkInterfaces": [
        {"name": "http",    "port": 8080,  "protocol": "TCP"},
        {"name": "metrics", "port": 9090,  "protocol": "TCP"},
        {"name": "grpc",    "port": 50051, "protocol": "TCP"},
    ],
}],
```
Step 5 will return three URL entries. The client looks them up by
name (`endpoints["grpc"]` etc.).

### Request a GPU

```python
"requiredResources": {"cpu": 4, "memory": 8192, "gpu": 1},
```
The pod is scheduled onto a GPU node with `runtimeClassName: nvidia`
and one GPU slot. The image needs to be CUDA-aware (e.g.
`nvcr.io/nvidia/...` or `pytorch/pytorch:*-cuda*`) for the GPU to
actually be visible inside the container.

**Note:** the LTH zone shares each GPU 4 ways via NVIDIA MPS, and
Xerces shares each GPU 2 ways. Asking for `gpu: 1` gets you one MPS
slot, not a full physical GPU.

### Pass environment variables

```python
"containerSpec": {
    "image": "myimage",
    "env": {"LOG_LEVEL": "debug", "WORKER_COUNT": "4"},
}
```
(Either dict-form or list-of-`{"name":"...", "value":"..."}` works.)

---

## Building your own app on the platform

You've read everything above. You understand the lifecycle, the
manifest, the data plane. Now you want to actually ship something.
End-to-end, that's three pieces of work:

1. **Build a container image** of your app.
2. **Push it to a registry the platform can reach.**
3. **Write a manifest** that points at it, and deploy it.

### Step A — Build your app as a container

Your app needs to listen on TCP ports — that's how the platform
exposes it. HTTP, gRPC, raw TCP, doesn't matter. Two small examples:

**Minimal Python HTTP server (`Dockerfile`):**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY server.py .
EXPOSE 8080
CMD ["python", "server.py"]
```

```python
# server.py
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"hello from edge\n")
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
```

**FastAPI app (`Dockerfile`):**

```dockerfile
FROM python:3.12-slim
RUN pip install fastapi uvicorn
COPY app.py /
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
```

```python
# app.py
from fastapi import FastAPI
app = FastAPI()
@app.get("/health")
def health(): return {"ok": True}
@app.get("/")
def root(): return {"msg": "hello from edge"}
```

**Things to bake into your image, not the manifest:**

- All Python / system / model dependencies.
- Configuration that doesn't change between deploys (the manifest
  has no Secrets or ConfigMaps yet, so put non-secret config in the
  image and pass small things via `env`).
- A working `/health` endpoint if you can. Even if it just returns
  200, it makes the readiness probe trivial.

**Build for the right architecture.** The platform runs `linux/amd64`
on every node. From an Apple Silicon Mac:

```sh
docker buildx build --platform linux/amd64 -t myapp:v1 .
```

### Step B — Push to a registry

Two paths supported today:

**1. Public Docker Hub / public ghcr.io / quay.io — no setup needed.**

```sh
docker tag myapp:v1 docker.io/myuser/myapp:v1
docker push docker.io/myuser/myapp:v1
```

**2. Private ghcr.io — the cluster has a pull secret for `ghcr.io`.**

```sh
# one-time login
echo $GITHUB_TOKEN | docker login ghcr.io -u <github-user> --password-stdin
# push
docker tag myapp:v1 ghcr.io/<github-user>/myapp:v1
docker push ghcr.io/<github-user>/myapp:v1
```

The platform automatically attaches `imagePullSecrets: [ghcr-secret]`
to every Deployment, so private images on `ghcr.io/ekeroid/...` and
related orgs pull without further config. Talk to the lab admin if
you need a new org / token.

Other registries (private Docker Hub, private quay, GitLab registry,
AWS ECR, etc.) **don't work yet** — there's no way to pass a custom
pull secret through the API. If you need one, the simplest workaround
is to push the image to ghcr.io.

### Step C — Adapt the manifest, run the demo

Copy `edge-demo/edge_demo.py` to your own folder and change
`DEMO_MANIFEST`:

```python
DEMO_MANIFEST = {
    "appName": "my-app",
    "appProvider": "team-x",
    "appSoftwareVersion": "0.1.0",
    "packageType": "CONTAINER",
    "containerSpec": {
        "image": "ghcr.io/<github-user>/myapp:v1",
        "readinessProbe": {
            "http": {"path": "/health", "port": 8080},
            "initialDelaySeconds": 2,
            "periodSeconds": 2,
        },
        "env": {"LOG_LEVEL": "info"},
    },
    "requiredResources": {"cpu": 1, "memory": 512},
    "componentSpec": [{
        "componentName": "api",
        "networkInterfaces": [
            {"name": "http", "port": 8080, "protocol": "TCP"},
        ],
    }],
}
```

Then:

```sh
python3 edge_demo.py
```

If it fails: see the [Failure modes](#failure-modes-youll-likely-hit)
table below.

### Step D — Iterate

Once your app deploys at all, the inner loop is:

1. Change code.
2. Re-build + push: `docker buildx build --platform linux/amd64 --push -t ghcr.io/.../myapp:v2 .`
3. Bump `imageTag` in the manifest (or use `:latest` and accept the
   pull-on-every-deploy cost).
4. Run `edge_demo.py` — it does DELETE + redeploy.

For a GUI / interactive client (like
[edgevision](../edgevision/)), the same applies but the client lives
in a long-running GUI app instead of a script. Use `edgevision/` as a
reference if you're building something more elaborate than a single
`curl`.

### Step E — Production maturation (later)

When you're past the demo stage:

- Stop hardcoding `:latest`. Use semver tags and bump them.
- Add a real `/health` that checks downstreams (DB, model server,
  whatever).
- Add a `livenessProbe` so a stuck container restarts itself.
- Surface metrics on a second `networkInterfaces` entry named
  `metrics` (port 9090, Prometheus convention).
- Log to stdout in structured JSON. `kubectl logs` is the only way
  to see them right now; Loki is set up but not yet wired per-app.
- Pin specific CPU / memory limits, not just requests. The platform
  doesn't enforce this yet but will eventually.
- When the warm-cache reaper lands (planned: 1 h grace after DELETE),
  your engine builds / model loads survive between sessions. Design
  your startup with that in mind.

### Reference: the EdgeVision manifest as a "real app" example

The YOLOv8 demo's manifest, for comparison — same shape, bigger:

```python
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
            "initialDelaySeconds": 30, "periodSeconds": 5,
        },
        "livenessProbe": {
            "http": {"path": "/health", "port": 8080},
            "initialDelaySeconds": 1200,           # ← V100 takes ~12 min to build TRT engines
            "periodSeconds": 15, "failureThreshold": 5,
        },
    },
    "requiredResources": {"cpu": 2, "memory": 8192, "gpu": 1},
    "componentSpec": [{
        "componentName": "infer",
        "networkInterfaces": [
            {"name": "http", "port": 8080,  "protocol": "TCP"},
            {"name": "grpc", "port": 50051, "protocol": "TCP"},
        ],
    }],
}
```

Differences worth noting:

- **GPU**: `gpu: 1` triggers the whole NVIDIA-runtime scheduling path.
- **Long initial liveness delay**: 1200 s instead of the default — the
  app needs ~12 min on a V100 to build its TensorRT engines on first
  boot. Without this, the liveness probe would kill the container
  mid-build.
- **Two ports** named `http` and `grpc` — the client picks the
  transport based on which name it looks up.
- **`imageRegistry` + `imageName` + `imageTag`** split form, same
  effect as `image: "ghcr.io/ekeroid/5glab/edgevision-infer:latest"`.

---

## Failure modes you'll likely hit

| Symptom | Cause | Fix |
|---|---|---|
| `Connection timed out` on every request | Not on 5G *and* not on the LTH VPN | Bring one up. `getent hosts camara.5glab.control.lth.se` to verify DNS. |
| Steps 1–5 OK, step 6 times out on LTH zone | On VPN: LTH NodePorts blocked by campus firewall | Use `--zone xerces-cloud-zone`, OR connect via the 5G CPE (then both zones work). |
| `404 App not found` on instantiate | Source IP changed between `POST /apps` and `POST /app-instances` | NAT/VPN flipped mid-flight. Re-run from the same network. |
| `403 Instance not owned by this tenant` | Same as above | Same fix. |
| `409 Instance not ready` from `/endpoints` | Polled too early | Wait for `status=ready`. |
| Phase stuck in `pulling` for >30 s | Slow ghcr/dockerhub, or missing pull secret for a private registry | Use a public image; or check `kubectl -n edgevision describe pod` |
| `failed: Container crash loop` | Container started, then exited non-zero | `kubectl -n edgevision logs <pod>` — the `message` field has the reason. |
| `404 Port 'X' not declared` in the URL | Port name doesn't match the manifest | Case-sensitive; must match `networkInterfaces[].name`. |

Deeper debugging — get on the cluster directly:

```sh
ssh ubuntu@130.235.32.171
kubectl -n edgevision get pods,svc -l tenant=<your-slug>
kubectl -n edgevision logs deployment/<your-slug>-app
kubectl -n edgevision describe pod -l tenant=<your-slug>
```

Your tenant slug is in step 5's endpoint URL host path. Or compute it
yourself:

```sh
python3 -c 'import hashlib,sys; print("t-" + hashlib.md5(sys.argv[1].encode()).hexdigest()[:8])' <your-source-ip>
```

---

## What's deliberately *not* in this demo

This is the smallest possible client. Things a real edge-aware app
would do that the demo skips:

- **Pick a zone based on capabilities.** The demo defaults to LTH (or
  whatever `--zone` says). Production code would filter by
  `gpuAvailable`, GPU model, CPU count, location.
- **Cache the `appId` across runs.** Re-registering on every run is
  wasteful — once registered, the manifest persists for the life of
  the NEF-shim process.
- **Reuse the instance across runs.** Tearing down and re-instantiating
  costs a few seconds on warm cache; much more on a cold zone.
- **Handle multi-zone failover.** If `lth-5glab-gpu-zone` errors, fall
  back to `xerces-cloud-zone`.
- **Use mTLS / OAuth.** The current API is open within the lab;
  production CAMARA would use OAuth2 with scoped tokens.

---

## File map

```
edge-demo/
├── edge_demo.py        ~240 lines, no abstractions
├── requirements.txt    just `requests`
└── README.md           this file
```

Six lifecycle endpoints. Three files. Nothing magical anywhere.
