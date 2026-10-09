#!/usr/bin/env python3
"""
dual_antenna_monitor —— 同時看兩支天線（預設 ANT 1 與 ANT 3）讀到的 tag，終端機即時刷新。

The 0x6D Run Inventory command carries ONE antenna byte, so "both antennas" here means
alternating continuous runs: start on ANT 1, abort after --dwell seconds, start on ANT 3,
and so on. Only documented commands are sent (0x10 abort, 0x6A, 0x64, 0x6D) — no guessed
multi-antenna values. Each read is filed under the antenna the READER reports in the tag
notification (DF_ANTENNA), not the one we asked for, so a mismatch would show up.

Usage:
    python3 dual_antenna_monitor.py                       # real box, ANT 1 + 3
    python3 dual_antenna_monitor.py --antennas 1,3 --dwell 0.5 --power 20
    python3 dual_antenna_monitor.py --plain               # scrolling log, no screen redraw
    python3 dual_antenna_monitor.py --sim                 # no hardware

The box allows ONE TCP client: stop the gate first (`sudo systemctl stop ppe-gate`).
After a client disconnects the box refuses new connections for ~1 min, so connecting is
retried for --connect-wait seconds.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from mpk_rfid import (DF_DEFAULT, RF_ULTRA_FAST, RfidReader, TagReport,
                      status_text)

DEFAULT_HOST = "100.206.151.186"
DEFAULT_PORT = 8080

# ANSI
CLEAR = "\x1b[H\x1b[2J"
DIM, BOLD, GREEN, YELLOW, RED, RESET = ("\x1b[2m", "\x1b[1m", "\x1b[32m",
                                        "\x1b[33m", "\x1b[31m", "\x1b[0m")


@dataclass
class Cell:
    """One (EPC, antenna) pair."""
    count: int = 0
    rssi: float = 0.0
    max_rssi: float = float("-inf")
    first_seen: float = 0.0
    last_seen: float = 0.0


@dataclass
class AntStat:
    runs: int = 0
    reads: int = 0
    last_end: Optional[int] = None        # status of the last run that ended on its own
    error: Optional[str] = None


@dataclass
class Board:
    """Thread-safe tally. Writer = reader RX thread, reader = main thread."""
    window: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    cells: Dict[Tuple[str, int], Cell] = field(default_factory=dict)
    events: deque = field(default_factory=lambda: deque(maxlen=12))
    mismatched: int = 0                   # reads whose ANT differs from the one requested
    requested: int = 0

    def add(self, tag: TagReport) -> Optional[str]:
        """Returns an event line when a tag appears (or reappears) on an antenna."""
        now = time.time()
        ant = tag.antenna_id or self.requested
        key = (tag.epc_hex, ant)
        with self.lock:
            if tag.antenna_id and tag.antenna_id != self.requested:
                self.mismatched += 1
            c = self.cells.get(key)
            event = None
            if c is None:
                c = self.cells[key] = Cell(first_seen=now)
                event = "NEW "
            elif now - c.last_seen > self.window:
                event = "BACK"
            c.count += 1
            c.rssi = tag.rssi_dbm
            c.max_rssi = max(c.max_rssi, tag.rssi_dbm)
            c.last_seen = now
            if event:
                line = (f"{time.strftime('%H:%M:%S')}  ANT{ant}  {event}  "
                        f"{tag.epc_hex}  {tag.rssi_dbm:6.1f} dBm")
                self.events.append(line)
                return line
        return None

    def snapshot(self):
        with self.lock:
            return ({k: Cell(**vars(v)) for k, v in self.cells.items()},
                    list(self.events), self.mismatched)


class Monitor:
    def __init__(self, args):
        self.args = args
        self.antennas: List[int] = args.antennas
        self.board = Board(window=args.window)
        self.stats: Dict[int, AntStat] = {a: AntStat() for a in self.antennas}
        self.current: int = self.antennas[0]
        self._ended_status: Optional[int] = None
        self._ended = threading.Event()
        self.started = time.time()

    # ---- RX-thread callbacks: record only (sending a command here would deadlock
    #      against a main thread waiting inside send_command — see rfid_reader.py)
    def on_tag(self, tag: TagReport) -> None:
        self.stats[self.current].reads += 1
        line = self.board.add(tag)
        if line and self.args.plain:
            print(line, flush=True)

    def on_finished(self, status: int, total: int, elapsed: int) -> None:
        self._ended_status = status
        self._ended.set()

    def log(self, msg: str) -> None:
        if self.args.verbose:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    # ---- connection
    def connect(self) -> RfidReader:
        deadline = time.time() + self.args.connect_wait
        attempt = 0
        while True:
            attempt += 1
            reader = RfidReader(self.args.host, self.args.port)
            try:
                reader.open()
                return reader
            except Exception as e:
                if time.time() >= deadline:
                    raise ConnectionError(
                        f"cannot connect to {self.args.host}:{self.args.port}: {e}\n"
                        "Is the gate (ppe-gate.service) still holding the box's one client "
                        "slot? `sudo systemctl stop ppe-gate`, then wait ~1 min.") from e
                print(f"connect attempt {attempt} failed ({e}); retrying…", flush=True)
                time.sleep(3.0)

    def prepare(self, reader: RfidReader) -> None:
        reader.on_log = self.log
        print(f"connected to {reader.description}", flush=True)
        # A previous client's continuous inventory outlives its connection; any command
        # then answers STATUS_BUSY. Abort first, unconditionally.
        try:
            reader.abort(timeout=2.0)
        except Exception:
            pass
        time.sleep(0.3)
        try:
            print(f"firmware: {reader.get_firmware_version()}", flush=True)
        except Exception as e:
            print(f"firmware query failed: {e}", flush=True)
        # Without DF_ANTENNA/DF_RSSI in the data format, reads carry neither.
        reader.set_data_format(DF_DEFAULT)
        reader.set_inventory_parameter(rf_mode=self.args.rf_mode)
        reader.on_tag = self.on_tag
        reader.on_inventory_finished = self.on_finished

    # ---- main loop
    def run(self, reader: RfidReader) -> None:
        a = self.args
        single = len(self.antennas) == 1
        next_draw = 0.0
        while True:
            for ant in self.antennas:
                if not reader.is_open:
                    raise ConnectionError("connection to the reader dropped")
                st = self.stats[ant]
                self.current = ant
                self.board.requested = ant
                self._ended.clear()
                self._ended_status = None
                try:
                    reader.start_inventory(antenna=ant, rf_mode=a.rf_mode,
                                           power_dbm=a.power)
                    st.runs += 1
                except Exception as e:
                    st.error = str(e)
                    time.sleep(0.5)
                    continue

                t_end = time.time() + (float("inf") if single else a.dwell)
                while time.time() < t_end:
                    if self._ended.wait(0.05):
                        # The run ended on its own: a refusal (e.g. no antenna on that
                        # port) or the box's habit of stopping "continuous" runs.
                        status = self._ended_status
                        st.last_end = status
                        if status is not None and status >= 0x10:
                            st.error = status_text(status)
                            if a.plain:
                                print(f"{time.strftime('%H:%M:%S')}  ANT{ant}  "
                                      f"REFUSED  {st.error}", flush=True)
                        break
                    if not a.plain and time.time() >= next_draw:
                        next_draw = time.time() + a.refresh
                        self.draw()
                else:
                    try:
                        reader.abort(timeout=2.0)
                    except Exception as e:
                        self.log(f"abort on ANT{ant} failed: {e}")
                    # abort's ack can arrive before the run's "ended" response; give the
                    # last notifications a moment so they are not filed under the next ANT.
                    self._ended.wait(0.2)
                    st.error = None          # it ran the full dwell: the port works

                if not a.plain and time.time() >= next_draw:
                    next_draw = time.time() + a.refresh
                    self.draw()

    # ---- screen
    def draw(self) -> None:
        cells, events, mismatched = self.board.snapshot()
        now = time.time()
        a = self.args
        out = [CLEAR]
        out.append(f"{BOLD}MPK-R-9504  {a.host}:{a.port}   ANT {', '.join(map(str, self.antennas))}"
                   f"   {a.power:g} dBm  RF mode {a.rf_mode}   dwell {a.dwell:g}s"
                   f"   up {now - self.started:5.0f}s{RESET}")
        stat_parts = []
        for ant in self.antennas:
            st = self.stats[ant]
            mark = f"{GREEN}●{RESET}" if ant == self.current else " "
            err = f" {RED}{st.error}{RESET}" if st.error else ""
            stat_parts.append(f"{mark} ANT{ant}: runs {st.runs}, reads {st.reads}{err}")
        out.append("   ".join(stat_parts))
        if mismatched:
            out.append(f"{YELLOW}{mismatched} reads reported a different ANT than requested{RESET}")
        out.append("")

        # one row per EPC, one column group per antenna
        epcs = sorted({e for e, _ in cells},
                      key=lambda e: -max(cells[(e, x)].last_seen for x in self.antennas
                                         if (e, x) in cells))
        hdr = f"{'EPC':<26}"
        for ant in self.antennas:
            hdr += f" │ {'ANT' + str(ant):^28}"
        out.append(BOLD + hdr + RESET)
        sub = " " * 26
        for _ in self.antennas:
            sub += f" │ {'reads':>6} {'RSSI':>6} {'max':>6} {'ago':>7}"
        out.append(DIM + sub + RESET)

        rows = epcs[:a.max_rows]
        for epc in rows:
            line = f"{epc:<26}"
            for ant in self.antennas:
                c = cells.get((epc, ant))
                if c is None:
                    line += f" │ {DIM}{'—':>6} {'':>6} {'':>6} {'':>7}{RESET}"
                    continue
                age = now - c.last_seen
                live = age <= a.window
                col = GREEN if live else DIM
                line += (f" │ {col}{c.count:>6} {c.rssi:>6.1f} {c.max_rssi:>6.1f}"
                         f" {age:>6.1f}s{RESET}")
            out.append(line)
        if len(epcs) > len(rows):
            out.append(DIM + f"… {len(epcs) - len(rows)} more" + RESET)
        if not epcs:
            out.append(DIM + "(no tags yet)" + RESET)

        out.append("")
        out.append(BOLD + "recent (NEW = first read on that antenna, "
                   f"BACK = seen again after >{a.window:g}s)" + RESET)
        out.extend(events[-8:] or [DIM + "(none)" + RESET])
        out.append("")
        out.append(DIM + f"green = read within {a.window:g}s   Ctrl-C to stop" + RESET)
        sys.stdout.write("\n".join(out) + "\n")
        sys.stdout.flush()

    def summary(self) -> None:
        cells, _, mismatched = self.board.snapshot()
        print()
        print(f"{'EPC':<26} {'ANT':>4} {'reads':>7} {'lastRSSI':>9} {'maxRSSI':>8}")
        print("-" * 58)
        for (epc, ant), c in sorted(cells.items()):
            print(f"{epc:<26} {ant:>4} {c.count:>7} {c.rssi:>9.1f} {c.max_rssi:>8.1f}")
        print("-" * 58)
        for ant in self.antennas:
            st = self.stats[ant]
            n = sum(1 for (_, x) in cells if x == ant)
            print(f"ANT{ant}: {n} unique tags, {st.reads} reads, {st.runs} runs"
                  + (f", last error {st.error}" if st.error else ""))
        if mismatched:
            print(f"{mismatched} reads carried a different ANT than requested")


def parse_antennas(s: str) -> List[int]:
    ants = [int(x) for x in s.replace(" ", "").split(",") if x]
    if not ants or any(not 1 <= x <= 4 for x in ants):
        raise argparse.ArgumentTypeError("antennas must be 1-4, e.g. 1,3")
    return ants


def main() -> int:
    ap = argparse.ArgumentParser(description="Live tag reads from two antennas of the MPK-R-9504")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--antennas", type=parse_antennas, default=[1, 3],
                    help="comma-separated ports to alternate between (default 1,3)")
    ap.add_argument("--dwell", type=float, default=0.5,
                    help="seconds on each antenna before switching (default 0.5)")
    ap.add_argument("--power", type=float, default=20.0, help="RF power dBm (default 20)")
    ap.add_argument("--rf-mode", type=int, default=RF_ULTRA_FAST,
                    help="103 fastest (hears >= -68 dBm), 285 most sensitive (>= -83 dBm)")
    ap.add_argument("--window", type=float, default=2.0,
                    help="a tag counts as 'present' if read within this many seconds")
    ap.add_argument("--refresh", type=float, default=0.25, help="screen refresh seconds")
    ap.add_argument("--max-rows", type=int, default=30)
    ap.add_argument("--plain", action="store_true",
                    help="print a line per NEW/BACK event instead of redrawing a table")
    ap.add_argument("--verbose", action="store_true", help="show library log lines")
    ap.add_argument("--connect-wait", type=float, default=90.0,
                    help="keep retrying the connection this long (default 90 s)")
    ap.add_argument("--sim", action="store_true", help="run against the bundled simulator")
    args = ap.parse_args()

    sim = None
    if args.sim:
        from simulator import ReaderSimulator
        sim = ReaderSimulator("127.0.0.1", 0, tag_rate=150.0).start()
        args.host, args.port = "127.0.0.1", sim.actual_port

    mon = Monitor(args)
    reader = None
    try:
        reader = mon.connect()
        mon.prepare(reader)
        mon.run(reader)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1
    finally:
        if reader is not None:
            try:
                reader.abort(timeout=2.0)
            except Exception:
                pass
            reader.close()
        if sim:
            sim.stop()
        mon.summary()
        if not args.sim:
            print("\nNote: the box refuses new connections for ~1 min after this disconnect; "
                  "a restarted gate reconnects on its own.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
