"""
Model export — ONNX + TensorRT FP16 engine build for Triton.

Runs once on first boot. Produces:
  /models/yolov8n_trt/1/model.plan  (TensorRT FP16 engine)
  /models/yolov8n_trt/config.pbtxt
"""

import argparse
import shutil
import subprocess
from pathlib import Path

from ultralytics import YOLO


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/models")
    args = parser.parse_args()

    models_root = Path(args.output)

    # ONNX export
    onnx_dir = models_root / "yolov8n_onnx"
    onnx_dir.mkdir(parents=True, exist_ok=True)

    print("[export] Loading YOLOv8n...")
    model = YOLO("yolov8n.pt")

    print("[export] Exporting to ONNX...")
    export_path = model.export(
        format="onnx",
        opset=12,
        dynamic=False,
        imgsz=640,
        simplify=True,
    )

    onnx_file = onnx_dir / "model.onnx"
    shutil.move(export_path, str(onnx_file))
    print(f"[export] ONNX: {onnx_file} ({onnx_file.stat().st_size / 1024 / 1024:.1f} MB)")

    # TensorRT FP16 build
    trt_dir = models_root / "yolov8n_trt" / "1"
    trt_dir.mkdir(parents=True, exist_ok=True)

    trt_plan = trt_dir / "model.plan"
    print("[export] Building TensorRT FP16 engine (this takes 1-2 minutes)...")

    import os
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

    print(f"[export] TRT: {trt_plan} ({trt_plan.stat().st_size / 1024 / 1024:.1f} MB)")

    # Write Triton config
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

    print("[export] Done — model repository ready")


if __name__ == "__main__":
    main()
