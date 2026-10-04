"""
mpk_rfid 測試 —— `python test_mpk_rfid.py` 或 `pytest -q` 都可以。

第 1 節的期望值全部取自 MPK-R-9504 Protocol User Guide v1.0 的範例封包。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mpk_rfid import (  # noqa: E402
    GPIO_PIN_COUNT,
    MT_TEXT,
    TEXT_CMD_INPUT_PORT,
    parse_gpio_text,
    CMD_ABORT,
    CMD_GET_FIRMWARE,
    CMD_RUN_INVENTORY,
    DF_ANTENNA,
    DF_DEFAULT,
    DF_RSSI,
    DF_TID,
    MT_COMMAND,
    MT_NOTIFICATION,
    MT_RESPONSE,
    RF_ULTRA_FAST,
    Packet,
    PacketParser,
    RfidReader,
    TagReport,
    TagStore,
    checksum,
    scan,
    status_text,
)
from simulator import ReaderSimulator  # noqa: E402


def h(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


# ---------------------------------------------- 1. checksum vs PDF 範例

PDF_EXAMPLES = [
    ("§2.3.1.1 Set Inventory parameter", MT_COMMAND, 0x64,
     "3F 00 67 8F 01 00 00 00 10", 0x2B),
    ("§2.3.1.2 Set Inventory parameter Rsp", MT_RESPONSE, 0x64, "00", 0xE4),
    ("§2.3.2.1 Get Inventory parameter", MT_COMMAND, 0x65, "3F", 0xDB),
    ("§2.3.3.1 Set Select parameter", MT_COMMAND, 0x66,
     "04 01 11 00 00 00 00 20 0A 11 11", 0xD3),
    ("§2.3.3.2 Set Select parameter Rsp", MT_RESPONSE, 0x66, "00", 0xE6),
    ("§2.3.4.1 Get Select parameter", MT_COMMAND, 0x67, "04", 0xE2),
    ("§2.3.4.2 Get Select parameter Rsp", MT_RESPONSE, 0x67,
     "00 04 01 31 00 00 00 00 20 0A 11 11", 0xF4),
    ("§2.3.5.1 Set Inventory Data Format", MT_COMMAND, 0x6A, "0F", 0xE4),
    ("§2.3.5.2 Set Inventory Data Format Rsp", MT_RESPONSE, 0x6A, "00", 0xEA),
    ("§2.3.7.1 Run Inventory Custom", MT_COMMAND, 0x6D,
     "01 00 67 07 D0 00 00 00 64 00 00 00 00", 0x35),
]


def test_checksum_matches_pdf_examples():
    for name, mt, mc, hexs, want in PDF_EXAMPLES:
        got = checksum(mt, mc, h(hexs))
        assert got == want, f"{name}: 0x{got:02X} != 0x{want:02X}"


def test_frame_layout_matches_pdf_examples():
    for name, mt, mc, hexs, want_cs in PDF_EXAMPLES:
        payload = h(hexs)
        f = Packet(mt, mc, payload).to_bytes()
        assert f[0] == 0x0A, name
        assert f[1] == mt and f[2] == mc, name
        assert f[3] == len(payload), f"{name}: PL 必須是 1 byte"
        assert f[-3] == want_cs, name
        assert f[-2:] == b"\x0d\x0a", name
        assert len(f) == 7 + len(payload), name


def test_run_inventory_payload_matches_pdf():
    got = RfidReader.build_run_inventory(antenna=1, rf_mode=103, power_dbm=20.0,
                                         time_ms=100, runs=0)
    assert got == h("01 00 67 07 D0 00 00 00 64 00 00 00 00"), got.hex(" ")


def test_inventory_parameter_payload_matches_pdf():
    got = RfidReader.build_inventory_parameter(
        rf_mode=103, initial_q=8, max_q=15, min_q=0, num_min_q_cycles=1,
        max_queries_since_valid_epc=16)
    assert got == h("3F 00 67 8F 01 00 00 00 10"), got.hex(" ")


def test_payload_too_long_rejected():
    try:
        Packet(MT_COMMAND, 0x64, b"\x00" * 256)
    except ValueError:
        return
    raise AssertionError("payload > 255 應該被拒絕")


# ---------------------------------------------- 2. PacketParser

def _stream() -> bytes:
    a = Packet(MT_COMMAND, CMD_ABORT)
    b = Packet(MT_NOTIFICATION, CMD_RUN_INVENTORY,
               TagReport.build(h("E28011700000020F00001234")))
    return a.to_bytes() + b.to_bytes()


def test_parser_single_feed():
    assert len(PacketParser().feed(_stream())) == 2


def test_parser_byte_by_byte():
    p = PacketParser()
    assert sum(len(p.feed(bytes([b]))) for b in _stream()) == 2


def test_parser_split_at_every_boundary():
    s = _stream()
    for cut in range(len(s) + 1):
        p = PacketParser()
        n = len(p.feed(s[:cut])) + len(p.feed(s[cut:]))
        assert n == 2, f"切在 {cut} 只得到 {n} 包"


def test_parser_resyncs_after_noise():
    p = PacketParser()
    assert len(p.feed(b"\xff\x00\x0a\x01" + _stream())) == 2
    assert p.error_count > 0


def test_parser_rejects_bad_checksum():
    f = bytearray(Packet(MT_COMMAND, CMD_ABORT).to_bytes())
    f[-3] ^= 0xFF
    assert PacketParser().feed(bytes(f)) == []


def test_parser_rejects_bad_end_mark():
    f = bytearray(Packet(MT_COMMAND, CMD_ABORT).to_bytes())
    f[-2] = 0x00
    assert PacketParser().feed(bytes(f)) == []


def test_parser_invalid_msg_type_does_not_stall():
    """假 preamble 後跟著超大 PL,不能讓解析器卡死。"""
    assert len(PacketParser().feed(b"\x0a\x01\xff" + _stream())) == 2


# ---------------------------------------------- 3. TagReport

def test_tag_report_full_format():
    t = TagReport.parse(h("1F"                       # bitmask
                          "01"                       # ANT=1
                          "0D F6 38"                 # 915000 kHz
                          "12 34 56 78"              # timestamp
                          "EA 84"                    # RSSI -5500 → -55.00 dBm
                          "01 00"                    # phase
                          "0E"                       # inventory length
                          "30 00"                    # PC
                          "E2 80 11 70 00 00 02 0F 00 00 12 34"))
    assert t is not None
    assert t.antenna_id == 1
    assert t.rf_channel_khz == 915000
    assert t.timestamp_us == 0x12345678
    assert abs(t.rssi_dbm + 55.0) < 1e-9, t.rssi_dbm     # 必須是有號
    assert t.pc == 0x3000
    assert t.epc_hex == "E28011700000020F00001234"


def test_tag_report_minimal_format():
    t = TagReport.parse(h("01 02 06 30 00 11 22 33 44"))
    assert t is not None
    assert t.antenna_id == 2 and t.epc_hex == "11223344" and t.rssi_dbm == 0.0


def test_tag_report_with_tid():
    t = TagReport.parse(TagReport.build(h("11223344"), fmt=DF_ANTENNA | DF_TID,
                                        antenna_id=3, tid=h("E2003412")))
    assert t is not None
    assert t.antenna_id == 3 and t.tid_hex == "E2003412" and t.epc_hex == "11223344"


def test_tag_report_roundtrip_all_formats():
    for fmt in (0, DF_ANTENNA, DF_RSSI, DF_DEFAULT, DF_DEFAULT | DF_TID):
        p = TagReport.build(h("AABBCCDD1122"), fmt=fmt, antenna_id=2,
                            rssi_dbm=-61.25, tid=h("E2003412"))
        t = TagReport.parse(p)
        assert t is not None, fmt
        assert t.epc_hex == "AABBCCDD1122", fmt
        if fmt & DF_RSSI:
            assert abs(t.rssi_dbm + 61.25) < 1e-9, fmt


def test_tag_report_truncated_returns_none_never_raises():
    full = TagReport.build(h("E28011700000020F00001234"))
    for n in range(len(full)):
        assert TagReport.parse(full[:n]) is None, f"長度 {n} 應該回 None"


def test_tag_report_negative_rssi_range():
    for dbm in (-83.0, -70.5, -38.25, -20.0):
        t = TagReport.parse(TagReport.build(h("1122"), fmt=DF_RSSI, rssi_dbm=dbm))
        assert t is not None and abs(t.rssi_dbm - dbm) < 1e-9, dbm


# ---------------------------------------------- 4. TagStore

def test_tag_store_counts():
    s = TagStore()
    t1 = TagReport.parse(TagReport.build(h("E28011700000020F00001234"), rssi_dbm=-60.0))
    t1b = TagReport.parse(TagReport.build(h("E28011700000020F00001234"), rssi_dbm=-45.0))
    t2 = TagReport.parse(TagReport.build(h("AABBCCDD")))
    for _ in range(4):
        s.add(t1)
    s.add(t1b)
    s.add(t2)

    assert s.unique_count == 2 and s.total_reads == 6
    top = s.snapshot()[0]
    assert top.count == 5
    assert abs(top.max_rssi + 45.0) < 1e-9      # 取最大值
    assert abs(top.rssi + 45.0) < 1e-9          # 最後一次
    s.clear()
    assert s.unique_count == 0 and s.total_reads == 0


def test_tag_store_thread_safe():
    s = TagStore()
    t = TagReport.parse(TagReport.build(h("11223344")))
    ths = [threading.Thread(target=lambda: [s.add(t) for _ in range(500)])
           for _ in range(8)]
    for x in ths:
        x.start()
    for x in ths:
        x.join()
    assert s.total_reads == 4000 and s.unique_count == 1
    assert s.snapshot()[0].count == 4000


# ---------------------------------------------- 5. status

def test_status_text():
    assert status_text(0x00) == "STATUS_OK"
    assert status_text(0x04) == "STATUS_BUSY"
    assert status_text(0x18) == "ERR_TEMPERATURE_HIGH"
    assert status_text(0x62).startswith("UNDEFINED")


# ---------------------------------------------- 6. 端對端 (對模擬器)

def test_end_to_end_streaming():
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=500.0).start()
    try:
        tags = []
        done = threading.Event()
        result = {}

        with RfidReader("127.0.0.1", sim.actual_port) as r:
            r.on_tag = tags.append
            r.on_inventory_finished = lambda st, tot, ms: (
                result.update(status=st, total=tot, ms=ms), done.set())

            assert r.get_firmware_version() == "MPK-R-9504 v1.0.3"
            assert 30 <= r.get_temperature() <= 60

            r.set_data_format(DF_DEFAULT)
            assert r.get_data_format() == DF_DEFAULT
            r.set_inventory_parameter(rf_mode=RF_ULTRA_FAST)

            assert not r.is_inventory_running
            r.start_inventory(power_dbm=20.0)
            assert r.is_inventory_running
            time.sleep(1.0)
            assert len(tags) > 50, f"1 秒只收到 {len(tags)} 個 tag"

            r.abort()
            assert done.wait(2.0), "沒有收到 inventory 結束回應"
            assert not r.is_inventory_running
            # Abort 收到的是「錯誤/中止型」payload [Status][ANT_ID],沒有統計值,
            # 所以這裡看 status 與實際收到的 tag 數,不是看 total。
            assert result["status"] == 0x03, f"status={result['status']:#04x}"
            assert r.parser_error_count == 0

            assert all(t.epc for t in tags)
            assert all(-90.0 <= t.rssi_dbm <= 0.0 for t in tags), "RSSI 必須是負值"
    finally:
        sim.stop()


def test_read_tags_blocking_helper():
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=400.0).start()
    try:
        with RfidReader("127.0.0.1", sim.actual_port) as r:
            rows = r.read_tags(seconds=1.0)
        assert len(rows) == 5, f"應該讀到 5 個唯一 EPC,得到 {len(rows)}"
        assert sum(x.count for x in rows) > 100
        assert rows[0].count >= rows[-1].count, "應依次數由多到少排序"
        assert all(len(x.epc) >= 8 for x in rows)
    finally:
        sim.stop()


def test_scan_one_liner():
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=400.0).start()
    try:
        rows = scan("127.0.0.1", sim.actual_port, seconds=1.0)
        assert len(rows) == 5
        assert all(-90 <= x.rssi <= 0 for x in rows)
    finally:
        sim.stop()


def test_reader_closes_after_context_manager():
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=200.0).start()
    try:
        with RfidReader("127.0.0.1", sim.actual_port) as r:
            r.start_inventory()
            time.sleep(0.2)
            assert r.is_inventory_running
        assert not r.is_open, "離開 with 之後應該已關閉"
    finally:
        sim.stop()


def test_command_timeout_and_lock_release():
    """連到一個什麼都不回的 server:必須丟 TimeoutError,且命令鎖要釋放。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    conns = []
    threading.Thread(target=lambda: conns.append(srv.accept()[0]), daemon=True).start()
    try:
        r = RfidReader("127.0.0.1", port)
        r.open()
        try:
            t0 = time.time()
            try:
                r.send_command(CMD_GET_FIRMWARE, timeout=0.6)
            except TimeoutError:
                assert 0.4 <= time.time() - t0 <= 2.0
            else:
                raise AssertionError("應該要丟 TimeoutError")

            t0 = time.time()          # 第二個命令不能卡在鎖裡
            try:
                r.send_command(CMD_ABORT, timeout=0.6)
            except TimeoutError:
                assert time.time() - t0 <= 2.0, "第二個命令卡在鎖裡"
            else:
                raise AssertionError("應該要丟 TimeoutError")
        finally:
            r.close()
    finally:
        for c in conns:
            c.close()
        srv.close()


def _abort_scenario(abort_style: str):
    """按下 Abort 後,不管兩個回應誰先到,都必須:
       1. 觸發 on_inventory_finished
       2. 不產生「未預期的回應」log
       3. abort() 本身正常返回"""
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=300.0,
                          abort_style=abort_style).start()
    try:
        logs = []
        done = threading.Event()
        result = {}

        with RfidReader("127.0.0.1", sim.actual_port) as r:
            r.on_log = logs.append
            r.on_inventory_finished = lambda st, tot, ms: (
                result.update(status=st, total=tot, ms=ms), done.set())
            r.start_inventory()
            time.sleep(0.5)
            r.abort()
            assert done.wait(2.0), f"[{abort_style}] 沒有觸發 on_inventory_finished"
            time.sleep(0.3)

        unexpected = [x for x in logs if "未預期的回應" in x]
        assert not unexpected, f"[{abort_style}] 出現未預期的回應: {unexpected}"
        assert result["status"] == 0x03, f"[{abort_style}] status={result['status']:#04x}"
        assert not r.is_inventory_running
        return logs
    finally:
        sim.stop()


def test_abort_end_response_before_ack():
    """實機順序:先送 0x6D 結束回應(2-byte 中止型),再送 Abort ack。
       這正是造成 `未預期的回應: RSP MC=0x6D PL=2 [03 01]` 的情境。"""
    logs = _abort_scenario("end_first")
    assert any("盤點結束" in x and "STATUS_ABORT" in x for x in logs), logs


def test_abort_ack_before_end_response():
    """相反順序也要能正確處理。"""
    _abort_scenario("ack_first")


def test_inventory_finished_parses_both_payload_shapes():
    """§2.3.7.3 兩種 payload 形狀都要能解析。"""
    got = []
    r = RfidReader.__new__(RfidReader)          # 不連線,只測解析
    r._inventory_running = True
    r.on_log = None
    r.on_inventory_finished = lambda st, tot, ms: got.append((st, tot, ms))

    # 正常型: Status + Total(4) + Elapsed(4)
    r._inventory_finished(Packet(MT_RESPONSE, CMD_RUN_INVENTORY,
                                 h("00 00 00 00 0A 00 00 00 66")))
    assert got[-1] == (0x00, 10, 102), got[-1]

    # 中止/錯誤型: Status + ANT_ID   ← 使用者實機收到的那一包
    r._inventory_running = True
    r._inventory_finished(Packet(MT_RESPONSE, CMD_RUN_INVENTORY, h("03 01")))
    assert got[-1] == (0x03, 0, 0), got[-1]
    assert not r._inventory_running

    # 錯誤型帶 message
    r._inventory_running = True
    r._inventory_finished(Packet(MT_RESPONSE, CMD_RUN_INVENTORY, h("62 01 AA BB")))
    assert got[-1] == (0x62, 0, 0), got[-1]


def test_inventory_finished_log_text():
    """log 要看得懂,不能只印 raw hex。"""
    logs = []
    r = RfidReader.__new__(RfidReader)
    r._inventory_running = True
    r.on_log = logs.append
    r.on_inventory_finished = None
    r._inventory_finished(Packet(MT_RESPONSE, CMD_RUN_INVENTORY, h("03 01")))
    assert "STATUS_ABORT" in logs[-1] and "ANT=1" in logs[-1], logs[-1]


# ---------------------------------------------- 7. GPIO 的 ASCII 文字框
#
# 實機封包(使用者提供):
#   0A 40 49 6E 70 75 74 50 6F 72 74 0D 0A            @InputPort     查詢指令
#   0A 40 49 6E 70 75 74 20 50 69 6E 31 2C 30 0D 0A   @Input Pin1,0  Pin1 接地
#   0A 40 49 6E 70 75 74 20 50 69 6E 31 2C 31 0D 0A   @Input Pin1,1  Pin1 high

GPIO_QUERY_BYTES = h("0A 40 49 6E 70 75 74 50 6F 72 74 0D 0A")
GPIO_PIN1_LOW_BYTES = h("0A 40 49 6E 70 75 74 20 50 69 6E 31 2C 30 0D 0A")
GPIO_PIN1_HIGH_BYTES = h("0A 40 49 6E 70 75 74 20 50 69 6E 31 2C 31 0D 0A")


def test_gpio_query_command_bytes_match_device():
    """我們送出的 @InputPort 必須跟實機的位元組一模一樣。"""
    got = Packet.text_frame(TEXT_CMD_INPUT_PORT).to_bytes()
    assert got == GPIO_QUERY_BYTES, got.hex(" ").upper()


def test_gpio_state_frames_parse():
    for raw, want in ((GPIO_PIN1_LOW_BYTES, (1, 0)),
                      (GPIO_PIN1_HIGH_BYTES, (1, 1))):
        out = PacketParser().feed(raw)
        assert len(out) == 1, out
        p = out[0]
        assert p.is_text and p.msg_type == MT_TEXT
        assert parse_gpio_text(p.text) == want, p.text
        assert p.to_bytes() == raw, "round-trip 不一致"


def test_parse_gpio_text_variants():
    assert parse_gpio_text("Input Pin1,0") == (1, 0)
    assert parse_gpio_text("Input Pin3,1") == (3, 1)
    assert parse_gpio_text("  Input  Pin2 , 1  ") == (2, 1)      # 空白放寬
    assert parse_gpio_text("input pin2,1") == (2, 1)             # 大小寫放寬
    assert parse_gpio_text("InputPort") is None                  # 查詢指令不是狀態
    assert parse_gpio_text("Output Pin1,0") is None
    assert parse_gpio_text("Input Pin1,2") is None               # 準位只能 0/1
    assert parse_gpio_text("") is None


def test_text_frame_byte_by_byte():
    p = PacketParser()
    assert sum(len(p.feed(bytes([b]))) for b in GPIO_PIN1_HIGH_BYTES) == 1
    assert p.error_count == 0


def test_text_and_binary_mixed_stream():
    """GPIO 文字框與 RFID 二進位封包走同一條連線,不可以互相干擾。"""
    tag = Packet(MT_NOTIFICATION, CMD_RUN_INVENTORY,
                 TagReport.build(h("E28011700000020F00001234")))
    stream = (GPIO_PIN1_LOW_BYTES + tag.to_bytes()
              + GPIO_PIN1_HIGH_BYTES + tag.to_bytes() + GPIO_QUERY_BYTES)

    p = PacketParser()
    out = p.feed(stream)
    assert len(out) == 5, [str(x) for x in out]
    assert [x.is_text for x in out] == [True, False, True, False, True]
    assert parse_gpio_text(out[0].text) == (1, 0)
    assert parse_gpio_text(out[2].text) == (1, 1)
    assert TagReport.parse(out[1].payload).epc_hex == "E28011700000020F00001234"
    assert p.error_count == 0


def test_text_and_binary_split_at_every_boundary():
    tag = Packet(MT_NOTIFICATION, CMD_RUN_INVENTORY,
                 TagReport.build(h("AABBCCDD")))
    stream = GPIO_PIN1_LOW_BYTES + tag.to_bytes() + GPIO_PIN1_HIGH_BYTES
    for cut in range(len(stream) + 1):
        p = PacketParser()
        n = len(p.feed(stream[:cut])) + len(p.feed(stream[cut:]))
        assert n == 3, f"切在 {cut} 只得到 {n} 包"


def test_text_frame_rejects_control_chars():
    """0x0A 0x40 後面接控制字元 → 不是文字框,要能重新同步。"""
    p = PacketParser()
    bad = b"\x0a\x40\x01\x02\x03\x0d\x0a"
    out = p.feed(bad + GPIO_PIN1_HIGH_BYTES)
    assert len(out) == 1 and parse_gpio_text(out[0].text) == (1, 1)
    assert p.error_count > 0


def test_text_frame_without_terminator_does_not_stall():
    """一直等不到 CRLF 也不能卡死 —— 超過上限就重新同步。"""
    p = PacketParser()
    p.feed(b"\x0a\x40" + b"A" * 400)
    out = p.feed(GPIO_PIN1_LOW_BYTES)
    assert len(out) == 1, [str(x) for x in out]
    assert p.error_count > 0


def test_empty_text_frame_rejected():
    p = PacketParser()
    out = p.feed(b"\x0a\x40\x0d\x0a" + GPIO_PIN1_LOW_BYTES)
    assert len(out) == 1 and parse_gpio_text(out[0].text) == (1, 0)


def test_gpio_query_against_simulator():
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=100.0).start()
    try:
        with RfidReader("127.0.0.1", sim.actual_port) as r:
            state = r.query_gpio(timeout=2.0)
            assert set(state) == set(range(1, GPIO_PIN_COUNT + 1)), state
            assert all(v in (0, 1) for v in state.values()), state
            assert r.gpio_updated_at > 0
            assert r.parser_error_count == 0
    finally:
        sim.stop()


def test_gpio_unsolicited_notification():
    """讀取器主動送 @Input PinN,V 時要能即時更新並觸發 callback。"""
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=100.0).start()
    try:
        events = []
        with RfidReader("127.0.0.1", sim.actual_port) as r:
            r.on_gpio = lambda pin, lvl, prev: events.append((pin, lvl, prev))
            r.query_gpio(timeout=2.0)
            events.clear()

            sim.gpio[2] = 1
            sim._send_gpio(2)
            time.sleep(0.4)
            assert (2, 1, 0) in events, events
            assert r.gpio[2] == 1

            sim.gpio[2] = 0
            sim._send_gpio(2)
            time.sleep(0.4)
            assert (2, 0, 1) in events, events
            assert r.gpio[2] == 0
    finally:
        sim.stop()


def test_gpio_during_inventory():
    """盤點進行中也要能收到 GPIO —— 文字框跟二進位通知混在同一條串流。"""
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=400.0).start()
    try:
        tags = []
        events = []
        with RfidReader("127.0.0.1", sim.actual_port) as r:
            r.on_tag = tags.append
            r.on_gpio = lambda pin, lvl, prev: events.append((pin, lvl, prev))
            r.start_inventory()
            time.sleep(0.4)

            sim.gpio[3] = 1
            sim._send_gpio(3)
            time.sleep(0.4)
            r.abort()

            assert len(tags) > 50, f"盤點資料太少: {len(tags)}"
            assert (3, 1, None) in events or (3, 1, 0) in events, events
            assert r.parser_error_count == 0, "文字框與二進位封包互相干擾了"
    finally:
        sim.stop()

# ---------------------------------------------- runner

def _run_all() -> int:
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as ex:
            failed += 1
            print(f"  FAIL  {name}: {type(ex).__name__}: {ex}")
    print()
    print(">>> 全部通過" if failed == 0 else f">>> 失敗 {failed} 項")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
