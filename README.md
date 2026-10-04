# PPE Gate Kiosk — Jetson test package (Phase 1)

Worker taps an RFID card → a frame is captured → YOLO checks the PPE checklist →
the screen shows OK/NG per item and an overall Pass/Fail.

**This package is Phase 1: the check engine and the screen.** The RFID trigger and the
live RTSP capture are Phase 2 — here you drive it by choosing image files from a test
bar, which exercises exactly the same decision code that will run on live frames.

---

## Does it need internet?

**No — not to run.** The page is fully self-contained: no CDN, no web fonts, no external
assets. Every request the browser makes is to this server. Inference is local.

Verified, not assumed: the full model-load + inference + decision path was run under
`docker run --network none` (no networking of any kind) and completed normally.

Internet is needed **once**, to pull the ~7.7 GB ARM64 base image before building
`ppe-safety:jetson`. After that the device can be offline permanently. If the Jetson has
no internet at all, see the `--offline` route in the main project's `package_for_jetson.sh`.

---

## Contents

```
ppe-gate-jetson/
├── run_gate_native.sh        # start here on JetPack 6 — no Docker
├── run_gate_jetson.sh        # the same kiosk, inside the container
├── Dockerfile.jetson         # builds ppe-safety:jetson ON the Jetson
├── config/gate.json          # checklist + thresholds (edit freely)
├── models/ppe.pt             # trained YOLOv8m, mAP50 0.880
├── models/data.yaml          # class names
├── samples/                  # 5 test images (some pass, some fail)
└── scripts/
    ├── gate_server.py        # web server + kiosk UI
    ├── ppe_check.py          # decision logic (no camera/model/UI deps)
    ├── test_ppe_check.py     # 21 assertions, no GPU needed
    └── export_trt.py         # one-time TensorRT engine build
```

---

## Setup

Two routes to the same kiosk. **On a JetPack 6 device, take Route A** — JetPack already
ships NVIDIA's CUDA torch, ultralytics, TensorRT and OpenCV, so the container buys you
nothing there but a 7.7 GB pull and a `docker` group membership. Take Route B when you
want the pinned, reproducible environment, or on a device whose JetPack is incomplete.

---

## Route A — native (no Docker)

### A1. Check the environment

```bash
python3 -c "import torch, torchvision, ultralytics, tensorrt; \
  print(torch.__version__, torchvision.__version__, tensorrt.__version__, torch.cuda.is_available())"
```

Expect something like `2.5.0 0.20.0 10.3.0 True`. Verified on this device: Orin NX,
JetPack 6 / L4T R36.4.0, torch 2.5.0 + CUDA 12.6, ultralytics 8.3.28, TensorRT 10.3.0.

If that line raises `operator torchvision::nms does not exist`, torchvision is built
against the wrong C++ ABI — see Troubleshooting. It is the one thing that reliably breaks
here, and it breaks **every** inference, because ultralytics runs NMS through
`torchvision.ops.nms`.

### A2. (Optional but recommended) Build the TensorRT engine

Several times faster. Must be built **on this device** — engines are tied to the specific
GPU and TensorRT version.

```bash
python3 scripts/export_trt.py --model models/ppe.pt --half
```

Produces `models/ppe.engine`, which `run_gate_native.sh` then picks up automatically.

### A3. Run

```bash
./run_gate_native.sh            # port 8100
```

The launcher preflights torch/torchvision/ultralytics and reports the CUDA device before
loading the model, so a broken environment fails in a second rather than after the ~30 s
model load. With the `.pt` weights expect roughly 100 ms per frame; the first frame is
slower while the model warms.

---

## Route B — container

### 1. Build the container image (once, on the Jetson)

**Build it on the Jetson, never on an x86 PC** — Docker will stamp an x86 image as arm64
and it dies on the device with `exec format error`.

```bash
cd ~/ppe-gate-jetson
docker pull ultralytics/ultralytics:latest-jetson-jetpack6      # ~7.7 GB, needs internet
docker build -f Dockerfile.jetson -t ppe-safety:jetson .

# Confirm it is genuinely ARM64 and has TensorRT:
docker run --rm ppe-safety:jetson python3 -c "import platform; print(platform.machine())"
# MUST print: aarch64
docker run --rm --runtime=nvidia ppe-safety:jetson \
  python3 -c "import tensorrt, torch; print(tensorrt.__version__, torch.cuda.is_available())"
```

### 2. (Optional but recommended) Build the TensorRT engine

Several times faster. Must be built **on this device** — engines are tied to the specific
GPU and TensorRT version.

```bash
docker run --rm --runtime=nvidia -v "$PWD:/app" -w /app ppe-safety:jetson \
  python3 scripts/export_trt.py --model models/ppe.pt --half
```

Produces `models/ppe.engine`, which `run_gate_jetson.sh` then picks up automatically.

### 3. Run

```bash
./run_gate_jetson.sh            # port 8100
```

Open **http://localhost:8100** on the Jetson, or `http://<jetson-ip>:8100` from a laptop
on the same network.

---

## Using it

- **Choose image(s)…** runs a check. Select **several images at once** to simulate the
  multi-frame burst the live version will capture — this is the interesting case.
- **H** hides the test bar so you see the screen as an operator would.
- `samples/image_53_*.jpg` and `samples/image_55_*.jpg` **pass**; the other four **fail**
  on Vest and/or Mask.

### Why so few passes?

Across all 82 held-out test images, only 2 pass the full Hardhat+Vest+Mask checklist. That
is mostly **not** a model problem — it is what the images actually show. Ground truth for
those images:

| item | worn | not worn | compliance |
|---|---|---|---|
| Hardhat | 110 | 41 | 73% |
| Safety Vest | 61 | 90 | 40% |
| Mask | 28 | 79 | 26% |

A perfect model would pass roughly 8% of them; we measure 3%, and the gap is the recall
shortfall that voting is there to close. Relaxing the checklist raises the rate as you would
expect — Hardhat+Vest passes 25%, Hardhat alone 47%.

The population at your gate is different: workers who turn up compliant. But it does mean
**including Mask will reject anyone not wearing one**, which is correct behaviour and worth
confirming is what you want before going live.

### The rule

An item is **OK** only when it is positively detected **on the worker**. Two details
matter:

- **Association.** The largest `Person` box is taken as the worker (largest = closest to
  the camera). A PPE box counts only if at least 50% of it lies inside that person, so a
  bystander's hardhat cannot satisfy the checklist for whoever is tapping in. Other people
  in frame are flagged on screen.
- **Voting.** An item must appear in at least `votes_required` of the captured frames.
  This exists because per-item recall multiplies: measured single-frame odds of catching
  all three items are only about 66%, so one frame per tap would falsely reject roughly a
  third of compliant workers.

Missing detection is always a **fail**, never a pass.

---

## Switching model — the model browser

The test bar has a **⚙ model:** button showing the model in use. It opens a browser
listing every `.pt`, `.engine` and `.onnx` in the models directory (`--models-dir`,
default: alongside `--model`); click one to make it live. Loading takes a second or two
while the server is warm, and checks are refused with a 503 until the new model is
serving, so a successful switch means the next check really used it.

**What is actually checked is the class list, not the architecture.** Ultralytics loads
YOLOv3, v5, v6, v8, v9, v10, v11 and RT-DETR through the same call, so the family is
irrelevant here. What matters is that the model provides every class named in
`config/gate.json` — currently `Hardhat`, `Mask`, `NO-Hardhat`, `NO-Mask`,
`NO-Safety Vest`, `Person`, `Safety Vest`. A model that lacks one is **rejected with a
409** listing exactly what is missing.

That guard is the point of the feature. A stock COCO model loads perfectly and has
`person` but no `Hardhat`, so without the check it would mark every item NG forever and
the screen would look like a stream of genuinely non-compliant workers. Matching is
case-sensitive, which is why COCO's lowercase `person` does not satisfy `Person`.

### Adding a model from another machine

**Choose model file…** in the same sheet opens the browser's native file dialog filtered
to `.pt`, `.engine` and `.onnx`, with a progress bar while it uploads.

It uploads, rather than pointing the server at a path, because it has to: a file input
hands the page the file's *bytes* and never its location — every browser refuses to
disclose that. So this is the way to push a model from your laptop to the gate without
scp, but picking a file that already sits in the models directory just sends it back to
the machine it came from. Use the list for those.

What happens to the upload depends on how it fails:

| Outcome | Result |
|---|---|
| Loads, has every required class | Saved and switched to |
| Loads, missing a class | **Saved** and listed, but not activated — it may suit a different `gate.json` |
| Not a loadable model at all | **Discarded**, so corrupt uploads cannot litter the browser |
| Same name as the model in use | Refused — switch away first |

Uploads are streamed to disk in 1 MB chunks and land via a `.part` file renamed on
completion, so a dropped connection cannot leave a half-written model that later loads
as garbage. The size ceiling is `--max-model-mb` (default 512).

Three flags shape it:

    --models-dir DIR   directory the browser lists (default: alongside --model)
    --max-model-mb N   largest accepted upload (default 512)
    --lock-model       browse but do not switch or upload; both routes return 403

Use `--lock-model` for a gate in production, where nobody on the LAN should be able to
change the detector. Note that a `.engine` is tied to the exact GPU and TensorRT version
it was built on, so an engine copied from another machine will fail to load.

API: `GET /api/models` lists them, `POST /api/model {"file": "ppe.engine"}` switches,
`POST /api/model_upload?name=<file>` takes the raw bytes as the body.
Add `"force": true` to override the class check — it is logged loudly and should be
reserved for testing a model you know is incomplete.

## Realtime collection mode

**Realtime** in the test bar turns on continuous detection: live boxes, a per-frame OK/NG
summary, and training frames harvested while somebody stands in front of the camera. It
is a *sampling session*, not a gate event, and differs from a trigger deliberately:

| | trigger (CAPTURE & CHECK) | realtime |
|---|---|---|
| spoken result | yes | **no** |
| `captures.csv` row | yes | **no** |
| image + YOLO label saved | `dataset/` | `dataset/realtime/` |
| verdict from | a voted burst of frames | **one frame, judged alone** |
| frames with no worker | discarded | discarded |

The summary is judged frame by frame, so the badges track exactly what the model sees
now, including its instability — a marginal detection visibly flickers. That is the point:
it shows you where the model is weak, which is what you are collecting data to fix.

    --realtime-interval S   seconds between saved samples (default 2.0)
    --realtime-max N        samples kept before the oldest are deleted (default 5000)

**Why an interval at all:** saving every processed frame is about 6 GB/hour — it would
fill this device in a day, and consecutive frames are near-duplicates that add almost no
training value. At the default 2 s it is ~300 MB/hour, and 5000 samples is ~860 MB.

The interval spaces out *samples*, not poll attempts: an empty gate leaves the throttle
ready, so the first frame with a worker in it is captured immediately rather than up to
one interval later.

**Pruning is independent of `captures.csv`.** The trigger recorder prunes by reading that
file; realtime writes no rows, so it prunes its own folder oldest-first by filename
(names are timestamp-prefixed, so lexical order is chronological). Without that, nothing
would ever bound it.

The browser is shown a lower-quality preview (`REALTIME_PREVIEW_QUALITY`, several frames
a second) while the frame written to disk is encoded separately at full
`CAPTURE_JPEG_QUALITY` — **training data never inherits the preview's compression.** Boxes
are drawn on the exact frame they were computed from, so the overlay always lines up.

## Collected capture data

Every gate trigger — the **CAPTURE & CHECK** button today, the through-beam sensor when
it is fitted — saves what it saw, so the deployment builds its own training set. Only
triggers are recorded; images uploaded through *Choose image(s)…* are test inputs the
gate never saw and are deliberately skipped.

**A trigger that detects no worker still saves the image, but to a separate folder and
with no label file:**

    dataset/images/  + labels/        worker found: training data
    dataset/no_person/images/         no worker: image only, for review

The missing `.txt` is the point. An empty label is not "no information" to YOLO — it
asserts the entire frame is background, so if a worker really was there and the model
missed them, a label file would train that miss straight back in. Withholding it makes
`no_person/` a review pile that cannot be merged into training until a human has looked at
the frames and labelled anything real.

Both kinds get a `captures.csv` row, so the log stays a complete record of what the gate
did, and pruning follows whatever path each row names — it spans both folders.

    ppe-gate-jetson/
      captures.csv                 one row per trigger, newest last
      dataset/
        images/ 20260902_230446_18643a.jpg
        labels/ 20260902_230446_18643a.txt

`dataset/` is the layout Ultralytics expects — it finds labels by swapping `/images/`
for `/labels/` — so the folder can be pointed at directly from a `data.yaml` and merged
with the Roboflow set. Class indices match `models/data.yaml`.

The image is the **exact JPEG the burst already encoded** (1280x960, q88, ~172 KB), so
recording costs a disk write and no second encode, and writes happen on a background
thread rather than inside the ~0.95 s tap.

### The CSV

| column | |
|---|---|
| `timestamp` | when the trigger fired |
| `image`, `label` | paths relative to this directory |
| `worker_id` | from the RFID reader, **blank** when nothing was read |
| `status` | `PASS` / `FAIL` / `NO_WORKER` |
| one pair per item | `Hardhat` = `OK`/`NG`, `Hardhat_votes` = `2/5` |
| `extra_people` | others in frame besides the one judged |
| `person_box` | the person the checklist actually judged |
| `boxes` | how many labels were written |

**No count limit by default.** At the measured 172 KB/capture, even years of gate
traffic stays well inside the 71 GB free on this device, so the record count was never
really the thing worth bounding — pass `--record-max N` to opt back into a FIFO ring
that evicts the oldest row (and **deletes the image and label it pointed at**) once N is
reached, if you want one anyway. At the
measured 172 KB per capture, 1000 records is about 170 MB.

The `_votes` columns exist for picking what to hand-label first. A split vote is the
model disagreeing with itself, which is worth far more correction effort than the
hundredth clean PASS.

### Which boxes go in the label file

By default **every detection above `--label-conf` (0.4)** is written, not just the
worker being judged. This matters: on one sample frame here, the default writes 18
boxes covering all six people, while `--label-primary-only` writes 3. Those two files
train very different models — the second tells YOLO that five hardhats and four missing
vests are background, because anything unlabelled is a negative.

Use `--label-primary-only` only if the gate genuinely frames one person at a time. The
CSV records the judged person's box either way, so the checklist audit trail does not
depend on this choice.

    --record-dir DIR        where captures.csv and dataset/ go (default: package root)
    --record-max N          captures kept before the oldest is deleted (default 1000)
    --label-conf F          minimum score to be written as a label (default 0.4)
    --label-primary-only    label only the closest person and their PPE
    --no-record             do not collect anything

These are pseudo-labels from the model being improved, so they need human review before
retraining — otherwise the model is only being taught to agree with itself.

## Spoken result

Every finished check announces itself from `MP3/`:

| result | clip |
|---|---|
| `PASS` | `check_pass.mp3` |
| `FAIL` | `check_fail.mp3` |
| `NO_WORKER` | `person_detect_fail.mp3` |

`NO_WORKER` gets its own clip rather than sharing `check_fail.mp3`. The trigger fired,
so somebody was at the gate and the model did not find them — a different problem for
whoever is listening to act on than "found a worker, PPE missing": one says look at the
camera or the gate, the other says look at the worker.

**Playback happens on the Jetson, not in the page.** The through-beam sensor will start
checks with no user gesture behind them, and that is exactly the case browsers block
autoplay in. The player is `gst-launch-1.0 playbin`, chosen because GStreamer is already
a dependency (the camera pipeline) and ships `avdec_mp3`: this box has no `mpg123`,
`ffplay` or `mpv`, and its SoX was built without an MP3 handler, so `play` cannot read
these files.

Clips are queued to a background thread, so a check never waits on audio. If a clip runs
past `--audio-timeout` (default 10 s) it is abandoned rather than blocking the next one.
A missing clip logs and is skipped; a missing player disables announcements at startup
with a warning rather than failing every check.

    --mp3-dir DIR       where the clips live (default: <package>/MP3)
    --audio-timeout S   abandon a clip that has not finished (default 10)
    --no-audio          stay silent

`MP3/` also holds `no_hardhat.mp3`, `no_safty_harness.mp3`, `no_bodycam.mp3`,
`multi-person-detected.mp3` and `RFID_fail.mp3`, which are **not wired up yet** — only
the pass/fail pair is. Announcing which item failed, or that extra people were in frame,
is a small addition to `announce()`.

## Records viewer

**See Records** in the test bar opens every row of `captures.csv` in a scrollable
table — timestamp, worker ID (blank when RFID read nothing), PASS/FAIL per item with
its vote count, extra people, box count, and a **View** button that opens the exact
saved image. Newest capture first. **Refresh** re-reads the CSV; nothing here auto-updates,
since a check running mid-scroll would be worse than a stale table.

`GET /api/records` returns the same data as JSON — `{columns, items, rows, count, csv,
record_max}` — for scripting against instead of the browser table.
`GET /api/record_image?file=<path>` serves one saved capture. It is confined to an
explicit allow-list of the three folders captures are written to (`dataset/images`,
`dataset/no_person/images`, `dataset/realtime/images`), so **View** works for review
images as well as training ones, while labels, configs and anything outside those folders
stay unreachable.

The table renders **100 rows per page** with First/Prev/Next/Last controls. That cap is
not cosmetic: `--record-max` now defaults to no limit, and building a few thousand table
rows at once is enough to lock up the kiosk browser on this device. Searching returns to
page 1, so a filter can never leave you on a page that no longer exists.

Worker IDs can arrive from off-network sources (`scripts/rfid.py`'s TCP/UDP/serial
listeners, or a raw POST to `/api/capture`), so every field is HTML-escaped before it
reaches the table rather than trusted the way an operator-typed value would be.

## Detection overlay: labels and the background-people filter

Every box drawn on a result carries a tag: **class name + confidence score**, and for
**people only**, the box area in px² as well. Area appears on person boxes alone because
that is the only place it means anything — it is the distance proxy the background filter
below works on, so on a hardhat it would just be noise. The
primary person's tag sits above their box; a PPE item's tag sits below its own box
specifically so it does not land on top of the person's tag — a hardhat's top edge is
usually only a few pixels from the person box's own top edge, so two "above" tags there
would otherwise stack on the same spot.

A slider under the test bar — `10,000 [====] 409,600 px²` — filters which **other**
detected people get drawn. Box area is a stand-in for distance from the camera: someone
in the background is smaller on screen than the worker actually at the gate, so raising
the threshold hides them one by one as their box shrinks below it. It redraws on every
drag, with no round trip to the server. Its ceiling is always the current frame's own
pixel area (`width x height`), not a guessed constant, so it stays correct if the
capture resolution ever changes.

**The primary person — the one the checklist is judging — is never hidden by this
slider**, regardless of value; only bystanders are subject to it. It hides with the test
bar (press H) since it is a display aid, not part of the pass/fail result.

## Remapping a model's class names to the checklist

The three item cards each carry two dropdowns — **Detect** (the class that satisfies
the item) and **Veto** (its NO-* counterpart, or *"ignored — no veto"*). They offer the
**active model's own class list**, so a mapping can never name a class the model cannot
see. Choosing one applies immediately and is **written back to `config/gate.json`**, so
it survives a restart exactly as though the file had been edited by hand.

Each card also carries a **Score ≥** threshold as a slider *and* a matching textbox —
two views of one value, so you can drag to explore or type an exact figure. Dragging
repaints the overlay live (the same threshold decides which boxes are drawn), and the
save to `config/gate.json` fires on release rather than on every pixel of the drag. It
is per-item because the right threshold differs by object: measured on one sample frame,
a hardhat scoring 0.912 passes at 0.40 and is correctly rejected at 0.95.

Above them sits **Worker class**, which is just as essential and easy to overlook:
association hangs off that single class name, so if the config says `Person` and the model
calls it `person`, **every check returns `NO_WORKER`** no matter how well the three items
are mapped.

This is what lets a model trained by someone else — with its own vocabulary — drive this
checklist without editing anything: set Worker class to their `person`, point `Hardhat` at
their `helmet`, `Safety Harness` at their `vest`, and set Veto to *ignored* for any item
their model has no negative class for. An item is NG whenever its Detect class isn't seen
in enough frames, veto or not.

### Switching to a model whose classes don't match yet

There is a deadlock to be aware of: a model is refused while the checklist still names
classes it doesn't have, but the dropdowns can only offer the **active** model's classes.
So clicking such a model reports what is missing and then offers to **switch anyway** —
after which every item reads NG (and shows `⚠ <class> — not in this model`) until you
repoint it. The alternative, if you'd rather not run mismatched for even a moment, is to
attach a class-list yaml renaming that model into your existing vocabulary *before*
switching to it.

### Attaching a class-list yaml to a model

Each row in the model browser shows its paired class-list yaml and that model's classes.
A yaml pairs with a model by **filename** — `helmet_v2.pt` pairs with `helmet_v2.yaml` —
and **Attach/Replace class list (.yaml)…** on the row uploads one, writing exactly that
same-stem filename, so attaching and auto-pairing are one mechanism rather than two.

A paired yaml **renames the model's classes by index**, which is how a foreign model gets
relabelled into this checklist's vocabulary. Verified end to end: a 5-class model whose
own names are `helmet, no-helmet, no-vest, person, vest`, given a yaml naming them
`Hardhat, NO-Hardhat, NO-Safety Vest, Person, Safety Vest`, returns **`Hardhat`** and
**`Safety Vest`** in real detections — no remapping needed at all.

**A yaml is only applied when its class count matches the model's.** A mismatch means the
wrong yaml is paired, and renaming by index anyway would silently mislabel every
detection rather than fix anything — so it is refused, with the reason reported, and the
model keeps its own names. Class *order* still has to be right; the count check catches
the wrong file, not a scrambled one.

The browser reads a paired yaml **without ever opening the model file**. That keeps the
listing instant, and avoids a real hazard: loading a bare `.onnx` on this device made
ultralytics try to auto-install `onnxruntime-gpu`, and in failing it downgraded numpy and
broke `cv2`/`torch` (see the numpy pin note). So an un-paired model simply shows no class
preview — the active model is the exception, since its names are already in memory.

Both remapping and yaml attachment are disabled by `--lock-model`, alongside model
switching, since all three change what the gate detects.

## Positional constraint (optional, off by default)

Containment only asks whether a PPE box sits *inside* the worker, not *where*. So a
hardhat carried in the hand, clipped to a belt, or resting on a bench beside the worker
currently satisfies the Hardhat item. `max_center_y` closes that:

```json
{ "label": "Hardhat", "classes": ["Hardhat"], "negatives": ["NO-Hardhat"],
  "conf": 0.4,
  "max_center_y": 0.35 }
```

The detection's vertical **centre** must fall within that fraction of the person's box,
measured from the top (`0.0` = top of the person, `1.0` = bottom). The centre is used
rather than the top edge because it barely moves when the box jitters, and it is
normalised by person height so it reads the same close to the camera or far from it.

**Absent from an item means the constraint is off**, which is the default everywhere. But
the position is *measured and logged on every check regardless* — in the console line and
as a `<item>_cy` column in `captures.csv` — so the threshold can be chosen from real gate
data instead of guessed.

### Choosing the number

Measured on the bundled samples: worn hardhats sit at **0.05–0.16**, while a hardhat held
at waist height computes to ~0.63. A threshold of 0.35 sits in that gap with roughly 2x
margin either side.

**But one bundled sample fails at 0.35** — a lineman with both arms raised, whose person
box stretches well above his head, putting a correctly worn hardhat at 0.543. That is the
rule's real weakness: it assumes the top of the person box is roughly the head, which
holds for someone walking upright at a gate and breaks for raised arms or a bent pose.

So collect first, then enable. Run normally for a few hundred captures, open the `_cy`
column, and pick a threshold above everything a *correctly worn* item produced at your
camera angle. Until you do, the field stays absent and nothing changes.

### Enabling it

Stop the gate, edit `config/gate.json`, add `"max_center_y"` to the item, restart:

    ./run_gate_native.sh          # stop with Ctrl-C first

Stop it first because **the UI rewrites this file**: the dropdowns and sliders persist
through `save_config()`, so an edit made while the gate is running is lost the next time
someone touches a control. Extra fields like this one survive that rewrite once saved,
because the whole config structure is round-tripped.

It also generalises — `max_center_y` works on any item, so a torso-mounted bodycam or a
vest could get its own band once there is data to justify one.

## Configuration — `config/gate.json`

```jsonc
{
  "items": [ { "label": "Hardhat", "classes": ["Hardhat"],
               "negatives": ["NO-Hardhat"], "conf": 0.40 }, ... ],
  "person_conf": 0.40,      // minimum score to accept a Person box
  "frames": 5,              // burst size Phase 2 will capture
  "votes_required": 2,      // frames an item must appear in (capped at frames supplied)
  "containment": 0.5,       // fraction of a PPE box that must sit inside the worker
  "negative_veto": true     // NO-* outscoring the positive forces NG
}
```

Restart the server after editing.

**On thresholds:** measured on the 82-image held-out split, positive-class precision is
0.96–1.00 at conf 0.40, so a false OK is rare. Recall is the weaker side (Mask 0.79 is the
binding constraint), which is what voting compensates for. Those numbers come from
construction scenes full of small, distant people — **your gate sees one close, centred
worker, so real recall should be better, but by an unknown amount.** Capture 50–100 frames
from the DINION at its final mounting position and re-measure before fixing production
thresholds.

Note the checklist is **Hardhat / Vest / Mask**. BodyCam is not a class this model knows and
cannot be detected at any threshold; adding it needs annotated images and a retrain.

---

## Checking the logic

```bash
python3 scripts/test_ppe_check.py                        # native

docker run --rm -v "$PWD/scripts:/app/scripts:ro" -w /app ppe-safety:jetson \
  python3 scripts/test_ppe_check.py                      # container
```

21 assertions covering association, both sides of the voting threshold, the negative veto,
empty frames and sub-threshold detections. No GPU or model needed. Run it after editing
`ppe_check.py`.

---

## Troubleshooting

**`RuntimeError: operator torchvision::nms does not exist`** (native route) — torchvision's
`_C.so` never loaded. The generic PyPI aarch64 wheel is built with
`_GLIBCXX_USE_CXX11_ABI=0`, NVIDIA's torch with `=1`, so the symbols never resolve; `pip`
sees a version pair that matches on paper and installs it happily. Install the CXX11-ABI
build instead:

```bash
pip install --force-reinstall --no-deps \
  https://download-r2.pytorch.org/whl/cu124/torchvision-0.20.0-cp310-cp310-linux_aarch64.whl
```

`--no-deps` is not optional — without it pip pulls a generic torch over NVIDIA's CUDA
build and you lose the GPU. Confirm the fix with
`python3 -c "import torch,torchvision;from torchvision.ops import nms;print(torch.cuda.is_available())"`.

**`could not select device driver "nvidia"`** — something added `--gpus all`. On Jetson use
`--runtime=nvidia` alone; `--gpus all` is the desktop/datacenter flag.

**`exec format error`** — the image was built on an x86 PC. Rebuild it on the Jetson.

**`ModuleNotFoundError: tensorrt`** — built from `ultralytics/ultralytics:latest` instead of
the `latest-jetson-jetpack6` tag. `latest` is an amd64 desktop image with no TensorRT.

**Page loads but every check says `No worker detected`** — no `Person` box scored above
`person_conf`. Lower it in `config/gate.json`, or check the image actually shows a person.

**Slow (seconds per frame)** — running the `.pt` weights instead of TensorRT. Do step 2.
