#!/usr/bin/env python3
"""
雙天線即時讀取測試 —— 終端機左邊 ANT 1、右邊 ANT 3,即時更新。

獨立工具,不經過也不修改 PPE gate:直接連 MPK-R-9504 讀取器,輪流對每支天線下
「限時盤點」(0x6D 一次只能指定一支天線),每筆讀取依「讀取器回報的天線編號」分邊
顯示 —— 不是依我們剛問哪一支,所以畫面也順便驗證了天線編號本身。

讀取器同一時間只接受一個連線,PPE gate 執行中時連不上:

    sudo systemctl stop ppe-gate
    python3 scripts/rfid_antenna_test.py              # q 結束 · c 清除統計
    sudo systemctl start ppe-gate

讀取器在前一個連線斷開後約一分鐘內會拒絕新連線,這段時間會自動重試(--wait)。

    python3 scripts/rfid_antenna_test.py --watch 0007     # 標出某張卡(EPC 片段)
    python3 scripts/rfid_antenna_test.py --sim            # 沒有實機:用函式庫的模擬器
"""

from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from collections import Counter, deque

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(_HERE, "RFID2", "rfid_console_tool_2")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from mpk_rfid import DF_DEFAULT, STATUS_BUSY, RfidError, RfidReader, status_text  # noqa: E402

DEFAULT_HOST, DEFAULT_PORT = "100.206.151.186", 8080
GATE_JSON = os.path.join(_HERE, "..", "config", "gate.json")
RF_MODE_NAMES = {103: "ULTRA_FAST", 302: "FAST", 345: "NORMAL", 285: "ULTRA_SENSITIVE"}

RATE_WINDOW_S = 2.0      # reads/s is counted over this much wall clock
END_GRACE_S = 2.0        # a timed run that has not reported its end by dwell + this is aborted
REDRAW_S = 0.2           # screen refresh period (tty)
PLAIN_EVERY_S = 1.0      # snapshot period when stdout is not a terminal (logs, pipes)
# A timed run normally ends with one of these; anything else (BUSY, ERR_ANTENNA_NOT_ENABLE,
# …) is shown in red on that antenna's side.
NORMAL_ENDS = {"STATUS_OK", "STATUS_STOP_CONDITION", "STATUS_INVENTORY_TIMEOUT", "STATUS_ABORT"}


# ---------------------------------------------------------------- display width
# Library messages are Chinese; CJK characters take two terminal columns, so padding and
# truncation go by display width, not len().

def dwidth(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def fit(s: str, w: int, right: bool = False) -> str:
    """Pad or cut `s` to exactly `w` columns."""
    if w <= 0:
        return ""
    if dwidth(s) > w:
        out, n = "", 0
        for c in s:
            cw = dwidth(c)
            if n + cw > w:
                break
            out, n = out + c, n + cw
        s = out
    pad = " " * (w - dwidth(s))
    return pad + s if right else s + pad


# ---------------------------------------------------------------- statistics

class TagStat:
    __slots__ = ("epc", "reads", "last", "peak", "first_seen", "last_seen", "recent")

    def __init__(self, epc: str, now: float):
        self.epc, self.reads = epc, 0
        self.last, self.peak = 0.0, float("-inf")
        self.first_seen = self.last_seen = now
        self.recent: deque = deque()

    def rate(self, now: float) -> float:
        while self.recent and now - self.recent[0] > RATE_WINDOW_S:
            self.recent.popleft()
        return len(self.recent) / RATE_WINDOW_S


class AntStat:
    def __init__(self, ant: int):
        self.ant = ant
        self.tags: dict = {}
        self.reads = 0
        self.runs = 0
        self.on_air = 0.0
        self.ends: Counter = Counter()
        self.last_end = "-"
        self.recent: deque = deque()

    def rate(self, now: float) -> float:
        while self.recent and now - self.recent[0] > RATE_WINDOW_S:
            self.recent.popleft()
        return len(self.recent) / RATE_WINDOW_S


class State:
    """Written by the reader's RX thread (on_tag / on_inventory_finished), read by the
    main thread's renderer — one lock around both."""

    def __init__(self, ants):
        self.lock = threading.Lock()
        self.ants = list(ants)
        self.stats = {a: AntStat(a) for a in ants}
        self.other = Counter()           # reads reporting an antenna we did not ask for
        self.current = None              # antenna on air right now (None = setup)
        self.notes: deque = deque(maxlen=1)
        self.started = time.time()

    def on_tag(self, t) -> None:
        if not t or not t.epc:
            return
        now = time.time()
        with self.lock:
            # 0 = the data format carried no antenna byte: fall back to the one on air.
            ant = t.antenna_id or self.current
            st = self.stats.get(ant)
            if st is None:
                self.other[ant] += 1
                return
            epc = t.epc_hex
            tag = st.tags.get(epc)
            if tag is None:
                tag = st.tags[epc] = TagStat(epc, now)
            tag.reads += 1
            tag.last = t.rssi_dbm
            tag.peak = max(tag.peak, t.rssi_dbm)
            tag.last_seen = now
            tag.recent.append(now)
            st.reads += 1
            st.recent.append(now)

    def clear(self) -> None:
        with self.lock:
            self.stats = {a: AntStat(a) for a in self.ants}
            self.other.clear()
            self.started = time.time()

    def note(self, msg: str) -> None:
        self.notes.append(time.strftime("%H:%M:%S ") + msg)


# ---------------------------------------------------------------- rendering

C = {"b": "\x1b[1m", "dim": "\x1b[2m", "inv": "\x1b[7m", "red": "\x1b[31m",
     "green": "\x1b[32m", "yellow": "\x1b[33m", "cyan": "\x1b[36m", "barbg": "\x1b[42m",
     "x": "\x1b[0m"}


class Painter:
    def __init__(self, color: bool):
        self.color = color

    def __call__(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return "".join(C[s] for s in styles) + text + C["x"]


def _bar(rssi: float, w: int, paint: Painter) -> str:
    """Last RSSI as a bar: -80 dBm empty … -30 dBm full."""
    if w <= 0:
        return ""
    n = max(0, min(w, round((rssi + 80.0) / 50.0 * w)))
    if paint.color:
        return paint(" " * n, "barbg") + " " * (w - n)
    return "#" * n + "." * (w - n)


def _side(st: AntStat, cur, w: int, rows: int, now: float, args, paint: Painter):
    """One antenna's half of the screen: exactly 4 + rows lines, each exactly w columns."""
    on_air = cur == st.ant
    head = f" ANT {st.ant}" + ("  * ON AIR" if on_air else "")
    lines = [paint(fit(head, w), "b", "inv", "cyan") if on_air else paint(fit(head, w), "b")]
    lines.append(fit(f" tags {len(st.tags)}   reads {st.reads}   {st.rate(now):.0f}/s"
                     f"   runs {st.runs}", w))
    bad = sum(n for e, n in st.ends.items() if e not in NORMAL_ENDS)
    end = f" last end: {st.last_end.replace('STATUS_', '')}" + (f"   errors {bad}" if bad else "")
    lines.append(paint(fit(end, w), "red") if st.last_end not in NORMAL_ENDS | {"-"}
                 else paint(fit(end, w), "dim"))

    # Columns: EPC, reads, r/s, last dBm, peak dBm, age, bar. A narrow screen drops age,
    # then r/s, then the bar; EPC gets what is left, cut from the LEFT — the
    # distinguishing digits of these badges are at the end.
    tags = list(st.tags.values())
    epc_len = max([len(t.epc) for t in tags] + [8])
    cols = {"reads": 7, "r/s": 6, "last": 7, "peak": 7, "age": 6}
    for drop in ("age", "r/s"):
        if 1 + sum(cols.values()) + min(epc_len, 10) <= w:
            break
        del cols[drop]
    fixed = 1 + sum(cols.values())
    bar_w = max(0, min(12, w - fixed - epc_len - 1))
    epc_w = max(4, min(max(epc_len, 3), w - fixed - (bar_w + 1 if bar_w else 0)))
    hdr = " " + fit("EPC", epc_w) + "".join(fit(c, n, True) for c, n in cols.items())
    lines.append(paint(fit(hdr, w), "dim"))

    # Sorted by PEAK, which only ever rises: sorting by the last read reshuffled the rows
    # five times a second and made them unreadable.
    fresh = sorted((t for t in tags if now - t.last_seen <= args.fresh), key=lambda t: -t.peak)
    stale = sorted((t for t in tags if now - t.last_seen > args.fresh), key=lambda t: -t.last_seen)
    shown = fresh + stale
    for t in shown[:rows]:
        epc = t.epc if len(t.epc) <= epc_w else ".." + t.epc[-(epc_w - 2):]
        age = now - t.last_seen
        watched = bool(args.watch) and args.watch.upper() in t.epc
        cells = {"reads": str(t.reads), "r/s": f"{t.rate(now):.0f}", "last": f"{t.last:.1f}",
                 "peak": f"{t.peak:.1f}", "age": f"{age:.1f}s" if age < 100 else "99+"}
        text = (">" if watched else " ") + fit(epc, epc_w) \
            + "".join(fit(cells[c], n, True) for c, n in cols.items())
        style = ("yellow", "b") if watched else (("green",) if age <= args.fresh else ("dim",))
        row = paint(text, *style)
        if bar_w:
            row += " " + (_bar(t.last, bar_w, paint) if age <= args.fresh else " " * bar_w)
        lines.append(row + " " * max(0, w - dwidth(text) - (bar_w + 1 if bar_w else 0)))
    if len(shown) > rows:
        lines[-1] = paint(fit(f" + {len(shown) - rows + 1} more", w), "dim")
    while len(lines) < 4 + rows:
        lines.append(" " * w)
    return lines


def render(state: State, args, cols: int, height: int, color: bool, firmware: str):
    paint = Painter(color)
    now = time.time()
    a, b = state.ants
    with state.lock:
        sa, sb, cur = state.stats[a], state.stats[b], state.current
        both = sorted((e for e in sa.tags if e in sb.tags),
                      key=lambda e: -max(sa.tags[e].peak, sb.tags[e].peak))
        n_both = min(len(both), 4)
        w = max(30, (cols - 3) // 2)
        rows = max(3, height - 4 - 4 - (2 + n_both if both else 1) - 2)
        left = _side(sa, cur, w, rows, now, args, paint)
        right = _side(sb, cur, w, rows, now, args, paint)

        up = int(now - state.started)
        title = (f" RFID antenna test  up {up // 60:02d}:{up % 60:02d}   {args.power:g} dBm"
                 f"   mode {args.rf_mode} {RF_MODE_NAMES.get(args.rf_mode, '')}"
                 f"   {args.dwell:g} s/antenna   {args.host}:{args.port}")
        out = [paint(fit(title, cols), "b"),
               paint(fit(f" firmware {firmware}   left = ANT {a}   right = ANT {b}"
                         f"   green = heard in the last {args.fresh:g} s", cols), "dim"), ""]
        out += [l + paint(" | ", "dim") + r for l, r in zip(left, right)]
        out.append("")
        if both:
            out.append(paint(fit(f" Heard by both (peak dBm): {len(both)} tag(s)", cols), "b"))
            for e in both[:n_both]:
                pa, pb = sa.tags[e].peak, sb.tags[e].peak
                win = a if pa >= pb else b
                line = (f"   {e}   ANT{a} {pa:6.1f}   ANT{b} {pb:6.1f}"
                        f"   ANT{win} +{abs(pa - pb):.1f} dB")
                watched = bool(args.watch) and args.watch.upper() in e
                out.append(paint(fit(line, cols), "yellow", "b") if watched else fit(line, cols))
        if state.other:
            out.append(paint(fit(" reads reporting other antennas: " + ", ".join(
                f"ANT {k}: {v}" for k, v in sorted(state.other.items())), cols), "red"))
        foot = " q quit   c clear" + (f"   watching *{args.watch}*" if args.watch else "")
        if state.notes:
            foot += "   " + state.notes[-1]
        out.append(paint(fit(foot, cols), "dim"))
    return out


def summary(state: State, args) -> str:
    """Plain text left on the normal screen after exit."""
    lines = []
    with state.lock:
        for ant in state.ants:
            st = state.stats[ant]
            ends = ", ".join(f"{k.replace('STATUS_', '')} {v}" for k, v in st.ends.most_common())
            lines.append(f"ANT {ant}: {len(st.tags)} tag(s), {st.reads} reads in {st.runs} runs"
                         f" ({st.on_air:.1f} s on air)   ends: {ends or '-'}")
            for t in sorted(st.tags.values(), key=lambda t: -t.peak):
                mark = "   <== watch" if args.watch and args.watch.upper() in t.epc else ""
                lines.append(f"    {t.epc:<26} reads {t.reads:6d}   peak {t.peak:6.1f} dBm{mark}")
        if state.other:
            lines.append(f"reads reporting other antennas: {dict(state.other)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- keyboard

class Keys:
    """Single keystrokes without Enter (cbreak), only when stdin is a terminal."""

    def __init__(self):
        self.fd, self.saved = None, None

    def __enter__(self):
        if sys.stdin.isatty():
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def poll(self) -> str:
        if self.fd is None or not select.select([self.fd], [], [], 0)[0]:
            return ""
        return os.read(self.fd, 32).decode(errors="ignore")


# ---------------------------------------------------------------- reader

def gate_active() -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", "ppe-gate"],
                              timeout=3).returncode == 0
    except Exception:
        return False


def connect(args) -> RfidReader:
    reader = RfidReader(args.host, args.port)
    deadline = time.monotonic() + args.wait
    hinted = False
    while True:
        try:
            reader.open()
            return reader
        except OSError as e:
            if time.monotonic() >= deadline:
                sys.exit(f"讀取器 {args.host}:{args.port} 連不上 ({e}),已等 {args.wait:g} s。")
            if not hinted:
                hinted = True
                if not args.sim and gate_active():
                    print("PPE gate 正在使用讀取器(一次只接受一個連線),先停止它:\n"
                          "    sudo systemctl stop ppe-gate", flush=True)
                print(f"連線被拒 ({e}) —— 剛斷線的話讀取器約一分鐘後才放行,"
                      f"最多重試 {args.wait:g} s …", flush=True)
            time.sleep(1.0)


def safe_abort(reader: RfidReader) -> None:
    # The box acks Abort only sometimes (none when nothing is running), so a timeout here
    # is normal; the inventory-finished notification is what really says it stopped.
    try:
        reader.abort(timeout=1.0)
    except Exception:
        pass


def prepare(reader: RfidReader, args) -> str:
    # The gate's continuous inventory outlives its connection: every setup command
    # answers STATUS_BUSY until it is aborted.
    safe_abort(reader)
    time.sleep(0.3)
    for attempt in range(5):
        try:
            reader.set_data_format(DF_DEFAULT)          # without it reads carry no RSSI / ANT
            reader.set_inventory_parameter(rf_mode=args.rf_mode)
            break
        except RfidError as e:
            if e.status != STATUS_BUSY or attempt == 4:
                raise
            safe_abort(reader)
            time.sleep(0.5)
    try:
        return reader.get_firmware_version()
    except Exception:
        return "?"


def run(args) -> int:
    sim = None
    if args.sim:
        from simulator import ReaderSimulator
        sim = ReaderSimulator(port=0, tag_rate=120.0).start()
        args.host, args.port = "127.0.0.1", sim.actual_port

    state = State(args.ants)
    done = threading.Event()
    ended: list = [None]

    def on_finished(status, total, elapsed):
        with state.lock:
            st = state.stats.get(state.current)
            if st is not None:            # None = setup (the gate's leftover being aborted)
                name = status_text(status)
                st.ends[name] += 1
                st.last_end = name
        ended[0] = status
        done.set()

    reader = connect(args)
    tty_out = sys.stdout.isatty()
    try:
        reader.on_tag = state.on_tag
        reader.on_inventory_finished = on_finished
        firmware = prepare(reader, args)
        if tty_out:
            sys.stdout.write("\x1b[?1049h\x1b[?25l")    # alternate screen, hide cursor
        last_draw = 0.0
        stop_at = time.monotonic() + args.seconds if args.seconds else None
        quit_ = False

        with Keys() as keys:
            def tick():
                nonlocal last_draw, quit_
                k = keys.poll().lower()
                if "q" in k:
                    quit_ = True
                if "c" in k:
                    state.clear()
                    state.note("cleared")
                if stop_at and time.monotonic() >= stop_at:
                    quit_ = True
                now = time.monotonic()
                if now - last_draw >= (REDRAW_S if tty_out else PLAIN_EVERY_S):
                    last_draw = now
                    size = shutil.get_terminal_size((160, 40))
                    lines = render(state, args, size.columns, size.lines, tty_out, firmware)
                    if tty_out:
                        sys.stdout.write("\x1b[H" + "\x1b[K\n".join(lines) + "\x1b[K\x1b[J")
                    else:
                        sys.stdout.write("\n".join(lines) + "\n" + "-" * 40 + "\n")
                    sys.stdout.flush()

            while not quit_:
                for ant in args.ants:
                    if quit_:
                        break
                    done.clear()
                    ended[0] = None
                    with state.lock:
                        state.current = ant
                    t0 = time.monotonic()
                    try:
                        reader.start_inventory(antenna=ant, rf_mode=args.rf_mode,
                                               power_dbm=args.power,
                                               time_ms=int(args.dwell * 1000))
                    except Exception as e:
                        state.note(f"ANT {ant} start failed: {e}")
                        safe_abort(reader)
                        time.sleep(0.3)
                        continue
                    deadline = t0 + args.dwell + END_GRACE_S
                    while not done.wait(0.05):
                        tick()
                        if quit_ or time.monotonic() > deadline:
                            if not quit_:
                                state.note(f"ANT {ant}: no end after "
                                           f"{args.dwell + END_GRACE_S:g} s, aborted")
                            safe_abort(reader)
                            done.wait(1.0)
                            break
                    with state.lock:
                        st = state.stats[ant]
                        st.runs += 1
                        st.on_air += time.monotonic() - t0
                    if ended[0] is not None and status_text(ended[0]) not in NORMAL_ENDS:
                        time.sleep(0.2)     # BUSY / error: do not hammer the box
                    tick()
    except KeyboardInterrupt:
        pass
    finally:
        safe_abort(reader)
        reader.close()
        if tty_out:
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
        if sim:
            sim.stop()
    print(summary(state, args))
    if not args.sim and not gate_active():
        print("\nPPE gate 目前是停止的 —— 測完記得:  sudo systemctl start ppe-gate")
    return 0


def _gate_defaults():
    """The gate's own power / mode, read-only, so a test matches what the gate uses."""
    try:
        with open(GATE_JSON, encoding="utf-8") as f:
            g = json.load(f)
        return float(g.get("rfid_power_dbm", 20.0)), int(g.get("rfid_rf_mode", 103))
    except Exception:
        return 20.0, 103


def main() -> int:
    power, mode = _gate_defaults()
    ap = argparse.ArgumentParser(description="MPK-R-9504 雙天線即時讀取測試(左 ANT 1 / 右 ANT 3)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--ants", default="1,3", help="兩支天線的埠號,左,右 (預設 1,3)")
    ap.add_argument("--power", type=float, default=power,
                    help=f"發射功率 dBm (預設 = gate.json 的 {power:g})")
    ap.add_argument("--rf-mode", type=int, default=mode,
                    help=f"接收模式 103/302/345/285 (預設 = gate.json 的 {mode})")
    ap.add_argument("--dwell", type=float, default=0.5, help="每支天線每輪讀幾秒 (預設 0.5)")
    ap.add_argument("--fresh", type=float, default=2.0, help="幾秒內讀到算「正在讀到」(綠色)")
    ap.add_argument("--watch", default="", help="要標出來的 EPC 片段,例如 0007")
    ap.add_argument("--seconds", type=float, default=0, help="跑幾秒後自動結束 (0 = 按 q)")
    ap.add_argument("--wait", type=float, default=90, help="連線被拒時重試幾秒 (預設 90)")
    ap.add_argument("--sim", action="store_true", help="不連實機,用函式庫的模擬器")
    args = ap.parse_args()

    try:
        args.ants = [int(x) for x in args.ants.split(",")]
    except ValueError:
        ap.error("--ants 要像 1,3")
    if len(args.ants) != 2 or len(set(args.ants)) != 2 or not all(1 <= a <= 4 for a in args.ants):
        ap.error("--ants 要剛好兩支不同的天線 (1-4),例如 1,3")
    if not 0.1 <= args.dwell <= 10:
        ap.error("--dwell 要在 0.1-10 秒之間")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
