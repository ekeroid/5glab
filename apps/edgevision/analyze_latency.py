"""
Analyze latency measurements from EdgeVision demo.

Usage: python3 analyze_latency.py [path_to_csv]
       If no path given, uses the most recent file in measurements/
"""

import csv
import math
import sys
from pathlib import Path


def load_csv(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            for k in ["total_ms", "server_ms", "preprocess_ms", "inference_ms", "postprocess_ms", "network_ms"]:
                row[k] = float(row[k])
            rows.append(row)
    return rows


def stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "avg": 0, "std": 0, "min": 0, "max": 0, "p50": 0, "p95": 0, "p99": 0}
    n = len(values)
    avg = sum(values) / n
    var = sum((v - avg) ** 2 for v in values) / n
    std = math.sqrt(var)
    s = sorted(values)
    return {
        "n": n,
        "avg": avg,
        "std": std,
        "min": s[0],
        "max": s[-1],
        "p50": s[n // 2],
        "p95": s[int(n * 0.95)],
        "p99": s[int(n * 0.99)],
    }


def print_stats(label: str, values: list[float]):
    s = stats(values)
    if s["n"] == 0:
        return
    print(f"  {label:25s}  avg={s['avg']:6.1f}ms  std={s['std']:5.1f}ms  "
          f"min={s['min']:5.1f}  p50={s['p50']:5.1f}  p95={s['p95']:5.1f}  p99={s['p99']:5.1f}  max={s['max']:6.1f}  n={s['n']}")


def analyze(rows: list[dict]):
    groups = {}
    for row in rows:
        key = (row["mode"], row["model"], row["transport"])
        groups.setdefault(key, []).append(row)

    for (mode, model, transport), samples in sorted(groups.items()):
        print(f"\n{'='*80}")
        print(f"  Mode: {mode}  |  Model: {model}  |  Transport: {transport}  |  Samples: {len(samples)}")
        print(f"{'='*80}")

        print_stats("End-to-end (total)", [r["total_ms"] for r in samples])
        print_stats("Network (UL + DL)", [r["network_ms"] for r in samples])
        print_stats("Server total", [r["server_ms"] for r in samples])
        print_stats("  Preprocess (decode+resize)", [r["preprocess_ms"] for r in samples])
        print_stats("  Inference (TensorRT)", [r["inference_ms"] for r in samples])
        print_stats("  Postprocess (NMS/masks)", [r["postprocess_ms"] for r in samples])


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        mdir = Path(__file__).parent / "measurements"
        csvs = sorted(mdir.glob("latency_*.csv"), key=lambda p: p.stat().st_mtime)
        if not csvs:
            print("No measurement files found in measurements/")
            sys.exit(1)
        path = str(csvs[-1])
        print(f"Using: {path}")

    rows = load_csv(path)
    if not rows:
        print("No data in file.")
        sys.exit(1)

    # Skip first 5 samples (warmup)
    rows = rows[5:]
    print(f"Total samples (after warmup skip): {len(rows)}")
    analyze(rows)


if __name__ == "__main__":
    main()
