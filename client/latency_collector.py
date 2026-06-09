"""
Latency data collector — zero-overhead async file writer.

Buffers timing samples in memory and flushes to CSV on a background thread.
No I/O on the inference hot path.
"""

import csv
import os
import threading
import time
from collections import deque
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent.parent / "measurements"
OUTPUT_DIR.mkdir(exist_ok=True)

_buffer: deque = deque()
_lock = threading.Lock()
from typing import Optional
_writer_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_session_file: str = ""


def start(session_name: str = ""):
    global _writer_thread, _session_file
    ts = time.strftime("%Y%m%d_%H%M%S")
    name = f"latency_{ts}_{session_name}.csv" if session_name else f"latency_{ts}.csv"
    _session_file = str(OUTPUT_DIR / name)
    _stop_event.clear()
    _writer_thread = threading.Thread(target=_flush_loop, daemon=True)
    _writer_thread.start()


def record(timing: dict, mode: str, model: str):
    """Record a timing sample. Called from inference thread — must be fast."""
    sample = {
        "timestamp": time.time(),
        "mode": mode,
        "model": model,
        "transport": timing.get("transport", ""),
        "total_ms": timing.get("total_ms", 0),
        "server_ms": timing.get("server_ms", 0),
        "preprocess_ms": timing.get("preprocess_ms", 0),
        "inference_ms": timing.get("inference_ms", 0),
        "postprocess_ms": timing.get("postprocess_ms", 0),
        "network_ms": timing.get("network_ms", 0),
    }
    _buffer.append(sample)


def stop():
    _stop_event.set()
    if _writer_thread:
        _writer_thread.join(timeout=2)


def _flush_loop():
    fields = ["timestamp", "mode", "model", "transport",
              "total_ms", "server_ms", "preprocess_ms",
              "inference_ms", "postprocess_ms", "network_ms"]

    with open(_session_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        f.flush()

        while not _stop_event.is_set():
            batch = []
            while _buffer:
                try:
                    batch.append(_buffer.popleft())
                except IndexError:
                    break
            if batch:
                writer.writerows(batch)
                f.flush()
            _stop_event.wait(timeout=1.0)

        # Final flush
        while _buffer:
            try:
                writer.writerow(_buffer.popleft())
            except IndexError:
                break


def get_output_path() -> str:
    return _session_file
