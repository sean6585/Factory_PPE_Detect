#!/bin/bash
# Launch the PPE Gate Kiosk directly on the Jetson Orin NX -- no Docker.
#
# Usage: ./run_gate_native.sh [port] [extra gate_server.py args...]   # default port 8100
#
#   ./run_gate_native.sh                                  # camera on, default RTSP URL
#   ./run_gate_native.sh 8100 --no-camera                 # file-upload UI only
#   CAM_RTSP='rtsp://service:pw@192.168.1.105:554/?h26x=4&line=1&inst=1' \
#     ./run_gate_native.sh                                # override the camera URL
#
# Quote an RTSP URL in SINGLE quotes -- a Bosch password ending in '!' triggers
# bash history expansion inside double quotes.
#
# Then open http://localhost:<port> on the Jetson, or http://<jetson-ip>:<port>
# from another machine on the same LAN.
#
# WHY THIS EXISTS alongside run_gate_jetson.sh: JetPack 6 already ships everything
# the container was built to provide -- NVIDIA's CUDA torch, ultralytics, TensorRT
# and OpenCV are installed system-wide on this device. Running natively skips the
# 7.7 GB base-image pull and the docker-group membership the container route needs.
# Use run_gate_jetson.sh instead when you want the pinned, reproducible environment.
#
# NO INTERNET IS NEEDED to run this. The page is fully self-contained (no CDN, no
# web fonts) and inference is local.

set -e

# First arg is the port only when it looks like one; everything else is forwarded
# to gate_server.py, so --rtsp/--no-camera work without editing the source.
PORT=8100
if [[ "${1:-}" =~ ^[0-9]+$ ]]; then PORT="$1"; shift; fi
PKG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

# Preflight. The failure worth catching by name is torchvision: ultralytics calls
# torchvision.ops.nms for every inference, and the generic PyPI aarch64 wheel is
# built with _GLIBCXX_USE_CXX11_ABI=0 while NVIDIA's torch is ABI=1. The mismatch
# does not surface at install time -- it surfaces as an import error on the first
# frame, so check it before the model spends 30 s loading.
"$PY" - <<'PREFLIGHT' || exit 1
import sys

try:
    import torch
except ImportError:
    sys.exit("ERROR: torch not found. On JetPack this is NVIDIA's wheel, not pip's.")

try:
    import torchvision  # noqa: F401
    from torchvision.ops import nms  # noqa: F401
except Exception as e:
    sys.exit(
        f"ERROR: torchvision is unusable ({e}).\n"
        "       Almost certainly the C++ ABI mismatch: the generic PyPI aarch64 wheel is\n"
        "       built ABI=0, NVIDIA's torch is ABI=1. Install the CXX11-ABI build:\n"
        "         pip install --force-reinstall --no-deps \\\n"
        "           https://download-r2.pytorch.org/whl/cu124/"
        "torchvision-0.20.0-cp310-cp310-linux_aarch64.whl\n"
        "       --no-deps matters: without it pip pulls a generic torch over NVIDIA's CUDA build."
    )

try:
    import ultralytics  # noqa: F401
except ImportError:
    sys.exit("ERROR: ultralytics not found.  pip install ultralytics")

if not torch.cuda.is_available():
    print("WARNING: CUDA is not available -- inference will fall back to CPU and be")
    print("         very slow (seconds per frame). Check the JetPack install.")
else:
    print(f"==> CUDA ready on {torch.cuda.get_device_name(0)}")
PREFLIGHT

# Prefer the TensorRT engine when it has been built on this device. Engines are tied
# to the specific GPU and TRT version, so one built elsewhere is unusable -- decided
# from the filesystem rather than by running and reacting to a failure.
if [ -f "$PKG/models/ppe.engine" ]; then
  MODEL="$PKG/models/ppe.engine"
  echo "==> Using TensorRT engine (fastest)"
else
  MODEL="$PKG/models/ppe.pt"
  echo "==> models/ppe.engine not found - using PyTorch weights (~100 ms/frame)."
  echo "    Build the engine once with:"
  echo "      $PY $PKG/scripts/export_trt.py --model $PKG/models/ppe.pt --half"
fi

echo "==> PPE Gate Kiosk on http://localhost:${PORT}   (Ctrl-C to stop)"

exec "$PY" "$PKG/scripts/gate_server.py" \
  --model "$MODEL" \
  --config "$PKG/config/gate.json" \
  --port "$PORT" \
  "$@"
