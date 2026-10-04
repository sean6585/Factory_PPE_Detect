import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from gate_light import BASE, PATTERNS, RESTORE_S, REFRESH_S, LightPolicy
from it_report import result_forms

fails = []
def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")

print("1. base states")
p = LightPolicy()
check("idle -> green steady", p.desired(0, None, False), "green")
check("checking -> yellow steady", p.desired(0, None, True), "yellow")
check("device down -> red steady", p.desired(0, "攝影機斷線", False), "red")
check("the fallback base is red steady", BASE, "red")
check("restore outlasts a refresh", RESTORE_S > REFRESH_S + 1, True)

print("2. flashes")
p.flash("green_flash", 5, now=10, tag="pass")
check("PASS -> green flashing", p.desired(11, None, True), "green_flash")
check("expires after its hold", p.desired(15.1, None, True), "yellow")
p.flash("red_flash", 5, now=20)
check("a flash outranks checking", p.desired(21, None, True), "red_flash")
p.flash("green_flash", 5, now=22, tag="pass")
check("newest event wins (PASS after FAIL)", p.desired(22.5, None, False), "green_flash")

print("3. a fault outranks every flash but an operator test")
p.flash("green_flash", 5, now=30, tag="pass")
check("red steady beats a PASS flash", p.desired(31, "RFID 讀取器離線", False), "red")
p.flash("yellow_flash", 5, now=32, tag="test", beats_abnormal=True)
check("test flash beats red steady", p.desired(33, "RFID 讀取器離線", False), "yellow_flash")

print("4. end() only ends its own event")
p = LightPolicy()
p.flash("green_flash", 5, now=40, tag="pass")
p.flash("red_flash", 5, now=41, tag="violation")      # a tailgater walks in
check("the passer crossing does not clear the tailgater's red", p.end("pass"), False)
check("still red flashing", p.desired(41.5, None, False), "red_flash")
p.flash("green_flash", 5, now=50, tag="pass")
check("crossing ends the pass window", p.end("pass"), True)
check("back to idle green", p.desired(50.5, None, False), "green")

print("5. patterns are 5 lamp digits")
check("all 5 digits", {len(v) for v in PATTERNS.values()}, {5})

print("6. IT: a PASS that did not go through is not sent; a FAIL always is")
cfg = {"item_fields": {"Hardhat": "helmet", "Safety Harness": "harness"}, "fab_area": "F"}
fail = {"ts": "2026-09-30 21:00:00", "status": "FAIL", "epc": "E", "image": "f.jpg",
        "items": {"Hardhat": False, "Safety Harness": True}}
ok = dict(fail, ts="2026-09-30 21:00:04", status="PASS", image="p.jpg",
          items={"Hardhat": True, "Safety Harness": True})
def labels(departure):
    return [f[2] for f in result_forms({"ts": "2026-09-30 21:00:09", "violation": None,
                                        "departure": departure, "checks": [fail, ok]}, cfg)]
check("went in: FAIL + PASS", labels("in"), ["Fail", "Pass"])
check("turned back: FAIL only", labels("back"), ["Fail"])
check("stood on: FAIL only", labels("none"), ["Fail"])
check("track lost: both (more is better than less)", labels("unknown"), ["Fail", "Pass"])

print("7. IT: FAIL, then walked in -> two posts, each with its own photo")
ev = {"ts": "2026-09-30 21:00:12", "violation": "未通過仍進入", "departure": "in",
      "image": "alarms/in.jpg", "epc": "E", "checks": [fail]}
forms = result_forms(ev, cfg)
check("the check, then the intrusion", [f[2] for f in forms], ["Fail", "未通過仍進入"])
check("check photo, then crossing photo", [f[1] for f in forms], ["f.jpg", "alarms/in.jpg"])
check("the check keeps its items", (forms[0][0]["helmet"], forms[0][0]["harness"]), ("Fail", "Pass"))
check("the intrusion's items are empty", (forms[1][0]["helmet"], forms[1][0]["harness"]), ("", ""))
ev_out = dict(ev, violation="未通過仍出場", departure="out")
check("walking OUT after a FAIL: only the check", [f[2] for f in result_forms(ev_out, cfg)], ["Fail"])

if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
