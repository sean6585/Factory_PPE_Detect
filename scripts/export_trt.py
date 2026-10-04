"""
Export a YOLOv8 .pt model to TensorRT .engine format for fast Jetson inference.
Run this ONCE on the Jetson Orin NX after the Docker container is running.

Usage (inside container on Jetson):
    python3 scripts/export_trt.py --model models/ppe.pt --imgsz 640
    python3 scripts/export_trt.py --model models/ppe.pt --imgsz 640 --half  # FP16 (faster)
"""

import argparse
import os
import sys


def export(model_path: str, imgsz: int, half: bool, batch: int):
    try:
        from ultralytics import YOLO
    except ImportError:
        print("ultralytics not found. Install it first.")
        sys.exit(1)

    if not os.path.exists(model_path):
        print(f"Model not found: {model_path}")
        sys.exit(1)

    print(f"Loading {model_path} ...")
    model = YOLO(model_path)

    print(f"Exporting to TensorRT (imgsz={imgsz}, half={half}, batch={batch}) ...")
    engine_path = model.export(
        format="engine",
        imgsz=imgsz,
        half=half,
        batch=batch,
        device=0,
    )
    print(f"\nTensorRT engine saved: {engine_path}")
    print("Use this .engine file path with inference.py --model flag on Jetson.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export YOLOv8 to TensorRT")
    parser.add_argument("--model", required=True, help="Path to .pt model file")
    parser.add_argument("--imgsz", type=int, default=640, help="Inference image size")
    parser.add_argument("--half", action="store_true",
                        help="Enable FP16 precision (recommended for Jetson)")
    parser.add_argument("--batch", type=int, default=1, help="Batch size (1 for real-time stream)")
    args = parser.parse_args()

    export(args.model, args.imgsz, args.half, args.batch)
