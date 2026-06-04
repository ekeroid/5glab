"""
Model export — ONNX + TensorRT FP16 engine build for Triton.

Exports both yolov8n (detection) and yolov8x-seg (segmentation).
Produces:
  /models/yolov8n_trt/1/model.plan
  /models/yolov8xseg_trt/1/model.plan
"""

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from ultralytics import YOLO


def _build_trt_engine(onnx_file: Path, trt_plan: Path):
    trtexec_bin = "/usr/src/tensorrt/bin/trtexec"
    env = os.environ.copy()
    env.pop("NVIDIA_VISIBLE_DEVICES", None)
    env["CUDA_VISIBLE_DEVICES"] = "0"

    result = subprocess.run([
        trtexec_bin,
        f"--onnx={onnx_file}",
        f"--saveEngine={trt_plan}",
        "--fp16",
    ], capture_output=True, text=True, env=env)

    if result.returncode != 0:
        print(f"[export] TensorRT build FAILED:\n{result.stderr[-2000:]}")
        raise RuntimeError("TensorRT engine build failed")


def export_yolov8n(models_root: Path):
    trt_dir = models_root / "yolov8n_trt" / "1"
    trt_plan = trt_dir / "model.plan"
    if trt_plan.exists():
        print(f"[export] yolov8n_trt already exists, skipping")
        return

    trt_dir.mkdir(parents=True, exist_ok=True)
    onnx_dir = models_root / "yolov8n_onnx"
    onnx_dir.mkdir(parents=True, exist_ok=True)

    print("[export] Loading YOLOv8n...")
    model = YOLO("yolov8n.pt")

    print("[export] Exporting YOLOv8n to ONNX...")
    export_path = model.export(format="onnx", opset=12, dynamic=False, imgsz=640, simplify=True)
    onnx_file = onnx_dir / "model.onnx"
    shutil.move(export_path, str(onnx_file))
    print(f"[export] ONNX: {onnx_file} ({onnx_file.stat().st_size / 1024 / 1024:.1f} MB)")

    print("[export] Building YOLOv8n TensorRT FP16 engine...")
    _build_trt_engine(onnx_file, trt_plan)
    print(f"[export] TRT: {trt_plan} ({trt_plan.stat().st_size / 1024 / 1024:.1f} MB)")

    config_path = models_root / "yolov8n_trt" / "config.pbtxt"
    config_path.write_text('''name: "yolov8n_trt"
backend: "tensorrt"
max_batch_size: 0

input [
  {
    name: "images"
    data_type: TYPE_FP32
    dims: [1, 3, 640, 640]
  }
]

output [
  {
    name: "output0"
    data_type: TYPE_FP32
    dims: [1, 84, 8400]
  }
]

instance_group [
  {
    kind: KIND_GPU
    count: 1
  }
]
''')


def export_yolov8x_seg(models_root: Path):
    trt_dir = models_root / "yolov8xseg_trt" / "1"
    trt_plan = trt_dir / "model.plan"
    if trt_plan.exists():
        print(f"[export] yolov8xseg_trt already exists, skipping")
        return

    trt_dir.mkdir(parents=True, exist_ok=True)
    onnx_dir = models_root / "yolov8xseg_onnx"
    onnx_dir.mkdir(parents=True, exist_ok=True)

    print("[export] Loading YOLOv8x-seg...")
    model = YOLO("yolov8x-seg.pt")

    print("[export] Exporting YOLOv8x-seg to ONNX...")
    export_path = model.export(format="onnx", opset=12, dynamic=False, imgsz=640, simplify=True)
    onnx_file = onnx_dir / "model.onnx"
    shutil.move(export_path, str(onnx_file))
    print(f"[export] ONNX: {onnx_file} ({onnx_file.stat().st_size / 1024 / 1024:.1f} MB)")

    print("[export] Building YOLOv8x-seg TensorRT FP16 engine (this takes 3-5 minutes)...")
    _build_trt_engine(onnx_file, trt_plan)
    print(f"[export] TRT: {trt_plan} ({trt_plan.stat().st_size / 1024 / 1024:.1f} MB)")

    # yolov8x-seg output: output0=[1, 116, 8400], output1=[1, 32, 160, 160]
    config_path = models_root / "yolov8xseg_trt" / "config.pbtxt"
    config_path.write_text('''name: "yolov8xseg_trt"
backend: "tensorrt"
max_batch_size: 0

input [
  {
    name: "images"
    data_type: TYPE_FP32
    dims: [1, 3, 640, 640]
  }
]

output [
  {
    name: "output0"
    data_type: TYPE_FP32
    dims: [1, 116, 8400]
  },
  {
    name: "output1"
    data_type: TYPE_FP32
    dims: [1, 32, 160, 160]
  }
]

instance_group [
  {
    kind: KIND_GPU
    count: 1
  }
]
''')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/models")
    parser.add_argument("--model", default="all", choices=["all", "detect", "seg"])
    args = parser.parse_args()

    models_root = Path(args.output)

    if args.model in ("all", "detect"):
        export_yolov8n(models_root)
    if args.model in ("all", "seg"):
        export_yolov8x_seg(models_root)

    print("[export] Done — model repository ready")


if __name__ == "__main__":
    main()
