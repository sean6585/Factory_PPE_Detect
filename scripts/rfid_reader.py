"""
RFID identity for the gate — MPK-R-9504 UHF reader.

Answers one question: **who was at the gate at time T?**

The reader does continuous inventory, not badge taps. Measured against its own simulator
it produces ~273 reads/second and sees every tag within several metres at once, so at any
instant a handful of EPCs all have fresh readings. Picking "the tag whose timestamp is
closest to T" would therefore choose almost at random among everyone in range.

What actually separates the worker crossing the gate from a colleague standing behind
them is **signal strength**: RSSI falls off with distance, so the strongest tag in a short
window around T is the one nearest the reader. That mirrors how the camera side already
picks its subject — largest person box, because largest means closest.

This module keeps a short rolling log of readings and resolves that question. It does not
trigger anything; the caller decides when a gate event happened.

Layering: `mpk_rfid.py` (vendor protocol, committed under RFID/rfid_console_tool/) is used
unmodified. Everything here is gate policy on top of it.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import defaultdict, deque
from typing import Callable

# mpk_rfid.py is kept where it shipped, with its README, tests and simulator beside it,
# rather than copied into scripts/. This codebase already carries one duplicated-file
# problem (ppe_check.py exists twice) and does not need a second.
#
# RFID2/rfid_console_tool_2 (v2.1.0) replaces RFID/rfid_console_tool (v2.0.0): a strict
# superset — adds the GPIO input text-protocol (see GpioSensor below) and fixes the
# inventory-finished handler to accept the 0x6D "finished" notification arriving out of
# order (real hardware can deliver it while another command is in flight; the old
# handler only recognised it when nothing else was pending and silently dropped it
# otherwise). The old vendor copy is left on disk, untouched, in case anything else
# still imports it directly.
_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "RFID2", "rfid_console_tool_2")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)


class TagObservations:
    """Rolling per-EPC log of (monotonic time, rssi), trimmed to a horizon.

    Times are `time.monotonic()`, deliberately not the reader's own DF_TIMESTAMP and not
    wall clock. The reader's clock is a different box needing sync nobody wants to
    maintain, and wall clock can step under NTP mid-shift, which would silently corrupt
    every window comparison. LAN delivery latency is a few milliseconds and roughly
    constant — far below the resolution this decision needs.
    """

    def __init__(self, horizon_s: float = 30.0):
        self.horizon_s = horizon_s
        self._obs: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()
        self.total_reads = 0

    def add(self, epc: str, rssi: float, t: float | None = None) -> None:
        """Called from the reader's RX thread — keep this cheap."""
        t = time.monotonic() if t is None else t
        with self._lock:
            self.total_reads += 1
            self._obs[epc].append((t, rssi))
            cutoff = t - self.horizon_s
            for e in list(self._obs):
                q = self._obs[e]
                while q and q[0][0] < cutoff:
                    q.popleft()
                if not q:
                    del self._obs[e]

    def window(self, t0: float, before: float, after: float) -> dict[str, list]:
        lo, hi = t0 - before, t0 + after
        with self._lock:
            return {e: [(t, r) for (t, r) in q if lo <= t <= hi]
                    for e, q in self._obs.items()
                    if any(lo <= t <= hi for (t, _) in q)}

    def summary(self, t0: float | None = None, before: float | None = None) -> list[dict]:
        """Every EPC heard in [t0-before, t0] (default: the whole horizon), one row each:
        peak and latest RSSI, read count, first/last monotonic time. Strongest first.

        This is the shared basis for two very different callers — the operator's
        "See RFID read" table (whole horizon, wall-clock display) and the image trigger's
        one-person rule (short window, count of rows above the RSSI floor) — so that what
        the operator sees in the popup is exactly what the gate decided on.
        """
        t0 = time.monotonic() if t0 is None else t0
        lo = t0 - (self.horizon_s if before is None else before)
        rows = []
        with self._lock:
            for epc, q in self._obs.items():
                obs = [(t, r) for (t, r) in q if lo <= t <= t0]
                if not obs:
                    continue
                rows.append({
                    "epc": epc,
                    "rssi": round(max(r for (_, r) in obs), 1),
                    "last_rssi": round(obs[-1][1], 1),
                    "reads": len(obs),
                    "first_t": obs[0][0],
                    "last_t": obs[-1][0],
                })
        rows.sort(key=lambda d: -d["rssi"])
        return rows

    def closest(self, t0: float, before: float = 3.0, after: float = 1.0) -> dict | None:
        """The EPC nearest the reader during [t0-before, t0+after], or None.

        "Nearest" is the strongest single reading in the window. Peak rather than mean,
        because a worker walking past the antenna produces one clear maximum while their
        average is dragged down by the approach; a stationary bystander further away
        never reaches that peak at all.
        """
        w = self.window(t0, before, after)
        if not w:
            return None
        ranked = []
        for epc, obs in w.items():
            peak = max(r for (_, r) in obs)
            ranked.append({
                "epc": epc,
                "rssi": round(peak, 1),
                "reads": len(obs),
                "first_seen": round(min(t for (t, _) in obs) - t0, 2),  # relative to T
                "last_seen": round(max(t for (t, _) in obs) - t0, 2),
            })
        ranked.sort(key=lambda d: -d["rssi"])
        best = dict(ranked[0])
        best["candidates"] = ranked            # kept so an ambiguous read is auditable
        return best


class GpioSensor:
    """Thread-safe snapshot of the reader's 3 GPIO inputs — this is the through-beam
    sensor's path in (wired into the MPK-R-9504 box's DI terminals, not the Jetson's
    own GPIO header). Fed from the RX thread via RfidService._on_gpio.

    The box pushes `Input PinN,V` on its own the instant a pin changes level, so this
    is normally kept current with zero polling from our side — RfidService's periodic
    request_gpio() call (see its POLL_S) exists only as a safety net: a push-only
    design means a pin that hasn't changed since we connected is unknown until asked
    once, and a dropped packet would otherwise go unnoticed indefinitely.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict[int, int] = {}
        self._at = 0.0
        self.transitions = 0

    def set(self, pin: int, level: int) -> None:
        with self._lock:
            if self._state.get(pin) != level:
                self.transitions += 1
            self._state[pin] = level
            self._at = time.time()

    def snapshot(self) -> dict[int, int]:
        with self._lock:
            return dict(self._state)

    @property
    def updated_at(self) -> float:
        with self._lock:
            return self._at


class RfidService:
    """Owns the reader connection and keeps TagObservations fed.

    The vendor library has no reconnect of its own, so this supervises the connection the
    way camera_loop() supervises the RTSP stream: retry, log, carry on. A gate whose
    reader dropped at 3am must still be reading badges at 6am without someone noticing.
    """

    # Safety-net re-ask for GPIO state — see GpioSensor's docstring. Generous on
    # purpose: the box pushes changes in real time on its own, this only covers a
    # dropped packet or a pin that never changed since connect.
    GPIO_POLL_S = 5.0

    def __init__(self, host: str, port: int, antenna: int = 1,
                 power_dbm: float = 20.0, rf_mode: int = 103, horizon_s: float = 30.0):
        self.host, self.port = host, port
        self.antenna, self.power_dbm, self.rf_mode = antenna, power_dbm, rf_mode
        self.obs = TagObservations(horizon_s)
        self.gpio = GpioSensor()
        self.connected = False
        self.last_error: str | None = None
        # Live power / receive-mode changes (reconfigure()): only the supervisor thread
        # may send commands, so a request is handed to it and its verdict waited on.
        self._reconfig_lock = threading.Lock()
        self._reconfig = threading.Event()
        self._reconfig_done = threading.Event()
        self._reconfig_want = (power_dbm, rf_mode)
        self._reconfig_result: dict | None = None
        self._finish_status: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Optional external hook for a genuine GPIO edge — (pin, level, prev), RX
        # thread, only called when prev is a known 0/1 (see _on_gpio). This is gate
        # policy (which pin, what to do about it), so it's set from outside by whoever
        # owns that policy (gate_server.py's sensor trigger), not decided in here.
        self.on_gpio_change: Callable[[int, int, int], None] | None = None
        # Tracks whether the box's own inventory session is actually still running,
        # independent of the TCP connection: real hardware can end continuous mode
        # (time=0, runs=0) on its own — STATUS_INVENTORY_TIMEOUT / STATUS_STOP_CONDITION
        # — while the socket stays open, which the old is_open-only supervision never
        # noticed. Set True right after start_inventory() succeeds, False by
        # _on_inventory_finished; the poll loop below reissues 0x6D when it goes False.
        self._inventory_running = False
        self._last_end_status: int | None = None

    def _on_tag(self, tag) -> None:
        """Runs on the vendor library's RX thread. Only appends — anything slower here
        would stall packet parsing and drop reads.

        The field is `rssi_dbm`, NOT `rssi`. on_tag delivers a TagReport (one read event)
        while `rssi` belongs to TagRecord (the aggregated store entry) — the vendor
        README's field list documents the latter, so the obvious guess is wrong and fails
        silently on every single read.
        """
        try:
            self.obs.add(tag.epc_hex, tag.rssi_dbm)
        except Exception as e:
            # Never swallow this quietly: a mistake here means the reader looks connected
            # and healthy while recording nothing at all.
            if self.last_error != str(e):
                self.last_error = str(e)
                print(f"[rfid] dropping reads — {type(e).__name__}: {e}", flush=True)

    def _on_gpio(self, pin: int, level: int, prev: int | None) -> None:
        """Runs on the RX thread, fired only on an actual level CHANGE (the vendor
        library already de-duplicates — see mpk_rfid.py's _handle_text). This is the
        through-beam sensor's event path once it's wired into one of the box's 3 DI
        terminals: a beam break shows up here as a level flip within a couple of ms of
        it happening, no polling needed to catch it.

        Only records + logs directly — any real work (a gate trigger) is handed off via
        on_gpio_change, same reason detect()/on_tag never do real work inline: this is
        the RX thread parsing every RFID/GPIO packet, and a burst check takes the better
        part of a second, which would stall it.
        """
        self.gpio.set(pin, level)
        was = "unknown" if prev is None else ("HIGH" if prev else "LOW")
        now = "HIGH" if level else "LOW"
        print(f"[rfid] GPIO IN{pin}: {was} -> {now}", flush=True)

        # prev is None the first time THIS connection hears about a pin — either the
        # seed query right after connect, or (since a fresh RfidReader is created on
        # every reconnect, with its own empty _gpio dict) the first push after a
        # reconnect. Either way that's "discovering whatever state it's already in",
        # not a new transition, so it must never be treated as a trigger-worthy edge —
        # a pin that happens to sit HIGH across a routine reconnect would otherwise
        # fire a phantom gate check for nobody.
        if prev is not None and self.on_gpio_change:
            try:
                self.on_gpio_change(pin, level, prev)
            except Exception as e:
                print(f"[rfid] on_gpio_change handler failed: {e}", flush=True)

    def reconfigure(self, power_dbm: float, rf_mode: int, timeout: float = 10.0) -> dict:
        """Change transmit power (dBm) and receive mode live, without a reconnect.

        Returns {"ok", "power_dbm", "rf_mode", "error"?}. The reader validates both: an
        out-of-range value is refused (ERR_PARAMETER_RF_POWER / _RF_MODE) and the previous
        settings are put back, so reading never stops. Not connected: the values are
        kept and used at the next connect."""
        want = (float(power_dbm), int(rf_mode))
        with self._reconfig_lock:
            if not self.connected:
                self.power_dbm, self.rf_mode = want
                return {"ok": True, "power_dbm": want[0], "rf_mode": want[1],
                        "note": "reader not connected — used at the next connect"}
            self._reconfig_want = want
            self._reconfig_result = None
            self._reconfig_done.clear()
            self._reconfig.set()
            if not self._reconfig_done.wait(timeout):
                return {"ok": False, "error": "the reader did not answer in time",
                        "power_dbm": self.power_dbm, "rf_mode": self.rf_mode}
            return self._reconfig_result

    def _apply_reconfig(self, reader) -> dict:
        """On the supervisor thread: stop inventory, set the mode, start at the new power,
        and watch for a refusal — it arrives as an "inventory ended" with an error status,
        because the run command itself is not acknowledged."""
        from mpk_rfid import status_text
        prev = (self.power_dbm, self.rf_mode)
        want = self._reconfig_want

        def run(power, mode):
            try:
                reader.abort()                   # the reader sends "ended" before the ack
            except Exception:
                pass
            self._inventory_running = False
            time.sleep(0.2)
            reader.set_inventory_parameter(rf_mode=mode)
            self._finish_status = None
            reader.start_inventory(antenna=self.antenna, rf_mode=mode, power_dbm=power)
            self._inventory_running = True
            time.sleep(1.0)
            st = self._finish_status
            if st is not None and st != 0:
                self._inventory_running = False
                raise RuntimeError(f"reader refused it: {status_text(st)} (0x{st:02X})")

        try:
            run(*want)
            self.power_dbm, self.rf_mode = want
            print(f"[rfid] now {want[0]:g} dBm, RF mode {want[1]}", flush=True)
            return {"ok": True, "power_dbm": want[0], "rf_mode": want[1]}
        except Exception as e:
            err = str(e)
            try:
                run(*prev)
            except Exception as e2:
                err += f"; restoring {prev[0]:g} dBm / mode {prev[1]} failed too: {e2}"
            print(f"[rfid] {want[0]:g} dBm / mode {want[1]} not applied: {err}", flush=True)
            return {"ok": False, "error": err, "power_dbm": self.power_dbm, "rf_mode": self.rf_mode}

    def _on_inventory_finished(self, status: int, total: int, elapsed_ms: int) -> None:
        """Runs on the RX thread. Records only — MUST NOT call start_inventory() here.

        The main thread can be inside send_command() holding _cmd_lock, waiting on a
        response that only this RX thread delivers; if this callback tried to reacquire
        that same lock to restart, the two would deadlock. The actual restart happens
        from the supervisor loop below, polling _inventory_running instead.
        """
        self._inventory_running = False
        self._finish_status = status
        if status != self._last_end_status:
            print(f"[rfid] inventory ended (status=0x{status:02X}) — restarting", flush=True)
        self._last_end_status = status

    def _loop(self) -> None:
        from mpk_rfid import DF_DEFAULT, RfidReader

        while not self._stop.is_set():
            reader = None
            try:
                reader = RfidReader(self.host, self.port)
                reader.on_tag = self._on_tag
                reader.on_gpio = self._on_gpio
                reader.on_inventory_finished = self._on_inventory_finished
                reader.open()
                self.connected, self.last_error = True, None
                print(f"[rfid] connected to {self.host}:{self.port}", flush=True)

                # Push notifications only fire on CHANGE, so a pin that has sat at the
                # same level since before we connected would otherwise stay "unknown"
                # forever — ask once, up front, the way rfid_console.py does.
                try:
                    for pin, level in reader.query_gpio(timeout=1.0).items():
                        self.gpio.set(pin, level)
                except Exception as e:
                    print(f"[rfid] initial GPIO query failed: {e}", flush=True)

                # Both calls are required before inventory, per the vendor README's
                # continuous-mode example. Without set_data_format the tag notifications
                # carry no RSSI — and RSSI is the entire basis for deciding which tag is
                # nearest the gate, so the reader appears to work while reporting nothing
                # usable. Commands must not be sent DURING inventory (STATUS_BUSY), so
                # they belong here, before start_inventory.
                reader.set_data_format(DF_DEFAULT)
                reader.set_inventory_parameter(rf_mode=self.rf_mode)
                reader.start_inventory(antenna=self.antenna, rf_mode=self.rf_mode,
                                       power_dbm=self.power_dbm)
                self._inventory_running = True

                gpio_backoff = 0.0
                next_gpio_poll = time.monotonic()
                inv_restart_at = 0.0
                # is_open is a PROPERTY on RfidReader, not a method — calling it raises
                # "'bool' object is not callable" and the supervisor then reconnects in a
                # tight loop while reading nothing.
                while not self._stop.is_set() and reader.is_open:
                    if self._reconfig.is_set():
                        self._reconfig.clear()
                        self._reconfig_result = self._apply_reconfig(reader)
                        self._reconfig_done.set()
                    now = time.monotonic()

                    # Safety-net GPIO re-ask (see GpioSensor / GPIO_POLL_S). Non-blocking
                    # request — never contend with an in-flight command for the lock.
                    if now >= next_gpio_poll:
                        next_gpio_poll = now + self.GPIO_POLL_S
                        try:
                            reader.request_gpio()
                        except Exception:
                            pass          # transient; the next poll or a push will catch up

                    # The box can silently end "continuous" inventory on its own; if it
                    # has, and we haven't just retried, reissue 0x6D. Same reasoning as
                    # rfid_console.py's _maybe_restart(): a short backoff on repeated
                    # failure so a persistently broken command channel doesn't get
                    # hammered, reset the moment a restart actually succeeds.
                    if not self._inventory_running and now >= inv_restart_at:
                        try:
                            reader.start_inventory(antenna=self.antenna, rf_mode=self.rf_mode,
                                                    power_dbm=self.power_dbm)
                            self._inventory_running = True
                            gpio_backoff = 0.0
                        except Exception as e:
                            print(f"[rfid] inventory restart failed: {e}", flush=True)
                            gpio_backoff = min(2.0, gpio_backoff * 2 or 0.1)
                            inv_restart_at = now + gpio_backoff

                    time.sleep(0.25)
            except Exception as e:
                self.last_error = str(e)
                print(f"[rfid] {e}; retrying in 3s", flush=True)
            finally:
                self.connected = False
                self._inventory_running = False
                if reader is not None:
                    try:
                        reader.abort()
                    except Exception:
                        pass
                    try:
                        reader.close()
                    except Exception:
                        pass
            if not self._stop.is_set():
                self._stop.wait(3.0)

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="RfidService", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {"host": self.host, "port": self.port, "connected": self.connected,
                "total_reads": self.obs.total_reads, "error": self.last_error,
                "gpio": self.gpio.snapshot(), "gpio_at": self.gpio.updated_at}
