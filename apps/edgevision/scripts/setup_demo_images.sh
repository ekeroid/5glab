#!/usr/bin/env bash
# Download COCO val2017 demo images for the camera API.
# Idempotent — skips already-downloaded files.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGES_DIR="${SCRIPT_DIR}/../camera-api/images"
BASE_URL="http://images.cocodataset.org/val2017"

IMAGES=(
    000000039769.jpg
    000000058350.jpg
    000000085329.jpg
    000000174482.jpg
    000000006471.jpg
    000000010092.jpg
    000000459153.jpg
    000000494869.jpg
    000000057597.jpg
    000000061418.jpg
    000000062808.jpg
    000000069213.jpg
    000000013597.jpg
    000000014226.jpg
    000000089670.jpg
    000000017714.jpg
    000000018737.jpg
    000000130599.jpg
    000000019786.jpg
    000000183648.jpg
)

mkdir -p "${IMAGES_DIR}"

downloaded=0
skipped=0
failed=0

echo "Downloading ${#IMAGES[@]} COCO val2017 images to ${IMAGES_DIR}..."
echo ""

for img in "${IMAGES[@]}"; do
    dest="${IMAGES_DIR}/${img}"
    if [[ -f "${dest}" ]]; then
        skipped=$((skipped + 1))
        continue
    fi

    url="${BASE_URL}/${img}"
    if curl -sfL -o "${dest}" "${url}"; then
        downloaded=$((downloaded + 1))
        echo "  ✓ ${img}"
    else
        failed=$((failed + 1))
        echo "  ✗ ${img} (failed)"
        rm -f "${dest}"
    fi
done

echo ""
echo "Summary: ${downloaded} downloaded, ${skipped} skipped, ${failed} failed"
echo "Total images in ${IMAGES_DIR}: $(ls "${IMAGES_DIR}"/*.jpg 2>/dev/null | wc -l | tr -d ' ')"
