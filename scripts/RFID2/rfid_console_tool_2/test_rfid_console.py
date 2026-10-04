"""
rfid_console.py 的測試 —— 重點在「讀取器自己停掉時要能持續讀取」。

    python test_rfid_console.py     或   pytest -q test_rfid_console.py
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rfid_console import ConsoleApp, ScriptedKeys  # noqa: E402
from simulator import ReaderSimulator  # noqa: E402


class _NullScreen:
    """吃掉畫面輸出,測試不需要真的畫。"""
    ansi = False

    def size(self):
        return 100, 30

    def draw(self, lines):
        self.last = lines

    def clear(self):
        pass


def _make_app(sim, auto_restart=True) -> ConsoleApp:
    app = ConsoleApp("127.0.0.1", sim.actual_port, antenna=1,
                     rf_mode=103, power_dbm=20.0, auto_restart=auto_restart)
    app.screen = _NullScreen()
    assert app.connect(), "連線失敗"
    return app


def _pump(app: ConsoleApp, seconds: float) -> None:
    """跑主迴圈的邏輯(不含鍵盤),要跟 ConsoleApp.run() 裡做的事一致。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app._maybe_restart()
        app._maybe_poll_gpio()
        app.render()
        time.sleep(app.POLL)


# ------------------------------------------------------------------

def test_auto_restart_keeps_reading_when_reader_stops_itself():
    """實機在連續模式下仍會自己結束盤點 → 必須自動重下命令,讀取不中斷。"""
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=400.0, auto_stop_after=0.4).start()
    try:
        app = _make_app(sim)
        try:
            app.start()
            assert app.want_reading and app.reading

            _pump(app, 1.0)
            mid = app.store.total_reads
            assert app.restarts >= 1, f"1 秒內應該重啟過,restarts={app.restarts}"
            assert mid > 50, f"讀到的量太少: {mid}"

            _pump(app, 1.0)
            assert app.store.total_reads > mid, "第二段時間沒有繼續讀到 tag"
            assert app.want_reading, "使用者沒按 E,意圖不該被關掉"
            assert app.restarts >= 2, f"restarts={app.restarts}"
        finally:
            app.shutdown()
    finally:
        sim.stop()


def test_no_auto_restart_flag_stays_stopped():
    """--no-auto-restart 時,讀取器停了就停了(診斷用)。"""
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=400.0, auto_stop_after=0.3).start()
    try:
        app = _make_app(sim, auto_restart=False)
        try:
            app.start()
            _pump(app, 1.0)
            assert app.restarts == 0
            assert not app.reading, "關掉自動重啟後不該還在盤點"
            frozen = app.store.total_reads
            _pump(app, 0.5)
            assert app.store.total_reads == frozen, "已停止卻還在累加"
        finally:
            app.shutdown()
    finally:
        sim.stop()


def test_stop_wins_over_auto_restart():
    """按 E 之後不可以被自動重啟接回去。"""
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=400.0, auto_stop_after=0.3).start()
    try:
        app = _make_app(sim)
        try:
            app.start()
            _pump(app, 0.8)
            assert app.restarts >= 1

            app.stop()
            assert not app.want_reading and not app.reading
            after_stop = app.store.total_reads
            restarts_at_stop = app.restarts

            _pump(app, 1.0)
            assert app.restarts == restarts_at_stop, "按 E 之後還在重啟"
            assert not app.reading
            assert app.store.total_reads == after_stop, "按 E 之後還在收 tag"
        finally:
            app.shutdown()
    finally:
        sim.stop()


def test_restart_does_not_deadlock_with_commands():
    """
    重啟由主迴圈負責(不是 RX 執行緒)。這裡一邊自動重啟、一邊從另一條
    執行緒送命令,驗證不會卡死 —— 若在 RX 執行緒裡重啟就會在這裡掛掉。
    """
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=300.0, auto_stop_after=0.25).start()
    try:
        app = _make_app(sim)
        done = threading.Event()
        errors = []

        def hammer():
            try:
                for _ in range(8):
                    try:
                        app.reader.get_firmware_version()   # 盤點中會回 BUSY
                    except Exception:
                        pass                                # BUSY / 逾時都算正常
                    time.sleep(0.05)
            except BaseException as ex:                     # noqa: BLE001
                errors.append(ex)
            finally:
                done.set()

        try:
            app.start()
            threading.Thread(target=hammer, daemon=True).start()
            _pump(app, 2.0)
            assert done.wait(3.0), "送命令的執行緒卡住了 —— 疑似死鎖"
            assert not errors, errors
            assert app.restarts >= 2, f"restarts={app.restarts}"
        finally:
            app.shutdown()
    finally:
        sim.stop()


def test_messages_do_not_flood_during_restart():
    """自動重啟的例行訊息不可以把 4 行訊息區洗版。"""
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=300.0, auto_stop_after=0.25).start()
    try:
        app = _make_app(sim)
        try:
            app.start()
            _pump(app, 2.0)
            assert app.restarts >= 4, f"重啟次數不足以驗證: {app.restarts}"

            msgs = app._snapshot_messages()
            starts = [m for m in msgs if "Inventory 開始" in m]
            ends = [m for m in msgs if "盤點結束" in m]
            assert len(starts) <= 1, f"「Inventory 開始」洗版了: {starts}"
            assert len(ends) <= 1, f"「盤點結束」洗版了: {ends}"
        finally:
            app.shutdown()
    finally:
        sim.stop()


def test_elapsed_time_not_reset_by_restart():
    """經過時間要從按 S 開始算,不能被自動重啟歸零。"""
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=300.0, auto_stop_after=0.3).start()
    try:
        app = _make_app(sim)
        try:
            app.start()
            _pump(app, 1.5)
            assert app.restarts >= 2
            assert app.\
                _elapsed() >= 1.4, f"經過時間被重啟歸零了: {app._elapsed():.2f}s"
        finally:
            app.shutdown()
    finally:
        sim.stop()


def test_render_while_rx_thread_logs():
    """render() 走訪訊息時 RX 執行緒同時在寫,不可以拋 RuntimeError。"""
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=500.0, auto_stop_after=0.15).start()
    try:
        app = _make_app(sim)
        stop = threading.Event()
        errs = []

        def spam():
            i = 0
            while not stop.is_set():
                app.msg(f"測試訊息 {i}")
                i += 1
                time.sleep(0.001)

        try:
            app.start()
            threading.Thread(target=spam, daemon=True).start()
            end = time.monotonic() + 1.5
            while time.monotonic() < end:
                try:
                    app._maybe_restart()
                    app.render()
                except Exception as ex:                     # noqa: BLE001
                    errs.append(ex)
                    break
                time.sleep(0.005)
            stop.set()
            assert not errs, errs
        finally:
            stop.set()
            app.shutdown()
    finally:
        sim.stop()


def test_scripted_keys_parsing():
    k = ScriptedKeys("s:0,c,e:0,q")
    got = []
    for _ in range(40):
        v = k.get_key()
        if v:
            got.append(v)
        time.sleep(0.01)
    assert got == ["s", "c", "e", "q"], got


def test_gpio_shown_and_polled():
    """GPIO 狀態要顯示在畫面上,而且輪詢會持續更新。"""
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=200.0).start()
    try:
        app = _make_app(sim)
        app.gpio_interval = 0.2
        try:
            # connect() 已經查過一次
            assert set(app.reader.gpio) == {1, 2, 3}, app.reader.gpio

            sim.gpio[2] = 1
            _pump(app, 0.8)                      # 輪詢應該把新狀態抓回來
            assert app.reader.gpio[2] == 1, app.reader.gpio

            app.render()
            row = [x for x in app.screen.last if x.startswith(" GPIO:")]
            assert row, app.screen.last
            assert "IN1" in row[0] and "IN2" in row[0] and "IN3" in row[0], row[0]
            assert "HIGH" in row[0], row[0]
        finally:
            app.shutdown()
    finally:
        sim.stop()


def test_gpio_poll_does_not_block_main_loop():
    """輪詢必須用 request_gpio()(不等回覆);用 query_gpio() 會把主迴圈卡住。"""
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=200.0).start()
    try:
        app = _make_app(sim)
        app.gpio_interval = 0.05             # 故意設很密
        try:
            app.start()
            t0 = time.monotonic()
            n = 0
            while time.monotonic() - t0 < 1.0:
                app._maybe_restart()
                app._maybe_poll_gpio()
                app.render()
                n += 1
                time.sleep(app.POLL)
            # 1 秒內至少要跑幾十圈;若被 query_gpio 卡住只會剩個位數
            assert n > 25, f"主迴圈被拖慢了,1 秒只跑 {n} 圈"
        finally:
            app.shutdown()
    finally:
        sim.stop()


def test_gpio_and_rfid_together():
    """一邊連續盤點一邊收 GPIO,兩者都要正常。"""
    sim = ReaderSimulator("127.0.0.1", 0, tag_rate=300.0,
                          auto_stop_after=0.3, gpio_toggle_interval=0.2).start()
    try:
        app = _make_app(sim)
        app.gpio_interval = 0.3
        try:
            app.start()
            _pump(app, 2.0)
            assert app.restarts >= 2, f"restarts={app.restarts}"
            assert app.store.total_reads > 100, app.store.total_reads
            assert app.reader.gpio_updated_at > 0
            assert app.reader.parser_error_count == 0, "文字框與二進位封包互相干擾"
        finally:
            app.shutdown()
    finally:
        sim.stop()

# ------------------------------------------------------------------

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
