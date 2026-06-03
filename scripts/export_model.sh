#!/usr/bin/env bash
# Export YOLOv8n to ONNX locally (for config reference).
# The actual in-cluster export is done by the model-loader Job.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL_DIR="${SCRIPT_DIR}/../triton/model_repository/yolov8n/1"

mkdir -p "${MODEL_DIR}"

echo "Exporting YOLOv8n to ONNX..."
python3 -c "
from ultralytics import YOLO
import shutil
model = YOLO('yolov8n.pt')
path = model.export(format='onnx', opset=12, dynamic=True, imgsz=640, simplify=True)
shutil.move(path, '${MODEL_DIR}/model.onnx')
print(f'Exported to ${MODEL_DIR}/model.onnx')
"

echo "Done. Model repository:"
find "${SCRIPT_DIR}/../triton/model_repository" -type f
