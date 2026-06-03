"""
EdgeVision GUI — Side-by-side input/output with latency tracking and edge toggle.

Uses OpenCV highgui. Press 'e' or click the mode button to toggle between
local CPU and edge GPU offload. Shows full CAMARA lifecycle event log.
Press 'q' or ESC to quit.
"""

import io
import threading
import time
from collections import deque

import cv2
import numpy as np
from PIL import Image

import camara
import config
import detector_local
import detector_remote

PANEL_W = 640
PANEL_H = 400
BAR_H = 50
LOG_H = 200
CHART_H = 60
WINDOW_W = PANEL_W * 2
WINDOW_H = BAR_H + PANEL_H + LOG_H + CHART_H


class EdgeVisionGUI:
    def __init__(self):
        self._mode = "local"
        self._edge_instance_id = None
        self._edge_endpoint = None
        self._edge_setup_in_progress = False

        self._event_log: list[tuple[str, str, str]] = []  # (timestamp, status_icon, message)
        self._latency_history = deque(maxlen=200)
        self._frame_count = 0
        self._fps = 0.0
        self._last_fps_time = time.time()
        self._fps_count = 0
        self._running = True

        self._latest_input = np.zeros((PANEL_H, PANEL_W, 3), dtype=np.uint8)
        self._latest_output = np.zeros((PANEL_H, PANEL_W, 3), dtype=np.uint8)
        self._latest_latency = 0.0
        self._latest_server_latency = 0.0
        self._latest_det_count = 0
        self._lock = threading.Lock()

    def _log_event(self, icon: str, msg: str):
        ts = time.strftime("%H:%M:%S")
        self._event_log.append((ts, icon, msg))
        print(f"[{ts}] {icon} {msg}")

    def _toggle_mode(self):
        if self._edge_setup_in_progress:
            return

        if self._mode == "local":
            self._mode = "edge"
            self._edge_setup_in_progress = True
            self._event_log.clear()
            self._log_event(">>", "EDGE OFFLOAD INITIATED")
            threading.Thread(target=self._setup_edge, daemon=True).start()
        else:
            self._mode = "local"
            self._log_event("<<", "Switched to LOCAL CPU mode")
            if self._edge_instance_id:
                threading.Thread(target=self._teardown_edge, daemon=True).start()

    def _setup_edge(self):
        try:
            # Step 1: Discover
            self._log_event("..", f"Discovering edge zones at {config.CAMARA_API_URL}")
            t0 = time.perf_counter()
            zone = camara.discover_zone()
            zone_id = zone["edgeCloudZoneId"]
            zone_name = zone.get("edgeCloudZoneName", zone_id)
            gpu = zone.get("capabilities", {}).get("gpuModel", "unknown")
            ms = (time.perf_counter() - t0) * 1000
            self._log_event("OK", f"Zone found: {zone_name} ({gpu}) [{ms:.0f}ms]")

            # Step 2: Register app
            self._log_event("..", "Registering app manifest (YOLOv8 + Triton + GPU)")
            t0 = time.perf_counter()
            app_id = camara.register_app()
            ms = (time.perf_counter() - t0) * 1000
            self._log_event("OK", f"App registered: {app_id[:8]}... [{ms:.0f}ms]")

            # Step 3: Instantiate
            self._log_event("..", f"Instantiating on zone {zone_name}...")
            t0 = time.perf_counter()
            instance_id = camara.instantiate_app(app_id, zone_id)
            self._edge_instance_id = instance_id
            ms = (time.perf_counter() - t0) * 1000
            self._log_event("OK", f"Instance created: {instance_id[:8]}... [{ms:.0f}ms]")

            # Step 4: Wait for ready
            self._log_event("..", "Waiting for GPU instance to become ready...")
            t0 = time.perf_counter()
            poll_count = 0
            for _ in range(60):
                if self._mode != "edge":
                    self._log_event("--", "Cancelled by user")
                    return
                status = camara.get_instance_status(instance_id)
                poll_count += 1
                if status == "ready":
                    break
                elif status == "failed":
                    raise RuntimeError("Instance failed to start")
                time.sleep(5)
            else:
                raise RuntimeError("Timeout waiting for instance (300s)")
            ms = (time.perf_counter() - t0) * 1000
            self._log_event("OK", f"Instance READY ({poll_count} polls, {ms/1000:.1f}s)")

            # Step 5: Get endpoint
            self._log_event("..", "Discovering inference endpoint...")
            t0 = time.perf_counter()
            endpoint = camara.get_endpoint(instance_id)
            self._edge_endpoint = endpoint
            ms = (time.perf_counter() - t0) * 1000
            self._log_event("OK", f"Endpoint: {endpoint} [{ms:.0f}ms]")

            # Done
            self._edge_setup_in_progress = False
            self._log_event("**", "EDGE OFFLOAD ACTIVE — inference via 5G MEC")

        except Exception as e:
            self._log_event("!!", f"FAILED: {e}")
            self._edge_setup_in_progress = False
            self._mode = "local"

    def _teardown_edge(self):
        if self._edge_instance_id:
            self._log_event("..", f"Terminating instance {self._edge_instance_id[:8]}...")
            try:
                camara.terminate_app(self._edge_instance_id)
                self._log_event("OK", "Instance terminated")
            except Exception as e:
                self._log_event("!!", f"Terminate failed: {e}")
            self._edge_instance_id = None
            self._edge_endpoint = None

    def _capture_loop(self):
        import requests

        while self._running:
            loop_start = time.perf_counter()

            try:
                resp = requests.get(f"{config.CAMERA_API_URL}/frame", timeout=10)
                resp.raise_for_status()
                jpeg_bytes = resp.content
            except Exception:
                time.sleep(1)
                continue

            img_array = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            input_frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
            if input_frame is None:
                continue
            input_resized = cv2.resize(input_frame, (PANEL_W, PANEL_H))

            infer_start = time.perf_counter()
            server_ms = 0.0
            try:
                if self._mode == "edge" and self._edge_endpoint and not self._edge_setup_in_progress:
                    detections, server_ms = detector_remote.detect(jpeg_bytes, self._edge_endpoint)
                else:
                    detections = detector_local.detect(jpeg_bytes)
            except Exception:
                detections = []
            infer_ms = (time.perf_counter() - infer_start) * 1000

            output_frame = self._annotate(input_frame.copy(), detections, infer_ms)
            output_resized = cv2.resize(output_frame, (PANEL_W, PANEL_H))

            with self._lock:
                self._latest_input = input_resized
                self._latest_output = output_resized
                self._latest_latency = infer_ms
                self._latest_server_latency = server_ms
                self._latest_det_count = len(detections)
                self._latency_history.append(infer_ms)
                self._frame_count += 1
                self._fps_count += 1

            now = time.time()
            if now - self._last_fps_time >= 1.0:
                self._fps = self._fps_count / (now - self._last_fps_time)
                self._fps_count = 0
                self._last_fps_time = now

            elapsed = (time.perf_counter() - loop_start) * 1000
            sleep_ms = max(0, config.FRAME_INTERVAL_MS - elapsed)
            if sleep_ms > 0 and self._running:
                time.sleep(sleep_ms / 1000)

    def _annotate(self, frame: np.ndarray, detections: list[dict], latency_ms: float) -> np.ndarray:
        for det in detections:
            label = det["label"]
            conf = det["confidence"]
            x1, y1, x2, y2 = [int(v) for v in det["bbox"]]

            hue = hash(label) % 360
            hsv = np.array([[[hue // 2, 200, 230]]], dtype=np.uint8)
            bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0][0]
            colour = (int(bgr[0]), int(bgr[1]), int(bgr[2]))

            cv2.rectangle(frame, (x1, y1), (x2, y2), colour, 2)
            text = f"{label} {conf:.0%}"
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(frame, (x1, y1 - th - 6), (x1 + tw + 4, y1), colour, -1)
            cv2.putText(frame, text, (x1 + 2, y1 - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        return frame

    def _draw_top_bar(self, canvas: np.ndarray):
        if self._mode == "edge" and not self._edge_setup_in_progress:
            badge_colour = (0, 180, 0)
            badge_text = "EDGE GPU"
        elif self._edge_setup_in_progress:
            badge_colour = (0, 140, 180)
            badge_text = "CONNECTING"
        else:
            badge_colour = (0, 120, 200)
            badge_text = "LOCAL CPU"

        # Button with border
        cv2.rectangle(canvas, (8, 8), (162, 44), (200, 200, 200), 1)
        cv2.rectangle(canvas, (10, 10), (160, 42), badge_colour, -1)
        cv2.putText(canvas, badge_text, (18, 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

        # Latency with breakdown
        total_ms = self._latest_latency
        server_ms = self._latest_server_latency
        is_edge_active = self._mode == "edge" and not self._edge_setup_in_progress
        net_ms = max(0, total_ms - server_ms) if is_edge_active and server_ms > 0 else 0

        lat_colour = (0, 255, 100) if is_edge_active else (0, 180, 255)
        if is_edge_active and server_ms > 0:
            lat_text = f"{total_ms:.0f}ms total (GPU:{server_ms:.0f} + net:{net_ms:.0f})"
        else:
            lat_text = f"{total_ms:.0f} ms"
        cv2.putText(canvas, lat_text, (180, 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, lat_colour, 1)

        # Stats
        history = list(self._latency_history)
        if history:
            avg = sum(history) / len(history)
            stats = f"FPS: {self._fps:.1f}  |  Avg: {avg:.0f}ms  |  Frames: {self._frame_count}"
        else:
            stats = "Starting..."
        cv2.putText(canvas, stats, (580, 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)

        # Controls hint
        cv2.putText(canvas, "[E] Toggle  [Q] Quit", (WINDOW_W - 220, 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (100, 100, 100), 1)

    def _draw_event_log(self, canvas: np.ndarray):
        log_y = BAR_H + PANEL_H
        # Background
        cv2.rectangle(canvas, (0, log_y), (WINDOW_W, log_y + LOG_H), (20, 20, 20), -1)
        # Header
        cv2.rectangle(canvas, (0, log_y), (WINDOW_W, log_y + 20), (40, 40, 40), -1)
        cv2.putText(canvas, "CAMARA MEC LIFECYCLE", (10, log_y + 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)

        # Events
        visible_events = self._event_log[-(LOG_H // 18):]
        for i, (ts, icon, msg) in enumerate(visible_events):
            y = log_y + 34 + i * 18

            # Icon colour
            if icon == "OK":
                icon_colour = (0, 220, 0)
                icon_text = "[OK]"
            elif icon == "!!":
                icon_colour = (0, 0, 255)
                icon_text = "[!!]"
            elif icon == "..":
                icon_colour = (0, 200, 255)
                icon_text = "[..]"
            elif icon == ">>":
                icon_colour = (255, 200, 0)
                icon_text = "[>>]"
            elif icon == "<<":
                icon_colour = (200, 150, 0)
                icon_text = "[<<]"
            elif icon == "**":
                icon_colour = (0, 255, 150)
                icon_text = "[**]"
            else:
                icon_colour = (150, 150, 150)
                icon_text = f"[{icon}]"

            cv2.putText(canvas, ts, (10, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (100, 100, 100), 1)
            cv2.putText(canvas, icon_text, (80, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, icon_colour, 1)
            cv2.putText(canvas, msg[:100], (120, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (200, 200, 200), 1)

    def _draw_latency_chart(self, canvas: np.ndarray):
        chart_y = BAR_H + PANEL_H + LOG_H
        cv2.line(canvas, (0, chart_y), (WINDOW_W, chart_y), (60, 60, 60), 1)

        history = list(self._latency_history)
        if len(history) < 2:
            return

        max_val = max(history) or 1
        n = len(history)
        bar_w = max(1, WINDOW_W // n)

        for i, val in enumerate(history):
            bar_h = int((val / max_val) * (CHART_H - 10))
            x = i * bar_w
            y_top = chart_y + CHART_H - bar_h - 3
            y_bot = chart_y + CHART_H - 3

            colour = (0, 180, 80) if (self._mode == "edge" and not self._edge_setup_in_progress) else (0, 140, 200)
            cv2.rectangle(canvas, (x, y_top), (x + bar_w - 1, y_bot), colour, -1)

        cv2.putText(canvas, f"{max_val:.0f}ms", (WINDOW_W - 60, chart_y + 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.3, (120, 120, 120), 1)

    def _render(self) -> np.ndarray:
        canvas = np.zeros((WINDOW_H, WINDOW_W, 3), dtype=np.uint8)
        canvas[:] = (30, 30, 30)

        with self._lock:
            input_frame = self._latest_input.copy()
            output_frame = self._latest_output.copy()

        # Panel labels
        cv2.putText(canvas, "INPUT (Camera)", (PANEL_W // 2 - 60, BAR_H - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (100, 100, 100), 1)
        cv2.putText(canvas, "OUTPUT (Detections)", (PANEL_W + PANEL_W // 2 - 75, BAR_H - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (100, 100, 100), 1)

        # Place panels
        canvas[BAR_H:BAR_H + PANEL_H, 0:PANEL_W] = input_frame
        canvas[BAR_H:BAR_H + PANEL_H, PANEL_W:PANEL_W * 2] = output_frame

        # Divider
        cv2.line(canvas, (PANEL_W, BAR_H), (PANEL_W, BAR_H + PANEL_H), (80, 80, 80), 2)

        self._draw_top_bar(canvas)
        self._draw_event_log(canvas)
        self._draw_latency_chart(canvas)

        return canvas

    def _on_mouse(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            if 8 <= x <= 162 and 8 <= y <= 44:
                self._toggle_mode()

    def run(self):
        threading.Thread(target=self._capture_loop, daemon=True).start()

        cv2.namedWindow("EdgeVision", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("EdgeVision", WINDOW_W, WINDOW_H)
        cv2.setMouseCallback("EdgeVision", self._on_mouse)

        while self._running:
            frame = self._render()
            cv2.imshow("EdgeVision", frame)

            key = cv2.waitKey(30) & 0xFF
            if key == ord('q') or key == 27:
                self._running = False
            elif key == ord('e') or key == ord('E'):
                self._toggle_mode()

        if self._edge_instance_id:
            self._teardown_edge()
        cv2.destroyAllWindows()


def main():
    print("EdgeVision GUI — CAMARA Edge Compute Demo")
    print(f"  Camera: {config.CAMERA_API_URL}")
    print(f"  CAMARA API: {config.CAMARA_API_URL}")
    print(f"  Frame interval: {config.FRAME_INTERVAL_MS}ms")
    print()
    print("Controls:")
    print("  [E] or click button — Toggle LOCAL/EDGE mode")
    print("  [Q] or ESC — Quit")
    print()

    gui = EdgeVisionGUI()
    gui.run()


if __name__ == "__main__":
    main()
