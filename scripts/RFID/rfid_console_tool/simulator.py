#!/usr/bin/env python3
"""
假的 MPK-R-9504 讀取器 —— TCP server,沒有實機時開發/測試用。

    python simulator.py --port 8888

不需要它的話直接刪掉,mpk_rfid.py 不依賴這個檔案 (只有 --sim 才會 import)。
"""

from __future__ import annotations

import argparse
import random
import socket
import threading
import time
from typing import List, Optional

from mpk_rfid import (
    CMD_ABORT,
    CMD_GET_FIRMWARE,
    CMD_GET_INV_FORMAT,
    CMD_GET_TEMPERATURE,
    CMD_RUN_INVENTORY,
    CMD_SET_INV_FORMAT,
    CMD_TAG_READ,
    DF_DEFAULT,
    MT_COMMAND,
    MT_NOTIFICATION,
    MT_RESPONSE,
    STATUS_BUSY,
    STATUS_OK,
    Packet,
    PacketParser,
    TagReport,
)

DEFAULT_EPCS = [
    "E28011700000020F00001234",
    "E28011700000020F00005678",
    "E28011700000020F0000ABCD",
    "3005FB63AC1F3681EC880468",
    "AAAA0000BBBB1111CCCC2222",
]


class ReaderSimulator:
    def __init__(self, host: str = "127.0.0.1", port: int = 8888,
                 epcs: Optional[List[str]] = None,
                 tag_rate: float = 200.0,
                 busy_during_inventory: bool = True,
                 firmware: str = "MPK-R-9504 v1.0.3"):
        self.host = host
        self.port = port
        self.epcs = [bytes.fromhex(e) for e in (epcs or DEFAULT_EPCS)]
        self.tag_rate = tag_rate
        self.busy_during_inventory = busy_during_inventory
        self.firmware = firmware
        self.verbose = False

        self._srv: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.actual_port = port

        self._conn: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._inv_stop = threading.Event()
        self._inv_thread: Optional[threading.Thread] = None
        self.data_format = DF_DEFAULT

    # ---------------------------------------------------------- 生命週期

    def start(self) -> "ReaderSimulator":
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self._srv.listen(1)
        self._srv.settimeout(0.3)
        self.actual_port = self._srv.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="Simulator")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._inv_stop.set()
        if self._srv:
            try:
                self._srv.close()
            except OSError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

    # ---------------------------------------------------------- 連線

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._srv.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            self._log(f"client 連入: {addr}")
            self._handle(conn)
            self._log("client 離線")

    def _handle(self, conn: socket.socket) -> None:
        self._conn = conn
        conn.settimeout(0.2)
        parser = PacketParser()
        try:
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except (socket.timeout, TimeoutError):
                    continue
                except OSError:
                    break
                if not data:
                    break
                for pkt in parser.feed(data):
                    self._on_command(pkt)
        finally:
            self._inv_stop.set()
            if self._inv_thread and self._inv_thread.is_alive():
                self._inv_thread.join(timeout=1.0)
            self._conn = None
            try:
                conn.close()
            except OSError:
                pass

    def _send(self, pkt: Packet) -> None:
        conn = self._conn
        if conn is None:
            return
        with self._send_lock:
            try:
                conn.sendall(pkt.to_bytes())
            except OSError:
                pass

    @property
    def _inventory_running(self) -> bool:
        return self._inv_thread is not None and self._inv_thread.is_alive()

    # ---------------------------------------------------------- 命令

    def _on_command(self, p: Packet) -> None:
        if self.verbose:
            self._log(f"收到 {p}")
        if p.msg_type != MT_COMMAND:
            return
        mc = p.msg_code

        # 盤點中對「非 Abort」命令回 BUSY —— 模擬實機常見行為
        if (self.busy_during_inventory and self._inventory_running
                and mc not in (CMD_ABORT, CMD_RUN_INVENTORY)):
            self._send(Packet(MT_RESPONSE, mc, bytes([STATUS_BUSY])))
            return

        if mc == CMD_GET_FIRMWARE:
            self._send(Packet(MT_RESPONSE, mc,
                              bytes([STATUS_OK]) + self.firmware.encode("ascii")))

        elif mc == CMD_GET_TEMPERATURE:
            temp = random.randint(35, 48)
            self._send(Packet(MT_RESPONSE, mc,
                              bytes([STATUS_OK]) + temp.to_bytes(2, "big")))

        elif mc == CMD_SET_INV_FORMAT:
            if p.payload:
                self.data_format = p.payload[0]
            self._send(Packet(MT_RESPONSE, mc, bytes([STATUS_OK])))

        elif mc == CMD_GET_INV_FORMAT:
            self._send(Packet(MT_RESPONSE, mc, bytes([STATUS_OK, self.data_format])))

        elif mc == CMD_RUN_INVENTORY:
            self._start_inventory(p.payload)

        elif mc == CMD_ABORT:
            self._inv_stop.set()
            self._send(Packet(MT_RESPONSE, mc, bytes([STATUS_OK])))

        elif mc == CMD_TAG_READ:
            epc = self.epcs[0]
            body = bytes([len(epc) + 2]) + (0x3000).to_bytes(2, "big") + epc
            self._send(Packet(MT_RESPONSE, mc,
                              bytes([STATUS_OK]) + body + b"\x11\x22\x33\x44"))

        else:
            self._send(Packet(MT_RESPONSE, mc, bytes([STATUS_OK])))

    def _start_inventory(self, payload: bytes) -> None:
        if self._inventory_running:
            self._send(Packet(MT_RESPONSE, CMD_RUN_INVENTORY, bytes([STATUS_BUSY])))
            return
        ant = payload[0] if payload else 1
        time_ms = int.from_bytes(payload[5:9], "big") if len(payload) >= 13 else 0
        runs = int.from_bytes(payload[9:13], "big") if len(payload) >= 13 else 0

        self._inv_stop.clear()
        self._inv_thread = threading.Thread(target=self._inventory_loop,
                                            args=(ant, time_ms, runs),
                                            daemon=True, name="SimInventory")
        self._inv_thread.start()

    def _inventory_loop(self, antenna: int, time_ms: int, runs: int) -> None:
        started = time.time()
        interval = 1.0 / max(1.0, self.tag_rate)
        singulations = 0
        status = STATUS_OK

        while not self._inv_stop.is_set():
            if time_ms and (time.time() - started) * 1000 >= time_ms:
                status = 0x02       # STATUS_INVENTORY_TIMEOUT
                break
            if runs and singulations >= runs:
                status = 0x01       # STATUS_STOP_CONDITION
                break

            payload = TagReport.build(
                epc=random.choice(self.epcs),
                fmt=self.data_format,
                antenna_id=antenna,
                rf_channel_khz=random.choice([915000, 917500, 920000, 922500]),
                timestamp_us=int((time.time() - started) * 1_000_000) & 0xFFFFFFFF,
                rssi_dbm=round(random.uniform(-72.0, -38.0), 2),
                phase_deg=round(random.uniform(0, 359), 1),
            )
            self._send(Packet(MT_NOTIFICATION, CMD_RUN_INVENTORY, payload))
            singulations += 1
            self._inv_stop.wait(interval)

        if self._inv_stop.is_set():
            status = 0x03           # STATUS_ABORT

        elapsed = int((time.time() - started) * 1000)
        self._send(Packet(MT_RESPONSE, CMD_RUN_INVENTORY,
                          bytes([status])
                          + singulations.to_bytes(4, "big")
                          + elapsed.to_bytes(4, "big")))

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[SIM] {msg}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="MPK-R-9504 讀取器模擬器 (TCP)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--rate", type=float, default=200.0, help="每秒送出的 tag 通知數")
    ap.add_argument("--no-busy", action="store_true",
                    help="盤點中不回 BUSY (模擬另一種實機行為)")
    args = ap.parse_args()

    sim = ReaderSimulator(args.host, args.port, tag_rate=args.rate,
                          busy_during_inventory=not args.no_busy)
    sim.verbose = True
    sim.start()
    print(f"模擬器啟動於 {args.host}:{sim.actual_port}  (Ctrl-C 結束)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()


if __name__ == "__main__":
    main()
