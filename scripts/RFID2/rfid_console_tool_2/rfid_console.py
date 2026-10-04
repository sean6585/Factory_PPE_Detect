#!/usr/bin/env python3
"""
MPK-R-9504 RFID 主控台測試程式 (TCP)。

    python rfid_console.py --host 192.168.1.200 --port 8888

熱鍵(直接按,不用按 Enter):

    S  開始持續讀取
    C  清除目前 buffer 中的 RFID 資料
    E  停止讀取
    G  立刻查一次 GPIO input (@InputPort)
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
import threading
import time
import unicodedata
from collections import deque
from typing import Deque, List, Optional

from mpk_rfid import (
    DF_ANTENNA,
    DF_CHANNEL,
    DF_DEFAULT,
    DF_RSSI,
    GPIO_PIN_COUNT,
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
    POLL = 0.02             # 主迴圈輪詢間隔 —— 按鍵反應與自動重啟的延遲上限

    def __init__(self, host: str, port: int, antenna: int,
                 rf_mode: int, power_dbm: float, auto_restart: bool = True,
                 gpio_interval: float = 1.0):
        self.host, self.port = host, port
        self.antenna, self.rf_mode, self.power_dbm = antenna, rf_mode, power_dbm
        self.auto_restart = auto_restart
        # GPIO 輪詢間隔(秒);0 = 不輪詢,只被動接收讀取器主動送來的狀態
        self.gpio_interval = gpio_interval
        self._gpio_next = 0.0

        self.store = TagStore()
        self.reader: Optional[RfidReader] = None
        self.screen = Screen()
        self.messages: Deque[str] = deque(maxlen=4)

        # want_reading = 使用者的意圖(按 S 開啟、按 E 關閉)
        # reading      = 讀取器「現在」真的在盤點
        # 實機就算下了連續模式 (time=0, runs=0) 也可能自己結束盤點,
        # 這兩個狀態分開之後,主迴圈才能在 reader 自己停掉時重新下命令。
        self.want_reading = False
        self.reading = False
        self.restarts = 0
        self.last_end_status: Optional[int] = None
        self._restart_at = 0.0
        self._backoff = 0.0

        self.started_at = 0.0
        self.elapsed_frozen = 0.0
        self._rate = 0.0
        self._rate_last_total = 0
        self._rate_last_time = time.monotonic()
        self.quit = False

        # msg() 會同時被主執行緒與 RX 執行緒呼叫,而 render() 要走訪 messages,
        # 沒有鎖的話會踩到「iteration 中途被修改」的 RuntimeError。
        self._msg_lock = threading.Lock()
        self._last_msg_text = ""
        self._last_msg_count = 0

    # -------------------------------------------------- 訊息

    def msg(self, text: str) -> None:
        """連續重複的訊息合併成「… × N」,避免洗版。"""
        with self._msg_lock:
            if text == self._last_msg_text and self.messages:
                self._last_msg_count += 1
                stamp = self.messages[-1][:8]
                self.messages[-1] = f"{stamp}  {text}   × {self._last_msg_count}"
                return
            self._last_msg_text = text
            self._last_msg_count = 1
            self.messages.append(f"{time.strftime('%H:%M:%S')}  {text}")

    def _snapshot_messages(self) -> List[str]:
        with self._msg_lock:
            return list(self.messages)

    def _reader_log(self, text: str) -> None:
        """
        函式庫的 log 進來這裡。自動重啟一旦開始跑,「Inventory 開始 / 盤點結束」
        每個循環都會來一次,4 行的訊息區馬上就被洗掉 —— 這兩種在重啟模式下
        改由狀態列的「自動重啟: N  上次結束: …」呈現,不進訊息區。
        """
        if (self.want_reading and self.restarts > 0
                and (text.startswith("Inventory 開始") or text.startswith("盤點結束"))):
            return
        self.msg(text)

    # -------------------------------------------------- 連線

    def connect(self) -> bool:
        self.msg(f"連線中… {self.host}:{self.port}")
        try:
            r = RfidReader(self.host, self.port)
            r.on_log = self._reader_log
            r.on_tag = self.store.add          # ← 在 RX 執行緒上被呼叫
            r.on_inventory_finished = self._on_inventory_finished
            r.on_gpio = self._on_gpio
            r.open()
        except Exception as ex:
            self.msg(f"連線失敗: {ex}")
            return False

        self.reader = r
        try:
            self.msg(f"韌體版本: {r.get_firmware_version()}")
        except Exception as ex:
            self.msg(f"讀取韌體版本失敗: {ex}")

        try:                                   # 開場先問一次 GPIO 目前狀態
            r.query_gpio(timeout=1.0)
        except Exception as ex:
            self.msg(f"讀取 GPIO 失敗: {ex}")
        return True

    def _on_gpio(self, pin: int, level: int, prev: Optional[int]) -> None:
        # 在 RX 執行緒上被呼叫 —— 只記訊息,狀態直接讀 reader.gpio
        if prev is not None:
            self.msg(f"GPIO IN{pin}: {'LOW' if prev == 0 else 'HIGH'}"
                     f" → {'LOW' if level == 0 else 'HIGH'}")

    def _on_inventory_finished(self, status: int, total: int, elapsed_ms: int) -> None:
        """
        ★ 這是在 RX 執行緒上被呼叫的,所以這裡「只記狀態」,絕對不要在這裡
          直接呼叫 start_inventory():

          主執行緒若正卡在 send_command() 裡持有 _cmd_lock 等待回應,
          而回應要靠 RX 執行緒送達 —— RX 執行緒此時去搶同一把鎖就會死鎖。
          真正的重啟交給主迴圈的 _maybe_restart()。

        訊息也不用自己印 —— reader.on_log 已經接到 self.msg 了。
        """
        # 結束狀態變了就講一聲(例如從 TIMEOUT 變成某個錯誤碼),
        # 一直是同一種就交給狀態列的計數,不洗版。
        if self.want_reading and self.restarts > 0 and status != self.last_end_status:
            self.msg(f"讀取器自行結束盤點: {status_text(status)} —— 自動重新開始")

        self.last_end_status = status
        self.reading = False
        if not self.want_reading:
            self.elapsed_frozen = time.monotonic() - self.started_at

    # -------------------------------------------------- 動作

    def _issue_start(self) -> bool:
        """實際下 0x6D。成功回 True。"""
        try:
            self.reader.start_inventory(antenna=self.antenna,
                                        rf_mode=self.rf_mode,
                                        power_dbm=self.power_dbm)
        except Exception as ex:
            self.msg(f"下盤點命令失敗: {ex}")
            return False
        self.reading = True
        return True

    def start(self) -> None:
        if self.reader is None or not self.reader.is_open:
            self.msg("尚未連線,無法開始")
            return
        if self.want_reading:
            self.msg("已經在讀取中")
            return

        try:                    # 盤點參數只在這裡設一次,重啟時不重設
            self.reader.set_data_format(DF_DEFAULT)
            self.reader.set_inventory_parameter(rf_mode=self.rf_mode)
        except Exception as ex:
            self.msg(f"設定盤點參數失敗: {ex}")
            return

        self.want_reading = True
        self.restarts = 0
        self.last_end_status = None
        self._backoff = 0.0
        self._restart_at = 0.0
        self.started_at = time.monotonic()
        self.elapsed_frozen = 0.0
        self._rate_last_total = self.store.total_reads
        self._rate_last_time = time.monotonic()

        if not self._issue_start():
            self.want_reading = False

    def _maybe_restart(self) -> None:
        """
        使用者要求持續讀取,但讀取器自己把盤點結束了 → 重新下命令。

        實機即使用連續模式 (time=0, runs=0) 仍可能回 STATUS_INVENTORY_TIMEOUT /
        STATUS_STOP_CONDITION 之類而停下來,這裡負責讓它接上。
        """
        if not (self.want_reading and self.auto_restart) or self.reading:
            return
        if self.reader is None or not self.reader.is_open:
            return
        now = time.monotonic()
        if now < self._restart_at:
            return

        self.restarts += 1
        if self._issue_start():
            self._backoff = 0.0
        else:
            # 一直失敗就逐步拉長間隔,避免把命令通道打爆
            self._backoff = min(2.0, self._backoff * 2 or 0.1)
            self._restart_at = now + self._backoff

    def _maybe_poll_gpio(self) -> None:
        """
        定時送 @InputPort。用 request_gpio()(不等回覆)而不是 query_gpio(),
        否則主迴圈會被卡住最多 1 秒,畫面與按鍵都會頓。
        """
        if not self.gpio_interval or self.reader is None or not self.reader.is_open:
            return
        now = time.monotonic()
        if now < self._gpio_next:
            return
        self._gpio_next = now + self.gpio_interval
        try:
            self.reader.request_gpio()
        except Exception:
            pass                                # 連線問題別在這裡洗版

    def query_gpio_now(self) -> None:
        """G 熱鍵:立刻查一次。"""
        if self.reader is None or not self.reader.is_open:
            self.msg("尚未連線")
            return
        try:
            self.reader.request_gpio()
            self.msg("已送出 GPIO 查詢 (@InputPort)")
        except Exception as ex:
            self.msg(f"查詢 GPIO 失敗: {ex}")

    def stop(self) -> None:
        if not (self.want_reading or self.reading):
            self.msg("目前沒有在讀取")
            return
        self.want_reading = False              # 先關意圖,才不會被自動重啟接回去
        self.elapsed_frozen = time.monotonic() - self.started_at
        if self.reading:
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
        if self.want_reading:                  # 清除後重新計時,速率才有意義
            self.started_at = time.monotonic()
            self.restarts = 0
        self.msg(f"已清除 buffer ({n} 個 EPC)")

    # -------------------------------------------------- 畫面

    def _elapsed(self) -> float:
        # 以「使用者的意圖」計時,自動重啟造成的短暫空檔不歸零
        return (time.monotonic() - self.started_at) if self.want_reading else self.elapsed_frozen

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

        if self.want_reading:
            state = "● 讀取中" if self.reading else "◐ 重新啟動中"
        else:
            state = "○ 已停止"
        lines.append(f" 狀態: {_pad(state, 14)} 經過: {self._elapsed():6.1f} s")
        lines.append(f" 唯一 Tag: {self.store.unique_count:<6} "
                     f"總讀取: {self.store.total_reads:<10,} "
                     f"速率: {self._rate:>6.0f} /s    封包錯誤: {errors}")

        # 讀取器自己結束盤點時,這行會說明重啟了幾次、上次為什麼停
        if self.restarts:
            why = ("上次結束: " + status_text(self.last_end_status)
                   if self.last_end_status is not None else "")
            lines.append(f" 自動重啟: {self.restarts:<6} {why}")

        # ---- GPIO input ----
        gpio = self.reader.gpio if self.reader else {}
        at = self.reader.gpio_updated_at if self.reader else 0.0
        cells = []
        for pin in range(1, GPIO_PIN_COUNT + 1):
            lvl = gpio.get(pin)
            if lvl is None:
                cells.append(_pad(f"IN{pin} - ----", 12))
            else:
                cells.append(_pad(f"IN{pin} {'●' if lvl else '○'} "
                                  f"{'HIGH' if lvl else 'LOW'}", 12))
        age = f"(更新於 {time.time() - at:.1f}s 前)" if at else "(尚未取得)"
        lines.append(" GPIO: " + "  ".join(cells) + "   " + age)
        lines.append(thin)

        # ---- 表頭 ----  欄寬: #(3) EPC(彈性) 次數(6) RSSI(7) Max(7) ANT(3) 最後(7)
        fixed = 3 + 6 + 7 + 7 + 3 + 7 + 7      # 含欄間空白與行首空白
        epc_w = max(16, min(40, cols - fixed))
        lines.append(" " + " ".join([
            _pad("#", 3, ">"), _pad("EPC", epc_w), _pad("次數", 6, ">"),
            _pad("RSSI", 7, ">"), _pad("Max", 7, ">"),
            _pad("ANT", 3, ">"), _pad("最後", 7, ">")]))

        snapshot: List[TagRecord] = self.store.snapshot()
        messages = self._snapshot_messages()
        # 已經排好的行數 + 底部(分隔 1 + 訊息 N + bar 1 + 熱鍵 1 + 緩衝 2)
        reserved = len(lines) + 1 + len(messages) + 2 + 2
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
        for m in messages:
            lines.append(_clip(" " + m, cols))
        lines.append(bar)
        lines.append(" [S] 開始讀取   [C] 清除 buffer   [E] 停止讀取   "
                     "[G] 查詢 GPIO   [Q] 離開")

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

        # 輪詢比重繪頻繁得多:按鍵反應快,讀取器自己停掉時也能馬上接回去,
        # 但畫面仍維持 4 Hz,不會閃。
        last_render = 0.0
        try:
            while not self.quit:
                key = keys.get_key()
                if key:
                    self.on_key(key)

                self._maybe_restart()
                self._maybe_poll_gpio()

                now = time.monotonic()
                if now - last_render >= self.REFRESH:
                    self.render()
                    last_render = now
                time.sleep(self.POLL)
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
        elif key == "g":
            self.query_gpio_now()
        elif key in ("q", "\x1b"):     # Q 或 Esc
            self.quit = True
            self.msg("離開中…")

    def shutdown(self) -> None:
        self.want_reading = False          # 先關意圖,免得關閉過程又被重啟
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
        epilog="熱鍵:  S 開始讀取   C 清除 buffer   E 停止讀取   "
               "G 查詢 GPIO   Q 離開")
    ap.add_argument("--host", default="100.206.151.186")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--antenna", type=int, default=1)
    ap.add_argument("--power", type=float, default=20.0, help="RF 功率 dBm")
    ap.add_argument("--rf-mode", type=int, default=RF_ULTRA_FAST)
    ap.add_argument("--sim", action="store_true", help="就地啟動模擬器並連上它")
    ap.add_argument("--sim-auto-stop", type=float, default=0.0, metavar="秒",
                    help="模擬器每 N 秒自己結束一次盤點(重現實機會自己停的行為)")
    ap.add_argument("--no-auto-restart", action="store_true",
                    help="讀取器自己結束盤點時不要自動重下命令(診斷用)")
    ap.add_argument("--gpio-interval", type=float, default=1.0, metavar="秒",
                    help="每 N 秒送一次 @InputPort 查 GPIO;0 = 不輪詢,只被動接收")
    ap.add_argument("--sim-gpio-toggle", type=float, default=0.0, metavar="秒",
                    help="模擬器每 N 秒隨機翻轉一個 GPIO 並主動回報")
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
        sim = ReaderSimulator("127.0.0.1", 0, tag_rate=300.0,
                              auto_stop_after=args.sim_auto_stop,
                              gpio_toggle_interval=args.sim_gpio_toggle).start()
        host, port = "127.0.0.1", sim.actual_port

    app = ConsoleApp(host, port, args.antenna, args.rf_mode, args.power,
                     auto_restart=not args.no_auto_restart,
                     gpio_interval=args.gpio_interval)
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
