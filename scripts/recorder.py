"""
On-site recording for replay (operator, 2026-10-07: 「把現場實際辨識的影片存下來讓我們來回測、改善」).

Two files per recording, side by side in <record dir>/recordings/:

  <YYYYmmdd_HHMMSS>.mkv    the camera's own H.264 stream as it arrives — no decode, no
                           re-encode, so native resolution (2592x1944 on this camera),
                           every frame, and next to no CPU. Matroska, not MP4: killed
                           mid-recording (a crash, a service restart) a .mkv still read 143
                           of ~150 frames back, a fragmented MP4 only 32, a plain one none
                           (measured 2026-10-08). Plays in VLC / Windows Media Player /
                           ffmpeg / OpenCV.
  <YYYYmmdd_HHMMSS>.jsonl  what the gate saw and decided, one JSON object per line:
                           "start" (the settings in force), "video_start" (the wall-clock
                           time of the first video frame: video time 0), then "tick"
                           (every trigger-loop tick: detections, track IDs, the subject,
                           the dwell, the outcome), "check", "alarm", "visit", and "stop".
                           Times are wall clock (time.time()).

The video is a SECOND session on the camera, separate from the gate's own decode, so the
gate never waits on the disk. The two are not frame-locked: a tick's time maps onto the
video as (t - video_start); expect ~0.1-0.2 s of skew between them — fine for reviewing
what the gate did, and a replay re-detects the video anyway.

Stops on its own after MAX_S, or when the disk gets low.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time

_CRED = re.compile(r"rtsp://[^@/\s]+@")
NAME_RE = re.compile(r"^\d{8}_\d{6}\.(mkv|jsonl)$")


def clean(msg) -> str:
    """Never let the camera password reach a log line: GStreamer errors quote the URL."""
    return _CRED.sub("rtsp://***@", str(msg))


class RecorderError(Exception):
    pass


class Recorder:
    MAX_S = 3600.0                       # one recording stops itself after an hour
    MIN_FREE_START = 5 * 1024 ** 3       # refuse to start with less free disk than this
    MIN_FREE_KEEP = 2 * 1024 ** 3        # ...and stop when it falls under this
    LOG_QUEUE_MAX = 4000                 # ~6 minutes of ticks if the disk stalls

    def __init__(self, rtsp: str, out_dir: str, protocol: str = "tcp"):
        self.rtsp, self.out_dir, self.protocol = rtsp, out_dir, protocol
        self._lock = threading.Lock()
        self._pipe = None
        self._name: str | None = None
        self._t0 = 0.0
        self._stopped = threading.Event()
        self._stop_reason = ""
        self.last_error: str | None = None
        self._q: "queue.Queue" = queue.Queue(maxsize=self.LOG_QUEUE_MAX)
        self._dropped = 0
        self._log_done = threading.Event()     # tells the log writer to finish and close

    # ── state ─────────────────────────────────────────────────────────────
    @property
    def active(self) -> bool:
        return self._pipe is not None

    def _path(self, ext: str) -> str:
        return os.path.join(self.out_dir, f"{self._name}.{ext}")

    def status(self) -> dict:
        with self._lock:
            active, name, t0 = self.active, self._name, self._t0
        out = {"active": active, "name": name, "error": self.last_error,
               "max_s": self.MAX_S, "free_gb": round(self._free() / 1024 ** 3, 1)}
        if active and name:
            out["elapsed"] = round(time.time() - t0, 1)
            try:
                out["bytes"] = os.path.getsize(self._path("mkv"))
            except OSError:
                out["bytes"] = 0
        return out

    def _free(self) -> int:
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            return shutil.disk_usage(self.out_dir).free
        except OSError:
            return 0

    # ── the log ───────────────────────────────────────────────────────────
    def log(self, kind: str, **fields) -> None:
        """Called from the gate's threads (the trigger loop, 10 Hz): never blocks."""
        if self._pipe is None:
            return
        fields["kind"] = kind
        fields.setdefault("t", round(time.time(), 3))
        try:
            self._q.put_nowait(fields)
        except queue.Full:
            self._dropped += 1

    def _log_writer(self, path: str, done: threading.Event) -> None:
        with open(path, "a", encoding="utf-8") as fh:
            last_flush = time.monotonic()
            while not (done.is_set() and self._q.empty()):
                try:
                    item = self._q.get(timeout=0.5)
                except queue.Empty:
                    item = None
                if item is not None:
                    fh.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
                if time.monotonic() - last_flush >= 1.0:
                    fh.flush()
                    last_flush = time.monotonic()

    # ── start / stop ──────────────────────────────────────────────────────
    def start(self, meta: dict | None = None) -> dict:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        with self._lock:
            if self._pipe is not None:
                raise RecorderError("already recording")
            if self._free() < self.MIN_FREE_START:
                raise RecorderError(f"less than {self.MIN_FREE_START // 1024 ** 3} GB free "
                                    f"in {self.out_dir} — free some space first")
            os.makedirs(self.out_dir, exist_ok=True)
            self._name = time.strftime("%Y%m%d_%H%M%S")
            self._t0 = time.time()
            self.last_error = None
            self._dropped = 0
            while not self._q.empty():
                self._q.get_nowait()
            pipe = Gst.parse_launch(
                f"rtspsrc name=src location={self.rtsp} protocols={self.protocol} latency=200 ! "
                "rtph264depay ! h264parse name=parse ! "
                f"matroskamux ! filesink location={self._path('mkv')}")
            # Video time 0 = the first frame through the parser; its wall-clock time is
            # what lines the log's ticks up with the picture.
            first = {"seen": False}

            def probe(pad, info):
                if not first["seen"]:
                    first["seen"] = True
                    self.log("video_start")
                return Gst.PadProbeReturn.OK
            pipe.get_by_name("parse").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe)
            self._pipe = pipe
            self._stopped.clear()
            self._stop_reason = ""
        self.log("start", file=f"{self._name}.mkv", **(meta or {}))
        self._log_done = done = threading.Event()
        threading.Thread(target=self._log_writer, args=(self._path("jsonl"), done),
                         name="RecorderLog", daemon=True).start()
        if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pipe.set_state(Gst.State.NULL)
            self._pipe = None
            done.set()
            raise RecorderError("the camera stream could not be opened")
        threading.Thread(target=self._watch, args=(pipe,), name="Recorder",
                         daemon=True).start()
        print(f"[record] recording to {self._path('mkv')}", flush=True)
        return self.status()

    def stop(self, reason: str = "stopped from the page") -> dict:
        from gi.repository import Gst
        with self._lock:
            pipe = self._pipe
            if pipe is None:
                return self.status()
            self._stop_reason = reason
        # EOS lets the muxer write its last fragment; _watch tears the pipeline down
        # when it comes out the other end.
        pipe.send_event(Gst.Event.new_eos())
        if not self._stopped.wait(8.0):
            pipe.set_state(Gst.State.NULL)
            self._finish(pipe, "stopped (the stream did not finish in time)")
        return self.status()

    def _watch(self, pipe) -> None:
        from gi.repository import Gst
        bus = pipe.get_bus()
        reason = None
        while reason is None:
            msg = bus.timed_pop_filtered(500 * Gst.MSECOND,
                                         Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if msg is not None:
                if msg.type == Gst.MessageType.ERROR:
                    err, dbg = msg.parse_error()
                    self.last_error = clean(err.message)
                    reason = f"error: {self.last_error}"
                else:
                    reason = self._stop_reason or "the stream ended"
                break
            if self._pipe is not pipe:              # stop() already gave up on it
                return
            if time.time() - self._t0 >= self.MAX_S and not self._stop_reason:
                self._stop_reason = f"reached the {self.MAX_S / 60:g}-minute limit"
                pipe.send_event(Gst.Event.new_eos())
            elif self._free() < self.MIN_FREE_KEEP and not self._stop_reason:
                self._stop_reason = "the disk is nearly full"
                pipe.send_event(Gst.Event.new_eos())
        pipe.set_state(Gst.State.NULL)
        self._finish(pipe, reason)

    def _finish(self, pipe, reason: str) -> None:
        with self._lock:
            if self._pipe is not pipe:
                return
            name, t0 = self._name, self._t0
            try:
                size = os.path.getsize(self._path("mkv"))
            except OSError:
                size = 0
            self.log("stop", seconds=round(time.time() - t0, 1), bytes=size, reason=reason,
                     log_lines_dropped=self._dropped)
            self._pipe = None
        self._log_done.set()
        self._stopped.set()
        print(f"[record] {name}.mkv stopped — {reason} ({size / 1024 ** 2:.0f} MB)", flush=True)

    # ── the list ──────────────────────────────────────────────────────────
    def recordings(self) -> list[dict]:
        """Newest first: name, video size, and the length from the log's stop line."""
        try:
            names = sorted((n for n in os.listdir(self.out_dir) if NAME_RE.match(n)
                            and n.endswith(".mkv")), reverse=True)
        except OSError:
            return []
        out = []
        for n in names:
            stem = n[:-4]
            row = {"name": stem, "video": n, "bytes": 0, "seconds": None,
                   "log": None, "recording": self.active and stem == self._name}
            try:
                row["bytes"] = os.path.getsize(os.path.join(self.out_dir, n))
            except OSError:
                pass
            log = os.path.join(self.out_dir, stem + ".jsonl")
            if os.path.isfile(log):
                row["log"] = stem + ".jsonl"
                row["seconds"] = _last_stop_seconds(log)
            out.append(row)
        return out


def _last_stop_seconds(path: str) -> float | None:
    """The "seconds" of the log's stop line, read from its tail (logs run to megabytes)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 4096))
            tail = fh.read().decode("utf-8", "ignore").strip().splitlines()
        for line in reversed(tail):
            if '"kind":"stop"' in line:
                return json.loads(line).get("seconds")
    except (OSError, ValueError):
        pass
    return None
