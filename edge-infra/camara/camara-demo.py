#!/usr/bin/env python3
"""
CAMARA Edge Discovery & Compute Offload Demo

Demonstrates edge compute offloading via the CAMARA Simple Edge Discovery API.
Compares local CPU matrix multiplication with GPU-accelerated edge compute.

Requirements: numpy, requests
    pip install numpy requests
"""

import argparse
import platform
import sys
import time
import uuid

import numpy as np

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package not installed. Run: pip install requests")
    sys.exit(1)


# ─── ANSI Colors ──────────────────────────────────────────────────────────────

class C:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    CYAN = "\033[36m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    MAGENTA = "\033[35m"
    RESET = "\033[0m"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def banner():
    print()
    print(f"{C.CYAN}╔════════════════════════════════════════════════════════════════════╗{C.RESET}")
    print(f"{C.CYAN}║{C.BOLD}          CAMARA Edge Discovery & Compute Offload Demo            {C.RESET}{C.CYAN}║{C.RESET}")
    print(f"{C.CYAN}╚════════════════════════════════════════════════════════════════════╝{C.RESET}")
    print()


def stage_header(num, title):
    print(f"{C.BOLD}{C.GREEN}[Stage {num}/4] {title}{C.RESET}")


def info(msg):
    print(f"  {msg}")


def detail(label, value):
    print(f"  {C.DIM}{label:<10}{C.RESET} {value}")


def error(msg):
    print(f"  {C.RED}ERROR:{C.RESET} {msg}")


def get_local_hardware():
    """Detect local CPU description."""
    machine = platform.machine()
    if machine == "arm64" and platform.system() == "Darwin":
        # Try to get Apple chip name
        try:
            import subprocess
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
        return "Apple Silicon (ARM64)"
    elif machine == "x86_64":
        return "x86_64 CPU"
    return f"{platform.processor() or machine}"


def format_gflops(gflops):
    """Format GFLOPS with comma separators."""
    if gflops >= 1000:
        return f"{gflops:,.0f}"
    return f"{gflops:.1f}"


def compute_gflops(size, iterations, elapsed):
    """Compute GFLOPS for matrix multiplication.

    FLOPs for one matmul of NxN matrices: 2*N^3 (multiply-add)
    """
    flops_per_iter = 2 * (size ** 3)
    total_flops = flops_per_iter * iterations
    gflops = total_flops / elapsed / 1e9
    return gflops


# ─── Stage 1: Local Compute ──────────────────────────────────────────────────

def stage_local_compute(size, iterations):
    stage_header(1, "Local Compute")
    info(f"Running matrix multiply {size}x{size} ({iterations} iterations) on CPU...")
    info("")

    # Warm up numpy
    _ = np.random.randn(256, 256) @ np.random.randn(256, 256)

    A = np.random.randn(size, size).astype(np.float64)
    B = np.random.randn(size, size).astype(np.float64)

    start = time.perf_counter()
    for _ in range(iterations):
        _ = A @ B
    elapsed = time.perf_counter() - start

    gflops = compute_gflops(size, iterations, elapsed)
    hw = get_local_hardware()

    info(f"  Elapsed:    {elapsed:.2f}s")
    info(f"  Throughput: {format_gflops(gflops)} GFLOPS")
    info(f"  Hardware:   {hw}")
    print()

    return {"elapsed": elapsed, "gflops": gflops, "hardware": hw}


# ─── Stage 2: Edge Discovery ─────────────────────────────────────────────────

MOCK_DISCOVERY_RESPONSE = {
    "edgeCloudZones": [
        {
            "edgeCloudZoneId": "lth-5glab-gpu-zone",
            "edgeCloudZoneName": "LTH 5G Lab GPU Edge",
            "edgeCloudProvider": "lth-kubernetes",
            "status": "active",
            "computeEndpoint": "http://camara.5glab.control.lth.se/compute/v0",
            "capabilities": {
                "gpuAvailable": True,
                "gpuModel": "NVIDIA L40S",
                "gpuCount": 2,
                "cpuCores": 128,
                "memoryGB": 768
            },
            "location": {
                "latitude": 55.7115,
                "longitude": 13.2108
            }
        }
    ]
}


def stage_edge_discovery(api_url, dry_run, headers=None):
    stage_header(2, "Edge Discovery via CAMARA API")
    endpoint = f"{api_url}/simple-edge-discovery/v0/edge-cloud-zones"
    params = {"device-ip": "10.45.0.2"}

    info(f"Querying: GET /simple-edge-discovery/v0/edge-cloud-zones?device-ip=10.45.0.2")
    info(f"  URL: {endpoint}")
    info("")

    if dry_run:
        info(f"{C.YELLOW}[dry-run] Using mock discovery response{C.RESET}")
        data = MOCK_DISCOVERY_RESPONSE
    else:
        try:
            resp = requests.get(endpoint, params=params, headers=headers or {}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.ConnectionError:
            error("Cannot reach the CAMARA API.")
            info("")
            info(f"  {C.YELLOW}Possible fixes:{C.RESET}")
            info(f"    - Ensure VPN/SSH tunnel to lab is active")
            info(f"    - Try: ssh -L 8080:130.235.32.171:80 user@gateway")
            info(f"    - Or use --dry-run to test with mock data")
            print()
            return None
        except requests.exceptions.HTTPError as e:
            error(f"API returned error: {e}")
            print()
            return None
        except requests.exceptions.Timeout:
            error("Request timed out (10s). Is the API endpoint reachable?")
            print()
            return None
        except Exception as e:
            error(f"Unexpected error: {e}")
            print()
            return None

    zones = data.get("edgeCloudZones", [])
    if not zones:
        error("No edge cloud zones discovered.")
        print()
        return None

    zone = zones[0]
    info(f"  {C.BOLD}Discovered edge zone:{C.RESET}")
    detail("Name:", zone.get("edgeCloudZoneName", "unknown"))
    detail("Provider:", zone.get("edgeCloudProvider", "unknown"))

    caps = zone.get("capabilities", {})
    if caps:
        gpu_model = caps.get("gpuModel", "unknown")
        gpu_count = caps.get("gpuCount", "?")
        detail("GPU:", f"{gpu_count}x {gpu_model}")
        detail("CPU:", f"{caps.get('cpuCores', '?')} cores")
        detail("Memory:", f"{caps.get('memoryGB', '?')} GB")

    loc = zone.get("location", {})
    if loc:
        lat = loc.get("latitude", 0)
        lon = loc.get("longitude", 0)
        lat_dir = "N" if lat >= 0 else "S"
        lon_dir = "E" if lon >= 0 else "W"
        detail("Location:", f"{abs(lat):.2f}°{lat_dir}, {abs(lon):.2f}°{lon_dir}")

    advertised_endpoint = zone.get("computeEndpoint", f"{api_url}/compute/v0")
    compute_endpoint = f"{api_url}/compute/v0/jobs"
    if advertised_endpoint != compute_endpoint.replace("/jobs", ""):
        info(f"  {C.DIM}(advertised: {advertised_endpoint}){C.RESET}")
    info("")
    info(f"  Compute endpoint: {C.CYAN}{compute_endpoint}{C.RESET}")
    print()

    return {"zone": zone, "compute_endpoint": compute_endpoint}


# ─── Stage 3: Edge Offload ────────────────────────────────────────────────────

MOCK_JOB_RESPONSE = {
    "jobId": f"edge-compute-{uuid.uuid4().hex[:8]}",
    "status": "completed",
    "result": {
        "elapsed_s": 0.006,
        "gflops": 113000,
        "backend": "gpu",
        "device": "NVIDIA L40S",
        "size": 4096,
        "iterations": 5
    }
}


def stage_edge_offload(compute_endpoint, size, iterations, api_url, dry_run, headers=None):
    stage_header(3, "Edge Compute Offload")
    info(f"Submitting workload to edge: matrix_multiply {size}x{size} ({iterations} iterations)")
    info("")

    payload = {
        "workload": "matrix_multiply",
        "size": size,
        "iterations": iterations
    }

    if dry_run:
        info(f"{C.YELLOW}[dry-run] Using mock job response{C.RESET}")
        data = MOCK_JOB_RESPONSE
        job_id = data.get("jobId", "unknown")
        info(f"  Job ID:  {job_id}")
        info(f"  Status:  {C.GREEN}completed{C.RESET}")
    else:
        try:
            resp = requests.post(compute_endpoint, json=payload, headers=headers or {}, timeout=30)
            resp.raise_for_status()
            submit_data = resp.json()
        except requests.exceptions.ConnectionError:
            error("Cannot reach compute endpoint.")
            print()
            return None
        except requests.exceptions.HTTPError as e:
            error(f"Compute API returned error: {e}")
            print()
            return None
        except requests.exceptions.Timeout:
            error("Submission timed out (30s).")
            print()
            return None
        except Exception as e:
            error(f"Unexpected error: {e}")
            print()
            return None

        job_id = submit_data.get("jobId", "unknown")
        info(f"  Job ID:  {job_id}")
        info(f"  Status:  {C.YELLOW}pending{C.RESET} → scheduling on GPU node...")

        poll_url = f"{compute_endpoint}/{job_id}"
        data = None
        for attempt in range(90):
            time.sleep(2)
            try:
                poll_resp = requests.get(poll_url, headers=headers or {}, timeout=10)
                poll_resp.raise_for_status()
                data = poll_resp.json()
                status = data.get("status", "unknown")
                if status == "completed":
                    info(f"  Status:  {C.GREEN}completed{C.RESET} ({(attempt+1)*2}s)")
                    break
                elif status == "failed":
                    error("Job failed on edge.")
                    error_info = data.get("result", {})
                    if error_info:
                        info(f"  Detail: {error_info}")
                    print()
                    return None
            except Exception:
                pass
        else:
            error("Job did not complete within 3 minutes.")
            print()
            return None

    result = data.get("result", {})
    if not result:
        error("No result data returned from edge.")
        print()
        return None

    elapsed = result.get("elapsed_s", 0)
    gflops = result.get("gflops", 0)
    device = result.get("device", "unknown GPU")
    backend = result.get("backend", "gpu")

    info("")
    info(f"  {C.BOLD}Result:{C.RESET}")
    info(f"    Elapsed:    {elapsed:.4f}s")
    info(f"    Throughput: {format_gflops(gflops)} GFLOPS")
    info(f"    Device:     {device}")
    info(f"    Backend:    {backend}")
    print()

    return {"elapsed": elapsed, "gflops": gflops, "device": device}


# ─── Stage 4: Comparison ─────────────────────────────────────────────────────

def stage_comparison(local_result, edge_result):
    stage_header(4, "Results Comparison")
    print()

    local_time = local_result["elapsed"]
    local_gflops = local_result["gflops"]
    local_hw = local_result["hardware"]

    edge_time = edge_result["elapsed"]
    edge_gflops = edge_result["gflops"]
    edge_hw = edge_result["device"]

    speedup_time = local_time / edge_time if edge_time > 0 else float("inf")
    speedup_gflops = edge_gflops / local_gflops if local_gflops > 0 else float("inf")

    # Table formatting
    col_w = [13, 10, 14, 27]
    total_w = sum(col_w) + len(col_w) + 1  # +1 for each border

    def row(cells, fill=" "):
        parts = []
        for i, cell in enumerate(cells):
            parts.append(f" {cell:<{col_w[i] - 1}}")
        return "│" + "│".join(parts) + "│"

    def sep(left, mid, right, h="─"):
        parts = []
        for w in col_w:
            parts.append(h * w)
        return left + mid.join(parts) + right

    print(f"  {C.CYAN}{sep('┌', '┬', '┐')}{C.RESET}")
    header = row(["LOCATION", "TIME", "THROUGHPUT", "HARDWARE"])
    print(f"  {C.CYAN}{C.BOLD}{header}{C.RESET}")
    print(f"  {C.CYAN}{sep('├', '┼', '┤')}{C.RESET}")

    local_row = row([
        "Local (CPU)",
        f"{local_time:.2f}s",
        f"{format_gflops(local_gflops)} GFLOPS",
        local_hw[:25]
    ])
    print(f"  {local_row}")

    edge_row = row([
        "Edge (GPU)",
        f"{edge_time:.3f}s",
        f"{format_gflops(edge_gflops)} GFLOPS",
        edge_hw[:25]
    ])
    print(f"  {C.GREEN}{edge_row}{C.RESET}")

    print(f"  {C.CYAN}{sep('├', '┼', '┤')}{C.RESET}")

    speedup_row = row([
        f"{C.BOLD}Speedup{C.RESET}",
        f"{C.BOLD}{speedup_time:.0f}x{C.RESET}",
        f"{C.BOLD}{speedup_gflops:.0f}x{C.RESET}",
        "via CAMARA Edge API"
    ])
    print(f"  {speedup_row}")
    print(f"  {C.CYAN}{sep('└', '┴', '┘')}{C.RESET}")

    print()
    info(f"{C.BOLD}Conclusion:{C.RESET} Edge GPU offload via CAMARA Simple Edge Discovery")
    info(f"achieved a {C.GREEN}{C.BOLD}{speedup_time:.0f}x{C.RESET} time speedup and "
         f"{C.GREEN}{C.BOLD}{speedup_gflops:.0f}x{C.RESET} throughput improvement.")
    print()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="CAMARA Edge Discovery & Compute Offload Demo"
    )
    parser.add_argument(
        "--api-url",
        default="http://camara.5glab.control.lth.se",
        help="Base URL of the CAMARA API (default: http://camara.5glab.control.lth.se)"
    )
    parser.add_argument(
        "--size",
        type=int,
        default=4096,
        help="Matrix size NxN (default: 4096)"
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=10,
        help="Number of iterations (default: 10)"
    )
    parser.add_argument(
        "--host-header",
        default=None,
        help="Override Host header (useful with port-forward, e.g. camara.5glab.control.lth.se)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip API calls and use mock data (for testing formatting)"
    )

    args = parser.parse_args()

    banner()

    headers = {}
    if args.host_header:
        headers["Host"] = args.host_header

    if args.dry_run:
        print(f"  {C.YELLOW}{C.BOLD}[DRY-RUN MODE]{C.RESET} API calls will use mock responses.\n")

    # Stage 1: Local compute
    local_result = stage_local_compute(args.size, args.iterations)

    # Stage 2: Edge discovery
    discovery = stage_edge_discovery(args.api_url, args.dry_run, headers)
    if discovery is None:
        print(f"{C.RED}Aborting: Edge discovery failed. Use --dry-run to test without API.{C.RESET}")
        sys.exit(1)

    compute_endpoint = discovery["compute_endpoint"]

    # Stage 3: Edge offload
    edge_result = stage_edge_offload(
        compute_endpoint, args.size, args.iterations, args.api_url, args.dry_run, headers
    )
    if edge_result is None:
        print(f"{C.RED}Aborting: Edge compute failed. Use --dry-run to test without API.{C.RESET}")
        sys.exit(1)

    # Stage 4: Comparison
    stage_comparison(local_result, edge_result)


if __name__ == "__main__":
    main()
