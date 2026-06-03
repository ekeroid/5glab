#!/bin/bash
set -e

MODEL_DIR="${MODEL_DIR:-/models}"
MODEL_NAME="yolov8n_trt"
PLAN_FILE="$MODEL_DIR/$MODEL_NAME/1/model.plan"

# Build TRT engine if not present on PVC
if [ ! -f "$PLAN_FILE" ]; then
    echo "[entrypoint] Building model repository..."
    python3 /opt/export_model.py --output "$MODEL_DIR"
else
    echo "[entrypoint] TRT engine found at $PLAN_FILE, skipping build"
fi

# Start Triton in background
echo "[entrypoint] Starting Triton Inference Server..."
tritonserver \
    --model-repository="$MODEL_DIR" \
    --log-verbose=0 \
    --strict-model-config=false \
    --model-control-mode=explicit \
    --load-model=yolov8n_trt &

TRITON_PID=$!

# Wait for Triton to be ready
echo "[entrypoint] Waiting for Triton to be ready..."
for i in $(seq 1 60); do
    if curl -s localhost:8000/v2/health/ready > /dev/null 2>&1; then
        echo "[entrypoint] Triton ready after ${i}s"
        break
    fi
    sleep 1
done

# Start sidecar (inference API)
echo "[entrypoint] Starting inference sidecar on :8080..."
exec uvicorn main:app --host 0.0.0.0 --port 8080 --app-dir /opt/sidecar
