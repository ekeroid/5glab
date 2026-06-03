"""
Configuration — All EdgeVision client settings via environment variables.

Controls operating mode (local CPU vs edge GPU offload), camera source,
CAMARA API endpoint, frame rate, and detection thresholds.
"""

import os


MODE = os.getenv("MODE", "local")  # local | edge
EDGE_TRANSPORT = os.getenv("EDGE_TRANSPORT", "grpc")  # grpc | http
CAMERA_API_URL = os.getenv("CAMERA_API_URL", "http://camera-api:8081")
CAMARA_API_URL = os.getenv("CAMARA_API_URL", "http://camara.5glab.control.lth.se")
CAMARA_HOST_HEADER = os.getenv("CAMARA_HOST_HEADER", "")
CAMARA_PROXY_BASE_URL = os.getenv("CAMARA_PROXY_BASE_URL", "")
LOCAL_GRPC_TARGET = os.getenv("LOCAL_GRPC_TARGET", "localhost:50051")
FRAME_INTERVAL_MS = int(os.getenv("FRAME_INTERVAL_MS", "500"))
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.35"))
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "./output")
DISPLAY_WINDOW = os.getenv("DISPLAY_WINDOW", "false").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
