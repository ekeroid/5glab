#!/usr/bin/env python3
"""Benchmark — automated CAMARA setup + inference latency measurement."""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "client"))

import config
import camara
import detector_remote

ITERATIONS = 20
JPEG_PATH = os.path.join(os.path.dirname(__file__), "camera-api", "images")


def get_test_jpeg() -> bytes:
    images = sorted(f for f in os.listdir(JPEG_PATH) if f.endswith(".jpg"))
    path = os.path.join(JPEG_PATH, images[0])
    with open(path, "rb") as f:
        return f.read()


def main():
    print("=== EdgeVision Latency Benchmark ===\n")

    jpeg = get_test_jpeg()
    print(f"Test image: {len(jpeg)} bytes\n")

    # Step 1: CAMARA setup
    print("[1] Discovering edge zone...")
    t0 = time.perf_counter()
    zone = camara.discover_zone()
    zone_id = zone["edgeCloudZoneId"]
    print(f"    Zone: {zone.get('edgeCloudZoneName', zone_id)} ({(time.perf_counter()-t0)*1000:.0f}ms)")

    print("[2] Registering app...")
    t0 = time.perf_counter()
    app_id = camara.register_app()
    print(f"    App: {app_id[:12]}... ({(time.perf_counter()-t0)*1000:.0f}ms)")

    print("[3] Instantiating...")
    t0 = time.perf_counter()
    instance_id = camara.instantiate_app(app_id, zone_id)
    print(f"    Instance: {instance_id[:12]}... ({(time.perf_counter()-t0)*1000:.0f}ms)")

    print("[4] Waiting for ready...")
    t0 = time.perf_counter()
    for i in range(60):
        detail = camara.get_instance_status(instance_id)
        phase = detail.get("phase", "unknown")
        status = detail["status"]
        if status == "ready":
            print(f"    READY in {(time.perf_counter()-t0)*1000:.0f}ms")
            break
        elif status == "failed":
            print(f"    FAILED: {detail.get('message')}")
            sys.exit(1)
        if i % 5 == 0:
            print(f"    ... {phase}: {detail.get('message', '')}")
        time.sleep(3)
    else:
        print("    TIMEOUT")
        sys.exit(1)

    print("[5] Getting endpoints...")
    grpc_ep, http_ep = camara.get_endpoints(instance_id)
    print(f"    gRPC: {grpc_ep}")
    print(f"    HTTP: {http_ep}")

    # Step 2: Benchmark gRPC
    print(f"\n--- gRPC benchmark ({ITERATIONS} iterations) ---")
    config.EDGE_TRANSPORT = "grpc"
    detector_remote._reset_channel()
    _run_benchmark(jpeg, grpc_ep)

    # Step 3: Benchmark HTTP
    print(f"\n--- HTTP benchmark ({ITERATIONS} iterations) ---")
    config.EDGE_TRANSPORT = "http"
    detector_remote._session = None
    _run_benchmark(jpeg, http_ep)

    # Cleanup
    print("\n[cleanup] Terminating instance...")
    camara.terminate_app(instance_id)
    print("Done.")


def _run_benchmark(jpeg: bytes, endpoint: str):
    timings = []

    # Warmup
    for _ in range(3):
        try:
            detector_remote.detect(jpeg, endpoint, max_retries=1)
        except Exception:
            time.sleep(1)

    for i in range(ITERATIONS):
        try:
            _, timing = detector_remote.detect(jpeg, endpoint, max_retries=1)
            timings.append(timing)
            print(f"  [{i+1:2d}] total={timing['total_ms']:5.1f}ms  "
                  f"server={timing['server_ms']:5.1f}ms  "
                  f"pre={timing['preprocess_ms']:4.1f}  "
                  f"infer={timing['inference_ms']:4.1f}  "
                  f"post={timing['postprocess_ms']:4.1f}  "
                  f"net={timing['network_ms']:5.1f}ms")
        except Exception as e:
            print(f"  [{i+1:2d}] ERROR: {e}")
        time.sleep(0.1)

    if timings:
        avg_total = sum(t["total_ms"] for t in timings) / len(timings)
        avg_server = sum(t["server_ms"] for t in timings) / len(timings)
        avg_net = sum(t["network_ms"] for t in timings) / len(timings)
        avg_pre = sum(t["preprocess_ms"] for t in timings) / len(timings)
        avg_infer = sum(t["inference_ms"] for t in timings) / len(timings)
        avg_post = sum(t["postprocess_ms"] for t in timings) / len(timings)
        p50_total = sorted(t["total_ms"] for t in timings)[len(timings)//2]
        p95_total = sorted(t["total_ms"] for t in timings)[int(len(timings)*0.95)]

        print(f"\n  SUMMARY ({len(timings)} samples):")
        print(f"    total:  avg={avg_total:.1f}ms  p50={p50_total:.1f}ms  p95={p95_total:.1f}ms")
        print(f"    server: avg={avg_server:.1f}ms (pre={avg_pre:.1f} + infer={avg_infer:.1f} + post={avg_post:.1f})")
        print(f"    network: avg={avg_net:.1f}ms")


if __name__ == "__main__":
    main()
