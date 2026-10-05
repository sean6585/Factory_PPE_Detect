"""In/out judgement of the image trigger's visits (_visit_update / _visit_resolve), with
synthetic person boxes — no camera, no model, no tower. Run: python3 scripts/test_visits.py

The site (2026-10-02): the camera faces the worker; the door to the restricted area is
beside the check spot, on one side of the IMAGE (door_side); the entrance path is
straight in front of the camera. So going IN = leaving the band towards the door side,
going OUT = walking away from the camera (the box shrinks).
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_server as gs   # noqa: E402

fails = []
def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")

# Everything _visit_resolve reaches outside the visit itself, stubbed.
said = []
gs.say = lambda key, *a, **k: said.append(key)
gs.signal = lambda *a, **k: None
gs.queue_alarm_record = lambda *a, **k: ""
gs._rfid_near = lambda: []
gs._state["record"] = True            # events go to _rec_q, read back below
gs._state["cfg"] = {"door_side": "right"}

AREA_TH = 300000
ZONE = [0.35, 0.65]
FRAME = np.zeros((960, 1280, 3), np.uint8)
W = FRAME.shape[1]


def box(cx, area, aspect=0.6):
    """A person box centred at cx (fraction of width) with this area, feet at y=900."""
    w = math.sqrt(area * aspect)
    h = area / w
    x = cx * W
    return [x - w / 2, 900 - h, x + w / 2, 900]


class Walk:
    """Feeds one person (one track ID) through _visit_update at 10 Hz."""
    t = 1000.0

    def __init__(self, tid):
        self.tid = tid

    def at(self, cx, area, ticks=1, aspect=0.6, firm=True):
        """firm=False: a box between track_person_conf and person_conf — followed only."""
        for _ in range(ticks):
            Walk.t += 0.1
            gs._visit_update([{"box": box(cx, area, aspect), "tid": self.tid, "firm": firm}],
                             AREA_TH, ZONE, Walk.t, FRAME)
        return self

    def shrink(self, cx, start, factor=0.9, ticks=15):
        a = start
        for _ in range(ticks):
            a *= factor
            self.at(cx, a)
        return self

    def gone(self, seconds=4.0):
        for _ in range(int(seconds * 10)):
            Walk.t += 0.1
            gs._visit_update([], AREA_TH, ZONE, Walk.t, FRAME)
        return self


def events():
    out = []
    while not gs._rec_q.empty():
        item = gs._rec_q.get_nowait()
        if "event" in item:
            out.append(item["event"])
    return out


def outcome(evs):
    return [(e["intent"], e["departure"], e.get("via"), e["violation"]) for e in evs]


def reset():
    gs._visits.clear()
    events()
    said.clear()


print("1. door on the right: out of the door, unchecked, walks away -> 未檢查即出場")
reset()
Walk(1).at(0.85, 400000, 3).at(0.5, 400000, 3).shrink(0.5, 400000).gone()
check("exit by walking away", outcome(events()), [("out", "out", "away", "未檢查即出場")])
check("spoken", said, ["exit_unchecked"])

print("2. walks up from the far end, then walks back -> turned back, no violation")
reset()
w = Walk(2)
for a in (80000, 120000, 180000, 250000, 320000, 400000):
    w.at(0.5, a)
w.at(0.5, 400000, 5).shrink(0.5, 400000)
check("entry turned back", outcome(events()), [("in", "back", "away", None)])
check("silent", said, [])

print("3. ...and comes back up again: a new visit")
for a in (180000, 250000, 320000, 400000):
    w.at(0.5, a)
v = gs._visits[2]
check("re-engaged, unresolved, 進場", (v["engaged"], v["resolved"], v["intent"]),
      (True, False, "in"))

print("4. walks up, then goes to the door (right edge) unchecked -> 未檢查即進入")
reset()
Walk(4).at(0.5, 400000, 5).at(0.6, 400000).at(0.7, 400000).gone()
check("entry through the door side", outcome(events()), [("in", "in", "edge", "未檢查即進入")])
check("spoken", said, ["intrusion"])

print("5. the ID jumps to a small box (tracker hand-off) -> not walking away")
reset()
w = Walk(5).at(0.5, 400000, 5).at(0.5, 80000, 15)
check("no departure from a jump", outcome(events()), [])
w.at(0.5, 400000, 3).shrink(0.5, 400000)       # big again, then really walks off
check("a real walk-away after it still counts", outcome(events()),
      [("in", "back", "away", None)])

print("6. the ID jumps sideways while shrinking -> not walking away")
reset()
Walk(6).at(0.85, 400000, 3).at(0.5, 400000, 3).at(0.5, 170000).at(0.62, 140000, 10)
check("no departure from a sideways jump", outcome(events()), [])

print("7. turns sideways / crouches / one partial box at the gate -> stays")
reset()
Walk(7).at(0.5, 400000, 5).at(0.5, 220000, 20).at(0.5, 400000, 3) \
    .at(0.5, 100000).at(0.5, 400000, 5)
check("no departure", outcome(events()), [])

print("8. door on the left mirrors it")
reset()
gs._state["cfg"]["door_side"] = "left"
Walk(8).at(0.15, 400000, 3).at(0.5, 400000, 3).shrink(0.5, 400000).gone()
Walk(9).at(0.5, 400000, 5).at(0.4, 400000).at(0.3, 400000).gone()
check("left door: out by walking away, in by the left edge", outcome(events()),
      [("out", "out", "away", "未檢查即出場"), ("in", "in", "edge", "未檢查即進入")])
gs._state["cfg"]["door_side"] = "right"

print("9. leaving by the far (non-door) edge still counts as going out")
reset()
Walk(10).at(0.85, 400000, 3).at(0.5, 400000, 3).at(0.4, 400000).at(0.3, 400000).gone()
check("out by the far edge", outcome(events()), [("out", "out", "edge", "未檢查即出場")])

print("10. out of the door, PASS, walks away -> went out, PASS window closed, no alarm")
reset()
w = Walk(11).at(0.85, 400000, 3).at(0.5, 400000, 3)
gs._visits[11]["checks"] = [{"status": "PASS", "epc": "E1", "items": {}}]
gs._image_trigger["pass_tid"] = 11
w.shrink(0.5, 400000).gone()
check("passed and went out", outcome(events()), [("out", "out", "away", None)])
check("PASS window closed", gs._image_trigger["pass_tid"], None)
check("silent", said, [])

print("11. a weak (tracking-only) box never starts a visit, but carries one on")
reset()
Walk(12).at(0.85, 400000, 3, firm=False).at(0.5, 400000, 5, firm=False).gone()
check("weak box alone: no visit, no alarm", (outcome(events()), said), ([], []))
w = Walk(13).at(0.85, 400000, 3).at(0.5, 400000, 3)          # firm: 出場 visit starts
a = 400000
for _ in range(15):                                          # walks off, score sagging
    a *= 0.9
    w.at(0.5, a, firm=False)
w.gone()
check("weak box finishes the walk-away", outcome(events()), [("out", "out", "away", "未檢查即出場")])

print("12. side-on out of the door (under AREA_TH, over the exit fraction), unchecked, walks away")
reset()
Walk(14).at(0.85, 200000, 3).at(0.5, 200000, 3).shrink(0.5, 200000).gone()   # 200k ≥ 0.6 × 300k
check("caught: 未檢查即出場", outcome(events()), [("out", "out", "away", "未檢查即出場")])
check("spoken", said, ["exit_unchecked"])

print("13. the same side-on box from the OTHER side (entering) needs the full AREA_TH")
reset()
Walk(15).at(0.15, 200000, 3).at(0.5, 200000, 3).shrink(0.5, 200000).gone()
check("no visit", outcome(events()), [])

print("14. out of the door, crosses into the band small and only grows past the bar there")
reset()
Walk(16).at(0.85, 160000, 3).at(0.6, 160000).at(0.55, 190000).at(0.5, 220000, 3) \
    .shrink(0.5, 220000).gone()
check("still 出場", outcome(events()), [("out", "out", "away", "未檢查即出場")])

print("15. far away in the middle wipes the door origin: walking up from there is 進場")
reset()
w = Walk(17).at(0.85, 400000, 3)                      # was on the door side once
for a in (60000, 90000, 140000, 220000, 320000, 400000):
    w.at(0.5, a)                                      # ...then came up the entrance path
check("intent 進場", gs._visits[17]["intent"], "in")

print("16. a front-on exit keeps the old walk-away line (half AREA_TH)")
reset()
w = Walk(18).at(0.85, 400000, 3).at(0.5, 400000, 3)
a = 400000
while a > 160000:
    a *= 0.9
    w.at(0.5, a)
w.at(0.5, 160000, 10)                                 # above 150k: not gone yet
check("not resolved at 160k", outcome(events()), [])
w.shrink(0.5, 160000, ticks=5).gone()
check("resolved under 150k", outcome(events()), [("out", "out", "away", "未檢查即出場")])

print("17. exit fraction 1.0 = the old behaviour: a side-on exit starts no visit")
reset()
gs._state["cfg"]["exit_area_fraction"] = 1.0
Walk(19).at(0.85, 200000, 3).at(0.5, 200000, 3).shrink(0.5, 200000).gone()
check("no visit", outcome(events()), [])
gs._state["cfg"].pop("exit_area_fraction")

print("18. the 2026-10-03 bench video: out of the door, to the back wall, straight back in")
# Area per tick as fractions of AREA_TH, read off the recording (bench AREA_TH 220k):
# the back wall stopped the walk at 0.41 for ~0.3 s.
VIDEO = [2.0, 1.31, 1.10, 0.95, 0.83, 0.65, 0.62, 0.62, 0.62, 0.48, 0.41, 0.41, 0.60,
         0.62, 0.62, 0.64, 0.64, 0.72, 0.72, 0.72, 0.77, 1.05, 1.52, 1.85]
def replay(tid):
    w = Walk(tid).at(0.85, 2.0 * AREA_TH, 2)            # door side (right in these tests)
    for f in VIDEO:
        w.at(0.5, f * AREA_TH)
    w.at(0.6, 1.85 * AREA_TH).at(0.7, 1.85 * AREA_TH).gone()   # back through the door
reset()
replay(20)
check("walk-away line 0.5: only a turn-back", outcome(events()), [("out", "back", "edge", None)])
reset()
gs._state["cfg"]["away_fraction"] = 0.65
replay(21)
check("walk-away line 0.65: exit, then an intrusion", outcome(events()),
      [("out", "out", "away", "未檢查即出場"), ("in", "in", "edge", "未檢查即進入")])
check("both spoken", said, ["exit_unchecked", "intrusion"])
gs._state["cfg"].pop("away_fraction")

print("19. PASS, then the same ID goes through: the 5 s window ends at once, next round opens")
import time as _time
def _pass(self):
    """This walker's visit just PASSed: the PASS window and its cooldown are open."""
    gs._visits[self.tid]["checks"] = [{"status": "PASS", "epc": "E1", "items": {}}]
    gs._image_trigger["pass_tid"] = self.tid
    gs._image_trigger["next_allowed"] = _time.monotonic() + 5.0
    gs._light_policy.flash("green_flash", 5.0, _time.monotonic(), "pass")
    return self
Walk._pass = _pass
for label, tid, walk in (
        ("in (to the door edge)", 30,
         lambda w: w.at(0.5, 400000, 5)._pass().at(0.6, 400000).at(0.7, 400000)),
        ("out (walks away)", 31,
         lambda w: w.at(0.85, 400000, 3).at(0.5, 400000, 3)._pass().shrink(0.5, 400000))):
    reset()
    walk(Walk(tid))
    check(f"{label}: no alarm", (outcome(events())[0][3], said), (None, []))
    check(f"{label}: cooldown over now", gs._image_trigger["next_allowed"] <= _time.monotonic(), True)
    check(f"{label}: green flash ended",
          gs._light_policy.desired(_time.monotonic(), None, False) != "green_flash", True)

print("20. PASS voice: 軌跡方向 adds 請進場/請出場 (2026-10-06), IT 回報 says just 「檢測通過」")
want = {("track", "in"): "pass_entry", ("track", "out"): "pass_leave",
        ("it", "in"): "pass", ("it", "out"): "pass"}
for (mode, intent), key in want.items():
    reset()
    gs._state["cfg"]["direction_source"] = mode
    gs.announce({"status": "PASS", "source": "image", "intent": intent, "items": []})
    check(f"{mode} mode, {intent}: says", said, [key])
gs._state["cfg"].pop("direction_source")

if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
