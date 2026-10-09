"""rfid_reader: the identity rule (pick_worker), per-antenna observations, and antennas
taking turns — the last against the vendor library's reader simulator, no hardware."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rfid_reader import RfidService, TagObservations, pick_worker   # noqa: E402
from simulator import ReaderSimulator                               # noqa: E402  (vendor dir)

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")


def row(epc, rssi):
    return {"epc": epc, "rssi": rssi, "reads": 5}


print("1. pick_worker: the strongest badge at or above the floor")
best, near = pick_worker([], -59)
check("nothing heard -> no worker", (best, near), (None, []))
best, near = pick_worker([row("A", -65), row("B", -70)], -59)
check("all below the floor -> no worker", best, None)
best, near = pick_worker([row("A", -50)], -59)
check("one badge -> that badge", best["epc"], "A")
best, near = pick_worker([row("A", -58), row("B", -52), row("C", -70)], -59)
check("two above the floor -> the strongest, no refusal", best["epc"], "B")
check("near lists both, strongest first", [r["epc"] for r in near], ["B", "A"])
best, _ = pick_worker([row("A", -59.0)], -59)
check("exactly at the floor counts", best["epc"], "A")

print("2. observations keep the antenna; a badge's strength is its peak over all of them")
obs = TagObservations()
t0 = 1000.0
obs.add("W", -62.0, t0 - 2.0, ant=1)       # worker: badge on the side facing antenna 3
obs.add("W", -48.0, t0 - 1.0, ant=3)
obs.add("Q", -60.0, t0 - 1.5, ant=1)       # next in line
obs.add("OLD", -40.0, t0 - 9.0, ant=1)     # outside the 3 s window
rows = obs.summary(t0, 3.0)
check("window drops old reads", sorted(r["epc"] for r in rows), ["Q", "W"])
w = next(r for r in rows if r["epc"] == "W")
check("union: W counts at its best antenna", w["rssi"], -48.0)
check("per-antenna peaks kept", w["ants"], {1: -62.0, 3: -48.0})
best, _ = pick_worker(rows, -59)
check("W chosen over Q", best["epc"], "W")
c = obs.closest(t0, 3.0, 1.0)
check("closest() also carries ants", c["ants"], {1: -62.0, 3: -48.0})
obs.add("X", -50.0, t0 - 0.5)               # no antenna byte: 0, left out of ants
check("unknown antenna not listed", next(r for r in obs.summary(t0, 3.0) if r["epc"] == "X")["ants"], {})


def run_service(antennas, seconds):
    sim = ReaderSimulator(port=0, tag_rate=200.0).start()
    svc = RfidService("127.0.0.1", sim.actual_port, antenna=antennas, power_dbm=20.0, rf_mode=103)
    svc.start()
    try:
        deadline = time.time() + 5
        while not svc.connected and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(seconds)
        return svc, sim
    except Exception:
        svc.stop(); sim.stop()
        raise


print("3. one antenna: continuous inventory, as before")
svc, sim = run_service(1, 1.5)
try:
    ants = {a for r in svc.obs.summary() for a in r["ants"]}
    check("connected", svc.connected, True)
    check("reads arrive", svc.obs.total_reads > 50, True)
    check("only antenna 1 heard", ants, {1})
    check("never ended (continuous)", svc._end_seq, 0)
finally:
    svc.stop(); sim.stop(); time.sleep(0.4)

print("4. antennas 1,3: they take turns and both are heard")
svc, sim = run_service([1, 3], 2.0)
try:
    ants = {a for r in svc.obs.summary() for a in r["ants"]}
    check("both antennas heard", ants, {1, 3})
    runs = svc._end_seq
    # 2 s at 0.25 s a run = 8; allow slack for connect and scheduling, but a 0.25 s
    # supervisor poll between runs (the bug _ended fixes) would give only ~4.
    check(f"runs back to back ({runs} in 2 s)", runs >= 6, True)
    check("status lists the antennas", svc.status()["antennas"], [1, 3])
    r = svc.reconfigure(22.0, 302)
    check("live power/mode change accepted (a timed run's normal end is no refusal)",
          (r.get("ok"), svc.power_dbm, svc.rf_mode), (True, 22.0, 302))
    before = svc._end_seq
    time.sleep(1.0)
    check("still taking turns after the change", svc._end_seq - before >= 3, True)
finally:
    svc.stop(); sim.stop(); time.sleep(0.4)

print("5. each antenna runs at its own power and mode; changing one leaves the other")
import mpk_rfid                                                    # noqa: E402
runs_sent = []
_orig_start = mpk_rfid.RfidReader.start_inventory


def _recording_start(self, antenna=1, rf_mode=103, power_dbm=20.0, time_ms=0, runs=0):
    runs_sent.append((antenna, power_dbm, rf_mode))
    return _orig_start(self, antenna=antenna, rf_mode=rf_mode, power_dbm=power_dbm,
                       time_ms=time_ms, runs=runs)


mpk_rfid.RfidReader.start_inventory = _recording_start


def sent_since(i):
    by_ant = {}
    for ant, p, m in runs_sent[i:]:
        by_ant.setdefault(ant, set()).add((p, m))
    return by_ant


sim = ReaderSimulator(port=0, tag_rate=200.0).start()
svc = RfidService("127.0.0.1", sim.actual_port, antenna=[1, 3], power_dbm=20.0, rf_mode=103,
                  antenna_settings={1: (18.0, 103), 3: (25.0, 285)})
svc.start()
try:
    deadline = time.time() + 5
    while not svc.connected and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(1.2)
    check("each antenna's runs carry its own settings", sent_since(0),
          {1: {(18.0, 103)}, 3: {(25.0, 285)}})
    check("settings() reports both",
          svc.settings(), {1: {"power_dbm": 18.0, "rf_mode": 103}, 3: {"power_dbm": 25.0, "rf_mode": 285}})
    r = svc.reconfigure(22.0, 302, antenna=3)
    check("change antenna 3 only: accepted", (r.get("ok"), r.get("antenna")), (True, 3))
    i = len(runs_sent)
    time.sleep(1.2)
    check("after it: antenna 3 new, antenna 1 untouched", sent_since(i),
          {1: {(18.0, 103)}, 3: {(22.0, 302)}})
    r = svc.reconfigure(20.0, 345)
    i = len(runs_sent)
    time.sleep(1.2)
    check("no antenna named: both changed", (r.get("ok"), sent_since(i)),
          (True, {1: {(20.0, 345)}, 3: {(20.0, 345)}}))
    try:
        svc.reconfigure(20.0, 103, antenna=2)
        check("an antenna not in use is refused", "accepted", "ValueError")
    except ValueError:
        check("an antenna not in use is refused", "ValueError", "ValueError")
finally:
    svc.stop(); sim.stop()
    mpk_rfid.RfidReader.start_inventory = _orig_start

print()
if fails:
    print(f"{len(fails)} FAILURE(S):")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
