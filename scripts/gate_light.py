"""The tower light: what it should show, and the thread that keeps showing it.

The operator's definitions (2026-09-30):
  green steady    channel open, devices normal — waiting for a worker
  yellow steady   a check is in progress (dwell, badge read, PPE burst)
  green flashing  PASS — the channel is open for this worker to go through
  yellow flashing a warning: people crowding the gate, or a worker who passed and did not go
  red flashing    a fail: no badge read, badge not on the whitelist, PPE failed, or someone
                  walked through without passing
  red steady      channel closed: a device is down or the gate itself is not running

Priority, highest first: red steady (abnormal) > the newest flash event > yellow steady
(checking) > green steady (idle). The newest event wins among flashes: every event is a
new instruction to whoever is at the gate — a PASS after a FAIL must show green at once.

Why the gate re-sends the state every second instead of setting it once: the unit's
`restore` timer reverts to its BASE state (set with `led`) when nobody refreshes, and a
flash that ends reverts to that base too — not to whatever showed before (both measured
on the NHV6-3). So the base is red steady, and every refresh carries restore=RESTORE_S:
if this process dies, the Jetson hangs, or the tower's cable is pulled, the tower falls
back to red steady by itself within RESTORE_S. A channel must never be left looking open
by a gate that stopped watching it.
"""

from __future__ import annotations

import threading
import time

# led / alert digit order: red, amber, green, blue, white. 1 = on, 2 = flash, 0 = off.
PATTERNS = {
    "red":          "10000",
    "red_flash":    "20000",
    "yellow":       "01000",
    "yellow_flash": "02000",
    "green":        "00100",
    "green_flash":  "00200",
}
LABELS = {
    "red": "紅燈恆亮", "red_flash": "紅燈閃", "yellow": "黃燈恆亮",
    "yellow_flash": "黃燈閃", "green": "綠燈恆亮", "green_flash": "綠燈閃",
}
BASE = "red"          # what the tower falls back to when the refreshes stop
REFRESH_S = 1.0
RESTORE_S = 4         # > REFRESH_S + the worst control latency seen (~0.4 s), with margin


class LightPolicy:
    """Pure: events in, the pattern name out. No I/O, so it is tested directly."""

    def __init__(self):
        self._lock = threading.Lock()
        self._event = None        # (name, until, tag, beats_abnormal)

    def flash(self, name: str, hold_s: float, now: float, tag: str = "",
              beats_abnormal: bool = False) -> None:
        if name not in PATTERNS:
            raise ValueError(f"unknown light pattern {name!r}")
        with self._lock:
            self._event = (name, now + hold_s, tag, beats_abnormal)

    def end(self, tag: str) -> bool:
        """End the current event early, but only if it is still the one tagged `tag` — a
        newer event (say a tailgater's red flash) must not be cleared by the older one's
        owner (the worker who passed walking through)."""
        with self._lock:
            if self._event and self._event[2] == tag:
                self._event = None
                return True
            return False

    def event(self, now: float):
        with self._lock:
            if self._event and now >= self._event[1]:
                self._event = None
            return self._event

    def desired(self, now: float, abnormal: str | None, checking: bool) -> str:
        ev = self.event(now)
        if ev and ev[3]:              # an operator's test flash outranks everything
            return ev[0]
        if abnormal:
            return "red"
        if ev:
            return ev[0]
        return "yellow" if checking else "green"


class LightDriver:
    """Keeps the tower showing LightPolicy.desired(). One thread; never raises."""

    def __init__(self, tower, policy: LightPolicy, inputs, log=print):
        self.tower, self.policy, self.inputs, self.log = tower, policy, inputs, log
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._state = {"light": None, "label": "", "reason": None, "tower_ok": None,
                       "error": "", "since": None}
        self._stop = False

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="tower-light").start()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    def wake(self) -> None:
        """Apply a new event now rather than at the next 1 s refresh."""
        self._wake.set()

    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _run(self) -> None:
        need_base = True              # at start, and again whenever the tower comes back
        while not self._stop:
            now = time.monotonic()
            try:
                abnormal, checking = self.inputs()
            except Exception as e:    # an input bug must not freeze the light: fail closed
                abnormal, checking = f"light input error: {e}", False
            name = self.policy.desired(now, abnormal, checking)
            ok, err = True, ""
            try:
                if need_base:
                    self.tower.set_base(PATTERNS[BASE])
                self.tower.show(PATTERNS[name], RESTORE_S)
                need_base = False
            except Exception as e:
                ok, err = False, str(e)
                need_base = True      # it may have rebooted: its base is gone
            with self._lock:
                prev = self._state
                if ok != prev["tower_ok"]:
                    self.log(f"[light] tower {'reachable' if ok else 'UNREACHABLE: ' + err}")
                if ok and (name != prev["light"] or abnormal != prev["reason"]):
                    self.log(f"[light] {LABELS[name]}"
                             + (f" — {abnormal}" if name == "red" and abnormal else ""))
                self._state = {"light": name, "label": LABELS[name], "reason": abnormal,
                               "tower_ok": ok, "error": err,
                               "since": prev["since"] if name == prev["light"] else time.time()}
            self._wake.wait(REFRESH_S)
            self._wake.clear()
