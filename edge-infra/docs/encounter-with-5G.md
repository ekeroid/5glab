# An Encounter with 5G: From Zero to Connected UEs

## A detailed account of deploying a private 5G Standalone network at LTH

---

## Table of Contents

1. [The Starting Point](#the-starting-point)
2. [The Cast of Characters: 5G Network Functions](#the-cast-of-characters-5g-network-functions)
3. [Glossary of Abbreviations](#glossary-of-abbreviations)
4. [Infrastructure Setup](#infrastructure-setup)
5. [Step 1: Deploying the 5G Core on Kubernetes](#step-1-deploying-the-5g-core-on-kubernetes)
6. [Step 2: Getting the gNB to Talk to the AMF](#step-2-getting-the-gnb-to-talk-to-the-amf)
7. [Step 3: The S-NSSAI Mismatch](#step-3-the-s-nssai-mismatch)
8. [Step 4: The NRF PLMN Disaster](#step-4-the-nrf-plmn-disaster)
9. [Step 5: Subscriber Provisioning and Authentication Hell](#step-5-subscriber-provisioning-and-authentication-hell)
10. [Step 6: The Incomplete Subscriber Record](#step-6-the-incomplete-subscriber-record)
11. [Step 7: The Missing DNN](#step-7-the-missing-dnn)
12. [Step 8: Benchmarking the Network](#step-8-benchmarking-the-network)
13. [Step 9: Radio Tuning](#step-9-radio-tuning)
14. [Radio Parameters Explained](#radio-parameters-explained)
15. [Lessons Learned](#lessons-learned)

---

## The Starting Point

The goal: build a private 5G Standalone (SA) network at Lund University (LTH) for research purposes. The equipment:

- **Radio**: Ericsson BB6651 G3 base station (gNB), operating on band n78 (3.5 GHz)
- **Core Network**: Open5GS v2.7.2, an open-source 5G core implementation
- **Platform**: Kubernetes cluster on bare-metal servers
- **UE Devices**: Commercial phones and a Teltonika TRM500 5G modem with programmable SIM cards
- **Experience level**: Approximately zero in 3GPP network deployment

What follows is a chronological account of every mistake, discovery, and eventual triumph.

---

## The Cast of Characters: 5G Network Functions

A 5G core network is not a single program — it's a collection of microservices called Network Functions (NFs). Here's what each one does:

| Network Function | Full Name | Role |
|-----------------|-----------|------|
| **AMF** | Access and Mobility Management Function | The "front door." Handles UE registration, authentication coordination, mobility (handovers), and connection management. The gNB talks to the AMF via the NGAP protocol. |
| **SMF** | Session Management Function | Manages PDU sessions (data connections). Decides which UPF to use, assigns IP addresses, and sets up the data path. |
| **UPF** | User Plane Function | The "data pipe." Forwards actual user traffic (packets) between the UE and the internet. Applies QoS rules and performs NAT. |
| **NRF** | Network Repository Function | The "phone book." Every NF registers here so others can discover it. If the NRF has wrong information, NFs can't find each other. |
| **UDM** | Unified Data Management | Manages subscriber identity and authentication data. Computes authentication vectors using the subscriber's keys. |
| **UDR** | Unified Data Repository | The database backend for UDM. Stores subscriber profiles, session policies, and authentication keys in MongoDB. |
| **AUSF** | Authentication Server Function | Handles the 5G-AKA authentication protocol. Verifies that the subscriber key on the SIM matches what's in the database. |
| **PCF** | Policy Control Function | Makes policy decisions (QoS, charging rules). Tells the SMF what bandwidth limits to apply. |
| **NSSF** | Network Slice Selection Function | Selects the appropriate network slice for a UE based on its subscription and request. |
| **SCP** | Service Communication Proxy | Routes messages between NFs. Acts as a service mesh — all inter-NF HTTP requests go through it. |
| **BSF** | Binding Support Function | Helps the PCF find the right session when policy changes need to be applied. |

Think of it like a restaurant: the AMF is the host who seats you, the SMF is the waiter who takes your order, the UPF is the kitchen that actually makes the food, and the NRF is the staff directory that tells everyone where to find each other.

---

## Glossary of Abbreviations

| Abbreviation | Full Form | What It Means in Practice |
|-------------|-----------|--------------------------|
| **PLMN** | Public Land Mobile Network | A unique identifier for a mobile network: MCC (country) + MNC (operator). Ours: 001/01 (test network). |
| **IMSI** | International Mobile Subscriber Identity | The unique ID for a SIM card. Format: MCC + MNC + MSIN (subscriber number). Example: 001010000000010. |
| **SUCI** | Subscription Concealed Identifier | Encrypted version of IMSI that the UE sends over the air (privacy protection). |
| **SUPI** | Subscription Permanent Identifier | The decrypted IMSI after the core network decodes the SUCI. |
| **S-NSSAI** | Single Network Slice Selection Assistance Information | Identifies a network slice. Has two parts: SST (Slice/Service Type, e.g., 1=eMBB) and SD (Slice Differentiator, optional). |
| **SST** | Slice/Service Type | The type of slice: 1=enhanced Mobile Broadband (eMBB), 2=URLLC, 3=MIoT. |
| **SD** | Slice Differentiator | Optional 3-byte value to distinguish multiple slices of the same SST. "ffffff" means "no specific SD." |
| **DNN** | Data Network Name | The name of the data network to connect to (like "internet" or "ims"). Equivalent to APN in 4G. |
| **TAC** | Tracking Area Code | A geographic/logical grouping of cells. UEs register in a tracking area, not individual cells. |
| **NGAP** | NG Application Protocol | The protocol between the gNB and the AMF (control plane). Runs over SCTP on port 38412. |
| **PDU Session** | Protocol Data Unit Session | A data connection between the UE and a data network. Has an IP address, QoS, and is anchored at a UPF. |
| **UE-AMBR** | UE Aggregate Maximum Bit Rate | The maximum total bandwidth allowed for a subscriber across all their connections. |
| **DRX** | Discontinuous Reception | Power-saving mode where the UE sleeps between monitoring intervals. Saves battery but adds latency. |
| **HARQ** | Hybrid Automatic Repeat Request | Error correction mechanism where the receiver ACKs or NACKs each transmission. Adds processing delay. |
| **SCS** | Subcarrier Spacing | Determines the width of each subcarrier in OFDM. Higher SCS = shorter symbols = lower latency but less coverage. |
| **TDD** | Time Division Duplexing | Uplink and downlink share the same frequency, taking turns in time. Pattern determines how slots are divided. |
| **gNB** | gNodeB | The 5G base station (radio unit + distributed unit + centralized unit). |
| **K** | Subscriber Key | A 128-bit secret key stored on the SIM and in the core network. Used for mutual authentication. |
| **OP** | Operator Key | The operator's root key, from which OPc is derived. |
| **OPc** | Operator Key (derived) | A derived key computed as AES(K, OP) XOR OP. Most systems store OPc directly, not OP. |
| **5G-AKA** | 5G Authentication and Key Agreement | The authentication protocol. The network proves it knows K, and the UE proves it knows K, without either transmitting K. |
| **NR** | New Radio | The 5G radio access technology (as opposed to LTE which is 4G). |
| **SCTP** | Stream Control Transmission Protocol | The transport protocol used for NGAP (gNB ↔ AMF). More reliable than TCP for signaling. |

---

## Infrastructure Setup

### The Physical Topology

```
                                    ┌──────────────────┐
                                    │  Ericsson BB6651  │
                                    │   G3 gNB         │
┌──────────┐                        │                  │
│   UE     │ ))) 5G Radio ))) ─────►│  n78 / 80 MHz    │
│ (Phone/  │                        │  2 cells/sectors │
│  TRM500) │                        └────────┬─────────┘
└──────────┘                                 │ 192.168.0.0/24
                                             │ NGAP (SCTP:38412)
                                             ▼
                                    ┌──────────────────┐
                                    │  k8sv2-1 Server  │
                                    │  (Kubernetes)    │
                                    │                  │
                                    │  AMF (hostNet)   │
                                    │  SMF, UPF, NRF   │
                                    │  UDM, UDR, AUSF  │
                                    │  PCF, NSSF, SCP  │
                                    │  MongoDB         │
                                    └────────┬─────────┘
                                             │
                                             │ NAT/Internet
                                             ▼
                                         🌐 Internet
```

### Key Network Details

- **gNB ↔ AMF**: The gNB connects to the AMF using NGAP over SCTP. The AMF must be reachable on the same network (192.168.0.201:38412). We used Kubernetes `hostNetwork: true` for the AMF pod so it binds directly to the server's physical IP.
- **Craft interface**: The gNB has a management port on link-local address 169.254.2.2, accessible from the toolserver's eno2 interface for MOSHELL (Ericsson's CLI tool).
- **Campus firewall**: The university's firewall blocks most ports. We access everything via SSH tunnels through the toolserver (130.235.32.171).

---

## Step 1: Deploying the 5G Core on Kubernetes

### The Helm Chart

We used the Gradiant Helm chart (v2.2.6) for Open5GS. The values file (`open5gs-values.yaml`) configures:
- Which NFs to enable (all 5G SA ones; all 4G ones disabled)
- MongoDB connection string
- PLMN identity (MCC=001, MNC=01)
- Network name, TAC, and slice configuration
- WebUI for subscriber management

### The MongoDB Challenge

The chart can deploy its own MongoDB, but we used an external MongoDB 6 instance with Longhorn persistent storage. Every NF that touches the database needs its own `dbURI` override — a quirk of the chart that caused confusion initially.

### Initial Deployment

```bash
helm upgrade --install open5gs gradiant/open5gs \
  -n open5gs --create-namespace \
  -f open5gs-values.yaml
```

This brings up all the NFs as Kubernetes pods. Looks easy, but the real fun hadn't started yet.

---

## Step 2: Getting the gNB to Talk to the AMF

### The Problem

After deploying the core, the gNB couldn't reach the AMF. The NG-Setup procedure (the first message a gNB sends to register with the AMF) was failing silently.

### Root Cause

The AMF was running inside the Kubernetes pod network (10.244.x.x) and not reachable from the gNB's network (192.168.0.0/24). The gNB sends NGAP over SCTP to port 38412, and it needs a real IP it can route to.

### The Fix

We configured the AMF pod with `hostNetwork: true` and pinned it to a specific node using `nodeSelector`. This makes the AMF bind to the host's physical IP (192.168.0.201) instead of a pod IP. The AMF config was updated:

```yaml
amf:
  ngap:
    server:
      - address: 192.168.0.201
  sbi:
    server:
      - address: 192.168.0.201
        port: 7777
```

### The Ongoing Annoyance

Every time you run `helm upgrade`, the AMF deployment resets and loses the `hostNetwork` setting. You have to re-apply a JSON patch after each upgrade. The old pod also holds port 38412, so you must delete it with `--grace-period=5` to release the port for the new pod.

---

## Step 3: The S-NSSAI Mismatch

### The Problem

NG-Setup succeeded (gNB and AMF could talk), but UEs couldn't register. The AMF rejected registration attempts.

### What's S-NSSAI?

When a UE connects, it tells the network which "slice" it wants to use. A slice is identified by:
- **SST** (Slice/Service Type): A number indicating the service category (1 = eMBB = normal broadband)
- **SD** (Slice Differentiator): An optional 3-byte hex value to distinguish multiple slices of the same type

### Root Cause

Our AMF was configured with `s_nssai: {sst: 1, sd: "000001"}`, but the gNB was broadcasting SST=1 with no SD. In 5G, "no SD" and "SD=000001" are different things. The AMF said "I don't support the slice you're offering" and rejected the connection.

### The Fix

Remove the SD from the AMF configuration:

```yaml
plmn_support:
  - plmn_id:
      mcc: "001"
      mnc: "01"
    s_nssai:
      - sst: 1    # No SD — matches the gNB
```

### Lesson

The value `sd: "ffffff"` means "no SD" in database records, but in the AMF config you simply omit the SD field entirely. These subtle representation differences between components are a recurring theme.

---

## Step 4: The NRF PLMN Disaster

### The Problem

After fixing the S-NSSAI, UEs could do NG-Setup but NF discovery failed. The AMF couldn't find the SMF, the UDM, or any other NF. Logs showed: `"No SEPP"` and `"NF-Discover failed [400]"`.

### What's Happening?

The SCP (Service Communication Proxy) routes all inter-NF messages. When it receives a request, it checks: "Is this for my own PLMN or a foreign one?" If foreign, it tries to route through a SEPP (Security Edge Protection Proxy) for inter-PLMN communication. We don't have a SEPP because we're a single network.

### Root Cause

The NRF's configmap had a **hardcoded** PLMN of 999/70 (a test value baked into the Helm chart template). Our AMF was registering with PLMN 001/01. When the SCP asked the NRF "who serves 001/01?", the NRF said "not me, I serve 999/70" — so the SCP treated it as a foreign network.

### The Fix

Patch the NRF configmap directly:

```bash
kubectl get configmap open5gs-nrf -n open5gs -o json | \
  jq '.data["nrf.yaml"] |= gsub("mcc: 999"; "mcc: 001") | 
      .data["nrf.yaml"] |= gsub("mnc: 70"; "mnc: 01")' | \
  kubectl apply -f -
```

Then restart the NRF pod. The chart's `config.mcc/mnc` values don't actually affect this section — it's a template bug.

### The Ongoing Annoyance

This patch is lost on every `helm upgrade`. Must be re-applied manually.

---

## Step 5: Subscriber Provisioning and Authentication Hell

### The Problem

NF discovery working. UE sends registration request. AMF receives it. But authentication fails: "MAC failure" in the logs.

### How 5G Authentication Works (Simplified)

1. UE sends SUCI (encrypted IMSI) to AMF
2. AMF asks AUSF to authenticate this subscriber
3. AUSF asks UDM to generate an authentication vector
4. UDM looks up the subscriber's **K** (subscriber key) and **OPc** (derived operator key) in the database
5. UDM computes a challenge (RAND) and expected response (XRES*)
6. The challenge goes back to the UE via the AMF
7. UE's SIM computes a response using its own K and OPc
8. If the response matches the expected response → authenticated
9. If not → "MAC failure" (Message Authentication Code mismatch)

### Root Cause: OP vs OPc

Our SIM card vendor provided:
- Subscriber Key (K): `00112233445566778899AABBCCDDEEFF`
- Operator Key: `62E75B8D6FA5BF46EC87A9276F9DF54D`

We initially configured this as OP (raw operator key) in the database. But it was actually **OPc** (the derived key). These are related by:

```
OPc = AES_encrypt(K, OP) XOR OP
```

If you store OPc in the OP field, the network computes `AES(K, OPc) XOR OPc` which gives garbage, and authentication fails with "MAC failure."

### The Fix

Store it as OPc, not OP:

```json
{
  "security": {
    "k": "00112233445566778899AABBCCDDEEFF",
    "opc": "62E75B8D6FA5BF46EC87A9276F9DF54D",
    "op": null
  }
}
```

### Lesson

SIM vendors are inconsistent about labeling. "Operator Key" usually means OPc in practice, even though OP and OPc are technically different things. If authentication fails with MAC error, try switching between OP and OPc.

---

## Step 6: The Incomplete Subscriber Record

### The Problem

Authentication succeeded! The UE registered! But then: "No AccessAndMobilitySubscriptionData" from the UDM, and "No UE-AMBR" from the UDR. The UE was stuck — registered but unable to establish a data connection.

### Root Cause

We had added subscribers using a minimal command that only created the security keys and slice info. But the UDR expects a **complete** subscriber record with fields that control mobility and session management. Missing fields:

| Field | Purpose | Required Value |
|-------|---------|----------------|
| `schema_version` | Database schema version | 1 |
| `ambr` | UE Aggregate Maximum Bit Rate (top-level) | {downlink: 1Gbps, uplink: 1Gbps} |
| `access_restriction_data` | Bitfield controlling access restrictions | 32 (no restrictions) |
| `subscriber_status` | Active/inactive | 0 (active) |
| `network_access_mode` | Packet-only/CS+PS | 0 |
| `operator_determined_barring` | Barring flags | 0 (none) |
| `subscribed_rau_tau_timer` | RAU/TAU timer value | 12 |

### The Fix

Patch each incomplete subscriber in MongoDB:

```javascript
db.subscribers.updateOne(
  {imsi: "001010000000018"},
  {$set: {
    schema_version: 1,
    ambr: {downlink: {value: 1000000000, unit: 0}, uplink: {value: 1000000000, unit: 0}},
    access_restriction_data: 32,
    network_access_mode: 0,
    subscriber_status: 0,
    operator_determined_barring: 0,
    subscribed_rau_tau_timer: 12
  }}
)
```

### Lesson

When adding subscribers via the WebUI, all fields are populated automatically. When scripting subscriber creation (e.g., via `open5gs-dbctl`), some fields may be missing. Always compare a working subscriber record against a new one to spot missing fields.

---

## Step 7: The Missing DNN

### The Problem

UE registers successfully. Tries to establish a PDU session. AMF logs: "DNN Not Supported OR Not Subscribed in the Slice."

### Investigation

By examining the AMF logs at info level, we could finally see what the UE was requesting:

```
UE SUPI[imsi-001010000000018] DNN[ims] S_NSSAI[SST:1 SD:0xffffff]
```

The UE device was requesting DNN "ims" (IP Multimedia Subsystem — used for VoLTE/VoNR voice calls), but our SMF only had DNN "internet" configured.

### The Fix

Three changes needed:

1. **Add "ims" DNN to SMF config** (in the configmap):
```yaml
session:
  - dnn: internet
    gateway: 10.45.0.1
    subnet: 10.45.0.0/16
  - dnn: ims
    gateway: 10.46.0.1
    subnet: 10.46.0.0/16
```

2. **Add "ims" DNN to UPF config** (separate TUN interface):
```yaml
session:
  - dev: ogstun
    dnn: internet
    gateway: 10.45.0.1
    subnet: 10.45.0.0/16
  - dev: ogstun2
    dnn: ims
    gateway: 10.46.0.1
    subnet: 10.46.0.0/16
```

3. **Configure the ogstun2 interface** in the UPF pod:
```bash
ip addr add 10.46.0.1/16 dev ogstun2
ip link set ogstun2 up
iptables -t nat -A POSTROUTING -s 10.46.0.0/16 ! -o ogstun2 -j MASQUERADE
```

4. **Add "ims" to subscriber records** in MongoDB.

### The Happy Ending

After these changes, UEs that requested "ims" initially failed (since the AMF had a cached stale context), but on retry they fell back to DNN "internet" and connected successfully. All four test UEs (IMSI ...010, ...011, ...017, ...018) eventually got PDU sessions with IP addresses on 10.45.0.0/16.

---

## Step 8: Benchmarking the Network

### The Tool

We built a custom TCP-based benchmark tool with a server running inside the Kubernetes cluster and a client on the laptop:

- **Server** (`benchmark-deploy.yaml`): Python TCP server in a pod, listening on port 9900. Timestamps every request to measure server processing time.
- **Client** (`benchmark-client.py`): Sends ping/download/upload requests and measures RTT, decomposing it into network time and server processing time.

### Profiles

| Profile | What it measures | Payload |
|---------|-----------------|---------|
| `latency-small` | Pure RTT | 64 bytes |
| `latency-medium` | RTT with moderate payload | 1 KB |
| `latency-large` | RTT with larger payload | 8 KB |
| `throughput-down` | Download speed | 64 KB chunks |
| `throughput-up` | Upload speed | 64 KB chunks |
| `mixed` | All three phases | Varies |

### Results: Where Does Time Go?

We ran the benchmark three ways:

| Path | Mean Latency (64B) | Throughput (DL) |
|------|--------------------|-----------------| 
| **Pod-to-pod** (inside cluster) | 0.23 ms | N/A |
| **SSH tunnel** (laptop → toolserver → cluster) | 14.99 ms | 26.93 Mbps |
| **5G radio** (laptop → TRM500 → gNB → core → server) | 10.10 ms | 33.17 Mbps |

### The Breakdown

```
End-to-end over 5G: 10.10 ms
├── Radio (UE ↔ gNB):  9.85 ms  (97.6%)
├── Core (gNB ↔ Pod):  0.23 ms  (2.2%)
└── Server processing: 0.02 ms  (0.2%)
```

The core network is essentially free — all the latency is in the radio air interface. This makes sense: the radio has to deal with scheduling, encoding, HARQ, and TDD slot timing. The core is just pod-to-pod networking on a local cluster.

---

## Step 9: Radio Tuning

With the network functional, we explored what could be tuned on the radio side to reduce that 10ms.

### Tool: MOSHELL

Ericsson's CLI tool for configuring base stations. Access:
```bash
ssh ubuntu@130.235.32.171      # SSH to toolserver
moshell 169.254.2.2            # Connect to gNB craft interface
```

### Experiment 1: Disabling DRX

**Hypothesis**: C-DRX (Connected-mode Discontinuous Reception) makes the UE sleep between monitoring cycles, adding latency.

**Command**:
```
set GNBDUFunction=1,UeCC=1,DrxProfile=Default,DrxProfileUeCfg=Base drxEnabled false
```

**Result**: No improvement. The UE was already staying awake because our continuous benchmark traffic kept the `drxInactivityTimer` running. DRX only hurts latency when there are idle gaps between packets.

### Experiment 2: Enabling Prescheduling

**Hypothesis**: Prescheduling allocates UL resources in advance, avoiding the scheduling request → grant → transmit cycle.

**Result**:

| Metric | Without | With Prescheduling |
|--------|---------|-------------------|
| Mean | 11.45 ms | 10.10 ms |
| P95 | 13.87 ms | 11.51 ms |
| P99 | 16.79 ms | 12.00 ms |
| Max | 22.24 ms | 13.80 ms |

Prescheduling significantly reduced tail latency — the worst-case times improved by 4-8ms. The distribution became tighter and more predictable.

### Experiment 3: Changing TDD Pattern

**Hypothesis**: More UL slots = shorter wait for uplink transmission opportunity.

**Current pattern**: TDD_ULDL_PATTERN_02 (DDDSU — 3 DL, 1 Special, 1 UL per 2.5ms)

**Attempted**: TDD_ULDL_PATTERN_03 (presumably DDSUU — 2 DL, 1 Special, 2 UL)

**Result**: Cell failed to activate (`availabilityStatus = FAILED`). The pattern was incompatible with the current special slot pattern or hardware configuration. Had to revert. The cell got stuck in DISABLED state and required manual recovery.

### Experiment 4: K1/K2 HARQ Timing

**Hypothesis**: Reducing K1 (DL-to-ACK delay) and K2 (grant-to-UL-data delay) would reduce round-trip time.

**Result**: These parameters are not exposed as configurable MOs on the BB6651 G3. They are auto-derived from the TDD pattern. Not tunable on this hardware/software version.

---

## Radio Parameters Explained

### Subcarrier Spacing (SCS) / Numerology

5G NR uses OFDM (Orthogonal Frequency Division Multiplexing) with configurable subcarrier spacing. The "numerology" determines the timing:

| Numerology | SCS | Slot Duration | Symbol Duration | Use Case |
|-----------|------|--------------|-----------------|----------|
| 0 | 15 kHz | 1.0 ms | 66.7 μs | Low-band FDD (< 1 GHz) |
| 1 | 30 kHz | 0.5 ms | 33.3 μs | Mid-band TDD (1-6 GHz) — **ours** |
| 2 | 60 kHz | 0.25 ms | 16.7 μs | Mid-band or mmWave |
| 3 | 120 kHz | 0.125 ms | 8.3 μs | mmWave (> 24 GHz) |

Higher SCS = shorter slots = lower latency, but also shorter cyclic prefix = less multipath tolerance = reduced coverage range. For n78 at 3.5 GHz, 30 kHz is the standard choice.

### TDD UL/DL Pattern

In TDD (Time Division Duplexing), the same frequency is shared between uplink and downlink by dividing time into slots:

- **D** (Downlink): gNB transmits to UE
- **U** (Uplink): UE transmits to gNB
- **S** (Special): Transition slot with DL symbols, guard period, and UL symbols

Our pattern (DDDSU, 2.5ms period):
```
Time →
┌─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┐
│  D  │  D  │  D  │  S  │  U  │  D  │  D  │  D  │  S  │  U  │
└─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┘
 0.5ms                          2.5ms                     5.0ms
```

**Latency impact**: If the UE has UL data to send and it's currently in a DL slot, it must wait until the next U slot. Worst case: ~2ms wait (4 slots × 0.5ms). Average: ~1ms.

**Tradeoffs**:
- More D slots → higher DL throughput, higher UL latency
- More U slots → lower UL latency, lower DL throughput
- Shorter period → lower maximum latency, but more special slots = more overhead

### DRX (Discontinuous Reception)

DRX lets the UE turn off its radio between scheduled monitoring occasions to save battery:

```
        ┌─on─┐                    ┌─on─┐                    ┌─on─┐
UE RX:  █████░░░░░░░░░░░░░░░░░░░░█████░░░░░░░░░░░░░░░░░░░░░█████
              ◄── sleep ──────────►     ◄── sleep ──────────►
              ◄───── drxLongCycle ─────►
```

Key parameters:
- **drxLongCycle**: Time between wake-ups (e.g., 40ms). Higher = more battery savings, more latency.
- **drxOnDurationTimer**: How long the UE stays awake listening (e.g., 4ms).
- **drxInactivityTimer**: How long the UE stays awake after the last received/transmitted data. As long as traffic keeps flowing, the timer resets and the UE never enters DRX sleep.

**Our finding**: With continuous benchmark traffic, `drxInactivityTimer` kept the UE permanently awake, making DRX irrelevant. DRX only matters for bursty traffic with idle gaps longer than the inactivity timer.

### HARQ (Hybrid Automatic Repeat Request)

HARQ provides rapid error correction at the physical layer:

1. gNB sends DL data in slot N
2. UE decodes and sends ACK/NACK in slot N + K1
3. If NACK: gNB retransmits in the next available DL slot

For uplink:
1. UE receives UL grant in slot N
2. UE transmits data in slot N + K2
3. gNB sends ACK/NACK back

**K1** (DL HARQ feedback timing): Number of slots between receiving DL data and sending the HARQ ACK/NACK. Minimum depends on UE processing capability.

**K2** (UL scheduling timing): Number of slots between receiving a UL grant and actually transmitting. The UE needs this time to prepare the transport block.

**TDD complication**: Even if K1=4, the ACK must be sent in a UL slot. If slot N+4 is a DL slot, the UE waits for the next available UL slot — potentially adding another full TDD period of delay.

On our BB6651, K1 and K2 are automatically derived from the TDD pattern and not directly tunable.

### Prescheduling

Without prescheduling, the UL path is:
1. UE has data → sends Scheduling Request (SR) in next available UL slot
2. gNB processes SR → sends UL grant in next DL slot
3. UE receives grant → transmits data in slot N + K2

With prescheduling:
1. gNB pre-allocates UL resources for active UEs
2. UE has data → transmits immediately in the next pre-allocated slot

This eliminates the SR → grant round-trip (saving ~2-5ms). The tradeoff is wasted UL resources when the UE has nothing to send.

### Configured Grant Periodicity

Related to prescheduling. A "configured grant" is a semi-persistent UL allocation that repeats every N slots without the UE needing to request it each time. Our value: 10 slots (5ms at 30 kHz SCS).

### SR (Scheduling Request) Periodicity

How often the UE has an opportunity to send a Scheduling Request on PUCCH. Lower periodicity = more frequent opportunities = lower average wait time for first UL grant. Not directly configurable on our node (auto-derived from the SR periodicity profile).

### Cell Range

Set to 5000 (meters). Affects timing advance calculations and guard periods. For a campus deployment this is generous — most UEs are within a few hundred meters.

### CSI-RS Periodicity

Channel State Information Reference Signals, sent every 40 slots (20ms). Used by the UE to measure channel quality and report back for link adaptation (choosing the right modulation and coding scheme).

### SSB Periodicity

Synchronization Signal Block periodicity: 20ms. This is how often the gNB broadcasts its identity and synchronization signals. UEs use SSBs for initial cell search, measurement, and handover decisions.

---

## Lessons Learned

### 1. Everything Must Be Consistent

A 5G network has dozens of configuration points that must all agree on the same PLMN, S-NSSAI, and DNN values. If any single component disagrees, things fail in mysterious ways with unhelpful error messages.

### 2. Logs Are Your Friends (At the Right Level)

Most problems were diagnosed by reading AMF/UDM/UDR logs. The info level gives you the what (registration accepted/rejected), but you sometimes need to correlate across multiple NFs to understand the why.

### 3. The Radio Is the Bottleneck

For a local deployment, the core network adds less than 0.25ms. The radio air interface dominates at ~10ms. If latency matters, tune the radio parameters — the core is already fast enough.

### 4. Helm Charts Lie

The Gradiant Open5GS Helm chart works, but it has hardcoded values (like the NRF's PLMN) that don't match what you configure. Always verify the actual configmaps after deployment.

### 5. Subscriber Records Must Be Complete

A subscriber with just security keys and a slice config will authenticate but fail at session establishment. The record needs UE-AMBR, access restriction data, subscriber status, and other "boring" fields that the UDR checks.

### 6. SIM Vendor Documentation Is Unreliable

"Operator Key" might mean OP or OPc depending on the vendor. Try both. If 5G-AKA fails with MAC error, switch between OP and OPc.

### 7. TDD Pattern Changes Are Dangerous

Changing the TDD pattern can render the cell unable to activate. Always be ready to revert, and remember that the cell must be locked (administrativeState=LOCKED) before modification.

### 8. DRX Doesn't Matter Under Load

For continuous traffic benchmarks, DRX is irrelevant because the inactivity timer keeps the UE awake. DRX only matters for the first packet after an idle period — relevant for IoT or interactive applications.

### 9. Prescheduling Helps the Most

Of all radio parameters tested, UL prescheduling provided the most significant latency improvement, particularly for tail latency (P99). It eliminates the scheduling request round-trip at the cost of some wasted UL capacity.

### 10. Keep a Reference Subscriber

Always maintain one known-working subscriber record that you can compare against when debugging new additions. The diff between a working and non-working record often reveals the missing field immediately.

---

## Summary of Final Configuration

| Component | Setting |
|-----------|---------|
| Band | n78 (3.5 GHz) |
| Bandwidth | 80 MHz |
| SCS | 30 kHz (numerology 1) |
| TDD Pattern | DDDSU (2.5ms period) |
| DRX | Disabled for benchmarking |
| Prescheduling | Enabled |
| PLMN | 001/01 |
| TAC | 1 |
| DNN | internet (10.45.0.0/16) + ims (10.46.0.0/16) |
| Best latency achieved | 10.10ms mean, 11.51ms P95 |
| Download throughput | 33.17 Mbps |
| Upload throughput | 17.92 Mbps |
