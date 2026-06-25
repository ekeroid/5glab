#!/usr/bin/env python3
"""
EdgeVision benchmark — Runs local CPU inference for 10s, then edge GPU
inference for 10s, and reports the performance comparison.

Prerequisites:
  - camera-api running on localhost:8081
  - kubectl port-forward -n edgevision svc/nef-shim 9191:80
"""

import sys
import os
import time
import statistics

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "client"))

os.environ.setdefault("CAMERA_API_URL", "http://localhost:8081")
os.environ.setdefault("CAMARA_API_URL", "http://localhost:9191")
os.environ.setdefault("CAMARA_PROXY_BASE_URL", "http://localhost:9191")
os.environ.setdefault("CONFIDENCE_THRESHOLD", "0.35")
os.environ.setdefault("OUTPUT_DIR", os.path.join(os.path.dirname(__file__), "..", "output"))
os.environ.setdefault("LOG_LEVEL", "WARNING")

import requests
import camara
import detector_local
import detector_remote

DURATION = 10


def fetch_frame() -> bytes:
    resp = requests.get(f"{os.environ['CAMERA_API_URL']}/frame", timeout=10)
    resp.raise_for_status()
    return resp.content


def run_local(duration: float) -> list[float]:
    """Run local inference for `duration` seconds, return list of latencies in ms."""
    latencies = []
    end = time.time() + duration
    while time.time() < end:
        jpeg = fetch_frame()
        t0 = time.perf_counter()
        dets = detector_local.detect(jpeg)
        ms = (time.perf_counter() - t0) * 1000
        latencies.append(ms)
        print(f"  LOCAL  {ms:6.0f}ms | {len(dets)} detections", flush=True)
    return latencies


def run_edge(duration: float) -> list[float]:
    """Run edge inference for `duration` seconds, return list of latencies in ms."""
    print("\n  Setting up CAMARA edge instance...")
    zone = camara.discover_zone()
    zone_id = zone["edgeCloudZoneId"]
    app_id = camara.register_app()
    instance_id = camara.instantiate_app(app_id, zone_id)

    # Wait for ready
    for _ in range(60):
        status = camara.get_instance_status(instance_id)
        if status == "ready":
            break
        time.sleep(5)
    else:
        raise RuntimeError("Instance not ready after 300s")

    endpoint = camara.get_endpoint(instance_id)
    print(f"  Endpoint: {endpoint}\n")

    latencies = []
    end = time.time() + duration
    try:
        while time.time() < end:
            jpeg = fetch_frame()
            t0 = time.perf_counter()
            dets = detector_remote.detect(jpeg, endpoint)
            ms = (time.perf_counter() - t0) * 1000
            latencies.append(ms)
            print(f"  EDGE   {ms:6.0f}ms | {len(dets)} detections", flush=True)
    finally:
        camara.terminate_app(instance_id)

    return latencies


def report(local_ms: list[float], edge_ms: list[float]):
    print("\n" + "=" * 60)
    print("BENCHMARK RESULTS")
    print("=" * 60)

    def stats(values):
        return {
            "frames": len(values),
            "mean": statistics.mean(values),
            "median": statistics.median(values),
            "p95": sorted(values)[int(len(values) * 0.95)] if len(values) >= 2 else values[-1],
            "min": min(values),
            "max": max(values),
            "fps": 1000 / statistics.mean(values),
        }

    ls = stats(local_ms)
    es = stats(edge_ms)

    print(f"\n{'':20s} {'LOCAL (CPU)':>14s}  {'EDGE (GPU)':>14s}  {'Speedup':>10s}")
    print(f"{'─' * 62}")
    print(f"{'Frames processed':20s} {ls['frames']:>14d}  {es['frames']:>14d}")
    print(f"{'Mean latency':20s} {ls['mean']:>11.0f} ms  {es['mean']:>11.0f} ms  {ls['mean']/es['mean']:>9.1f}x")
    print(f"{'Median latency':20s} {ls['median']:>11.0f} ms  {es['median']:>11.0f} ms  {ls['median']/es['median']:>9.1f}x")
    print(f"{'P95 latency':20s} {ls['p95']:>11.0f} ms  {es['p95']:>11.0f} ms")
    print(f"{'Min latency':20s} {ls['min']:>11.0f} ms  {es['min']:>11.0f} ms")
    print(f"{'Max latency':20s} {ls['max']:>11.0f} ms  {es['max']:>11.0f} ms")
    print(f"{'Throughput':20s} {ls['fps']:>10.1f} fps  {es['fps']:>10.1f} fps")
    print(f"\n{'─' * 62}")

    if es['mean'] < ls['mean']:
        print(f"  Edge GPU is {ls['mean']/es['mean']:.1f}x faster than local CPU")
    else:
        print(f"  Local CPU is {es['mean']/ls['mean']:.1f}x faster (network overhead dominates)")

    print()
    print("  Note: Only ~200KB JPEG is sent over the wire (preprocessing")
    print("  happens server-side). Remaining edge latency is preprocessing")
    print("  on a single CPU core + GPU inference + network round-trip.")
    print()


def main():
    print("=" * 60)
    print("EdgeVision Benchmark: LOCAL vs EDGE")
    print("=" * 60)

    print(f"\n[Phase 1] Running LOCAL (CPU) inference for {DURATION}s...\n")
    local_ms = run_local(DURATION)

    print(f"\n[Phase 2] Running EDGE (GPU) inference for {DURATION}s...")
    edge_ms = run_edge(DURATION)

    report(local_ms, edge_ms)


if __name__ == "__main__":
    main()
