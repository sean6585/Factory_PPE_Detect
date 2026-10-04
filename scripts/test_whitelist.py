import json
import os
import sys
import tempfile
import time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from whitelist import Whitelist
from it_report import UNREGISTERED, result_forms

fails = []
def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")

tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "whitelist.json")
def write(text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    # mtime resolution can be coarse; bump it so every rewrite is seen as a change
    t = time.time() + len(os.listdir(tmp)) + write.n
    os.utime(path, (t, t))
    write.n += 1
write.n = 0

wl = Whitelist(path)

print("1. no file -> off, everyone allowed")
check("absent file -> None", wl.allowed("202609290008"), None)
check("state says absent", wl.state()["exists"], False)

print("2. a list in force")
write(json.dumps({"enabled": True, "ids": ["202609290008", " 2026092900ab "]}))
check("listed badge", wl.allowed("202609290008"), True)
check("unlisted badge", wl.allowed("202609290004"), False)
check("case and spaces ignored", wl.allowed("2026092900AB"), True)
check("count", wl.state()["count"], 2)

print("3. picked up without a restart")
write(json.dumps({"ids": ["202609290004"]}))
check("newly added badge", wl.allowed("202609290004"), True)
check("removed badge", wl.allowed("202609290008"), False)
check("enabled defaults to true", wl.state()["enabled"], True)

print("4. switched off")
write(json.dumps({"enabled": False, "ids": []}))
check("enabled:false -> None", wl.allowed("anything"), None)

print("5. broken file fails CLOSED, never open")
write("{not json")
check("bad JSON -> refused", wl.allowed("202609290004"), False)
check("error reported", bool(wl.state()["error"]), True)
write(json.dumps({"ids": [202609290004]}))
check("number instead of string -> refused", wl.allowed("202609290004"), False)
write(json.dumps({"enabled": True, "ids": []}))
check("empty list lets nobody through", wl.allowed("202609290004"), False)

print("6. back to absent")
os.remove(path)
check("deleted file -> off again", wl.allowed("202609290004"), None)

print("7. IT form for an unregistered badge")
cfg = {"item_fields": {"Hardhat": "helmet", "Safety Harness": "harness", "Bodycam": "bodyCam"},
       "fab_area": "F20P3A-L40-A-1"}
ref = {"ts": "2026-09-30 20:00:00", "status": UNREGISTERED, "epc": "202609290004",
       "image": "/rec/dataset/alarms/images/a.jpg", "items": {}}
again = dict(ref, ts="2026-09-30 20:00:05", image="")
ev = {"ts": "2026-09-30 20:00:09", "violation": None, "departure": "back", "checks": [ref, again]}
forms = result_forms(ev, cfg)
check("one POST per badge per visit", len(forms), 1)
f, photo, label = forms[0]
check("sent as Fail", f["ppeResult"], "Fail")
check("carries the badge", f["rfid"], "202609290004")
check("items empty", (f["helmet"], f["harness"], f["bodyCam"]), ("", "", ""))
check("photo of the refusal", photo, "/rec/dataset/alarms/images/a.jpg")
ev_in = dict(ev, violation="被拒絕仍進入", departure="in", image="/rec/b.jpg")
forms = result_forms(ev_in, cfg)
check("refused, then walked in -> refusal + entry", [x[2] for x in forms], ["ID未登入", "被拒絕仍進入"])

if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
