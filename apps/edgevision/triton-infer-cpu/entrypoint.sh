#!/bin/bash
set -e

MODEL_DIR="${MODEL_DIR:-/opt/models}"
mkdir -p "$MODEL_DIR"

# Export YOLOv8n to ONNX if not present
if [ ! -f "$MODEL_DIR/yolov8n.onnx" ]; then
    echo "[entrypoint] Exporting YOLOv8n to ONNX (first boot, ~30s)..."
    python3 -c "
from ultralytics import YOLO
model = YOLO('yolov8n.pt')
model.export(format='onnx', imgsz=640, simplify=True)
import shutil
shutil.move('yolov8n.onnx', '$MODEL_DIR/yolov8n.onnx')
"
fi

echo "[entrypoint] Starting CPU inference sidecar on :8080 + :50051..."
exec uvicorn main:app --host 0.0.0.0 --port 8080
