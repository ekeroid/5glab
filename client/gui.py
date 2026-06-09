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
import detector_local_seg
import detector_remote
import latency_collector
import modem

PANEL_W = 640
PANEL_H = 400
BAR_H = 50
LOG_H = 200
CHART_H = 80
BREAKDOWN_H = 40
RADIO_H = 30
WINDOW_W = PANEL_W * 2
WINDOW_H = BAR_H + PANEL_H + LOG_H + CHART_H + BREAKDOWN_H + RADIO_H


class EdgeVisionGUI:
    def __init__(self):
        self._mode = "local"
        self._model_mode = "detect"  # "detect" or "seg"
        self._edge_instance_id = None
        self._edge_endpoint = None
        self._edge_grpc_endpoint = None
        self._edge_http_endpoint = None
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
        self._latest_timing: dict = {}
        self._latest_det_count = 0
        self._lock = threading.Lock()

        latency_collector.start("edgevision")

    def _log_event(self, icon: str, msg: str):
        ts = time.strftime("%H:%M:%S")
        self._event_log.append((ts, icon, msg))
        print(f"[{ts}] {icon} {msg}")

    def _toggle_transport(self):
        if config.EDGE_TRANSPORT == "grpc":
            config.EDGE_TRANSPORT = "http"
            self._edge_endpoint = self._edge_http_endpoint
        else:
            config.EDGE_TRANSPORT = "grpc"
            self._edge_endpoint = self._edge_grpc_endpoint
        detector_remote._reset_channel()
        self._log_event(">>", f"Transport: {config.EDGE_TRANSPORT.upper()} → {self._edge_endpoint}")

    def _toggle_model(self):
        if self._model_mode == "detect":
            self._model_mode = "seg"
            self._log_event(">>", "Model: YOLOv8x-seg (instance segmentation)")
        else:
            self._model_mode = "detect"
            self._log_event(">>", "Model: YOLOv8n (detection)")

    def _toggle_mode(self):
        if self._edge_setup_in_progress:
            return

        if self._mode == "local":
            if self._edge_endpoint:
                self._mode = "edge"
                with self._lock:
                    self._latest_timing = {}
                    self._latest_latency = 0.0
                self._log_event(">>", "Switched to EDGE GPU mode")
            else:
                self._mode = "edge"
                self._edge_setup_in_progress = True
                self._event_log.clear()
                self._log_event(">>", "EDGE OFFLOAD INITIATED")
                threading.Thread(target=self._setup_edge, daemon=True).start()
        else:
            self._mode = "local"
            with self._lock:
                self._latest_timing = {}
                self._latest_latency = 0.0
            self._log_event("<<", "Switched to LOCAL CPU mode")

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
            self._log_event("..", "Waiting for GPU instance...")
            t0 = time.perf_counter()
            last_phase = ""
            phase_start = t0
            for _ in range(60):
                if self._mode != "edge":
                    self._log_event("--", "Cancelled by user")
                    return
                detail = camara.get_instance_status(instance_id)
                phase = detail.get("phase", "")
                if phase != last_phase:
                    if last_phase:
                        phase_ms = (time.perf_counter() - phase_start) * 1000
                        self._log_event("OK", f"  {last_phase}: {phase_ms:.0f}ms")
                    last_phase = phase
                    phase_start = time.perf_counter()
                    self._log_event("..", f"  {phase}: {detail.get('message', '')}")
                if detail["status"] == "ready":
                    phase_ms = (time.perf_counter() - phase_start) * 1000
                    self._log_event("OK", f"  {phase}: {phase_ms:.0f}ms")
                    break
                elif detail["status"] == "failed":
                    raise RuntimeError(f"Instance failed: {detail.get('message')}")
                time.sleep(3)
            else:
                raise RuntimeError("Timeout waiting for instance (180s)")
            total_s = time.perf_counter() - t0
            self._log_event("OK", f"Instance READY in {total_s:.1f}s")

            # Step 5: Get endpoint
            self._log_event("..", "Discovering inference endpoint...")
            t0 = time.perf_counter()
            grpc_ep, http_ep = camara.get_endpoints(instance_id)
            self._edge_grpc_endpoint = grpc_ep
            self._edge_http_endpoint = http_ep
            if config.EDGE_TRANSPORT == "grpc" and grpc_ep:
                self._edge_endpoint = grpc_ep
            else:
                config.EDGE_TRANSPORT = "http"
                self._edge_endpoint = http_ep
            ms = (time.perf_counter() - t0) * 1000
            self._log_event("OK", f"gRPC: {grpc_ep or 'N/A'}  HTTP: {http_ep} [{ms:.0f}ms]")

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
            timing = {}
            try:
                if self._mode == "edge" and self._edge_endpoint and not self._edge_setup_in_progress:
                    try:
                        detections, timing = detector_remote.detect(
                            jpeg_bytes, self._edge_endpoint, model=self._model_mode)
                    except RuntimeError:
                        if config.EDGE_TRANSPORT == "grpc" and self._edge_http_endpoint:
                            config.EDGE_TRANSPORT = "http"
                            self._edge_endpoint = self._edge_http_endpoint
                            detector_remote._reset_channel()
                            self._log_event(">>", f"gRPC unreachable, falling back to HTTP")
                            detections, timing = detector_remote.detect(
                                jpeg_bytes, self._edge_endpoint, model=self._model_mode)
                        else:
                            raise
                elif self._model_mode == "seg":
                    detections, timing = detector_local_seg.detect(jpeg_bytes)
                else:
                    t0 = time.perf_counter()
                    detections = detector_local.detect(jpeg_bytes)
                    local_ms = (time.perf_counter() - t0) * 1000
                    timing = {"total_ms": local_ms, "inference_ms": local_ms, "transport": "local"}
            except Exception:
                detections = []
            infer_ms = timing.get("total_ms", (time.perf_counter() - infer_start) * 1000)

            output_frame = self._annotate(input_frame.copy(), detections, infer_ms)
            output_resized = cv2.resize(output_frame, (PANEL_W, PANEL_H))

            latency_collector.record(timing, self._mode, self._model_mode)

            with self._lock:
                self._latest_input = input_resized
                self._latest_output = output_resized
                self._latest_latency = infer_ms
                self._latest_timing = timing
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
        overlay = frame.copy()
        for det in detections:
            label = det["label"]
            conf = det["confidence"]
            x1, y1, x2, y2 = [int(v) for v in det["bbox"]]

            hue = hash(label) % 360
            hsv = np.array([[[hue // 2, 200, 230]]], dtype=np.uint8)
            bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0][0]
            colour = (int(bgr[0]), int(bgr[1]), int(bgr[2]))

            # Draw mask polygon if available
            mask_poly = det.get("mask_polygon")
            if mask_poly and len(mask_poly) > 2:
                pts = np.array(mask_poly, dtype=np.int32).reshape((-1, 1, 2))
                cv2.fillPoly(overlay, [pts], colour)
                cv2.polylines(frame, [pts], True, colour, 2)
            else:
                cv2.rectangle(frame, (x1, y1), (x2, y2), colour, 2)

            text = f"{label} {conf:.0%}"
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(frame, (x1, y1 - th - 6), (x1 + tw + 4, y1), colour, -1)
            cv2.putText(frame, text, (x1 + 2, y1 - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

        # Blend mask overlay at 40% opacity
        cv2.addWeighted(overlay, 0.4, frame, 0.6, 0, frame)
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

        # Transport badge
        if self._mode == "edge" and not self._edge_setup_in_progress:
            transport = config.EDGE_TRANSPORT.upper()
        elif self._edge_setup_in_progress:
            transport = ""
        else:
            transport = "LOCAL"
        tx = 170
        cv2.putText(canvas, transport, (tx, 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)

        # Total latency
        total_ms = self._latest_latency
        lat_colour = (0, 255, 100) if (self._mode == "edge" and not self._edge_setup_in_progress) else (0, 180, 255)
        cv2.putText(canvas, f"{total_ms:.0f}ms", (230, 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, lat_colour, 2)

        # Stats
        history = list(self._latency_history)
        if history:
            avg = sum(history) / len(history)
            stats = f"FPS: {self._fps:.1f}  |  Avg: {avg:.0f}ms  |  #{self._frame_count}"
        else:
            stats = "Starting..."
        cv2.putText(canvas, stats, (580, 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)

        # Model badge
        model_text = "SEG" if self._model_mode == "seg" else "DET"
        model_colour = (180, 0, 180) if self._model_mode == "seg" else (150, 150, 150)
        cv2.putText(canvas, model_text, (310, 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, model_colour, 1)

        # Controls hint
        cv2.putText(canvas, "[E] Mode [T] Transport [M] Model [Q] Quit", (WINDOW_W - 390, 34),
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
        max_visible = (LOG_H - 34) // 18
        visible_events = self._event_log[-max_visible:]
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

    def _draw_breakdown_bar(self, canvas: np.ndarray):
        """Draw a stacked horizontal bar showing latency breakdown."""
        bar_y = BAR_H + PANEL_H + LOG_H
        cv2.rectangle(canvas, (0, bar_y), (WINDOW_W, bar_y + BREAKDOWN_H), (25, 25, 25), -1)
        cv2.line(canvas, (0, bar_y), (WINDOW_W, bar_y), (60, 60, 60), 1)

        timing = self._latest_timing
        if not timing:
            return

        total = timing.get("total_ms", 0)
        if total <= 0:
            return

        # Segments: network (orange), preprocess (blue), inference (green), postprocess (purple)
        segments = []
        is_edge = self._mode == "edge" and not self._edge_setup_in_progress

        if is_edge:
            segments = [
                ("Network", timing.get("network_ms", 0), (0, 140, 255)),
                ("Preprocess", timing.get("preprocess_ms", 0), (200, 150, 0)),
                ("GPU Infer", timing.get("inference_ms", 0), (0, 200, 0)),
                ("Postprocess", timing.get("postprocess_ms", 0), (180, 0, 180)),
            ]
        else:
            segments = [
                ("CPU Infer", timing.get("inference_ms", 0), (0, 140, 200)),
            ]

        # Draw stacked bar
        margin = 10
        bar_width = WINDOW_W - 200 - margin * 2
        bar_height = 18
        bx = margin
        by = bar_y + (BREAKDOWN_H - bar_height) // 2

        # Background
        cv2.rectangle(canvas, (bx, by), (bx + bar_width, by + bar_height), (50, 50, 50), -1)

        # Draw segments
        x_offset = bx
        for name, ms, colour in segments:
            if ms <= 0:
                continue
            seg_w = max(1, int((ms / total) * bar_width))
            cv2.rectangle(canvas, (x_offset, by), (x_offset + seg_w, by + bar_height), colour, -1)
            if seg_w > 40:
                cv2.putText(canvas, f"{ms:.0f}", (x_offset + 3, by + 13),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.3, (255, 255, 255), 1)
            x_offset += seg_w

        # Legend on the right
        lx = WINDOW_W - 180
        ly = bar_y + 12
        for name, ms, colour in segments:
            if ms <= 0:
                continue
            cv2.rectangle(canvas, (lx, ly - 6), (lx + 8, ly + 2), colour, -1)
            cv2.putText(canvas, f"{name}: {ms:.1f}ms", (lx + 12, ly),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.3, (180, 180, 180), 1)
            ly += 14

    def _draw_latency_chart(self, canvas: np.ndarray):
        chart_y = BAR_H + PANEL_H + LOG_H + BREAKDOWN_H
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

    def _draw_radio_bar(self, canvas: np.ndarray):
        """Draw radio metrics strip at the bottom."""
        radio_y = BAR_H + PANEL_H + LOG_H + CHART_H + BREAKDOWN_H
        cv2.rectangle(canvas, (0, radio_y), (WINDOW_W, radio_y + RADIO_H), (15, 15, 15), -1)
        cv2.line(canvas, (0, radio_y), (WINDOW_W, radio_y), (60, 60, 60), 1)

        rd = modem.get_radio_data()
        if not rd:
            cv2.putText(canvas, "RADIO: waiting for modem...", (10, radio_y + 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (80, 80, 80), 1)
            return

        # Connection type badge
        conntype = rd.get("conntype", "N/A")
        cv2.putText(canvas, conntype, (10, radio_y + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 220, 180), 1)

        # Band
        band = rd.get("band", "")
        bw = rd.get("bandwidth", "")
        band_text = f"{band} {bw}MHz" if bw != "N/A" else band
        cv2.putText(canvas, band_text, (130, radio_y + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (180, 180, 180), 1)

        # SINR with colour coding
        sinr = rd.get("sinr", "N/A")
        if isinstance(sinr, (int, float)):
            if sinr >= 20:
                sinr_colour = (0, 220, 0)
            elif sinr >= 10:
                sinr_colour = (0, 200, 200)
            else:
                sinr_colour = (0, 100, 255)
            sinr_text = f"SINR: {sinr} dB"
        else:
            sinr_colour = (100, 100, 100)
            sinr_text = f"SINR: {sinr}"
        cv2.putText(canvas, sinr_text, (310, radio_y + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, sinr_colour, 1)

        # RSRP
        rsrp = rd.get("rsrp", "N/A")
        if isinstance(rsrp, (int, float)):
            if rsrp >= -80:
                rsrp_colour = (0, 220, 0)
            elif rsrp >= -100:
                rsrp_colour = (0, 200, 200)
            else:
                rsrp_colour = (0, 100, 255)
            rsrp_text = f"RSRP: {rsrp} dBm"
        else:
            rsrp_colour = (100, 100, 100)
            rsrp_text = f"RSRP: {rsrp}"
        cv2.putText(canvas, rsrp_text, (460, radio_y + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, rsrp_colour, 1)

        # RSRQ
        rsrq = rd.get("rsrq", "N/A")
        rsrq_text = f"RSRQ: {rsrq} dB" if isinstance(rsrq, (int, float)) else f"RSRQ: {rsrq}"
        cv2.putText(canvas, rsrq_text, (620, radio_y + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)

        # PCI + Cell ID
        pci = rd.get("pci", "N/A")
        cellid = rd.get("cellid", "N/A")
        cv2.putText(canvas, f"PCI:{pci} Cell:{cellid}", (760, radio_y + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (100, 100, 100), 1)

        # Temp
        temp = rd.get("temp", "N/A")
        if isinstance(temp, (int, float)):
            cv2.putText(canvas, f"{temp}C", (WINDOW_W - 50, radio_y + 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (100, 100, 100), 1)

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
        self._draw_breakdown_bar(canvas)
        self._draw_latency_chart(canvas)
        self._draw_radio_bar(canvas)

        return canvas

    def _on_mouse(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            if 8 <= x <= 162 and 8 <= y <= 44:
                self._toggle_mode()

    def run(self):
        modem.start()
        threading.Thread(target=self._capture_loop, daemon=True).start()

        cv2.namedWindow("EdgeVision", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("EdgeVision", WINDOW_W, WINDOW_H)
        cv2.setMouseCallback("EdgeVision", self._on_mouse)

        while self._running:
            key = cv2.waitKey(30) & 0xFF
            if key == ord('q') or key == 27:
                self._running = False
            elif key == ord('e') or key == ord('E'):
                self._toggle_mode()
            elif key == ord('t') or key == ord('T'):
                self._toggle_transport()
            elif key == ord('m') or key == ord('M'):
                self._toggle_model()

            frame = self._render()
            cv2.imshow("EdgeVision", frame)

        latency_collector.stop()
        print(f"\nLatency data saved: {latency_collector.get_output_path()}")
        modem.stop()
        if self._edge_instance_id:
            self._teardown_edge()
        cv2.destroyAllWindows()


def main():
    import sys
    auto_edge = "--edge" in sys.argv

    print("EdgeVision GUI — CAMARA Edge Compute Demo")
    print(f"  Camera: {config.CAMERA_API_URL}")
    print(f"  CAMARA API: {config.CAMARA_API_URL}")
    print(f"  Frame interval: {config.FRAME_INTERVAL_MS}ms")
    print(f"  Auto-edge: {auto_edge}")
    print()
    print("Controls:")
    print("  [E] or click button — Toggle LOCAL/EDGE mode")
    print("  [M] — Toggle model: YOLOv8n detect / YOLOv8x-seg")
    print("  [Q] or ESC — Quit")
    print()

    gui = EdgeVisionGUI()
    if auto_edge:
        gui._toggle_mode()
    gui.run()


if __name__ == "__main__":
    main()
