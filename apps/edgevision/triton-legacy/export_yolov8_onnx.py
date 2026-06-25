"""
YOLOv8n ONNX export + pipeline setup — Run by the model-loader Job inside k8s.

Downloads YOLOv8n weights, exports to ONNX format with dynamic batching,
sets up the preprocess Python backend model and ensemble pipeline config,
and places everything in the Triton model repository on the shared PVC.
"""

import shutil
from pathlib import Path

from ultralytics import YOLO

MODELS_ROOT = Path("/models")
WORKSPACE = Path("/workspace")


def main():
    print("=" * 50)
    print("EdgeVision Model Loader — YOLOv8n + Pipeline Setup")
    print("=" * 50)

    # --- yolov8n ONNX model ---
    model_dir = MODELS_ROOT / "yolov8n" / "1"
    model_dir.mkdir(parents=True, exist_ok=True)

    print("\nLoading YOLOv8n...")
    model = YOLO("yolov8n.pt")

    print("Exporting to ONNX (opset=12, dynamic=True, imgsz=640)...")
    export_path = model.export(
        format="onnx",
        opset=12,
        dynamic=True,
        imgsz=640,
        simplify=True,
    )

    dest = model_dir / "model.onnx"
    shutil.move(export_path, str(dest))
    print(f"  Model saved: {dest} ({dest.stat().st_size / 1024 / 1024:.1f} MB)")

    # Copy yolov8n config
    src = WORKSPACE / "config.pbtxt"
    if src.exists():
        shutil.copy2(str(src), str(MODELS_ROOT / "yolov8n" / "config.pbtxt"))

    # --- preprocess Python backend ---
    preprocess_dir = MODELS_ROOT / "preprocess" / "1"
    preprocess_dir.mkdir(parents=True, exist_ok=True)

    src = WORKSPACE / "preprocess_config.pbtxt"
    if src.exists():
        shutil.copy2(str(src), str(MODELS_ROOT / "preprocess" / "config.pbtxt"))

    src = WORKSPACE / "preprocess_model.py"
    if src.exists():
        shutil.copy2(str(src), str(preprocess_dir / "model.py"))

    print("  Preprocess model installed")

    # --- yolov8n_pipeline ensemble ---
    pipeline_dir = MODELS_ROOT / "yolov8n_pipeline" / "1"
    pipeline_dir.mkdir(parents=True, exist_ok=True)

    src = WORKSPACE / "pipeline_config.pbtxt"
    if src.exists():
        shutil.copy2(str(src), str(MODELS_ROOT / "yolov8n_pipeline" / "config.pbtxt"))

    print("  Pipeline ensemble installed")

    print("\n✓ Model repository ready for Triton")
    print("  /models/yolov8n/config.pbtxt")
    print("  /models/yolov8n/1/model.onnx")
    print("  /models/preprocess/config.pbtxt")
    print("  /models/preprocess/1/model.py")
    print("  /models/yolov8n_pipeline/config.pbtxt")


if __name__ == "__main__":
    main()
