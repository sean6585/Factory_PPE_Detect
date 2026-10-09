"""
PPE gate kiosk — Phase 1: check engine + display, driven by still images.

Phase 2 replaces the test bar with a live RTSP burst and a network RFID trigger;
everything below the capture layer (association, voting, checklist, display) is the
same code that will run on the Jetson.

Served as a web page for the same reason as gui_server.py: the container has no
tkinter and its OpenCV is a headless build, so cv2.imshow cannot be used. On the
Jetson this page runs fullscreen in a kiosk browser.

Usage (inside the container — see run_gate_x86.sh):
    python3 scripts/gate_server.py --model models/ppe.pt --config config/gate.json
"""

import argparse
import json
import csv
import os
import pathlib
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import cv2
import numpy as np
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ppe_check import _area, containment, evaluate, load_config   # noqa: E402
from patlite_tower import LAMPS, SPEECH_LANGS, Tower, TowerError   # noqa: E402
from whitelist import Whitelist                                    # noqa: E402
from rfid_reader import pick_worker                                # noqa: E402
from gate_light import LightDriver, LightPolicy                    # noqa: E402

# Detections are returned above this floor and the checklist applies its own per-item
# thresholds on top, so one inference serves every item regardless of its conf.
INFER_FLOOR = 0.05
MAX_FRAMES_CACHED = 24

MB = 1024 * 1024
GB = 1024 * MB

# scripts/ lives one level below the package root, which is where web/, config/, models/
# and MP3/ sit.
PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(PKG_ROOT, "web")

# The kiosk page, served from web/ as three ordinary files rather than one Python string.
# Splitting them out is what gives the browser real filenames: a JS error now reports
# "app.js:412" instead of a line number inside a blob, and an editor can lint and
# highlight them. They are still entirely local — no CDN, no network — which is the
# constraint that actually matters for a gate that must work when the factory network
# does not.
WEB_ASSETS = {
    "/":        ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css",    "text/css; charset=utf-8"),
    "/app.js":  ("app.js",     "application/javascript; charset=utf-8"),
    # 大字報 (demo) mode — a full-screen visitor view layered over the page.
    "/demo.css": ("demo.css",  "text/css; charset=utf-8"),
    "/demo.js":  ("demo.js",   "application/javascript; charset=utf-8"),
}

# Quality of the JPEG kept for a capture. This is the buffer the recorder writes to the
# dataset verbatim, so it is the quality of the training data, not just of the preview —
# which is why it sits well above a display-oriented setting. Measured on a real gate
# frame at 1280x960: ~172 KB at q88, against ~120 KB at q80 and ~308 KB at q95.
CAPTURE_JPEG_QUALITY = 88

# The recorder and the audio player each run one background thread, so these bound how
# far behind they may fall before work is dropped rather than queued forever. The
# recorder's is generous (disk is fast, and losing a training sample is a real loss); the
# player's is deliberately short, because stale pass/fail announcements are worse than
# silence — a clip that plays after the next worker has arrived is actively misleading.
RECORD_QUEUE_MAX = 64
AUDIO_QUEUE_MAX = 4

# Ceiling for an attached class-list yaml. Generous for a file that holds a list of class
# names; it exists to reject something that is obviously not one.
MAX_YAML_BYTES = 1 * MB

# Realtime mode's preview JPEG. Lower than CAPTURE_JPEG_QUALITY on purpose: this one is
# only looked at, several times a second, while the frame SAVED to the dataset is encoded
# separately at full quality. Training data must not inherit a preview's compression.
REALTIME_PREVIEW_QUALITY = 70

# Chunk size for streaming a model upload to disk. A .pt is ~50 MB and a .onnx ~100 MB,
# and this process already holds a loaded network plus a CUDA context on a shared-memory
# device, so uploads are never read into RAM whole.
UPLOAD_CHUNK_BYTES = 1 * MB

_model = None
_lock = threading.Lock()
_state = {}
_frames = OrderedDict()      # id -> {"jpeg": bytes, "dets": [...], "w":, "h":}
_last_result = {"result": None}

# Serialises every call to run_gate_check() — see that function's own docstring: two
# overlapping triggers would interleave their frame grabs and vote over a mixture of
# both bursts. Held by both the button's /api/capture handler and the through-beam
# sensor's dispatcher (sensor_triggered / _run_sensor_check below), so whichever fires
# second just waits its turn rather than corrupting the first one's burst.
_gate_check_lock = threading.Lock()

# ── live camera (Phase 2) ─────────────────────────────────────────────────
# One background thread owns the RTSP capture and keeps only the newest frame
# (max-buffers=1 drop=true, plus this latest-wins buffer) so the MJPEG feed and
# the live check both see real-time video, never a backlog.
_camera = {"frame": None, "lock": threading.Lock(), "ts": 0.0, "ok": False}

# Realtime's own native-resolution stream (2026-10-07). Realtime samples are training
# data, but the gate's pipeline is hardware-scaled to --cap-width x --cap-height
# (1280x960): the 5 MP camera reduced to 1.2 MP before anything sees it. Raising the
# gate's own capture instead would re-scale every px² setting (trigger_min_area and its
# slider are calibrated in 1280x960 pixels) and slow every check burst (a 5 MP JPEG
# encodes in ~58 ms vs ~14 ms). So realtime opens a SECOND session on the same camera at
# its native size, only while the page polls it, and closes it when realtime stops.
# Measured on this camera: native 2592x1944 holds 30 fps at ~84 % of one core (vs ~30 %
# at 1280x960) — a cost paid only while realtime runs.
_camera_full = {"frame": None, "ts": 0.0, "lock": threading.Lock(), "want_until": 0.0,
                "running": False, "pipeline": None}
REALTIME_FULL_IDLE_S = 10.0    # the full-res stream closes this long after the last poll
REALTIME_FULL_FRESH_S = 1.0    # a full-res frame older than this is not "now"


def build_camera_pipeline(rtsp: str, width: int, height: int, protocol: str = "tcp") -> str:
    """Low-latency Jetson NVDEC pipeline, hardware-scaled to width x height (0 x 0 =
    the camera's native size, unscaled — realtime's full-resolution stream).

    protocol picks how rtspsrc carries RTP: "tcp" interleaves it inside the RTSP
    connection (no separate UDP ports, immune to UDP loss, but a lost/late TCP segment
    stalls everything behind it — head-of-line blocking); "udp" is raw RTP/UDP (no
    stalls, but a lost packet just corrupts/drops that frame instead of waiting for it).
    Measured on this camera's direct point-to-point link: average latency is the same
    either way (~24ms), TCP occasionally spikes (one 215ms stall observed against udp's
    worst case of ~50ms), because there's rarely real loss for TCP to recover from on a
    dedicated link — so the choice here is "rare stall" vs "rare corrupted frame", not
    a clear latency win for either.
    """
    size = f"width={width},height={height}," if width and height else ""
    return (
        f"rtspsrc location={rtsp} latency=0 protocols={protocol} "
        "drop-on-latency=true buffer-mode=none ! "
        "rtph264depay ! h264parse ! "
        "nvv4l2decoder disable-dpb=true enable-max-performance=true ! "
        f"nvvidconv ! video/x-raw,{size}format=BGRx ! "
        "videoconvert ! video/x-raw,format=BGR ! "
        "appsink drop=true max-buffers=1 sync=false"
    )


def latest_frame():
    with _camera["lock"]:
        f = _camera["frame"]
        return None if f is None else f.copy()


def camera_loop(pipeline: str):
    """Open the stream and pump the newest frame into _camera; auto-reconnect."""
    while not _state.get("stop"):
        cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        if not cap.isOpened():
            _camera["ok"] = False
            print("[camera] open failed, retry in 3s", flush=True)
            time.sleep(3)
            continue
        print("[camera] connected", flush=True)
        _camera["ok"] = True
        fails = 0
        while not _state.get("stop"):
            ok, frame = cap.read()
            if not ok or frame is None:
                fails += 1
                if fails > 30:
                    print("[camera] stream stalled, reconnecting", flush=True)
                    break
                time.sleep(0.02)
                continue
            fails = 0
            with _camera["lock"]:
                _camera["frame"] = frame
                _camera["ts"] = time.time()
        cap.release()
        _camera["ok"] = False
        time.sleep(1)


def _full_wanted() -> bool:
    return not _state.get("stop") and time.time() < _camera_full["want_until"]


def full_camera_loop():
    """Realtime's native-resolution stream: runs while realtime keeps polling
    (full_frame() extends want_until), then closes itself and frees the frame."""
    try:
        while _full_wanted():
            cap = cv2.VideoCapture(_camera_full["pipeline"], cv2.CAP_GSTREAMER)
            if not cap.isOpened():
                print("[camera-full] open failed, retry in 3s", flush=True)
                time.sleep(3)
                continue
            print("[camera-full] connected (realtime, native resolution)", flush=True)
            fails = 0
            while _full_wanted():
                ok, frame = cap.read()
                if not ok or frame is None:
                    fails += 1
                    if fails > 30:
                        print("[camera-full] stream stalled, reconnecting", flush=True)
                        break
                    time.sleep(0.02)
                    continue
                fails = 0
                with _camera_full["lock"]:
                    _camera_full["frame"] = frame
                    _camera_full["ts"] = time.time()
            cap.release()
    finally:
        with _camera_full["lock"]:
            _camera_full["running"] = False
            _camera_full["frame"] = None        # 15 MB, and a stale frame must never be "now"
        print("[camera-full] closed (realtime idle)", flush=True)


def full_frame():
    """The newest native-resolution frame for realtime, or None while its stream is
    still opening. Every call keeps the stream wanted for REALTIME_FULL_IDLE_S more,
    and starts it if it is not running."""
    if not _camera_full["pipeline"]:
        return None
    now = time.time()
    with _camera_full["lock"]:
        _camera_full["want_until"] = now + REALTIME_FULL_IDLE_S
        if not _camera_full["running"]:
            _camera_full["running"] = True
            threading.Thread(target=full_camera_loop, name="CameraFull", daemon=True).start()
        f, ts = _camera_full["frame"], _camera_full["ts"]
    if f is None or now - ts > REALTIME_FULL_FRESH_S:
        return None
    return f.copy()


def load_model(path: str, device: str, imgsz: int):
    global _model
    from ultralytics import YOLO
    print(f"Loading model: {path}")
    _model = YOLO(path)
    _model.predict(np.zeros((imgsz, imgsz, 3), dtype=np.uint8),
                   conf=INFER_FLOOR, imgsz=imgsz, device=device, verbose=False)
    print("Model warm.")
    # Override AFTER the warm-up, not before. For a .engine (and .onnx) YOLO() defers
    # building the real backend until the first predict(); before that there is no
    # names table to write to, and apply_yaml_override reports "could not be applied".
    # This looked like it worked for months because the paired yaml matched the
    # embedded names exactly, so the post-write equality check passed without any
    # write having landed. It surfaced the first time a yaml actually RENAMED a class:
    # the live "attach class list" path (model already warm) applied it, the next
    # restart silently reverted to the embedded names, and the checklist's remapped
    # class then matched nothing — every check failing, no error anywhere.
    warn = apply_yaml_override(_model, path)
    if warn:
        print(f"[model] WARNING: {warn}", flush=True)
    print(f"Classes: {list(_model.names.values())}")


def detect(bgr: np.ndarray) -> tuple[list[dict], float]:
    # Bind the model and its class names INSIDE the lock. A swap between the predict
    # and the name lookup would otherwise decode this frame's class ids against the
    # next model's name table — silently mislabelling every box.
    with _lock:
        m = _model
        names = m.names
        t0 = time.time()
        res = m.predict(bgr, conf=INFER_FLOOR, imgsz=_state["imgsz"],
                        device=_state["device"], verbose=False)[0]
        ms = (time.time() - t0) * 1000.0
    dets = [{"name": names[int(b.cls[0])],
             "score": float(b.conf[0]),
             "box": [float(v) for v in b.xyxy[0]]}
            for b in res.boxes]
    dets.sort(key=lambda d: -d["score"])
    return dets, round(ms, 1)


# ── spoken result announcement ────────────────────────────────────────────
# Played on the Jetson rather than in the page: the through-beam sensor will trigger
# checks with no user gesture behind them, and browsers block autoplay in exactly that
# case. gst-launch is the player because GStreamer is already a dependency here (the
# camera pipeline) and ships avdec_mp3 — this box has no mpg123, ffplay or mpv, and
# SoX was built without an MP3 handler.
_audio_q: "queue.Queue" = queue.Queue(maxsize=AUDIO_QUEUE_MAX)
# Outcome of the most recent Jetson playback: None until something has been played. The
# IT heartbeat reports it as speaker_dead only on a gate with NO tower — with a tower the
# voices are the tower's, and its health comes from the light refresher every second.
# This one learns only on a real announcement, so a quiet spell hides a dead speaker.
_audio_health = {"ok": None, "t": 0.0, "error": ""}


def _audio_result(ok: bool, error: str = "") -> None:
    _audio_health.update(ok=ok, t=time.time(), error=error)


def audio_loop():
    while not _state.get("stop"):
        path = _audio_q.get()
        if path is None:
            return
        try:
            # A nonzero exit here has historically gone completely unnoticed: gst-launch
            # can fail to reach an audio sink (wrong/missing XDG_RUNTIME_DIR when this
            # runs under systemd rather than a login session — GStreamer then silently
            # falls back from pulsesink to a raw alsasink instead of erroring) and this
            # loop would just report nothing, so a mute Jetson looked identical to a
            # healthy one in every log. -q still suppresses gst's own chatter on stdout,
            # but stderr is now captured so a real failure has something to show.
            proc = subprocess.run(
                ["gst-launch-1.0", "-q", "playbin", "uri=" + pathlib.Path(path).as_uri()],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                timeout=_state["audio_timeout"])
            if proc.returncode != 0:
                print(f"[audio] {os.path.basename(path)} exited {proc.returncode}: "
                      f"{proc.stderr.strip()[-300:]}", flush=True)
                _audio_result(False, f"exit {proc.returncode}")
            else:
                _audio_result(True)
        except subprocess.TimeoutExpired:
            print(f"[audio] {os.path.basename(path)} did not finish in "
                  f"{_state['audio_timeout']}s", flush=True)
            _audio_result(False, "timeout")
        except Exception as e:
            print(f"[audio] could not play {os.path.basename(path)}: {e}", flush=True)
            _audio_result(False, str(e))


def announce(res: dict):
    """Voice and light for a finished check. Never blocks the caller.

    The operator's definitions (2026-09-30):
      PASS       「檢測通過」 (spoken since 2026-09-30 evening; it was silent before) and the
                 tower flashes green for the pass window — the channel is open for this
                 worker until they cross the door band's far edge (the image trigger ends
                 it there) or IMAGE_TRIGGER_COOLDOWN_PASS_S runs out.
      FAIL       「檢測未通過」 and a red flash.
      NO_WORKER  silent, light unchanged. The trigger fired but the burst found nobody —
                 a detection problem to look at in See Records, not an instruction a
                 worker could act on.
    """
    status = res.get("status")
    # An image-trigger check goes to IT at the verdict, not when the visit ends: IT's
    # reply says whether this worker is entering or leaving (operator, 2026-10-01).
    to_it = (res.get("source") == "image" and status in ("PASS", "FAIL")
             and _it is not None and _it.running)
    if status == "PASS":
        signal("green_flash", IMAGE_TRIGGER_COOLDOWN_PASS_S, tag="pass")
        if to_it:
            state, done = _post_check_now(res)
        if res.get("source") == "image" and direction_source() == "track":
            # Our track already knows the direction — no need to wait for IT's reply.
            at = {"in": "entry", "out": "leave"}.get(res.get("intent"))
            say(_pass_voice(at, "track"))
            if at:
                _show_pass_direction(at)
            return
        if not to_it or not pass_voice_says_direction("it"):
            # Nothing to wait for: no IT answer is coming, or the words no longer depend
            # on it (the post above still runs and still sets the visit's direction).
            say("pass")
            return

        def speak():
            # Wait briefly for IT's direction; with none (IT slow or down) say the plain
            # words — a PASS is never left unannounced for want of a reply.
            done.wait(IT_VOICE_WAIT_S)
            at = state.get("access_type") if state.get("state") == "sent" else None
            say(_pass_voice(at, "it"))
        threading.Thread(target=speak, daemon=True, name="pass-voice").start()
    elif status == "FAIL":
        say_to("fail", res.get("track_id"))
        signal("red_flash")
        if to_it:
            _post_check_now(res)


def play_clip(clip: str) -> None:
    """Queue one MP3 from --mp3-dir by name. Never blocks the caller."""
    if not _state.get("audio"):
        return
    path = os.path.join(_state["mp3_dir"], clip)
    if not os.path.isfile(path):
        print(f"[audio] missing clip: {path}", flush=True)
        return
    try:
        _audio_q.put_nowait(path)
    except queue.Full:
        print("[audio] player is behind — skipped one announcement", flush=True)


# ── capture recording (Phase 3: on-site dataset collection) ───────────────
# Every gate trigger keeps the frame the vote judged best, its detections as a YOLO
# label file, and one CSV row. Writes happen on a background thread: a tap already
# costs ~0.95 s and disk I/O has no business being inside it.
_rec_q: "queue.Queue" = queue.Queue(maxsize=RECORD_QUEUE_MAX)


def yolo_label_lines(dets, w, h, name_to_idx, conf, primary=None) -> list[str]:
    """Detections as YOLO label rows: `cls cx cy bw bh`, normalised and clamped.

    By default every box is written, not just the primary worker's. A label file that
    covers one worker in a frame containing six teaches the model that the other five
    hardhats are background — the opposite of what this dataset is being collected for.
    Which person the checklist actually judged is recorded in the CSV instead.

    `primary` (--label-primary-only) restricts the file to the closest person and the
    PPE contained in them, using the checklist's own containment rule. Correct only if
    the frames are single-worker; otherwise it trains against the extra people.
    """
    lines = []
    for d in dets:
        if d["score"] < conf:
            continue
        if primary is not None:
            if d["box"] is not primary and \
                    containment(d["box"], primary) < _state["cfg"]["containment"]:
                continue
        idx = name_to_idx.get(d["name"])
        if idx is None:                      # class the current model has, config does not
            continue
        x1, y1, x2, y2 = d["box"]
        cx, cy = (x1 + x2) / 2 / w, (y1 + y2) / 2 / h
        bw, bh = (x2 - x1) / w, (y2 - y1) / h
        cx, cy = min(max(cx, 0.0), 1.0), min(max(cy, 0.0), 1.0)
        bw, bh = min(max(bw, 0.0), 1.0), min(max(bh, 0.0), 1.0)
        if bw <= 0 or bh <= 0:
            continue
        lines.append(f"{idx} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
    return lines


def csv_columns(cfg: dict) -> list[str]:
    # The _cy column is the observed vertical position of the item on the worker
    # (0.0 = top of the person box, 1.0 = bottom). Logged on every check whether or not
    # max_center_y is configured, so a threshold can be set from real gate data.
    # "alarm" is what the speaker said for this row, in ALARM_TEXT's words. status is what
    # happened (PASS / FAIL / NO_WORKER, or a refusal: CROWD / MULTI_TAG / NO_TAG /
    # READER_DOWN); alarm is what the worker heard. Two refusals share one announcement,
    # so neither column alone tells the whole story.
    # "direction" is the INTENT the bbox track showed when the check ran (進場 / 出場): which
    # side the worker came from. What they then actually did — went through, turned back —
    # is only known seconds later and lives in events.jsonl (see _visit_resolve).
    # "_score" / "_neg_score" (2026-10-02, operator): the highest score the model gave the
    # item's class / its NO- class on the worker over the burst, whatever the item's conf
    # — so an NG says whether the helmet was seen at 0.62 under a 0.70 bar or not at all.
    # "rfid_tags" (2026-10-02, operator): every badge the reader heard in the window when
    # the row was written, "EPC -58 dBm x12; …", strongest first, "(弱)" = under the
    # one-person floor at that moment. On 「檢測口請淨空」 rows it is who was there.
    # All appended after the columns that existed before, so captures.csv migrates in
    # place (see _write_record) instead of rotating to a .bak.
    cols = ["timestamp", "image", "label", "worker_id", "status", "alarm", "direction"]
    for item in cfg["items"]:
        cols += [item["label"], f"{item['label']}_votes", f"{item['label']}_cy"]
    cols += ["extra_people", "person_box", "boxes"]
    for item in cfg["items"]:
        cols += [f"{item['label']}_score", f"{item['label']}_neg_score"]
    # person_area / person_score (2026-10-03, operator): the judged person's box area in
    # px² and its detection score — what the area threshold and the Score / Tracking
    # thresholds are tuned against. Rows from before have their area backfilled from
    # person_box on migration; their score is unknown and stays blank.
    return cols + ["rfid_tags", "person_area", "person_score"]


def _person_cells(box, score) -> dict:
    """captures.csv's person_area / person_score cells for one person box."""
    return {"person_area": int(_area(box)) if box else "",
            "person_score": "" if score is None else round(float(score), 3)}


def _tags_text(rows) -> str:
    """captures.csv's rfid_tags cell for the reader rows (summary() / closest() shape:
    epc, rssi = peak dBm in the window, reads), strongest first."""
    if not rows:
        return ""
    floor = _state["cfg"].get("rfid_min_rssi", RFID_MIN_RSSI_DEFAULT)
    multi = _rfid is not None and len(_rfid.antennas) > 1

    def ants(r) -> str:
        # Per-antenna peaks, only with several antennas: what calibrating them needs.
        a = r.get("ants") or {}
        return (" [" + " / ".join(f"A{k} {v:g}" for k, v in a.items()) + "]") if multi and a else ""
    return "; ".join(f"{r['epc']} {r['rssi']:g} dBm x{r['reads']}{ants(r)}"
                     + (" (弱)" if r["rssi"] < floor else "")
                     for r in sorted(rows, key=lambda r: -r["rssi"]))


def _prune(rows: list[dict], cap: int, base: str) -> list[dict]:
    """Keep the newest `cap` rows and delete the files the dropped ones point at.

    cap <= 0 means no limit — the default. At the measured 172 KB/capture (see
    ppe-dataset-collection memory note) even years of gate traffic stays well inside
    the 71 GB free on this device; the record count is not the thing that needed
    bounding. --record-max N re-enables the FIFO ring for anyone who wants one.
    """
    if cap <= 0 or len(rows) <= cap:
        return rows
    for old in rows[:len(rows) - cap]:
        for key in ("image", "label"):
            f = os.path.join(base, old.get(key, ""))
            try:
                if old.get(key) and os.path.isfile(f):
                    os.remove(f)
            except OSError:
                pass
    return rows[len(rows) - cap:]


def _record_paths(rec: dict) -> tuple[str, str]:
    """(image, label) paths, relative to record_dir, where _write_record will put `rec`.

    One function so the path a caller is TOLD (queue_record puts it on the result, so a
    visit event can point at the check's picture) is the path actually written.
    """
    stem = rec["stem"]
    if rec.get("alarm_only"):
        # A refusal's or violation's frame: evidence for whoever reviews the alarm, never
        # training data. Its boxes are exactly the ones that may be wrong (a phantom
        # second person), and labelling them would train the mistake straight back in.
        return os.path.join("dataset", "alarms", "images", stem + ".jpg"), ""
    if rec["has_person"]:
        # "train" split subfolder so dataset/{images,labels} is plain YOLO layout
        # (images/train + labels/train) and can be merged straight into an external
        # training platform's dataset root without any reshuffling.
        return (os.path.join("dataset", "images", "train", stem + ".jpg"),
                os.path.join("dataset", "labels", "train", stem + ".txt"))
    return os.path.join("dataset", "no_person", "images", stem + ".jpg"), ""


def _write_record(rec: dict):
    """Write one triggered capture, routed by whether a worker was actually found.

    A capture with a worker is training data: image + YOLO label under
    dataset/images/train and dataset/labels/train (plain YOLO split layout, so this
    folder merges straight into an external training platform's dataset root).
    A capture WITHOUT one goes to dataset/no_person/ as an image alone, deliberately with
    no .txt beside it. An empty label is not "no information" to YOLO — it asserts the
    whole frame is background, so if a worker really was there and the model missed them,
    a label file would train that miss straight back in. Keeping the image but withholding
    the label makes the folder a review pile: it cannot be merged into training until a
    human has looked at the frames and labelled anything real.

    A refusal (queue_alarm_record) goes to dataset/alarms/, image only, for the same reason.

    All three kinds get a captures.csv row, so the log stays a complete record of what the
    gate did. _prune() deletes whatever paths a row names, so it spans every folder already.
    """
    base = _state["record_dir"]
    stem = rec["stem"]
    has_person = rec["has_person"]

    img_rel, lbl_rel = _record_paths(rec)
    os.makedirs(os.path.join(base, os.path.dirname(img_rel)), exist_ok=True)
    if lbl_rel:
        os.makedirs(os.path.join(base, os.path.dirname(lbl_rel)), exist_ok=True)

    # The JPEG is the exact q88 buffer the burst already encoded — no re-encode.
    with open(os.path.join(base, img_rel), "wb") as fh:
        fh.write(rec["jpeg"])
    if lbl_rel:
        with open(os.path.join(base, lbl_rel), "w") as fh:
            fh.write("\n".join(rec["lines"]) + ("\n" if rec["lines"] else ""))

    cols = csv_columns(_state["cfg"])
    path = _state["record_csv"]
    rows = []
    if os.path.isfile(path):
        try:
            with open(path, newline="") as fh:
                rd = csv.DictReader(fh)
                old = list(rd.fieldnames or [])
                if old == cols:
                    rows = list(rd)
                elif old and [c for c in cols if c in old] == old:
                    # The schema only GREW — every existing column is still there, in the
                    # same order, and something new was added. DictReader already maps by
                    # name, so the old rows carry over with the new cells blank. Rotating
                    # the file away here (the branch below) would empty See Records for
                    # the sake of one extra column.
                    rows = list(rd)
                    added = [c for c in cols if c not in old]
                    if "alarm" in added:
                        # Rows written before the column existed were checks, and each was
                        # announced with the clip its status implies — so this is what the
                        # worker heard, not a guess.
                        for r in rows:
                            key = _STATUS_ALARM.get(r.get("status", ""))
                            if key:
                                r["alarm"] = ALARM_TEXT[key][0]
                    if "person_area" in added:
                        # The box was always recorded; its area is just arithmetic on it.
                        for r in rows:
                            try:
                                x1, y1, x2, y2 = map(float, (r.get("person_box") or "").split())
                                r["person_area"] = int(_area([x1, y1, x2, y2]))
                            except ValueError:
                                pass
                    print(f"[record] captures.csv gained column(s) {added} — "
                          f"{len(rows)} existing rows kept", flush=True)
                else:
                    # The checklist changed, so old rows no longer line up. Keep them as
                    # data rather than silently writing misaligned columns underneath.
                    shutil.move(path, path + ".{}.bak".format(time.strftime("%Y%m%d%H%M%S")))
                    print(f"[record] checklist changed — previous CSV kept as {path}.*.bak",
                          flush=True)
        except Exception as e:
            print(f"[record] could not read {path} ({e}); starting a new one", flush=True)

    row = dict(rec["row"])
    row["image"], row["label"] = img_rel, lbl_rel
    rows.append(row)
    if _state["record_max"] > 0:
        rows = _prune(rows, _state["record_max"], base)

    tmp = path + ".part"
    with open(tmp, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(rows)
    os.replace(tmp, path)
    cap_str = f"{len(rows)}/{_state['record_max']}" if _state["record_max"] > 0 else str(len(rows))
    where = ("dataset/alarms/ (evidence, not training)" if rec.get("alarm_only")
             else "dataset/images(+labels)/train/" if has_person
             else "dataset/no_person/ (image only, unlabelled)")
    print(f"[record] {stem} worker={row['worker_id'] or '(blank)'} "
          f"{row['status']} boxes={row['boxes']} -> {where} ({cap_str})", flush=True)


def _write_event(ev: dict) -> None:
    """Append one resolved visit to events.jsonl — the IT outbox and the audit trail of
    every in/out decision. Append-only JSON lines: a crash can lose at most the line
    being written, never the file, and the future curl sender can resume from an offset.
    """
    path = os.path.join(_state["record_dir"], "events.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


def recorder_loop():
    while not _state.get("stop"):
        rec = _rec_q.get()
        if rec is None:
            return
        try:
            if "event" in rec:
                _write_event(rec["event"])
            else:
                _write_record(rec)
        except Exception as e:
            print(f"[record] FAILED to save {'event' if 'event' in rec else 'capture'}: {e}",
                  flush=True)


# ── realtime collection mode ──────────────────────────────────────────────
# A continuous detect-and-collect mode, separate from the gate trigger. It shows live
# boxes and a per-frame OK/NG summary, and quietly harvests training frames while
# somebody stands in front of the camera. Deliberately NOT a gate check: it plays no
# announcement and writes no captures.csv row, because it is not a pass/fail event that
# happened at the gate — it is a sampling session.
_realtime = {"jpeg": None, "last_save": 0.0, "saved": 0, "lock": threading.Lock()}

# Set by main() when --rfid-host is given. None means the gate runs without identity,
# which is the default and a fully supported mode: the PPE check never depends on it.
_rfid = None

# On-site recording for replay (scripts/recorder.py): the camera stream plus a log of
# what the gate saw and decided. Set by main() when the camera is on; idle until the
# page's 錄影 button starts it.
_recorder = None


def _recording_meta() -> dict:
    """The settings in force when a recording starts — what a replay must run with."""
    cfg = _state["cfg"]
    keys = ("model", "person_class", "person_conf", "track_person_conf", "trigger_min_area",
            "trigger_zone", "door_side", "exit_area_fraction", "away_fraction", "image_dwell",
            "frames", "burst_before", "rfid_min_rssi", "direction_source")
    f = latest_frame()
    return {"settings": {k: cfg.get(k) for k in keys}, "items": cfg.get("items"),
            "gate_frame": [f.shape[1], f.shape[0]] if f is not None else None,
            "tick_s": IMAGE_TRIGGER_PERIOD_S, "burst_interval": _state.get("burst_interval"),
            "rfid": _rfid.settings() if _rfid else None}


def _rec_log(kind: str, **fields) -> None:
    """One line in the active recording's log; nothing (and next to no cost) otherwise."""
    if _recorder is not None and _recorder.active:
        _recorder.log(kind, **fields)


def model_folder_name() -> str:
    """A filesystem-safe folder name for the currently active model, e.g.
    'lite_0902_0801' from '.../models/lite_0902_0801.engine'. Collapses anything
    outside [A-Za-z0-9_-] to '_' — not just tidiness, this is what makes it safe to
    join straight into a filesystem path with no traversal risk, regardless of what a
    model file ends up named."""
    stem = os.path.splitext(os.path.basename(_state.get("model_path") or "model"))[0]
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")
    return safe or "model"


def realtime_dirs() -> tuple[str, str]:
    # Per-active-model subfolder, THEN the "train" split — dataset/realtime/<model>/
    # images/train + labels/train. The per-model split matters for correctness, not
    # just tidiness: a YOLO label file is class INDICES, not names, and two different
    # models almost never agree on what index 0 means. Capturing model A's and model
    # B's realtime samples into the same folder would silently produce a dataset where
    # half the labels are lying — no error anywhere, just wrong training data. See
    # write_realtime_classes_yaml() for the other half of the fix: a yaml recording
    # which names those indices actually mean, saved alongside.
    base = os.path.join(_state["record_dir"], "dataset", "realtime", model_folder_name())
    return os.path.join(base, "images", "train"), os.path.join(base, "labels", "train")


def write_realtime_classes_yaml(img_dir: str, names: dict) -> None:
    """Write dataset/realtime/<model>/classes.yaml once per model folder, recording the
    index -> name mapping the label files in that folder were written against.

    Written next to images/train (one level up), not inside it — a stray .yaml there
    is harmless for training (loaders read images/labels by extension) but keeps the
    folder self-describing without digging through gate.json or server logs to figure
    out, months later, which model's vocabulary a given batch of labels used.

    Not named the same as a paired class-override yaml (<model-stem>.yaml next to a
    model file, see apply_yaml_override) — that mechanism RENAMES a model's classes;
    this one just RECORDS them, and living in a different directory entirely, the two
    can never be confused for each other.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(img_dir)), "classes.yaml")
    if os.path.isfile(path):
        return                      # written once per folder; the model behind a given
                                     # folder name doesn't change class order mid-run
    try:
        with open(path, "w") as fh:
            yaml.safe_dump({"names": dict(names), "nc": len(names)}, fh,
                           default_flow_style=False, sort_keys=False)
    except OSError as e:
        print(f"[record] could not write {path}: {e}", flush=True)


def prune_realtime(cap: int):
    """Keep the newest `cap` realtime samples, deleting the oldest image+label pairs.

    Filenames are timestamp-prefixed, so lexical order IS chronological order — which is
    why this can prune without an index. The trigger recorder prunes from captures.csv
    instead, and realtime writes no CSV rows, so it needs its own ceiling or nothing
    would ever bound it.
    """
    if cap <= 0:
        return
    img_dir, lbl_dir = realtime_dirs()
    try:
        names = sorted(f for f in os.listdir(img_dir) if f.endswith(".jpg"))
    except OSError:
        return
    for name in names[:max(0, len(names) - cap)]:
        stem = os.path.splitext(name)[0]
        for path in (os.path.join(img_dir, name), os.path.join(lbl_dir, stem + ".txt")):
            try:
                os.remove(path)
            except OSError:
                pass


def save_realtime_sample(bgr, dets: list[dict], res: dict) -> bool:
    """Write one realtime frame + YOLO label. Returns whether it was written.

    Same no-worker rule as the trigger recorder: an empty label asserts the whole image
    is background, so a frame where the model missed a present worker would train that
    miss back in.
    """
    if res.get("status") == "NO_WORKER" or not res.get("person_box"):
        return False
    img_dir, lbl_dir = realtime_dirs()
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(lbl_dir, exist_ok=True)

    with _lock:
        names = _model.names
    write_realtime_classes_yaml(img_dir, names)
    name_to_idx = {n: i for i, n in names.items()}
    primary = None
    if _state.get("label_primary_only"):
        idx = res.get("person_index")
        if idx is not None and 0 <= idx < len(dets):
            primary = dets[idx]["box"]
    h, w = bgr.shape[:2]
    lines = yolo_label_lines(dets, w, h, name_to_idx, _state["label_conf"], primary)

    # Encoded here at full capture quality, independently of the low-quality preview the
    # browser is shown — the dataset gets the good pixels.
    ok, enc = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), CAPTURE_JPEG_QUALITY])
    if not ok:
        return False
    stem = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    with open(os.path.join(img_dir, stem + ".jpg"), "wb") as fh:
        fh.write(enc.tobytes())
    with open(os.path.join(lbl_dir, stem + ".txt"), "w") as fh:
        fh.write("\n".join(lines) + ("\n" if lines else ""))
    prune_realtime(_state["realtime_max"])
    return True


def queue_record(res: dict, worker_raw: str):
    """Hand one triggered capture to the writer thread. Never blocks the gate.

    A capture with no worker in it IS recorded, but its image goes to dataset/no_person/
    with no label file (see _write_record). An empty label is not "no information" to
    YOLO — it asserts the whole image is background — so if the model simply missed a
    person who was there, a label would train that miss straight back in.

    Sets res["record_image"] to the path the image will be written at, so a visit event
    resolved seconds later can point at this check's picture.
    """
    if not _state.get("record"):
        return
    has_person = bool(res.get("person_box")) and res.get("status") != "NO_WORKER"
    frame = _frames.get(res.get("frame_id"))
    if not frame:
        return
    with _lock:
        names = _model.names
    name_to_idx = {n: i for i, n in names.items()}
    primary = None
    if _state.get("label_primary_only"):
        # CheckResult carries the primary's exact index, so take the box from there
        # rather than re-deriving it by comparing rounded coordinates.
        idx = res.get("person_index")
        if idx is not None and 0 <= idx < len(frame["dets"]):
            primary = frame["dets"][idx]["box"]
    lines = (yolo_label_lines(frame["dets"], frame["w"], frame["h"],
                              name_to_idx, _state["label_conf"], primary)
             if has_person else [])
    row = {
        "timestamp": res["ts"],
        "worker_id": worker_raw or "",
        "status": res["status"],
        "alarm": ALARM_TEXT[_STATUS_ALARM.get(res["status"], "fail")][0],
        "direction": INTENT_TEXT.get(res.get("intent"), ""),
        "extra_people": res.get("extra_people", 0),
        "person_box": " ".join(str(v) for v in res["person_box"]) if res.get("person_box") else "",
        "boxes": len(lines),
        "rfid_tags": _tags_text((res.get("rfid") or {}).get("candidates")),
    }
    idx = res.get("person_index")
    dets = res.get("detections") or []
    row.update(_person_cells(res.get("person_box"),
                             dets[idx]["score"] if idx is not None and 0 <= idx < len(dets)
                             else None))
    for item in res.get("items", []):
        # A switched-off item still records what was seen, but marked so a later reader
        # of the CSV can't mistake its NG for one that failed the worker — the status
        # column says PASS while this says NG, and "(off)" is why.
        verdict = "OK" if item["ok"] else "NG"
        row[item["label"]] = verdict if item.get("enabled", True) else f"{verdict} (off)"
        row[f"{item['label']}_votes"] = f"{item['votes']}/{item['frames']}"
        cy = item.get("center_y")
        row[f"{item['label']}_cy"] = "" if cy is None else cy
        for key, col in (("seen_score", "_score"), ("neg_score", "_neg_score")):
            v = item.get(key)
            row[item["label"] + col] = "" if v is None else v
    rec = {"stem": time.strftime("%Y%m%d_%H%M%S") + "_" + res["frame_id"][:6],
           "jpeg": frame["jpeg"], "lines": lines, "row": row,
           "has_person": has_person}
    res["record_image"] = _record_paths(rec)[0]
    try:
        _rec_q.put_nowait(rec)
    except queue.Full:
        print("[record] writer is behind — dropped one capture", flush=True)


# A check's status → the ALARM_TEXT entry its announcement came from (announce()).
_STATUS_ALARM = {"PASS": "pass", "FAIL": "fail", "NO_WORKER": "no_worker"}


def queue_alarm_record(status: str, alarm_key: str | None, bgr, worker: str = "",
                       people: int = 0, box=None, alarm_text: str | None = None,
                       direction: str = "", tags: list | None = None,
                       score: float | None = None) -> str:
    """Record a refusal or a violation — an event that ran no check — as a captures.csv
    row, and return the path its image will be written at ("" if nothing was queued).

    Refusals: before this only checks wrote rows, so See Records could not show a single
    「請依序單人進入」: the speaker said things the record never did. Called only where the
    clip actually plays — which, since every refusal speaks, is every refusal.
    Violations are silent (nothing is spoken) but are the rows that matter most, so they
    pass their own alarm_text instead of an announcement key.

    The frame is kept as evidence: a false crowd alarm is diagnosed by looking at it.
    Encoded here on the caller's thread (~20-60 ms) — these are seconds apart at most.

    tags: the reader rows the decision was made on (the RFID refusals pass theirs); left
    out, it is the badges heard in the last rfid_before seconds as of now — for a crowd,
    a violation or an unregistered badge, who was at the gate.
    """
    _rec_log("alarm", status=status, alarm=alarm_text or ALARM_TEXT.get(alarm_key, ("",))[0],
             worker=worker, people=people, box=[int(v) for v in box] if box else None)
    if not _state.get("record") or bgr is None:
        return ""
    if tags is None and _rfid is not None:
        tags = _rfid.obs.summary(time.monotonic(), _state["rfid_before"])
    ok, enc = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), CAPTURE_JPEG_QUALITY])
    if not ok:
        return ""
    row = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "worker_id": worker,
        "status": status,
        "alarm": alarm_text if alarm_text is not None else ALARM_TEXT[alarm_key][0],
        "direction": direction,
        "extra_people": max(0, people - 1),
        "person_box": " ".join(str(round(v, 1)) for v in box) if box else "",
        "boxes": "",
        "rfid_tags": _tags_text(tags),
        **_person_cells(box, score),
    }
    rec = {"stem": time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6],
           "jpeg": enc.tobytes(), "lines": [], "row": row,
           "has_person": False, "alarm_only": True}
    try:
        _rec_q.put_nowait(rec)
    except queue.Full:
        print("[record] writer is behind — dropped one alarm record", flush=True)
        return ""
    return _record_paths(rec)[0]


MODEL_EXTS = (".pt", ".engine", ".onnx")


YAML_EXTS = (".yaml", ".yml")


def find_paired_yaml(model_path: str) -> str | None:
    """A same-stem .yaml/.yml next to a model file, if one exists (e.g. helmet.pt +
    helmet.yaml). This is also where an explicit "attach a class list" upload lands,
    so pairing-by-filename and pairing-by-attachment are the same mechanism."""
    stem = os.path.splitext(model_path)[0]
    for ext in YAML_EXTS:
        cand = stem + ext
        if os.path.isfile(cand):
            return cand
    return None


def class_names_from_yaml_doc(doc) -> list[str]:
    """Pull an ordered class list out of an already-parsed YOLO-style data.yaml.
    Ultralytics itself writes either convention — a plain list, or a dict keyed by
    index — so both are accepted; a dict is sorted by its integer key to recover the
    intended order."""
    if not isinstance(doc, dict):
        raise ValueError("not a YAML mapping")
    names = doc.get("names")
    if names is None:
        raise ValueError("no 'names:' field")
    if isinstance(names, dict):
        return [v for _, v in sorted((int(k), v) for k, v in names.items())]
    if isinstance(names, list):
        if not names:
            raise ValueError("'names:' is empty")
        return list(names)
    raise ValueError("'names:' must be a list or a dict")


def read_yaml_class_names(yaml_path: str) -> list[str]:
    """class_names_from_yaml_doc(), reading the yaml off disk first."""
    with open(yaml_path) as fh:
        doc = yaml.safe_load(fh) or {}
    return class_names_from_yaml_doc(doc)


def apply_yaml_override(model, model_path: str) -> str | None:
    """If model_path has a paired yaml with the same class COUNT as the model, replace
    the model's own embedded names with the yaml's. Returns a warning string when a
    paired yaml exists but is NOT applied (bad yaml, or a class-count mismatch); None
    when there is nothing to warn about (no yaml, or the swap succeeded).

    Count-matching is the safety rule: a yaml is trusted to relabel a model only when
    that cannot desync an index from the checkpoint's own true class order. A
    mismatched count means the wrong yaml is paired, or the model doesn't match it —
    using it anyway would silently rename detections rather than fixing them.
    """
    yp = find_paired_yaml(model_path)
    if not yp:
        return None
    try:
        names = read_yaml_class_names(yp)
    except Exception as e:
        return f"paired yaml {os.path.basename(yp)} unreadable ({e}) — ignoring it"
    if len(names) != len(model.names):
        return (f"paired yaml {os.path.basename(yp)} has {len(names)} class(es), "
                f"model reports {len(model.names)} — ignoring the yaml")

    # YOLO.names is a read-only property on a PyTorch model (it proxies the inner
    # nn.Module), while a .engine/.onnx AutoBackend exposes a plain settable attribute.
    # So write through to whichever inner objects exist, then verify through the public
    # property rather than trusting any single assignment to have taken.
    new = {i: n for i, n in enumerate(names)}
    for target in (getattr(model, "model", None),
                   getattr(getattr(model, "predictor", None), "model", None)):
        if target is None:
            continue
        try:
            target.names = new
        except Exception:
            pass
    try:
        model.names = new
    except Exception:
        pass
    if dict(model.names) != new:
        return (f"paired yaml {os.path.basename(yp)} could not be applied — this build "
                f"of ultralytics does not allow relabelling that model type")
    print(f"[model] class names overridden from {os.path.basename(yp)}", flush=True)
    return None


def required_classes(cfg: dict) -> list[str]:
    """Every class name the checklist needs a model to provide."""
    want = {cfg["person_class"]}
    for item in cfg["items"]:
        want.update(item.get("classes", []))
        want.update(item.get("negatives", []))
    return sorted(want)


def list_models() -> list[dict]:
    """Filesystem view of the models directory — no model file is ever loaded here to
    build it, only (optionally) a tiny paired yaml.

    Loading a model to read its own embedded names would work for a .pt or .engine
    (both carry it reliably — confirmed on this device), but a bare .onnx from another
    pipeline can lack it entirely, and worse, ultralytics can react to a missing/odd
    ONNX runtime by silently reinstalling packages — that is what downgraded numpy
    on this box once already (see the jetson-numpy1-pin note). So a class-list preview
    here comes ONLY from a paired yaml, never from opening the model file; the active
    model's real names (already resident in memory, zero extra cost) are the one
    exception, and compatibility is otherwise still settled properly at load time.
    """
    d = _state["models_dir"]
    out = []
    for name in sorted(os.listdir(d)):
        if not name.lower().endswith(MODEL_EXTS):
            continue
        full = os.path.join(d, name)
        if not os.path.isfile(full):
            continue
        st = os.stat(full)
        is_active = os.path.realpath(full) == os.path.realpath(_state["model_path"])
        yp = find_paired_yaml(full)
        classes, yaml_warning = None, None
        if is_active:
            with _lock:
                classes = sorted(_model.names.values())
        elif yp:
            try:
                classes = sorted(read_yaml_class_names(yp))
            except Exception as e:
                yaml_warning = f"{os.path.basename(yp)} unreadable: {e}"
        out.append({
            "file": name,
            "format": os.path.splitext(name)[1].lstrip("."),
            "size_mb": round(st.st_size / MB, 1),
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "active": is_active,
            "yaml": os.path.basename(yp) if yp else None,
            "classes": classes,
            "yaml_warning": yaml_warning,
        })
    return out


def resolve_model(file: str) -> str:
    """Map a browser-supplied filename to a path, refusing anything outside models/."""
    if not file or "/" in file or "\\" in file:
        raise ValueError("Model must be a plain filename inside the models directory")
    # The no-separator rule above already makes traversal impossible, so normalise with
    # abspath rather than realpath — realpath would resolve symlinks and reject a models
    # directory that legitimately links its weights in from elsewhere.
    full = os.path.abspath(os.path.join(_state["models_dir"], file))
    if os.path.dirname(full) != os.path.abspath(_state["models_dir"]):
        raise ValueError("Model must be a plain filename inside the models directory")
    if not os.path.isfile(full):
        raise FileNotFoundError(f"No such model: {file}")
    if not full.lower().endswith(MODEL_EXTS):
        raise ValueError(f"Not a model file: {file}")
    return full


def safe_model_name(name: str) -> str:
    """Sanitise a browser-supplied filename down to something writable in models/."""
    base = os.path.basename((name or "").strip().replace("\\", "/"))
    if not base or base.startswith("."):
        raise ValueError("Model needs a filename")
    if not base.lower().endswith(MODEL_EXTS):
        raise ValueError(f"'{base}' is not a model file — expected .pt, .engine or .onnx")
    return base


def switch_model(file: str, force: bool = False) -> dict:
    """Load a different model and make it live, or refuse and keep the current one.

    The check that matters is the class list, not the architecture: ultralytics loads
    v3/v5/v6/v8/v9/v10/v11 and RT-DETR through the same call, but a model that lacks
    'Hardhat' loads perfectly and then reports NG on every worker forever. That failure
    is invisible on the kiosk screen, so it is refused here unless explicitly forced.
    """
    from ultralytics import YOLO

    full = resolve_model(file)
    if os.path.realpath(full) == os.path.realpath(_state["model_path"]):
        return {"file": file, "switched": False, "reason": "already active"}

    _state["swapping"] = True
    try:
        t0 = time.time()
        cand = YOLO(full)
        # Warm outside the lock so live traffic keeps using the old model meanwhile —
        # and BEFORE the yaml override / class check, for the same reason as in
        # load_model(): a .engine has no names table to override until its first
        # predict(), so the order below is what makes a renaming yaml actually take,
        # and what makes the "does this model have the classes we need" check judge
        # the RENAMED names rather than the embedded ones.
        cand.predict(np.zeros((_state["imgsz"], _state["imgsz"], 3), dtype=np.uint8),
                     conf=INFER_FLOOR, imgsz=_state["imgsz"],
                     device=_state["device"], verbose=False)
        yaml_warn = apply_yaml_override(cand, full)
        if yaml_warn:
            print(f"[model] WARNING: {yaml_warn}", flush=True)
        names = sorted(cand.names.values())
        missing = [c for c in required_classes(_state["cfg"]) if c not in names]
        if missing and not force:
            raise ValueError(
                "Model does not provide the classes this checklist needs: "
                + ", ".join(missing)
                + f". It has: {', '.join(names[:12])}"
                + ("…" if len(names) > 12 else "")
            )
        global _model
        with _lock:
            _model = cand
            _state["model_path"] = full
        secs = round(time.time() - t0, 1)
        print(f"[model] switched to {file} in {secs}s "
              f"({len(names)} classes){' [FORCED, missing: ' + ', '.join(missing) + ']' if missing else ''}",
              flush=True)
        # Remembered next to the checklist it was validated against, so a restart
        # brings back the model the operator chose — not whatever --model the unit
        # file happened to pin (see main(): the flag is now only the fallback).
        _state["cfg"]["model"] = file
        save_config()
        return {"file": file, "switched": True, "load_seconds": secs,
                "classes": names, "missing": missing, "yaml_warning": yaml_warn}
    finally:
        _state["swapping"] = False


def save_config():
    """Write _state["cfg"] back to the file it was loaded from, atomically.

    _state["cfg"] IS the parsed config file's own structure (load_config() merges
    defaults into it, but every key this file already had stays exactly as it was),
    so dumping it back reproduces the file with only the deliberately-changed fields
    different — not a partial or reformatted rewrite.
    """
    path = _state.get("config_path")
    if not path:
        return                                    # no --config given at startup; nothing to persist to
    tmp = path + ".part"
    with open(tmp, "w") as fh:
        json.dump(_state["cfg"], fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def set_item_class(label: str, cls: str, negative) -> dict:
    """Remap one checklist item's positive (and optionally negative/veto) class to a
    different name from the ACTIVE model's own class list, live and persisted.

    Restricted to the active model's real names (not an arbitrary string) so a typo
    can't silently create an item that will never match anything. `negative` may be
    None or "" for "ignored — no veto", matching a model that has no NO-* equivalent.
    """
    item = next((i for i in _state["cfg"]["items"] if i["label"] == label), None)
    if item is None:
        raise ValueError(f"No checklist item called '{label}'")
    cls = cls.strip()
    if not cls:
        raise ValueError("A positive class must be chosen")
    with _lock:
        available = set(_model.names.values())
    if cls not in available:
        raise ValueError(f"'{cls}' is not a class the active model provides")
    neg = (negative or "").strip()
    if neg and neg not in available:
        raise ValueError(f"'{neg}' is not a class the active model provides")

    item["classes"] = [cls]
    item["negatives"] = [neg] if neg else []
    save_config()
    print(f"[config] {label}: classes={item['classes']} negatives={item['negatives']} "
          f"(saved to {_state.get('config_path')})", flush=True)
    return {"label": label, "classes": item["classes"], "negatives": item["negatives"]}


def set_item_label(label: str, new_label: str) -> dict:
    """Rename one checklist item — the heading the kiosk shows for it — live and persisted.

    The label is the item's identity everywhere in this program: the key the UI's
    remap/threshold calls address it by, the per-item column names in captures.csv
    (csv_columns), and what the operator reads on the screen. So renaming has one
    consequence worth knowing: the next recorded capture sees a CSV whose header no
    longer matches, and _write_record() rotates the old file to captures.csv.<ts>.bak
    and starts a fresh one — existing rows are kept, not lost, but they live in the
    .bak from then on. That's the existing behaviour for any checklist change; this
    just adds a new way to trigger it.

    Uniqueness is enforced because two items with the same label would be
    indistinguishable to every label-keyed lookup above.
    """
    item = next((i for i in _state["cfg"]["items"] if i["label"] == label), None)
    if item is None:
        raise ValueError(f"No checklist item called '{label}'")
    new = " ".join(str(new_label or "").split())      # trim + collapse inner whitespace
    if not new:
        raise ValueError("A name is required")
    if len(new) > 40:
        raise ValueError("Keep the name under 40 characters")
    if new == label:
        return {"label": label, "changed": False}
    if any(i["label"] == new for i in _state["cfg"]["items"]):
        raise ValueError(f"There is already an item called '{new}'")
    item["label"] = new
    save_config()
    print(f"[config] renamed item '{label}' -> '{new}' (saved to {_state.get('config_path')})",
          flush=True)
    return {"label": new, "old_label": label, "changed": True}


def set_item_enabled(label: str, enabled: bool) -> dict:
    """Switch one checklist item in or out of the verdict, live and persisted.

    A disabled item is still evaluated and still shown (see ppe_check.evaluate) — it
    just can't fail the worker. This is the operator's fix for an item mapped to a
    class the active model doesn't have, which would otherwise make EVERY check fail
    (the checklist is all-or-nothing): switch it off rather than delete it, and switch
    it back on when a model that knows the class is loaded.

    Refuses to switch off the LAST enabled item. evaluate() already fails closed on an
    empty checklist, so the gate wouldn't open — but it would fail everyone with no
    visible reason, which is the worse of the two silent states. Better to refuse here
    with a message than to let the UI reach it. Unlike a rename, this does not touch
    captures.csv's columns, so no .bak rotation follows.
    """
    item = next((i for i in _state["cfg"]["items"] if i["label"] == label), None)
    if item is None:
        raise ValueError(f"No checklist item called '{label}'")
    enabled = bool(enabled)
    if not enabled:
        others_on = [i for i in _state["cfg"]["items"]
                     if i is not item and i.get("enabled", True)]
        if not others_on:
            raise ValueError("At least one item must stay enabled — the gate needs "
                             "something to judge")
    item["enabled"] = enabled
    save_config()
    print(f"[config] {label}: {'enabled' if enabled else 'DISABLED — not judged'} "
          f"(saved to {_state.get('config_path')})", flush=True)
    return {"label": label, "enabled": enabled}


def set_item_conf(label: str, conf) -> dict:
    """Set one checklist item's confidence threshold, live and persisted.

    Per-item because the right threshold differs by object: a hardhat is large and
    reliably scored, a small object at the same threshold may never clear it. This is
    the same `conf` the checklist votes on AND the one the overlay filters boxes by,
    so raising it visibly drops weak boxes off the picture as well as out of the vote.
    """
    item = next((i for i in _state["cfg"]["items"] if i["label"] == label), None)
    if item is None:
        raise ValueError(f"No checklist item called '{label}'")
    try:
        c = float(conf)
    except (TypeError, ValueError):
        raise ValueError("Threshold must be a number")
    if not (0.0 <= c <= 1.0):
        raise ValueError("Threshold must be between 0 and 1")
    item["conf"] = round(c, 2)
    save_config()
    print(f"[config] {label}: conf={item['conf']} (saved to {_state.get('config_path')})",
          flush=True)
    return {"label": label, "conf": item["conf"]}


def set_person_conf(conf) -> dict:
    """Set the score a detection needs to count as the worker, live and persisted.

    Distinct from the per-item thresholds and more consequential than any of them: this
    one decides whether there is a worker at all. Set it too high and every check reads
    NO_WORKER; too low and a coat on a chair becomes the person the checklist is judged
    on, and every PPE box gets associated to it.
    """
    try:
        c = float(conf)
    except (TypeError, ValueError):
        raise ValueError("Threshold must be a number")
    if not (0.0 <= c <= 1.0):
        raise ValueError("Threshold must be between 0 and 1")
    _state["cfg"]["person_conf"] = round(c, 2)
    save_config()
    print(f"[config] person_conf={_state['cfg']['person_conf']} "
          f"(saved to {_state.get('config_path')})", flush=True)
    return {"person_conf": _state["cfg"]["person_conf"]}


# The person score the visit TRACKER follows (operator, 2026-10-03): lower than
# person_conf so an ID survives a worker walking away or turning their back — the score
# drops before the box gets small, and a visit that loses its ID mid-band ends "unknown"
# and accuses nobody (2026-10-02: walk-outs missed that way). It only FOLLOWS: who is at
# the gate, the crowd rule and the start of a visit all still need person_conf, so a weak
# phantom box can never start a visit (or a 擅自闖入) of its own. Never above person_conf.
TRACK_PERSON_CONF_DEFAULT = 0.6


def track_person_conf() -> float:
    cfg = _state["cfg"]
    return min(float(cfg.get("track_person_conf", TRACK_PERSON_CONF_DEFAULT)),
               float(cfg["person_conf"]))


def set_track_person_conf(conf) -> dict:
    try:
        c = float(conf)
    except (TypeError, ValueError):
        raise ValueError("Threshold must be a number")
    if not (0.05 <= c <= 1.0):
        raise ValueError("Threshold must be between 0.05 and 1")
    _state["cfg"]["track_person_conf"] = round(c, 2)
    save_config()
    print(f"[config] track_person_conf={_state['cfg']['track_person_conf']} "
          f"(in effect {track_person_conf():g}; saved to {_state.get('config_path')})",
          flush=True)
    return {"track_person_conf": _state["cfg"]["track_person_conf"],
            "effective": track_person_conf()}


# Two thresholds the image trigger decides on. Persisted in gate.json like person_conf
# because they change what the gate does, not just what the page shows — but they are
# NOT in ppe_check.DEFAULT_CONFIG: evaluate() never reads them, they belong to the
# trigger layer in this file. main() setdefault()s them into cfg so /api/config always
# carries a value and an old gate.json keeps working untouched.
TRIGGER_MIN_AREA_DEFAULT = 10000    # px² — the web slider's historical default
RFID_MIN_RSSI_DEFAULT = -60.0       # dBm — mid-range for a UHF tag a metre or two out
RFID_RSSI_RANGE = (-100.0, 0.0)
TRIGGER_ZONE_DEFAULT = [0.0, 1.0]   # left/right edges as fractions of frame width:
                                    # the whole frame, i.e. no horizontal restriction
TRIGGER_ZONE_MIN_WIDTH = 0.02


DIRECTION_SOURCES = ("it", "track")


def direction_source() -> str:
    """Who decides whether a worker is entering or leaving (operator's switch, 2026-10-01):
    "it" — the IT service's access_type in its reply to the PASS (the default), or
    "track" — our own bbox track (where the worker came from). Violations — 擅自闖入 /
    闖出 — are judged by us either way; IT posting is the same either way."""
    src = _state["cfg"].get("direction_source", "it")
    return src if src in DIRECTION_SOURCES else "it"


def set_direction_source(src) -> dict:
    if src not in DIRECTION_SOURCES:
        raise ValueError(f"direction source must be one of {', '.join(DIRECTION_SOURCES)}")
    _state["cfg"]["direction_source"] = src
    save_config()
    print(f"[config] direction_source={src} (saved to {_state.get('config_path')})", flush=True)
    return {"direction_source": src}


# Which side of the CAMERA IMAGE the door into the restricted area is on (operator's
# switch, 2026-10-02). The camera faces the worker, so the floor plan's left is usually
# the image's right — the site plan has the door to the left of the check area, which
# puts it on the image's RIGHT. "left" is the default only because it is what the bench
# was built and tested with; set it from what a worker walking to the door actually
# does on the live view, never from the floor plan alone (a camera may mirror).
# "both" (operator, 2026-10-09): the door is at the camera's end, the band's two sides are
# its walls, so a worker leaving can step into the band from EITHER side and one going in
# leaves it by either side. See _is_door.
DOOR_SIDES = ("left", "right", "both")


# Leaving the plant, a worker steps out of the door SIDEWAYS, and a side-on box is only
# ~55-65 % of a front-on one — on the bench that is under AREA_TH, so the visit never
# started and walking out unchecked went unreported (operator, 2026-10-03). A track that
# came from the door's side therefore starts its (出場) visit at this fraction of AREA_TH.
# Entering workers walk up facing the camera and keep the full AREA_TH, and nobody
# arrives from the door's side but someone coming out, so background people gain nothing.
# Above 1 (operator, 2026-10-09: 「出場的 person trigger 面積應該要比進場大」 — a worker
# leaving comes out by the door, near the camera, and walks AWAY, box big → small) it
# raises the bar instead: someone from the door's side must be that much bigger than
# AREA_TH both to start their visit and to be checked (_gate_area). Below 1 the check
# still needs the full AREA_TH, as before — only the visit starts early. The cost above 1:
# a door-side worker who never gets that big is not followed at all, so walking out
# unchecked is not seen for them, as for anyone under the area threshold.
EXIT_AREA_FRACTION_DEFAULT = 0.6
EXIT_AREA_FRACTION_RANGE = (0.2, 2.0)


def _gate_area(d: dict, min_area: float) -> float:
    """How big person box `d` must be to count as AT the gate (the dwell, the crowd rule):
    AREA_TH, or exit_area_fraction of it when above 1 and the track came from the door's
    side. Reads the track's remembered side (_visits[tid]["came_from"]) from earlier ticks."""
    v = _visits.get(d.get("tid")) or {}
    if _is_door(v.get("came_from")):
        return min_area * max(1.0, exit_area_fraction())
    return min_area


# Where "walked away" starts, as a fraction of AREA_TH (of the visit's own peak, for a
# side-on exit — see _away_line). 0.5 suits a long approach path; a shallow room never
# lets a worker get that small: on the bench (2026-10-03 video) a worker walking to the
# back wall bottomed out at 91k against a 110k line for ~0.3 s, turned and came back —
# the exit went uncounted, so the walk back in was not an intrusion either. Operator-set.
AWAY_FRACTION_DEFAULT = 0.5
AWAY_FRACTION_RANGE = (0.3, 0.8)    # operator raised the cap from 0.7 (2026-10-04), knowing
                                    # the cost: a side-on box is ~55-65 % of a front-on one,
                                    # so above ~0.7 a worker turning to the door can read as
                                    # walking off (losing their PASS) — theirs to weigh


# How long one person must stand in the door zone before the check runs — the page's
# 「停留」 (2026-10-07; before that only --image-dwell). gate.json's image_dwell wins over
# --image-dwell, which is only the starting value, the same as `model`. Shorter = faster,
# but a worker still stepping into place gets judged mid-step, and the RFID window looks
# back --rfid-before seconds from the end of the dwell. The live value is _state's.
IMAGE_DWELL_RANGE = (0.3, 5.0)


def _dwell_from(cfg: dict, fallback: float) -> float:
    """The dwell to start with: gate.json's if it holds a usable one, else --image-dwell."""
    lo, hi = IMAGE_DWELL_RANGE
    try:
        sec = float(cfg.get("image_dwell", fallback))
    except (TypeError, ValueError):
        sec = fallback
    return min(max(sec, lo), hi)


def set_image_dwell(seconds) -> dict:
    lo, hi = IMAGE_DWELL_RANGE
    try:
        sec = float(seconds)
    except (TypeError, ValueError):
        raise ValueError("Dwell must be a number of seconds")
    if not lo <= sec <= hi:
        raise ValueError(f"Dwell must be between {lo:g} and {hi:g} s")
    _state["image_dwell"] = _state["cfg"]["image_dwell"] = round(sec, 2)
    save_config()
    print(f"[config] image_dwell={_state['image_dwell']} s "
          f"(saved to {_state.get('config_path')})", flush=True)
    return {"image_dwell": _state["image_dwell"]}


def away_fraction() -> float:
    lo, hi = AWAY_FRACTION_RANGE
    try:
        f = float(_state["cfg"].get("away_fraction", AWAY_FRACTION_DEFAULT))
    except (TypeError, ValueError):
        f = AWAY_FRACTION_DEFAULT
    return min(max(f, lo), hi)


def set_away_fraction(frac) -> dict:
    lo, hi = AWAY_FRACTION_RANGE
    try:
        f = float(frac)
    except (TypeError, ValueError):
        raise ValueError("Fraction must be a number")
    if not lo <= f <= hi:
        raise ValueError(f"Fraction must be between {lo:g} and {hi:g}")
    _state["cfg"]["away_fraction"] = round(f, 2)
    save_config()
    print(f"[config] away_fraction={_state['cfg']['away_fraction']} "
          f"(saved to {_state.get('config_path')})", flush=True)
    return {"away_fraction": _state["cfg"]["away_fraction"]}


def exit_area_fraction() -> float:
    lo, hi = EXIT_AREA_FRACTION_RANGE
    try:
        f = float(_state["cfg"].get("exit_area_fraction", EXIT_AREA_FRACTION_DEFAULT))
    except (TypeError, ValueError):
        f = EXIT_AREA_FRACTION_DEFAULT
    return min(max(f, lo), hi)


def set_exit_area_fraction(frac) -> dict:
    lo, hi = EXIT_AREA_FRACTION_RANGE
    try:
        f = float(frac)
    except (TypeError, ValueError):
        raise ValueError("Fraction must be a number")
    if not lo <= f <= hi:
        raise ValueError(f"Fraction must be between {lo:g} and {hi:g}")
    _state["cfg"]["exit_area_fraction"] = round(f, 2)
    save_config()
    print(f"[config] exit_area_fraction={_state['cfg']['exit_area_fraction']} "
          f"(saved to {_state.get('config_path')})", flush=True)
    return {"exit_area_fraction": _state["cfg"]["exit_area_fraction"]}


def door_side() -> str:
    side = _state["cfg"].get("door_side", "left")
    return side if side in DOOR_SIDES else "left"


def set_door_side(side) -> dict:
    if side not in DOOR_SIDES:
        raise ValueError(f"door side must be one of {', '.join(DOOR_SIDES)}")
    _state["cfg"]["door_side"] = side
    save_config()
    print(f"[config] door_side={side} (saved to {_state.get('config_path')})", flush=True)
    return {"door_side": side}


# The reader's receive modes (mpk_rfid RF_*): speed vs. how faint a tag it can still hear.
# At the gate there are a handful of badges, not hundreds, so sensitivity is what matters.
RFID_MODES = {103: ("最快", "-68 dBm"), 302: ("快", "-68 dBm"),
              345: ("一般", "-74 dBm"), 285: ("最靈敏", "-83 dBm")}
RFID_MODE_DEFAULT = 103     # what the gate ran before the setting existed
RFID_POWER_RANGE = (5.0, 33.0)   # what the page offers; the READER has the final say


def _antenna_settings_from(cfg: dict) -> dict[int, tuple[float, int]]:
    """gate.json rfid_antenna_settings ({"1": {"power_dbm", "rf_mode"}, …}) as
    RfidService wants it. Ports without an entry fall back to rfid_power_dbm /
    rfid_rf_mode — which is all a gate.json from before 2026-10-07 has."""
    out = {}
    for k, v in (cfg.get("rfid_antenna_settings") or {}).items():
        try:
            out[int(k)] = (float(v["power_dbm"]), int(v["rf_mode"]))
        except (KeyError, TypeError, ValueError):
            print(f"[config] ignoring rfid_antenna_settings[{k!r}]: {v!r}", flush=True)
    return out


def rfid_settings() -> dict:
    ants = _rfid.settings() if _rfid else {}
    return {"available": _rfid is not None,
            "connected": bool(_rfid and _rfid.connected),
            # The first antenna's, as the single setting used to be (older pages read it).
            "power_dbm": _rfid.power_dbm if _rfid else None,
            "rf_mode": _rfid.rf_mode if _rfid else None,
            "antennas": [{"antenna": a, **s} for a, s in ants.items()],
            "power_range": RFID_POWER_RANGE,
            "modes": [{"value": k, "label": v[0], "sensitivity": v[1]} for k, v in RFID_MODES.items()]}


def set_rfid_settings(power, mode, antenna=None) -> dict:
    """Apply live — to one antenna, or to every antenna when none is named — and keep
    it only if the reader accepted it (a refused value is put back by the reader
    service and never saved)."""
    try:
        power, mode = float(power), int(mode)
        antenna = None if antenna in (None, "") else int(antenna)
    except (TypeError, ValueError):
        raise ValueError("power must be a number, mode and antenna integers")
    if not RFID_POWER_RANGE[0] <= power <= RFID_POWER_RANGE[1]:
        raise ValueError(f"power must be {RFID_POWER_RANGE[0]:g}-{RFID_POWER_RANGE[1]:g} dBm")
    if mode not in RFID_MODES:
        raise ValueError(f"mode must be one of {', '.join(map(str, RFID_MODES))}")
    out = _rfid.reconfigure(power, mode, antenna)
    if out.get("ok"):
        _state["cfg"]["rfid_antenna_settings"] = {str(a): s for a, s in _rfid.settings().items()}
        # The single setting stays, as the first antenna's, for a gate run without the
        # per-antenna one (and for anything still reading it).
        _state["cfg"]["rfid_power_dbm"] = _rfid.power_dbm
        _state["cfg"]["rfid_rf_mode"] = _rfid.rf_mode
        save_config()
    out.update(rfid_settings())
    return out


def set_trigger_zone(left, right) -> dict:
    """Horizontal band of the frame a person must stand in to start the dwell timer.

    Fractions of frame width, not pixels: the trigger judges the 1280-wide capture,
    the page shows a 720p stream and the result canvas is full-res — one number that
    means the same thing in all three. The person's box CENTRE must fall inside;
    the whole box needn't (arms out, or a box that bleeds into the door frame, would
    otherwise disqualify someone standing exactly at the door).
    """
    try:
        lo, hi = float(left), float(right)
    except (TypeError, ValueError):
        raise ValueError("Edges must be numbers")
    if not (0.0 <= lo <= 1.0 and 0.0 <= hi <= 1.0):
        raise ValueError("Edges must be between 0 and 1")
    if hi - lo < TRIGGER_ZONE_MIN_WIDTH:
        raise ValueError("Right edge must be to the right of the left edge")
    _state["cfg"]["trigger_zone"] = [round(lo, 3), round(hi, 3)]
    save_config()
    print(f"[config] trigger_zone={_state['cfg']['trigger_zone']} "
          f"(saved to {_state.get('config_path')})", flush=True)
    return {"trigger_zone": _state["cfg"]["trigger_zone"]}


def set_trigger_min_area(area) -> dict:
    """px² a Person box must reach before the image trigger starts its dwell timer.

    Box area is the gate's proxy for distance (see primary_person): the same number
    that hides background people on the picture now also says "close enough to be
    the one at the gate". One slider, one meaning, on both sides.
    """
    try:
        a = float(area)
    except (TypeError, ValueError):
        raise ValueError("Area must be a number")
    if a < 0:
        raise ValueError("Area must be >= 0")
    _state["cfg"]["trigger_min_area"] = int(a)
    save_config()
    print(f"[config] trigger_min_area={int(a)} (saved to {_state.get('config_path')})",
          flush=True)
    return {"trigger_min_area": _state["cfg"]["trigger_min_area"]}


def set_rfid_min_rssi(rssi) -> dict:
    """dBm a tag's peak must reach to count as a person AT the gate (vs. in range).

    Below it, a colleague's badge a few metres back is still heard by the reader but
    ignored by the one-person rule; above it, two tags mean two people and the gate
    refuses rather than guess which one is being checked.
    """
    try:
        r = float(rssi)
    except (TypeError, ValueError):
        raise ValueError("RSSI must be a number")
    lo, hi = RFID_RSSI_RANGE
    if not (lo <= r <= hi):
        raise ValueError(f"RSSI must be between {lo:g} and {hi:g} dBm")
    _state["cfg"]["rfid_min_rssi"] = round(r, 1)
    save_config()
    print(f"[config] rfid_min_rssi={_state['cfg']['rfid_min_rssi']} dBm "
          f"(saved to {_state.get('config_path')})", flush=True)
    return {"rfid_min_rssi": _state["cfg"]["rfid_min_rssi"]}


def set_person_class(cls: str) -> dict:
    """Remap which class counts as the worker, live and persisted.

    Separate from the checklist items and just as essential: association hangs off this
    one name (primary_person() looks for it), so a model that calls it 'person' while the
    config says 'Person' detects nobody and every check returns NO_WORKER no matter how
    the three items are mapped. Found exactly that way — remapping all three items onto
    a foreign model still returned NO_WORKER until this became remappable too.
    """
    cls = (cls or "").strip()
    if not cls:
        raise ValueError("A worker class must be chosen")
    with _lock:
        available = set(_model.names.values())
    if cls not in available:
        raise ValueError(f"'{cls}' is not a class the active model provides")
    _state["cfg"]["person_class"] = cls
    save_config()
    print(f"[config] person_class={cls} (saved to {_state.get('config_path')})", flush=True)
    return {"person_class": cls}


def store_frame(jpeg: bytes, dets: list[dict], w: int, h: int) -> str:
    fid = uuid.uuid4().hex[:12]
    _frames[fid] = {"jpeg": jpeg, "dets": dets, "w": w, "h": h}
    while len(_frames) > MAX_FRAMES_CACHED:
        _frames.popitem(last=False)
    return fid


class GateNotReady(RuntimeError):
    """A gate check cannot run right now (camera down, model swapping, no frames).

    Carries an HTTP status so the API can keep reporting exactly what it did before,
    while non-HTTP callers — the RFID reader, and the through-beam sensor after it —
    can simply catch this and log. The status is a hint for one caller, not a coupling
    to HTTP in the check itself.
    """

    def __init__(self, message: str, http_status: int = 503):
        super().__init__(message)
        self.http_status = http_status


def run_gate_check(worker: str = "", source: str = "api", rfid: dict | None = None,
                   intent: str | None = None, tid=None, pre: list | None = None) -> dict:
    """One complete gate check: burst, detect, vote, announce, record. Returns the result.

    THIS is the gate trigger, and every trigger must come through here — the button,
    the through-beam sensor and the image trigger — so that all of them get the same
    burst, the same voting, the same spoken result and the same dataset capture.
    Uploads via /api/check deliberately do not: they are test images the gate never saw,
    and recording them would pollute the training data.

    `rfid` is an identity already resolved by the caller (the image trigger settles
    "who, and is it only one" BEFORE it decides to run a check at all). When given, the
    post-burst RFID window below is skipped: re-resolving would use a different window
    and could name a different tag than the one the trigger just approved.

    Raises GateNotReady when the camera or model is not in a state to check.

    NOTE for whoever wires the next trigger: this is not yet safe to call concurrently.
    Two overlapping triggers would interleave their frame grabs and vote over a mixture
    of both bursts. It has never mattered with a single button, but it will the moment a
    reader can fire while somebody is pressing CAPTURE & CHECK — serialise callers with a
    lock before enabling a second trigger source.
    """
    if _state.get("swapping"):
        raise GateNotReady("Model is being switched — try again in a moment")
    if not _state.get("camera"):
        raise GateNotReady("Camera is disabled on this server", 409)
    if not _camera["ok"] or latest_frame() is None:
        raise GateNotReady("Camera not connected yet")

    # T — the moment the gate was triggered. The RFID window is measured from here, not
    # from whenever the burst happens to finish.
    trigger_t = time.monotonic()
    n = int(_state["cfg"].get("frames", 5))
    ids = []
    # Frames the image trigger already detected during the dwell (_pre_frames), oldest
    # first — at most n-1, so at least one is always taken after the trigger.
    for e in (pre or [])[-max(0, n - 1):]:
        ok, enc = cv2.imencode(".jpg", e["f"], [int(cv2.IMWRITE_JPEG_QUALITY), CAPTURE_JPEG_QUALITY])
        if ok:
            h, w = e["f"].shape[:2]
            ids.append(store_frame(enc.tobytes(), e["dets"], w, h))
    n_pre = len(ids)
    for k in range(n - n_pre):
        if ids:
            # Deliberate pause, and the largest part of a tap. See --burst-interval:
            # voting only means anything if the frames differ — and that holds between
            # the last frame kept from the dwell and the first new one too.
            time.sleep(_state["burst_interval"])
        f = latest_frame()
        if f is None:
            break
        dets, _ms = detect(f)
        ok, enc = cv2.imencode(".jpg", f, [int(cv2.IMWRITE_JPEG_QUALITY), CAPTURE_JPEG_QUALITY])
        if not ok:
            continue
        h, w = f.shape[:2]
        ids.append(store_frame(enc.tobytes(), dets, w, h))
    if n_pre:
        print(f"[check] burst: {n_pre} frame(s) from the dwell + {len(ids) - n_pre} new, "
              f"{time.monotonic() - trigger_t:.2f} s", flush=True)
    if not ids:
        raise GateNotReady("Could not grab any live frames")

    # Identity is resolved AFTER the burst, deliberately. The window looks forward as
    # well as back, and the burst has already spent that time doing the safety-critical
    # work — so the later, better-informed answer costs nothing extra in the common case.
    if rfid:
        worker = rfid["epc"]
    elif _rfid is not None and not worker:
        after = _state["rfid_after"]
        remaining = (trigger_t + after) - time.monotonic()
        if remaining > 0:
            time.sleep(min(remaining, after))       # bounded; never waits on a stalled clock
        rfid = _rfid.obs.closest(trigger_t, _state["rfid_before"], after)
        if rfid:
            worker = rfid["epc"]
            others = len(rfid["candidates"]) - 1
            print(f"[rfid] T{rfid['first_seen']:+.2f}s..{rfid['last_seen']:+.2f}s  "
                  f"{rfid['epc']}  {rfid['rssi']} dBm  {rfid['reads']} reads"
                  + (f"  (+{others} other tag(s) in range)" if others else ""), flush=True)
        else:
            print("[rfid] no tag seen in the window — worker id left blank", flush=True)

    res = finalize_check(ids, worker, source=source, intent=intent, tid=tid)
    # "in" / "out" from the bbox track, when the image trigger knew it. The button and the
    # beam sensor have no track to read, so their checks leave it unset.
    res["intent"] = intent
    if rfid:
        res["rfid"] = {k: rfid[k] for k in ("epc", "rssi", "reads")}
        res["rfid"]["others_in_range"] = len(rfid["candidates"]) - 1
        # Every badge in the window, not just the one used — the record's rfid_tags.
        res["rfid"]["candidates"] = [{k: c[k] for k in ("epc", "rssi", "reads")}
                                     for c in rfid["candidates"]]
    queue_record(res, worker)
    return res


# ── through-beam sensor trigger ─────────────────────────────────────────────
# The sensor's DI wire lands on one of the RFID box's 3 GPIO inputs (see
# rfid_reader.py's GpioSensor) — there is no separate sensor hardware path, the box
# carries both signals over the one Ethernet link. main() wires _rfid.on_gpio_change to
# sensor_triggered when --sensor-pin is given, turning a beam break into exactly the
# same run_gate_check() the CAPTURE & CHECK button calls — same burst, vote, spoken
# result and dataset capture, "sensor" source instead of "api".
SENSOR_COOLDOWN_S = 3.0   # one crossing must not fire a dozen overlapping checks if the
                          # beam flickers, or someone lingers astride it
_sensor_last_trigger = 0.0


def sensor_triggered(pin: int, level: int, prev: int) -> None:
    """Registered as RfidService.on_gpio_change — runs on the RFID library's RX thread,
    called only for a genuine transition (never the initial state discovery; see
    rfid_reader.py's _on_gpio for why that distinction matters).

    Only the pin named by --sensor-pin counts — the box has 3 GPIO inputs and only one
    of them is the through-beam sensor; the other two (if wired to anything at all)
    must never be able to fire a gate check.

    Reacts to the RISING edge only (level == 1): a through-beam sensor marks a moment
    (the beam broke), not a held condition, so treating a continuously-HIGH pin as
    trigger-worthy would re-fire for as long as someone stands in it. Which level
    actually means "broken" is a --sensor-pin wiring/polarity question, not something
    guessed here — this reacts to whatever "1" has been configured to mean.

    Also checks sensor_enabled — the web UI's runtime on/off toggle (/api/sensor_trigger)
    — separately from which pin is configured: an operator can silence the trigger
    without the server forgetting which pin it would use if re-enabled.

    Hands off to a fresh thread immediately: this callback runs on the same RX thread
    that parses every RFID tag and GPIO packet, and a burst check takes the better part
    of a second (deliberate — see --burst-interval), which would stall that parsing and
    could drop reads or further GPIO events for the whole window.
    """
    if pin != _state.get("sensor_pin") or level != 1 or not _state.get("sensor_enabled"):
        return
    global _sensor_last_trigger
    now = time.monotonic()
    if now - _sensor_last_trigger < SENSOR_COOLDOWN_S:
        return
    _sensor_last_trigger = now
    threading.Thread(target=_run_sensor_check, name="SensorTrigger", daemon=True).start()


def _run_sensor_check() -> None:
    """The actual triggered check, on its own thread — never inline in sensor_triggered.
    Serialised against the button via _gate_check_lock, same as any other caller of
    run_gate_check() must be (see that function's docstring)."""
    with _gate_check_lock:
        try:
            res = run_gate_check("", source="sensor")
            print(f"[sensor] triggered check: {res['status']}", flush=True)
        except GateNotReady as e:
            print(f"[sensor] trigger ignored — {e}", flush=True)
        except Exception as e:
            print(f"[sensor] trigger FAILED: {type(e).__name__}: {e}", flush=True)


# ── image trigger ────────────────────────────────────────────────────────────
# The default trigger. Runs detection on the live feed continuously and fires a gate
# check once the largest Person has stood close enough (box area >= cfg
# trigger_min_area) for --image-dwell seconds. Nothing to wire and nothing to tap —
# just walk up and stand still — which is why it replaced the through-beam sensor as
# the default; that path stays available behind its own toggle.
#
# Before the check runs, the RFID reader names the worker (rfid_reader.pick_worker):
#   tags at or above cfg rfid_min_rssi in the last --rfid-before seconds, a tag's
#   strength being its peak over every antenna (--rfid-antenna 1,3 = the union)
#     → the STRONGEST is the worker; run the check with that EPC.
#   none  → 「ID讀取失敗」 + red flash, no check. Nobody badged, and a verdict without an
#           identity can't be traced to anyone. (Reader down: silent, red steady.)
# Two or more used to refuse with 「檢測口請淨空」 (two badges = two people, so don't
# guess whose verdict it is). With two antennas across the walkway the next worker in
# line was heard above the floor so often that the gate kept refusing, and the operator
# chose the strongest instead (2026-10-07) — accepting a wrong name when two badges are
# close in strength. Two PEOPLE in the door zone (the camera's crowd rule) still refuse.
# Every refusal is spoken, every time — including the same worker refused again a few
# seconds later (operator, 2026-09-30).
# Once the tags change (someone steps back, someone badges) or the person leaves the
# area, the next refusal speaks again.
IMAGE_TRIGGER_PERIOD_S = 0.10    # one tick every 100 ms, as a fixed PERIOD — the loop
                                 # sleeps whatever is left after the ~65 ms of work, rather
                                 # than a fixed sleep on top of it (the old 150 ms sleep made
                                 # a 215 ms tick). ~10 Hz is for the tracker: at ~4.6 Hz a
                                 # walking worker moves far enough between frames that IDs
                                 # risk breaking. The camera delivers a new frame every
                                 # 40-80 ms, so 100 ms still sees a fresh frame nearly every
                                 # tick. It does not slow the PPE burst: the loop yields to
                                 # it (see _gate_check_lock below).
IMAGE_TRIGGER_COOLDOWN_S = 2.0   # provisional hold, set the moment the dwell completes —
                                 # the verdict is not known for another ~1 s (the burst),
                                 # so this is what a refusal keeps and what covers the gap
                                 # until _image_trigger_fire replaces it with one of the two
                                 # below. Measured from dwell completion, not the verdict.
# The two outcomes want opposite things, and both are measured from the VERDICT:
IMAGE_TRIGGER_COOLDOWN_PASS_S = 5.0   # the PASS window: green flashing, the channel open for
                                      # this worker. It ends EARLY the moment they cross the
                                      # band's far edge (_visit_resolve re-arms the gate for
                                      # the next worker). Still standing there when it runs
                                      # out: yellow flash (passed but did not go) and the
                                      # normal 2 s re-check starts — operator, 2026-09-30.
IMAGE_TRIGGER_COOLDOWN_FAIL_S = 0.5   # a worker who failed is still standing there fixing a
                                      # strap, so the sooner they get a fresh verdict the
                                      # better. The floor is the announcement itself
                                      # (check_fail.mp3 is 1.78 s): re-checking before they
                                      # have heard why they failed helps nobody, and with the
                                      # dwell on top this still lands ~2.3 s after the verdict.
IMAGE_TRIGGER_COOLDOWN_ID_S = 0.5     # after 「ID讀取失敗」 / 「ID未登入」 (operator, 2026-10-07:
                                      # "比照檢測未通過" — the worker is still standing there
                                      # sorting out their badge). Measured from the refusal,
                                      # which has no burst before it, so the next one comes
                                      # ~1.7 s later (this + the 1.2 s dwell): sooner than
                                      # id_read_fail.mp3 lasts (2.09 s; ID_not_registered 1.80).
                                      # Whether the tower cuts the playing voice off for the
                                      # next or ignores the new command is unmeasured. Crowd
                                      # and reader-down keep IMAGE_TRIGGER_COOLDOWN_S.
# Both debounces are measured in SECONDS, not ticks. They used to be tick counts (5 and
# 3), which silently halved when the tick rate doubled — the crowd confirm would have
# dropped to ~0.3 s and let the phantom-box false alarms back in. Time keeps their
# meaning fixed whatever the tick rate, including when a busy GPU stretches a tick.
# The check's first frames come from the dwell itself (operator, 2026-10-07: 「前 3 個
# frame + 後 2 個」). The trigger loop already runs the model on the newest frame every
# tick while the worker stands there; those detections used to be thrown away and the
# burst then took 5 fresh frames, ~0.68 s from dwell to verdict (journal, 92 checks).
# Now the last `burst_before` ticks of the SAME worker in the door zone are voted on
# with the rest taken fresh — 3 + 2 → ~0.3 s. Ticks are 0.1 s apart, so they are
# distinct camera frames (see --burst-interval). At least one frame is always taken
# after the trigger, so the verdict never rests only on what was seen before it.
BURST_BEFORE_DEFAULT = 3
PRE_BURST_MAX_AGE_S = 0.6        # an older tick is not "the moment of the trigger"
_pre_burst: deque = deque(maxlen=6)   # {"t", "f", "dets", "tid"} of recent in-zone ticks


def burst_before() -> int:
    """How many of the check's frames come from the dwell (gate.json burst_before),
    leaving at least one to be taken after the trigger."""
    n = int(_state["cfg"].get("frames", 5))
    try:
        k = int(_state["cfg"].get("burst_before", BURST_BEFORE_DEFAULT))
    except (TypeError, ValueError):
        k = BURST_BEFORE_DEFAULT
    return max(0, min(k, n - 1))


def _pre_frames(tid, t: float) -> list[dict]:
    """The last burst_before() dwell ticks of track `tid` (any, without a track ID),
    none older than PRE_BURST_MAX_AGE_S before t, oldest first."""
    k = burst_before()
    if k <= 0:
        return []
    picks = [e for e in list(_pre_burst)
             if 0 <= t - e["t"] <= PRE_BURST_MAX_AGE_S and (tid is None or e["tid"] == tid)]
    return picks[-k:]


IMAGE_TRIGGER_MISS_S = 1.0       # nobody qualifying in the zone for this long, unbroken,
                                 # and the visit is over. Crucially the dwell timer keeps
                                 # running underneath: a worker the model drops for a few
                                 # frames must not start their 2 s over.
CROWD_CONFIRM_S = 0.65           # more than one person, seen on every tick for this long,
                                 # before the gate says so. Everything else in this loop is
                                 # debounced; firing a crowd refusal off a single frame made
                                 # it the one place a momentary phantom box (a reflection, a
                                 # duplicate box on a moving worker) became an announcement.
                                 # Far shorter than two people take to walk up, far longer
                                 # than a blip.
# There is deliberately no "walked in without finishing" alarm. One existed
# (「勿擅自闖入此區」, fired when a subject left across the left edge mid-dwell) and was
# removed at the operator's request: at a busy gate it added one more voice to an
# already crowded speaker, for an event nobody acts on. Leaving the zone in any
# direction now just ends the visit silently.

TRACK_HOLD_S = 6.0               # how long a subject's trail stays on the live view after
                                 # they were last seen. Not a tracker: these are just the
                                 # positions the loop already computed, kept so the
                                 # operator can SEE which way a worker walked —
                                 # nothing downstream consumes them.
TRACK_SAMPLES = 64               # ring buffer size; TRACK_HOLD_S / POLL_S with headroom

ALARM_HOLD_S = 5.0               # how long an announcement stays on the kiosk before the
                                 # screen returns to 待命中, measured from the moment the
                                 # outcome was decided. Deliberately longer than
                                 # IMAGE_TRIGGER_COOLDOWN_S: for a check the burst itself
                                 # eats most of the cooldown (RFID window + 5 frames ≈ 1-1.5 s),
                                 # so clearing exactly when the gate re-arms would flash the
                                 # PASS/FAIL verdict for well under a second. A refusal has
                                 # no burst and simply holds the full 5 s.

# What the kiosk shows for each outcome, in the operator's own words. Kept here rather
# than in web/app.js so the screen and the speaker are driven by one list: a clip added
# or renamed without its caption is the kind of drift nobody notices until a worker is
# standing at a red screen that says nothing.
ALARM_TEXT = {
    "crowd":       ("檢測口請淨空",     "兩人以上同時在檢測口"),
    "multi":       ("檢測口請淨空",     "多張識別證同時感應"),
    "no_tag":      ("ID讀取失敗",       "未配戴識別證或訊號過弱"),
    "reader_down": ("RFID 讀取器離線",  "通道暫停開放"),
    "fail":        ("檢測未通過",       "請確認配戴後再試一次"),
    "intrusion":   ("擅自闖入",         "未經檢測進入"),
    "fail_entered": ("擅自闖入",        "檢測未通過仍進入"),
    "out_unchecked": ("未檢查即出場",   "出場前也要先檢測"),
    "out_fail":    ("未通過仍出場",     "檢測未通過就離開"),
    # The headline is what the speaker SAYS: there is no 「被拒絕仍出場」 clip, so a refused
    # worker walking out hears 「未檢查即出場」 (no PPE check was done) — the screen said
    # 被拒絕仍出場 over that voice (operator, 2026-10-06). The detail stays underneath, and
    # See Records / IT still carry the exact VIOLATION_TEXT 被拒絕仍出場.
    "out_refused": ("未檢查即出場",     "被拒絕後直接離開"),
    "no_worker":   ("未偵測到人員",     "請站到閘門前"),
    "unregistered": ("ID未登入",        "識別證不在白名單內"),
    # 「檢測通過」 — no 「請進入」: whether this worker is going in or out is not known
    # from the verdict alone.
    "pass":        ("檢測通過", ""),
    "pass_entry":  ("檢測通過", "請進場"),       # IT's access_type decided the direction
    "pass_leave":  ("檢測通過", "請出場"),
}

_image_trigger = {
    "lock": threading.Lock(),
    "watching": False,       # thread is alive and allowed to look at frames
    "dwell_start": None,     # t_enter: monotonic time the subject entered the zone
    "miss_since": None,      # monotonic time the subject was first missing (None = seen)
    "crowd_since": None,     # monotonic time more than one person was first seen
    "next_allowed": 0.0,     # monotonic time the next trigger may fire
    "person_area": 0,        # subject's box area on the last tick, for the UI
    "person_cx": None,       # ...and its centre x as a fraction of frame width
    "in_zone": False,        # ...and whether that centre is inside cfg trigger_zone
    "people": 0,             # how many boxes cleared the area threshold this tick
    "boxes": [],             # this tick's person boxes, normalised, for the overlay
    "ppe": [],               # ...and the checklist's item boxes (helmet, harness, vetoes)
    "track": deque(maxlen=TRACK_SAMPLES),   # (t, cx, cy, area) of the subject, newest last
    "last_event": None,      # {"t": wall, "outcome": ..., "epcs": [...]} of the last fire
    "alarm_until": 0.0,      # monotonic time the kiosk should stop showing it
    "subject_intent": None,  # "in" / "out" once the subject's visit has started
    "pass_tid": None,        # track ID whose PASS window is open (green flashing)
    "pass_until": 0.0,       # ...and when it runs out if they have not crossed
}


def _dwell_clear(it: dict) -> None:
    """Forget the current dwell timer."""
    it["dwell_start"] = None
    it["miss_since"] = None
    it["crowd_since"] = None


def _iou(a, b) -> float:
    """Intersection over union of two xyxy boxes."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0 else 0.0


def _image_trigger_status() -> dict:
    it = _image_trigger
    cfg = _state["cfg"]
    with it["lock"]:
        now = time.monotonic()
        dwell = (now - it["dwell_start"]) if it["dwell_start"] else 0.0
        cooldown = max(0.0, it["next_allowed"] - now)
        return {
            "available": bool(_state.get("camera")),
            "enabled": bool(_state.get("image_trigger_enabled")),
            "watching": it["watching"],
            "dwell_s": _state["image_dwell"],
            "dwell": round(dwell, 1),
            # Being timed, or a burst running (the light's yellow steady) — the kiosks
            # show 「檢測中」 for the whole of it, not only while the dwell counts.
            "checking": gate_checking(),
            "cooldown": round(cooldown, 1),
            "person_area": it["person_area"],
            "person_cx": it["person_cx"],
            "in_zone": it["in_zone"],
            "people": it["people"],
            "boxes": it["boxes"],
            "ppe": it["ppe"],
            "subject_intent": INTENT_TEXT.get(it["subject_intent"], ""),
            "miss": round(now - it["miss_since"], 1) if it["miss_since"] else 0,
            "miss_limit": IMAGE_TRIGGER_MISS_S,
            "min_area": cfg.get("trigger_min_area", TRIGGER_MIN_AREA_DEFAULT),
            "min_rssi": cfg.get("rfid_min_rssi", RFID_MIN_RSSI_DEFAULT),
            "zone": cfg.get("trigger_zone", TRIGGER_ZONE_DEFAULT),
            "last_event": it["last_event"],
            # last_event is history and stays put (the trigger bar shows "last 00:44:43
            # checked" indefinitely); this says whether the kiosk should still be
            # ANNOUNCING it, so the next worker never walks up to the previous one's alarm.
            "alarm_active": now < it["alarm_until"],
            "alarm_text": ALARM_TEXT,
            "light": _light.state() if _light else None,
            "direction_source": direction_source(),
            # The 大字報 adds 請進場 / 請出場 under 「檢測通過」 only when the voice says it.
            "pass_says_direction": pass_voice_says_direction(),
            "door_side": door_side(),
            "it_live": _it is not None and _it.running,
            # [age_seconds, cx, cy, area] oldest first. Aged out here rather than on
            # write so the trail fades on its own once somebody walks away.
            "track": [[round(now - t, 2), tcx, tfy, ta]
                      for (t, tcx, tfy, ta) in it["track"] if now - t <= TRACK_HOLD_S],
        }


def _make_person_tracker():
    """A standalone ByteTrack instance that gives each person box a persistent ID.

    Deliberately NOT model.track(). That registers its callbacks on the YOLO model object
    itself, and they then run on every later predict() — including the PPE burst's — and
    replace each result with the tracked boxes only (trackers.track.on_predict_postprocess_end).
    Low-scoring helmet or harness boxes that never became tracks would vanish from the
    burst: a silent change to what the checklist is judged on. Here the tracker only ever
    sees the person boxes this loop hands it; the burst's detections never pass through it.
    """
    from ultralytics.trackers.byte_tracker import BYTETracker
    from ultralytics.utils import IterableSimpleNamespace, YAML
    from ultralytics.utils.checks import check_yaml
    return BYTETracker(args=IterableSimpleNamespace(**YAML.load(check_yaml("bytetrack.yaml"))))


def _assign_track_ids(tracker, persons: list[dict], shape) -> None:
    """Run one tracker update over this tick's person boxes and write each box's ID
    into it as d["tid"] (None if the tracker has not confirmed it yet).

    Fed EVERY person above track_person_conf, not only those above the area threshold: a worker
    walking up starts small, and giving them an ID early is what keeps it unbroken at the
    moment they grow past AREA_TH. Background people get IDs internally but nothing reads
    them — every decision still filters on area first.
    """
    from ultralytics.engine.results import Boxes
    for d in persons:
        d["tid"] = None
    arr = np.array([[*d["box"], d["score"], 0] for d in persons], dtype=np.float32).reshape(-1, 6)
    for row in tracker.update(Boxes(arr, shape[:2]), None):
        idx = int(row[-1])                     # index back into the boxes we fed in
        if 0 <= idx < len(persons):
            persons[idx]["tid"] = int(row[4])


# ── PATLITE signal tower (scripts/patlite_tower.py) ──────────────────────────
# Tri-colour light + voice on its own Ethernet port. So far only the page's Speaker test
# drives it; the gate's announcements still go through the MP3 audio thread.
_tower = None               # Tower when --tower-host is set
_tower_web = None           # {"web_user", "web_pass"} from config/tower.local.json — the
                            # unit's web login, needed only for settings the command API
                            # lacks (speaker volume). Never commit that file.
_tower_web_lock = threading.Lock()   # the unit allows one web login at a time
TOWER_TEST_FLASH_S = 5      # a test flash reverts on the unit's own restore timer


# Voices registered on the tower's own channels (its web UI → Voice Registration,
# 2026-09-30), so light and voice come from one device. The same words are in MP3/ for a
# gate with no tower (--tower-host ""), which plays them on the Jetson instead.
VOICE = {
    "crowd":        (1, "gate_clear.mp3"),         # 檢測口請淨空  (crowd, two badges)
    "intrusion":    (2, "intrusion.mp3"),          # 擅自闖入      (walked in unchecked)
    "fail":         (3, "ppe_fail.mp3"),           # 檢測未通過    (PPE fail, or entered anyway)
    "pass":         (6, "ppe_pass.mp3"),           # 檢測通過
    "exit_unchecked": (7, "exit_unchecked.mp3"),   # 未檢查即出場 (walked out unchecked / refused)
    "exit_fail":    (8, "exit_fail.mp3"),          # 未通過仍出場 (walked out after a FAIL)
    "pass_entry":   (9, "ppe_pass_entry.mp3"),     # 檢測通過請進場 (IT answered entry)
    "pass_leave":   (10, "ppe_pass_leave.mp3"),    # 檢測通過請出場 (IT answered leave)
    "no_tag":       (4, "id_read_fail.mp3"),       # ID讀取失敗
    "unregistered": (5, "ID_not_registered.mp3"),  # ID未登入
}
FLASH_HOLD_S = 5.0          # a red / yellow flash, unless a newer event replaces it
_light_policy = LightPolicy()
_light = None               # LightDriver when a tower is configured


IT_VOICE_WAIT_S = 1.5       # how long a PASS voice waits for IT's access_type
# Does the PASS voice add the direction — 「檢測通過請進場」 / 「檢測通過請出場」 (tower
# channels 9 / 10) — or say just 「檢測通過」 (channel 6)? Per 進出場判斷 mode:
#   track (軌跡方向): yes — the track knows the way at once (operator asked for it
#                    back, 2026-10-06);
#   it    (IT 回報):  no — plain 「檢測通過」 at once, no waiting on IT's reply (2026-10-05).
# Either way the direction is still worked out and still judges went-through /
# violations; this only decides the spoken words (and the 大字報 shows the same).
PASS_VOICE_SAYS_DIRECTION = {"track": True, "it": True}


def pass_voice_says_direction(mode: str | None = None) -> bool:
    return bool(PASS_VOICE_SAYS_DIRECTION.get(mode or direction_source(), False))


def _pass_voice(at, mode: str | None = None) -> str:
    """The VOICE key for a PASS whose direction is `at` ("entry" / "leave" / None)."""
    if not pass_voice_says_direction(mode):
        return "pass"
    return {"entry": "pass_entry", "leave": "pass_leave"}.get(at, "pass")


def _post_check_now(res: dict):
    """Post this check to IT on a background thread. Returns (state, done): state is the
    dict the check's visit entry also carries (so its later events.jsonl line knows it was
    sent, and the outbox does not send it again), done is set when the reply is in.
    A failed post leaves state "failed" and the outbox delivers it when the visit ends."""
    from it_report import check_fields
    state = res["it_state"] = {"state": "pending"}
    done = threading.Event()
    items = {i["label"]: i["ok"] for i in res.get("items", []) if i.get("enabled", True)}
    check = {"ts": res["ts"], "status": res["status"], "items": items,
             "epc": res.get("worker_id") if res.get("worker_id") != "—" else ""}

    def run():
        try:
            fields = check_fields(check, _it.cfg)
            frame = _frames.get(res.get("frame_id"))
            code, reply = _it.post_check(fields, frame["jpeg"] if frame else None,
                                         f"{res.get('frame_id', 'check')}.jpg")
            if code < 400:
                at = reply.get("access_type") if isinstance(reply, dict) else None
                state.update(state="sent", access_type=at,
                             id=reply.get("id") if isinstance(reply, dict) else None)
                if (at in ("entry", "leave") and res["status"] == "PASS"
                        and direction_source() == "it"):
                    _apply_it_direction(res, at)
            elif code >= 500 or code in (408, 429):
                state.update(state="failed", error=f"HTTP {code}")
            else:
                state.update(state="rejected", error=f"HTTP {code}")
        except Exception as e:
            state.update(state="failed", error=f"{type(e).__name__}: {e}")
            print(f"[it] could not post at the verdict ({e}) — the outbox will retry", flush=True)
        finally:
            done.set()
    threading.Thread(target=run, daemon=True, name="it-verdict").start()
    return state, done


def _apply_it_direction(res: dict, access_type: str) -> None:
    """IT's access_type is the direction from now on: the visit is judged against it
    (went through = left by the edge IT's direction points to), and the kiosk shows it.
    The visit may not have the check attached yet (IT answering within the burst's own
    bookkeeping) — _visit_attach picks it up from res["it_state"] then."""
    intent = {"entry": "in", "leave": "out"}[access_type]
    for v in list(_visits.values()):
        if any(c.get("it") is res.get("it_state") for c in v.get("checks", [])):
            if v.get("intent_it") != intent:
                print(f"[visit] #{v['tid']} IT says {access_type} → {INTENT_TEXT[intent]}"
                      + (f" (the track had guessed {INTENT_TEXT[v['intent']]})"
                         if v.get("intent") and v["intent"] != intent else ""), flush=True)
            v["intent_it"] = intent
    _show_pass_direction(access_type)


def _show_pass_direction(access_type: str) -> None:
    """The kiosk's alarm box: 「檢測通過」 over 「請進場」 / 「請出場」."""
    it = _image_trigger
    with it["lock"]:
        if it["last_event"] and it["last_event"].get("outcome") == "check":
            it["last_event"] = dict(it["last_event"], outcome="pass_" + access_type)


def say(key: str) -> None:
    """Speak one of VOICE. Never blocks: the tower answers in ~0.3 s but can take up to
    its 3 s timeout when unplugged, so the request runs on a short-lived thread."""
    if not _state.get("audio"):
        return
    ch, clip = VOICE[key]
    if _tower is None:
        play_clip(clip)
        return

    def run():
        try:
            _tower.play(ch)
        except Exception as e:
            print(f"[tower] voice ch{ch} ({key}) failed: {e}", flush=True)
    threading.Thread(target=run, daemon=True, name="tower-voice").start()


# The same words to the same person within VOICE_REPEAT_S are spoken once (operator,
# 2026-10-07: 「同一個人 5 秒內相同內容只播一次」). A worker who stays at the gate was told
# 「ID讀取失敗」 every ~2 s: on site, about half of every ID讀取失敗 / 檢測未通過 / 檢測口請淨空 /
# ID未登入 came within 6 s of the same words before it (10-05..07, up to 20 in a row). Only
# those four repeat; PASS and every violation are one-off events and always spoken. The
# light, the record and the screen are unchanged — only the voice stays quiet. Measured
# from the last time it was SPOKEN, so someone who stays hears it again every ~6 s.
# "Same person" is the same VISIT (track ID + when the visit started): walking off and
# coming back is a new visit and is spoken to again — the case that got the old
# badge-based "don't repeat" removed on 2026-09-30. No track ID (button, sensor, a tracker
# hiccup): always spoken.
VOICE_REPEAT_S = 5.0
VOICE_REPEAT_KEYS = frozenset({"no_tag", "unregistered", "fail", "crowd"})
_voice_said: dict[tuple, float] = {}       # (voice key, tid, visit start) -> last spoken
_voice_lock = threading.Lock()


def say_to(key: str, tid=None, now: float | None = None) -> bool:
    """say(key) to the person on track `tid`, unless they heard these same words less
    than VOICE_REPEAT_S ago in this visit. Returns whether it was spoken."""
    if key in VOICE_REPEAT_KEYS and tid is not None:
        v = _visits.get(tid) or {}
        who = (key, tid, v.get("started"))
        now = time.monotonic() if now is None else now
        with _voice_lock:
            for k in [k for k, t in _voice_said.items() if now - t >= VOICE_REPEAT_S]:
                del _voice_said[k]
            if who in _voice_said:
                print(f"[voice] {key} to #{tid} again within {VOICE_REPEAT_S:g} s — not "
                      "spoken (light, record and screen as usual)", flush=True)
                return False
            _voice_said[who] = now
    say(key)
    return True


def signal(name: str, hold_s: float = FLASH_HOLD_S, tag: str = "") -> None:
    """Put a flash on the tower light (gate_light.LightPolicy decides what actually shows:
    red steady for a device fault outranks it)."""
    if _light is None:
        return
    _light_policy.flash(name, hold_s, time.monotonic(), tag)
    _light.wake()


def gate_abnormal() -> str | None:
    """Why the channel is closed (red steady), or None. Also true while the gate is still
    starting up — no frame yet means nobody is being watched."""
    if _state.get("camera") and (not _camera["ok"] or time.time() - _camera["ts"] > CCTV_STALE_S):
        return "攝影機斷線"
    if _rfid is not None and not _rfid.connected:
        return "RFID 讀取器離線"
    # No "reader deaf" rule (closed after N badge-less people in a row) any more: removed
    # at the operator's request, 2026-10-05 — on site nobody may be carrying a badge at
    # all, and the gate must keep checking (and saying 「ID讀取失敗」) rather than close.
    # A reader that is connected but hears nothing (antenna off) is therefore not shown
    # as a fault; every worker just gets ID讀取失敗.
    if _state.get("swapping"):
        return "切換模型中"
    if not _state.get("image_trigger_enabled"):
        return "影像觸發關閉"
    return None


def gate_checking() -> bool:
    """Yellow steady: someone is being timed in the door band, or a burst is running."""
    return _image_trigger["dwell_start"] is not None or _gate_check_lock.locked()


# ── badge whitelist (scripts/whitelist.py, config/whitelist.json) ────────────
_whitelist = None           # Whitelist, always created; off while its file is absent


# ── IT reporting (scripts/it_report.py) ──────────────────────────────────────
_it = None                  # ITReporter when config/it.json has enabled: true
CCTV_STALE_S = 10.0         # no fresh frame for this long counts as a dead camera, even
                            # while the RTSP session still claims to be connected


def device_status() -> str:
    """The heartbeat's device_status. Camera and speaker are the two devices the format
    names. A camera covered by something still delivers frames and is NOT caught here —
    that needs a picture-content check this does not do."""
    cam_dead = bool(_state.get("camera")) and (
        not _camera["ok"] or time.time() - _camera["ts"] > CCTV_STALE_S)
    if _light is not None:
        # The tower is the speaker (and the light). Its health is known every second from
        # the light refresher, so a dead one is reported without waiting for a FAIL.
        spk_dead = _light.state()["tower_ok"] is False
    else:
        spk_dead = bool(_state.get("audio")) and _audio_health["ok"] is False
    return ("cctv_speaker_dead" if cam_dead and spk_dead else "cctv_dead" if cam_dead
            else "speaker_dead" if spk_dead else "normal")


# ── visits: in / out, and what each worker actually did ──────────────────────
# One record per ByteTrack ID, in RAM only — never written to disk. What IS written is
# each visit's OUTCOME (captures.csv, events.jsonl). A record is a handful of fields, not
# a trail, so it does not grow while someone stands there: ~200 bytes, at most VISIT_MAX
# of them, and each one is deleted a few seconds after its ID leaves the frame.
#
# The site's geometry: one side of the door band is the restricted side (the door,
# door_side(): left or right of the IMAGE), the other is the entrance side — or, with
# door_side "both", both sides are the door and the entrance is only straight ahead
# (walking up from / away into the far end of the picture).
#   intent     decided the moment an ID first reaches the band big enough to count
#              (area ≥ AREA_TH). Came from the door's region → "out" (leaving the
#              restricted area); from the other side, or appeared inside the band from
#              nowhere (walked up towards the camera) → "in".
#   departure  the edge it then leaves the band by. The far side of its intent → it went
#              through; the side it came from → it turned back. OR, since 2026-10-02, walking
#              AWAY from the camera: the site plan has the entrance path straight in front
#              of the camera, so a worker leaving the plant goes out by shrinking, not by
#              crossing an edge (→ through for 出場, back for 進場). See _away_step.
# Every tracked person's side is followed, whatever their size, so a worker who was
# still small in the left region keeps that origin once they grow. But only an ID that
# reaches the band at AREA_TH becomes a visit: background people are never judged.
CROSS_MIN_AREA_FRACTION = 0.5   # a crossing counts only if the box is still ≥ half AREA_TH
                                # (half, not all: a worker crouching or turning shrinks)
# Walking away = the box shrinks below CROSS_MIN_AREA_FRACTION × AREA_TH — the same "no
# longer near the gate" size as above — steadily, and stays there for AWAY_CONFIRM_S.
# Relative to AREA_TH, not to the worker's own size at the gate, on purpose: a worker
# turning sideways to the door (width ~0.55) or crouching (height ~0.5) shrinks too, but
# from well above AREA_TH, so they never get this small without actually moving off.
AWAY_CONFIRM_S = 0.3   # 3 ticks — a one-frame shrink (a partial box) is not a departure
AWAY_MIN_STEP = 0.5    # between two sightings the area may at most halve, and the centre
AWAY_MAX_SHIFT = 0.1   # move ≤ 10 % of the width: a bigger jump is the tracker handing
                       # this ID to someone small in the background, not a person walking
VISIT_LOST_S = 3.5     # unseen this long = gone (ByteTrack itself drops an ID at ~3 s)
VISIT_IDLE_S = 30.0    # checked, then neither left nor re-checked for this long → did not go
VISIT_MAX = 50         # safety cap on records, whatever happens
VISIT_MAX_CHECKS = 10  # results kept per visit (a worker failing again and again)

INTENT_TEXT = {"in": "進場", "out": "出場"}
# Written to See Records and reported to IT; never spoken (operator's call).
VIOLATION_TEXT = {
    ("no_check", "in"): "未檢查即進入",  ("no_check", "out"): "未檢查即出場",
    ("fail", "in"):     "未通過仍進入",  ("fail", "out"):     "未通過仍出場",
    ("refused", "in"):  "被拒絕仍進入",  ("refused", "out"):  "被拒絕仍出場",
}
_visits: dict[int, dict] = {}      # tid -> record. Touched only by the watcher thread.


def _side(cx: float, zone) -> str:
    return "L" if cx < zone[0] else "R" if cx > zone[1] else "B"


def _is_door(letter) -> bool:
    """Is this _side() letter ("L", "R", "B" or None) the door's (restricted) region?
    One side for door_side left/right; both sides for "both"."""
    return letter in {"left": ("L",), "right": ("R",), "both": ("L", "R")}[door_side()]


def _visit_start(v: dict, came_from, now: float, area: float) -> None:
    """Begin a visit: the ID has just reached the band big enough to count. Intent is the
    side it last came from (came_from) — the door's region means it is walking out."""
    v.update(engaged=True, resolved=False, started=now,     # started: say_to's "same visit"
             intent="out" if _is_door(came_from) else "in",
             intent_it=None, checks=[], last_activity=now,
             away_since=None, away_blocked=False, rearm=False, peak=area)


def _away_line(v: dict, min_area: float) -> float:
    """Below this area a visit's box counts as walked away (and an edge crossing as a
    background hand-off): away_fraction (default half) of the biggest the worker got at
    the gate, never above that fraction of AREA_TH. A front-on worker reaches AREA_TH,
    so theirs is the plain line; a side-on exit (started at exit_area_fraction) gets one
    in proportion to its own size, or it would have to "walk away" to almost nothing."""
    return away_fraction() * min(min_area, v.get("peak") or min_area)


def _away_step(v: dict, area: float, cx: float, small: bool, now: float) -> bool:
    """Follow one engaged visit's walk away from the camera; True once it is confirmed.

    Walking away is gradual — at 10 Hz the box loses a few percent per tick and its centre
    barely moves. The tracker handing the ID to someone already small in the background
    is a jump, and is blocked until the box is big again (else the next tick, small to
    small, would look smooth)."""
    prev_area, prev_cx = v.get("prev_area"), v.get("prev_cx")
    v["prev_area"], v["prev_cx"] = area, cx
    if not small:
        v["away_since"], v["away_blocked"] = None, False
        return False
    if v.get("away_blocked"):
        return False
    if v.get("away_since") is None:
        if (prev_area is None or area < AWAY_MIN_STEP * prev_area
                or abs(cx - prev_cx) > AWAY_MAX_SHIFT):
            v["away_blocked"] = True
            print(f"[visit] #{v['tid']} box jumped to small (area {int(prev_area or 0)} → "
                  f"{int(area)}, cx {prev_cx if prev_cx is not None else -1:.2f} → {cx:.2f}) — "
                  f"not read as walking away", flush=True)
            return False
        v["away_since"] = now
    return now - v["away_since"] >= AWAY_CONFIRM_S


def _visit_update(persons: list[dict], min_area: float, zone, now: float, frame) -> None:
    """Advance every tracked person by one tick; resolve visits that just ended."""
    w = frame.shape[1]
    seen = set()
    for d in persons:
        tid = d.get("tid")
        if tid is None:
            continue
        seen.add(tid)
        v = _visits.get(tid)
        if v is None:
            v = _visits[tid] = {"tid": tid, "side": None, "last_t": now, "engaged": False,
                                "resolved": False, "intent": None, "checks": [],
                                "last_activity": now}
        cx = (d["box"][0] + d["box"][2]) / 2 / w
        side = _side(cx, zone)
        prev = v["side"]
        if v.get("gone_at") is not None:
            # Back after vanishing mid-visit (see below): where, and after how long.
            print(f"[visit] #{tid} back in view after {now - v['gone_at']:.1f} s at "
                  f"cx={cx:.2f} (side {side}; was {prev})", flush=True)
            v["gone_at"] = None
        v["last_t"] = now
        v["cx"] = cx
        area = _area(d["box"])
        v["area"] = area
        v["score"] = d.get("score", 0.0)
        v["box"] = list(d["box"])
        # Where this track last came from, kept across ticks — not just the tick before
        # the band: a side-on worker out of the door may only grow past the exit bar a
        # few ticks INSIDE the band. Small in the middle of the frame = far down the
        # entrance path, which wipes it: whoever walks up from there is coming in.
        # Out of the DOOR counts only for a box already at the exit bar (exit_area_fraction
        # × AREA_TH) there, on a track never smaller than that before (operator,
        # 2026-10-09: 「面積也要大於門檻」). The door is at the camera, so a worker stepping
        # out of it is big from the first sighting; someone walking up the hall along that
        # side starts small — the 2026-10-08 recording's #240, 100k against a 190k AREA_TH,
        # read as leaving once the zone edge moved in. A door-side sighting that does not
        # qualify leaves the origin as it was. The bar here stops at AREA_TH: a multiplier
        # above 1 raises when a worker from the door is followed and checked (_gate_area),
        # and must not turn a door-side worker under it into one walking in — who would
        # then be checked sooner, at the plain AREA_TH, not later.
        door_min = min_area * min(1.0, exit_area_fraction())
        if side != "B":
            if not _is_door(side) or (area >= door_min and not v.get("was_small")):
                v["came_from"] = side
        elif area < CROSS_MIN_AREA_FRACTION * min_area:
            v["came_from"] = None
        if area < door_min:
            v["was_small"] = True
        if v["engaged"] and not v["resolved"]:
            v["peak"] = max(v.get("peak") or 0.0, area)
        small = area < (_away_line(v, min_area) if v["engaged"]
                        else CROSS_MIN_AREA_FRACTION * min_area)
        # Big enough to be at the gate: AREA_TH, or exit_area_fraction of it for a track
        # out of the door's side (EXIT_AREA_FRACTION_DEFAULT). A box under person_conf is
        # only being FOLLOWED (track_person_conf): it can carry a visit on, never start one.
        need = min_area * (exit_area_fraction() if _is_door(v.get("came_from"))
                           else 1.0)
        at_gate = side == "B" and area >= need and d.get("firm", True)
        if v["resolved"] and small:
            v["rearm"] = True        # walked off after the visit ended: next time is new
        if at_gate and (not v["engaged"]
                        or (v["resolved"] and (prev != "B" or v.get("rearm")))):
            _visit_start(v, v.get("came_from"), now, area)   # first arrival, or another go
            v["prev_area"], v["prev_cx"] = area, cx
        elif v["engaged"] and not v["resolved"]:
            intent = v.get("intent_it") or v["intent"]       # IT's answer wins
            away_how = "through" if intent == "out" else "back"
            if _away_step(v, area, cx, small, now):
                # Walked away from the camera: the way out of the plant on this site, and
                # an entering worker giving up.
                _visit_resolve(v, away_how, frame, box=d["box"], via="away")
            elif prev == "B" and side != "B":
                went_through = _is_door(side) == (intent == "in")
                if small and v.get("away_since") is not None:
                    # Already walking away, and drifted out of the band on the way.
                    _visit_resolve(v, away_how, frame, box=d["box"], via="away")
                elif went_through and small:
                    # Only a box that is still near the gate can walk through it. A small
                    # one crossing the edge is someone in the background — usually the
                    # tracker having handed this visit's ID to them (2026-10-01: 擅自闖入
                    # announced for people walking past at the back). Not counted; the
                    # visit ends as unknown, which accuses nobody.
                    print(f"[visit] #{tid} crossed the edge as a small box (area {int(area)} < "
                          f"{int(_away_line(v, min_area))}) — not counted as "
                          f"walking through", flush=True)
                    _visit_resolve(v, "lost", None)
                else:
                    _visit_resolve(v, "through" if went_through else "back", frame,
                                   box=d["box"])
        v["side"] = side
        d["intent"] = (v.get("intent_it") or v["intent"]) if v["engaged"] else None
    for tid, v in list(_visits.items()):
        if tid in seen:
            # Standing still after a check, with no re-check coming: did not go through.
            if (v["engaged"] and not v["resolved"] and v["checks"]
                    and now - v["last_activity"] >= VISIT_IDLE_S):
                _visit_resolve(v, "stayed", None)
            continue
        # Diagnostics: a visitor still mid-visit who has not been detected for 0.5 s. When a
        # walk-in is missed, this line says whether they vanished BEFORE crossing the band's
        # edge (too close to the camera, half out of frame) — the case seen 2026-09-30.
        if (v["engaged"] and not v["resolved"] and v.get("gone_at") is None
                and now - v["last_t"] >= 0.5):
            v["gone_at"] = v["last_t"]
            print(f"[visit] #{tid} out of view mid-visit, last seen at cx={v.get('cx', -1):.2f} "
                  f"area {int(v.get('area', 0))} score {v.get('score', 0):.2f} "
                  f"(side {v['side']})", flush=True)
        if now - v["last_t"] >= VISIT_LOST_S:
            if v["engaged"] and not v["resolved"]:
                _visit_resolve(v, "lost", None)
            del _visits[tid]
    if len(_visits) > VISIT_MAX:
        # Never-engaged (background) records go first, then the stalest.
        for tid in sorted(_visits, key=lambda t: (_visits[t]["engaged"],
                                                  _visits[t]["last_t"]))[:len(_visits) - VISIT_MAX]:
            del _visits[tid]


def _visit_reset() -> None:
    """The tracker is about to restart its IDs from 1, so every record keyed by an old ID
    is meaningless. Visits already under way are closed as "lost" first, so a FAIL that
    was waiting to see which way the worker went is still reported, not dropped."""
    for v in list(_visits.values()):
        if v["engaged"] and not v["resolved"]:
            _visit_resolve(v, "lost", None)
    _visits.clear()


def _visit_attach(tid, status: str, res: dict | None = None) -> None:
    """Hang a check result (PASS / FAIL / NO_WORKER) or a refusal ("REFUSED:crowd", ...)
    on the visit of the ID it was about. The LATEST one is what the departure is judged
    against: fail, fix, re-check PASS, then walk in is a normal entry."""
    v = _visits.get(tid) if tid is not None else None
    if v is None:
        return False
    entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "status": status}
    if res:
        entry.update(epc=res.get("worker_id") if res.get("worker_id") != "—" else "",
                     image=res.get("record_image", ""),
                     items={i["label"]: i["ok"] for i in res.get("items", [])
                            if i.get("enabled", True)})
        if res.get("it_state") is not None:
            entry["it"] = res["it_state"]          # shared: the poster fills it in
            at = res["it_state"].get("access_type")
            if at in ("entry", "leave") and status == "PASS" and direction_source() == "it":
                v["intent_it"] = {"entry": "in", "leave": "out"}[at]
    v["checks"] = (v["checks"] + [entry])[-VISIT_MAX_CHECKS:]
    v["last_activity"] = time.monotonic()
    return True


RECALL_S = 30.0         # how far back a badge's own FAIL is recalled — see _recalled_fail
_badge_results: dict[str, tuple[float, str]] = {}   # epc -> (monotonic t, PASS/FAIL), latest


def _recalled_fail(epcs: list[str]) -> str | None:
    """A badge heard right now whose own check FAILED in the last RECALL_S seconds.

    The tracker sometimes gives a worker a new ID between their check and their walk
    through (seen 2026-09-30: #29 failed, stepped back, came out as #32). The new ID has
    no checks of its own, so without this the gate calls a failed worker 未檢查. Only
    ever UPGRADES a violation's wording (未檢查 → 未通過); it never cancels one."""
    now = time.monotonic()
    for epc in epcs:
        t_status = _badge_results.get(epc)
        if t_status and t_status[1] == "FAIL" and now - t_status[0] <= RECALL_S:
            return epc
    return None


def _rfid_near(window_s: float = 3.0) -> list[str]:
    """Badges above the one-person floor in the last few seconds, strongest first — who
    was at the gate when a violation happened, as far as the reader can tell."""
    if _rfid is None:
        return []
    floor = _state["cfg"].get("rfid_min_rssi", RFID_MIN_RSSI_DEFAULT)
    return [r["epc"] for r in _rfid.obs.summary(time.monotonic(), window_s) if r["rssi"] >= floor]


def _visit_resolve(v: dict, how: str, frame, box=None, via: str = "edge") -> None:
    """Close a visit and write down what happened.

    how: "through" (left by the far edge), "back" (by the edge it came from), "stayed"
    (checked, never left, VISIT_IDLE_S passed), "lost" (the ID vanished mid-visit).
    via: "edge" (crossed a band edge) or "away" (walked away from the camera; through
    for 出場, back for 進場) — kept on the event so a wrong call can be traced.

    Always an events.jsonl line — the audit of every in/out decision and the IT outbox,
    with it_report saying which lines go to IT. A worker who went THROUGH without a PASS
    as their latest result is a violation: also a silent VIOLATION row in See Records
    carrying the frame of that moment. Nothing here is ever spoken.
    """
    v["resolved"] = True
    if via == "away":
        # Walked away from the camera: whatever side this track came from no longer says
        # anything about it. Kept, a worker who came out of the door, walked off far enough
        # to count as gone, turned and came back carried "from the door's side" into the
        # next visit — read as leaving again, so walking back INTO the door ended "back"
        # (returning where they came from) and the intrusion went unreported (2026-10-08
        # recording, #242). Cleared, the way back is a new 進場 visit.
        v["came_from"] = None
    intent = v.get("intent_it") or v["intent"]       # IT's access_type, when it answered
    last = v["checks"][-1] if v["checks"] else None
    # The worker whose PASS window is open has now left the band one way or the other:
    # close it, and re-arm at once so the next worker is not kept waiting for its end.
    it = _image_trigger
    with it["lock"]:
        was_pass_subject = it["pass_tid"] is not None and it["pass_tid"] == v["tid"]
        if was_pass_subject:
            it["pass_tid"] = None
            if how in ("through", "back"):
                it["next_allowed"] = time.monotonic()
    if was_pass_subject and how == "through":
        _light_policy.end("pass")
        if _light is not None:
            _light.wake()
    checked = [c for c in v["checks"] if not c["status"].startswith("REFUSED")]
    violation = None
    if how == "through" and not (last and last["status"] == "PASS"):
        kind = ("fail" if last and last["status"] == "FAIL"
                else "refused" if last and last["status"].startswith("REFUSED")
                else "no_check")
        if kind == "no_check":
            recalled = _recalled_fail(_rfid_near())
            if recalled:
                kind = "fail"
                print(f"[visit] #{v['tid']} has no check of its own, but badge {recalled} "
                      f"FAILED within {RECALL_S:g} s — counted as 未通過", flush=True)
        violation = VIOLATION_TEXT[(kind, intent)]
    departure = {"through": intent, "back": "back", "stayed": "none", "lost": "unknown"}[how]
    _rec_log("visit", track=v["tid"], how=how, via=via, intent=intent, departure=departure,
             violation=violation)
    # IT hears about every visit that had a PPE result, every violation, and every badge
    # the whitelist refused. Other refusals alone (crowd, two badges, no badge) are
    # warnings only — unless the worker then went through anyway, a violation.
    unregistered = any(c["status"] == "REFUSED:unregistered" for c in v["checks"])
    it_report = bool(violation or checked or unregistered)

    epc = next((c.get("epc") for c in reversed(checked) if c.get("epc")), "")
    image = ""
    if last and last["status"] == "PASS" and how in ("back", "stayed"):
        # Passed, then did not go (該進場未進場 / 該出場未出場): a warning, silent.
        signal("yellow_flash")
    if violation:
        # Went through without a PASS: spoken, a red flash and the kiosk's alarm box, both
        # ways (operator, 2026-09-30). Walking IN is always 「擅自闖入」 — after a FAIL too:
        # 「檢測未通過」 there only repeated the verdict heard a moment before, so the
        # intrusion was never announced as one (operator). Walking OUT: 「未通過仍出場」 after
        # a FAIL, 「未檢查即出場」 with no PPE check (none, or refused before one) — each
        # its own words, never the verdict's.
        if intent == "in":
            key = "fail_entered" if kind == "fail" else "intrusion"
            say("intrusion")
        else:
            key = {"no_check": "out_unchecked", "fail": "out_fail",
                   "refused": "out_refused"}[kind]
            say("exit_fail" if kind == "fail" else "exit_unchecked")
        signal("red_flash", tag="violation")
        with it["lock"]:
            it["last_event"] = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "outcome": key,
                                "epcs": [], "area": 0}
            it["alarm_until"] = time.monotonic() + ALARM_HOLD_S
    if violation:
        if not epc:
            epc = " ".join(_rfid_near())
        # box = the person who crossed: the record's person_box says WHO triggered it,
        # which is what a false alarm is diagnosed from.
        image = queue_alarm_record("VIOLATION", None, frame, worker=epc, box=box,
                                   score=v.get("score"),
                                   alarm_text=violation, direction=INTENT_TEXT[intent])
    ev = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "track_id": v["tid"],
        "intent": intent,                 # in / out — where they came from
        "departure": departure,           # in / out / back / none / unknown — what they did
        "via": via if how in ("through", "back") else None,   # edge / away
        "violation": violation,
        "epc": epc,
        "ppe": checked[-1]["status"] if checked else None,
        "items": checked[-1].get("items") if checked else None,
        "check_image": checked[-1].get("image", "") if checked else "",
        "image": image,                   # the frame at the moment of a violation
        "checks": v["checks"],
        "it_report": it_report,
    }
    where = (f"  [area {int(_area(box))} cx {(box[0] + box[2]) / 2 / frame.shape[1]:.2f}]"
             if box is not None and frame is not None else "")
    if how in ("through", "back") and via == "away":
        where = "  (walked away)" + where
    print(f"[visit] #{v['tid']} {INTENT_TEXT[intent]} → {departure}{where}"
          f"{'  ppe=' + ev['ppe'] if ev['ppe'] else ''}"
          f"{'  VIOLATION ' + violation if violation else ''}"
          f"  it_report={it_report}", flush=True)
    if _state.get("record"):
        try:
            _rec_q.put_nowait({"event": ev})
        except queue.Full:
            print("[record] writer is behind — dropped one visit event", flush=True)


def _refuse_unregistered(tid, epc: str) -> None:
    """Exactly one badge at the gate, and the whitelist does not know it.

    Unlike the other refusals, which are warnings for the worker to fix, this one is about
    WHO is at the gate, so it is also reported to IT and flashed red on the tower. Spoken
    and recorded every time, like every refusal; IT receives it once per badge per visit
    (it_report.result_forms de-duplicates). Voice channel 5, red flash.
    """
    direction = INTENT_TEXT.get((_visits.get(tid) or {}).get("intent"), "")
    image = ""
    print(f"[image] refused — {epc} is not on the whitelist: ID未登入", flush=True)
    say_to("unregistered", tid)
    signal("red_flash")
    v = _visits.get(tid) or {}
    image = queue_alarm_record("UNREGISTERED", "unregistered", latest_frame(), worker=epc,
                               people=_image_trigger["people"], direction=direction,
                               box=v.get("box"), score=v.get("score"))
    entry = {"worker_id": epc, "record_image": image, "items": []}
    if _visit_attach(tid, "REFUSED:unregistered", entry):
        return
    # No visit to hang it on — the tracker had no ID for the subject on this tick. IT must
    # still hear about it, so it goes out now as a visit of its own.
    ev = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "track_id": None, "intent": None,
          "departure": "unknown", "violation": None, "epc": epc, "ppe": None,
          "items": None, "check_image": "", "image": image,
          "checks": [{"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "status": "REFUSED:unregistered", "epc": epc, "image": image,
                      "items": {}}],
          "it_report": True}
    if _state.get("record"):
        try:
            _rec_q.put_nowait({"event": ev})
        except queue.Full:
            print("[record] writer is behind — dropped one unregistered-badge event", flush=True)


def image_trigger_loop() -> None:
    it = _image_trigger
    tracker = _make_person_tracker()
    next_tick = time.monotonic()
    while not _state.get("stop"):
        # Fixed-rate: sleep only what is left of the period. If a tick overran (a busy
        # GPU), start the next one immediately but do not try to catch up with a burst
        # of back-to-back ticks.
        next_tick += IMAGE_TRIGGER_PERIOD_S
        delay = next_tick - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_tick = time.monotonic()
        if not _state.get("image_trigger_enabled") or _state.get("swapping"):
            with it["lock"]:
                it["watching"] = False
                _dwell_clear(it)
            tracker.reset()                   # IDs from before a pause or model swap mean nothing
            _visit_reset()                    # ...and neither do visits keyed by them
            continue
        # A burst (button, sensor, or our own) owns the camera and the GPU for the
        # better part of a second; watching through it would only vote on stale
        # frames and slow the burst down.
        if _gate_check_lock.locked():
            continue
        f = latest_frame()
        if f is None:
            with it["lock"]:
                it["watching"] = False
            continue
        try:
            dets, _ms = detect(f)
        except Exception as e:
            print(f"[image] detect failed: {type(e).__name__}: {e}", flush=True)
            continue
        cfg = _state["cfg"]
        min_area = cfg.get("trigger_min_area", TRIGGER_MIN_AREA_DEFAULT)
        zone = cfg.get("trigger_zone", TRIGGER_ZONE_DEFAULT)
        # Everyone big enough to be AT the gate, not merely the closest one. Two people
        # arriving together have to be caught here, before the dwell even starts, rather
        # than by RFID two seconds later — and the subject is the largest of them, the
        # same "largest means closest" rule the checklist itself judges on.
        # Tracked: every person box down to track_person_conf. Counted ("firm"): only
        # those at person_conf — see TRACK_PERSON_CONF_DEFAULT.
        persons = [d for d in dets
                   if d["name"] == cfg["person_class"]
                   and d["score"] >= track_person_conf()]
        for d in persons:
            d["firm"] = d["score"] >= cfg["person_conf"]
        try:
            _assign_track_ids(tracker, persons, f.shape)
        except Exception as e:
            # Tracking is observation only for now; it must never stop the gate.
            print(f"[image] tracker update failed: {type(e).__name__}: {e}", flush=True)
            for d in persons:
                d["tid"] = None
        # Big enough to be AT the gate — and, to count, standing in the door zone (ROI).
        # Only people_in feed the crowd rule and the dwell (operator, 2026-10-05): on site
        # people stand beside the gate and someone sits right under the camera, and of
        # that day's 100 「檢測口請淨空」 refusals only 4 had two people inside the zone.
        # The subject is the largest person IN the zone; with nobody there, the largest
        # anywhere, only so the status line can say "outside door zone".
        people = [d for d in persons if d["firm"] and _area(d["box"]) >= _gate_area(d, min_area)]
        W = f.shape[1]
        people_in = [d for d in people
                     if zone[0] <= (d["box"][0] + d["box"][2]) / 2 / W <= zone[1]]
        subject = (max(people_in, key=lambda d: _area(d["box"])) if people_in
                   else max(people, key=lambda d: _area(d["box"])) if people else None)
        area = int(_area(subject["box"])) if subject else 0
        cx = (round((subject["box"][0] + subject["box"][2]) / 2 / W, 3)
              if subject else None)
        in_zone = subject is not None and subject in people_in
        now = time.monotonic()
        # Before the dwell logic, and on every tick including the cooldown: a second
        # person slipping through while the gate is still cooling down after a pass is
        # exactly the case the dwell cannot see and the visit tracker must.
        try:
            _visit_update(persons, min_area, zone, now, f)
        except Exception as e:
            print(f"[visit] update failed: {type(e).__name__}: {e}", flush=True)
        subject_tid = subject.get("tid") if subject else None
        if in_zone:
            _pre_burst.append({"t": now, "f": f, "dets": dets, "tid": subject_tid})
        outcome = None
        with it["lock"]:
            it["watching"] = True
            it["person_area"] = area
            it["person_cx"] = cx
            it["in_zone"] = in_zone
            it["people"] = len(people_in)
            it["subject_intent"] = subject.get("intent") if subject else None
            # Every person box the tick saw, normalised, for the live overlay. Boxes
            # BELOW the area threshold are included too, flagged 0: seeing what nearly
            # counted is what makes the area slider tunable, and a phantom box is far
            # easier to recognise on the picture than in a log line.
            h, w = f.shape[:2]
            it["boxes"] = [
                [round(d["box"][0] / w, 4), round(d["box"][1] / h, 4),
                 round(d["box"][2] / w, 4), round(d["box"][3] / h, 4),
                 round(d["score"], 2), int(_area(d["box"])),
                 # 1 = counts toward the crowd rule (big, firm, in the zone); 3 = under
                 # AREA_TH but in a visit (a side-on worker out of the door)
                 2 if d is subject and in_zone else (1 if d in people_in
                                                     else 3 if d.get("intent") else 0),
                 d.get("tid"), INTENT_TEXT.get(d.get("intent"), "")]
                for d in persons]
            # The checklist's own item boxes (helmet, harness…) from this same tick, for the
            # live overlay: the result picture no longer freezes, so this is where the
            # operator sees them (2026-10-01). Same class lists and thresholds the check
            # uses; kind 1 = the item itself, 0 = its veto class (no-helmet, …).
            ppe = []
            items = [i for i in cfg.get("items", []) if i.get("enabled", True)]
            for d in dets:
                for item in items:
                    if d["score"] < item["conf"]:
                        continue
                    kind = (1 if d["name"] in item["classes"]
                            else 0 if d["name"] in item.get("negatives", []) else None)
                    if kind is None:
                        continue
                    ppe.append([round(d["box"][0] / w, 4), round(d["box"][1] / h, 4),
                                round(d["box"][2] / w, 4), round(d["box"][3] / h, 4),
                                round(d["score"], 2), item["label"], d["name"], kind])
                    break
            it["ppe"] = ppe
            if cx is not None:
                # Drawn at the box centre, so the trail sits on the worker rather than
                # at their feet — which also keeps it visible when the legs are cut off
                # by the bottom of the frame. cx is unchanged and is still the number
                # every decision reads.
                it["track"].append((now, cx,
                                    round((subject["box"][1] + subject["box"][3])
                                          / 2 / f.shape[0], 3), area))

            pass_timed_out = None
            if it["pass_tid"] is not None and now >= it["pass_until"]:
                pass_timed_out, it["pass_tid"] = it["pass_tid"], None
            if now < it["next_allowed"]:
                _dwell_clear(it)                      # cooling down; time must not accrue
            elif len(people_in) > 1:
                if it["crowd_since"] is None:
                    it["crowd_since"] = now
                # Until the crowd is confirmed the dwell timer is left strictly alone: a
                # phantom second box for a few frames must not cost a worker the
                # seconds they have already stood there, any more than a missed
                # detection does.
                if now - it["crowd_since"] >= CROWD_CONFIRM_S:
                    outcome = "crowd"
                    _dwell_clear(it)
                    it["next_allowed"] = now + IMAGE_TRIGGER_COOLDOWN_S
            elif subject is not None and in_zone:
                it["crowd_since"] = None
                it["miss_since"] = None               # recovered — the timer runs on
                if it["dwell_start"] is None:
                    it["dwell_start"] = now
                elif now - it["dwell_start"] >= _state["image_dwell"]:
                    outcome = "check"
                    _dwell_clear(it)
                    it["next_allowed"] = now + IMAGE_TRIGGER_COOLDOWN_S
            else:
                it["crowd_since"] = None
                if it["dwell_start"] is not None:
                    if it["miss_since"] is None:
                        it["miss_since"] = now
                    if now - it["miss_since"] >= IMAGE_TRIGGER_MISS_S:
                        # Gone — whichever way they went, the visit ends in silence.
                        _dwell_clear(it)
        # Every tick into the recording's log: what replaying the video must reproduce.
        # Boxes in gate-frame pixels; a person's entry ends with its track ID.
        _rec_log("tick", dets=[[d["name"], round(d["score"], 3)] + [int(v) for v in d["box"]]
                               + ([d.get("tid")] if d["name"] == cfg["person_class"] else [])
                               for d in dets],
                 subject=subject_tid, in_zone=in_zone, people=len(people_in),
                 dwell=round(now - it["dwell_start"], 2) if it["dwell_start"] else None,
                 outcome=outcome)
        if pass_timed_out is not None:
            # Passed, and still has not crossed the band when the window closed: a warning,
            # and — since the cooldown ends with the window — the normal re-check follows.
            print(f"[image] #{pass_timed_out} passed but did not go through in "
                  f"{IMAGE_TRIGGER_COOLDOWN_PASS_S:g} s — yellow, re-checking", flush=True)
            signal("yellow_flash")
        if outcome == "check":
            _image_trigger_fire(now, area, subject_tid)
        elif outcome == "crowd":
            _visit_attach(subject_tid, "REFUSED:crowd")
            # Every box that voted, with its full geometry and its overlap with the
            # largest one. A refusal writes no CSV row and no image, so this line is the
            # only evidence it leaves — and the IoU/containment figures are exactly what
            # a same-person de-duplication threshold would have to be tuned against.
            # NMS has already run at iou=0.7, so anything logged here survived that.
            ranked = sorted(people_in, key=lambda d: -_area(d["box"]))
            big = ranked[0]["box"]
            parts = []
            for i, d in enumerate(ranked):
                b = d["box"]
                s = (f"#{i} {d['score']:.2f} area={int(_area(b))} "
                     f"cx={(b[0] + b[2]) / 2 / f.shape[1]:.3f} "
                     f"box=[{int(b[0])},{int(b[1])},{int(b[2])},{int(b[3])}]")
                if i:
                    s += (f" IoU={_iou(b, big):.2f}"
                          f" contain={containment(b, big):.2f}")
                parts.append(s)
            _image_trigger_refuse("crowd", f"{len(people_in)} people over {min_area} px² "
                                           f"in the zone for {CROWD_CONFIRM_S:g} s: "
                                           + "  |  ".join(parts), tid=subject_tid)
            queue_alarm_record("CROWD", "crowd", f, people=len(people_in), box=big,
                               score=ranked[0]["score"],
                               direction=INTENT_TEXT.get(subject.get("intent") if subject else None, ""))


def _image_trigger_refuse(outcome: str, why: str, tid=None) -> None:
    """A refusal decided by the camera alone — no RFID read, no burst. 「檢測口請淨空」
    and a yellow flash: a crowd is a warning (operator, 2026-09-30), not a fail.

    Unlike the RFID refusals these are not de-duplicated against a tag set: there is no
    identity involved, and the cooldown is what stops a crowd standing at the gate from
    being told once per tick.
    """
    it = _image_trigger
    with it["lock"]:
        it["last_event"] = {"t": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "outcome": outcome, "epcs": [],
                            "area": it["person_area"]}
        it["alarm_until"] = time.monotonic() + ALARM_HOLD_S
    print(f"[image] {outcome}: {why} -> 檢測口請淨空", flush=True)
    say_to("crowd", tid)
    signal("yellow_flash")


def _image_trigger_fire(t: float, area: int, tid=None) -> None:
    """Dwell satisfied at monotonic time t. Apply the one-person rule, then check.
    `tid` is the subject's track ID: the result is hung on that ID's visit."""
    it = _image_trigger
    cfg = _state["cfg"]
    min_rssi = cfg.get("rfid_min_rssi", RFID_MIN_RSSI_DEFAULT)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    rfid = None
    outcome = None
    epcs: list[str] = []
    rows: list[dict] = []

    if _rfid is None:
        # No reader on this server (sandbox / bench). There is no identity to gate on,
        # so behave like the button: check, worker ID blank.
        outcome = "check"
    else:
        rows = _rfid.obs.summary(t, _state["rfid_before"])
        # The strongest badge at or above the floor, over every antenna (pick_worker —
        # it used to be "exactly one, else 檢測口請淨空"; operator, 2026-10-07).
        best, near = pick_worker(rows, min_rssi)
        epcs = [r["epc"] for r in near]                # strongest first: epcs[0] is best
        heard = ", ".join(f"{r['epc']} {r['rssi']} dBm" for r in rows) or "nothing"
        if best:
            outcome = "check"
            rfid = {"epc": best["epc"], "rssi": best["rssi"], "reads": best["reads"],
                    "candidates": rows}
            # We know who it is — and they may still not be allowed in. None (no
            # whitelist file) lets it through, exactly as before the list existed.
            if _whitelist is not None and _whitelist.allowed(best["epc"]) is False:
                outcome = "unregistered"
        else:
            outcome = "no_tag" if _rfid.connected else "reader_down"
        chose = (f"; took the strongest, {near[0]['rssi'] - near[1]['rssi']:.1f} dB over "
                 f"{near[1]['epc']}" if len(near) > 1 else "")
        print(f"[image] person area={area} dwelled {_state['image_dwell']:g}s; "
              f"tags >= {min_rssi:g} dBm: {len(near)} (heard: {heard}) -> {outcome}{chose}",
              flush=True)

    with it["lock"]:
        it["last_event"] = {"t": stamp, "outcome": outcome, "epcs": epcs, "area": area}
        it["alarm_until"] = time.monotonic() + ALARM_HOLD_S
        if outcome in ("no_tag", "unregistered"):
            it["next_allowed"] = time.monotonic() + IMAGE_TRIGGER_COOLDOWN_ID_S

    if outcome == "check":
        with _gate_check_lock:
            try:
                intent = (_visits.get(tid) or {}).get("intent")
                res = run_gate_check("", source="image", rfid=rfid, intent=intent, tid=tid,
                                     pre=_pre_frames(tid, t))
                _visit_attach(tid, res["status"], res)
                if rfid and res["status"] in ("PASS", "FAIL"):
                    _badge_results[rfid["epc"]] = (time.monotonic(), res["status"])
                print(f"[image] triggered check: {res['status']}"
                      f"{'  (' + INTENT_TEXT[intent] + ')' if intent else ''}", flush=True)
                # Re-arm on the verdict, not on the dwell: only now is it known whether
                # the worker is walking away (PASS) or still standing at the gate fixing
                # something (FAIL). The provisional hold set when the dwell completed is
                # replaced here, and the burst that just ran has already consumed ~1 s of it.
                hold = (IMAGE_TRIGGER_COOLDOWN_PASS_S if res["status"] == "PASS"
                        else IMAGE_TRIGGER_COOLDOWN_FAIL_S)
                with it["lock"]:
                    it["next_allowed"] = time.monotonic() + hold
                    if res["status"] == "PASS":
                        it["pass_tid"] = tid
                        it["pass_until"] = it["next_allowed"]
            except GateNotReady as e:
                print(f"[image] trigger ignored — {e}", flush=True)
            except Exception as e:
                print(f"[image] trigger FAILED: {type(e).__name__}: {e}", flush=True)
        return
    if outcome == "unregistered":
        _refuse_unregistered(tid, epcs[0])
        return
    _visit_attach(tid, "REFUSED:" + outcome)
    # EVERY check speaks and flashes — the operator's rule (2026-09-30): "每一次的檢測都要
    # 有語音". There used to be a "don't repeat for the same badges" rule; it silenced a
    # worker who walked away and came back under the same track ID, which is exactly the
    # case the gate must not go quiet on. No badge, or a reader that is down: 「ID讀取失敗」
    # and red (with the reader down, the light's red steady outranks the flash). Two
    # badges are no longer a refusal (pick_worker, 2026-10-07); old MULTI_TAG rows stay
    # readable in See Records.
    say_to("no_tag", tid); signal("red_flash")
    print(f"[image] refused — {outcome}: {ALARM_TEXT[outcome][0]}", flush=True)
    queue_alarm_record({"no_tag": "NO_TAG", "reader_down": "READER_DOWN"}[outcome], outcome,
                       latest_frame(), worker=" ".join(epcs), people=it["people"],
                       direction=INTENT_TEXT.get((_visits.get(tid) or {}).get("intent"), ""),
                       tags=rows, box=(_visits.get(tid) or {}).get("box"),
                       score=(_visits.get(tid) or {}).get("score"))


def finalize_check(ids: list[str], worker: str, preview: bool = False,
                   source: str = "api", intent: str | None = None, tid=None) -> dict:
    """Vote the checklist over a burst of already-detected frames and record it.
    Shared by the file-upload path (/api/check) and the live path (/api/capture).

    preview=True is the folder-playback view (web/app.js pbTick): the same evaluate(),
    the same result shape, but it is NOT a gate event — so it does not become
    _last_result (which the kiosk auto-shows and the sensor path overwrites), does
    not announce, and does not log a [check] line per frame. A 500-image folder at
    5 fps would otherwise play 500 MP3s and drown the journal.
    """
    bursts = [_frames[i]["dets"] for i in ids]
    cfg = _state["cfg"]
    if source == "image":
        # The trigger fired on a person IN the door zone; the checklist must judge that
        # person, not a bigger bystander beside the gate (ppe_check.primary_person).
        z = cfg.get("trigger_zone", TRIGGER_ZONE_DEFAULT)
        w = _frames[ids[0]].get("w") or 0
        if w and not (z[0] <= 0 and z[1] >= 1):
            cfg = dict(cfg, subject_zone_px=[z[0] * w, z[1] * w])
    res = evaluate(bursts, cfg).to_dict()

    # Report which frame the UI should display: the one evaluate() judged best.
    best = 0
    for k, i in enumerate(ids):
        if _frames[i]["dets"] is res["detections"]:
            best = k
            break
    res["worker_id"] = worker or "—"
    res["frame_id"] = ids[best]
    res["width"] = _frames[ids[best]]["w"]
    res["height"] = _frames[ids[best]]["h"]
    res["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if preview:
        res["preview"] = True
        return res
    res["source"] = source          # before announce(): an image-trigger PASS asks IT first
    res["intent"] = intent          # ...and in track mode says the track's direction
    res["track_id"] = tid           # ...and a FAIL to the same visit is said once per 5 s
    _rec_log("check", status=res.get("status"), worker=res.get("worker_id"), source=source,
             intent=intent, track=tid,
             items={i["label"]: {"ok": i["ok"], "votes": i["votes"], "frames": i["frames"]}
                    for i in res.get("items", []) if isinstance(i, dict)})
    _last_result["result"] = res
    announce(res)
    def _item_log(i):
        s = f"{i['label']}={'OK' if i['ok'] else 'NG'}({i['votes']}/{i['frames']})"
        if i.get("center_y") is not None:
            s += f" cy={i['center_y']}"
        if i.get("position_rejected"):
            s += " [POSITION-REJECTED]"
        if not i.get("enabled", True):
            s += " [OFF — not judged]"
        return s

    print(f"[check] worker={res['worker_id']} status={res['status']} "
          f"frames={res['frames']} "
          + " ".join(_item_log(i) for i in res["items"]), flush=True)
    return res


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if _state["verbose"]:
            super().log_message(fmt, *args)

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str, ctype: str, name: str):
        """Stream a file (a recording runs to gigabytes) instead of reading it whole."""
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        with open(path, "rb") as fh:
            remaining = size                 # a recording still running keeps growing
            while remaining > 0:
                chunk = fh.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def _err(self, code, msg):
        print(f"[{code}] {self.command} {self.path} -> {msg}", flush=True)
        self._json({"error": msg}, code)

    def _model_yaml_upload(self, u, length: int, body: bytes):
        """Attach/replace the class-list yaml paired with one model file.

        Unlike a model upload this is tiny (a few KB) and already fully read by the
        caller, so it just gets validated and written — no chunked streaming needed.
        Writing it to <model-stem>.yaml is the same file find_paired_yaml() looks for,
        so an explicit attach here IS what auto-pairing picks up from then on.
        """
        from urllib.parse import parse_qs
        if not _state.get("can_switch"):
            return self._err(403, "Model switching is disabled (--lock-model)")
        q = parse_qs(u.query)
        model_file = (q.get("model") or [""])[0]
        try:
            full = resolve_model(model_file)
        except (ValueError, FileNotFoundError) as e:
            return self._err(400, str(e))
        if length > MAX_YAML_BYTES:
            return self._err(413, "That's too large to be a class-list yaml")

        try:
            doc = yaml.safe_load(body) or {}
            names = class_names_from_yaml_doc(doc)
        except Exception as e:
            return self._err(400, f"Not a usable class-list yaml: {e}")

        yaml_path = os.path.splitext(full)[0] + ".yaml"
        tmp = yaml_path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(body)
        os.replace(tmp, yaml_path)
        print(f"[model] {os.path.basename(yaml_path)} attached to {model_file} "
              f"({len(names)} classes)", flush=True)

        # If this yaml belongs to the model already running, re-apply the override
        # immediately — the operator sees the effect without needing to switch models.
        applied_live, warn, classes = False, None, sorted(names)
        if os.path.realpath(full) == os.path.realpath(_state["model_path"]):
            with _lock:
                warn = apply_yaml_override(_model, full)
                classes = sorted(_model.names.values())
            applied_live = warn is None

        return self._json({"model": model_file, "yaml": os.path.basename(yaml_path),
                           "classes": classes, "applied_live": applied_live,
                           "warning": warn})

    def _model_upload(self, u, length: int):
        """Receive a model file and save it into the models directory.

        Streamed to disk in chunks rather than read into memory: a .pt is 50 MB and a
        .onnx can be 100 MB, and this process is already holding a loaded network plus
        CUDA context on a shared-memory device.
        """
        from urllib.parse import parse_qs

        if not _state.get("can_switch"):
            self.rfile.read(length)                  # drain, or the connection wedges
            return self._err(403, "Model switching is disabled (--lock-model)")

        q = parse_qs(u.query)
        try:
            name = safe_model_name((q.get("name") or [""])[0])
        except ValueError as e:
            self.rfile.read(length)
            return self._err(400, str(e))

        cap = _state["max_model"]
        if length > cap:
            self.rfile.read(min(length, UPLOAD_CHUNK_BYTES))
            return self._err(413, f"Model is {length / MB:.0f} MB; limit is "
                                  f"{cap / MB:.0f} MB (raise --max-model-mb)")

        dest = os.path.join(_state["models_dir"], name)
        if os.path.exists(dest) and os.path.realpath(dest) == os.path.realpath(_state["model_path"]):
            self.rfile.read(length)
            return self._err(409, f"'{name}' is the model currently in use — upload it "
                                  "under a different name, switch away first")

        free = shutil.disk_usage(_state["models_dir"]).free
        if free < length * 2:
            self.rfile.read(length)
            return self._err(507, f"Not enough disk: {free / GB:.1f} GB free, "
                                  f"need ~{length * 2 / GB:.1f} GB to land this safely")

        replaced = os.path.exists(dest)
        tmp = dest + ".part"
        got = 0
        try:
            with open(tmp, "wb") as fh:
                while got < length:
                    chunk = self.rfile.read(min(UPLOAD_CHUNK_BYTES, length - got))
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
            if got != length:
                raise IOError(f"upload truncated at {got}/{length} bytes")
            os.replace(tmp, dest)
        except Exception as e:
            for f in (tmp,):
                if os.path.exists(f):
                    os.remove(f)
            return self._err(500, f"Could not save the model: {e}")

        print(f"[model] uploaded {name} ({got / MB:.1f} MB)"
              + (" [replaced]" if replaced else ""), flush=True)

        # Saved is saved. If it turns out to be incompatible the file stays put and shows
        # up in the browser — deleting 50 MB the operator just waited on would be rude,
        # and the same weights may well suit a different config/gate.json.
        try:
            res = switch_model(name, force=bool((q.get("force") or [""])[0]))
            res.update({"saved": True, "size_mb": round(got / MB, 1),
                        "replaced": replaced})
            return self._json(res)
        except ValueError as e:
            return self._json({"saved": True, "switched": False, "file": name,
                               "size_mb": round(got / MB, 1), "replaced": replaced,
                               "error": str(e)}, 409)
        except Exception as e:
            # Distinct from the mismatch above: this file is not a loadable model at all
            # (truncated, corrupt, wrong format), so keeping it would just litter the
            # browser with something that can never be selected.
            try:
                os.remove(dest)
                gone = " The upload was discarded."
            except OSError:
                gone = ""
            return self._json({"saved": False, "switched": False, "file": name,
                               "error": f"Not a loadable model: {str(e).rstrip('.')}.{gone}"}, 409)

    def _records(self):
        """Every row of captures.csv, newest first — the file is appended oldest-last."""
        path = _state["record_csv"]
        rows = []
        if os.path.isfile(path):
            try:
                with open(path, newline="") as fh:
                    rd = csv.DictReader(fh)
                    rows = list(rd)
            except Exception as e:
                return self._err(500, f"Could not read {path}: {e}")
        rows.reverse()
        return self._json({
            "columns": csv_columns(_state["cfg"]),
            "items": [i["label"] for i in _state["cfg"]["items"]],
            "rows": rows,
            "count": len(rows),
            "csv": path,
            "record_max": _state["record_max"] or None,
        })

    # The only directories a capture image may be served from. Kept as an explicit
    # allow-list rather than "anywhere under dataset/" so that adding a folder is a
    # deliberate act; the resolved path's PARENT must equal one of these exactly, which
    # is what stops ../ escaping and stops labels or configs being served as images.
    IMAGE_ROOTS = (("dataset", "images", "train"),
                   ("dataset", "no_person", "images"),
                   ("dataset", "alarms", "images"))

    def _record_image(self, rel: str):
        """Serve one saved capture, from any folder captures are written to."""
        base = os.path.abspath(_state["record_dir"])
        roots = [os.path.join(base, *parts) for parts in self.IMAGE_ROOTS]
        full = os.path.abspath(os.path.join(base, rel or ""))
        parent = os.path.dirname(full)

        # dataset/realtime/<model>/images/train — <model> is whichever model was
        # active when the sample was saved (model_folder_name()), so it can't be a
        # fixed IMAGE_ROOTS entry the way the others are. Accepted structurally
        # instead: relpath() against the realtime root must land exactly 3 components
        # deep, always ending in images/train. abspath() above already collapsed any
        # ../, so a match here is still guaranteed confined under record_dir.
        rt_root = os.path.join(base, "dataset", "realtime")
        if parent == rt_root or parent.startswith(rt_root + os.sep):
            rel_parts = os.path.relpath(parent, rt_root).split(os.sep)
            if rel_parts[-2:] == ["images", "train"] and len(rel_parts) == 3:
                roots.append(parent)

        if parent not in roots:
            return self._err(400, "Bad image path")
        if not os.path.isfile(full):
            return self._err(404, "Image not found — it may have been evicted by --record-max")
        with open(full, "rb") as fh:
            return self._send(200, fh.read(), "image/jpeg")

    def _mjpeg(self):
        """Stream the newest camera frame as multipart/x-mixed-replace MJPEG."""
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        target_h = _state["stream_h"]
        period = 1.0 / max(1, _state["stream_fps"])
        q = [int(cv2.IMWRITE_JPEG_QUALITY), _state["stream_q"]]
        try:
            while not _state.get("stop"):
                f = latest_frame()
                if f is None:
                    time.sleep(0.05)
                    continue
                h, w = f.shape[:2]
                if h > target_h:
                    f = cv2.resize(f, (int(w * target_h / h), target_h))
                ok, enc = cv2.imencode(".jpg", f, q)
                if not ok:
                    continue
                buf = enc.tobytes()
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(buf)}\r\n\r\n".encode())
                self.wfile.write(buf)
                self.wfile.write(b"\r\n")
                time.sleep(period)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # ── GET ───────────────────────────────────────────────────────────────
    def do_GET(self):
        u = urlparse(self.path)

        if u.path in WEB_ASSETS:
            name, ctype = WEB_ASSETS[u.path]
            try:
                # Read per request, not cached at import. The page is fetched once per
                # kiosk load so the read costs nothing, and it means editing web/app.js
                # and refreshing the browser is enough — no restart, which would drop the
                # camera connection and reload the model just to see a CSS change.
                with open(os.path.join(WEB_DIR, name), "rb") as fh:
                    return self._send(200, fh.read(), ctype)
            except OSError as e:
                return self._err(500, f"Cannot read web/{name}: {e}")

        if u.path == "/api/config":
            with _lock:
                classes = sorted(_model.names.values())
            return self._json({
                "items": [i["label"] for i in _state["cfg"]["items"]],
                "cfg": _state["cfg"],
                "model": _state["model_path"],
                "device": str(_state["device"]),
                "camera": bool(_state.get("camera")),
                "models_dir": _state["models_dir"],
                "can_switch": bool(_state.get("can_switch")),
                "realtime_interval": _state["realtime_interval"],
                # The active model's own class list — what the per-item dropdowns on
                # the main screen offer to remap to. Sourced the same way detect() itself
                # reads names, so a dropdown can never offer a class the model can't see.
                "classes": classes,
            })

        if u.path == "/api/models":
            try:
                return self._json({"models": list_models(),
                                   "dir": _state["models_dir"],
                                   "active": os.path.basename(_state["model_path"]),
                                   "required_classes": required_classes(_state["cfg"])})
            except OSError as e:
                return self._err(500, f"Could not read models directory: {e}")

        if u.path == "/api/last":
            return self._json(_last_result["result"] or {})

        if u.path == "/api/rfid_status":
            if _rfid is None:
                return self._json({"enabled": False})
            return self._json(dict(_rfid.status(), enabled=True,
                                   window=[_state["rfid_before"], _state["rfid_after"]],
                                   sensor_pin=_state.get("sensor_pin"),
                                   sensor_enabled=bool(_state.get("sensor_enabled"))))

        if u.path == "/api/image_trigger":
            return self._json(_image_trigger_status())

        # Every tag the reader has heard recently, one row per EPC — the "See RFID read"
        # popup. Same summary() the image trigger's one-person rule uses, so the popup
        # shows exactly the numbers the gate decides on. Times are converted from the
        # observation log's monotonic clock to wall clock here, at display time only.
        if u.path == "/api/it_status":
            s = _it.status() if _it else {"enabled": False}
            s["available"] = _it is not None
            s["device_status"] = device_status()
            return self._json(s)

        # Speaker volume, read from the tower's settings page (~1 s: a web login).
        if u.path == "/api/tower_volume":
            if _tower is None or _tower_web is None:
                return self._err(409, "No tower, or no config/tower.local.json with its web login")
            try:
                with _tower_web_lock:
                    return self._json(_tower.get_volume(_tower_web["web_user"],
                                                        _tower_web["web_pass"]))
            except TowerError as e:
                return self._err(502, str(e))

        if u.path == "/api/rfid_settings":
            return self._json(rfid_settings())

        if u.path == "/api/whitelist":
            return self._json(_whitelist.state() if _whitelist else {"exists": False})

        # Speaker test popup: is the tower there, and what is it showing right now.
        if u.path == "/api/tower":
            if _tower is None:
                return self._json({"enabled": False})
            out = {"enabled": True, "host": _tower.host}
            try:
                st = _tower.status()
                lamps = st.get("Unit_Status") or []
                out.update(reachable=True, version=st.get("Software_Version"),
                           sound_ch=st.get("Sound_CH"),
                           lamps={n: (lamps[i] if i < len(lamps) else None)
                                  for i, n in enumerate(LAMPS)})
            except TowerError as e:
                out.update(reachable=False, error=str(e))
            return self._json(out)

        if u.path == "/api/rfid_reads":
            if _rfid is None:
                return self._json({"enabled": False, "rows": []})
            offset = time.time() - time.monotonic()
            rows = []
            for r in _rfid.obs.summary():
                rows.append({
                    "epc": r["epc"], "rssi": r["rssi"], "last_rssi": r["last_rssi"],
                    "reads": r["reads"],
                    "first_seen": time.strftime("%H:%M:%S", time.localtime(r["first_t"] + offset)),
                    "last_seen": time.strftime("%H:%M:%S", time.localtime(r["last_t"] + offset)),
                    "age": round(time.monotonic() - r["last_t"], 1),
                    "registered": _whitelist.allowed(r["epc"]) if _whitelist else None,
                    "ants": r.get("ants") or {},
                })
            return self._json({
                "enabled": True, "connected": _rfid.connected,
                "antennas": _rfid.antennas,
                "whitelist": _whitelist.state() if _whitelist else None,
                "horizon_s": _rfid.obs.horizon_s,
                "min_rssi": _state["cfg"].get("rfid_min_rssi", RFID_MIN_RSSI_DEFAULT),
                "total_reads": _rfid.obs.total_reads,
                "rows": rows,
            })

        if u.path == "/api/camera_status":
            return self._json({"enabled": bool(_state.get("camera")),
                               "connected": _camera["ok"],
                               "age": round(time.time() - _camera["ts"], 2) if _camera["ts"] else None})

        # Kept for external viewers (VLC/ffplay pointed straight at it) — the web page no
        # longer uses it. Push-MJPEG gives a browser no way to skip ahead when its own
        # decode+repaint falls behind, so the kiosk's display lag grew without bound over
        # a session (measured: 5s -> 20s+). web/app.js polls /api/live_frame instead.
        if u.path == "/api/stream":
            if not _state.get("camera"):
                return self._err(409, "Camera is disabled on this server")
            return self._mjpeg()

        # ONE current frame, encoded on demand — the pull-model counterpart to /api/stream
        # and what web/app.js's live view actually uses (see LIVE_POLL_MS there). Same
        # stream_h/stream_q settings, so switching mechanisms changed nothing about the
        # picture itself. Same latest_frame() the gate check reads, so what the operator
        # sees IS the frame a CAPTURE & CHECK fired at that instant would have got.
        # No caching header games are needed: app.js cache-busts with ?t=.
        if u.path == "/api/live_frame":
            if not _state.get("camera"):
                return self._err(409, "Camera is disabled on this server")
            f = latest_frame()
            if f is None:
                return self._err(503, "Camera not connected yet")
            h, w = f.shape[:2]
            target_h = _state["stream_h"]
            if h > target_h:
                f = cv2.resize(f, (int(w * target_h / h), target_h))
            ok, enc = cv2.imencode(".jpg", f, [int(cv2.IMWRITE_JPEG_QUALITY), _state["stream_q"]])
            if not ok:
                return self._err(500, "Could not encode frame")
            return self._send(200, enc.tobytes(), "image/jpeg")

        # One realtime tick: detect the newest camera frame, judge it on its own, save it
        # if the throttle allows. No announcement and no captures.csv row — see _realtime.
        if u.path == "/api/realtime":
            if not _state.get("camera"):
                return self._err(409, "Camera is disabled on this server")
            if _state.get("swapping"):
                return self._err(503, "Model is being switched — try again in a moment")
            frame = latest_frame()
            if frame is None:
                return self._err(503, "Camera not connected yet")
            # Detect and save on the camera's native resolution (see _camera_full); the
            # gate-size frame stands in only while that stream is still opening.
            full = full_frame() if _state.get("realtime_full") else None
            src = frame if full is None else full
            dets, ms = detect(src)

            # Single frame, judged on its own: the summary tracks what the model sees
            # right now rather than lagging behind a vote window.
            res = evaluate([dets], _state["cfg"]).to_dict()

            saved = False
            now = time.time()
            with _realtime["lock"]:
                due = now - _realtime["last_save"] >= _state["realtime_interval"]
            # Never a gate-size stand-in into a full-resolution dataset: while the native
            # stream opens (~1-2 s) nothing is saved.
            if due and _state.get("record") and (full is not None
                                                 or not _state.get("realtime_full")):
                try:
                    saved = save_realtime_sample(src, dets, res)
                except Exception as e:
                    print(f"[realtime] could not save sample: {e}", flush=True)
                # The clock advances only on an actual write, not on every attempt. An
                # empty gate therefore leaves the throttle "due", so the first frame with
                # a worker in it is captured immediately rather than up to one interval
                # later — the interval is meant to space out SAMPLES, not poll attempts.
                if saved:
                    with _realtime["lock"]:
                        _realtime["last_save"] = now
                        _realtime["saved"] += 1

            # The page gets the preview and the boxes in the GATE frame's pixels: a 5 MP
            # preview four times a second would be ~1 MB per tick for the browser to
            # decode, and the page's area slider and box filter speak gate-frame px².
            h, w = frame.shape[:2]
            preview = src
            if full is not None:
                fh, fw = full.shape[:2]
                kx, ky = w / fw, h / fh
                scale = lambda b: [b[0] * kx, b[1] * ky, b[2] * kx, b[3] * ky]
                for d in res["detections"]:
                    d["box"] = scale(d["box"])
                if res.get("person_box"):
                    res["person_box"] = scale(res["person_box"])
                preview = cv2.resize(full, (w, h), interpolation=cv2.INTER_AREA)
            ok, enc = cv2.imencode(".jpg", preview,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), REALTIME_PREVIEW_QUALITY])
            if ok:
                with _realtime["lock"]:
                    _realtime["jpeg"] = enc.tobytes()
            return self._json({
                "status": res["status"], "items": res["items"],
                "detections": res["detections"], "person_box": res.get("person_box"),
                "person_index": res.get("person_index"),
                "extra_people": res.get("extra_people", 0),
                "width": w, "height": h, "infer_ms": ms,
                "saved": saved, "saved_total": _realtime["saved"],
                # What samples are saved at: [w, h] of the frame judged; null while the
                # native stream is still opening (nothing is saved then).
                "sample_size": [src.shape[1], src.shape[0]]
                               if full is not None or not _state.get("realtime_full") else None,
            })

        # The exact frame the boxes were computed from, so the overlay always lines up.
        if u.path == "/api/realtime_image":
            with _realtime["lock"]:
                buf = _realtime["jpeg"]
            if not buf:
                return self._err(404, "No realtime frame yet")
            return self._send(200, buf, "image/jpeg")

        if u.path == "/api/records":
            return self._records()

        if u.path == "/api/record_image":
            from urllib.parse import parse_qs
            return self._record_image((parse_qs(u.query).get("file") or [""])[0])

        if u.path == "/api/recording":
            if _recorder is None:
                return self._json({"available": False})
            return self._json(dict(_recorder.status(), available=True))

        if u.path == "/api/recordings":
            if _recorder is None:
                return self._json({"available": False, "rows": []})
            return self._json({"available": True, "dir": _recorder.out_dir,
                               "rows": _recorder.recordings()})

        if u.path.startswith("/recordings/"):
            # Only names the recorder itself makes, from its own folder — never a path.
            from recorder import NAME_RE
            name = u.path[len("/recordings/"):]
            if _recorder is None or not NAME_RE.match(name):
                return self._err(404, "No such recording")
            path = os.path.join(_recorder.out_dir, name)
            if not os.path.isfile(path):
                return self._err(404, "No such recording")
            ctype = "video/x-matroska" if name.endswith(".mkv") else "application/x-ndjson"
            return self._send_file(path, ctype, name)

        if u.path.startswith("/api/frame/"):
            fid = u.path.rsplit("/", 1)[-1]
            f = _frames.get(fid)
            if not f:
                return self._err(404, "Frame expired or unknown")
            return self._send(200, f["jpeg"], "image/jpeg")

        return self._err(404, "No such route")

    # ── POST ──────────────────────────────────────────────────────────────
    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0:
            return self._err(400, "Empty body")

        # Handled before the read() below, which would pull the whole model into RAM.
        if u.path == "/api/model_upload":
            return self._model_upload(u, n)
        if n > _state["max_upload"]:
            return self._err(413, "Payload too large")
        body = self.rfile.read(n)

        # One captured frame: decode, run the model, cache it, return its detections.
        if u.path == "/api/frame":
            if _state.get("swapping"):
                return self._err(503, "Model is being switched — try again in a moment")
            arr = np.frombuffer(body, dtype=np.uint8)
            bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if bgr is None:
                return self._err(400, "Could not decode that file as an image")
            dets, ms = detect(bgr)
            ok, enc = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), CAPTURE_JPEG_QUALITY])
            if not ok:
                return self._err(500, "Could not re-encode frame")
            h, w = bgr.shape[:2]
            fid = store_frame(enc.tobytes(), dets, w, h)
            return self._json({"id": fid, "width": w, "height": h,
                               "detections": dets, "infer_ms": ms})

        # Swap the live model. The response is withheld until the new model is warm and
        # actually serving, so a 200 here means the next check really used it. Measured on
        # this Jetson with the process already warm: ~1.4 s for .pt, ~1.0 s for .engine.
        # (The ~30 s in run_gate_native.sh is cold CUDA/torch import, paid once at boot.)
        if u.path == "/api/model":
            if not _state.get("can_switch"):
                return self._err(403, "Model switching is disabled (--lock-model)")
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                return self._json(switch_model(str(req.get("file") or ""),
                                               bool(req.get("force"))))
            except FileNotFoundError as e:
                return self._err(404, str(e))
            except ValueError as e:
                return self._err(409, str(e))
            except Exception as e:
                return self._err(500, f"Could not load that model: {e}")

        if u.path == "/api/model_yaml_upload":
            return self._model_yaml_upload(u, len(body), body)

        # Rename a checklist item (its on-screen heading). Same --lock-model gate as the
        # other checklist edits: it changes gate.json, and it changes captures.csv's
        # column names (see set_item_label for the .bak rotation that follows).
        if u.path == "/api/item_label":
            if not _state.get("can_switch"):
                return self._err(403, "Checklist remapping is disabled (--lock-model)")
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                return self._json(set_item_label(str(req.get("label") or ""),
                                                 str(req.get("new_label") or "")))
            except ValueError as e:
                return self._err(400, str(e))
            except Exception as e:
                return self._err(500, f"Could not rename the item: {e}")

        # Switch a checklist item in/out of the verdict. Same --lock-model gate as the
        # other checklist edits; see set_item_enabled for the last-item refusal.
        if u.path == "/api/item_enabled":
            if not _state.get("can_switch"):
                return self._err(403, "Checklist remapping is disabled (--lock-model)")
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                return self._json(set_item_enabled(str(req.get("label") or ""),
                                                   bool(req.get("enabled"))))
            except ValueError as e:
                return self._err(400, str(e))
            except Exception as e:
                return self._err(500, f"Could not switch the item: {e}")

        # Remap which of the active model's classes satisfies one checklist item, and
        # persist the change to config/gate.json so it survives a restart. This is what
        # lets a model trained by someone else, with a different class vocabulary, drive
        # the same three-item checklist without editing the file by hand.
        if u.path == "/api/item_conf":
            if not _state.get("can_switch"):
                return self._err(403, "Checklist remapping is disabled (--lock-model)")
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                return self._json(set_item_conf(str(req.get("label") or ""),
                                                req.get("conf")))
            except ValueError as e:
                return self._err(400, str(e))
            except Exception as e:
                return self._err(500, f"Could not update the threshold: {e}")

        if u.path == "/api/person_conf":
            if not _state.get("can_switch"):
                return self._err(403, "Checklist remapping is disabled (--lock-model)")
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                return self._json(set_person_conf(req.get("conf")))
            except ValueError as e:
                return self._err(400, str(e))
            except Exception as e:
                return self._err(500, f"Could not update the worker threshold: {e}")

        if u.path == "/api/image_dwell":
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                return self._json(set_image_dwell(req.get("seconds")))
            except ValueError as e:
                return self._err(400, str(e))

        if u.path == "/api/away_fraction":
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                return self._json(set_away_fraction(req.get("fraction")))
            except ValueError as e:
                return self._err(400, str(e))

        if u.path == "/api/exit_area_fraction":
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                return self._json(set_exit_area_fraction(req.get("fraction")))
            except ValueError as e:
                return self._err(400, str(e))

        if u.path == "/api/track_person_conf":
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                return self._json(set_track_person_conf(req.get("conf")))
            except ValueError as e:
                return self._err(400, str(e))

        if u.path == "/api/person_class":
            if not _state.get("can_switch"):
                return self._err(403, "Checklist remapping is disabled (--lock-model)")
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                return self._json(set_person_class(str(req.get("class") or "")))
            except ValueError as e:
                return self._err(400, str(e))
            except Exception as e:
                return self._err(500, f"Could not update the worker class: {e}")

        if u.path == "/api/sensor_trigger":
            # Runtime on/off for the through-beam sensor, independent of --sensor-pin
            # (which pin — a hardware/deployment fact, fixed at startup) the same way
            # --model's initial value and a live model switch are independent: this is
            # operational state, not part of the checklist, so unlike person_conf/
            # person_class it is NOT written to gate.json — it resets to armed on
            # restart, same as the CLI flag always implied before this toggle existed.
            if _state.get("sensor_pin") is None:
                return self._err(409, "No --sensor-pin configured on this server")
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            enabled = bool(req.get("enabled"))
            _state["sensor_enabled"] = enabled
            print(f"[sensor] {'enabled' if enabled else 'disabled'} via UI", flush=True)
            return self._json({"sensor_pin": _state["sensor_pin"], "sensor_enabled": enabled})

        # Runtime on/off for the image trigger. Operational state like sensor_trigger:
        # not persisted, comes back ON at restart (unless --no-image-trigger).
        if u.path == "/api/image_trigger":
            if not _state.get("camera"):
                return self._err(409, "Camera is disabled on this server")
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            enabled = bool(req.get("enabled"))
            _state["image_trigger_enabled"] = enabled
            print(f"[image] trigger {'enabled' if enabled else 'disabled'} via UI", flush=True)
            return self._json(_image_trigger_status())

        # IT reporting on/off. Unlike the trigger switches this IS persisted (into
        # config/it.json): whether the gate reports to the site's IT service is a
        # deployment decision, and a reboot must not silently change it.
        # Where IT reports go: one of config/it.json's "targets" (the site service, or the
        # mock on this box). Persisted like the on/off switch, shown on the kiosk.
        if u.path == "/api/it_target":
            if _it is None:
                return self._err(409, "IT reporting is not available on this server")
            try:
                name = str(json.loads(body).get("target") or "")
            except Exception:
                return self._err(400, "Body must be JSON")
            if name not in (_it.cfg.get("targets") or {}):
                return self._err(400, f"no IT target {name!r}")
            try:
                from it_report import save_target
                save_target(_state["it_config_path"], name)
            except Exception as e:
                return self._err(500, f"Could not save {_state['it_config_path']}: {e}")
            _it.set_target(name)
            s = _it.status()
            s.update(available=True, device_status=device_status())
            return self._json(s)

        if u.path == "/api/it_enable":
            if _it is None:
                return self._err(409, "IT reporting is not available on this server "
                                      "(no config/it.json, or recording is off)")
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            enabled = bool(req.get("enabled"))
            try:
                from it_report import save_enabled
                save_enabled(_state["it_config_path"], enabled)
            except Exception as e:
                return self._err(500, f"Could not save {_state['it_config_path']}: {e}")
            if enabled:
                _it.start(fresh=True)
            else:
                _it.stop()
            print(f"[it] reporting {'ON' if enabled else 'OFF'} via UI "
                  f"(saved to {_state['it_config_path']})", flush=True)
            s = _it.status()
            s.update(available=True, device_status=device_status())
            return self._json(s)

        # Speaker test: speak a line, flash one lamp, or switch everything off. Operator
        # driven, on demand — nothing in the check path calls this.
        if u.path == "/api/tower":
            if _tower is None:
                return self._err(409, "No signal tower configured on this server (--tower-host)")
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            action = req.get("action")
            try:
                if action == "speak":
                    text, lang = str(req.get("text") or ""), str(req.get("lang") or "en")
                    _tower.speak(text, lang)
                    msg = f"Speaking ({lang}): {text.strip()}"
                elif action == "flash":
                    lamp = str(req.get("lamp") or "")
                    if lamp not in LAMPS:
                        raise ValueError(f"lamp must be one of {', '.join(LAMPS)}")
                    # Through the light policy, not a raw command: the gate re-sends its own
                    # state every second and would wipe a raw flash at once. A test flash
                    # outranks everything, red steady included, for its few seconds.
                    _light_policy.flash({"red": "red_flash", "amber": "yellow_flash",
                                         "green": "green_flash"}[lamp],
                                        TOWER_TEST_FLASH_S, time.monotonic(), tag="test",
                                        beats_abnormal=True)
                    if _light is not None:
                        _light.wake()
                    # The manual calls it amber; the page (and the operator) say yellow.
                    shown = {"amber": "yellow"}.get(lamp, lamp)
                    msg = f"{shown} lamp flashing for {TOWER_TEST_FLASH_S} s"
                elif action == "off":
                    _light_policy.end("test")
                    if _light is not None:
                        _light.wake()
                    _tower.control(stop=1)
                    msg = "Test stopped — sound off, the light is back under the gate's control"
                else:
                    return self._err(400, "action must be speak, flash or off")
            except ValueError as e:
                return self._err(400, str(e))
            except TowerError as e:
                print(f"[tower] test {action} failed: {e}", flush=True)
                return self._err(502, str(e))
            print(f"[tower] test via UI: {msg}", flush=True)
            return self._json({"ok": True, "message": msg})

        # Speaker volume 0-15, written through the tower's web UI (~3 s, no restart),
        # then 「檢測通過」 played once so the operator hears the new level.
        if u.path == "/api/tower_volume":
            if _tower is None or _tower_web is None:
                return self._err(409, "No tower, or no config/tower.local.json with its web login")
            try:
                req = json.loads(body)
                vol = int(req.get("volume"))
                mute = req.get("mute")          # absent = leave the tower's Mute as it is
                if mute is not None and not isinstance(mute, bool):
                    raise ValueError
            except Exception:
                return self._err(400, "Body must be JSON with an integer volume "
                                      "(and optionally a true/false mute)")
            try:
                with _tower_web_lock:
                    out = _tower.set_volume(vol, _tower_web["web_user"], _tower_web["web_pass"],
                                            mute=mute)
            except ValueError as e:
                return self._err(400, str(e))
            except TowerError as e:
                print(f"[tower] volume -> {vol} failed: {e}", flush=True)
                return self._err(502, str(e))
            print(f"[tower] speaker volume set to {vol}/{_tower.VOLUME_MAX}"
                  f"{', MUTED' if out.get('mute') else ''} via UI", flush=True)
            if req.get("preview", True) and not out.get("mute"):
                say("pass")
            return self._json(out)

        if u.path == "/api/trigger_area":
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                return self._json(set_trigger_min_area(req.get("area")))
            except ValueError as e:
                return self._err(400, str(e))
            except Exception as e:
                return self._err(500, f"Could not update the area threshold: {e}")

        if u.path == "/api/rfid_min_rssi":
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                return self._json(set_rfid_min_rssi(req.get("rssi")))
            except ValueError as e:
                return self._err(400, str(e))
            except Exception as e:
                return self._err(500, f"Could not update the RSSI threshold: {e}")

        if u.path == "/api/recording":
            if _recorder is None:
                return self._err(409, "Recording needs the camera, which is off on this server")
            try:
                on = bool(json.loads(body).get("on"))
            except Exception:
                return self._err(400, "Body must be JSON")
            from recorder import RecorderError
            try:
                out = _recorder.start(_recording_meta()) if on else _recorder.stop()
            except RecorderError as e:
                return self._err(409, str(e))
            except Exception as e:
                return self._err(500, f"Could not {'start' if on else 'stop'} recording: "
                                      f"{type(e).__name__}: {e}")
            return self._json(dict(out, available=True))

        if u.path == "/api/rfid_settings":
            if _rfid is None:
                return self._err(409, "No RFID reader on this server")
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                out = set_rfid_settings(req.get("power_dbm"), req.get("rf_mode"),
                                        req.get("antenna"))
            except ValueError as e:
                return self._err(400, str(e))
            return self._json(out) if out.get("ok") else self._err(422, out.get("error", "refused"))

        if u.path == "/api/direction_source":
            try:
                src = json.loads(body).get("source")
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                return self._json(set_direction_source(src))
            except ValueError as e:
                return self._err(400, str(e))

        if u.path == "/api/door_side":
            try:
                side = json.loads(body).get("side")
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                return self._json(set_door_side(side))
            except ValueError as e:
                return self._err(400, str(e))

        if u.path == "/api/trigger_zone":
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                return self._json(set_trigger_zone(req.get("left"), req.get("right")))
            except ValueError as e:
                return self._err(400, str(e))
            except Exception as e:
                return self._err(500, f"Could not update the door zone: {e}")

        if u.path == "/api/item_class":
            if not _state.get("can_switch"):
                return self._err(403, "Checklist remapping is disabled (--lock-model)")
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            try:
                return self._json(set_item_class(str(req.get("label") or ""),
                                                  str(req.get("class") or ""),
                                                  req.get("negative")))
            except (KeyError, ValueError) as e:
                return self._err(400, str(e))
            except Exception as e:
                return self._err(500, f"Could not update the checklist: {e}")

        # Evaluate the checklist over a burst of already-detected frames.
        if u.path == "/api/check":
            try:
                req = json.loads(body)
            except Exception:
                return self._err(400, "Body must be JSON")
            ids = req.get("frames") or []
            worker = str(req.get("worker_id") or "").strip()
            if not ids:
                return self._err(400, "No frames supplied")
            missing = [i for i in ids if i not in _frames]
            if missing:
                return self._err(410, f"Frame(s) no longer cached: {', '.join(missing)}")

            return self._json(finalize_check(ids, worker, preview=bool(req.get("preview"))))

        # Grab a live burst straight off the camera, detect each frame, then vote.
        # This is the Phase-2 path: same association/voting/display as /api/check,
        # but the frames come from the RTSP stream instead of an upload.
        # Thin adapter: the check itself lives in run_gate_check() so the RFID reader and
        # the through-beam sensor can trigger exactly the same thing without going through
        # HTTP to reach it.
        if u.path == "/api/capture":
            try:
                req = json.loads(body) if body else {}
            except Exception:
                req = {}
            worker = str(req.get("worker_id") or "").strip()
            try:
                # Same lock the sensor trigger holds — see _gate_check_lock's comment.
                # A button press mid-sensor-burst waits its turn instead of interleaving.
                with _gate_check_lock:
                    return self._json(run_gate_check(worker, source="api"))
            except GateNotReady as e:
                return self._err(e.http_status, str(e))

        # Stops the whole process. No graceful HTTP-triggered stop existed before this —
        # the only prior path was Ctrl-C reaching the main thread's KeyboardInterrupt.
        # A request handler runs on its own thread (ThreadingHTTPServer), so it can't
        # raise KeyboardInterrupt there; os._exit() from a short-lived helper thread
        # instead, timed to fire after this response has gone out. Daemon threads
        # (camera/recorder/audio) need no explicit join — the process exit reclaims them.
        # Recovery depends entirely on whatever restarts the process: with the
        # ppe-gate.service systemd unit (Restart=always) that's a few seconds; without
        # it, the gate stays down until someone runs run_gate_native.sh by hand — see
        # CLAUDE.md and the ppe-gate-shutdown-button memory note before relying on this
        # in a deployment that doesn't have the unit installed.
        if u.path == "/api/shutdown":
            print(f"[shutdown] requested by {self.client_address[0]}", flush=True)
            _state["stop"] = True

            def _die():
                time.sleep(0.3)
                os._exit(0)

            threading.Thread(target=_die, daemon=True).start()
            return self._json({"ok": True, "message": "Shutting down"})

        return self._err(404, "No such route")



CAMERA_LOCAL = "config/camera.local.json"


def default_rtsp() -> str:
    """Camera URL from, in order: CAM_RTSP env, config/camera.local.json, placeholder.

    The URL carries the camera password, so the real one lives in a gitignored local
    file instead of in source that gets pushed to GitHub. Losing that file costs a
    two-line edit; leaking it in git history costs a password rotation.
    """
    env = os.environ.get("CAM_RTSP")
    if env:
        return env
    path = os.path.join(PKG_ROOT, CAMERA_LOCAL)
    try:
        with open(path) as fh:
            url = json.load(fh).get("rtsp")
        if url:
            return url
    except FileNotFoundError:
        print(f"NOTE: {CAMERA_LOCAL} not found — copy camera.local.json.example and "
              "fill in the camera password, or set CAM_RTSP.", flush=True)
    except Exception as e:
        print(f"NOTE: could not read {CAMERA_LOCAL} ({e})", flush=True)
    return "rtsp://user:password@192.168.1.105:554/?h26x=4&line=1&inst=1"


def _antenna_list(text: str) -> list[int]:
    """--rfid-antenna: "1" or "1,3" → [1, 3]. The MPK-R-9504 has ports 1-4."""
    try:
        ants = [int(a) for a in str(text).split(",") if a.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port list: {text!r} (e.g. 1 or 1,3)")
    if not ants or len(set(ants)) != len(ants) or not all(1 <= a <= 4 for a in ants):
        raise argparse.ArgumentTypeError(f"ports must be distinct, 1-4: {text!r}")
    return ants


def main():
    ap = argparse.ArgumentParser(description="PPE gate kiosk (Phase 1)")
    ap.add_argument("--model", default="models/ppe.pt")
    ap.add_argument("--config", default="config/gate.json")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--device", default="0")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--max-upload-mb", type=int, default=40)
    ap.add_argument("--models-dir", default=None,
                    help="directory the model browser lists (default: alongside --model)")
    ap.add_argument("--mp3-dir", default=None,
                    help="directory holding check_pass.mp3 / check_fail.mp3 / "
                         "person_detect_fail.mp3 (default: <pkg>/MP3)")
    ap.add_argument("--audio-timeout", type=float, default=10.0,
                    help="give up on a clip that has not finished in this many seconds")
    ap.add_argument("--no-audio", action="store_true",
                    help="do not announce results out loud")
    ap.add_argument("--record-dir", default=None,
                    help="where captures.csv and dataset/ are written (default: package root)")
    ap.add_argument("--record-max", type=int, default=0,
                    help="cap captures as a FIFO ring, deleting the oldest row's image and "
                         "label as new ones arrive; 0 (default) keeps every capture")
    ap.add_argument("--label-conf", type=float, default=0.4,
                    help="minimum score for a detection to be written to the label file")
    ap.add_argument("--label-primary-only", action="store_true",
                    help="label only the closest person and the PPE inside them, instead "
                         "of every detection (see yolo_label_lines for the trade-off)")
    ap.add_argument("--rfid-host", default=None,
                    help="MPK-R-9504 reader address; omitted means no RFID (the default)")
    ap.add_argument("--rfid-port", type=int, default=8888)
    ap.add_argument("--rfid-antenna", type=_antenna_list, default=[1],
                    help="antenna port(s) on the reader box: 1, or e.g. 1,3 — several take "
                         "turns (RfidService.ANT_DWELL_S each) and a badge counts at its "
                         "strongest on any of them")
    ap.add_argument("--rfid-power", type=float, default=20.0, help="dBm")
    ap.add_argument("--rfid-before", type=float, default=3.0,
                    help="seconds BEFORE the trigger to consider; the approach is the "
                         "informative part, since RSSI rises as the worker walks in")
    ap.add_argument("--rfid-after", type=float, default=1.0,
                    help="seconds AFTER the trigger to consider. The burst already covers "
                         "most of this, so only the remainder is ever waited for")
    ap.add_argument("--sensor-pin", type=int, default=None, choices=(1, 2, 3),
                    help="through-beam sensor's GPIO input pin (1-3) on the RFID box — "
                         "a level of 1 on this pin fires a gate check exactly like the "
                         "CAPTURE & CHECK button. Requires --rfid-host. Omitted means no "
                         "sensor trigger. Starts switched OFF either way; the web UI's "
                         "Sensor Trigger toggle turns it on")
    ap.add_argument("--whitelist", default=None,
                    help="badge whitelist (default: config/whitelist.json). Absent or "
                         "\"enabled\": false means every badge is allowed; unreadable "
                         "means none is. Re-read on change, no restart needed")
    ap.add_argument("--tower-host", default="192.168.10.1",
                    help="PATLITE signal tower (tri-colour light + voice) address; \"\" "
                         "disables it. Only the page's Speaker test uses it so far")
    ap.add_argument("--it-config", default=None,
                    help="IT reporting settings (default: config/it.json). Absent, or "
                         "\"enabled\": false, means nothing is sent anywhere")
    ap.add_argument("--no-image-trigger", action="store_true",
                    help="start with the image trigger (the default trigger: a person "
                         "standing close for --image-dwell seconds fires a check) "
                         "switched off; the web UI can turn it on later")
    ap.add_argument("--image-dwell", type=float, default=1.2,
                    help="seconds one person must stay inside the door zone, above the "
                         "area threshold, before the image trigger fires (default 2.0). "
                         f"Detection dropouts shorter than IMAGE_TRIGGER_MISS_S "
                         f"({IMAGE_TRIGGER_MISS_S:g} s) do not reset it")
    ap.add_argument("--realtime-interval", type=float, default=2.0,
                    help="seconds between saved samples in realtime mode (default 2.0; "
                         "at ~172 KB a sample that is ~300 MB/hour)")
    ap.add_argument("--realtime-max", type=int, default=5000,
                    help="most realtime samples kept before the oldest are deleted "
                         "(default 5000: ~860 MB at 1280x960, ~5.5 GB at the camera's "
                         "native 5 MP); 0 disables pruning")
    ap.add_argument("--realtime-gate-res", action="store_true",
                    help="realtime detects and saves the gate's own --cap-width x "
                         "--cap-height frame instead of opening a second, native-"
                         "resolution camera stream while it runs")
    ap.add_argument("--no-record", action="store_true",
                    help="do not save triggered captures to the dataset")
    ap.add_argument("--max-model-mb", type=int, default=512,
                    help="largest model file accepted by the upload picker")
    ap.add_argument("--lock-model", action="store_true",
                    help="serve the browser read-only; refuse runtime model switches")
    ap.add_argument("-v", "--verbose", action="store_true")
    # ── live camera (Phase 2) ──
    ap.add_argument("--rtsp", default=default_rtsp(),
                    help="Bosch RTSP URL (low-latency NVDEC pipeline is built around it); "
                         "normally comes from config/camera.local.json — see default_rtsp()")
    ap.add_argument("--no-camera", action="store_true",
                    help="disable the live stream; run the Phase-1 file-upload UI only")
    ap.add_argument("--cap-width", type=int, default=1280,
                    help="hardware-scaled capture width fed to detection + stream")
    ap.add_argument("--cap-height", type=int, default=960)
    ap.add_argument("--rtsp-protocol", choices=("tcp", "udp"), default="tcp",
                    help="how rtspsrc carries RTP — tcp (default) interleaves it in the "
                         "RTSP connection (no UDP loss, but a lost segment stalls "
                         "everything behind it); udp is raw RTP (no stalls, a lost "
                         "packet just corrupts/drops that frame). Measured near-identical "
                         "average latency on this camera's direct link; see "
                         "build_camera_pipeline()'s docstring for the actual numbers")
    ap.add_argument("--stream-height", type=int, default=720, help="MJPEG display height")
    ap.add_argument("--stream-fps", type=int, default=15, help="MJPEG feed FPS to the browser")
    ap.add_argument("--stream-quality", type=int, default=80, help="MJPEG JPEG quality 1-100")
    ap.add_argument("--burst-interval", type=float, default=0.08,
                    help="seconds between frames in a live capture burst. This pause is "
                         "deliberate, and it is the single largest part of a tap: voting "
                         "only means anything if the frames DIFFER, and the camera "
                         "delivers a new frame every ~40-80 ms, so grabbing faster than "
                         "that samples the same image twice and turns corroboration into "
                         "a duplicated vote. 0.08 s is therefore about the floor; going "
                         "lower makes taps look faster while quietly weakening the check.")
    args = ap.parse_args()

    if not os.path.isfile(args.model):
        raise SystemExit(f"Model not found: {args.model} (cwd {os.getcwd()})")

    device = args.device
    if device != "cpu":
        try:
            import torch
            if not torch.cuda.is_available():
                print("CUDA not available — falling back to CPU.")
                device = "cpu"
        except Exception:
            device = "cpu"

    cfg = load_config(args.config)
    # Trigger-layer thresholds, see set_trigger_min_area / set_rfid_min_rssi. Filled in
    # here rather than in ppe_check.DEFAULT_CONFIG because evaluate() never uses them.
    cfg.setdefault("trigger_min_area", TRIGGER_MIN_AREA_DEFAULT)
    cfg.setdefault("rfid_min_rssi", RFID_MIN_RSSI_DEFAULT)
    cfg.setdefault("trigger_zone", list(TRIGGER_ZONE_DEFAULT))
    cfg.setdefault("track_person_conf", TRACK_PERSON_CONF_DEFAULT)
    cfg.setdefault("exit_area_fraction", EXIT_AREA_FRACTION_DEFAULT)
    cfg.setdefault("away_fraction", AWAY_FRACTION_DEFAULT)
    cfg.setdefault("burst_before", BURST_BEFORE_DEFAULT)
    cfg["image_dwell"] = _dwell_from(cfg, args.image_dwell)   # so the page can show it

    # The model the operator last switched to from the page wins over --model, which
    # is only the starting point for a config that has never seen a switch. It has to
    # be an existing file in the models directory (the page can only ever pick one of
    # those); anything else falls back to the flag, loudly. --lock-model means "the
    # command line decides", so it also ignores the remembered choice.
    models_dir = os.path.realpath(
        args.models_dir or os.path.dirname(os.path.abspath(args.model)))
    model_path = args.model
    remembered = cfg.get("model")
    if remembered and not args.lock_model:
        cand = os.path.join(models_dir, os.path.basename(str(remembered)))
        if os.path.isfile(cand):
            model_path = cand
            print(f"==> Model remembered from {args.config}: {remembered} "
                  f"(overrides --model {os.path.basename(args.model)})")
        else:
            print(f"WARNING: {args.config} remembers model '{remembered}' but it is not "
                  f"in {models_dir} — using --model {args.model} instead")

    _state.update({
        "cfg": cfg, "config_path": args.config, "imgsz": args.imgsz, "device": device,
        "model_path": model_path, "verbose": args.verbose,
        "max_upload": args.max_upload_mb * MB,
        "camera": not args.no_camera,
        "stream_h": args.stream_height, "stream_fps": args.stream_fps,
        "stream_q": max(1, min(100, args.stream_quality)),
        "burst_interval": max(0.0, args.burst_interval),
        "models_dir": models_dir,
        "can_switch": not args.lock_model,
        "max_model": args.max_model_mb * MB,
        "audio": not args.no_audio,
        "mp3_dir": os.path.realpath(
            args.mp3_dir or os.path.join(PKG_ROOT, "MP3")),
        "audio_timeout": max(1.0, args.audio_timeout),
        "record": not args.no_record,
        "rfid_before": max(0.0, args.rfid_before),
        "rfid_after": max(0.0, args.rfid_after),
        "sensor_pin": args.sensor_pin,
        # Starts OFF even when a pin is configured: the image trigger is the default
        # now, and two triggers armed at once would double-fire on the same worker (a
        # beam break and a 3 s dwell describe the same arrival). The web UI's toggle
        # (/api/sensor_trigger) arms it; the pin stays known so that toggle works.
        "sensor_enabled": False,
        "image_trigger_enabled": not args.no_image_trigger and not args.no_camera,
        # gate.json (the page's 「停留」) over --image-dwell — see IMAGE_DWELL_RANGE.
        "image_dwell": _dwell_from(cfg, args.image_dwell),
        "realtime_interval": max(0.0, args.realtime_interval),
        "realtime_max": max(0, args.realtime_max),
        "realtime_full": not args.realtime_gate_res,
        "record_dir": os.path.realpath(
            args.record_dir or PKG_ROOT),
        "record_max": max(0, args.record_max),
        "label_conf": args.label_conf,
        "label_primary_only": args.label_primary_only,
        "swapping": False,
        "stop": False,
    })

    if _state.get("audio"):
        if not shutil.which("gst-launch-1.0"):
            print("WARNING: gst-launch-1.0 not found — result announcements disabled.")
            _state["audio"] = False
        else:
            # The Jetson copies of the tower's voices — played only when there is no tower.
            missing = [c for _, c in VOICE.values()
                       if not os.path.isfile(os.path.join(_state["mp3_dir"], c))]
            if missing:
                print(f"WARNING: missing in {_state['mp3_dir']}: {', '.join(missing)}")
            threading.Thread(target=audio_loop, daemon=True).start()
            print(f"==> Announcing results from {_state['mp3_dir']}")

    missing_ui = [n for n, _ in WEB_ASSETS.values()
                  if not os.path.isfile(os.path.join(WEB_DIR, n))]
    if missing_ui:
        raise SystemExit(f"ERROR: missing UI file(s) in {WEB_DIR}: {', '.join(missing_ui)}\n"
                         "       The kiosk page is served from there; the package is incomplete.")

    _state["record_csv"] = os.path.join(_state["record_dir"], "captures.csv")
    if _state.get("record"):
        os.makedirs(os.path.join(_state["record_dir"], "dataset", "images", "train"), exist_ok=True)
        os.makedirs(os.path.join(_state["record_dir"], "dataset", "labels", "train"), exist_ok=True)
        threading.Thread(target=recorder_loop, daemon=True).start()
        cap_msg = f"max {_state['record_max']}" if _state["record_max"] > 0 else "no cap"
        print(f"==> Recording triggers to {_state['record_csv']} "
              f"({cap_msg}, labels >= {_state['label_conf']})")

    global _whitelist
    _whitelist = Whitelist(args.whitelist or os.path.join(PKG_ROOT, "config", "whitelist.json"))
    wl = _whitelist.state()
    print(f"==> Whitelist {wl['path']}: "
          + ("absent — every badge allowed" if not wl["exists"]
             else "switched off — every badge allowed" if not wl["enabled"]
             else f"UNREADABLE ({wl['error']}) — every badge refused" if wl["error"]
             else f"{wl['count']} ID(s)"))

    # No probe here: an unplugged tower must not delay start-up, and the Speaker test
    # popup reports reachability itself when opened.
    if args.tower_host:
        global _tower, _tower_web
        _tower = Tower(args.tower_host)
        web_path = os.path.join(PKG_ROOT, "config", "tower.local.json")
        try:
            with open(web_path, encoding="utf-8") as fh:
                _tower_web = json.load(fh)
            _tower_web["web_user"], _tower_web["web_pass"]      # both must be present
        except FileNotFoundError:
            _tower_web = None
        except Exception as e:
            _tower_web = None
            print(f"WARNING: {web_path} unreadable ({e}) — speaker volume control off")
        print(f"==> Signal tower at {args.tower_host} (Speaker test on the page)")

    # IT reporting reads events.jsonl, which only the recorder writes — so it needs
    # recording on. Off unless config/it.json exists AND says "enabled": true.
    from it_report import ITReporter, load_config as load_it_config
    it_path = args.it_config or os.path.join(PKG_ROOT, "config", "it.json")
    try:
        it_cfg = load_it_config(it_path)
    except Exception as e:
        it_cfg = None
        print(f"WARNING: could not read {it_path} ({e}) — IT reporting off")
    _state["it_config_path"] = it_path
    if it_cfg and _state.get("record"):
        # Created whenever it CAN report, so the kiosk's switch can start it later; only
        # started now if the config says so.
        global _it
        _it = ITReporter(it_cfg, _state["record_dir"], device_status,
                         lambda: _state.get("stop"))
        if it_cfg["enabled"]:
            _it.start()
        print(f"==> IT reporting {'ON' if it_cfg['enabled'] else 'off (switch on from the kiosk)'}: "
              f"fabArea={it_cfg['fab_area']} clientId={it_cfg['client_id']} "
              f"heartbeat every {it_cfg['heartbeat_interval_s']}s")
    else:
        why = "no config" if not it_cfg else "recording is off"
        print(f"==> IT reporting unavailable ({why}; {it_path})")

    if args.sensor_pin and not args.rfid_host:
        raise SystemExit("ERROR: --sensor-pin requires --rfid-host — the through-beam "
                         "sensor's DI is wired into the RFID box, there is no separate "
                         "connection for it.")

    if args.rfid_host:
        from rfid_reader import RfidService
        global _rfid
        # Power and receive mode: gate.json's (set from the RFID popup) win over the
        # command line, like the model does — the operator tunes them on site.
        _rfid = RfidService(args.rfid_host, args.rfid_port, antenna=args.rfid_antenna,
                            power_dbm=float(cfg.get("rfid_power_dbm", args.rfid_power)),
                            rf_mode=int(cfg.get("rfid_rf_mode", RFID_MODE_DEFAULT)),
                            antenna_settings=_antenna_settings_from(cfg))
        print("==> RFID " + "; ".join(
            f"antenna {a}: {s['power_dbm']:g} dBm, receive mode {s['rf_mode']} "
            f"({RFID_MODES.get(s['rf_mode'], ('?', '?'))[0]})" for a, s in _rfid.settings().items())
            + (f" — taking turns {RfidService.ANT_DWELL_S:g} s each"
               if len(_rfid.antennas) > 1 else ""))
        if args.sensor_pin:
            _rfid.on_gpio_change = sensor_triggered
            print(f"==> Through-beam trigger on GPIO IN{args.sensor_pin} available "
                  f"(starts OFF; level 1 fires a check, {SENSOR_COOLDOWN_S}s cooldown)")
        _rfid.start()
        print(f"==> RFID identity from {args.rfid_host}:{args.rfid_port} "
              f"(window T-{args.rfid_before}s..T+{args.rfid_after}s)")

    load_model(_state["model_path"], device, args.imgsz)

    if _state.get("camera"):
        pipeline = build_camera_pipeline(args.rtsp, args.cap_width, args.cap_height,
                                         args.rtsp_protocol)
        threading.Thread(target=camera_loop, args=(pipeline,), daemon=True).start()
        global _recorder
        from recorder import Recorder
        _recorder = Recorder(args.rtsp, os.path.join(_state["record_dir"], "recordings"),
                             args.rtsp_protocol)
        if _state["realtime_full"]:
            # Opened on demand by the first realtime poll, never at startup.
            _camera_full["pipeline"] = build_camera_pipeline(args.rtsp, 0, 0, args.rtsp_protocol)
            print("==> Realtime samples at the camera's native resolution "
                  "(second stream, open only while realtime runs)")
        threading.Thread(target=image_trigger_loop, name="ImageTrigger", daemon=True).start()
        zl, zr = cfg["trigger_zone"]
        print(f"==> Image trigger {'ON' if _state['image_trigger_enabled'] else 'off'}: "
              f"Person box >= {cfg['trigger_min_area']} px², centre within "
              f"{zl:.0%}..{zr:.0%} of frame width, for {_state['image_dwell']:g}s "
              f"fires a check; the strongest badge >= {cfg['rfid_min_rssi']:g} dBm is the worker"
              + ("" if args.rfid_host else " (no reader: worker ID left blank)"))

    if _tower is not None:
        global _light
        _light = LightDriver(_tower, _light_policy,
                             lambda: (gate_abnormal(), gate_checking()),
                             log=lambda m: print(m, flush=True))
        _light.start()
        print(f"==> Tower light driven by the gate: base red steady, refreshed every second "
              f"(falls back to red within a few seconds if the gate stops)")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("\n" + "=" * 62)
    print(f"  PPE Gate Kiosk  →  http://localhost:{args.port}")
    print("=" * 62)
    print(f"  checklist : {', '.join(i['label'] for i in cfg['items'])}")
    print(f"  rule      : all items must be seen in >= {cfg['votes_required']} "
          f"of {cfg['frames']} frames, on the closest person")
    print(f"  device    : {device}")
    if _state.get("camera"):
        safe = args.rtsp
        if "@" in safe:
            safe = "rtsp://***@" + safe.split("@", 1)[1]
        print(f"  camera    : {safe}")
        print(f"              capture {args.cap_width}x{args.cap_height} · "
              f"stream {args.stream_height}p@{args.stream_fps}fps")
    else:
        print("  camera    : disabled (--no-camera)")
    print("  Ctrl-C to stop.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        _state["stop"] = True


if __name__ == "__main__":
    main()
