# LTH 5G Lab

A working **private 5G Standalone network** with an **edge-compute
platform** on top, used for research at Lund University (LTH / EIT —
Department of Electrical and Information Technology, with
Department of Automatic Control, NextG2Com / AORTA / WARA-Ops).
This repository contains every script, manifest, document and demo
needed to bring the whole stack up from blank servers.

If you've never touched 5G, Kubernetes or CAMARA before — read on.
Everything is explained from scratch.

---

## What can you do with 5G + edge compute?

5G is not just a faster pipe. With a **5G Standalone** network and
**GPU/CPU servers placed inside the operator's edge** — close to the
radio masts, not in some hyperscale cloud thousands of kilometres away
— you get something qualitatively new: **compute that is part of the
network**. An app can ask the network "where can I run a container
near this device?" and within seconds have GPU-accelerated code
running one hop from the UE.

That changes what a device has to carry. Anything that is hard or
expensive to do on the device — heavy AI inference, video analytics,
real-time control, multi-device coordination — can be **offloaded**
to the edge, then accessed over a single-digit-millisecond radio
link. The result:

- **Devices get lighter and cheaper.** A phone, a sensor, a vehicle
  doesn't need a top-end GPU; the network provides one on demand.
- **Battery life stretches.** The heaviest compute happens in a
  power-cooled data centre, not on a hand-held lithium cell.
- **Latency drops.** Round-trip to a central cloud is 50–100 ms; to
  a 5G edge it's 5–15 ms. New workloads become possible — XR,
  cooperative autonomy, real-time robotic control.
- **Devices can collaborate** through a shared edge instead of
  having to mesh with each other directly.
- **Capability becomes a network service.** A developer asks the
  network "give me a GPU container near this UE", the network does
  it. No infrastructure to build, no DevOps team to hire.

The mechanism that makes this *programmable* is **CAMARA** — an
open-source API standard (Linux Foundation + GSMA) that gives apps
a uniform way to talk to the network. Discover edge zones, deploy a
container, get a URL, use it, tear it down. Six HTTP endpoints. The
same API at every operator that implements it.

This lab is a working end-to-end realisation of that vision:

- A **real 5G SA network** (Ericsson BB6651 gNB + Open5GS core) with
  test UEs registering, getting IPs and talking to the world.
- A **CAMARA-compliant edge-compute API** (`nef-shim`) sitting on top
  of two Kubernetes clusters: one inside the lab (NVIDIA L40S GPUs),
  one remote (NVIDIA V100 in Ericsson's Xerces cloud).
- **Real demo applications** showing what the platform is for:
  a 150-line CAMARA walkthrough, a YOLO object-detection app that
  offloads inference to the edge over 5G in ~50 ms end-to-end, and
  a TCP benchmark for measuring exactly where the time goes.

Everything is open and reproducible. The rest of this README walks
through how it's built.

---

## What is in this repository

```
5G-setup-2026/
├── README.md                ← you are here
├── .gitignore
│
├── apps/                    ← user-facing applications running on the platform
│   ├── edgevision/          ← real-time YOLO object detection / segmentation
│   ├── edge-demo/           ← 150-line walkthrough of the edge-compute API
│   └── benchmark/           ← TCP latency / throughput measurement tooling
│
└── edge-infra/              ← the platform itself: 5G + Kubernetes + APIs
    ├── ansible/             ← Ansible playbooks that install the cluster
    ├── open5gs/             ← Helm values + extras for the 5G core
    ├── monitoring/          ← Prometheus + Grafana + Loki
    ├── nef-shim/            ← CAMARA API service (FastAPI + Python)
    ├── camara/              ← earlier CAMARA prototype
    ├── kubeconfigs/         ← (gitignored) admin credentials
    ├── secrets/             ← (gitignored) tokens / keys
    └── docs/                ← protocol references, screenshots, write-ups
```

Two strict halves:

- **`edge-infra/`** is everything you need to **operate** the lab.
  Bringing the servers up, running the 5G core, exposing the CAMARA
  API. Largely set up once, then left alone.
- **`apps/`** is everything that **uses** the platform. Adding a new
  demo? It goes in `apps/`. Nothing in `apps/` should ever ship
  Ansible playbooks or k8s control-plane manifests.

---

## What the lab consists of

### Physical equipment

| Box | Role | IP | What's on it |
|---|---|---|---|
| **gNB Ericsson BB6651 G3** | 5G radio base station | mgmt: link-local `169.254.2.2` | Band n78, 80 MHz, 2 sectors. Connected to the AIR 6419 antenna. |
| **`k8sv2-1.eit.lth.se`** | Kubernetes control plane | `130.235.32.171` | Hosts the Open5GS AMF (NGAP `192.168.0.201:38412`) and the CAMARA control plane (`camara.5glab.control.lth.se`). |
| **`k8sv2-2 … k8sv2-7`** | K8s workers (6 nodes) | `130.235.32.172 … .177` | Run Open5GS NFs, MongoDB, Longhorn storage, monitoring stack. |
| **`gpuserver01-5g`** | GPU worker | `130.235.32.181` | 2× NVIDIA L40S, 128 cores, 768 GB RAM. The edge-compute target. |
| **`garage-toolserver001`** | Operator workstation | `130.235.32.169` | Has the MOSHELL tooling for talking to the gNB craft port. |
| **`core5g001`** | Original 5G core box | `130.235.32.168` | Historical; the cluster has taken over its role. |
| **5G CPE (Teltonika TRM500)** | A UE we control | `192.168.1.1` over USB / LAN | Test SIM cards behind it (IMSIs `001010000000010` … `018`). |
| **Phones with test SIMs** | Mobile UEs | — | Real-world bring-your-own UE testing. |

Full IP allocation, MACs and firewall openings are in
[`edge-infra/docs/5g_lab.txt`](edge-infra/docs/5g_lab.txt).

### Second cluster — Xerces (remote)

A separate Kubernetes cluster on Ericsson's **Xerces OpenStack** cloud
is registered as a second edge zone. One NVIDIA Tesla V100 GPU.
Public floating IP `129.192.83.16`. Used to demonstrate that the
CAMARA API can target multiple zones with one client call.

### Network paths

```
   ╭─────────╮    radio    ╭────────────────╮    NGAP/SCTP   ╭───────────────╮
   │   UE    │ ◄────────►  │  gNB BB6651    │ ───────────►   │ k8sv2-1       │
   │ (phone, │   (n78)     │  + AIR 6419    │  192.168.0.201 │ Open5GS AMF   │
   │  CPE,   │             ╰────────────────╯                ╰───────┬───────┘
   │  modem) │                                                       │  k8s SBI / PFCP
   ╰─────────╯                                                       ▼
                                                            ╭─────────────────╮
              ◄────── PDU sessions over UPF ◄──────         │ Open5GS SMF/UPF │
              10.45.0.0/16 (internet DNN)                   │ NRF/UDM/UDR/…   │
              10.46.0.0/16 (ims DNN)                        ╰────────┬────────┘
                                                                     │
                                                                     ▼
                                                            ╭─────────────────╮
                                                            │ Apps tier       │
                                                            │ NEF-shim (CAMARA)│
                                                            │ EdgeVision pods │
                                                            │ GPU on L40S     │
                                                            ╰─────────────────╯
```

A UE attaches to the gNB → AMF authenticates it → SMF assigns it an
IP from `10.45.0.0/16` → UPF routes its traffic into the cluster's
overlay → app pods are reachable on the same flat L3 network.

---

## How the 5G core (Open5GS) is set up

We run **Open5GS v2.7.2** as a set of Kubernetes pods in the
`open5gs` namespace, installed via Helm using
[`edge-infra/open5gs/open5gs-values.yaml`](edge-infra/open5gs/open5gs-values.yaml).

### The cast of Network Functions

A 5G core isn't one program but ~12 microservices. We use the 5G SA
subset:

| NF | Role |
|---|---|
| **AMF** | Access & Mobility — UE registration, NAS, mobility. Talks NGAP/SCTP to the gNB. |
| **SMF** | Session Management — sets up PDU sessions, assigns IPs, controls the UPF. |
| **UPF** | User Plane — forwards the actual user packets. Owns the `ogstun` interfaces. |
| **NRF** | Service registry — every NF registers, others discover via NRF. |
| **UDM** | Subscriber Data Management — computes auth vectors from `K` + `OPc`. |
| **UDR** | Database backend for UDM — talks to MongoDB. |
| **AUSF** | Auth Server — handles 5G-AKA. |
| **PCF** | Policy Control — bandwidth limits / charging. |
| **NSSF** | Slice selection. |
| **SCP** | Service Communication Proxy — routes SBI messages. |
| **BSF** | Binding Support — helps PCF find sessions. |

All ship as separate Deployments in the `open5gs` namespace plus a
**MongoDB** Deployment for subscriber storage.

### How we got here — the abridged setup story

The hard-won lessons of the first bring-up are documented in
[`edge-infra/docs/encounter-with-5G.md`](edge-infra/docs/encounter-with-5G.md).
Highlights:

- The AMF runs with `hostNetwork: true` pinned to `k8sv2-1` so the
  gNB can reach `192.168.0.201:38412` for NGAP. Without this, the
  AMF is buried inside the pod network and the gNB can't find it.
- Slice config: **SST=1, no SD**. Mismatched SD (gNB advertising
  none, AMF expecting `000001`) cost us a day.
- NRF's templated config had `mcc: 999 / mnc: 70` hardcoded. We
  patch it to `001/01`. Reverts on every `helm upgrade`.
- Subscribers need a *complete* MongoDB record — security keys
  alone don't get you a PDU session. UE-AMBR, access restriction
  data, subscriber status all required.
- Two DNNs are configured: `internet` (10.45.0.0/16, primary) and
  `ims` (10.46.0.0/16, so UEs that probe for VoLTE don't loop).
- `K` and `OPc` from SIM vendors: SIM vendors call OPc "OP" by
  mistake. If auth fails with MAC mismatch, switch them around.

### gNB tuning

| Setting | Value | Why |
|---|---|---|
| Band | n78 (3.5 GHz) | Mid-band TDD |
| Bandwidth | 80 MHz | |
| Subcarrier spacing | 30 kHz (numerology 1) | Mid-band TDD default |
| TDD pattern | DDDSU (2.5 ms) | 3 downlink, 1 special, 1 uplink per period |
| DRX | Disabled | For latency benchmarking |
| Prescheduling | Enabled | Cuts P99 UL latency from 17 ms to 12 ms |

Tweaks happen via MOSHELL on the gNB craft port — see
[`edge-infra/docs/moshell.md`](edge-infra/docs/moshell.md).

### Provisioning subscribers

Either via the Open5GS WebUI (`kubectl port-forward -n open5gs deploy/open5gs-webui 8080:3000`) or by inserting Mongo docs directly. The current test SIMs are IMSI
`001010000000010` … `001010000000018`.

---

## How Kubernetes is set up

A 7-worker + 1-control-plane cluster. Kubernetes **v1.35.3** on
**Ubuntu 24.04 LTS**.

| Component | Purpose |
|---|---|
| **kubeadm** | bootstraps the control plane |
| **containerd** | container runtime |
| **Calico** | CNI / overlay network |
| **Longhorn** | storage for stateful workloads (MongoDB primarily) |
| **NVIDIA GPU Operator** | exposes GPU as `nvidia.com/gpu` resource on `gpuserver01-5g`. **MPS replicas=4** → each L40S advertises as 4 shareable slots, so the node has `allocatable.gpu: 8`. |
| **Envoy Gateway** | external HTTP ingress (port 80) for `camara.5glab.control.lth.se` |
| **Prometheus + Loki + Grafana** | metrics + logs ([dashboard JSON](edge-infra/monitoring/grafana-5g-dashboard.json)) |

The whole cluster is bootstrapped from
[`edge-infra/ansible/`](edge-infra/ansible/) — `k8s-install.yaml`,
`longhorn-install.yaml`, `open5gs-deploy.yaml`.

To re-run the install (assuming fresh Ubuntu servers in
[`servers.txt`](edge-infra/ansible/servers.txt)):

```sh
cd edge-infra/ansible
ansible-playbook upgrade.yaml --ask-become-pass
ansible-playbook k8s-install.yaml --ask-become-pass
ansible-playbook longhorn-install.yaml --ask-become-pass
ansible-playbook open5gs-deploy.yaml
```

---

## How the edge-compute platform is exposed

The 5G core gives UEs IP connectivity. The **edge-compute platform**
is what makes the cluster's GPU power *usable* by an application:

1. The app asks the network "where can I run a container?" — CAMARA
   **Edge Discovery**.
2. The app sends a manifest — CAMARA **Edge App Management**.
3. The network schedules the container on the right zone.
4. The app asks "where do I send my data?" — CAMARA **App Endpoint
   Discovery**.
5. The app talks directly to that container — over the 5G network.

In real 5G this is implemented by a **NEF** (Network Exposure
Function). Open5GS doesn't ship one, so we built **`nef-shim`** — a
small FastAPI service that speaks the CAMARA API and translates
each call into Kubernetes API calls on the right cluster.

### How `nef-shim` works

Lives in [`edge-infra/nef-shim/`](edge-infra/nef-shim/). One pod, in
the `edgevision` namespace on LTH. Holds two Kubernetes API clients:

- one for the local LTH cluster (in-cluster service account)
- one for Xerces (kubeconfig file mounted from a Secret)

On every CAMARA call:

```
POST /edge-app-management/v0/app-instances  { appId, edgeCloudZoneId }
            │
            ▼
   nef-shim picks the right cluster
   builds a Deployment + NodePort Service from the manifest
   labels everything with the tenant slug t-<md5(source-ip)[:8]>
   returns the appInstanceId
```

**Tenant isolation is by source IP.** Two UEs on different PDU
sessions get different IP addresses → different tenant slugs →
different Deployments. No tokens, no auth — fine for a lab.

The **inference data plane bypasses `nef-shim` entirely.** It returns
`host:port` to the client at endpoint-discovery time; the client
connects directly to the pod's NodePort. The shim is control-plane
only.

### How endpoint URLs look

```
LTH zone:      http://camara.5glab.control.lth.se:32628        ← from a 5G UE
Xerces zone:   http://129.192.83.16:30661                       ← from anywhere
```

Symmetric across zones. The only thing that differs is the
externally reachable hostname/IP of each zone's cluster.

### Reachability matrix

| You are on … | LTH zone | Xerces zone |
|---|---|---|
| 5G UE (PDU on `10.45.0.0/16`) | ✅ all ports | ✅ all ports |
| LTH VPN / Eduroam | ✅ port 80 only (control plane) <br>❌ NodePorts blocked by campus firewall | ✅ all ports (OpenStack SG `0.0.0.0/0`) |
| Public internet | ❌ everything blocked | ✅ all ports |

**Practical: a 5G UE can use either zone. From the VPN, use Xerces.
Other networks: Xerces only.**

---

## The applications

Each `apps/<name>/` directory has its own README with full
instructions. Quick tour:

### `apps/edge-demo/` — the 150-line walkthrough

The simplest possible client of the platform. Runs an `nginx`
container at the edge, walks the full 7-step lifecycle in plain
print statements. Read this first if you're new.

```sh
cd apps/edge-demo
pip install -r requirements.txt
python3 edge_demo.py                       # LTH zone
python3 edge_demo.py --zone xerces-cloud-zone   # works from any network
```

### `apps/edgevision/` — the YOLO offload demo

Real-time object detection + segmentation, offloadable to the edge
GPU. Tkinter GUI shows two panes (input vs annotated output) plus a
latency breakdown. Press `E` to toggle LOCAL ↔ EDGE, `T` for gRPC ↔
HTTP, `M` for detect ↔ segment.

```sh
cd apps/edgevision
./demo.sh start
```

Cold start on L40S: ~4 minutes (TensorRT engine build). Hot path:
~50 ms end-to-end including the 5G radio.

Full architecture in
[`apps/edgevision/HOW-IT-WORKS.md`](apps/edgevision/HOW-IT-WORKS.md).
Slides in `apps/edgevision/EdgeVision-Overview-v2.pptx`.

### `apps/benchmark/` — RTT + throughput measurement

Custom TCP benchmark for measuring where time is spent: radio,
core, server. Used to produce the latency tables in the EdgeVision
deck (Avg ~55 ms over 5G SA, of which 31 ms is the radio).

```sh
cd apps/benchmark
./run-benchmark.sh
```

---

## Day-to-day operator cheatsheet

```sh
# Talk to the LTH cluster
export KUBECONFIG=$PWD/edge-infra/kubeconfigs/5glab-kubeconfig
kubectl -n open5gs get pods           # 5G core health
kubectl -n edgevision get pods        # NEF-shim + any tenant pods

# Talk to the Xerces cluster
export KUBECONFIG=$PWD/edge-infra/kubeconfigs/zone2-kubeconfig.yaml
kubectl -n edgevision get pods

# Open5GS WebUI
kubectl -n open5gs port-forward deploy/open5gs-webui 8080:3000   # admin / 1423

# AMF / SMF logs (find a misbehaving UE)
kubectl -n open5gs logs deploy/open5gs-amf --tail=200
kubectl -n open5gs logs deploy/open5gs-smf --tail=200

# Talk to the gNB
ssh garage@130.235.32.169          # toolserver (garage / garage)
moshell 169.254.2.2                # gNB craft port (link-local)
```

---

## What's still rough

- **Secrets in the repo.** `edge-infra/secrets/` and
  `edge-infra/kubeconfigs/` are `.gitignore`d but shouldn't live
  here at all. To move: vault, 1Password, sealed-secrets, your call.
- **No warm-cache reaper.** `DELETE /app-instances/{id}` tears the
  pod down immediately; the TensorRT engine cache evaporates with
  the emptyDir. Cold-start every time. Fix is a PersistentVolume on
  `/models` and a delayed reaper. Plan exists; not implemented.
- **No IMSI-keyed tenancy.** Identity is the laptop's source IP. A
  VPN flip = new tenant. Acceptable for a demo, not for anything
  real.
- **NRF PLMN patch + AMF `hostNetwork` patch** revert on every
  `helm upgrade open5gs`. Re-apply manually. Or fork the chart.
- **`apps/edgevision/measurements/`** isn't versioned but accumulates
  data on every run.

---

## Further reading

- [`edge-infra/docs/encounter-with-5G.md`](edge-infra/docs/encounter-with-5G.md)
  — the original day-by-day bring-up notes. Worth reading even just
  to know why some of the workarounds exist.
- [`edge-infra/docs/moshell.md`](edge-infra/docs/moshell.md) — MOSHELL
  recipes for the gNB.
- [`apps/edgevision/HOW-IT-WORKS.md`](apps/edgevision/HOW-IT-WORKS.md)
  — runtime-accurate description of the EdgeVision flow.
- [`apps/edge-demo/README.md`](apps/edge-demo/README.md) — beginner
  walkthrough of the CAMARA API itself.
- CAMARA project: <https://camaraproject.org> /
  <https://github.com/camaraproject>
- Open5GS: <https://open5gs.org>
