#!/bin/bash
set -e

MODEL_DIR="${MODEL_DIR:-/models}"

# Export seg model if not yet built
if [ ! -f "$MODEL_DIR/yolov8xseg_trt/1/model.plan" ]; then
    echo "[entrypoint] Building YOLOv8x-seg TRT engine (first boot only, ~3-5 min)..."
    cd /opt && python3 /opt/sidecar/export_model.py --output "$MODEL_DIR" --model seg
    cd /
fi

echo "[entrypoint] Starting Triton Inference Server..."
tritonserver \
    --model-repository="$MODEL_DIR" \
    --log-verbose=0 \
    --strict-model-config=false \
    --model-control-mode=explicit \
    --load-model=yolov8n_trt \
    --load-model=yolov8xseg_trt &

TRITON_PID=$!

echo "[entrypoint] Waiting for Triton to be ready..."
for i in $(seq 1 60); do
    if curl -s localhost:8000/v2/health/ready > /dev/null 2>&1; then
        echo "[entrypoint] Triton ready after ${i}s"
        break
    fi
    sleep 1
done

echo "[entrypoint] Starting inference sidecar on :8080 + :50051..."
exec uvicorn main:app --host 0.0.0.0 --port 8080 --app-dir /opt/sidecar
