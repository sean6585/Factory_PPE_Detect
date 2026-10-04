#!/usr/bin/env python3
"""
MPK-R-9504 RFID 主控台測試程式 (TCP)。

    python rfid_console.py --host 192.168.1.200 --port 8888

熱鍵(直接按,不用按 Enter):

    S  開始持續讀取
    C  清除目前 buffer 中的 RFID 資料
    E  停止讀取
    Q  離開 (Esc 或 Ctrl-C 也可以)

讀取中畫面會持續更新,顯示 buffer 內每個 EPC 的次數 / RSSI / 最後讀到的時間。

沒有實機時可以先試跑(需要同目錄的 simulator.py):

    python rfid_console.py --sim

自動化 / 展示用,照腳本自動按鍵:

    python rfid_console.py --sim --script "s:3,c,s:2,e:1,q"
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
import unicodedata
from collections import deque
from typing import Deque, List, Optional

from mpk_rfid import (
    DF_ANTENNA,
    DF_CHANNEL,
    DF_DEFAULT,
    DF_RSSI,
    RF_ULTRA_FAST,
    RfidReader,
    TagRecord,
    TagStore,
    status_text,
)


# ============================================================ 鍵盤(不用按 Enter)


class KeyReader:
    """
    非阻塞讀取單一按鍵。Windows 用 msvcrt,Linux/macOS 用 termios。

    stdin 不是終端機(例如被導向檔案)時 get_key() 永遠回 None,不會壞掉。
    """

    def __init__(self):
        self._posix = False
        self._fd = None
        self._saved = None
        self._msvcrt = None
        self._enabled = False

    def __enter__(self) -> "KeyReader":
        try:
            import msvcrt                       # Windows
            self._msvcrt = msvcrt
            self._enabled = True
            return self
        except ImportError:
            pass

        try:                                    # POSIX
            import termios
            import tty
            if not sys.stdin.isatty():
                return self
            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self._posix = True
            self._enabled = True
        except Exception:
            self._enabled = False
        return self

    def __exit__(self, *exc):
        if self._posix and self._saved is not None:
            try:
                import termios
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except Exception:
                pass
        return False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get_key(self) -> Optional[str]:
        """有按鍵就回傳該字元(英文字母一律小寫),沒有就回 None。"""
        if not self._enabled:
            return None

        if self._msvcrt is not None:
            if not self._msvcrt.kbhit():
                return None
            ch = self._msvcrt.getch()
            if ch in (b"\x00", b"\xe0"):       # 功能鍵/方向鍵,吃掉第二個 byte
                self._msvcrt.getch()
                return None
            try:
                return ch.decode("latin-1").lower()
            except Exception:
                return None

        import select
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if not r:
            return None
        ch = sys.stdin.read(1)
        return ch.lower() if ch else None


class ScriptedKeys:
    """照腳本自動送出按鍵 —— 自動化測試 / 展示用。格式: "s:3,c,s:2,e:1,q" """

    def __init__(self, script: str):
        self._steps: List[tuple] = []
        for item in script.split(","):
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                key, _, sec = item.partition(":")
                self._steps.append((key.strip().lower()[:1], float(sec)))
            else:
                self._steps.append((item.lower()[:1], 0.0))
        self._i = 0
        self._next_at = time.monotonic()

    enabled = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_key(self) -> Optional[str]:
        if self._i >= len(self._steps):
            return None
        now = time.monotonic()
        if now < self._next_at:
            return None
        key, wait = self._steps[self._i]
        self._i += 1
        self._next_at = now + wait
        return key


# ============================================================ 畫面


def _enable_ansi() -> bool:
    """Windows 10+ 的 cmd 需要打開 VT 處理才吃 ANSI escape。"""
    if os.name != "nt":
        return sys.stdout.isatty()
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)                     # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


class Screen:
    """整頁重繪,用 ANSI 游標歸位避免閃爍;不支援 ANSI 就退回一般 print。"""

    def __init__(self):
        self.ansi = _enable_ansi()
        self._first = True

    def size(self) -> tuple:
        try:
            s = shutil.get_terminal_size((100, 30))
            return s.columns, s.lines
        except Exception:
            return 100, 30

    def draw(self, lines: List[str]) -> None:
        if self.ansi:
            out = ["\033[H"]                        # 游標回左上角
            for ln in lines:
                out.append(ln + "\033[K\n")         # 清到行尾
            out.append("\033[J")                    # 清掉下方殘留
            sys.stdout.write("".join(out))
        else:
            if self._first:
                self._first = False
            print("\n" * 2 + "\n".join(lines))
        sys.stdout.flush()

    def clear(self) -> None:
        if self.ansi:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()


def _w(s: str) -> int:
    """顯示寬度 —— 中日韓全形字佔 2 欄,不然表格會對不齊。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _clip(s: str, width: int) -> str:
    if _w(s) <= width:
        return s
    out, used = [], 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        if used + cw > width - 1:
            break
        out.append(c)
        used += cw
    return "".join(out) + "…"


def _pad(s: str, width: int, align: str = "<") -> str:
    """依顯示寬度補空白(str.ljust 用的是字元數,對全形字會算錯)。"""
    s = _clip(s, width)
    gap = max(0, width - _w(s))
    if align == ">":
        return " " * gap + s
    if align == "^":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


# ============================================================ 主程式


class ConsoleApp:
    REFRESH = 0.25          # 畫面更新間隔(秒)

    def __init__(self, host: str, port: int, antenna: int,
                 rf_mode: int, power_dbm: float):
        self.host, self.port = host, port
        self.antenna, self.rf_mode, self.power_dbm = antenna, rf_mode, power_dbm

        self.store = TagStore()
        self.reader: Optional[RfidReader] = None
        self.screen = Screen()
        self.messages: Deque[str] = deque(maxlen=4)

        self.reading = False
        self.started_at = 0.0
        self.elapsed_frozen = 0.0
        self._rate = 0.0
        self._rate_last_total = 0
        self._rate_last_time = time.monotonic()
        self.quit = False

    # -------------------------------------------------- 訊息

    def msg(self, text: str) -> None:
        self.messages.append(f"{time.strftime('%H:%M:%S')}  {text}")

    # -------------------------------------------------- 連線

    def connect(self) -> bool:
        self.msg(f"連線中… {self.host}:{self.port}")
        try:
            r = RfidReader(self.host, self.port)
            r.on_log = self.msg
            r.on_tag = self.store.add          # ← 在 RX 執行緒上被呼叫
            r.on_inventory_finished = self._on_inventory_finished
            r.open()
        except Exception as ex:
            self.msg(f"連線失敗: {ex}")
            return False

        self.reader = r
        try:
            self.msg(f"韌體版本: {r.get_firmware_version()}")
        except Exception as ex:
            self.msg(f"讀取韌體版本失敗: {ex}")
        return True

    def _on_inventory_finished(self, status: int, total: int, elapsed_ms: int) -> None:
        # 注意:這是在 RX 執行緒上被呼叫的
        if self.reading:
            self.elapsed_frozen = time.monotonic() - self.started_at
        self.reading = False
        self.msg(f"盤點結束: {status_text(status)}  singulation={total}  {elapsed_ms} ms")

    # -------------------------------------------------- 動作

    def start(self) -> None:
        if self.reader is None or not self.reader.is_open:
            self.msg("尚未連線,無法開始")
            return
        if self.reading:
            self.msg("已經在讀取中")
            return
        try:
            self.reader.set_data_format(DF_DEFAULT)
            self.reader.set_inventory_parameter(rf_mode=self.rf_mode)
            self.reader.start_inventory(antenna=self.antenna,
                                        rf_mode=self.rf_mode,
                                        power_dbm=self.power_dbm)
        except Exception as ex:
            self.msg(f"開始讀取失敗: {ex}")
            return
        self.reading = True
        self.started_at = time.monotonic()
        self.elapsed_frozen = 0.0
        self._rate_last_total = self.store.total_reads
        self._rate_last_time = time.monotonic()

    def stop(self) -> None:
        if not self.reading:
            self.msg("目前沒有在讀取")
            return
        self.elapsed_frozen = time.monotonic() - self.started_at
        try:
            self.reader.abort()
        except Exception as ex:
            self.msg(f"停止失敗: {ex}")
        self.reading = False

    def clear(self) -> None:
        n = self.store.unique_count
        self.store.clear()
        self._rate_last_total = 0
        self._rate_last_time = time.monotonic()
        self._rate = 0.0
        if self.reading:                       # 清除後重新計時,速率才有意義
            self.started_at = time.monotonic()
        self.msg(f"已清除 buffer ({n} 個 EPC)")

    # -------------------------------------------------- 畫面

    def _elapsed(self) -> float:
        return (time.monotonic() - self.started_at) if self.reading else self.elapsed_frozen

    def _update_rate(self) -> None:
        now = time.monotonic()
        dt = now - self._rate_last_time
        if dt >= 0.5:
            total = self.store.total_reads
            self._rate = (total - self._rate_last_total) / dt
            self._rate_last_total = total
            self._rate_last_time = now

    def render(self) -> None:
        self._update_rate()
        cols, rows_avail = self.screen.size()
        cols = max(72, min(cols, 160))
        bar = "=" * cols
        thin = "-" * cols

        connected = self.reader is not None and self.reader.is_open
        conn_txt = "已連線" if connected else "未連線"
        errors = self.reader.parser_error_count if self.reader else 0

        lines: List[str] = []
        lines.append(bar)
        title = " MPK-R-9504 RFID 讀取測試"
        right = f"{self.host}:{self.port}  [{conn_txt}] "
        lines.append(_clip(title + " " * max(1, cols - _w(title) - _w(right)) + right, cols))
        lines.append(bar)

        state = "● 讀取中" if self.reading else "○ 已停止"
        lines.append(f" 狀態: {_pad(state, 12)}   經過: {self._elapsed():6.1f} s")
        lines.append(f" 唯一 Tag: {self.store.unique_count:<6} "
                     f"總讀取: {self.store.total_reads:<10,} "
                     f"速率: {self._rate:>6.0f} /s    封包錯誤: {errors}")
        lines.append(thin)

        # ---- 表頭 ----  欄寬: #(3) EPC(彈性) 次數(6) RSSI(7) Max(7) ANT(3) 最後(7)
        fixed = 3 + 6 + 7 + 7 + 3 + 7 + 7      # 含欄間空白與行首空白
        epc_w = max(16, min(40, cols - fixed))
        lines.append(" " + " ".join([
            _pad("#", 3, ">"), _pad("EPC", epc_w), _pad("次數", 6, ">"),
            _pad("RSSI", 7, ">"), _pad("Max", 7, ">"),
            _pad("ANT", 3, ">"), _pad("最後", 7, ">")]))

        snapshot: List[TagRecord] = self.store.snapshot()
        # 保留:標題 6 行 + 表頭 1 + 分隔 1 + 訊息區 (len+2) + 熱鍵 2
        reserved = 6 + 1 + 1 + len(self.messages) + 2 + 3
        max_rows = max(3, rows_avail - reserved)
        shown = snapshot[:max_rows]

        now = time.time()
        for i, r in enumerate(shown, 1):
            age = now - r.last_seen
            max_rssi = "-" if r.max_rssi == float("-inf") else f"{r.max_rssi:.1f}"
            lines.append(" " + " ".join([
                _pad(str(i), 3, ">"), _pad(r.epc, epc_w), _pad(str(r.count), 6, ">"),
                _pad(f"{r.rssi:.1f}", 7, ">"), _pad(max_rssi, 7, ">"),
                _pad(str(r.antenna), 3, ">"), _pad(f"{age:.1f}s", 7, ">")]))

        if not snapshot:
            lines.append("      (buffer 是空的 —— 按 S 開始讀取)")
        elif len(snapshot) > len(shown):
            lines.append(f"      … 還有 {len(snapshot) - len(shown)} 筆 (視窗放大可看到更多)")

        lines.append(thin)
        for m in self.messages:
            lines.append(_clip(" " + m, cols))
        lines.append(bar)
        lines.append(" [S] 開始讀取    [C] 清除 buffer    [E] 停止讀取    [Q] 離開")

        self.screen.draw(lines)

    # -------------------------------------------------- 主迴圈

    def run(self, keys) -> int:
        self.screen.clear()
        if not self.connect():
            self.render()
            print()
            return 1

        if not getattr(keys, "enabled", False):
            self.msg("警告:偵測不到終端機輸入,熱鍵無效(用 --script 或在真的主控台執行)")

        try:
            while not self.quit:
                key = keys.get_key()
                if key:
                    self.on_key(key)
                self.render()
                time.sleep(self.REFRESH)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def on_key(self, key: str) -> None:
        if key == "s":
            self.start()
        elif key == "c":
            self.clear()
        elif key == "e":
            self.stop()
        elif key in ("q", "\x1b"):     # Q 或 Esc
            self.quit = True
            self.msg("離開中…")

    def shutdown(self) -> None:
        if self.reader is not None:
            try:
                if self.reading:
                    self.reader.abort()
            except Exception:
                pass
            try:
                self.reader.close()
            except Exception:
                pass
        self.reading = False
        self.render()
        print()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="MPK-R-9504 RFID 主控台測試程式",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="熱鍵:  S 開始讀取   C 清除 buffer   E 停止讀取   Q 離開")
    ap.add_argument("--host", default="192.168.1.200")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--antenna", type=int, default=1)
    ap.add_argument("--power", type=float, default=20.0, help="RF 功率 dBm")
    ap.add_argument("--rf-mode", type=int, default=RF_ULTRA_FAST)
    ap.add_argument("--sim", action="store_true", help="就地啟動模擬器並連上它")
    ap.add_argument("--script", default=None,
                    help='照腳本自動按鍵,例如 "s:3,c,s:2,e:1,q"')
    args = ap.parse_args()

    sim = None
    host, port = args.host, args.port
    if args.sim:
        try:
            from simulator import ReaderSimulator
        except ImportError:
            print("--sim 需要同目錄的 simulator.py", file=sys.stderr)
            return 1
        sim = ReaderSimulator("127.0.0.1", 0, tag_rate=300.0).start()
        host, port = "127.0.0.1", sim.actual_port

    app = ConsoleApp(host, port, args.antenna, args.rf_mode, args.power)
    if sim:
        app.msg(f"模擬器已啟動於 {host}:{port}")

    try:
        keys = ScriptedKeys(args.script) if args.script else KeyReader()
        with keys:
            return app.run(keys)
    finally:
        if sim:
            sim.stop()


if __name__ == "__main__":
    sys.exit(main())
