"""
YOLOv8n ONNX export — Run by the model-loader Job inside k8s.

Downloads YOLOv8n weights, exports to ONNX, and sets up the Triton model
repository with preprocess (Python backend) and pipeline ensemble on the PVC.
"""

import shutil
from pathlib import Path

from ultralytics import YOLO

MODELS_ROOT = Path("/models")
WORKSPACE = Path("/workspace")


def main():
    # --- ONNX export ---
    model_dir = MODELS_ROOT / "yolov8n" / "1"
    model_dir.mkdir(parents=True, exist_ok=True)

    print("Loading YOLOv8n...")
    model = YOLO("yolov8n.pt")

    print("Exporting to ONNX...")
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

    # Copy ONNX config
    src = WORKSPACE / "config.pbtxt"
    if src.exists():
        shutil.copy2(str(src), str(MODELS_ROOT / "yolov8n" / "config.pbtxt"))

    # --- Preprocess model (Python backend) ---
    preprocess_dir = MODELS_ROOT / "preprocess" / "1"
    preprocess_dir.mkdir(parents=True, exist_ok=True)

    src = WORKSPACE / "preprocess_model.py"
    if src.exists():
        shutil.copy2(str(src), str(preprocess_dir / "model.py"))

    src = WORKSPACE / "preprocess_config.pbtxt"
    if src.exists():
        shutil.copy2(str(src), str(MODELS_ROOT / "preprocess" / "config.pbtxt"))

    # --- Pipeline ensemble ---
    pipeline_dir = MODELS_ROOT / "yolov8n_pipeline"
    pipeline_dir.mkdir(parents=True, exist_ok=True)

    src = WORKSPACE / "pipeline_config.pbtxt"
    if src.exists():
        shutil.copy2(str(src), str(pipeline_dir / "config.pbtxt"))

    print("  Preprocess model installed")
    print("  Pipeline ensemble installed")
    print()
    print("✓ Model repository ready for Triton")
    print(f"  {MODELS_ROOT / 'yolov8n' / 'config.pbtxt'}")
    print(f"  {dest}")
    print(f"  {MODELS_ROOT / 'preprocess' / 'config.pbtxt'}")
    print(f"  {MODELS_ROOT / 'preprocess' / '1' / 'model.py'}")
    print(f"  {MODELS_ROOT / 'yolov8n_pipeline' / 'config.pbtxt'}")


if __name__ == "__main__":
    main()
