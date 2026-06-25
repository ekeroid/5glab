#!/usr/bin/env python3
"""
5G Core Benchmark Client

Workload profiles:
  latency-small    - 64-byte ping/pong (measures pure RTT)
  latency-medium   - 1KB ping/pong
  latency-large    - 8KB ping/pong
  throughput-down  - Download 64KB chunks (server -> client)
  throughput-up    - Upload 64KB chunks (client -> server)
  mixed            - Mix of small latency + throughput

Usage:
  python3 benchmark-client.py [host] [port] [profile] [duration_seconds]

Examples:
  python3 benchmark-client.py 127.0.0.1 9900 latency-small 10
  python3 benchmark-client.py 127.0.0.1 9900 throughput-down 10
  python3 benchmark-client.py 127.0.0.1 9900 mixed 15
"""
import socket
import struct
import time
import sys
import statistics

HEADER_FMT = "!BIId"  # type(1), sequence(4), payload_size(4), timestamp(8)
HEADER_SIZE = struct.calcsize(HEADER_FMT)

MSG_PING = 1
MSG_DOWNLOAD = 2
MSG_UPLOAD = 3
MSG_PONG = 4
MSG_DATA = 5
MSG_ACK = 6

PROFILES = {
    "latency-small":  {"type": "latency", "size": 64},
    "latency-medium": {"type": "latency", "size": 1024},
    "latency-large":  {"type": "latency", "size": 8192},
    "throughput-down": {"type": "download", "size": 65536},
    "throughput-up":   {"type": "upload", "size": 65536},
    "mixed":          {"type": "mixed", "size": 0},
}

def recv_exact(sock, n):
    if n == 0:
        return b''
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed")
        buf.extend(chunk)
    return bytes(buf)

def do_ping(sock, seq, size):
    payload = b'\xAA' * size
    msg = struct.pack(HEADER_FMT, MSG_PING, seq, size, 0.0) + payload
    t0 = time.perf_counter()
    sock.sendall(msg)
    hdr = recv_exact(sock, HEADER_SIZE)
    _, _, resp_size, server_proc = struct.unpack(HEADER_FMT, hdr)
    recv_exact(sock, resp_size)
    t1 = time.perf_counter()
    rtt = t1 - t0
    return rtt, server_proc

def do_download(sock, seq, size):
    body = struct.pack("!I", size)
    msg = struct.pack(HEADER_FMT, MSG_DOWNLOAD, seq, len(body), 0.0) + body
    t0 = time.perf_counter()
    sock.sendall(msg)
    hdr = recv_exact(sock, HEADER_SIZE)
    _, _, resp_size, server_proc = struct.unpack(HEADER_FMT, hdr)
    recv_exact(sock, resp_size)
    t1 = time.perf_counter()
    rtt = t1 - t0
    return rtt, server_proc, resp_size

def do_upload(sock, seq, size):
    payload = b'\xBB' * size
    msg = struct.pack(HEADER_FMT, MSG_UPLOAD, seq, size, 0.0) + payload
    t0 = time.perf_counter()
    sock.sendall(msg)
    hdr = recv_exact(sock, HEADER_SIZE)
    _, _, _, server_proc = struct.unpack(HEADER_FMT, hdr)
    t1 = time.perf_counter()
    rtt = t1 - t0
    return rtt, server_proc, size

def percentile(data, p):
    sorted_data = sorted(data)
    idx = (len(sorted_data) - 1) * p / 100
    lower = int(idx)
    upper = lower + 1
    if upper >= len(sorted_data):
        return sorted_data[-1]
    weight = idx - lower
    return sorted_data[lower] * (1 - weight) + sorted_data[upper] * weight

def report_latency(rtts_ms, server_ms, network_ms, label, payload_size=None):
    if not rtts_ms:
        print(f"  {label}: no data")
        return
    size_str = f", {payload_size}B" if payload_size else ""
    print(f"\n  {label} ({len(rtts_ms)} samples{size_str}):")
    print(f"  {'':4}{'RTT':>10}{'Network':>10}{'Server':>10}")
    print(f"  {'':4}{'-'*10}{'-'*10}{'-'*10}")
    print(f"    Mean: {statistics.mean(rtts_ms):7.2f} ms {statistics.mean(network_ms):7.2f} ms {statistics.mean(server_ms):7.2f} ms")
    print(f"    P75:  {percentile(rtts_ms, 75):7.2f} ms {percentile(network_ms, 75):7.2f} ms {percentile(server_ms, 75):7.2f} ms")
    print(f"    P95:  {percentile(rtts_ms, 95):7.2f} ms {percentile(network_ms, 95):7.2f} ms {percentile(server_ms, 95):7.2f} ms")
    print(f"    P99:  {percentile(rtts_ms, 99):7.2f} ms {percentile(network_ms, 99):7.2f} ms {percentile(server_ms, 99):7.2f} ms")
    print(f"    Min:  {min(rtts_ms):7.2f} ms {min(network_ms):7.2f} ms {min(server_ms):7.2f} ms")
    print(f"    Max:  {max(rtts_ms):7.2f} ms {max(network_ms):7.2f} ms {max(server_ms):7.2f} ms")

def report_throughput(rtts, server_procs, bytes_list, label):
    if not rtts:
        print(f"  {label}: no data")
        return
    total_bytes = sum(bytes_list)
    total_time = sum(rtts)
    throughput_mbps = (total_bytes * 8) / (total_time * 1_000_000) if total_time > 0 else 0
    rtts_ms = [t * 1000 for t in rtts]
    server_ms = [t * 1000 for t in server_procs]
    network_ms = [r - s for r, s in zip(rtts_ms, server_ms)]
    print(f"\n  {label} ({len(rtts)} transfers, {total_bytes/(1024*1024):.1f} MB in {total_time:.2f}s):")
    print(f"    Throughput: {throughput_mbps:.2f} Mbps")
    print(f"  {'':4}{'RTT':>10}{'Network':>10}{'Server':>10}")
    print(f"  {'':4}{'-'*10}{'-'*10}{'-'*10}")
    print(f"    Mean: {statistics.mean(rtts_ms):7.2f} ms {statistics.mean(network_ms):7.2f} ms {statistics.mean(server_ms):7.2f} ms")
    print(f"    P95:  {percentile(rtts_ms, 95):7.2f} ms {percentile(network_ms, 95):7.2f} ms {percentile(server_ms, 95):7.2f} ms")

def run_latency(sock, size, duration):
    rtts = []
    server_procs = []
    seq = 0
    end_time = time.time() + duration
    while time.time() < end_time:
        rtt, srv = do_ping(sock, seq, size)
        rtts.append(rtt)
        server_procs.append(srv)
        seq += 1
    return rtts, server_procs

def run_download(sock, size, duration):
    rtts = []
    server_procs = []
    bytes_list = []
    seq = 0
    end_time = time.time() + duration
    while time.time() < end_time:
        rtt, srv, b = do_download(sock, seq, size)
        rtts.append(rtt)
        server_procs.append(srv)
        bytes_list.append(b)
        seq += 1
    return rtts, server_procs, bytes_list

def run_upload(sock, size, duration):
    rtts = []
    server_procs = []
    bytes_list = []
    seq = 0
    end_time = time.time() + duration
    while time.time() < end_time:
        rtt, srv, b = do_upload(sock, seq, size)
        rtts.append(rtt)
        server_procs.append(srv)
        bytes_list.append(b)
        seq += 1
    return rtts, server_procs, bytes_list

def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9900
    profile = sys.argv[3] if len(sys.argv) > 3 else "latency-small"
    duration = int(sys.argv[4]) if len(sys.argv) > 4 else 10

    if profile not in PROFILES:
        print(f"Unknown profile '{profile}'. Available: {', '.join(PROFILES.keys())}")
        sys.exit(1)

    cfg = PROFILES[profile]

    print(f"{'=' * 62}")
    print(f"  5G Core Benchmark")
    print(f"  Server:   {host}:{port}")
    print(f"  Profile:  {profile}")
    print(f"  Duration: {duration}s")
    print(f"{'=' * 62}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.connect((host, port))
    print(f"  Connected.\n")
    print(f"  Breakdown: RTT = Network (wire time) + Server (processing)")

    if cfg["type"] == "latency":
        rtts, server_procs = run_latency(sock, cfg["size"], duration)
        rtts_ms = [t * 1000 for t in rtts]
        server_ms = [t * 1000 for t in server_procs]
        network_ms = [r - s for r, s in zip(rtts_ms, server_ms)]
        report_latency(rtts_ms, server_ms, network_ms, "Latency", cfg["size"])

    elif cfg["type"] == "download":
        rtts, server_procs, bytes_list = run_download(sock, cfg["size"], duration)
        report_throughput(rtts, server_procs, bytes_list, "Download (64KB chunks)")

    elif cfg["type"] == "upload":
        rtts, server_procs, bytes_list = run_upload(sock, cfg["size"], duration)
        report_throughput(rtts, server_procs, bytes_list, "Upload (64KB chunks)")

    elif cfg["type"] == "mixed":
        d = duration // 3 or 3

        print(f"\n  --- Phase 1: Latency (64B, {d}s) ---")
        rtts, server_procs = run_latency(sock, 64, d)
        rtts_ms = [t * 1000 for t in rtts]
        server_ms = [t * 1000 for t in server_procs]
        network_ms = [r - s for r, s in zip(rtts_ms, server_ms)]
        report_latency(rtts_ms, server_ms, network_ms, "Latency", 64)

        print(f"\n  --- Phase 2: Download (64KB, {d}s) ---")
        rtts, server_procs, bytes_list = run_download(sock, 65536, d)
        report_throughput(rtts, server_procs, bytes_list, "Download")

        print(f"\n  --- Phase 3: Upload (64KB, {d}s) ---")
        rtts, server_procs, bytes_list = run_upload(sock, 65536, d)
        report_throughput(rtts, server_procs, bytes_list, "Upload")

    sock.close()
    print(f"\n{'=' * 62}")
    print(f"  Benchmark complete.")
    print(f"{'=' * 62}")

if __name__ == "__main__":
    main()
