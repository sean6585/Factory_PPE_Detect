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

print("14. out of the door AT the exit bar, smaller in the band, grows past the bar there")
# Since 2026-10-09 the door-side sighting itself must reach the bar (0.6 × 300k = 180k
# here): 「面積也要大於門檻」. What it does in the band afterwards is as before.
reset()
Walk(16).at(0.85, 190000, 3).at(0.6, 160000).at(0.55, 190000).at(0.5, 220000, 3) \
    .shrink(0.5, 220000).gone()
check("still 出場", outcome(events()), [("out", "out", "away", "未檢查即出場")])
reset()
Walk(18).at(0.85, 160000, 3).at(0.6, 160000).at(0.55, 190000).at(0.5, 320000, 3) \
    .shrink(0.5, 320000).gone()
check("under the bar on the door side: not out of the door (進場, walked off = back)",
      outcome(events()), [("in", "back", "away", None)])

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

print("21. same words to the same person within 5 s are spoken once (2026-10-07)")
reset()
gs._voice_said.clear()
gs._visits[7] = {"tid": 7, "started": 100.0}
gs._visits[8] = {"tid": 8, "started": 101.0}
T0 = 1000.0
check("first ID讀取失敗: spoken", gs.say_to("no_tag", 7, now=T0), True)
check("again 2 s later, same person: silent", gs.say_to("no_tag", 7, now=T0 + 2.0), False)
check("again 4.9 s later: still silent", gs.say_to("no_tag", 7, now=T0 + 4.9), False)
check("someone else, same words: spoken", gs.say_to("no_tag", 8, now=T0 + 2.0), True)
check("same person, different words: spoken", gs.say_to("fail", 7, now=T0 + 3.0), True)
check("5 s after it was last SPOKEN: spoken again", gs.say_to("no_tag", 7, now=T0 + 5.1), True)
gs._visits[7]["started"] = 200.0            # walked off and came back: a new visit
check("same track ID, new visit: spoken", gs.say_to("no_tag", 7, now=T0 + 6.0), True)
check("violations are never held back (1)", gs.say_to("intrusion", 7, now=T0 + 6.1), True)
check("violations are never held back (2)", gs.say_to("intrusion", 7, now=T0 + 6.2), True)
check("no track ID (button / sensor): always (1)", gs.say_to("no_tag", None, now=T0 + 6.3), True)
check("no track ID (button / sensor): always (2)", gs.say_to("no_tag", None, now=T0 + 6.4), True)
check("what was actually said", said,
      ["no_tag", "no_tag", "fail", "no_tag", "no_tag", "intrusion", "intrusion", "no_tag", "no_tag"])
reset()
gs._voice_said.clear()
gs.announce({"status": "FAIL", "source": "image", "track_id": 8, "items": []})
gs.announce({"status": "FAIL", "source": "image", "track_id": 8, "items": []})
check("a FAIL verdict repeated to the same visit: said once", said, ["fail"])
gs._visits.pop(7, None); gs._visits.pop(8, None)

print("23. exit multiplier above 1: someone from the door must be BIGGER to count (2026-10-09)")
reset()
gs._state["cfg"]["exit_area_fraction"] = 1.5                 # door side needs 450k here
Walk(41).at(0.85, 400000, 3).at(0.5, 400000, 3).shrink(0.5, 400000).gone()
check("door side at 1.33 × AREA_TH: not followed", outcome(events()), [])
Walk(42).at(0.85, 500000, 3).at(0.5, 500000, 3).shrink(0.5, 500000, ticks=22).gone()   # long enough under the line
check("door side at 1.67 × AREA_TH: caught", outcome(events()), [("out", "out", "away", "未檢查即出場")])
Walk(43).at(0.15, 350000, 3).at(0.5, 350000, 3)
check("entering keeps the plain AREA_TH (visit started)", gs._visits[43].get("engaged"), True)
Walk(44).at(0.85, 500000, 2).at(0.5, 500000, 1)
check("check bar for a door-side track: 1.5 × AREA_TH", gs._gate_area({"tid": 44}, AREA_TH), 1.5 * AREA_TH)
check("check bar for an entering track: AREA_TH", gs._gate_area({"tid": 43}, AREA_TH), AREA_TH)
check("check bar for an unknown track: AREA_TH", gs._gate_area({"tid": 999}, AREA_TH), AREA_TH)
gs._state["cfg"]["exit_area_fraction"] = 0.85
check("below 1 the check bar stays AREA_TH (only the visit starts early)",
      gs._gate_area({"tid": 44}, AREA_TH), AREA_TH)
gs._state["cfg"]["exit_area_fraction"] = 2.5
check("capped at 2.0", gs.exit_area_fraction(), 2.0)
gs._state["cfg"].pop("exit_area_fraction")
reset()

print("24. out of the door, walks off far enough to count as gone, comes back IN (2026-10-08 #242)")
# The recording's numbers: walk-away line 0.8 × AREA_TH; the worker got down to ~0.57 ×
# (109k of 190k) — gone by the line, yet above the 0.5 × that used to wipe "came from".
reset()
gs._state["cfg"]["away_fraction"] = 0.8
w = Walk(51).at(0.85, 1.9 * AREA_TH, 3).at(0.5, 1.9 * AREA_TH, 2)     # out of the door (right)
for f in (1.6, 1.3, 1.1, 0.95, 0.8, 0.7, 0.62, 0.58, 0.57, 0.57, 0.57, 0.57, 0.57):
    w.at(0.5, f * AREA_TH)                                             # walks away
check("first visit: 出場 through, walking away", outcome(events()), [("out", "out", "away", "未檢查即出場")])
check("where it came from is forgotten", gs._visits[51].get("came_from"), None)
for f in (0.6, 0.7, 0.85, 1.0, 1.2, 1.5, 1.8):                         # turns, walks back up
    w.at(0.5, f * AREA_TH)
w.at(0.5, 1.8 * AREA_TH, 3).at(0.7, 1.8 * AREA_TH).at(0.85, 1.8 * AREA_TH).gone()   # into the door
check("way back is 進場, the door crossing an intrusion",
      outcome(events()), [("in", "in", "edge", "未檢查即進入")])
gs._state["cfg"].pop("away_fraction")
reset()

print("25. door on BOTH sides (左 & 右): out of either side, in through either side (2026-10-09)")
reset()
gs._state["cfg"].update(door_side="both", exit_area_fraction=1.0)
BIG = 1.6 * AREA_TH
Walk(61).at(0.15, BIG, 3).at(0.5, BIG, 3).shrink(0.5, BIG, ticks=22).gone()
check("out of the LEFT, walks away: 出場", outcome(events()), [("out", "out", "away", "未檢查即出場")])
Walk(62).at(0.85, BIG, 3).at(0.5, BIG, 3).shrink(0.5, BIG, ticks=22).gone()
check("out of the RIGHT, walks away: 出場", outcome(events()), [("out", "out", "away", "未檢查即出場")])
def walk_up(tid):
    w = Walk(tid)
    for f in (0.2, 0.35, 0.5, 0.7, 0.9, 1.1, 1.3, 1.5):
        w.at(0.5, f * AREA_TH)                       # up the entrance path, straight ahead
    return w
walk_up(63).at(0.5, BIG, 3).at(0.2, BIG).gone()
check("walks up, in through the LEFT: 進場 through", outcome(events()), [("in", "in", "edge", "未檢查即進入")])
walk_up(64).at(0.5, BIG, 3).at(0.8, BIG).gone()
check("walks up, in through the RIGHT: 進場 through", outcome(events()), [("in", "in", "edge", "未檢查即進入")])
w = Walk(65)
for f in (0.3, 0.5, 0.8, 1.1, 1.4):
    w.at(0.85, f * AREA_TH)                          # up the hall along the right, small first
w.at(0.5, BIG, 3).at(0.2, BIG).gone()
check("along the right from far away: 進場, not out of the door",
      outcome(events()), [("in", "in", "edge", "未檢查即進入")])
Walk(66).at(0.15, BIG, 3).at(0.5, BIG, 3).at(0.85, BIG).gone()
check("out of the left, back in on the right: 出場 → back", outcome(events()), [("out", "back", "edge", None)])
gs._state["cfg"].pop("exit_area_fraction"); gs._state["cfg"]["door_side"] = "right"
reset()

print("22. the check votes on the dwell's last 3 frames + 2 new ones (2026-10-07)")
_saved = {k: getattr(gs, k) for k in ("latest_frame", "detect", "finalize_check", "queue_record")}
voted = {}
fresh_detects = []
gs.latest_frame = lambda: FRAME.copy()
gs.detect = lambda f: (fresh_detects.append(1) or [{"name": "fresh", "score": 1.0, "box": [0, 0, 1, 1]}], 1.0)
gs.finalize_check = lambda ids, worker, **k: (voted.__setitem__("dets", [gs._frames[i]["dets"][0]["name"] for i in ids])
                                             or {"status": "PASS", "items": []})
gs.queue_record = lambda *a, **k: None
gs._state.update(camera=True, swapping=False, burst_interval=0.0)
gs._camera["ok"] = True
gs._state["cfg"]["frames"] = 5
def pre(n, tid=4, t0=50.0):
    return [{"t": t0 + 0.1 * i, "f": FRAME, "tid": tid,
             "dets": [{"name": f"dwell{i}", "score": 1.0, "box": [0, 0, 1, 1]}]} for i in range(n)]
try:
    gs.run_gate_check("", source="image", rfid={"epc": "E", "rssi": -50, "reads": 3, "candidates": []},
                      pre=pre(3))
    check("3 kept + 2 new, kept ones first", voted["dets"], ["dwell0", "dwell1", "dwell2", "fresh", "fresh"])
    check("only 2 new frames were detected", len(fresh_detects), 2)
    fresh_detects.clear()
    gs.run_gate_check("", source="image", rfid={"epc": "E", "rssi": -50, "reads": 3, "candidates": []},
                      pre=pre(6))
    check("never all 5 from the dwell: the newest 4 + 1 new",
          voted["dets"], ["dwell2", "dwell3", "dwell4", "dwell5", "fresh"])
    fresh_detects.clear()
    gs.run_gate_check("", source="api")
    check("button / sensor (no dwell): 5 new, as before", (voted["dets"], len(fresh_detects)), (["fresh"] * 5, 5))
    # Which dwell ticks are taken: same worker, recent, newest last.
    gs._pre_burst.clear()
    for e in pre(2, tid=9, t0=99.0) + pre(5, tid=4, t0=99.5):
        gs._pre_burst.append(e)
    picked = gs._pre_frames(4, 100.0)
    check("only the subject's own ticks, the last 3", [e["dets"][0]["name"] for e in picked], ["dwell2", "dwell3", "dwell4"])
    check("ticks older than 0.6 s are not used (at 100.45 only the 99.9 one is left)",
          [e["dets"][0]["name"] for e in gs._pre_frames(4, 100.45)], ["dwell4"])
    gs._state["cfg"]["burst_before"] = 9
    check("burst_before is capped at frames - 1", gs.burst_before(), 4)
    gs._state["cfg"]["burst_before"] = 0
    check("burst_before 0 = all new, as before", gs._pre_frames(4, 100.0), [])
finally:
    for k, v in _saved.items():
        setattr(gs, k, v)
    gs._state["cfg"].pop("burst_before", None); gs._state["cfg"].pop("frames", None)
    gs._pre_burst.clear()

if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
