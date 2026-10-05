# CLAUDE.md

Guidance for Claude Code sessions working in this repository.
Human-oriented orientation lives in [CODEBASE_GUIDE.md](CODEBASE_GUIDE.md); this file is
the operational contract — what to check, what to run, and what not to touch.

---

## Project goal

A PPE compliance gate running on a Jetson Orin NX in a factory. A worker arrives, a frame
is captured from a fixed Bosch camera, YOLO checks a Hardhat / Safety Harness / Bodycam
checklist, and a kiosk screen shows OK/NG per item plus Pass/Fail. It also collects its
own training data as it runs.

**This is a live safety system on real hardware.** The gate is frequently running while
you work. Prefer additive changes, verify against real models before claiming success, and
never leave it in a state where a check silently passes when it should fail.

---

## Architecture in one pass

```
run_gate_native.sh   preflight (torch/torchvision ABI, CUDA) → picks .engine over .pt
  └→ scripts/gate_server.py        one process, several threads:
       ├─ camera thread    RTSP → newest-frame buffer (latest wins, never a backlog)
       ├─ image trigger    detects that buffer continuously; dwell → RFID one-person
       │                   rule → run_gate_check()   (the default trigger)
       ├─ RFID thread      scripts/rfid_reader.py: tag RSSI log + the sensor's GPIO
       ├─ recorder thread  writes captures.csv + dataset/{images,labels}
       ├─ audio thread     gst-launch-1.0 playbin, one clip at a time
       ├─ tower light      scripts/gate_light.py: re-sends the light state to the
       │                   PATLITE (scripts/patlite_tower.py, 192.168.10.1) every
       │                   second; voices are the tower's channels 1-10 (say())
       └─ HTTP server      ~22 API routes + web/ served as static files
            └→ scripts/ppe_check.py   pure decision logic: association + voting
```

`ppe_check.py` is pure and tested. `gate_server.py` is everything else and is not.

---

## Where state and config live

| What | Where | Notes |
|---|---|---|
| Checklist: items, class mappings, thresholds | `config/gate.json` | **written at runtime by the UI** |
| Image-trigger area (px²) + RFID one-person RSSI floor (dBm) + RFID transmit power / receive mode + door side of the image + tracking person score + exit area fraction + walk-away line | `config/gate.json` (`trigger_min_area`, `rfid_min_rssi`, `rfid_power_dbm`, `rfid_rf_mode`, `door_side`, `track_person_conf`, `exit_area_fraction`, `away_fraction`) | same file, same UI-written caveat; defaults live in `gate_server.py`, not `DEFAULT_CONFIG` |
| Trigger on/off (image, sensor) | `_state` only | resets on restart: image ON, sensor OFF |
| Camera RTSP URL + password | `config/camera.local.json` | gitignored, never commit |
| Tower web login (for speaker volume) | `config/tower.local.json` (`web_user`, `web_pass`) | same rule as the camera file: never commit, never echo. The volume itself lives on the tower (0-15), set from the Speaker test slider through its web UI — one web login at a time, so a browser logged into the tower blocks it. The unit's **Mute** box (same page, 「靜音」 checkbox in the popup) silences every voice while `/api/status` still reports the channel playing — the gate cannot see it; check it first when the speaker is silent (2026-10-03) |
| Active model | `config/gate.json` (`model`, a file name in `models/`) | written on every UI switch/upload; wins over `--model` at startup, which is only the fallback (missing file, fresh config, or `--lock-model`) |
| Runtime settings | `_state` dict in `gate_server.py` | 23 keys, untyped |
| Collected training data | `captures.csv`, `dataset/` | gitignored |
| Visit outcomes / IT outbox | `events.jsonl` (append-only, one JSON line per visit) | `it_report` marks what goes to IT |
| Badge whitelist | `config/whitelist.json` (`{"enabled": true, "ids": ["…"]}`) | hand-edited, never written by the UI; re-read on change (no restart). Absent / `enabled:false` = every badge allowed; unreadable = every badge refused (fail closed). IDs are JSON **strings** |
| Tower voices | the tower itself: channels 1-10 (web UI → Voice Registration) | 1 檢測口請淨空 · 2 擅自闖入 · 3 檢測未通過 · 4 ID讀取失敗 · 5 ID未登入 · 6 檢測通過 · 7 未檢查即出場 · 8 未通過仍出場 · 9 檢測通過請進場 · 10 檢測通過請出場 — `VOICE` in gate_server.py. Same words in `MP3/` for a gate with no tower. A factory reset of the unit wipes them |
| IT reporting settings | `config/it.json` | `targets` (`tsmc-test` = the site service, `mock` = `scripts/it_mock.py` on this box via the `/etc/hosts` name `it-mock.test`) and `target` = the live one, switched from the kiosk next to IT Report (persisted, shown as `IT → <target>`). **On site it must be `tsmc-test`.** `fab_area` `F20P3A-L40-A-1`, `client_id`, item→field map. `enabled` and `target` are written by the kiosk — don't hand-edit them while the gate runs |
| IT mock receiver | `scripts/it_mock.py` (port 8900, page at `/`), data in `it_mock_data/` | replies like the real service: `201 {"id", "access_type": "entry"/"leave", "message"}`; modes toggle/entry/leave/500/400/timeout from its page. Test data with worker photos — not a deliverable. Started by hand (`setsid nohup python3 scripts/it_mock.py &`), not a service |
| IT sender progress | `events.sent` (byte offset), `events.rejected.jsonl` (HTTP 4xx, set aside) | delete `events.sent` and the sender restarts at the END of the file, never replaying history |
| Refusal & violation evidence | `dataset/alarms/images/` | never training data |
| Model class names | the model checkpoint itself | *not* `data.yaml` — see gotchas |

`config/gate.json` being runtime-written matters: **the UI overwrites it.** Do not
hand-edit it while the gate is running, and never test UI persistence against the real
file — copy it and pass `--config <copy>`.

---

## Build & test commands

```bash
# Tests (fast, no GPU, no camera). Run before every commit.
python3 scripts/test_ppe_check.py
python3 scripts/test_visits.py        # in/out judgement: door side, edges, walking away
python3 scripts/test_gate_light.py    # light policy + IT forms
python3 scripts/test_whitelist.py

# Syntax check after editing the big file
python3 -c "import ast; ast.parse(open('scripts/gate_server.py').read())"

# The UI now lives in web/ as ordinary files, so a normal linter works on web/app.js.
# It is served off disk per request: edit it and refresh the browser, no restart needed.
# Only files listed in WEB_ASSETS (gate_server.py) are served — a NEW file needs an entry
# there and a restart. web/demo.js + demo.css = the 「大字報」 visitor view (2026-10-04):
# a full-screen layer over the developer page, fed by three hooks in app.js
# (window.demoResult in paintSummary, window.demoAlarm in paintAlarm, window.DEMO_ON in
# liveTick). It reads, never decides; the developer page keeps running underneath.

# Run the real gate
./run_gate_native.sh

# Run an isolated sandbox that cannot touch production state
./run_gate_native.sh 8123 --no-camera --no-record --no-audio \
  --config /tmp/gate_copy.json --models-dir /tmp/models --record-dir /tmp/rec
```

**Exercising a check without a camera:** POST a `samples/*.jpg` to `/api/frame`, take the
returned id, POST it to `/api/check`. That covers the whole decision path.

**Verifying the UI:** there is no test harness for the page. Headless Chromium works and
is the only way to catch CSS/JS problems the Python syntax check cannot see:

```bash
chromium --headless --disable-gpu --no-sandbox --user-data-dir=/tmp/ch \
  --virtual-time-budget=4000 --screenshot=/tmp/shot.png http://localhost:8123/
```

Use an isolated `--user-data-dir`; the operator's own Chromium is usually running.

---

## Known gotchas

**numpy must stay on 1.x.** JetPack's `cv2` and NVIDIA's `torch` are built against numpy
1.x. Anything that upgrades it breaks both at import time. `pip install --user
"numpy==1.26.4"` restores it. This has happened more than once — most recently by merely
*loading a bare `.onnx`*, which made ultralytics try to auto-install `onnxruntime-gpu` and
downgrade numpy while failing. **Re-check `numpy`, `cv2` and `torch` import after any pip
activity or any ONNX work.**

**torchvision must be the CXX11-ABI build** matching NVIDIA's torch. The generic PyPI
aarch64 wheel is ABI=0 and fails on the first inference, not at install. The launcher
preflights this by name.

**`.engine` files are device- and TensorRT-version-specific.** One built elsewhere will
not load. Rebuilding takes ~11 minutes: `python3 scripts/export_trt.py --model
models/ppe.pt --imgsz 640 --half`.

**Class names come from the checkpoint, not `data.yaml`.** `data.yaml` is a training
input and is never read while serving. A paired `<model-stem>.yaml` can *rename* a
model's classes, but only when the class counts match — that guard prevents silently
mislabelling every detection.

**Python and JavaScript round `.25` differently** (half-to-even vs half-up). Never match
detections across the boundary by comparing rounded coordinates; use `person_index`.

**The checklist is all-or-nothing.** One item pointing at a class the active model lacks
makes *every* check fail. Currently `Bodycam` is in that state.

**Inference is not the bottleneck.** ~50 ms/frame on the engine. A measured tap is ~0.71 s:
~250 ms inference, ~64 ms JPEG encoding, and ~320 ms of *deliberate* sleeping between
burst frames. Do not reach for the model to make taps faster — and do not shrink
`--burst-interval` below ~0.08 s: the camera only produces a new frame every ~40-80 ms, so
faster sampling repeats frames and the vote stops being corroboration.

**Never use `model.track()` on the gate's model.** It registers ByteTrack callbacks on the
YOLO object itself; they then run on *every* later `predict()` — including the PPE burst —
and replace each result with the tracked boxes only. Helmet/harness boxes that never
became tracks silently vanish from the checklist's input. Person IDs come from a
standalone `BYTETracker` fed only person boxes (`_make_person_tracker`). The tracker
needs the `lap` package; ultralytics pip-installed it automatically the first time
(numpy was untouched — but re-check numpy/cv2/torch after any such auto-install).

**`captures.csv`'s header is compared on every write.** A pure *addition* of columns
migrates in place (old rows kept, new cells blank or backfilled); anything else — a
renamed or removed checklist item — rotates the file to `captures.csv.<ts>.bak`, which
empties See Records. Add columns; don't reorder or rename them.

---

## What NOT to touch without asking

- **`config/gate.json`** — it is live operational configuration, and the UI writes it.
  Changing a class mapping or threshold changes what the gate lets through.
- **The fail-closed rule in `ppe_check.py`.** An undetected item is a FAIL. Never relax
  this to make results look better.
- **`config/camera.local.json`** — contains the camera password. Never commit, never echo
  it into logs or commit messages.
- **`captures.csv` / `dataset/`** — the operator's collected data. Deleting is destructive
  and irreversible; ask first.
- **Anything under `models/`** — `.pt` weights are the one artifact only retraining can
  replace.
- **The numpy / torchvision pins.** The pip warnings on this box (`ultralytics requires
  opencv-python`, `onnxruntime-gpu not found`, `ml-dtypes requires numpy>=2`) are
  *expected* and must not be "fixed".

---

## Working style that fits this project

- **Verify on real hardware, not by reasoning.** Several bugs this codebase has had were
  invisible in review and obvious in a screenshot or a live inference call.
- **Test against copies.** UI actions persist to `config/gate.json`; a careless test
  rewrites the live checklist.
- **Clean up test servers and scratch files.** Check for strays with
  `ps -o pid,cmd -C python3 | grep gate_server`. Note that `pkill -f` patterns matching the
  invoking command line will kill the calling shell.
- **Comment the *why*.** This codebase's rationale comments are load-bearing; the reasons
  are non-obvious and expensive to re-derive.
- **One concern per commit.** The history is the only design record for many decisions.

---

## Current status

- Phase 1 (check engine + kiosk screen) — done.
- Phase 2 (live RTSP + trigger) — done. Two trigger sources, both funnelled through
  `run_gate_check()` under `_gate_check_lock`:
  - **Image trigger (default, ON at start).** `image_trigger_loop` runs a dwell state
    machine over the live feed at a fixed `IMAGE_TRIGGER_PERIOD_S` (100 ms — raised from
    ~215 ms so track IDs survive a brisk walk). Every person box carries a ByteTrack ID,
    and each ID that reaches the door band at AREA_TH becomes a **visit** (`_visits`,
    RAM only, a few fields per ID, capped, dropped ~3.5 s after the ID vanishes):
    - **Intent** is the side it came from — the door's (restricted) region → 出場,
      anything else incl. appearing in the band → 進場. It goes on the check's
      `captures.csv` row (`direction`) and the live overlay. Which side of the IMAGE
      the door is on is `gate.json` `door_side` (`left` default / `right`), the kiosk's
      「門在畫面」 select under the picture, marked 「門 · 管制區」 on the live view. The
      site plan (door left of the check area, camera facing the worker) puts it on the
      image's right — but the operator turned the camera's **mirror** on, so on the live
      gate it is **left** (set 2026-10-02). Mirroring flips only left/right; walking
      towards / away from the camera is unaffected. Confirm on site by watching a worker
      walk to the door.
    - **Side-on exits** (2026-10-03, `exit_area_fraction`, default 0.6, the 「出場（門那側來）×」
      box next to the area threshold): a track that came from the door's side starts its
      出場 visit at that fraction of AREA_TH — a worker stepping out of the door is
      side-on (~55-65 % of a front-on box) and never reached AREA_TH, so walking out
      unchecked went unreported. Entering visits keep the full AREA_TH. "Came from" is
      remembered per track (`came_from`, the last non-band side) rather than read off the
      tick before the band; small in the middle of the frame (far down the entrance path)
      wipes it. The walk-away / edge-crossing line is `_away_line`: `away_fraction`
      (default 0.5, the page's 「走遠線 ×」, 0.3-0.8 — the operator raised the cap from 0.7 on
      2026-10-04; above ~0.7 a worker turning side-on to the door can read as walking off) of the visit's own peak area, capped
      at that fraction of AREA_TH (so front-on visits use the plain line). A shallow room
      needs it higher: the 2026-10-03 bench video had a worker reach the back wall at
      0.41 × AREA_TH for ~0.3 s and come straight back — uncounted at 0.5, an exit plus an
      intrusion at 0.65 (replayed in test_visits.py section 18). Such a
      visit is drawn blue on the live view (box flag 3).
    - **Tracking threshold** (2026-10-03, `track_person_conf`, default 0.6, the page's
      「Tracking ≥」 under the worker Score): the visit tracker follows person boxes down to
      it, so an ID survives a worker walking away or turning their back (visits used to
      end "unknown" mid-band when the score sagged under person_conf). Only "firm" boxes
      (≥ person_conf) count as someone at the gate, feed the crowd rule or START a visit;
      the server never uses it above person_conf. `out of view` journal lines carry the
      last score.
    - **Walking away** (2026-10-02): on site the entrance path is straight in front of the
      camera, so going OUT means walking away, not crossing an edge. The box shrinking
      below `CROSS_MIN_AREA_FRACTION` × AREA_TH, steadily (`_away_step`: area at most
      halves and the centre moves ≤ 10 % between sightings, else it is a tracker hand-off
      and is blocked until the box is big again), for `AWAY_CONFIRM_S` (0.3 s) = departure
      `via: "away"`: through for 出場, turned back for 進場. Turning sideways or crouching
      does not trigger it — the threshold is relative to AREA_TH, not the worker's own
      size. A resolved visit whose box went small re-arms (`rearm`), so walking up again
      is a new visit. Covered by `scripts/test_visits.py`.
    - **Departure** is the edge it leaves the band by (or walking away, above); it runs on every tick, cooldown
      included, which is what catches a tailgater. Going through without a PASS as the
      latest result is a **violation** (未檢查即進入 / 未通過仍進入 / 被拒絕仍進入 and the
      出場 forms): a VIOLATION row with the frame of that moment, a red flash, the kiosk
      alarm box and a voice, both directions. Walking IN is always 「擅自闖入」 (after a
      FAIL too — 「檢測未通過」 there just repeated the verdict). Walking OUT: 「未通過仍出場」
      after a FAIL, 「未檢查即出場」 otherwise. Walking IN after a FAIL is TWO IT posts: the
      check (items + check photo) and the intrusion (Fail, items empty, crossing photo). A visit with no check of its own takes a
      badge's FAIL from the last `RECALL_S` (30 s) if that badge is being read — the fix
      for a track ID switch between a worker's check and their walk-through.
    - A crossing only counts as walking through if the box is still ≥
      `CROSS_MIN_AREA_FRACTION` (0.5) × AREA_TH at that moment — a small box crossing is a
      background person the tracker handed the ID to (false 擅自闖入, 2026-10-01); it ends
      the visit as unknown. VIOLATION rows now carry the crossing box (`person_box`).
    - Every closed visit is one line in `events.jsonl` — the IT outbox. `it_report` is
      true for any visit with a PPE result and every violation; refusals alone are
      warnings only.
    - Known weak spot: an ID switch in the band (a big jump, or a detection dropout while
      walking) makes the new ID look like it appeared from nowhere, so an exiting worker
      can be read as entering. Seen in testing only with an unrealistic 245 px jump.
    - Every box above `cfg.trigger_min_area` **whose centre is inside the door zone
      (ROI)** counts as a person at the gate — not just the largest. Two of them refuses
      (「檢測口請淨空」, after `CROWD_CONFIRM_S`), before any dwell, so a pair is told to
      queue rather than checked as one. People outside the ROI are ignored by the crowd
      rule (operator, 2026-10-05: of that day's 100 crowd refusals on site only 4 had two
      people in the zone). The dwell subject is the largest person IN the zone, and an
      image-trigger check passes the zone to `ppe_check.evaluate` (`subject_zone_px`) so
      the checklist judges that same person — never a bigger bystander beside the gate;
      nobody in the zone = NO_WORKER.
    - Exactly one, with its box centre inside the `cfg.trigger_zone` band, starts
      `t_enter`. The timer **survives brief dropouts**: only `IMAGE_TRIGGER_MISS_S` (1 s)
      of unbroken absence ends the visit. Never reset the timer on a single miss — the
      model drops a frame often enough that a worker would never accumulate the dwell.
    - Both debounces are in **seconds**, not ticks, so changing the tick rate does not
      silently change them.
    - Leaving the zone ends the visit **silently, in either direction**. A
      「勿擅自闖入此區」 alarm for walking in mid-dwell existed and was removed at the
      operator's request (too many voices on one speaker, for an event nobody acts
      on) — don't reintroduce it without asking.
    - Two person boxes must persist for `CROWD_CONFIRM_S` (0.65 s) before the crowd
      refusal fires; each refusal journals every box with its IoU/containment against
      the largest, the data a same-person de-dup threshold would be tuned from.
    - **RFID power / receive mode** are set live from the 「See RFID read」 popup
      (`RfidService.reconfigure`, on the reader thread: abort → mode → start at the new
      power; a refusal arrives as an "inventory ended" error and the old values are put
      back). Default 20 dBm / mode 103 (最快, hears ≥ −68 dBm only — so an RSSI floor below
      −68 changes nothing in that mode); 285 (最靈敏) hears ≥ −83 dBm. The real reader
      ACCEPTED 25–33 dBm on 2026-10-01 — acceptance is not proof it transmits that much.
    - Dwell satisfied → the one-tag rule, looking **back** `--rfid-before` (3 s) from
      that moment: exactly one tag whose peak RSSI in the window is ≥
      `cfg.rfid_min_rssi` → check with that EPC; none → `RFID_fail.mp3`; two or more →
      `multi-person-detected.mp3`. **Every refusal speaks, flashes and is recorded, every
      time** (operator: 每一次的檢測都要有語音) — there is no repeat suppression any more;
      a refused worker who stays hears it again each ~4 s cycle and stays under red.
      `[visit] #N out of view mid-visit, last seen at cx=…` in the journal marks a visitor
      who vanished before crossing an edge — how a walk-in close to the camera is missed.
    - Exactly one tag, but not in `config/whitelist.json` → **UNREGISTERED**:
      `ID_not_registered.mp3` (「ID未登入」, a generated placeholder — replace freely,
      same name), a 5 s red flash on the tower, an UNREGISTERED row + frame, and —
      unlike every other refusal — **reported to IT** (ppeResult=Fail, items empty, that
      badge, the refusal's photo; once per badge per visit). Not repeated while the same
      badge stays. Only the image trigger applies the list; the button and the DI sensor
      path do not.
    - Cooldown depends on the verdict: PASS opens a **5 s window** (green flashing) that
      ends early when the worker goes through (far edge, or walks away for 出場); still there at 5 s →
      yellow flash and the normal re-check. FAIL 0.5 s, refusals 2 s.
    - **The light** (operator's definitions, `scripts/gate_light.py`): green steady =
      open/idle, yellow steady = checking (dwell or burst), green flash = PASS window,
      yellow flash = warning (crowd, two badges, passed but did not go), red flash =
      fail (no tag, unregistered, PPE FAIL, walked in), red steady = closed (camera
      dead, reader disconnected, trigger off, model swapping). The gate re-sends it
      every second with the tower's restore timer as a dead-man switch: the base is
      red steady, so a dead gate / Jetson / cable turns the tower red within ~5 s
      (measured). Never drive the tower with raw commands while the gate runs — they
      are overwritten within a second; the Speaker test goes through the policy.
    - **Voices** come from the tower (`say()`): PASS 「檢測通過」 (spoken since the
      evening of 2026-09-30); NO_WORKER is silent; FAIL
      「檢測未通過」; crowd and two badges 「檢測口請淨空」; no tag 「ID讀取失敗」;
      unregistered 「ID未登入」; reader down is silent (the light is red steady).
      There is NO "reader deaf" rule (closed after N badge-less people in a row) — removed
      at the operator's request on 2026-10-05: nobody may carry a badge on site, and the
      gate keeps checking and saying 「ID讀取失敗」 instead of closing. So a connected
      reader that hears nothing is NOT shown as a fault.
      The tower answers HTTP 200 even on failure; `Error. [002]` to every command
      means the unit was reset (needs its first-access account + Enable Feature →
      HTTP Command Control, and the voices re-registered).
    - Every announcement is a `captures.csv` row with an `alarm` column (what was said),
      and every row has `rfid_tags` — each badge heard in the RFID window with its peak
      dBm and read count, `(弱)` under the floor (on 「檢測口請淨空」 rows: who was there;
      the RFID refusals record the exact rows they decided on). Check rows also carry
      `<item>_score` / `<item>_neg_score`: the best score of the item's class / its NO-
      class on the worker over the burst, whatever the conf (`ItemResult.seen_score` /
      `neg_score`, observation only — the verdict is unchanged). Every row also has
      `person_area` / `person_score` — the judged person's box area and detection score
      (crowd: the largest box; violation: the crossing box; RFID refusals: the visit's
      last box, which also fills their `person_box` now). See Records shows them as
      「Person (px² · score)」; older rows got their area backfilled from `person_box`.
      Refusals are rows too (status CROWD / MULTI_TAG / NO_TAG / READER_DOWN), with
      their frame in `dataset/alarms/images/` — evidence, never training data.
    - **In/out source is a kiosk switch** (`gate.json` `direction_source`, UI 「進出場判斷」):
      `it` (default) — IT's access_type decides, as below; `track` — our bbox track
      decides (the original behaviour) and IT's answer is only logged. IT posting is
      identical in both modes. **The PASS voice depends on the mode**
      (`PASS_VOICE_SAYS_DIRECTION = {"track": True, "it": False}`): 軌跡方向 says
      「檢測通過請進場」 / 「檢測通過請出場」 (tower channels 9 / 10, back on 2026-10-06);
      IT 回報 says just 「檢測通過」 at once (2026-10-05). The 大字報 adds 請進場 / 請出場
      under the banner exactly when the voice does (status `pass_says_direction`).
    - **IT decides in/out** (in `it` mode; operator, 2026-10-01): an image-trigger PASS/FAIL is posted
      to IT AT THE VERDICT (`_post_check_now`, photo from memory). With
      `PASS_VOICE_SAYS_DIRECTION["it"]` on, the PASS voice waits up to `IT_VOICE_WAIT_S` (1.5 s)
      for the reply's `access_type` — entry 「檢測通過請進場」, leave 「檢測通過請出場」, no
      reply 「檢測通過」; it is OFF, so 「檢測通過」 plays at once. That answer becomes the visit's
      direction (`intent_it`, over the track's guess), and went-through / violations are
      judged against it — but violations themselves (擅自闖入 / 闖出) are still ours. A
      check posted at the verdict is marked in its visit entry (`it.state`) so the outbox
      never sends it twice; a failed post is delivered by the outbox when the visit ends.
    - **IT reporting** (`scripts/it_report.py`, format from the operator's test tool;
      a PASS whose worker then turned back or stood on is NOT sent, every FAIL is — this
      only applies to checks the outbox still holds, i.e. not posted at the verdict):
      a heartbeat every 30 s (`device_status` normal / cctv_dead / speaker_dead) and one
      multipart `ppe-result` POST — fields + `photo` — per PASS/FAIL check, sent when
      the visit resolves. Times are UTC. Durable and at-least-once via `events.sent`.
      Walking IN with no PPE check at all (未檢查即進入 / 被拒絕仍進入) is sent as
      `ppeResult=Fail` with the item fields empty and the photo of the moment — the
      form has no violation field; operator's call. **Exits without a PASS are sent the
      same way since 2026-10-06** (未檢查即出場 / 未通過仍出場 / 被拒絕仍出場 — before that
      only entries were; `it_report.through_without_pass`), so after a FAIL an exit is two
      posts too. There is no in/out field yet (operator: ignore for now), so IT cannot
      tell an exit violation from an entry one.
      The kiosk's **IT Report** button switches it on/off and persists to
      `config/it.json`; switching ON reports from that moment (no replay of what
      happened while off), whereas a service restart with it ON delivers what waits.
      The TSMC test domain does not resolve on the bench LAN — real delivery can only
      be proven on-site; everything else was verified against a local mock.

    Area, zone and RSSI are sliders on the page, door side a select; all persist to
    `gate.json`.
  - **Through-beam sensor (OFF at start).** Its DI is wired into the RFID box's GPIO
    (`rfid_reader.GpioSensor`), `--sensor-pin` names the input, the page's Sensor
    Trigger toggle arms it. Reader library: `scripts/RFID2/rfid_console_tool_2/`
    (v2.1.0) via `scripts/rfid_reader.py`. `scripts/rfid.py` is the retired badge-tap
    abstraction — delete rather than extend.
- Phase 3 (on-site dataset collection) — working; discards no-worker captures.
- A maintainability refactor is in progress. Done: these docs, named constants, and
  extracting the web UI from a Python string into `web/`. Next: route dispatch tables,
  splitting `gate_server.py` into modules, and replacing `_state` with a typed object.
