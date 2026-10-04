# Codebase guide

For the engineer who inherits this system. The [README](README.md) covers *operating*
the gate; this file covers *changing* it — what the parts are, why they are shaped the
way they are, and which decisions will bite you if you undo them without knowing why.

---

## What this system is

A worker arrives at a gate. A frame is captured from a fixed camera, YOLO detects PPE,
and a kiosk screen shows OK/NG per item plus an overall Pass/Fail. It runs natively on a
Jetson Orin NX (no Docker), serving a web page on port 8100 that doubles as the kiosk
display and the maintenance UI.

Everything is local. No internet is needed to run it, and the page pulls no CDN, no web
fonts, and no external anything — that is deliberate, because the gate must keep working
when the factory network does not.

---

## Layout

```
ppe-gate-jetson/
├── run_gate_native.sh     ← START HERE. The way the gate is actually launched.
├── run_gate_jetson.sh        Docker alternative; rarely used, see README.
├── web/                   ← the kiosk page. Ordinary files; edit and refresh, no restart.
│   ├── index.html            markup only
│   ├── app.css               all styling
│   └── app.js                all behaviour
├── scripts/
│   ├── gate_server.py     ← the server (~1,400 lines). See below.
│   ├── ppe_check.py       ← the decision logic. Pure, no I/O, fully tested.
│   ├── test_ppe_check.py  ← the only automated tests. Run them before you commit.
│   ├── rfid.py            ← NOT WIRED UP. Dead code today. See "Unfinished work".
│   ├── export_trt.py         one-shot: .pt → TensorRT .engine, run on the target device
│   └── bench_models.py       one-shot: measures .pt vs .engine inference latency
├── config/
│   ├── gate.json          ← the checklist. WRITTEN AT RUNTIME by the UI, see below.
│   ├── camera.local.json  ← camera password. gitignored. Never commit it.
│   └── camera.local.json.example
├── models/                   .pt / .engine / .onnx, plus optional paired class-list yaml
├── MP3/                      spoken announcements
├── samples/                  test images, for exercising the checklist without a camera
├── captures.csv           ← collected training data index (gitignored)
└── dataset/               ← collected images + YOLO labels (gitignored)
```

---

## The two modules that matter

### `ppe_check.py` — the decision logic

Pure functions. No camera, no model, no HTTP, no files. Given lists of detections and a
config, it decides pass/fail. **This is the only part with real test coverage, and the
only part you can safely reason about in isolation.** If you are changing what the gate
*decides*, change it here and add a test.

Two problems it exists to solve, both non-obvious:

- **Association.** The model returns every box in the frame with no idea who wears what.
  With two people at the gate, a compliant bystander's hardhat would otherwise satisfy
  the checklist for the worker tapping in. So one *primary* person is chosen (largest
  box = closest to camera) and a PPE box only counts if it sits inside that person.
- **Voting.** Per-item recall multiplies across the checklist. At conf 0.40 the measured
  single-frame chance of catching all three items was only ~66%, so one frame per tap
  would falsely reject about a third of compliant workers. Requiring an item in
  `votes_required` frames of a short burst recovers recall, because a real hardhat
  appears in most frames while a false positive rarely repeats.

It is **fail-closed**: an item passes only when positively detected. A missing detection
is a fail, never a pass. Do not "fix" this to be lenient.

### `gate_server.py` — everything else

One file, and honestly too big (see "Known rough edges"). It owns, roughly in order:

| Concern | What it does |
|---|---|
| Camera | One background thread owns the RTSP capture, keeping only the newest frame |
| Inference | `load_model` / `detect`, plus the model-swap machinery |
| Class mapping | Reconciling the checklist's class names with whatever the model calls things |
| Recording | Saving triggered captures as training data (background writer thread) |
| Realtime | Continuous detect-and-collect mode, separate from the gate trigger |
| Audio | Spoken pass/fail, played on the Jetson via GStreamer |
| HTTP | ~17 API routes, plus serving `web/` as three static files |

---

## How a check actually flows

```
trigger (CAPTURE & CHECK button today; RFID / through-beam sensor next)
  └→ run_gate_check()          ← every trigger goes through this one function
       ↑ POST /api/capture is a thin adapter over it
       ├→ grab N frames off the camera thread, spaced by --burst-interval
       ├→ detect() each frame
       ├→ ppe_check.evaluate()  ── association + voting → PASS / FAIL / NO_WORKER
       ├→ announce()            ── queue the MP3, never blocks
       ├→ queue_record()        ── queue the training sample, never blocks
       └→ JSON back to the page, which draws boxes on a canvas
```

The two `queue_*` calls hand off to background threads on purpose: a gate tap already
costs about a second, and neither disk I/O nor audio playback has any business being
inside it.

`run_gate_check()` is deliberately not HTTP-aware: it raises `GateNotReady` (carrying an
HTTP status as a hint) so the API can answer exactly as before while a reader thread just
catches and logs. **It is not yet safe to call concurrently** — two overlapping triggers
would interleave their frame grabs and vote over a mixture of both bursts. That has never
mattered with one button; it will the moment a second trigger source exists, so serialise
callers with a lock before wiring one.

---

## Threads and how they interact

Everything runs in one process. Five kinds of thread, and the shared state between them is
small on purpose.

```
                      ┌───────────────────────────────────────────────┐
                      │  MAIN THREAD                                  │
                      │  main() → ThreadingHTTPServer.serve_forever()  │
                      │  spawns the rest, then only accepts sockets    │
                      └───────────────────────────────────────────────┘
                                        │ one thread per request
    ┌───────────────────────────────────┼───────────────────────────────────┐
    ▼                                   ▼                                   ▼
┌──────────────────┐        ┌──────────────────────┐        ┌──────────────────────┐
│ HTTP /api/*      │        │ HTTP /api/stream     │        │ HTTP /api/realtime   │
│ short-lived      │        │ LONG-LIVED (MJPEG),  │        │ polled ~4 Hz while    │
│ run_gate_check() │        │ loops until the      │        │ realtime mode is on   │
└──────────────────┘        │ browser disconnects  │        └──────────────────────┘
    │                       └──────────────────────┘                    │
    │  read newest frame                │  read newest frame            │ read newest frame
    ▼                                   ▼                               ▼
╔═════════════════════════════════════════════════════════════════════════════════╗
║ _camera = {frame, ts, ok}        guarded by _camera["lock"]                      ║
║ latest-wins: ONE frame, never a backlog, so no consumer can slow another down    ║
╚═════════════════════════════════════════════════════════════════════════════════╝
                                        ▲ writes the newest frame
                      ┌─────────────────┴─────────────────┐
                      │ CAMERA THREAD  camera_loop()      │
                      │ RTSP → NVDEC → 1280×960           │
                      │ reconnects itself on stall        │
                      └───────────────────────────────────┘

╔═════════════════════════════════════════════════════════════════════════════════╗
║ _model + its class names   guarded by _lock  (bound together — see detect())     ║
║ _frames (last 24 JPEGs)    NO LOCK — see "Known rough edges"                     ║
║ _state (settings)          effectively read-only once main() has run             ║
╚═════════════════════════════════════════════════════════════════════════════════╝

  run_gate_check() hands off both slow jobs and returns; it never waits on I/O:
                    │                                     │
            _rec_q ─┘ (64 deep)               (4 deep) ────┘ _audio_q
               ▼                                            ▼
  ┌──────────────────────────────┐        ┌──────────────────────────────┐
  │ RECORDER THREAD              │        │ AUDIO THREAD                 │
  │ dataset/ + labels/ +         │        │ gst-launch playbin, one clip │
  │ captures.csv, prunes         │        │ at a time, 10 s timeout      │
  └──────────────────────────────┘        └──────────────────────────────┘
```

**Planned trigger path** (not built — the button stands in for it today):

```
┌────────────────────┐  beam broken   ┌──────────────────────────────────────┐
│ THROUGH-BEAM       │───────────────▶│ run_gate_check(worker, "sensor")     │
│ digital input      │   the WHEN     │ ── the same function the button calls│
└────────────────────┘                └──────────────────────────────────────┘
                                                        ▲ worker id joined in
┌────────────────────┐  ~273 reads/s  ┌──────────────────────────────────────┐
│ RFID RX THREAD     │───────────────▶│ PRESENCE TRACKER                     │
│ mpk_rfid, owns the │  on_tag fires  │ rolling EPC → [(t, rssi)] window     │
│ TCP socket         │  on ITS thread │ answers "who was at the gate at T"   │
│ ⚠ no auto-reconnect│  — never block └──────────────────────────────────────┘
└────────────────────┘    it there
```

The sensor supplies *when*, RFID supplies *who*, the camera supplies *what*. Keeping them
separate matters: the PPE check is the safety-critical path and must not wait on identity.

## Where state lives

There are four kinds, and confusing them causes most of the surprises:

| State | Lives in | Survives restart? |
|---|---|---|
| Checklist (items, classes, thresholds) | `config/gate.json` | yes |
| Camera URL + password | `config/camera.local.json` | yes, gitignored |
| Which model is active | in memory; `--model` sets the initial one | **no** |
| Collected training data | `captures.csv` + `dataset/` | yes, gitignored |
| Everything else at runtime | `_state`, a module-level dict | no |

**`config/gate.json` is written by the running program.** The Detect/Veto dropdowns, the
Score ≥ sliders (per item **and** for the worker class) and the Worker class selector all
persist through `save_config()`. So if
you hand-edit that file while the gate is running, your edit will be overwritten the next
time someone touches the UI. Stop the gate first.

---

## Conventions

- **Private module state is `_prefixed`** (`_model`, `_state`, `_frames`, `_camera`).
  Anything `_`-prefixed is process-global and usually touched from more than one thread.
- **`_lock` guards the model.** `detect()` binds both the model *and* its class-name table
  inside the lock, because a model swap between the predict and the name lookup would
  decode one model's class ids against the next model's names.
- **Handler methods are `_verb`** (`_records`, `_model_upload`, `_mjpeg`); route dispatch
  lives in `do_GET` / `do_POST`.
- **Comments explain *why*.** The codebase deliberately carries a lot of rationale — much
  of it is hard-won and non-obvious, and re-deriving it costs hours. Keep that habit.
- **`_state` reads: `.get()` for optional feature toggles** (`audio`, `camera`, `record`,
  `stop`, `swapping`), plain `_state["x"]` for values that are always present, and always
  `_state["x"] = ...` for writes. This is a stopgap — a typed config object replaces the
  dict later in the refactor.
- **Tunables are named constants at the top of the module**, with the measurement or
  reasoning behind the number in a comment. `CAPTURE_JPEG_QUALITY` decides the quality of
  the *training data*, not just the preview, which is why it is high.
- **Errors that a person must act on get printed with a `[tag]`** (`[camera]`, `[model]`,
  `[record]`, `[audio]`, `[rfid]`, `[config]`) so the console is greppable in the field.

---

## Running and testing locally

```bash
./run_gate_native.sh                 # the real thing, port 8100
./run_gate_native.sh 8123 --no-camera --no-record --no-audio   # safe sandbox
python3 scripts/test_ppe_check.py    # the test suite (fast, no GPU, no camera)
```

The preflight in `run_gate_native.sh` deliberately checks `torchvision` **by name**,
because the common failure is an ABI mismatch that does not surface at install time — it
surfaces on the first frame, after the model has spent 30 seconds loading.

**Testing without a camera or a gate:** run with `--no-camera` and POST a sample image to
`/api/frame`, then `/api/check` with the returned id. That exercises the whole decision
path with no hardware. The `samples/` images exist for exactly this.

**Testing without touching production:** copy `config/gate.json` somewhere and pass
`--config <copy> --record-dir <tmp> --models-dir <tmp>`. The UI persists config changes to
disk, so a UI-driven test against the real config *will* rewrite your live checklist.

---

## Non-obvious design decisions

Undo these only if you know why they are here.

**The web UI is three files in `web/`, served off disk on every request.** The real
constraint is that the page needs no *external* assets — no CDN, no fonts, nothing the
factory network could take away. Serving them from this same process satisfies that while
keeping them lintable, highlightable, and named: a JS error reports `app.js:412` rather
than a line inside a blob.

Reading per request rather than caching at import is deliberate. The page is fetched once
per kiosk load, so the read costs nothing, and it means **editing `web/app.js` and
refreshing the browser is enough** — no restart, which would drop the camera connection
and reload the model just to see a CSS change. Startup checks all three files exist and
exits with a clear message if not, so a missing file fails at launch rather than as a
blank screen at the gate.

**Class names come from the model, never from `data.yaml`.** At runtime the class list is
read from the loaded checkpoint (`model.names`), which `.pt` and `.engine` both carry
reliably. `models/data.yaml` is a *training* input and is not read while serving.

**A paired class-list yaml renames a model's classes by index, and only when the class
counts match.** That count check is a safety rule, not a formality: renaming by index with
the wrong file would silently mislabel every detection rather than fix anything.

**The model browser never opens a model file to preview its classes.** Beyond speed, this
avoids a real hazard — loading a bare `.onnx` on this device made ultralytics try to
auto-install `onnxruntime-gpu` and, in failing, downgrade numpy out from under `cv2` and
`torch`.

**Result audio plays on the Jetson, not in the browser.** The sensor will trigger checks
with no user gesture behind them, and that is exactly the case browsers block autoplay in.
`gst-launch-1.0` is the player because GStreamer is already a dependency and ships
`avdec_mp3`; this box has no `mpg123`/`ffplay`/`mpv`, and its SoX was built without MP3.

**A capture with no worker detected is saved to `dataset/no_person/` without a label.**
An empty label asserts the whole image is background, so labelling a frame where the model
*missed* a present worker would train that miss back in. Keeping the image but withholding
the label makes it a review pile rather than training data. Realtime mode still discards
these outright — it fires continuously, so an unattended camera would otherwise fill the
disk with empty gate footage.

**Only triggers are recorded.** Images uploaded through *Choose image(s)…* are test inputs
the gate never saw; recording them would pollute the dataset.

**Realtime mode is not a gate check.** It plays no announcement and writes no
`captures.csv` row, because it is a sampling session rather than a pass/fail event that
happened at the gate. Its samples land in `dataset/realtime/` so the two sources stay
distinguishable, and it prunes its own folder — the trigger recorder prunes from the CSV,
which realtime does not write to, so it would otherwise be unbounded.

**Box area is shown on person boxes only.** Area is the distance proxy the
background-people filter works on. On a hardhat it is noise.

**The burst spacing is a deliberate pause, and it is the biggest single part of a tap.**
The capture loop sleeps `--burst-interval` between frames. Voting only means anything if
the frames differ, and this camera delivers a new frame every ~40-80 ms, so grabbing
faster than that samples the same image twice — turning corroboration into a duplicated
vote while the screen still reports "seen 2/5". Measured here, frames stay distinct down
to 0.04 s and begin repeating at 0.02 s, so the 0.08 s default keeps roughly 2x margin.
Lowering it further makes taps look faster while quietly weakening the check.

**The primary person is identified by `person_index`, never by comparing coordinates.**
Python rounds `.25` half-to-even and JavaScript rounds half-up, so a box at `196.25` reads
`196.2` from the server and `196.3` in the browser and never matches itself. That bug drew
the judged worker twice.

---

### The worker threshold is the one to be careful with

`person_conf` decides whether there is a worker at all, which makes it more consequential
than any per-item threshold. Set it too high and every check reports `NO_WORKER` no matter
how good the PPE detection is; set it too low and a coat on a chair becomes the primary
person, and every PPE box gets associated to it. It is now adjustable from the Worker
class row rather than only by hand-editing `gate.json`.

## Known rough edges

Honest list, roughly worst first:

1. **`do_POST` is ~158 lines, `main()` ~147** — long `if path ==` chains.
2. **`_state` is a 23-key untyped global dict**, read inconsistently as `_state["x"]` and
   `_state.get("x")`. A typo is a runtime `KeyError` inside a request handler.
3. **`ppe_check.py` exists twice** — here and in the x86 parent project, 8 lines apart. A
   fix must be applied in both; nothing enforces it.
4. **Only `ppe_check.py` has tests.** Routing, recording, model management and config
   persistence are verified by hand.
5. **Partial type hints** — about half the functions lack return annotations.

---

## Unfinished work

- **`scripts/RFID/rfid_console_tool/`** is the real reader's library: `mpk_rfid.py` for
  the **MPK-R-9504 UHF reader** over TCP, plus an interactive console, a simulator, and 26
  tests. All pass, and it drives the simulator end to end. **Nothing in the gate imports it
  yet** — it is committed as a verified starting point, not as a wired feature.

  Read the protocol notes in its README before touching it: payload length is 1 byte
  despite the spec saying 2 (the doc's own example checksums only validate at 1), and RSSI
  must be parsed as signed int16 or negative values wrap to +600.

- **`scripts/rfid_reader.py` is the working integration**: a supervised connection that
  reconnects like `camera_loop()` does, plus a rolling window of readings that answers
  *who was at the gate at time T*. Enabled with `--rfid-host`; absent by default, and the
  PPE check never depends on it.

  **Identity is chosen by peak RSSI within `[T-3s, T+1s]`, not by timestamp.** At ~273
  reads/second over several metres, every tag in range has a fresh timestamp at T, so
  "nearest in time" picks at random among everyone present. Signal strength is what
  separates the person crossing the gate from a colleague behind them — the same logic
  the camera uses when it picks the largest person box.

  Two vendor-API traps, both of which fail *silently*: `on_tag` delivers a `TagReport`
  whose field is **`rssi_dbm`**, not the `rssi` of `TagRecord` that the README documents;
  and `set_data_format(DF_DEFAULT)` **must** be called before `start_inventory` or the
  notifications carry no RSSI at all. Either mistake leaves the reader looking connected
  and healthy while recording nothing usable.

- **`scripts/rfid.py` is the WRONG abstraction and should be retired.** It was written for
  badge-tap readers — line-delimited card IDs over TCP/UDP/serial, one tap per card. The
  actual reader does *continuous UHF inventory*: measured against its simulator, **821
  reads across 5 tags in 3 seconds**, seeing every tag in range at once. Only its
  `TapDispatcher` (cooldown + busy rejection) is worth keeping; the three listeners are
  dead weight. Nothing imports either file today.

- **Turning continuous reads into gate events is the open design problem.** A tag is seen
  hundreds of times a second, and several workers' tags are visible simultaneously, so the
  integration has to decide *when* somebody arrived and *which* tag is the person at the
  gate. The plan is presence tracking (absent→present transitions) plus strongest RSSI —
  which mirrors what the camera already does, picking the largest person box because
  largest means closest.

- **The intended trigger may be a through-beam sensor as well**, giving a precise *when*
  while RFID gives *who*; UHF alone cannot distinguish entering from walking past. **How
  the sensor is wired is undecided** — on the Jetson's 40-pin GPIO it needs an
  opto-isolator or relay first, because industrial through-beams are 12–24 V and those
  pins are 3.3 V.
- **The `Bodycam` checklist item** has no corresponding class in the model currently
  deployed, so it reads NG on every check — and since the checklist is all-or-nothing,
  every check fails. Either retrain with a bodycam class or drop the item.
