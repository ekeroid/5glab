"""
EdgeVision client — Main processing loop for CAMARA edge compute offloading demo.

Operates in two modes:
  - LOCAL: Runs YOLOv8n inference on the local CPU
  - EDGE: Discovers an edge zone via CAMARA, provisions a GPU Triton instance,
          and offloads inference through the NEF proxy

The CAMARA control plane lifecycle (discovery → registration → instantiation →
endpoint discovery) is logged step-by-step as it is the demo centrepiece.
"""

import logging
import signal
import sys
import time

import requests

import camara
import config
import detector_local
import detector_remote
import display

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("edgevision")

# Edge state
_app_instance_id: str | None = None
_triton_proxy_url: str | None = None
_shutdown = False


def _signal_handler(sig, frame):
    global _shutdown
    logger.info("Shutdown signal received")
    _shutdown = True


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


def _setup_edge() -> tuple[str, str]:
    """
    Full CAMARA edge lifecycle setup.

    Returns (app_instance_id, triton_proxy_url)
    """
    logger.info("=" * 60)
    logger.info("CAMARA EDGE COMPUTE SETUP")
    logger.info("=" * 60)

    # Step 1: Discover edge zone
    logger.info("[Step 1/5] Discovering edge cloud zones...")
    zone = camara.discover_zone()
    zone_id = zone["edgeCloudZoneId"]
    logger.info(f"  → Zone: {zone.get('edgeCloudZoneName')} (id={zone_id})")

    # Step 2: Register app manifest
    logger.info("[Step 2/5] Registering app manifest...")
    app_id = camara.register_app()
    logger.info(f"  → App registered: {app_id}")

    # Step 3: Instantiate on zone
    logger.info("[Step 3/5] Instantiating app on edge zone...")
    instance_id = camara.instantiate_app(app_id, zone_id)
    logger.info(f"  → Instance: {instance_id}")

    # Step 4: Wait for ready
    logger.info("[Step 4/5] Waiting for instance to become ready...")
    max_wait = 300  # 5 minutes
    poll_interval = 5
    elapsed = 0
    while elapsed < max_wait:
        status = camara.get_instance_status(instance_id)
        if status == "ready":
            logger.info(f"  → Instance READY after {elapsed}s")
            break
        elif status == "failed":
            raise RuntimeError(f"Instance {instance_id} failed to start")
        time.sleep(poll_interval)
        elapsed += poll_interval
    else:
        raise RuntimeError(f"Instance {instance_id} not ready after {max_wait}s")

    # Step 5: Get endpoint
    logger.info("[Step 5/5] Discovering inference endpoint...")
    endpoint = camara.get_endpoint(instance_id)
    logger.info(f"  → Proxy endpoint: {endpoint}")

    logger.info("=" * 60)
    logger.info("EDGE SETUP COMPLETE — starting inference loop")
    logger.info("=" * 60)

    return instance_id, endpoint


def _teardown_edge(instance_id: str):
    """Terminate edge instance on shutdown."""
    logger.info("Terminating edge instance...")
    try:
        camara.terminate_app(instance_id)
        logger.info("Edge instance terminated")
    except Exception as e:
        logger.error(f"Failed to terminate instance: {e}")


def main():
    global _app_instance_id, _triton_proxy_url

    logger.info(f"EdgeVision starting in {config.MODE.upper()} mode")
    logger.info(f"  Camera: {config.CAMERA_API_URL}")
    logger.info(f"  Frame interval: {config.FRAME_INTERVAL_MS}ms")
    logger.info(f"  Confidence threshold: {config.CONFIDENCE_THRESHOLD}")
    logger.info(f"  Output: {config.OUTPUT_DIR}")

    # Edge setup
    if config.MODE == "edge":
        logger.info(f"  CAMARA API: {config.CAMARA_API_URL}")
        _app_instance_id, _triton_proxy_url = _setup_edge()

    # Main processing loop
    frame_num = 0
    while not _shutdown:
        loop_start = time.perf_counter()

        try:
            # Fetch frame
            resp = requests.get(f"{config.CAMERA_API_URL}/frame", timeout=10)
            resp.raise_for_status()
            jpeg_bytes = resp.content
            frame_source = resp.headers.get("X-Frame-Source", "unknown")

            # Run inference
            infer_start = time.perf_counter()
            if config.MODE == "edge" and _triton_proxy_url:
                detections = detector_remote.detect(jpeg_bytes, _triton_proxy_url)
            else:
                detections = detector_local.detect(jpeg_bytes)
            infer_ms = (time.perf_counter() - infer_start) * 1000

            # Annotate and save
            display.annotate(
                jpeg_bytes=jpeg_bytes,
                detections=detections,
                mode=config.MODE,
                latency_ms=infer_ms,
                app_instance_id=_app_instance_id,
                proxy_endpoint=_triton_proxy_url,
            )

            frame_num += 1
            logger.info(
                f"Frame {frame_num:04d} | {config.MODE.upper()} | "
                f"{infer_ms:.0f}ms | {len(detections)} detections | {frame_source}"
            )

        except requests.exceptions.ConnectionError:
            logger.warning("Camera API unreachable, retrying...")
        except Exception as e:
            logger.error(f"Frame processing error: {e}")

        # Wait for next frame
        elapsed = (time.perf_counter() - loop_start) * 1000
        sleep_ms = max(0, config.FRAME_INTERVAL_MS - elapsed)
        if sleep_ms > 0 and not _shutdown:
            time.sleep(sleep_ms / 1000)

    # Cleanup
    if config.MODE == "edge" and _app_instance_id:
        _teardown_edge(_app_instance_id)

    logger.info("EdgeVision stopped")


if __name__ == "__main__":
    main()
