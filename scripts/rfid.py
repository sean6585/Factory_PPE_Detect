"""
RFID tap ingest for the PPE gate — Phase 2.

Card readers do not agree on how they deliver an ID, and the reader for this gate
is not chosen yet, so this module accepts every transport a badge reader is likely
to speak and funnels them all into ONE callback:

    HTTP    POST/GET /api/tap        (handled in gate_server.py; reader webhooks, curl)
    TCP     --rfid-tcp 9000          (Wiegand->Ethernet converters: line per card)
    UDP     --rfid-udp 9000          (same, connectionless)
    Serial  --rfid-serial /dev/ttyUSB0   (USB/RS232 readers)
    HID     keyboard-wedge readers type into the kiosk page and POST /api/tap

Everything above the callback — debounce, card->worker lookup, the capture burst —
is transport-independent, so swapping the reader later touches only a flag.

Wire format: readers commonly wrap the ID in STX/ETX and end with CR, CRLF or LF,
and pad with leading zeros. normalize() strips all of that so the same card reads
the same whichever transport it arrived on.
"""

import json
import os
import socket
import socketserver
import threading
import time

# Framing characters emitted by common readers, stripped before matching.
_TRIM = "\x02\x03\r\n\t\x00 "


def normalize(raw) -> str:
    """One card ID, canonical form: no framing, no padding, upper case."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    return str(raw).strip(_TRIM).strip().upper()


def loose(card: str) -> str:
    """Match key that ignores leading zeros — readers disagree on ID width."""
    return card.lstrip("0") or "0"


class CardRegistry:
    """card ID -> worker, reloaded from disk when the file changes.

    Cards get issued while the gate is running, so the file is re-read on an mtime
    change rather than at startup only: adding a worker must not need a restart
    that drops the camera connection.
    """

    def __init__(self, path: str | None):
        self.path = path
        self._mtime = None
        self._by_id = {}
        self._by_loose = {}
        self.reload()

    def reload(self) -> bool:
        if not self.path or not os.path.isfile(self.path):
            return False
        try:
            mtime = os.path.getmtime(self.path)
            if mtime == self._mtime:
                return False
            with open(self.path) as fh:
                raw = json.load(fh)
        except Exception as e:                       # a bad edit must not kill the gate
            print(f"[rfid] cards file unreadable ({e}); keeping previous list", flush=True)
            return False

        cards = raw.get("cards", raw) if isinstance(raw, dict) else {}
        by_id, by_loose = {}, {}
        for cid, val in cards.items():
            rec = {"worker_id": str(val)} if isinstance(val, str) else dict(val or {})
            key = normalize(cid)
            rec.setdefault("worker_id", key)
            rec["card"] = key
            by_id[key] = rec
            by_loose.setdefault(loose(key), rec)
        self._by_id, self._by_loose, self._mtime = by_id, by_loose, mtime
        print(f"[rfid] {len(by_id)} card(s) loaded from {self.path}", flush=True)
        return True

    def lookup(self, card: str):
        self.reload()
        return self._by_id.get(card) or self._by_loose.get(loose(card))

    def __len__(self):
        return len(self._by_id)


class TapDispatcher:
    """Turns raw reads into gate taps: de-bounces, then calls the handler.

    Two guards, and they reject for different reasons:
      * cooldown — a reader re-sends the same card many times a second while the
        badge is held against it; without this one badge is one tap per read.
      * busy — a tap arriving mid-check would interleave two bursts on one camera.
    """

    def __init__(self, handler, cooldown: float = 3.0, busy=None):
        self.handler = handler
        self.cooldown = max(0.0, cooldown)
        self.busy = busy or (lambda: False)
        self._last = {}                    # card -> monotonic time of last accepted tap
        self._lock = threading.Lock()
        self.stats = {"accepted": 0, "repeat": 0, "busy": 0}

    def tap(self, raw, source: str = "?") -> dict:
        card = normalize(raw)
        if not card:
            return {"ok": False, "reason": "empty"}

        now = time.monotonic()
        with self._lock:
            prev = self._last.get(card)
            if prev is not None and now - prev < self.cooldown:
                self.stats["repeat"] += 1
                return {"ok": False, "reason": "cooldown", "card": card}
            if self.busy():
                self.stats["busy"] += 1
                return {"ok": False, "reason": "busy", "card": card}
            self._last[card] = now
            # Bound the table; a gate sees many cards over a long uptime.
            if len(self._last) > 512:
                for k in sorted(self._last, key=self._last.get)[:256]:
                    del self._last[k]
            self.stats["accepted"] += 1

        print(f"[rfid] tap card={card} via {source}", flush=True)
        return self.handler(card, source)


# ── transports ────────────────────────────────────────────────────────────
class _LineTCPHandler(socketserver.StreamRequestHandler):
    timeout = 300

    def handle(self):
        disp = self.server.dispatcher
        while True:
            try:
                line = self.rfile.readline()
            except (socket.timeout, OSError):
                return
            if not line:
                return
            if normalize(line):
                disp.tap(line, f"tcp:{self.client_address[0]}")


class _UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data = self.request[0]
        if normalize(data):
            self.server.dispatcher.tap(data, f"udp:{self.client_address[0]}")


class _TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _UDPServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve_tcp(host: str, port: int, dispatcher: TapDispatcher):
    srv = _TCPServer((host, port), _LineTCPHandler)
    srv.dispatcher = dispatcher
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[rfid] TCP reader listener on {host}:{port}", flush=True)
    return srv


def serve_udp(host: str, port: int, dispatcher: TapDispatcher):
    srv = _UDPServer((host, port), _UDPHandler)
    srv.dispatcher = dispatcher
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[rfid] UDP reader listener on {host}:{port}", flush=True)
    return srv


def serve_serial(device: str, baud: int, dispatcher: TapDispatcher, stop=lambda: False):
    """Read newline-delimited IDs off a serial reader; reconnect if unplugged."""
    try:
        import serial                                   # pyserial, optional
    except ImportError:
        print("[rfid] --rfid-serial needs pyserial (pip install pyserial)", flush=True)
        return None

    def loop():
        while not stop():
            try:
                with serial.Serial(device, baud, timeout=1) as sp:
                    print(f"[rfid] serial reader on {device} @ {baud}", flush=True)
                    buf = bytearray()
                    while not stop():
                        chunk = sp.read(64)
                        if not chunk:
                            continue
                        buf.extend(chunk)
                        # Readers end an ID with CR, LF or ETX; split on any of them.
                        while True:
                            idx = min((buf.find(bytes([c])) for c in (13, 10, 3)
                                       if buf.find(bytes([c])) >= 0), default=-1)
                            if idx < 0:
                                break
                            line, buf = buf[:idx], buf[idx + 1:]
                            if normalize(line):
                                dispatcher.tap(line, f"serial:{device}")
                        if len(buf) > 128:              # junk with no terminator
                            del buf[:-32]
            except Exception as e:
                print(f"[rfid] serial {device}: {e}; retry in 3s", flush=True)
                time.sleep(3)

    threading.Thread(target=loop, daemon=True).start()
    return True
