"""
Compare inference latency of models/ppe.pt against models/ppe.engine on this device.

Mirrors gate_server.detect(): same predict() call, same INFER_FLOOR, same imgsz, so the
numbers reported here are the ones a gate tap actually pays -- times the burst size in
config/gate.json ("frames").

Usage (on the Jetson):
    python3 scripts/bench_models.py                       # both models, real samples
    python3 scripts/bench_models.py --runs 30 --imgsz 640
"""

import argparse
import json
import os
import statistics
import sys
import time

import cv2
import numpy as np

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INFER_FLOOR = 0.05          # keep in step with gate_server.INFER_FLOOR


def load_images(imgsz):
    d = os.path.join(PKG, "samples")
    files = sorted(f for f in os.listdir(d) if f.lower().endswith((".jpg", ".png")))
    imgs = [cv2.imread(os.path.join(d, f)) for f in files]
    imgs = [i for i in imgs if i is not None]
    if not imgs:
        imgs = [np.zeros((imgsz, imgsz, 3), dtype=np.uint8)]
        print("No sample images found - benchmarking on a blank frame instead.")
    return imgs


def bench(model_path, imgs, runs, imgsz, warmup):
    from ultralytics import YOLO

    print(f"\n--- {os.path.basename(model_path)} ---")
    t0 = time.time()
    model = YOLO(model_path)
    load_s = time.time() - t0

    for _ in range(warmup):
        model.predict(imgs[0], conf=INFER_FLOOR, imgsz=imgsz, device=0, verbose=False)

    times, ndets = [], []
    for i in range(runs):
        img = imgs[i % len(imgs)]
        t = time.time()
        res = model.predict(img, conf=INFER_FLOOR, imgsz=imgsz, device=0, verbose=False)[0]
        times.append((time.time() - t) * 1000.0)
        ndets.append(len(res.boxes))

    times.sort()
    stats = {
        "model": os.path.basename(model_path),
        "load_s": round(load_s, 1),
        "mean_ms": round(statistics.mean(times), 1),
        "median_ms": round(statistics.median(times), 1),
        "p90_ms": round(times[int(len(times) * 0.9) - 1], 1),
        "min_ms": round(times[0], 1),
        "max_ms": round(times[-1], 1),
        "mean_dets": round(statistics.mean(ndets), 1),
    }
    print(f"  load {stats['load_s']}s | mean {stats['mean_ms']} ms | median {stats['median_ms']} ms "
          f"| p90 {stats['p90_ms']} ms | min {stats['min_ms']} | max {stats['max_ms']}")
    print(f"  mean detections/frame: {stats['mean_dets']}  (sanity check: engine should match .pt)")
    return stats


def main():
    ap = argparse.ArgumentParser(description="Benchmark PPE .pt vs .engine")
    ap.add_argument("--runs", type=int, default=25)
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--imgsz", type=int, default=640)
    args = ap.parse_args()

    imgs = load_images(args.imgsz)
    print(f"Benchmarking on {len(imgs)} sample image(s), {args.runs} runs each, imgsz={args.imgsz}")

    results = []
    for name in ("ppe.pt", "ppe.engine"):
        p = os.path.join(PKG, "models", name)
        if os.path.exists(p):
            results.append(bench(p, imgs, args.runs, args.imgsz, args.warmup))
        else:
            print(f"\n--- {name}: not present, skipped ---")

    if len(results) == 2:
        pt, eng = results
        speedup = pt["mean_ms"] / eng["mean_ms"] if eng["mean_ms"] else 0
        burst = 5
        try:
            with open(os.path.join(PKG, "config", "gate.json")) as f:
                burst = json.load(f).get("frames", 5)
        except Exception:
            pass
        print(f"\n==> Engine is {speedup:.2f}x faster "
              f"({pt['mean_ms']} ms -> {eng['mean_ms']} ms per frame)")
        print(f"==> Per gate tap ({burst}-frame burst, sequential): "
              f"{pt['mean_ms'] * burst / 1000:.2f}s -> {eng['mean_ms'] * burst / 1000:.2f}s")
        if abs(pt["mean_dets"] - eng["mean_dets"]) > 0.5:
            print(f"WARNING: detection counts differ ({pt['mean_dets']} vs {eng['mean_dets']}) - "
                  "FP16 may have changed results, check the checklist output before trusting it.")


if __name__ == "__main__":
    sys.exit(main())
