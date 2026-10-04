#!/bin/bash
# Launch the PPE Gate Kiosk on the Jetson Orin NX.
#
# Usage: ./run_gate_jetson.sh [port]        # default 8100
#
# Then open http://localhost:<port> on the Jetson, or http://<jetson-ip>:<port>
# from another machine on the same LAN.
#
# NO INTERNET IS NEEDED to run this. The page is fully self-contained (no CDN, no
# web fonts) and inference is local. Verified by running the whole model + decision
# path under `docker run --network none`.
#
# Requires the ppe-safety:jetson image to exist on this device -- see Dockerfile.jetson.
# `--runtime=nvidia` only: do NOT add `--gpus all`, which is the desktop flag and
# aborts on L4T with `could not select device driver "nvidia" with capabilities: [[gpu]]`.

set -e

PORT="${1:-8100}"
PKG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! docker image inspect ppe-safety:jetson >/dev/null 2>&1; then
  echo "ERROR: image ppe-safety:jetson not found on this device."
  echo "       Build it here first (see Dockerfile.jetson header):"
  echo "         docker pull ultralytics/ultralytics:latest-jetson-jetpack6"
  echo "         docker build -f Dockerfile.jetson -t ppe-safety:jetson ."
  exit 1
fi

# Prefer the TensorRT engine when it has been built on this device. Engines are tied
# to the specific GPU and TRT version, so one built elsewhere is unusable -- decided
# from the filesystem rather than by running and reacting to a failure.
if [ -f "$PKG/models/ppe.engine" ]; then
  MODEL="/app/models/ppe.engine"
  echo "==> Using TensorRT engine (fastest)"
else
  MODEL="/app/models/ppe.pt"
  echo "==> models/ppe.engine not found - using PyTorch weights (slower)."
  echo "    Build the engine once with:"
  echo "      docker run --rm --runtime=nvidia -v \"$PKG:/app\" -w /app ppe-safety:jetson \\"
  echo "        python3 scripts/export_trt.py --model models/ppe.pt --half"
fi

echo "==> PPE Gate Kiosk on http://localhost:${PORT}   (Ctrl-C to stop)"

TTY_FLAGS=()
[ -t 0 ] && [ -t 1 ] && TTY_FLAGS=(-it)

# models/ is writable so the TensorRT export can drop ppe.engine beside the weights.
docker run --rm "${TTY_FLAGS[@]}" \
  --runtime=nvidia \
  -p "${PORT}:${PORT}" \
  -v "$PKG/scripts:/app/scripts:ro" \
  -v "$PKG/config:/app/config:ro" \
  -v "$PKG/models:/app/models" \
  -w /app \
  ppe-safety:jetson \
  python3 scripts/gate_server.py \
    --model "$MODEL" \
    --config /app/config/gate.json \
    --port "${PORT}"
