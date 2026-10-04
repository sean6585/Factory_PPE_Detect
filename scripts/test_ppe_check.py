import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from ppe_check import evaluate, load_config, containment

cfg = load_config(None)
WORKER = [100, 100, 300, 600]          # primary person box
def d(name, score, box): return {"name": name, "score": score, "box": box}
def person(box, s=0.9):  return d("Person", s, box)

# boxes inside WORKER
HAT   = d("Hardhat",     0.90, [150, 110, 250, 180])
VEST  = d("Safety Vest", 0.90, [130, 250, 280, 420])
MASK  = d("Mask",        0.90, [170, 190, 230, 240])
FULL  = [person(WORKER), HAT, VEST, MASK]

fails = []
def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")

print("1. geometry")
check("hardhat is inside worker", round(containment(HAT['box'], WORKER), 2), 1.0)
check("outside box scores 0",     containment([900,900,950,950], WORKER), 0.0)

print("2. single frame, votes capped to frame count")
r = evaluate([FULL], cfg)
check("all three present -> PASS", r.status, "PASS")
check("every item ok",             [i.ok for i in r.items], [True, True, True])
check("votes_required capped to 1", r.items[0].votes_required, 1)

print("3. missing item fails closed")
r = evaluate([[person(WORKER), HAT, MASK]], cfg)
check("no vest -> FAIL", r.status, "FAIL")
check("vest row is NG",  [i.ok for i in r.items], [True, False, True])

print("4. ASSOCIATION: a bystander's PPE must not count for the worker")
BYSTANDER = person([600, 100, 700, 400], 0.88)          # smaller = further away
THEIR_VEST = d("Safety Vest", 0.95, [610, 200, 690, 330])
r = evaluate([[person(WORKER), HAT, MASK, BYSTANDER, THEIR_VEST]], cfg)
check("bystander vest ignored -> FAIL", r.status, "FAIL")
check("primary is the larger person",   r.person_box, [100.0, 100.0, 300.0, 600.0])
check("extra person reported",          r.extra_people, 1)

print("5. VOTING across a 5-frame burst (needs 2)")
burst_1 = [FULL] + [[person(WORKER), VEST, MASK]] * 4          # hat in 1 of 5
r = evaluate(burst_1, cfg)
check("hat in 1/5 -> NG", r.items[0].ok, False)
check("votes counted",    r.items[0].votes, 1)
check("threshold is 2",   r.items[0].votes_required, 2)

burst_2 = [FULL, FULL] + [[person(WORKER), VEST, MASK]] * 3    # hat in 2 of 5
r = evaluate(burst_2, cfg)
check("hat in 2/5 -> OK", r.items[0].ok, True)
check("overall PASS",     r.status, "PASS")

print("6. no worker in frame")
r = evaluate([[HAT, VEST, MASK]], cfg)
check("no Person -> NO_WORKER", r.status, "NO_WORKER")
check("not passed",             r.passed, False)
r = evaluate([], cfg)
check("no frames -> NO_WORKER",  r.status, "NO_WORKER")

print("7. negative veto: NO-Hardhat outscores Hardhat")
WEAK_HAT = d("Hardhat", 0.45, [150, 110, 250, 180])
NO_HAT   = d("NO-Hardhat", 0.80, [150, 110, 250, 180])
r = evaluate([[person(WORKER), WEAK_HAT, NO_HAT, VEST, MASK]], cfg)
check("vetoed -> NG",       r.items[0].ok, False)
check("veto source recorded", r.items[0].vetoed_by, "NO-Hardhat")
# ...but a confident positive beats a weak negative
r = evaluate([[person(WORKER), HAT, d("NO-Hardhat", 0.50, HAT["box"]), VEST, MASK]], cfg)
check("strong positive wins", r.items[0].ok, True)

print("8. low-confidence detections are ignored")
r = evaluate([[person(WORKER), d("Hardhat", 0.30, HAT["box"]), VEST, MASK]], cfg)
check("hat below conf -> NG", r.items[0].ok, False)

print("9. a disabled item is shown but does not decide the verdict")
import copy
cfg_off = copy.deepcopy(cfg)
cfg_off["items"][2]["enabled"] = False                 # Mask switched off
r = evaluate([[person(WORKER), HAT, VEST]], cfg_off)   # no mask on the worker
check("missing DISABLED item -> still PASS", r.status, "PASS")
check("disabled row is still reported",      len(r.items), 3)
check("disabled row still shows its own NG", r.items[2].ok, False)
check("disabled row flagged enabled=False",  r.items[2].enabled, False)
check("enabled rows flagged enabled=True",   [i.enabled for i in r.items[:2]], [True, True])
r = evaluate([[person(WORKER), HAT]], cfg_off)         # vest ALSO missing -> real fail
check("missing ENABLED item still fails",    r.status, "FAIL")
check("absent key defaults to enabled",      all(i.enabled for i in evaluate([FULL], cfg).items), True)

print("10. nothing enabled fails closed — never a vacuous PASS")
cfg_none = copy.deepcopy(cfg)
for it in cfg_none["items"]: it["enabled"] = False
r = evaluate([FULL], cfg_none)                          # fully compliant worker...
check("all items off -> not passed", r.passed, False)   # ...still cannot pass
check("all items off -> FAIL",       r.status, "FAIL")

print("11. scores seen on the worker are recorded, whatever the conf, and change nothing")
r = evaluate([[person(WORKER), d("Hardhat", 0.30, HAT["box"]), d("NO-Hardhat", 0.22, HAT["box"]),
               VEST, MASK]], cfg)
check("below-conf hat: still NG",      r.items[0].ok, False)
check("its score is kept",             r.items[0].seen_score, 0.3)
check("the NO- class score is kept",   r.items[0].neg_score, 0.22)
check("nothing seen -> None",          (r.items[1].neg_score, r.items[2].neg_score), (None, None))
r = evaluate([[person(WORKER), HAT, VEST, MASK, d("Hardhat", 0.95, [900, 900, 950, 950])]], cfg)
check("off-worker detections ignored", r.items[0].seen_score, HAT["score"])
r = evaluate([[d("Hardhat", 0.9, HAT["box"])]], cfg)
check("no worker -> no item rows, so no scores", r.items, [])

print()
if fails:
    print(f"{len(fails)} FAILURE(S):")
    for f in fails: print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
