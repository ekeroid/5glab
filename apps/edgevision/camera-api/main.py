"""
Camera API — Fake camera frame server for the EdgeVision demo.

Serves JPEG images from the images/ directory in round-robin order,
simulating a surveillance camera feed. Used by the EdgeVision client
for both local and edge-offloaded YOLOv8 object detection.
"""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response

app = FastAPI(title="EdgeVision Camera API")

IMAGES_DIR = Path(os.getenv("IMAGES_DIR", "/app/images"))

_frame_index = 0
_image_files: list[Path] = []


def _load_images():
    global _image_files
    _image_files = sorted(
        p for p in IMAGES_DIR.iterdir()
        if p.suffix.lower() in (".jpg", ".jpeg", ".png")
    )


@app.on_event("startup")
async def startup():
    _load_images()


@app.get("/frame")
async def get_frame():
    """Return the next JPEG frame from the image directory."""
    global _frame_index

    if not _image_files:
        _load_images()
    if not _image_files:
        return Response(content=b"", status_code=503)

    img_path = _image_files[_frame_index % len(_image_files)]
    data = img_path.read_bytes()
    idx = _frame_index
    _frame_index = (_frame_index + 1) % len(_image_files)

    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "X-Frame-Index": str(idx),
            "X-Frame-Source": img_path.name,
        },
    )


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "ok",
        "frame_count": len(_image_files),
        "current_index": _frame_index,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
