#!/usr/bin/env python3
"""
mpk_rfid —— MPK-R-9504 UHF RFID 讀取器 (TCP)。單一檔案,零第三方相依。

依照 MPK-R-9504 Protocol User Guide v1.0 實作。只做「讀取 RFID tag」。

最簡用法 —— 掃描 5 秒,拿到結果:

    from mpk_rfid import scan

    for t in scan("192.168.1.200", 8888, seconds=5):
        print(t.epc, t.count, f"{t.rssi:.1f} dBm")

要即時處理每一筆讀取:

    from mpk_rfid import RfidReader

    with RfidReader("192.168.1.200", 8888) as r:
        r.on_tag = lambda tag: print(tag.epc_hex, tag.rssi_dbm)
        r.start_inventory(power_dbm=20.0)
        time.sleep(5)
        r.abort()

命令列試跑(不需要實機,需要同目錄的 simulator.py):

    python mpk_rfid.py --sim
    python mpk_rfid.py --host 192.168.1.200 --port 8888 --seconds 10
"""

from __future__ import annotations

import argparse
import re
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

__version__ = "2.1.0"

__all__ = [
    "scan", "RfidReader", "TagReport", "TagRecord", "TagStore",
    "Packet", "PacketParser", "RfidError",
    "checksum", "status_text", "parse_gpio_text",
]


# =============================================================== 協定常數
#
# 封包格式 (Figure 1):
#   [Preamble 0x0A][MT 1][MC 1][PL 1][Payload PL][Checksum 1][EndMark 0x0D 0x0A]
# Checksum = MT ^ MC ^ PL ^ Payload 全部位元組 XOR  (§1.2.5)
# 全部欄位為大端序 (big-endian)。
#
# 註:文件 §1.2.3 寫 Payload Length 是 2 bytes,但 Figure 1 標示 header 共 3
#     bytes,且文件中所有範例封包的 checksum 都只有在 PL = 1 byte 時才吻合,
#     故依範例實作。

PREAMBLE = 0x0A
END_MARK = b"\x0d\x0a"
OVERHEAD = 7          # Preamble + MT + MC + PL + Checksum + EndMark(2)

# Message Type (Table 2)
MT_COMMAND = 0x80
MT_RESPONSE = 0x81
MT_NOTIFICATION = 0x82
MT_ALL = (MT_COMMAND, MT_RESPONSE, MT_NOTIFICATION)

# ---- GPIO 用的 ASCII 文字框 ----
#
# GPIO 走的是「同一個 TCP 連線、同樣的 0x0A 開頭 + 0x0D0A 結尾」,
# 但中間不是二進位協定,而是純文字,而且沒有長度欄位、沒有 checksum:
#
#     0x0A '@' <ASCII 文字> 0x0D 0x0A
#
# 實測封包:
#   0A 40 49 6E 70 75 74 50 6F 72 74 0D 0A            → "@InputPort"    (查詢指令)
#   0A 40 49 6E 70 75 74 20 50 69 6E 31 2C 30 0D 0A   → "@Input Pin1,0" (Pin1 = LOW/接地)
#   0A 40 49 6E 70 75 74 20 50 69 6E 31 2C 31 0D 0A   → "@Input Pin1,1" (Pin1 = HIGH)
#
# 注意 0x40 就是 '@' —— 剛好落在二進位協定的 MT 欄位位置,所以解析器
# 是用「第 2 個位元組是不是 0x40」來決定要走文字還是二進位路徑。
MT_TEXT = 0x40
TEXT_PREFIX = b"@"
MAX_TEXT_LEN = 200            # 文字框內文長度上限(防呆,避免雜訊害解析器空等)

TEXT_CMD_INPUT_PORT = "InputPort"   # 查詢全部 GPIO input 狀態
GPIO_PIN_COUNT = 3                  # 本機型只有 3 個 GPIO input

#: 比對 "Input Pin1,0" / "Input Pin3,1";空白與大小寫都放寬
_GPIO_INPUT_RE = re.compile(r"^input\s*pin\s*(\d+)\s*,\s*([01])$", re.IGNORECASE)


def parse_gpio_text(text: str) -> Optional[Tuple[int, int]]:
    """
    解析 GPIO input 文字框的內文。

        "Input Pin1,0" → (1, 0)
        "Input Pin3,1" → (3, 1)

    不是 GPIO 狀態就回 None。
    """
    m = _GPIO_INPUT_RE.match(text.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))

# Message Code (Table 3) —— 只列讀取 tag 會用到的
CMD_GET_FIRMWARE = 0x01
CMD_GET_TEMPERATURE = 0x05
CMD_ABORT = 0x10
CMD_SET_INV_PARAM = 0x64
CMD_SET_INV_FORMAT = 0x6A
CMD_GET_INV_FORMAT = 0x6B
CMD_RUN_INVENTORY = 0x6D
CMD_TAG_READ = 0x70
MC_UNKNOWN = 0xFF     # 命令中沒有對應 MC 時,回應的 MC

# Inventory Data Format bitmask (Table 33) —— 決定 tag 通知帶哪些欄位
DF_ANTENNA = 0x01     # 1 byte
DF_CHANNEL = 0x02     # 3 bytes, kHz
DF_TIMESTAMP = 0x04   # 4 bytes, us
DF_RSSI = 0x08        # 2 bytes, 0.01 dBm (有號)
DF_PHASE = 0x10       # 2 bytes, 0.087 度
DF_TID = 0x20         # 1 byte len + variable
DF_DEFAULT = 0x1F     # 以上除 TID 全開

# RF Mode ID (§2.3.1) —— 數字越小通常越快、靈敏度越低
RF_ULTRA_FAST = 103   # 1000+ tags/s, -68 dBm
RF_FAST = 302         #  800+ tags/s, -68 dBm
RF_NORMAL = 345       #  400+ tags/s, -74 dBm
RF_ULTRA_SENSITIVE = 285  # 50+ tags/s, -83 dBm

STATUS_OK = 0x00
STATUS_BUSY = 0x04

# Table 34 (Status) + Table 35 (Error)
_STATUS_TEXT: Dict[int, str] = {
    0x00: "STATUS_OK", 0x01: "STATUS_STOP_CONDITION", 0x02: "STATUS_INVENTORY_TIMEOUT",
    0x03: "STATUS_ABORT", 0x04: "STATUS_BUSY", 0x05: "STATUS_ACCESS_NO_TAG",
    0x06: "STATUS_ACCESS_FAIL",

    0x10: "ERR_ABORT", 0x11: "ERR_ANTENNA_NOT_ENABLE", 0x12: "ERR_OP_STATUS",
    0x13: "ERR_INVENTORY_INTERNAL_PACKET", 0x14: "ERR_RF_FRONT_END",
    0x15: "ERR_SEQUENCE_GENERATOR", 0x16: "ERR_ACCESS_TIMEOUT",
    0x17: "ERR_HANDLE_NOT_MATCH", 0x18: "ERR_TEMPERATURE_HIGH",

    0x40: "ERR_PROTOCOL_PREAMBLE", 0x41: "ERR_PROTOCOL_MSG_TYPE",
    0x42: "ERR_PROTOCOL_MSG_CODE", 0x43: "ERR_PROTOCOL_PAYLOAD_LENGTH",
    0x44: "ERR_PROTOCOL_CHECKSUM", 0x45: "ERR_PROTOCOL_END_MARK",
    0x46: "ERR_PROTOCOL_MSG_TIMEOUT", 0x47: "ERR_PROTOCOL_BUFFER_FULL",

    0x48: "ERR_PARAMETER_LENGTH", 0x49: "ERR_PARAMETER_REGION",
    0x4A: "ERR_PARAMETER_ANTENNA_PORT", 0x4B: "ERR_PARAMETER_MASK",
    0x4C: "ERR_PARAMETER_ENABLE", 0x4D: "ERR_PARAMETER_PRIORITY",
    0x4E: "ERR_PARAMETER_RF_POWER", 0x4F: "ERR_PARAMETER_TIME",
    0x50: "ERR_PARAMETER_TIME_RUNS_ZERO", 0x51: "ERR_PARAMETER_TIME_RUNS_TAGS_ZERO",
    0x52: "ERR_PARAMETER_RF_MODE", 0x53: "ERR_PARAMETER_Q_OPTION",
    0x54: "ERR_PARAMETER_TARGET", 0x55: "ERR_PARAMETER_FAST_ID",
    0x56: "ERR_PARAMETER_SELECT_NUMBER", 0x57: "ERR_PARAMETER_TRUNCATE",
    0x58: "ERR_PARAMETER_BIT_ADDRESS", 0x59: "ERR_PARAMETER_BIT_LENGTH_MASK",
    0x5A: "ERR_PARAMETER_RESPONSE_OPTION", 0x5B: "ERR_PARAMETER_MEMORY_BANK",
    0x5C: "ERR_PARAMETER_WORD_ADDRESS", 0x5D: "ERR_PARAMETER_WORD_LENGTH",
    0x5E: "ERR_PARAMETER_WORD_DATA", 0x5F: "ERR_PARAMETER_KILL_PASSWORD_ZERO",
    0x60: "ERR_PARAMETER_LOCK_MASK", 0x61: "ERR_PARAMETER_LOCK_ACTION",

    0x90: "ERR_CONFIG_USER_ERASE", 0xA0: "ERR_EMBEDED", 0xFF: "ERR_UNKNOWN",
}


def status_text(code: int) -> str:
    return _STATUS_TEXT.get(code, f"UNDEFINED(0x{code:02X})")


class RfidError(Exception):
    """讀取器回傳非 OK 的 status code。"""

    def __init__(self, status: int, message: str = ""):
        self.status = status
        super().__init__(message or f"讀取器回傳 {status_text(status)}")


# =============================================================== 封包


def checksum(msg_type: int, msg_code: int, payload: bytes) -> int:
    """§1.2.5  XOR(MT, MC, PL, Payload...)"""
    cs = msg_type ^ msg_code ^ len(payload)
    for b in payload:
        cs ^= b
    return cs & 0xFF


@dataclass
class Packet:
    msg_type: int
    msg_code: int
    payload: bytes = b""

    def __post_init__(self):
        if len(self.payload) > 255:
            raise ValueError(f"Payload 長度 {len(self.payload)} 超過 255 (PL 欄位僅 1 byte)")

    @property
    def status(self) -> int:
        """回應封包的第一個 payload byte 是 Status Code。"""
        return self.payload[0] if self.payload else 0xFF

    @property
    def is_text(self) -> bool:
        """是不是 GPIO 用的 ASCII 文字框 (0x0A '@' … CRLF)。"""
        return self.msg_type == MT_TEXT

    @property
    def text(self) -> str:
        """文字框的內文(不含 '@' 與 CRLF)。"""
        return self.payload.decode("ascii", "replace")

    @classmethod
    def text_frame(cls, text: str) -> "Packet":
        """組一個 ASCII 文字框,例如 Packet.text_frame("InputPort")。"""
        return cls(MT_TEXT, 0, text.encode("ascii"))

    def to_bytes(self) -> bytes:
        if self.msg_type == MT_TEXT:
            # 文字框沒有 MC、沒有長度、沒有 checksum
            return bytes([PREAMBLE, MT_TEXT]) + self.payload + END_MARK
        return (bytes([PREAMBLE, self.msg_type, self.msg_code, len(self.payload)])
                + self.payload
                + bytes([checksum(self.msg_type, self.msg_code, self.payload)])
                + END_MARK)

    def __str__(self) -> str:
        if self.msg_type == MT_TEXT:
            return f"TXT @{self.text}"
        kind = {MT_COMMAND: "CMD", MT_RESPONSE: "RSP", MT_NOTIFICATION: "NTF"}
        return (f"{kind.get(self.msg_type, '???')} MC=0x{self.msg_code:02X} "
                f"PL={len(self.payload)} [{self.payload.hex(' ').upper()}]")


class PacketParser:
    """
    串流解析器:把任意切割的位元組餵進來,吐出 checksum 正確的封包。
    遇到雜訊會自動重新同步。非執行緒安全 —— 只在 RX 執行緒使用。
    """

    def __init__(self):
        self._buf = bytearray()
        self.error_count = 0

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> List[Packet]:
        self._buf += data
        out: List[Packet] = []

        while True:
            start = self._buf.find(PREAMBLE)
            if start < 0:
                self._buf.clear()
                break
            if start > 0:
                del self._buf[:start]
            if len(self._buf) < 2:
                break

            # ---- GPIO 的 ASCII 文字框: 0x0A '@' <文字> 0x0D 0x0A ----
            # 沒有長度也沒有 checksum,只能靠 CRLF 收尾 + 「內文必須是可列印
            # ASCII」來當防呆,免得雜訊裡的 0x0A 0x40 把解析器帶進死巷。
            if self._buf[1] == MT_TEXT:
                consumed = self._try_text_frame(out)
                if consumed is None:
                    break                       # 資料還沒收完,等下一批
                continue

            if len(self._buf) < 4:
                break

            mt, mc, pl = self._buf[1], self._buf[2], self._buf[3]

            # MT 只可能是 0x80/0x81/0x82。先擋掉不合法的,避免假 Preamble
            # 讀到一個很大的 PL,害解析器一直等「永遠不會來」的資料而卡死。
            if mt not in MT_ALL:
                self.error_count += 1
                del self._buf[0]
                continue

            total = OVERHEAD + pl
            if len(self._buf) < total:
                break                       # 還沒收完,等下一批

            payload = bytes(self._buf[4:4 + pl])
            if (bytes(self._buf[5 + pl:7 + pl]) == END_MARK
                    and self._buf[4 + pl] == checksum(mt, mc, payload)):
                out.append(Packet(mt, mc, payload))
                del self._buf[:total]
            else:
                self.error_count += 1       # 假 Preamble,丟掉再往後找
                del self._buf[0]

        if len(self._buf) > 65536:
            self._buf.clear()
        return out

    @staticmethod
    def _is_printable(data: bytes) -> bool:
        return all(0x20 <= b <= 0x7E for b in data)

    def _try_text_frame(self, out: List[Packet]) -> Optional[bool]:
        """
        嘗試從 buffer 開頭切出一個文字框。

        回傳 True  = 已產出一個文字框(或丟掉 1 byte 重新同步),可以繼續掃
        回傳 None  = 資料還沒收完,等下一批
        """
        limit = min(len(self._buf), 2 + MAX_TEXT_LEN + len(END_MARK))
        idx = self._buf.find(END_MARK, 2, limit)

        if idx >= 0:
            body = bytes(self._buf[2:idx])
            if body and self._is_printable(body):
                out.append(Packet(MT_TEXT, 0, body))
                del self._buf[:idx + len(END_MARK)]
            else:
                self.error_count += 1           # 空的或含控制字元 → 不是文字框
                del self._buf[0]
            return True

        # 還沒看到完整 CRLF。資料剛好切在 CR 與 LF 中間時,尾端這個 0x0D 是
        # 結束符的前半,不是內文 —— 不先剝掉會被當成非法控制字元而整框丟掉。
        region = bytes(self._buf[2:limit])
        if region.endswith(END_MARK[:1]):
            region = region[:-1]

        # 內文出現非可列印字元,或長度超過上限 → 判定不是文字框,重新同步
        if (not self._is_printable(region)
                or len(self._buf) >= 2 + MAX_TEXT_LEN + len(END_MARK)):
            self.error_count += 1
            del self._buf[0]
            return True

        return None


# =============================================================== Tag


@dataclass
class TagReport:
    """
    一次 tag 讀取事件 —— Tag Response Data (MT=0x82, MC=0x6D)。§2.3.7.2

    欄位順序由 payload 第 1 個 byte 的 bitmask 決定:
      Bitmask(1) → ANT(1) → RFCh(3) → Timestamp(4) → RSSI(2) → Phase(2)
      → [TID Len(1) + TID] → Inventory Len(1) + Inventory Data(PC + EPC)
    """
    fmt: int = 0
    antenna_id: int = 0
    rf_channel_khz: int = 0
    timestamp_us: int = 0
    rssi_dbm: float = 0.0      # 原始 0.01 dBm,有號
    phase_deg: float = 0.0     # 原始 0.087 度
    tid: bytes = b""
    pc: int = 0                # Protocol Control word
    epc: bytes = b""

    @property
    def epc_hex(self) -> str:
        return self.epc.hex().upper()

    @property
    def tid_hex(self) -> str:
        return self.tid.hex().upper()

    @classmethod
    def parse(cls, p: bytes) -> Optional["TagReport"]:
        """格式不符一律回 None,不丟例外 —— 收到雜訊不該弄死 RX 執行緒。"""
        if not p or len(p) < 2:
            return None

        i = 0
        t = cls()
        t.fmt = p[i]; i += 1

        if t.fmt & DF_ANTENNA:
            if i + 1 > len(p):
                return None
            t.antenna_id = p[i]; i += 1
        if t.fmt & DF_CHANNEL:
            if i + 3 > len(p):
                return None
            t.rf_channel_khz = int.from_bytes(p[i:i + 3], "big"); i += 3
        if t.fmt & DF_TIMESTAMP:
            if i + 4 > len(p):
                return None
            t.timestamp_us = int.from_bytes(p[i:i + 4], "big"); i += 4
        if t.fmt & DF_RSSI:
            if i + 2 > len(p):
                return None
            # 有號!RSSI 為負值,用無號解析會變成 +600 多
            t.rssi_dbm = struct.unpack_from(">h", p, i)[0] / 100.0; i += 2
        if t.fmt & DF_PHASE:
            if i + 2 > len(p):
                return None
            t.phase_deg = struct.unpack_from(">H", p, i)[0] * 0.087; i += 2
        if t.fmt & DF_TID:
            if i + 1 > len(p):
                return None
            n = p[i]; i += 1
            if i + n > len(p):
                return None
            t.tid = p[i:i + n]; i += n

        if i + 1 > len(p):
            return None
        inv_len = p[i]; i += 1
        if inv_len < 2 or i + inv_len > len(p):
            return None

        t.pc = int.from_bytes(p[i:i + 2], "big")
        t.epc = p[i + 2:i + inv_len]
        return t

    @staticmethod
    def build(epc: bytes, fmt: int = DF_DEFAULT, antenna_id: int = 1,
              rf_channel_khz: int = 915000, timestamp_us: int = 0,
              rssi_dbm: float = -55.0, phase_deg: float = 0.0,
              pc: int = 0x3000, tid: bytes = b"") -> bytes:
        """組出一個 Tag Response Data payload —— 測試與模擬器用。"""
        out = bytearray([fmt])
        if fmt & DF_ANTENNA:
            out.append(antenna_id & 0xFF)
        if fmt & DF_CHANNEL:
            out += rf_channel_khz.to_bytes(3, "big")
        if fmt & DF_TIMESTAMP:
            out += (timestamp_us & 0xFFFFFFFF).to_bytes(4, "big")
        if fmt & DF_RSSI:
            out += struct.pack(">h", int(round(rssi_dbm * 100)))
        if fmt & DF_PHASE:
            out += struct.pack(">H", int(round(phase_deg / 0.087)) & 0xFFFF)
        if fmt & DF_TID:
            out.append(len(tid))
            out += tid
        out.append(len(epc) + 2)
        out += pc.to_bytes(2, "big")
        out += epc
        return bytes(out)


@dataclass
class TagRecord:
    """同一個 EPC 的彙總統計。"""
    epc: str = ""
    tid: str = ""
    pc: int = 0
    antenna: int = 0
    count: int = 0                      # 被讀到幾次
    rssi: float = 0.0                   # 最後一次的 RSSI (dBm)
    max_rssi: float = float("-inf")
    channel_khz: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0


class TagStore:
    """執行緒安全的 tag 統計。寫入端 = RX 執行緒,讀取端 = 你的主執行緒。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._map: Dict[str, TagRecord] = {}
        self._total = 0

    @property
    def unique_count(self) -> int:
        with self._lock:
            return len(self._map)

    @property
    def total_reads(self) -> int:
        with self._lock:
            return self._total

    def add(self, t: TagReport) -> None:
        if not t or not t.epc:
            return
        epc = t.epc_hex
        now = time.time()
        with self._lock:
            r = self._map.get(epc)
            if r is None:
                r = TagRecord(epc=epc, first_seen=now)
                self._map[epc] = r
            r.count += 1
            r.last_seen = now
            r.pc = t.pc
            r.antenna = t.antenna_id
            r.rssi = t.rssi_dbm
            r.max_rssi = max(r.max_rssi, t.rssi_dbm)
            r.channel_khz = t.rf_channel_khz
            if t.tid:
                r.tid = t.tid_hex
            self._total += 1

    def clear(self) -> None:
        with self._lock:
            self._map.clear()
            self._total = 0

    def snapshot(self, sort_by: str = "count", desc: bool = True) -> List[TagRecord]:
        import copy
        with self._lock:
            items = [copy.copy(r) for r in self._map.values()]
        items.sort(key=lambda r: getattr(r, sort_by), reverse=desc)
        return items


# =============================================================== TCP


class _Tcp:
    """極簡 TCP 連線 —— read() 逾時回 b"",連線中斷才丟例外。"""

    def __init__(self, host: str, port: int,
                 connect_timeout: float = 3.0, read_timeout: float = 0.2):
        self.host, self.port = host, port
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout
        self._sock: Optional[socket.socket] = None

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def open(self) -> None:
        if self._sock is not None:
            return
        s = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(self.read_timeout)
        self._sock = s

    def close(self) -> None:
        s, self._sock = self._sock, None
        if s is None:
            return
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            s.close()
        except OSError:
            pass

    def write(self, data: bytes) -> None:
        if self._sock is None:
            raise ConnectionError("TCP 未連線")
        self._sock.sendall(data)

    def read(self, n: int = 8192) -> bytes:
        if self._sock is None:
            raise ConnectionError("TCP 未連線")
        try:
            data = self._sock.recv(n)
        except (socket.timeout, TimeoutError):
            return b""
        except OSError as ex:
            raise ConnectionError(f"TCP 讀取失敗: {ex}") from ex
        if not data:
            raise ConnectionError("對方關閉連線")
        return data


# =============================================================== Reader


class RfidReader:
    """
    MPK-R-9504 讀取器 (TCP)。

    執行緒模型
    ----------
    * open() 會啟動一條 daemon RX 執行緒。
    * on_tag / on_log / on_inventory_finished 都在 **RX 執行緒** 上被呼叫,
      不是你的主執行緒。要更新 GUI 請自行 marshal
      (tkinter 用 after,PyQt 用 signal)。
      最省事的作法是 `reader.on_tag = store.add`,主執行緒定期 snapshot()。
    * 同一時間只允許一個命令在飛行中。
    * start_inventory() 送出後「不等待」回應 —— tag 通知會先湧入,
      結束時才觸發 on_inventory_finished。
    """

    def __init__(self, host: str, port: int, connect_timeout: float = 3.0):
        self._tcp = _Tcp(host, port, connect_timeout)
        self._parser = PacketParser()
        self._cmd_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._rx: Optional[threading.Thread] = None
        self._running = False

        self._pending_code: Optional[int] = None
        self._pending_packet: Optional[Packet] = None
        self._pending_event = threading.Event()
        self._inventory_running = False

        # ---- GPIO input 狀態 ----
        self._gpio_lock = threading.Lock()
        self._gpio: Dict[int, int] = {}          # pin(1起算) → 0/1
        self._gpio_at: float = 0.0               # 最後一次收到 GPIO 訊息的時間
        self._gpio_seen: set = set()
        self._gpio_expect: Optional[int] = None
        self._gpio_event = threading.Event()

        # ---- callbacks (在 RX 執行緒觸發) ----
        self.on_tag: Optional[Callable[[TagReport], None]] = None
        self.on_inventory_finished: Optional[Callable[[int, int, int], None]] = None
        self.on_gpio: Optional[Callable[[int, int, Optional[int]], None]] = None  # (pin, level, prev)
        self.on_text: Optional[Callable[[str], None]] = None      # 看不懂的文字框
        self.on_log: Optional[Callable[[str], None]] = None
        self.on_packet: Optional[Callable[[Packet, bool], None]] = None  # (packet, 是否為送出)

    # ---------------------------------------------------------- 連線

    @property
    def description(self) -> str:
        return f"{self._tcp.host}:{self._tcp.port}"

    @property
    def is_open(self) -> bool:
        return self._running and self._tcp.is_open

    @property
    def is_inventory_running(self) -> bool:
        return self._inventory_running

    @property
    def parser_error_count(self) -> int:
        return self._parser.error_count

    def open(self) -> None:
        if self._running:
            raise RuntimeError("已經連線")
        self._parser.reset()
        self._tcp.open()
        self._running = True
        self._rx = threading.Thread(target=self._rx_loop, name="RfidReader.Rx", daemon=True)
        self._rx.start()
        self._log(f"已連線: {self.description}")

    def close(self) -> None:
        self._running = False
        t, self._rx = self._rx, None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._tcp.close()
        self._inventory_running = False
        self._pending_packet = None
        self._pending_event.set()          # 叫醒還在等的呼叫端
        self._log("已中斷連線")

    def __enter__(self) -> "RfidReader":
        self.open()
        return self

    def __exit__(self, *exc):
        try:
            if self._inventory_running:
                self.abort()
        except Exception:
            pass
        self.close()
        return False

    # ---------------------------------------------------------- RX

    def _rx_loop(self) -> None:
        try:
            while self._running:
                data = self._tcp.read()
                if not data:
                    continue
                for pkt in self._parser.feed(data):
                    try:
                        self._dispatch(pkt)
                    except Exception as ex:   # callback 出錯不該弄死 RX 執行緒
                        self._log(f"處理封包時發生例外: {ex!r}  ({pkt})")
        except Exception as ex:
            if self._running:
                self._running = False
                self._log(f"接收執行緒結束: {ex}")
                self._pending_event.set()

    def _dispatch(self, p: Packet) -> None:
        if self.on_packet:
            self.on_packet(p, False)

        if p.is_text:
            self._handle_text(p.text)
            return

        if p.msg_type == MT_NOTIFICATION:
            if p.msg_code == CMD_RUN_INVENTORY:
                tag = TagReport.parse(p.payload)
                if tag is None:
                    self._log(f"Tag 通知解析失敗: {p}")
                elif self.on_tag:
                    self.on_tag(tag)
            else:
                self._log(f"其他通知: {p}")
            return

        if p.msg_type != MT_RESPONSE:
            return

        pending = self._pending_code

        # 0x6D 的回應 = 盤點結束,它是「非同步」送達的:很可能在我們正在等
        # 別的命令回應時插進來。實機在收到 Abort 後,通常是先送這個結束回應、
        # 再送 Abort 的 ack,所以這裡不能因為「正在等 0x10」就把它丟掉。
        # (只有呼叫端真的自己 send_command(0x6D) 時才讓它走一般配對流程)
        if p.msg_code == CMD_RUN_INVENTORY and pending != CMD_RUN_INVENTORY:
            self._inventory_finished(p)
            return

        if pending is not None and p.msg_code in (pending, MC_UNKNOWN):
            self._pending_packet = p
            self._pending_event.set()
        else:
            self._log(f"未預期的回應: {p}")

    def _inventory_finished(self, p: Packet) -> None:
        """
        處理盤點結束回應 (0x81 / 0x6D)。§2.3.7.3 有兩種 payload 形狀:

          正常: [Status 1][Total Singulations 4][Time elapsed 4]      → PL=9
          錯誤/中止: [Status 1][ANT_ID 1][Message 變動長度]           → PL>=2

        按下 Abort 後收到的通常是後者,例如 [03 01] = STATUS_ABORT, ANT 1。
        """
        self._inventory_running = False
        total = elapsed = 0
        detail = ""

        if len(p.payload) >= 9:
            total = int.from_bytes(p.payload[1:5], "big")
            elapsed = int.from_bytes(p.payload[5:9], "big")
            detail = f"總 singulation={total}, 耗時={elapsed} ms"
        elif len(p.payload) >= 2:
            detail = f"ANT={p.payload[1]}"
            if len(p.payload) > 2:
                detail += f", msg={p.payload[2:].hex(' ').upper()}"

        self._log(f"盤點結束: {status_text(p.status)}"
                  + (f"  ({detail})" if detail else ""))
        if self.on_inventory_finished:
            self.on_inventory_finished(p.status, total, elapsed)

    def _handle_text(self, text: str) -> None:
        """處理 ASCII 文字框(GPIO 走這條)。在 RX 執行緒上被呼叫。"""
        hit = parse_gpio_text(text)
        if hit is None:
            self._log(f"文字框: @{text}")
            if self.on_text:
                self.on_text(text)
            return

        pin, level = hit
        with self._gpio_lock:
            prev = self._gpio.get(pin)
            self._gpio[pin] = level
            self._gpio_at = time.time()
            self._gpio_seen.add(pin)
            expect = self._gpio_expect
            done = expect is not None and len(self._gpio_seen) >= expect
        if done:
            self._gpio_event.set()
        if prev != level and self.on_gpio:
            self.on_gpio(pin, level, prev)

    # ---------------------------------------------------------- GPIO

    @property
    def gpio(self) -> Dict[int, int]:
        """目前已知的 GPIO input 狀態 {pin: 0/1}(快照,可安全讀取)。"""
        with self._gpio_lock:
            return dict(self._gpio)

    @property
    def gpio_updated_at(self) -> float:
        """最後一次收到 GPIO 訊息的時間 (time.time());沒收過是 0。"""
        with self._gpio_lock:
            return self._gpio_at

    def request_gpio(self) -> bool:
        """
        送出 GPIO 查詢 (`@InputPort`) 但**不等待**回覆 —— 狀態會由 RX 執行緒
        在收到 `@Input PinN,V` 時自動更新,讀 `reader.gpio` 即可。

        給輪詢用:不會阻塞,命令通道忙碌時直接跳過這次(回 False)。
        """
        if not self._cmd_lock.acquire(timeout=0.05):
            return False
        try:
            self._write(Packet.text_frame(TEXT_CMD_INPUT_PORT))
            return True
        finally:
            self._cmd_lock.release()

    def query_gpio(self, timeout: float = 1.0,
                   expect: int = GPIO_PIN_COUNT) -> Dict[int, int]:
        """
        送出 `@InputPort` 並等所有 pin 回報完(或逾時),回傳 {pin: 0/1}。

        文字框沒有 Message Code 可以配對,所以這裡是「等收滿 expect 個 pin」,
        逾時就把當下已知的狀態回傳(不丟例外)。
        """
        with self._cmd_lock:
            with self._gpio_lock:
                self._gpio_seen = set()
                self._gpio_expect = expect
            self._gpio_event.clear()
            try:
                self._write(Packet.text_frame(TEXT_CMD_INPUT_PORT))
                self._gpio_event.wait(timeout)
            finally:
                with self._gpio_lock:
                    self._gpio_expect = None
        return self.gpio

    # ---------------------------------------------------------- 命令

    def _write(self, p: Packet) -> None:
        if not self._tcp.is_open:
            raise ConnectionError("尚未連線")
        with self._write_lock:
            self._tcp.write(p.to_bytes())
        if self.on_packet:
            self.on_packet(p, True)

    def send_command(self, msg_code: int, payload: bytes = b"",
                     timeout: float = 3.0) -> Packet:
        """送出命令並等待對應的 Response。"""
        with self._cmd_lock:
            self._pending_packet = None
            self._pending_code = msg_code
            self._pending_event.clear()
            try:
                self._write(Packet(MT_COMMAND, msg_code, payload))
                if not self._pending_event.wait(timeout):
                    raise TimeoutError(f"命令 0x{msg_code:02X} 等待回應逾時 ({timeout}s)")
                pkt = self._pending_packet
                if pkt is None:
                    raise ConnectionError("等待回應時連線中斷")
                return pkt
            finally:
                self._pending_code = None
                self._pending_packet = None

    def send_checked(self, msg_code: int, payload: bytes = b"",
                     timeout: float = 3.0) -> Packet:
        """送出命令並檢查 Status Code,非 OK 就丟 RfidError。"""
        r = self.send_command(msg_code, payload, timeout)
        if r.status != STATUS_OK:
            raise RfidError(r.status, f"命令 0x{msg_code:02X} 失敗: {status_text(r.status)}")
        return r

    # ------------------------------------------- 讀取器資訊

    def get_firmware_version(self) -> str:
        d = self.send_checked(CMD_GET_FIRMWARE).payload[1:]
        if not d:
            return "(空)"
        # 欄位格式原廠文件未細列;可印成 ASCII 就印 ASCII,否則印 hex
        try:
            s = d.decode("ascii").strip("\x00 ")
            if s and all(0x20 <= ord(c) <= 0x7E for c in s):
                return s
        except UnicodeDecodeError:
            pass
        return d.hex(" ").upper()

    def get_temperature(self) -> float:
        """回傳攝氏度。欄位長度依實機可能不同,已做容錯。"""
        d = self.send_checked(CMD_GET_TEMPERATURE).payload
        if len(d) >= 3:
            return float(struct.unpack_from(">h", d, 1)[0])
        if len(d) >= 2:
            return float(struct.unpack_from(">b", d, 1)[0])
        return float("nan")

    # ------------------------------------------- 盤點設定

    def set_data_format(self, fmt: int = DF_DEFAULT) -> None:
        """0x6A —— 決定 tag 通知要帶哪些欄位 (DF_* 旗標的 OR)。"""
        self.send_checked(CMD_SET_INV_FORMAT, bytes([fmt & 0xFF]))

    def get_data_format(self) -> int:
        d = self.send_checked(CMD_GET_INV_FORMAT).payload
        return d[1] if len(d) >= 2 else 0

    @staticmethod
    def build_inventory_parameter(rf_mode: int = RF_ULTRA_FAST,
                                  initial_q: int = 8, max_q: int = 15,
                                  min_q: int = 0, num_min_q_cycles: int = 1,
                                  fixed_q: bool = False,
                                  sel: int = 0, session: int = 0,
                                  target: int = 0, dual_target: bool = False,
                                  fast_tid: bool = False,
                                  max_queries_since_valid_epc: int = 16) -> bytes:
        """§2.3.1 Set Inventory parameter (0x64) 的 payload,bitmask 固定 0x3F。"""
        inv_target = (((sel & 0x03) << 6) | ((session & 0x03) << 4)
                      | ((target & 0x01) << 3) | ((1 if dual_target else 0) << 2))
        return (b"\x3f"
                + rf_mode.to_bytes(2, "big")
                + bytes([((initial_q & 0x0F) << 4) | (max_q & 0x0F),
                         ((min_q & 0x0F) << 4) | (num_min_q_cycles & 0x0F),
                         0x80 if fixed_q else 0x00,
                         inv_target,
                         0x01 if fast_tid else 0x00,
                         max_queries_since_valid_epc & 0xFF]))

    def set_inventory_parameter(self, **kwargs) -> None:
        """0x64 —— 設定 RF Mode / Q / Session / Target 等盤點參數。"""
        self.send_checked(CMD_SET_INV_PARAM, self.build_inventory_parameter(**kwargs))

    # ------------------------------------------- 盤點

    @staticmethod
    def build_run_inventory(antenna: int = 1, rf_mode: int = RF_ULTRA_FAST,
                            power_dbm: float = 20.0,
                            time_ms: int = 0, runs: int = 0) -> bytes:
        """§2.3.7 Run Inventory Custom Command (0x6D) 的 13-byte payload。"""
        return (bytes([antenna & 0xFF])
                + rf_mode.to_bytes(2, "big")
                + int(round(power_dbm * 100)).to_bytes(2, "big")   # 0.01 dBm
                + (time_ms & 0xFFFFFFFF).to_bytes(4, "big")
                + (runs & 0xFFFFFFFF).to_bytes(4, "big"))

    def start_inventory(self, antenna: int = 1, rf_mode: int = RF_ULTRA_FAST,
                        power_dbm: float = 20.0,
                        time_ms: int = 0, runs: int = 0) -> None:
        """
        開始盤點 (0x6D)。time_ms=0 且 runs=0 → 連續模式,要用 abort() 停止。

        不等待回應 —— tag 通知會先湧入,結束時才觸發 on_inventory_finished。
        """
        if self._inventory_running:
            raise RuntimeError("Inventory 已在執行中")
        payload = self.build_run_inventory(antenna, rf_mode, power_dbm, time_ms, runs)
        self._inventory_running = True
        try:
            with self._cmd_lock:
                self._write(Packet(MT_COMMAND, CMD_RUN_INVENTORY, payload))
        except Exception:
            self._inventory_running = False
            raise
        mode = "連續模式" if (time_ms == 0 and runs == 0) else "限時/限次"
        self._log(f"Inventory 開始 (ANT={antenna}, RFMode={rf_mode}, "
                  f"{power_dbm:.1f} dBm, {mode})")

    def abort(self, timeout: float = 3.0) -> Packet:
        """0x10 —— 停止進行中的盤點。"""
        try:
            return self.send_command(CMD_ABORT, timeout=timeout)
        finally:
            self._inventory_running = False

    def read_tags(self, seconds: float = 5.0, antenna: int = 1,
                  rf_mode: int = RF_ULTRA_FAST, power_dbm: float = 20.0,
                  data_format: int = DF_DEFAULT,
                  store: Optional[TagStore] = None) -> List[TagRecord]:
        """
        阻塞式:盤點 `seconds` 秒後停止,回傳彙總後的 tag 清單。

        會覆寫 on_tag —— 需要即時處理請改用 start_inventory() + on_tag。
        """
        store = store or TagStore()
        self.set_data_format(data_format)
        self.set_inventory_parameter(rf_mode=rf_mode)
        self.on_tag = store.add
        self.start_inventory(antenna=antenna, rf_mode=rf_mode, power_dbm=power_dbm)
        try:
            time.sleep(seconds)
        finally:
            try:
                self.abort()
            except Exception:
                pass
            time.sleep(0.2)      # 等最後的通知收完
        return store.snapshot()

    # ------------------------------------------- 讀 tag 記憶體

    def tag_read(self, antenna: int = 1, memory_bank: int = 3,
                 word_address: int = 0, word_length: int = 2,
                 access_password: int = 0, response_option: int = 0x01,
                 timeout: float = 5.0) -> Packet:
        """
        §2.4.1 Tag Read Command (0x70) —— 讀取單一 tag 的記憶體。
        memory_bank: 0=RFU/Reserved, 1=EPC, 2=TID, 3=USER
        """
        payload = (bytes([antenna & 0xFF, response_option & 0xFF])
                   + access_password.to_bytes(4, "big")
                   + bytes([memory_bank & 0xFF])
                   + word_address.to_bytes(2, "big")
                   + bytes([word_length & 0xFF]))
        return self.send_command(CMD_TAG_READ, payload, timeout)

    # -------------------------------------------

    def _log(self, msg: str) -> None:
        if self.on_log:
            try:
                self.on_log(msg)
            except Exception:
                pass


# =============================================================== 便利函式


def scan(host: str, port: int, seconds: float = 5.0,
         antenna: int = 1, rf_mode: int = RF_ULTRA_FAST,
         power_dbm: float = 20.0,
         on_log: Optional[Callable[[str], None]] = None) -> List[TagRecord]:
    """
    最簡單的用法:連線 → 盤點 `seconds` 秒 → 關閉 → 回傳 tag 清單。

        for t in scan("192.168.1.200", 8888, seconds=5):
            print(t.epc, t.count, t.rssi)
    """
    with RfidReader(host, port) as reader:
        reader.on_log = on_log
        return reader.read_tags(seconds=seconds, antenna=antenna,
                                rf_mode=rf_mode, power_dbm=power_dbm)


# =============================================================== CLI


def _main() -> int:
    ap = argparse.ArgumentParser(description="MPK-R-9504 RFID 盤點")
    ap.add_argument("--host", default="192.168.1.200")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--antenna", type=int, default=1)
    ap.add_argument("--power", type=float, default=20.0, help="RF 功率 dBm")
    ap.add_argument("--rf-mode", type=int, default=RF_ULTRA_FAST)
    ap.add_argument("--sim", action="store_true", help="就地啟動模擬器並連上它")
    ap.add_argument("--trace", action="store_true", help="印出封包 hex")
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
        print(f"[模擬器] 已啟動於 {host}:{port}\n")

    def log(msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    store = TagStore()
    try:
        with RfidReader(host, port) as reader:
            reader.on_log = log
            if args.trace:
                reader.on_packet = lambda p, out: log(("→ " if out else "← ") + str(p))

            log(f"韌體版本: {reader.get_firmware_version()}")

            reader.set_data_format(DF_DEFAULT)
            reader.set_inventory_parameter(rf_mode=args.rf_mode)
            reader.on_tag = store.add
            reader.start_inventory(antenna=args.antenna, rf_mode=args.rf_mode,
                                   power_dbm=args.power)

            deadline = time.time() + args.seconds
            last = 0
            while time.time() < deadline:
                time.sleep(1.0)
                total = store.total_reads
                log(f"唯一 Tag={store.unique_count}  總讀取={total}  速率={total - last}/s")
                last = total
            reader.abort()
            time.sleep(0.3)
            errors = reader.parser_error_count
    except KeyboardInterrupt:
        print("\n中斷", flush=True)
        errors = 0
    except Exception as ex:
        print(f"錯誤: {ex}", file=sys.stderr)
        if sim:
            sim.stop()
        return 1
    finally:
        if sim:
            sim.stop()

    rows = store.snapshot()
    print()
    print(f"{'#':>3}  {'EPC':<26} {'PC':>5} {'次數':>6} {'ANT':>4} "
          f"{'RSSI':>8} {'MaxRSSI':>8}  {'通道(kHz)':>10}")
    print("-" * 82)
    for i, r in enumerate(rows, 1):
        print(f"{i:>3}  {r.epc:<26} {r.pc:04X} {r.count:>6} {r.antenna:>4} "
              f"{r.rssi:>8.2f} {r.max_rssi:>8.2f}  {r.channel_khz:>10}")
    print("-" * 82)
    print(f"唯一 Tag: {store.unique_count}   總讀取次數: {store.total_reads}   "
          f"封包錯誤: {errors}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
