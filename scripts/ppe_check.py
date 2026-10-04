"""
PPE gate check — decision logic, independent of camera, model and UI.

The rule is FAIL-CLOSED: an item passes only when it is positively detected on the
worker. A missing detection is a fail, never a pass. Measured on the 82-image test
split, positive-class precision is 0.96-1.00 at conf 0.40, so a false OK is rare;
recall is the weaker side, which is why votes_required exists (see below).

Two things this module exists to get right:

1. ASSOCIATION. The model returns every box in the frame with no notion of who is
   wearing what. With two people at the gate, a compliant bystander's hardhat would
   otherwise satisfy the checklist for the worker tapping in. So one primary Person
   is chosen (largest box = closest to the camera) and a PPE box only counts when it
   sits inside that person.

2. VOTING. Per-item recall multiplies across the checklist: at conf 0.40 the measured
   single-frame chance of catching all three items is only ~66%, so one frame per tap
   would falsely reject roughly a third of compliant workers. Requiring an item in
   >= votes_required frames of a short burst recovers recall, because a real hardhat
   appears in most frames while a false positive rarely repeats.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Iterable

DEFAULT_CONFIG = {
    # Each checklist row. `classes` are model labels that satisfy it; `negatives` are
    # the model's explicit NO-* counterparts, used only as a veto (see negative_veto).
    # An optional `enabled: false` keeps the row on the screen and in the data but
    # takes it OUT of the verdict — see evaluate(). Absent means enabled.
    "items": [
        {"label": "Hardhat", "classes": ["Hardhat"],      "negatives": ["NO-Hardhat"],      "conf": 0.40},
        {"label": "Vest",    "classes": ["Safety Vest"],  "negatives": ["NO-Safety Vest"],  "conf": 0.40},
        {"label": "Mask",    "classes": ["Mask"],         "negatives": ["NO-Mask"],         "conf": 0.40},
    ],
    "person_class": "Person",
    "person_conf": 0.40,
    # Burst size the capture layer should grab, and how many of those frames an item
    # must appear in. Capped at the number of frames actually supplied.
    "frames": 5,
    "votes_required": 2,
    # Fraction of a PPE box that must lie inside the primary person's box.
    "containment": 0.5,
    # If the NO-* counterpart outscores the positive class on this worker, call it NG
    # even though the positive fired. Guards against a hardhat detected on a hat that
    # the model separately flags as absent.
    "negative_veto": True,
}


def load_config(path: str | None) -> dict:
    """Read a config file, filling anything absent from DEFAULT_CONFIG."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))   # deep copy
    if path and os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
        cfg.update(user)
    return cfg


# ── geometry ──────────────────────────────────────────────────────────────
def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def center_y_fraction(box, person_box) -> float | None:
    """Vertical centre of `box` as a fraction of the person's height.

    0.0 is the top of the person box, 1.0 the bottom. Normalising by height is what
    makes the number comparable between a worker close to the camera and one further
    away. The CENTRE is used rather than the top edge because it barely moves when the
    box jitters by a few pixels.

    Note this assumes the top of the person box is roughly the head, which holds for
    someone walking upright at a gate but NOT for a raised arm or a bent-over pose —
    the reason any threshold on it wants to come from real data at the real camera
    angle rather than from intuition.
    """
    h = person_box[3] - person_box[1]
    if h <= 0:
        return None
    return ((box[1] + box[3]) / 2 - person_box[1]) / h


def containment(inner, outer) -> float:
    """Fraction of `inner`'s area that lies inside `outer`. 0 when inner is empty."""
    ix1, iy1 = max(inner[0], outer[0]), max(inner[1], outer[1])
    ix2, iy2 = min(inner[2], outer[2]), min(inner[3], outer[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    a = _area(inner)
    return inter / a if a > 0 else 0.0


# ── results ───────────────────────────────────────────────────────────────
@dataclass
class ItemResult:
    label: str
    ok: bool
    votes: int
    frames: int
    votes_required: int
    best_score: float = 0.0
    vetoed_by: str | None = None      # set when a NO-* class overruled a positive
    # The highest score the model gave this item's classes (seen_score) and its NO-*
    # classes (neg_score) on the worker in any burst frame, WHATEVER the item's conf —
    # observation only, for the record. best_score is 0 on an NG row by construction;
    # these say whether the model saw it at 0.62 under a 0.70 bar, or not at all (None).
    seen_score: float | None = None
    neg_score: float | None = None
    # Where this item sat on the worker, 0.0 = top of the person box, 1.0 = bottom.
    # Recorded on every check whether or not max_center_y is configured, so the
    # threshold can be chosen from real gate data instead of guessed.
    center_y: float | None = None
    # True when max_center_y is set AND a positive was rejected for being too low.
    position_rejected: bool = False
    # False when the operator switched this row out of the verdict (item["enabled"]).
    # ok/votes are still computed and reported so the screen and the CSV keep showing
    # what was actually seen — it just doesn't decide PASS/FAIL.
    enabled: bool = True


@dataclass
class CheckResult:
    passed: bool
    status: str                        # PASS | FAIL | NO_WORKER
    items: list[ItemResult] = field(default_factory=list)
    person_box: list[float] | None = None
    # Index of the primary person WITHIN `detections`. The UI needs to tell the judged
    # worker apart from bystanders, and matching on person_box coordinates cannot do it
    # reliably: Python rounds .25 half-to-even while JavaScript rounds half-up, so a box
    # at 196.25 becomes 196.2 here and 196.3 in the browser and never matches itself.
    person_index: int | None = None
    extra_people: int = 0              # others in frame; UI should warn
    frames: int = 0
    detections: list[dict] = field(default_factory=list)   # primary frame, for display

    def to_dict(self) -> dict:
        d = asdict(self)
        d["items"] = [asdict(i) if not isinstance(i, dict) else i for i in self.items]
        return d


# ── core ──────────────────────────────────────────────────────────────────
def primary_person(dets: Iterable[dict], cfg: dict):
    """Largest Person box above threshold — at a gate, largest means closest."""
    people = [d for d in dets
              if d["name"] == cfg["person_class"] and d["score"] >= cfg["person_conf"]]
    if not people:
        return None, 0
    people.sort(key=lambda d: -_area(d["box"]))
    return people[0], len(people) - 1


def evaluate(frames_dets: list[list[dict]], cfg: dict | None = None) -> CheckResult:
    """
    frames_dets: one list of detections per captured frame. Each detection is
                 {"name": str, "score": float, "box": [x1,y1,x2,y2]}.
    """
    cfg = cfg or DEFAULT_CONFIG
    n = len(frames_dets)
    if n == 0:
        return CheckResult(passed=False, status="NO_WORKER", frames=0)

    # The frame with the largest primary person is the one shown to the operator and
    # the one whose boxes are reported; voting still runs across every frame.
    best_idx, best_person, best_extra, best_area = 0, None, 0, -1.0
    for i, dets in enumerate(frames_dets):
        p, extra = primary_person(dets, cfg)
        a = _area(p["box"]) if p else -1.0
        if a > best_area:
            best_idx, best_person, best_extra, best_area = i, p, extra, a

    if best_person is None:
        return CheckResult(passed=False, status="NO_WORKER", frames=n,
                           detections=frames_dets[best_idx])

    need = min(cfg["votes_required"], n)
    results: list[ItemResult] = []

    for item in cfg["items"]:
        votes, best_score, veto = 0, 0.0, None
        center_y, position_rejected = None, False
        seen_score = neg_score = None

        for dets in frames_dets:
            person, _ = primary_person(dets, cfg)
            if person is None:
                continue
            box = person["box"]
            on_worker = [d for d in dets
                         if containment(d["box"], box) >= cfg["containment"]]

            for d in on_worker:
                if d["name"] in item["classes"]:
                    seen_score = max(seen_score or 0.0, d["score"])
                elif d["name"] in item.get("negatives", []):
                    neg_score = max(neg_score or 0.0, d["score"])

            candidates = [d for d in on_worker
                          if d["name"] in item["classes"] and d["score"] >= item["conf"]]
            neg = [d for d in on_worker
                   if d["name"] in item.get("negatives", []) and d["score"] >= item["conf"]]

            # Observe before filtering: the position of the best candidate is recorded
            # even when the constraint then rejects it, because a rejected value is
            # exactly the data needed to tell a real violation from a bad threshold.
            if candidates:
                best_cand = max(candidates, key=lambda d: d["score"])
                cy = center_y_fraction(best_cand["box"], box)
                if cy is not None and (center_y is None or cy < center_y):
                    center_y = cy

            # Optional positional constraint. Absent from the config means off, which is
            # the default: a hardhat must be ON THE HEAD, not merely inside the person's
            # box, so a hat carried in the hand or clipped to a belt should not satisfy
            # the item. Only applied when the item explicitly opts in.
            max_cy = item.get("max_center_y")
            if max_cy is None:
                pos = candidates
            else:
                pos = []
                for d in candidates:
                    dcy = center_y_fraction(d["box"], box)
                    if dcy is None or dcy <= max_cy:
                        pos.append(d)
                if candidates and not pos:
                    position_rejected = True

            if not pos:
                continue
            top_pos = max(pos, key=lambda d: d["score"])
            top_neg = max(neg, key=lambda d: d["score"]) if neg else None

            if cfg["negative_veto"] and top_neg and top_neg["score"] > top_pos["score"]:
                veto = top_neg["name"]
                continue

            votes += 1
            best_score = max(best_score, top_pos["score"])

        results.append(ItemResult(
            label=item["label"], ok=votes >= need, votes=votes, frames=n,
            votes_required=need, best_score=round(best_score, 3), vetoed_by=veto,
            seen_score=None if seen_score is None else round(seen_score, 3),
            neg_score=None if neg_score is None else round(neg_score, 3),
            center_y=None if center_y is None else round(center_y, 3),
            position_rejected=position_rejected,
            enabled=bool(item.get("enabled", True))))

    # Only enabled rows decide the verdict. Disabled rows were still evaluated above so
    # the operator can see what the model saw, but a row the operator has switched off
    # cannot fail the worker — that is the whole point of the switch (an item mapped to
    # a class the active model doesn't have would otherwise make EVERY check fail).
    #
    # Still fail-closed: a checklist with NOTHING enabled does not pass anyone.
    # `all()` of an empty list is True, and that vacuous truth would turn a
    # misconfigured gate into an open one. The API refuses to disable the last item;
    # this guards the file being hand-edited into that state anyway.
    judged = [r for r in results if r.enabled]
    passed = bool(judged) and all(r.ok for r in judged)
    return CheckResult(
        passed=passed,
        status="PASS" if passed else "FAIL",
        items=results,
        person_box=[round(v, 1) for v in best_person["box"]],
        # Identity, not equality — best_person IS one of these dicts.
        person_index=next((i for i, d in enumerate(frames_dets[best_idx])
                           if d is best_person), None),
        extra_people=best_extra,
        frames=n,
        detections=frames_dets[best_idx],
    )
