#!/bin/bash
# EdgeVision Demo — start/stop script
#
# Usage:
#   ./demo.sh start    Start camera-api + GUI (edge mode with gRPC)
#   ./demo.sh stop     Kill everything and tear down edge instance
#   ./demo.sh restart  Stop then start
#
# Environment overrides:
#   EDGE_TRANSPORT=http|grpc  (default: grpc)
#   FRAME_INTERVAL_MS=200     (default: 200)

set -e
cd "$(dirname "$0")"

# Activate project venv
VENV_DIR="$(cd .. && pwd)/.venv"
if [ -f "$VENV_DIR/bin/activate" ]; then
    source "$VENV_DIR/bin/activate"
fi

CAMERA_API_URL="http://localhost:8081"
CAMARA_API_URL="http://camara.5glab.control.lth.se"
EDGE_TRANSPORT="${EDGE_TRANSPORT:-grpc}"
FRAME_INTERVAL_MS="${FRAME_INTERVAL_MS:-200}"

start() {
    echo "=== EdgeVision Demo ==="
    echo "  Transport: $EDGE_TRANSPORT"
    echo "  Frame interval: ${FRAME_INTERVAL_MS}ms"
    echo ""

    # Start camera-api if not running
    if ! curl -s "$CAMERA_API_URL/health" > /dev/null 2>&1; then
        echo "[1/2] Starting camera-api..."
        cd camera-api
        IMAGES_DIR="$(pwd)/images" python3 main.py &
        echo $! > /tmp/edgevision-camera.pid
        cd ..
        sleep 2
    else
        echo "[1/2] Camera-api already running"
    fi

    # Start GUI
    echo "[2/2] Starting GUI..."
    CAMERA_API_URL="$CAMERA_API_URL" \
    CAMARA_API_URL="$CAMARA_API_URL" \
    EDGE_TRANSPORT="$EDGE_TRANSPORT" \
    FRAME_INTERVAL_MS="$FRAME_INTERVAL_MS" \
    python3 client/gui.py --edge &
    echo $! > /tmp/edgevision-gui.pid

    echo ""
    echo "Demo running. Controls:"
    echo "  [E] Toggle LOCAL/EDGE mode"
    echo "  [T] Toggle gRPC/HTTP transport"
    echo "  [M] Toggle model: detect/seg"
    echo "  [Q] Quit GUI"
    echo ""
    echo "Stop with: ./demo.sh stop"
}

stop() {
    echo "Stopping demo..."

    # Kill GUI
    if [ -f /tmp/edgevision-gui.pid ]; then
        kill "$(cat /tmp/edgevision-gui.pid)" 2>/dev/null && echo "  GUI stopped"
        rm -f /tmp/edgevision-gui.pid
    fi
    pkill -f "python3 client/gui.py" 2>/dev/null || true
    pkill -f "python3 gui.py" 2>/dev/null || true

    # Kill camera-api (only the local python one, not Docker)
    if [ -f /tmp/edgevision-camera.pid ]; then
        kill "$(cat /tmp/edgevision-camera.pid)" 2>/dev/null && echo "  Camera-api stopped"
        rm -f /tmp/edgevision-camera.pid
    fi

    # Terminate edge instance via CAMARA API
    echo "  Cleaning up edge instances..."
    curl -s -X DELETE "$CAMARA_API_URL/edge-app-management/v0/app-instances/all" > /dev/null 2>&1 || true

    echo "Done."
}

case "${1:-}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 2; start ;;
    *)
        echo "Usage: $0 {start|stop|restart}"
        exit 1
        ;;
esac
