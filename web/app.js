const $ = id => document.getElementById(id);
const cv = $("cv"), ctx = cv.getContext("2d");
let CFG = null, LAST = null, img = null;

const COL = {ok:"#00b400", ng:"#e00000", person:"#f59e0b", bystander:"#6b7280",
             pred:"#2b6cb0"};   // a prediction the checklist did NOT judge — drawn dashed

// "Show all predictions": a debugging view of what the MODEL said, layered over the
// verdict view. Off, the picture only shows boxes that took part in the decision (the
// kiosk reading). On, every prediction above its threshold is drawn too — PPE on other
// people, NO-* classes, anything not in the checklist, and every other person
// regardless of the area slider — in dashed blue so none of it can be mistaken for a
// verdict colour. Remembered per browser: an operator checking a model wants it to
// stay on across reloads; a worker-facing kiosk wants it to stay off.
let SHOW_ALL = true;
try { SHOW_ALL = localStorage.getItem("showAll") !== "0"; } catch (e) {}
$("showAll").checked = SHOW_ALL;
$("showAll").onchange = () => {
  SHOW_ALL = $("showAll").checked;
  try { localStorage.setItem("showAll", SHOW_ALL ? "1" : "0"); } catch (e) {}
  if (LAST) draw(LAST);
};

// ── background-person filter / image-trigger area ───────────────────────
// A box's area is a proxy for distance from the camera: someone in the background is
// smaller on screen than the worker actually at the gate. The primary person (who the
// checklist is judging) is never hidden by this — only OTHER detected people are, so
// a crowded background doesn't clutter the result image.
//
// The same number is the image trigger's "close enough" line on the server (cfg
// trigger_min_area): the largest person's box must exceed it for the dwell time before
// a check fires. So the slider is no longer a per-browser display preference — it is
// saved to gate.json on release, and every kiosk shows the value the gate is using.
let AREA_MIN = 10000;

function fmtArea(n){ return Math.round(n).toLocaleString(); }
function fmtScore(v){ return (v === undefined || v === null) ? "" : Number(v).toFixed(2); }

// Slider and number box are two views of AREA_MIN; whichever the operator touches
// updates the other. Saved on release / Enter / blur — never on every pixel or keystroke.
function paintArea(){
  $("areaSlider").value = AREA_MIN;
  $("areaNum").value = AREA_MIN;
  paintExitFrac();
}

// 出場 visits (a track that came from the door's side) start at this fraction of the area
// threshold — a side-on worker stepping out of the door is smaller than a front-on one.
let EXIT_FRAC = 0.6, AWAY_FRAC = 0.5, VANISH_S = 0.5;
function paintExitFrac(){
  if (document.activeElement !== $("exitFrac")) $("exitFrac").value = EXIT_FRAC.toFixed(2);
  $("exitFracPx").textContent = `= ${fmtArea(AREA_MIN * EXIT_FRAC)} px²`;
  if (document.activeElement !== $("awayFrac")) $("awayFrac").value = AWAY_FRAC.toFixed(2);
  $("awayFracPx").textContent = `= ${fmtArea(AREA_MIN * AWAY_FRAC)} px²`;
  if (document.activeElement !== $("vanishSec")) $("vanishSec").value = VANISH_S.toFixed(1);
}
// 「消失即出場」: a leaving worker out of view this long inside the zone has gone behind a
// wall — through, like walking away.
$("vanishSec").onkeydown = e => { if (e.key === "Enter") $("vanishSec").blur(); };
$("vanishSec").onchange = async () => {
  const v = Number($("vanishSec").value);
  try{
    if (isNaN(v)) throw new Error("not a number");
    const r = await fetch("/api/vanish_exit", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({seconds: v})});
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    VANISH_S = d.vanish_exit_s;
    if (CFG) CFG.cfg.vanish_exit_s = VANISH_S;
  }catch(e){ $("err").textContent = "Could not save 消失即出場: " + e.message; }
  paintExitFrac();
};
// "Walked away" line: a visit's box under this fraction of the area threshold (held
// 0.3 s) has left — out for 出場, turned back for 進場.
$("awayFrac").onkeydown = e => { if (e.key === "Enter") $("awayFrac").blur(); };
$("awayFrac").onchange = async () => {
  const v = Number($("awayFrac").value);
  try{
    if (isNaN(v)) throw new Error("not a number");
    const r = await fetch("/api/away_fraction", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({fraction: v})});
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    AWAY_FRAC = d.away_fraction;
    if (CFG) CFG.cfg.away_fraction = AWAY_FRAC;
  }catch(e){ $("err").textContent = "Could not save the walk-away line: " + e.message; }
  paintExitFrac();
};
$("exitFrac").onkeydown = e => { if (e.key === "Enter") $("exitFrac").blur(); };
$("exitFrac").onchange = async () => {
  const v = Number($("exitFrac").value);
  try{
    if (isNaN(v)) throw new Error("not a number");
    const r = await fetch("/api/exit_area_fraction", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({fraction: v})});
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    EXIT_FRAC = d.exit_area_fraction;
    if (CFG) CFG.cfg.exit_area_fraction = EXIT_FRAC;
  }catch(e){ $("err").textContent = "Could not save the exit area fraction: " + e.message; }
  paintExitFrac();
};

$("areaSlider").oninput = () => {
  AREA_MIN = Number($("areaSlider").value);
  $("areaNum").value = AREA_MIN;
  paintExitFrac();
  if (LAST) draw(LAST);
};
$("areaNum").oninput = () => {
  const v = Number($("areaNum").value);
  if (isNaN(v) || v < 0) return;
  AREA_MIN = Math.round(v);
  if (AREA_MIN > Number($("areaSlider").max)) $("areaSlider").max = AREA_MIN;
  $("areaSlider").value = AREA_MIN;
  if (LAST) draw(LAST);
};
$("areaNum").onkeydown = e => { if (e.key === "Enter") $("areaNum").blur(); };   // blur fires onchange

async function saveArea(){
  paintArea();                              // normalise a half-typed value in the box
  try{
    const r = await fetch("/api/trigger_area", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({area: AREA_MIN})});
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    if (CFG) CFG.cfg.trigger_min_area = d.trigger_min_area;
  }catch(e){
    $("err").textContent = "Could not save the area threshold: " + e.message;
  }
}
$("areaSlider").onchange = saveArea;
$("areaNum").onchange = saveArea;

// ── RFID badge threshold (dBm) ──────────────────────────────────────────
// The image trigger counts tags whose peak RSSI in the last few seconds is at least
// this. Exactly one → that worker is checked; none → RFID_fail.mp3; two or more →
// multi-person-detected.mp3. Saved to gate.json like the area slider.
let RSSI_MIN = -60;

function fmtRssi(v){ return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(Math.round(v)); }

function paintRssi(){
  $("rssiVal").textContent = fmtRssi(RSSI_MIN);
  $("rssiHint").textContent = `tags ≥ ${fmtRssi(RSSI_MIN)} dBm count`;
}

$("rssiSlider").oninput = () => { RSSI_MIN = Number($("rssiSlider").value); paintRssi(); };
$("rssiSlider").onchange = async () => {
  try{
    const r = await fetch("/api/rfid_min_rssi", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({rssi: RSSI_MIN})});
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    if (CFG) CFG.cfg.rfid_min_rssi = d.rfid_min_rssi;
    if ($("rfidModal").classList.contains("on") && RFID_ROWS) renderRfid(RFID_ROWS);
  }catch(e){
    $("err").textContent = "Could not save the RFID threshold: " + e.message;
  }
};

// ── door zone (left/right edges) ────────────────────────────────────────
// The image trigger also needs the person to be AT the door, not merely big: the box
// centre must fall between these two edges, stored as fractions of frame width so the
// same numbers mean the same thing on the 1280-wide capture the server judges, the
// 720p live stream and the full-res result canvas. Saved to gate.json on release.
let ZONE = [0, 1];
let DOOR_SIDE = "left";       // which side of the picture the restricted area's door is on
const ZONE_MIN_GAP = 0.02;    // handles can't cross or sit closer than 2% of the width
const ZONE_KEY_STEP = 0.01;

function paintZone(){
  const [l, r] = ZONE;
  $("zoneHL").style.left = (l * 100) + "%";
  $("zoneHR").style.left = (r * 100) + "%";
  $("zoneHL").setAttribute("aria-valuenow", Math.round(l * 100));
  $("zoneHR").setAttribute("aria-valuenow", Math.round(r * 100));
  $("zoneFill").style.left = (l * 100) + "%";
  $("zoneFill").style.width = ((r - l) * 100) + "%";
  $("zoneVal").textContent = `${Math.round(l * 100)}% – ${Math.round(r * 100)}%`;
  $("zoneHint").textContent = (l <= 0 && r >= 1) ? "whole frame" : "box centre must be inside";
  positionZoneOverlay();
}

// Lays the overlay exactly over whichever picture is showing. Both #live and #cv keep
// their aspect ratio under max-width/max-height, so the element's own box IS the
// rendered picture — no letterbox maths needed. Called from a ResizeObserver on the
// two elements (first frame decoded, window resized, live<->result swap) and from the
// handles; nothing here runs per video frame.
// Which element currently holds the picture: the result canvas when a verdict is up,
// otherwise the live <img>. Both keep their aspect ratio under max-width/max-height, so
// the element's own box IS the rendered picture — no letterbox maths needed.
function pictureEl(){
  return cv.classList.contains("ready") ? cv
       : ($("live").classList.contains("on") ? $("live") : null);
}

// The trigger overlay (boxes + track) describes the CURRENT camera frame, so it may
// only be drawn over the live feed. Over a frozen check result it would put this
// second's boxes on a picture taken seconds ago — boxes that line up with nothing.
function liveEl(){
  return (!cv.classList.contains("ready") && $("live").classList.contains("on"))
    ? $("live") : null;
}

function positionZoneOverlay(){
  const ov = $("zoneOverlay");
  const el = pictureEl();
  placeOver($("trackCv"), liveEl());
  positionExitRoi();
  const [l, r] = ZONE;
  if (!el || !el.offsetWidth || (l <= 0 && r >= 1)){ ov.classList.remove("on"); return; }
  ov.style.left = el.offsetLeft + "px";
  ov.style.top = el.offsetTop + "px";
  ov.style.width = el.offsetWidth + "px";
  ov.style.height = el.offsetHeight + "px";
  $("zoneShadeL").style.left = "0"; $("zoneShadeL").style.width = (l * 100) + "%";
  $("zoneShadeR").style.left = (r * 100) + "%"; $("zoneShadeR").style.width = ((1 - r) * 100) + "%";
  $("zoneEdgeL").style.left = (l * 100) + "%";
  $("zoneEdgeR").style.left = (r * 100) + "%";
  $("doorMark").className = "doorMark " + DOOR_SIDE;
  $("doorMark").textContent = {left: "◀ 管制區", right: "管制區 ▶",
                               both: "◀ 管制區（左右兩側） ▶"}[DOOR_SIDE] || "管制區";
  ov.classList.add("on");
}

// ── check spot / exit area (exit_roi) ───────────────────────────────────
// One rectangle, two jobs (operator, 2026-10-09). The image trigger's dwell only runs while
// the person's FEET — the bottom-centre of their box — are in it; and an exit only counts
// once the worker's feet have been in it (judged gone without that = not out yet). Drawn
// by dragging on the picture after pressing 畫檢測／出場區; stored as fractions of the frame,
// like the door zone, so it means the same on the live stream, the 1280 frame the gate
// judges and a result. The server snaps an edge within 1 % of the border to the border.
let EXIT_ROI = null;          // [x1, y1, x2, y2] fractions, or null = off
let ROI_DRAW = null;          // armed: {} before the press, {x0, y0, x1, y1} while dragging
let ROI_CLEAR_TIMER = null;   // 清除 asks for a second press within 3 s

// The server's EXIT_ROI_SNAP, so the feet dots agree with what the gate decides.
function snapRoi(r){
  return r && r.map(v => v <= 0.01 ? 0 : v >= 0.99 ? 1 : v);
}

function roiRect(d){
  return [Math.min(d.x0, d.x1), Math.min(d.y0, d.y1), Math.max(d.x0, d.x1), Math.max(d.y0, d.y1)];
}

function positionExitRoi(){
  const el = pictureEl(), box = $("exitRoi"), layer = $("exitRoiLayer");
  if (!el || !el.offsetWidth){ box.classList.remove("on"); layer.classList.remove("on"); return; }
  if (ROI_DRAW){
    layer.style.left = el.offsetLeft + "px"; layer.style.top = el.offsetTop + "px";
    layer.style.width = el.offsetWidth + "px"; layer.style.height = el.offsetHeight + "px";
    layer.classList.add("on");
  } else layer.classList.remove("on");
  const r = ROI_DRAW && ROI_DRAW.x0 != null ? roiRect(ROI_DRAW) : EXIT_ROI;
  if (!r){ box.classList.remove("on"); return; }
  box.style.left = (el.offsetLeft + r[0] * el.offsetWidth) + "px";
  box.style.top = (el.offsetTop + r[1] * el.offsetHeight) + "px";
  box.style.width = ((r[2] - r[0]) * el.offsetWidth) + "px";
  box.style.height = ((r[3] - r[1]) * el.offsetHeight) + "px";
  box.classList.add("on");
}

function paintExitRoiBtns(){
  $("exitRoiDraw").classList.toggle("on", !!ROI_DRAW);
  $("exitRoiDraw").textContent = ROI_DRAW ? "在畫面上拖曳框出檢測／出場區…（Esc 取消）"
                               : EXIT_ROI ? "重畫檢測／出場區" : "畫檢測／出場區";
  $("exitRoiClear").hidden = !EXIT_ROI || !!ROI_DRAW;
}

async function saveExitRoi(rect){
  try{
    const r = await fetch("/api/exit_roi", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({rect})});
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    EXIT_ROI = snapRoi(d.exit_roi);
    if (CFG) CFG.cfg.exit_roi = d.exit_roi;
  }catch(e){ $("err").textContent = "Could not save the exit area: " + e.message; }
  paintExitRoiBtns(); positionExitRoi();
}

function stopRoiDraw(){ ROI_DRAW = null; paintExitRoiBtns(); positionExitRoi(); }

$("exitRoiDraw").onclick = () => {
  if (ROI_DRAW) return stopRoiDraw();                        // pressed again: cancel
  if (!pictureEl()){ $("err").textContent = "No picture to draw the exit area on yet."; return; }
  ROI_DRAW = {}; paintExitRoiBtns(); positionExitRoi();
};
function roiFrac(e){
  const b = $("exitRoiLayer").getBoundingClientRect();
  return [Math.min(1, Math.max(0, (e.clientX - b.left) / b.width)),
          Math.min(1, Math.max(0, (e.clientY - b.top) / b.height))];
}
$("exitRoiLayer").addEventListener("pointerdown", e => {
  if (!ROI_DRAW) return;
  e.preventDefault();
  try { $("exitRoiLayer").setPointerCapture(e.pointerId); } catch (err) {}   // keeps the drag past the edge
  const [x, y] = roiFrac(e);
  ROI_DRAW = {x0: x, y0: y, x1: x, y1: y};
  positionExitRoi();
});
$("exitRoiLayer").addEventListener("pointermove", e => {
  if (!ROI_DRAW || ROI_DRAW.x0 == null) return;
  [ROI_DRAW.x1, ROI_DRAW.y1] = roiFrac(e);
  positionExitRoi();
});
$("exitRoiLayer").addEventListener("pointerup", e => {
  if (!ROI_DRAW || ROI_DRAW.x0 == null) return;
  [ROI_DRAW.x1, ROI_DRAW.y1] = roiFrac(e);
  const rect = roiRect(ROI_DRAW);
  ROI_DRAW = null;
  saveExitRoi(rect);
});
document.addEventListener("keydown", e => { if (e.key === "Escape" && ROI_DRAW) stopRoiDraw(); });
$("exitRoiClear").onclick = () => {
  const b = $("exitRoiClear");
  if (!b.classList.contains("arming")){
    b.classList.add("arming"); b.textContent = "再按一次：清除檢測／出場區";
    ROI_CLEAR_TIMER = setTimeout(() => { b.classList.remove("arming"); b.textContent = "清除"; }, 3000);
    return;
  }
  clearTimeout(ROI_CLEAR_TIMER);
  b.classList.remove("arming"); b.textContent = "清除";
  saveExitRoi(null);
};

// ── walking track ───────────────────────────────────────────────────────
// The positions the trigger loop already computed, replayed as a floor path with an
// arrow for the direction. Purely a view: nothing here feeds a decision, and the same
// /api/image_trigger poll that drives the status bar carries it, so there is no extra
// request and no extra inference — a few line segments at ~7 fps.
let SHOW_TRACK = true;
try { SHOW_TRACK = localStorage.getItem("showTrack") !== "0"; } catch (e) {}
$("showTrack").checked = SHOW_TRACK;
$("showTrack").onchange = () => {
  SHOW_TRACK = $("showTrack").checked;
  try { localStorage.setItem("showTrack", SHOW_TRACK ? "1" : "0"); } catch (e) {}
  if (!SHOW_TRACK) $("trackCv").classList.remove("on");
};

function placeOver(el, target){
  if (!target || !target.offsetWidth){ el.classList.remove("on"); return false; }
  el.style.left = target.offsetLeft + "px";
  el.style.top = target.offsetTop + "px";
  if (el.width !== target.offsetWidth || el.height !== target.offsetHeight){
    el.width = target.offsetWidth;          // resizing a canvas also clears it
    el.height = target.offsetHeight;
  }
  return true;
}

// Person boxes exactly as the trigger counted them this tick. Drawn while the image
// trigger is armed, because the question these answer — "why did it say two people?" —
// only arises when it is. Amber = the subject the dwell is judged on, red = another box
// that counts toward the crowd rule, faint grey dashed = a person too small to count,
// blue = under the area threshold but followed in a visit (a side-on worker coming out
// of the door, started at the exit fraction) — not at the gate, still judged in/out.
// Two red boxes sitting on one worker is a phantom split; that is the thing to look for.
function drawBoxes(g, s, W, H){
  const boxes = (s && s.enabled && s.watching && s.boxes) || [];
  const k = Math.max(1, Math.min(W, H) / 640);
  for (const b of boxes){
    const [x1, y1, x2, y2, score, area, flag, tid, intent, feetOut] = b;
    const x = x1 * W, y = y1 * H, w = (x2 - x1) * W, h = (y2 - y1) * H;
    g.setLineDash(flag ? [] : [7 * k, 5 * k]);
    g.strokeStyle = flag === 2 ? COL.person : flag === 1 ? COL.ng
                  : flag === 3 ? COL.pred : COL.bystander;
    g.lineWidth = Math.max(2, (flag ? 3 : 2) * k);
    g.globalAlpha = flag ? 1 : 0.55;
    g.strokeRect(x, y, w, h);
    g.setLineDash([]);
    if (flag){
      // The track ID first: whether it stays the same number while a worker walks
      // through is exactly what the 10 Hz loop is being tried for. "#?" = not yet confirmed.
      // 進場 / 出場 appears once the ID reaches the door zone — what the gate has
      // decided this person is doing, read off which side they came from.
      const label = `#${tid ?? "?"}${intent ? " " + intent : ""}${feetOut ? " 腳✓" : ""}`
                  + `  ${fmtScore(score)}  ${fmtArea(area)}`;
      g.font = `${Math.max(10, Math.round(11.5 * k))}px ui-monospace,Menlo,monospace`;
      const tw = g.measureText(label).width + 10, th = 18 * k;
      const ty = y - th >= 0 ? y - th : y;
      g.globalAlpha = .78;
      g.fillStyle = flag === 2 ? COL.person : flag === 3 ? COL.pred : COL.ng;
      g.fillRect(x, ty, tw, th);
      g.globalAlpha = 1;
      g.fillStyle = "#fff";
      g.textBaseline = "middle";
      g.fillText(label, x + 5, ty + th / 2);
    }
    if (EXIT_ROI && tid != null){
      // The feet (bottom-centre) — what the exit area reads: filled when inside it now.
      const fx = (x1 + x2) / 2, fy = y2;
      const inside = fx >= EXIT_ROI[0] && fx <= EXIT_ROI[2] && fy >= EXIT_ROI[1] && fy <= EXIT_ROI[3];
      g.globalAlpha = 1;
      g.beginPath();
      g.arc(fx * W, fy * H, 6 * k, 0, 2 * Math.PI);
      g.lineWidth = Math.max(2, 2.5 * k);
      g.strokeStyle = "#06b6d4";
      if (inside){ g.fillStyle = "#06b6d4"; g.fill(); }
      g.stroke();
    }
    g.globalAlpha = 1;
  }
}

// The checklist's item boxes on the live view, as the trigger saw them this tick: green
// for the item (helmet, harness), red for its veto class (no-helmet, …), labelled with the
// item and score. The same classes and thresholds the check itself counts.
function drawPpe(g, s, W, H){
  const boxes = (s && s.enabled && s.watching && s.ppe) || [];
  const k = Math.max(1, Math.min(W, H) / 640);
  g.font = `${Math.max(10, Math.round(11 * k))}px ui-monospace,Menlo,monospace`;
  g.textBaseline = "middle";
  for (const [x1, y1, x2, y2, score, label, cls, kind] of boxes){
    const x = x1 * W, y = y1 * H, w = (x2 - x1) * W, h = (y2 - y1) * H;
    const col = kind ? COL.ok : COL.ng;
    g.globalAlpha = 1;
    g.lineWidth = Math.max(2, 2.5 * k);
    g.strokeStyle = col;
    g.strokeRect(x, y, w, h);
    const text = `${kind ? label : cls} ${fmtScore(score)}`;
    const tw = g.measureText(text).width + 8, th = 16 * k;
    const ty = y + h + th <= H ? y + h : y - th;          // under the box, so it never
    g.globalAlpha = .8;                                    // sits on the person's own tag
    g.fillStyle = col;
    g.fillRect(x, ty, tw, th);
    g.globalAlpha = 1;
    g.fillStyle = "#fff";
    g.fillText(text, x + 4, ty + th / 2);
  }
}

function drawTrack(s){
  const el = $("trackCv");
  if (!placeOver(el, liveEl())){ el.classList.remove("on"); return; }
  const pts = (SHOW_TRACK && s && s.track) || [];
  const g = el.getContext("2d");
  g.clearRect(0, 0, el.width, el.height);
  drawBoxes(g, s, el.width, el.height);
  drawPpe(g, s, el.width, el.height);
  const anyBox = s && s.enabled && s.watching
    && ((s.boxes && s.boxes.length) || (s.ppe && s.ppe.length));
  if (pts.length < 2){ el.classList.toggle("on", !!anyBox); return; }
  el.classList.add("on");

  const W = el.width, H = el.height;
  const xy = pts.map(p => [p[1] * W, p[2] * H]);
  const k = Math.max(1, Math.min(W, H) / 640);
  const oldest = pts[0][0] || 1;

  // Trail, older segments faded — direction reads without needing an animation.
  g.lineCap = "round"; g.lineJoin = "round";
  g.strokeStyle = COL.person;
  g.lineWidth = Math.max(2, 3.5 * k);
  for (let i = 1; i < xy.length; i++){
    g.globalAlpha = Math.max(0.12, 1 - pts[i][0] / oldest);
    g.beginPath();
    g.moveTo(xy[i-1][0], xy[i-1][1]);
    g.lineTo(xy[i][0], xy[i][1]);
    g.stroke();
  }
  g.globalAlpha = 1;

  // Arrow aimed along the last stretch of travel, not the last sample: consecutive
  // samples jitter by a pixel or two and a per-sample arrow spins on the spot.
  const head = xy[xy.length - 1];
  let ref = xy[0];
  for (let i = xy.length - 1; i >= 0; i--){
    if (Math.hypot(head[0]-xy[i][0], head[1]-xy[i][1]) > 12 * k){ ref = xy[i]; break; }
  }
  const dx = head[0] - ref[0], dy = head[1] - ref[1];
  if (Math.hypot(dx, dy) > 4 * k){
    const a = Math.atan2(dy, dx), size = 14 * k;
    g.fillStyle = COL.person;
    g.beginPath();
    g.moveTo(head[0] + Math.cos(a) * size, head[1] + Math.sin(a) * size);
    g.lineTo(head[0] + Math.cos(a + 2.5) * size * .8, head[1] + Math.sin(a + 2.5) * size * .8);
    g.lineTo(head[0] + Math.cos(a - 2.5) * size * .8, head[1] + Math.sin(a - 2.5) * size * .8);
    g.closePath(); g.fill();
  }
  g.fillStyle = COL.person;
  g.beginPath(); g.arc(head[0], head[1], 5 * k, 0, 7); g.fill();

  // Net horizontal travel is exactly what the exit rule reads, so name it in those terms.
  const net = pts[pts.length - 1][1] - pts[0][1];
  if (Math.abs(net) > 0.015){
    const label = (net < 0 ? "← " : "→ ") + Math.abs(Math.round(net * 100)) + "%  "
                + (net < 0 ? "toward the gate" : "toward the entrance");
    g.font = `${Math.max(11, Math.round(12.5 * k))}px ui-monospace,Menlo,monospace`;
    const h = 22 * k, tw = g.measureText(label).width + 14;
    const tx = Math.min(Math.max(head[0] - tw / 2, 4), Math.max(4, W - tw - 4));
    const ty = Math.max(head[1] - 36 * k, 4);
    g.fillStyle = "rgba(15,15,15,.72)";
    g.fillRect(tx, ty, tw, h);
    g.fillStyle = "#fff";
    g.textBaseline = "middle";
    g.fillText(label, tx + 7, ty + h / 2);
  }
}

// Moves one edge to fraction x, never letting it cross (or crowd) the other one.
function setZoneEdge(which, x){
  x = Math.min(1, Math.max(0, x));
  if (which === "L") ZONE = [Math.min(x, ZONE[1] - ZONE_MIN_GAP), ZONE[1]];
  else               ZONE = [ZONE[0], Math.max(x, ZONE[0] + ZONE_MIN_GAP)];
  ZONE = [Math.max(0, ZONE[0]), Math.min(1, ZONE[1])];
  paintZone();
}

// Pointer handling on the whole bar. Pressing picks whichever edge is nearer (a press
// ON a handle picks that handle, so two handles sitting together are still separable)
// and captures the pointer, so the drag keeps working even when the mouse leaves the
// bar. Saved once, on release — not on every move.
let ZONE_DRAG = null;
const zoneDual = $("zoneDual");
const zoneX = clientX => {
  const r = zoneDual.getBoundingClientRect();
  return r.width ? (clientX - r.left) / r.width : 0;
};
zoneDual.onpointerdown = e => {
  if (e.button !== 0 && e.pointerType === "mouse") return;
  const x = zoneX(e.clientX);
  ZONE_DRAG = e.target.id === "zoneHL" ? "L" : e.target.id === "zoneHR" ? "R"
            : (Math.abs(x - ZONE[0]) <= Math.abs(x - ZONE[1]) ? "L" : "R");
  zoneDual.classList.add("dragging");
  $(ZONE_DRAG === "L" ? "zoneHL" : "zoneHR").classList.add("active");
  zoneDual.setPointerCapture(e.pointerId);
  setZoneEdge(ZONE_DRAG, x);
  e.preventDefault();
};
zoneDual.onpointermove = e => { if (ZONE_DRAG) setZoneEdge(ZONE_DRAG, zoneX(e.clientX)); };
zoneDual.onpointerup = zoneDual.onpointercancel = e => {
  if (!ZONE_DRAG) return;
  zoneDual.classList.remove("dragging");
  $("zoneHL").classList.remove("active"); $("zoneHR").classList.remove("active");
  ZONE_DRAG = null;
  saveZone();
};
for (const [id, which] of [["zoneHL", "L"], ["zoneHR", "R"]]){
  $(id).onkeydown = e => {
    const d = e.key === "ArrowLeft" ? -ZONE_KEY_STEP : e.key === "ArrowRight" ? ZONE_KEY_STEP : 0;
    if (!d) return;
    e.preventDefault();
    setZoneEdge(which, ZONE[which === "L" ? 0 : 1] + d);
  };
  $(id).onkeyup = e => { if (e.key === "ArrowLeft" || e.key === "ArrowRight") saveZone(); };
}

async function saveZone(){
  try{
    const r = await fetch("/api/trigger_zone", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({left: ZONE[0], right: ZONE[1]})});
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    if (CFG) CFG.cfg.trigger_zone = d.trigger_zone;
  }catch(e){
    $("err").textContent = "Could not save the door zone: " + e.message;
  }
}

new ResizeObserver(positionZoneOverlay).observe($("live"));
new ResizeObserver(positionZoneOverlay).observe(cv);

// The live frame is only --stream-height (720) px tall, and an <img> under max-width /
// max-height shrinks to fit but never grows — on a big screen it sat in the middle of
// the panel with a margin all round (operator, 2026-10-04: 貼齊背後方框). So while the
// live view is up, the picture is sized to the largest box of its own shape that the
// column has room for, and the panel hugs it ("hug"): no grey band on any side. The
// element's box is still exactly the rendered picture, which every overlay relies on.
// Paused, or showing an uploaded result, the panel goes back to filling the column.
function fitLive(){
  const img = $("live"), panel = $("imgPanel"), col = $("imgCol");
  const on = img.classList.contains("on") && img.naturalWidth && img.naturalHeight;
  panel.classList.toggle("hug", !!on);
  if (!on){ img.style.width = img.style.height = ""; return; }
  const ps = getComputedStyle(panel), cs = getComputedStyle(col);
  const padX = parseFloat(ps.paddingLeft) + parseFloat(ps.paddingRight);
  const padY = parseFloat(ps.paddingTop) + parseFloat(ps.paddingBottom);
  // The room the column leaves the panel: its height less every other shown row + gaps.
  const rows = [...col.children].filter(c => c !== panel && c.offsetParent !== null);
  const gap = parseFloat(cs.rowGap) || 0;
  const H = col.clientHeight - rows.reduce((a, c) => a + c.offsetHeight, 0)
            - gap * rows.length - padY;
  const W = col.clientWidth - padX;
  if (W <= 0 || H <= 0) return;
  const k = Math.min(W / img.naturalWidth, H / img.naturalHeight);
  const w = Math.floor(img.naturalWidth * k) + "px", h = Math.floor(img.naturalHeight * k) + "px";
  if (img.style.width !== w) img.style.width = w;
  if (img.style.height !== h) img.style.height = h;
}
new ResizeObserver(fitLive).observe($("imgCol"));
$("live").addEventListener("load", fitLive);
// Live on/off (pause, a result taking the panel, back to live) flips #live's class.
new MutationObserver(fitLive).observe($("live"), {attributes: true, attributeFilter: ["class"]});

function applyThresholdsFromCfg(){
  if (!CFG || !CFG.cfg) return;
  if (Array.isArray(CFG.cfg.trigger_zone) && CFG.cfg.trigger_zone.length === 2){
    ZONE = [Number(CFG.cfg.trigger_zone[0]), Number(CFG.cfg.trigger_zone[1])];
    paintZone();
  }
  if (CFG.cfg.exit_area_fraction != null) EXIT_FRAC = Number(CFG.cfg.exit_area_fraction);
  if (CFG.cfg.away_fraction != null) AWAY_FRAC = Number(CFG.cfg.away_fraction);
  if (CFG.cfg.vanish_exit_s != null) VANISH_S = Number(CFG.cfg.vanish_exit_s);
  EXIT_ROI = Array.isArray(CFG.cfg.exit_roi) && CFG.cfg.exit_roi.length === 4
    ? snapRoi(CFG.cfg.exit_roi.map(Number)) : null;
  paintExitRoiBtns(); positionExitRoi();
  paintDwell(CFG.cfg.image_dwell);
  if (CFG.cfg.trigger_min_area != null){
    AREA_MIN = Number(CFG.cfg.trigger_min_area);
    if (AREA_MIN > Number($("areaSlider").max)) $("areaSlider").max = AREA_MIN;
    paintArea();
  }
  if (CFG.cfg.rfid_min_rssi != null){
    RSSI_MIN = Number(CFG.cfg.rfid_min_rssi);
    $("rssiSlider").value = RSSI_MIN;
    paintRssi();
  }
}

async function boot(){
  CFG = await (await fetch("/api/config")).json();
  $("modelName").textContent = (CFG.model || "").split("/").pop() || "?";
  applyThresholdsFromCfg();
  renderItems(null);
  if (CFG.camera){
    startLive();
    pollCam();
    pollLast();
  } else {
    $("cap").style.display = "none";
    $("camState").style.display = "none";
    $("trigger").style.display = "none";
    $("rtBtn").style.display = "none";   // realtime reads the camera; nothing to read
    $("liveToggleBar").style.display = "none";   // nothing to pause without a camera
    $("zoneBar").classList.add("hidden");        // no live feed, no image trigger
  }
  pollDI();
  pollIT();
  pollITReport();
  const prev = await (await fetch("/api/last")).json();
  // A live gate boots into standby — pollLast's first poll records its last result as
  // the baseline without showing it. Only a camera-less server shows it here.
  if (prev && prev.status && !CFG.camera) show(prev);
}

// Live view: polls for one fresh JPEG on an interval — /api/live_frame, not the old
// /api/stream multipart push — exactly like every other "current status" value in
// this app (camState, DI, realtime mode) already works. This is the fix for the
// streaming-delay investigation, not just a pause button: a server-PUSHED
// multipart/x-mixed-replace feed gives the browser no way to skip ahead if its own
// JPEG decode+repaint falls behind, which it reliably will sharing this Jetson's CPU
// with the very process producing the stream (camera decode + inference + serving) —
// the backlog just grows, unbounded, for as long as the tab stays open (this is why
// it grew from "5 seconds" to "20+ seconds" over the session, not a one-time glitch).
// Polling structurally cannot accumulate a backlog: each tick shows whatever the
// camera thread's buffer holds AT THAT MOMENT (same latest_frame() the server uses
// for CAPTURE & CHECK), and a slow tick just means fewer fresh frames, never a queue
// of stale ones.
const LIVE_POLL_MS = 250;
let LIVE_PAUSED = false;
let LIVE_TIMER = null;
let LIVE_BUSY = false;     // one frame in flight at a time — same guard rtTick() uses
                           // (RT_BUSY); without it a slow server stacks up overlapping
                           // requests, a milder cousin of the very backlog this replaces

function startLive(){
  LIVE_PAUSED = false;
  paintLiveToggle();
  $("live").classList.add("on");
  $("livePaused").classList.remove("on");
  cv.classList.remove("ready");
  $("toLive").classList.remove("on");
  $("imgHint").style.display = "none";
  if (!LIVE_TIMER) liveTick();
}

// Stops the browser decoding the video feed at all — see LIVE_POLL_MS's comment for
// why this was needed even after switching to polling: it also removes the per-tick
// decode+paint cost entirely for anyone who doesn't need to be watching right now.
// CAPTURE & CHECK and the sensor trigger are unaffected either way: both read the
// camera thread's own latest frame server-side, never through this polling loop.
function pauseLive(){
  LIVE_PAUSED = true;
  paintLiveToggle();
  clearTimeout(LIVE_TIMER);
  LIVE_TIMER = null;
  LIVE_BUSY = false;
  $("live").classList.remove("on");
  $("livePaused").classList.add("on");
}

// Cache-busted the same way rtShow() busts /api/realtime_image — every tick is a
// different frame at the same URL. Swaps #live's src only after the new image has
// fully decoded (onload), so the visible frame never flickers to a half-loaded one.
function liveTick(){
  if (LIVE_PAUSED) { LIVE_TIMER = null; return; }
  // While 大字報 is up it polls the same frames itself (demo.js) and covers this view.
  if (!LIVE_BUSY && !window.DEMO_ON) {
    LIVE_BUSY = true;
    const nextImg = new Image();
    nextImg.onload = () => {
      LIVE_BUSY = false;
      if (!LIVE_PAUSED) $("live").src = nextImg.src;
    };
    nextImg.onerror = () => { LIVE_BUSY = false; };   // camera hiccup; next tick retries
    nextImg.src = "/api/live_frame?t=" + Date.now();
  }
  LIVE_TIMER = setTimeout(liveTick, LIVE_POLL_MS);
}

function paintLiveToggle(){
  const btn = $("liveToggle");
  btn.classList.toggle("paused", LIVE_PAUSED);
  btn.innerHTML = LIVE_PAUSED ? "&#9654; Resume live view" : "&#9208; Pause live view";
}

$("liveToggle").onclick = () => { LIVE_PAUSED ? startLive() : pauseLive(); };

// Switch the panel to the annotated result (called from show()). A result image takes
// over the panel regardless of pause state, and stops the live poll loop while it's
// showing — nobody's looking at the live feed underneath it. startLive() (Back to
// live, or a fresh boot) restarts the loop.
function showResult(){
  $("live").classList.remove("on");
  $("livePaused").classList.remove("on");
  if (CFG.camera) $("toLive").classList.add("on");
  clearTimeout(LIVE_TIMER);
  LIVE_TIMER = null;
  LIVE_BUSY = false;
}

async function pollCam(){
  try{
    const s = await (await fetch("/api/camera_status")).json();
    const el = $("camState");
    if (s.connected){ el.textContent = "camera: live"; el.className = "tag up"; }
    else { el.textContent = "camera: connecting…"; el.className = "tag down"; }
  }catch(e){}
  setTimeout(pollCam, 2000);
}

// Auto-refresh the kiosk display when a NEW result lands server-side — the whole point
// of a sensor trigger is that nobody has to be at the keyboard for the screen to show
// it. frame_id is unique per capture (store_frame() mints a fresh one every call), so
// comparing it against whatever's currently shown (LAST) is enough to detect a new one
// without re-rendering identical data on every tick. Skips while realtime mode owns the
// display — that has its own continuous polling/redraw (rtTick), and the two would
// otherwise fight over the panel.
// The first poll only takes a BASELINE: whatever result the gate already holds may be
// hours old, and a kiosk that has just loaded must stand by with 「—」, not replay it.
let LAST_BASELINE = false;
async function pollLast(){
  if (!RT_ON && !PB.active){          // realtime and folder playback both own the panel
    try{
      const d = await (await fetch("/api/last")).json();
      if (!LAST_BASELINE){
        LAST_BASELINE = true;
        if (d && d.frame_id) LAST = d;
      } else if (d && d.frame_id && (!LAST || d.frame_id !== LAST.frame_id)) showCheck(d);
    }catch(e){}
  }
  setTimeout(pollLast, 300);
}

// Through-beam sensor status, from the RFID box's GPIO input(s) — see /api/rfid_status.
// Independent of the camera, so this polls regardless of CFG.camera. Hides the whole
// bar when RFID isn't configured on this server at all (--rfid-host not given), same
// as camState hides when there's no camera.
//
// DI_POLL_MS matches RT_POLL_MS below rather than camState's 2000ms: the backend side
// is already effectively instant (RfidService._on_gpio updates the second the box
// pushes a change, no polling there), so the display lag people actually see is just
// this interval. 150ms keeps it well under what a person perceives as "delayed" while
// still being nowhere near tight enough to matter for server/network load.
const DI_POLL_MS = 150;
let SENSOR_TOGGLE_BUSY = false;   // suppress one poll tick right after a click, so the
                                   // server's echoed state doesn't race a fresh click

function paintSensorToggle(pin, on){
  const btn = $("sensorToggle");
  if (pin == null){ btn.classList.add("hidden"); return; }
  btn.classList.remove("hidden");
  btn.classList.toggle("off", !on);
  btn.textContent = `Sensor Trigger (IN${pin}): ${on ? "ON" : "OFF"}`;
}

// The bar itself now hosts both triggers, so it only disappears when NEITHER exists
// (no reader and no camera); each half hides on its own.
let RFID_ON = false;   // reader configured on this server — gates the "See RFID read" popup

async function pollDI(){
  try{
    const s = await (await fetch("/api/rfid_status")).json();
    const el = $("diState");
    RFID_ON = !!s.enabled;
    if (!s.enabled){
      el.classList.add("hidden");
      paintSensorToggle(null, false);
    } else {
      el.classList.remove("hidden");
      const pins = Object.keys(s.gpio || {}).sort();
      el.textContent = !pins.length ? "Digital Input: —"
        : pins.length === 1 ? "Digital Input: " + s.gpio[pins[0]]
        : "Digital Input: " + pins.map(p => `IN${p}=${s.gpio[p]}`).join("  ");
      if (!SENSOR_TOGGLE_BUSY) paintSensorToggle(s.sensor_pin, s.sensor_enabled);
    }
    $("diBar").classList.toggle("hidden", !s.enabled && !(CFG && CFG.camera));
  }catch(e){}
  setTimeout(pollDI, DI_POLL_MS);
}

// ── image trigger status ────────────────────────────────────────────────
// Polled at the same rate as the DI level: the interesting part is watching the dwell
// count up while someone stands at the gate, which needs to feel live.
let IT_TOGGLE_BUSY = false;

function paintImageTrigger(s){
  const btn = $("itToggle"), st = $("itState");
  if (!s || !s.available){ btn.classList.add("hidden"); st.classList.add("hidden"); return; }
  btn.classList.remove("hidden"); st.classList.remove("hidden");
  btn.classList.toggle("off", !s.enabled);
  btn.classList.toggle("on", s.enabled && s.watching);
  btn.textContent = `Image Trigger: ${s.enabled ? "ON" : "OFF"}`;

  let txt, armed = false;
  const at = s.person_area > 0
    ? `${s.subject_intent ? s.subject_intent + " · " : ""}person ${fmtArea(s.person_area)} px²` : "";
  if (!s.enabled) txt = "off";
  else if (!s.watching) txt = "waiting for camera";
  else if (s.cooldown > 0) txt = `cooldown ${s.cooldown.toFixed(1)}s`;
  else if (s.people > 1) txt = `${s.people} people at the gate — one at a time`;
  else if (s.dwell > 0){
    armed = true;
    txt = `${at} · dwell ${s.dwell.toFixed(1)} / ${s.dwell_s}s`;
    // Counting continues through a dropout; show it so a flickering detection is
    // visible as "recovering" rather than looking like a stuck timer.
    if (s.miss > 0) txt += ` · missed ${s.miss}/${s.miss_limit}s`;
  }
  else if (s.person_area > 0 && !s.in_zone) txt = `${at} — outside door zone (centre ${Math.round((s.person_cx ?? 0) * 100)}%)`;
  // In the zone and big enough, but the feet are off the check spot (exit_roi): the
  // dwell waits — said here, or a worker standing beside the spot looks ignored.
  else if (s.person_area > 0 && s.on_spot === false) txt = `${at} · in zone — 腳不在檢測位置`;
  else if (s.person_area > 0) txt = `${at} · in zone`;
  else txt = "watching · nobody";
  if (s.last_event){
    const ev = s.last_event, what = {check: "checked", no_tag: "no tag → ID讀取失敗",
      reader_down: "reader down (silent, red)", crowd: "crowd → 檢測口請淨空",
      multi: `${ev.epcs.length} tags → 檢測口請淨空`,
      intrusion: "walked in unchecked → 擅自闖入", fail_entered: "entered after FAIL → 檢測未通過",
      out_unchecked: "walked out unchecked → 未檢查即出場", out_fail: "left after FAIL → 未通過仍出場",
      out_refused: "left after refusal → 未檢查即出場",
      pass_entry: "passed → 請進場", pass_leave: "passed → 請出場",
      unregistered: "badge not on whitelist → ID未登入"}[ev.outcome] || ev.outcome;
    txt += `  ·  last ${ev.t.slice(11)} ${what}`;
  }
  st.textContent = txt;
  st.classList.toggle("armed", armed);
}

// ── who decides in/out: IT's access_type, or our own track ─────────────────
let DIR_BUSY = false;
function paintDirSource(s){
  if (!s || !s.direction_source) return;
  const sel = $("dirSource"), hint = $("dirHint");
  if (!DIR_BUSY && document.activeElement !== sel) sel.value = s.direction_source;
  // IT mode with IT reporting off: nobody answers, so the track's direction is what
  // is actually used and a PASS says the plain 「檢測通過」 — say so, not silently.
  const noIT = s.direction_source === "it" && !s.it_live;
  hint.hidden = !noIT;
  hint.textContent = noIT ? "IT Report 關閉中：暫用軌跡方向" : "";
}
// ── how long a person stands in the door zone before the check runs ─────────
// gate.json image_dwell (the server clamps and saves it; --image-dwell is only the start).
function paintDwell(sec){
  if (sec == null || document.activeElement === $("dwellSec")) return;
  $("dwellSec").value = Number(sec).toFixed(1);
}
$("dwellSec").onkeydown = e => { if (e.key === "Enter") $("dwellSec").blur(); };
$("dwellSec").onchange = async () => {
  const v = Number($("dwellSec").value);
  try{
    if (isNaN(v)) throw new Error("not a number");
    const r = await fetch("/api/image_dwell", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({seconds: v})});
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    if (CFG) CFG.cfg.image_dwell = d.image_dwell;
    paintDwell(d.image_dwell);
  }catch(e){
    $("err").textContent = "Could not save the dwell time: " + e.message;
    if (CFG) paintDwell(CFG.cfg.image_dwell);
  }
};

// ── which side of the picture the door is on ────────────────────────────
let DOOR_BUSY = false;
function paintDoorSide(s){
  if (!s || !s.door_side) return;
  const sel = $("doorSide");
  if (!DOOR_BUSY && document.activeElement !== sel) sel.value = s.door_side;
  if (s.door_side !== DOOR_SIDE){ DOOR_SIDE = s.door_side; positionZoneOverlay(); }
}
$("doorSide").onchange = async () => {
  DOOR_BUSY = true;
  try{
    const r = await fetch("/api/door_side", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({side: $("doorSide").value})});
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    DOOR_SIDE = d.door_side; positionZoneOverlay();
  }catch(e){ $("err").textContent = "Could not switch the restricted-area side: " + e.message; }
  finally{ DOOR_BUSY = false; }
};

$("dirSource").onchange = async () => {
  DIR_BUSY = true;
  try{
    const r = await fetch("/api/direction_source", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({source: $("dirSource").value})});
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
  }catch(e){ $("err").textContent = "Could not switch the in/out source: " + e.message; }
  finally{ DIR_BUSY = false; }
};

// ── tower light, as the gate is driving it ───────────────────────────────
// The kiosk mirrors the light so an operator (or a test) can see it without looking up
// at the tower — and sees at once when the tower itself has stopped answering.
function paintLight(l){
  const el = $("lightState");
  if (!l){ el.textContent = "light: no tower"; el.className = "tag lightTag"; return; }
  if (l.tower_ok === false){
    el.textContent = "light: TOWER UNREACHABLE"; el.className = "tag lightTag red"; return;
  }
  const colour = (l.light || "").split("_")[0];
  el.textContent = "light: " + (l.label || "…") + (l.light === "red" && l.reason ? " — " + l.reason : "");
  el.className = "tag lightTag " + colour + ((l.light || "").endsWith("_flash") ? " flash" : "");
}

// ── alarm panel ─────────────────────────────────────────────────────────
// Shows what the gate last ANNOUNCED, not what the checklist decided — they differ
// whenever a refusal happens before the burst (a crowd, a missing badge, someone
// walking in). The Chinese comes from the server's ALARM_TEXT so the screen and the
// MP3s stay one list; a "check" outcome defers to the verdict, which arrives
// separately via /api/last.
let ALARM_TEXT = null;

function paintAlarm(s){
  if (s && s.alarm_text) ALARM_TEXT = s.alarm_text;
  if (!ALARM_TEXT) return;
  // Only while the server says it is still announcing — once that expires the screen
  // goes back to standby, so a stale verdict never greets the next worker.
  const box = $("alarmBox"), ev = (s && s.alarm_active) ? s.last_event : null;
  let key = null, when = "", pending = false;

  if (ev){
    when = ev.t ? ev.t.slice(11) : "";
    if (ev.outcome === "check"){
      // The server sets this event the moment the dwell completes, BEFORE the burst runs;
      // until this check's own result is in, LAST is the PREVIOUS check, and showing it
      // flashed a stale 「檢測通過」 for ~1 s (2026-10-05). A result belongs to the event
      // when it is at least as new (both "YYYY-MM-DD HH:MM:SS", so strings compare).
      const fresh = LAST && LAST.ts && ev.t && LAST.ts >= ev.t;
      key = fresh ? ({PASS: "pass", NO_WORKER: "no_worker"}[LAST.status] || "fail") : null;
      pending = !fresh;
    } else key = ev.outcome;
  }
  if (window.demoAlarm) window.demoAlarm(key, s, pending);   // 大字報's banner (demo.js)
  const t = key && ALARM_TEXT[key];
  if (!t){
    box.className = "idle";
    $("alarmMain").textContent = pending ? "檢測中…" : "待命中";
    $("alarmSub").textContent = pending ? "" : "等待人員進入閘門";
    $("alarmWhen").textContent = "";
    return;
  }
  box.className = key.startsWith("pass") ? "ok" : "alarm";   // pass, pass_entry, pass_leave
  $("alarmMain").textContent = t[0];
  $("alarmSub").textContent = t[1] || "";
  $("alarmWhen").textContent = when;
}

async function pollIT(){
  try{
    const s = await (await fetch("/api/image_trigger")).json();
    if (!IT_TOGGLE_BUSY) paintImageTrigger(s);
    trackDirection(s);
    paintAlarm(s);
    paintLight(s.light);
    paintDirSource(s);
    paintDoorSide(s);
    drawTrack(s);
  }catch(e){}
  setTimeout(pollIT, DI_POLL_MS);
}

// IT reporting status — every 5 s is plenty; the heartbeat itself is every 30 s.
let IT_BUSY = false;

let IT_ON = false, IT_ARM_TIMER = null;
function paintITButton(s){
  const btn = $("itEnable");
  if (!s.available){ btn.classList.add("hidden"); return; }
  btn.classList.remove("hidden");
  IT_ON = !!s.enabled;
  if (btn.classList.contains("arming")) return;     // waiting for the confirming click
  // Colours flipped relative to the sensor toggle: OFF here is a deliberate, ordinary
  // state (not yet commissioned), so it stays neutral; ON is what should be confirmed.
  btn.classList.toggle("on", !!s.enabled);
  btn.classList.remove("off");
  btn.textContent = `IT Report: ${s.enabled ? "ON" : "OFF"}`;
}

// Switching ON takes a second click on the button itself, not a confirm() dialog. A
// browser told to "prevent this page from creating additional dialogs" — or one that
// blocks dialogs outright — answers confirm() with Cancel without showing anything, and
// the button then silently did nothing: no request, no error (2026-10-09). OFF is one
// click, as before.
function disarmIT(){
  clearTimeout(IT_ARM_TIMER); IT_ARM_TIMER = null;
  $("itEnable").classList.remove("arming");
}
$("itEnable").onclick = async () => {
  const btn = $("itEnable"), want = !IT_ON;
  if (want && !btn.classList.contains("arming")){
    btn.classList.add("arming");
    btn.textContent = "再按一次：開始上報 IT";
    $("err").textContent = "IT Report：再按一次確認。從現在開始上報，關閉期間的紀錄不會補送。";
    IT_ARM_TIMER = setTimeout(() => {
      disarmIT(); btn.textContent = "IT Report: OFF"; $("err").textContent = "";
    }, 4000);
    return;
  }
  disarmIT();
  $("err").textContent = "";
  IT_BUSY = true;
  try{
    const r = await fetch("/api/it_enable", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({enabled: want})});
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    paintITButton(d);
  }catch(e){
    $("err").textContent = "Could not switch IT reporting: " + e.message;
  }finally{
    IT_BUSY = false;
  }
};

// The target selector: filled once from the server's list, then kept in step with it
// (another kiosk may switch it). Hidden when the config has no named targets.
let IT_TARGET_BUSY = false;
function paintITTarget(s){
  const sel = $("itTarget"), names = s.targets || [];
  sel.hidden = names.length < 2;
  if (sel.options.length !== names.length)
    sel.innerHTML = names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  if (!IT_TARGET_BUSY && document.activeElement !== sel && s.target) sel.value = s.target;
  sel.classList.toggle("mockTarget", s.target === "mock");
}
$("itTarget").onchange = async () => {
  IT_TARGET_BUSY = true;
  try{
    const r = await fetch("/api/it_target", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({target: $("itTarget").value})});
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    paintITTarget(d);
  }catch(e){ $("err").textContent = "Could not switch IT target: " + e.message; }
  finally{ IT_TARGET_BUSY = false; }
};

async function pollITReport(){
  const el = $("itRep");
  try{
    const s = await (await fetch("/api/it_status")).json();
    if (!IT_BUSY) paintITButton(s);
    paintITTarget(s);
    const dev = s.device_status && s.device_status !== "normal" ? ` · ${s.device_status}` : "";
    if (!s.enabled){
      el.textContent = "IT: off" + dev;
      el.className = "tag" + (dev ? " bad" : "");
    } else {
      const failing = s.hb_ok === false || (s.last_error && s.pending_bytes > 0);
      const kb = s.pending_bytes > 0 ? ` · ${Math.ceil(s.pending_bytes / 1024)} KB waiting` : "";
      el.textContent = `IT → ${s.target || "?"}: sent ${s.sent}${s.rejected ? ` · rejected ${s.rejected}` : ""}${kb}`
        + ` · heartbeat ${s.hb_ok === null ? "…" : s.hb_ok ? "ok" : "FAILING"}${dev}`;
      el.title = (s.last_error || s.hb_error || "IT reporting healthy")
        + `\n${s.result_url || ""}`
        + (s.last_reply ? `\nlast reply: ${JSON.stringify(s.last_reply)}` : "")
        + `\nfabArea ${s.fab_area} · clientId ${s.client_id}`;
      el.className = "tag " + (failing || dev ? "bad" : "good");
    }
  }catch(e){}
  setTimeout(pollITReport, 5000);
}

$("itToggle").onclick = async () => {
  const want = $("itToggle").classList.contains("off");
  IT_TOGGLE_BUSY = true;
  try{
    const r = await fetch("/api/image_trigger", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({enabled: want})});
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    paintImageTrigger(d);
  }catch(e){
    $("err").textContent = "Could not toggle the image trigger: " + e.message;
  }finally{
    IT_TOGGLE_BUSY = false;
  }
};

// Flips the through-beam sensor trigger on/off at runtime — the pin itself stays
// whatever --sensor-pin set at boot (a hardware fact, not something to change from a
// web page); this only controls whether a beam break is currently allowed to fire a
// check. No confirm() dialog, unlike Shut down: this is fully reversible with one more
// click and leaves the gate running either way, nothing like a ~15s outage.
$("sensorToggle").onclick = async () => {
  const want = $("sensorToggle").classList.contains("off");   // off -> turning on
  SENSOR_TOGGLE_BUSY = true;
  try{
    const r = await fetch("/api/sensor_trigger", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({enabled: want})});
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    paintSensorToggle(d.sensor_pin, d.sensor_enabled);
  }catch(e){
    $("err").textContent = "Could not toggle the sensor trigger: " + e.message;
  }finally{
    SENSOR_TOGGLE_BUSY = false;
  }
};

// One card per checklist row. Rendered before any check so the screen shows the
// items the gate will test, not a blank panel.
const NO_VETO = "";   // sentinel for the negative dropdown's "ignored" option

// Which model class satisfies each checklist item is normally fixed in gate.json, but
// a model trained by someone else uses its own vocabulary — these two dropdowns let
// the operator repoint an item at whatever the ACTIVE model actually calls it, live,
// without editing the file. Options are the active model's own class list (CFG.classes)
// so a typo can never create a mapping that will never match anything.
async function remapItem(label, cls, neg){
  try{
    const r = await fetch("/api/item_class", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({label, class: cls, negative: neg})});
    const d = await r.json().catch(() => ({error:`HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    CFG = await (await fetch("/api/config")).json();   // pick up the saved mapping
    renderItems(PANEL);
  }catch(e){ $("err").textContent = "Could not remap that item: " + e.message; }
}

// When the stored mapping names a class the ACTIVE model doesn't have — which is exactly
// the state right after force-switching to a model with a different vocabulary — no
// <option> would match and the browser would silently display the FIRST option instead,
// making the item look already-remapped while gate.json still says otherwise. So the
// stale value is added explicitly, selected and flagged, to show the real state.
function classOptions(options, selected){
  const stale = selected && !options.includes(selected)
    ? `<option value="${esc(selected)}" selected>⚠ ${esc(selected)} — not in this model</option>`
    : "";
  return stale + options.map(c =>
    `<option value="${esc(c)}"${c === selected ? " selected" : ""}>${esc(c)}</option>`).join("");
}

async function setItemConf(label, conf){
  try{
    const r = await fetch("/api/item_conf", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({label, conf: Number(conf)})});
    const d = await r.json().catch(() => ({error:`HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    CFG = await (await fetch("/api/config")).json();
    renderItems(PANEL);
    if (LAST) draw(LAST);
  }catch(e){ $("err").textContent = "Could not set that threshold: " + e.message; }
}

// Switch on an item card: in or out of the verdict. The server may refuse (it won't
// let the LAST enabled item be switched off — a gate with nothing to judge fails
// everyone silently); on refusal the checkbox is flipped back so the screen never
// shows a state the server didn't accept. On success the card re-renders from CFG, and
// the stale LAST result is re-evaluated client-side... except it can't be: the verdict
// (PASS/FAIL) was computed server-side against the old checklist. So the verdict text
// is left as-is until the next check, and only the row's own badge updates — an
// honest reflection of what has actually been re-judged.
async function setItemEnabled(label, enabled, box){
  try{
    const r = await fetch("/api/item_enabled", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({label, enabled})});
    const d = await r.json().catch(() => ({error:`HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    $("err").textContent = "";
    CFG = await (await fetch("/api/config")).json();
    if (LAST && LAST.items) LAST.items.forEach(i => { if (i.label === label) i.enabled = enabled; });
    renderItems(PANEL);
    if (LAST) draw(LAST);
  }catch(e){
    if (box) box.checked = !enabled;              // server said no — reflect its state
    $("err").textContent = "Could not switch that item: " + e.message;
  }
}

// Pencil on an item card: swap the heading for a text box in place. Enter or clicking
// away saves; Esc (or an unchanged/empty name) just puts the heading back. The whole
// card re-renders from CFG afterwards, so every label-keyed handler on it (remap,
// threshold) picks up the new name — that's why nothing here patches the DOM by hand.
function editItemName(card, label){
  const nameEl = card.querySelector(".name");
  const btn = card.querySelector(".editName");
  if (!nameEl || card.querySelector(".nameInput")) return;    // already editing
  const input = document.createElement("input");
  input.className = "nameInput";
  input.value = label;
  input.maxLength = 40;
  nameEl.replaceWith(input);
  btn.style.display = "none";
  input.focus();
  input.select();

  let done = false;
  const restore = () => {
    if (done) return;
    done = true;
    input.replaceWith(nameEl);
    btn.style.display = "";
  };
  const commit = async () => {
    if (done) return;
    const v = input.value.trim();
    if (!v || v === label){ restore(); return; }
    done = true;
    input.disabled = true;
    try{
      const r = await fetch("/api/item_label", {method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({label, new_label: v})});
      const d = await r.json().catch(() => ({error:`HTTP ${r.status}`}));
      if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
      $("err").textContent = "";
      CFG = await (await fetch("/api/config")).json();
      // A stale result (LAST) still carries the OLD label in its items[]; re-rendering
      // with it would show "—" for the renamed row until the next check. Patch the
      // label through so the badge stays meaningful.
      if (LAST && LAST.items) LAST.items.forEach(i => { if (i.label === label) i.label = v; });
      renderItems(PANEL);
      if (LAST) draw(LAST);
    }catch(e){
      $("err").textContent = "Could not rename that item: " + e.message;
      done = false;
      input.disabled = false;
      restore();
    }
  };
  input.onkeydown = e => {
    if (e.key === "Enter"){ e.preventDefault(); commit(); }
    else if (e.key === "Escape"){ e.preventDefault(); restore(); }
  };
  input.onblur = commit;
}

async function setPersonConf(conf){
  try{
    const r = await fetch("/api/person_conf", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({conf: Number(conf)})});
    const d = await r.json().catch(() => ({error:`HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    CFG = await (await fetch("/api/config")).json();
    renderItems(PANEL);
  }catch(e){ $("err").textContent = "Could not set the worker threshold: " + e.message; }
}

// The in/out tracker's own, lower person threshold (gate.json track_person_conf). Not
// locked with the checklist: it never decides who is judged, only how long an ID lasts.
async function setTrackConf(conf){
  try{
    const r = await fetch("/api/track_person_conf", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({conf: Number(conf)})});
    const d = await r.json().catch(() => ({error:`HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    CFG = await (await fetch("/api/config")).json();
    renderItems(PANEL);
  }catch(e){ $("err").textContent = "Could not set the tracking threshold: " + e.message; }
}

async function remapPerson(cls){
  try{
    const r = await fetch("/api/person_class", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({class: cls})});
    const d = await r.json().catch(() => ({error:`HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    CFG = await (await fetch("/api/config")).json();
    renderItems(PANEL);
  }catch(e){ $("err").textContent = "Could not set the worker class: " + e.message; }
}

function renderItems(res){
  const el = $("items"); el.innerHTML = "";
  const classes = CFG.classes || [];
  const locked = !CFG.can_switch;

  // Association hangs off this one class: get it wrong and every check is NO_WORKER,
  // however well the three items below are mapped.
  const psel = $("personSel");
  psel.innerHTML = classOptions(classes, CFG.cfg.person_class);
  psel.disabled = locked;
  psel.onchange = () => remapPerson(psel.value);

  // The worker threshold decides whether there is anyone to judge at all, so it gets the
  // same slider+textbox treatment as the item thresholds rather than living only in
  // gate.json where nobody would find it.
  const pcr = $("personConfRange"), pcn = $("personConfNum");
  const pconf = (CFG.cfg.person_conf ?? 0.4).toFixed(2);
  pcr.value = pconf; pcn.value = pconf;
  pcr.disabled = locked; pcn.disabled = locked;
  pcr.oninput  = () => { pcn.value = Number(pcr.value).toFixed(2); };
  pcr.onchange = () => setPersonConf(pcr.value);
  pcn.oninput  = () => { const v = Number(pcn.value); if (!isNaN(v) && v>=0 && v<=1) pcr.value = v; };
  pcn.onchange = () => setPersonConf(pcn.value);

  const tcr = $("trackConfRange"), tcn = $("trackConfNum");
  const tconf = Number(CFG.cfg.track_person_conf ?? 0.6);
  tcr.value = tconf.toFixed(2); tcn.value = tconf.toFixed(2);
  tcr.oninput  = () => { tcn.value = Number(tcr.value).toFixed(2); };
  tcr.onchange = () => setTrackConf(tcr.value);
  tcn.oninput  = () => { const v = Number(tcn.value); if (!isNaN(v) && v>=0.05 && v<=1) tcr.value = v; };
  tcn.onchange = () => setTrackConf(tcn.value);
  // Above Score it has no effect (the server uses the lower of the two) — say so.
  const pc = Number(CFG.cfg.person_conf ?? 0.4);
  $("trackHint").textContent = tconf > pc
    ? `above Score — in effect ${pc.toFixed(2)}` : "keeps following a worker walking off";
  (CFG.cfg.items || []).forEach(item => {
    const label = item.label;
    const enabled = item.enabled !== false;          // absent key = enabled
    const r = res ? res.items.find(i => i.label === label) : null;
    // A switched-off item still gets judged and reported, but its badge must not read
    // like a real verdict — "off" says the row was seen and deliberately not counted.
    const state = !enabled ? "off" : !r ? "idle" : (r.ok ? "ok" : "ng");
    const text  = !enabled ? "off" : !r ? "—"   : (r.ok ? "OK" : "NG");
    const posSel = item.classes && item.classes[0] || "";
    const negSel = item.negatives && item.negatives[0] || NO_VETO;
    const conf = (item.conf ?? 0.4).toFixed(2);
    const d = document.createElement("div");
    d.className = "item" + (enabled ? "" : " off");
    d.innerHTML = `<div class="itemLeft">
        <div class="nameRow">
          <span class="name">${esc(label)}</span>
          <button class="editName" title="Rename this item" ${locked ? "disabled" : ""}>&#9998;</button>
          <label class="judge" title="${enabled
              ? "Counted in the PPE verdict. Switch off to keep watching this item without letting it fail the worker."
              : "NOT counted in the PPE verdict — still detected and shown, but cannot fail the worker."}">
            <input type="checkbox" class="judgeBox" ${enabled ? "checked" : ""} ${locked ? "disabled" : ""}>
            <span class="track"></span>
            <span>${enabled ? "judged" : "not judged"}</span>
          </label>
        </div>
        <div class="remap">
          <label>Detect</label>
          <select class="posSel" ${locked ? "disabled" : ""}>${classOptions(classes, posSel)}</select>
          <label>Veto</label>
          <select class="negSel" ${locked ? "disabled" : ""}>
            <option value="${NO_VETO}"${negSel === NO_VETO ? " selected" : ""}>(ignored — no veto)</option>
            ${classOptions(classes, negSel)}
          </select>
        </div>
        <div class="remap confRow">
          <label>Score &ge;</label>
          <input type="range" class="confRange" min="0" max="1" step="0.01"
                 value="${conf}" ${locked ? "disabled" : ""}>
          <input type="number" class="confNum" min="0" max="1" step="0.01"
                 value="${conf}" ${locked ? "disabled" : ""}>
        </div>
      </div>
      <div><div class="badge ${state}">${text}</div>
      <div class="votes">${r ? `seen ${r.votes}/${r.frames} · need ${r.votes_required}` : ""}</div></div>`;
    const posEl = d.querySelector(".posSel"), negEl = d.querySelector(".negSel");
    const onChange = () => remapItem(label, posEl.value, negEl.value);
    posEl.onchange = onChange;
    negEl.onchange = onChange;
    d.querySelector(".editName").onclick = () => editItemName(d, label);
    d.querySelector(".judgeBox").onchange = e => setItemEnabled(label, e.target.checked, e.target);

    // Slider and textbox are two views of one value: dragging updates the number and
    // repaints the overlay live (draw() filters boxes by this same threshold), but the
    // save only fires on release/commit so a drag isn't dozens of writes to gate.json.
    const rng = d.querySelector(".confRange"), num = d.querySelector(".confNum");
    const preview = v => {
      const it = (CFG.cfg.items || []).find(i => i.label === label);
      if (it) it.conf = Number(v);
      if (LAST) draw(LAST);
    };
    rng.oninput = () => { num.value = Number(rng.value).toFixed(2); preview(rng.value); };
    rng.onchange = () => setItemConf(label, rng.value);
    num.oninput = () => {
      const v = Number(num.value);
      if (!isNaN(v) && v >= 0 && v <= 1){ rng.value = v; preview(v); }
    };
    num.onchange = () => setItemConf(label, num.value);
    el.appendChild(d);
  });
}

// ── realtime collection mode ────────────────────────────────────────────
// Polls /api/realtime, draws the boxes on the exact frame they were computed from, and
// shows a per-frame OK/NG summary. The server decides when to save a training sample;
// this loop only asks. Nothing here plays audio or writes captures.csv — realtime is a
// sampling session, not a gate event.
let RT_ON = false, RT_TIMER = null, RT_BUSY = false;
const RT_POLL_MS = 250;          // display cadence; the SAVE rate is --realtime-interval

function setRealtime(on){
  if (on && PB.active) stopPlayback();     // the two both own the panel; last one wins
  RT_ON = on;
  $("rtBtn").classList.toggle("on", on);
  $("rtInfo").classList.toggle("on", on);
  $("rtBtn").innerHTML = on ? "&#9673; Realtime: ON" : "&#9673; Realtime: off";
  if (on){
    $("err").textContent = "";
    rtTick();
  } else {
    clearTimeout(RT_TIMER);
    $("rtInfo").textContent = "";
    startLive();                 // hand the panel back to the plain camera feed
  }
}

async function rtTick(){
  if (!RT_ON) return;
  if (RT_BUSY){ RT_TIMER = setTimeout(rtTick, RT_POLL_MS); return; }
  RT_BUSY = true;
  try{
    const r = await fetch("/api/realtime");
    const d = await r.json().catch(() => ({error:`HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    rtShow(d);
    // sample_size: what samples are saved at; null = the native-resolution stream is
    // still opening and nothing is saved yet (absent on an older server).
    const sz = d.sample_size === undefined ? ""
             : d.sample_size ? ` · ${d.sample_size[0]}×${d.sample_size[1]}`
             : " · 5MP stream starting…";
    $("rtInfo").textContent = `${d.saved_total} sample(s) saved · ${d.infer_ms} ms${sz}`;
  }catch(e){
    $("err").textContent = "Realtime stopped: " + e.message;
    setRealtime(false);
    return;
  }finally{ RT_BUSY = false; }
  RT_TIMER = setTimeout(rtTick, RT_POLL_MS);
}

function rtShow(d){
  LAST = d;
  PANEL = d;
  paintDirection();
  renderItems(d);
  $("worker").textContent = "—";
  const v = $("verdict");
  v.textContent = d.status === "PASS" ? "Pass"
                : d.status === "NO_WORKER" ? "No worker detected" : "Fail";
  v.className = d.status === "PASS" ? "pass" : "fail";
  $("warn").textContent = d.extra_people
    ? `⚠ ${d.extra_people} other person(s) in frame — judging the closest only.` : "";

  // Cache-busted because every tick is a different frame at the same URL.
  const nextImg = new Image();
  nextImg.onload = () => {
    if (!RT_ON) return;
    img = nextImg;
    $("imgHint").style.display = "none";
    showResult();
    cv.classList.add("ready");
    draw(d);
  };
  nextImg.src = "/api/realtime_image?t=" + Date.now();
}

// `preloaded` (optional): an already-decoded Image to draw on instead of fetching
// /api/frame/<id>. Folder playback passes the local file it just uploaded — the browser
// already holds those bytes, so a second round trip per frame would be pure waste, and
// it would depend on the server's 24-frame cache still holding the id.
// The side panel's text for a result (or the idle state for none) — split out of
// show() so folder playback can put the REAL last result's summary back when it stops,
// without re-fetching and re-drawing that result's picture over the live feed.
// PANEL is the result the side panel is SHOWING — null while it stands by with 「—」.
// Every re-render (a class dropdown, a threshold, a rename) draws from PANEL, never from
// LAST: LAST is the gate's latest result, and painting it into an idle panel is exactly
// the stale verdict the operator saw left behind (2026-10-01).
let PANEL = null;

// 進出場 row: the person at the gate NOW — the image-trigger poll's subject_intent, which
// is IT's access_type once it answered, else the side the track came from — and, with
// nobody there, the checked worker's while their result is on the panel. A result's own
// `intent` is only the track's guess at the verdict and IT may answer otherwise a moment
// later, so the row follows that worker's box (track_id) in the poll and keeps the last
// direction it saw (`dir_seen`): a worker IT called 出場 must not flip back to the
// track's 進場 the moment they walk off.
const INTENT_LABEL = {in: "進場", out: "出場"};
let DIR_LIVE = "";
function trackDirection(s){
  DIR_LIVE = (s && s.enabled && s.watching && s.subject_intent) || "";
  const tid = PANEL && PANEL.track_id;
  if (tid != null && s && s.boxes){
    const b = s.boxes.find(b => b[7] === tid && b[8]);
    if (b) PANEL.dir_seen = b[8];
  }
  paintDirection();
}
function paintDirection(){
  const t = DIR_LIVE || (PANEL && (PANEL.dir_seen || INTENT_LABEL[PANEL.intent])) || "";
  const el = $("direction");
  el.textContent = t || "—";
  el.className = "val" + (t === "進場" ? " in" : t === "出場" ? " out" : "");
}

function paintSummary(res){
  if (window.demoResult) window.demoResult(res);     // 大字報 mirrors the panel (demo.js)
  PANEL = res || null;
  paintDirection();
  renderItems(res);
  const v = $("verdict");
  if (!res){
    $("worker").textContent = "—"; v.textContent = "—"; v.className = "idle";
    $("warn").textContent = "";
    return;
  }
  $("worker").textContent = res.worker_id || "—";
  v.textContent = res.status === "PASS" ? "Pass"
                : res.status === "NO_WORKER" ? "No worker detected" : "Fail";
  v.className = res.status === "PASS" ? "pass" : "fail";
  $("warn").textContent = res.extra_people
    ? `⚠ ${res.extra_people} other person(s) in frame — checked the closest only.` : "";
}

// A check off the live camera never takes the picture over (operator, 2026-09-30 — PASS
// first, then FAIL too): whoever watches the kiosk needs the door, not a frozen frame.
// The verdict still lands in the side panel (every item OK/NG) and the alarm box, the
// judged frame is in See Records, and a result frame still being held gives way to live.
// Only uploaded test images (no live feed to go back to) are shown as a result picture.
// A live verdict stays in the panel as long as the alarm box holds it (the server's
// ALARM_HOLD_S), then the panel goes back to standby — Worker ID, PPE Check and every
// item 「—」 — so the next worker never walks up to the previous one's result.
const RESULT_HOLD_MS = 5000;
let RESULT_TIMER = null;
function showCheck(res){
  if (res && CFG.camera){
    LAST = res;
    paintSummary(res);
    clearTimeout(RESULT_TIMER);
    RESULT_TIMER = setTimeout(() => { if (PANEL === res) paintSummary(null); }, RESULT_HOLD_MS);
    if ($("toLive").classList.contains("on") && !LIVE_PAUSED) startLive();
    return;
  }
  show(res);
}

function show(res, preloaded){
  LAST = res;
  paintSummary(res);

  if (preloaded){
    img = preloaded;
    $("imgHint").style.display = "none";
    showResult();
    cv.classList.add("ready");
    draw(res);
    return;
  }
  if (res.frame_id){
    img = new Image();
    img.onload = () => {
      $("imgHint").style.display = "none";
      showResult();                    // hide live feed, reveal result + "Back to live"
      cv.classList.add("ready");
      draw(res);
    };
    img.src = "/api/frame/" + res.frame_id;
  }
}

// Small filled tag drawn at a box's top-left corner: "<class name> <area px²>". A
// colour-coded left stripe ties it back to the box (green/red verdict, amber primary,
// grey bystander) while the tag body stays dark so the text reads over any background.
// `below=true` anchors the tag under (x,y) instead of over it — used for a PPE item's
// own box so its tag doesn't sit on top of the wearer's Person tag: a hardhat's top
// edge is normally within a few pixels of the person box's top edge, so two "above"
// tags there would stack on the same spot and become unreadable.
function tagBox(text, x, y, color, k, below){
  ctx.font = `${Math.max(11, Math.round(12 * k))}px ui-monospace,Menlo,monospace`;
  const stripe = 3 * k, pad = 4 * k;
  const tw = ctx.measureText(text).width;
  const th = Math.max(15, Math.round(16 * k));
  let ty;
  if (below){
    ty = y + 2 * k;
    if (ty + th > cv.height) ty = y - th - 2 * k;      // flip up if clipped at the bottom
  } else {
    ty = y - th - 2 * k >= 0 ? y - th - 2 * k : y + 2 * k;   // flip down if clipped at the top
  }
  ctx.fillStyle = "rgba(15,15,15,.68)";
  ctx.fillRect(x, ty, stripe + tw + pad * 2, th);
  ctx.fillStyle = color;
  ctx.fillRect(x, ty, stripe, th);
  ctx.fillStyle = "#fff";
  ctx.textBaseline = "middle";
  ctx.fillText(text, x + stripe + pad, ty + th / 2 + 0.5);
}

// Box colour follows the checklist verdict, not the raw class: the hardhat box is
// green when the Hardhat row passed and red when it did not, matching the panel.
function draw(res){
  // Only a result whose picture is ON SCREEN can be redrawn. A live check (showCheck) and
  // the boot baseline keep LAST for the side panel without ever loading its frame, so the
  // threshold / area / class controls that redraw "the current result" would hand
  // drawImage a null image and throw — after their change had already been saved
  // (2026-10-03: "Could not set that threshold: …drawImage…" on the harness slider).
  if (!img || !cv.classList.contains("ready")) return;
  cv.width = res.width; cv.height = res.height;
  ctx.drawImage(img, 0, 0);
  const k = Math.max(1, Math.min(cv.width, cv.height) / 640);
  ctx.lineWidth = Math.max(3, Math.round(3.5 * k));

  // The slider's ceiling is this frame's own pixel area, so "no filtering" always means
  // the true max rather than a guessed constant, and it stays right if the capture
  // resolution ever changes.
  const maxArea = res.width * res.height;
  if (Number($("areaSlider").max) !== maxArea){
    $("areaSlider").max = maxArea;
    $("areaMax").textContent = fmtArea(maxArea);
    if (AREA_MIN > maxArea){ AREA_MIN = maxArea; }
  }
  paintArea();

  const area = b => Math.max(0, b[2] - b[0]) * Math.max(0, b[3] - b[1]);

  if (res.person_box){
    ctx.strokeStyle = COL.person;
    ctx.setLineDash([10*k, 7*k]);
    ctx.strokeRect(res.person_box[0], res.person_box[1],
                   res.person_box[2]-res.person_box[0], res.person_box[3]-res.person_box[1]);
    ctx.setLineDash([]);
    const pd = (res.person_index === undefined || res.person_index === null)
      ? null : (res.detections || [])[res.person_index];
    tagBox(`${pd ? pd.name : "Person"}${pd ? " " + fmtScore(pd.score) : ""} ${fmtArea(area(res.person_box))}`,
           res.person_box[0], res.person_box[1], COL.person, k);
  }

  const items = CFG.cfg.items || [];
  const personClass = CFG.cfg.person_class || "Person";
  const personConf  = CFG.cfg.person_conf ?? 0.4;
  const byLabel = {}, negOf = {};
  items.forEach(it => {
    it.classes.forEach(c => byLabel[c] = it.label);
    (it.negatives || []).forEach(c => negOf[c] = it.label);
  });
  // "Passing the threshold" for a box the checklist didn't judge: a checklist class
  // (positive OR its NO-* veto) uses its own item's Score; the person class uses the
  // worker threshold; a class the checklist knows nothing about (e.g. a model's
  // 'machinery') uses the loosest threshold configured anywhere — the server already
  // floors everything at INFER_FLOOR, so this is the most permissive view that still
  // means "would have counted somewhere".
  const loosest = Math.min(personConf, ...items.map(it => it.conf ?? 0.4));
  const thresholdFor = name => {
    const it = items.find(i => i.classes.includes(name) || (i.negatives || []).includes(name));
    if (it) return it.conf ?? 0.4;
    if (name === personClass) return personConf;
    return loosest;
  };

  // Mirror the server's association rule. Without this, a bystander's hardhat is
  // drawn in green even though it was never counted — which reads, wrongly, as if
  // it satisfied the checklist.
  const inside = (b, p) => {
    if (!p) return false;
    const w = Math.max(0, Math.min(b[2],p[2]) - Math.max(b[0],p[0]));
    const h = Math.max(0, Math.min(b[3],p[3]) - Math.max(b[1],p[1]));
    const a = area(b);
    return a > 0 && (w*h)/a >= (CFG.cfg.containment ?? 0.5);
  };

  // The server hands over the primary person's exact index in res.detections so the
  // bystander loop can skip it. Do NOT go back to matching on person_box coordinates:
  // Python rounds .25 half-to-even and JavaScript rounds half-up, so a box at 196.25
  // reads 196.2 from the server and 196.3 here, never matches, and the judged worker
  // gets painted a second time in bystander grey on top of their own amber box.
  const primaryIdx = (res.person_index === undefined) ? null : res.person_index;

  (res.detections || []).forEach((d, di) => {
    const [x1,y1,x2,y2] = d.box;
    const onWorker = inside(d.box, res.person_box);

    // 1. A checklist positive ON the worker — the judged view. Owns this box whether
    //    or not it clears the Score: below it, it is deliberately not drawn (it did
    //    not count), and "show all" does not override that — the threshold is the
    //    whole point of what "show all predictions" means by "passing".
    const label = byLabel[d.name];
    if (label && onWorker){
      const row = res.items.find(i => i.label === label);
      const it = items.find(i => i.label === label);
      if (row && it && d.score >= (it.conf ?? 0.4)){
        // A switched-off item's box is drawn in bystander grey, not red: red on the
        // picture would read as "this failed the worker", and it didn't.
        ctx.setLineDash([]);
        ctx.strokeStyle = (it.enabled === false || row.enabled === false) ? COL.bystander
                        : row.ok ? COL.ok : COL.ng;
        ctx.strokeRect(x1, y1, x2-x1, y2-y1);
        tagBox(`${d.name} ${fmtScore(d.score)}`, x1, y2, ctx.strokeStyle, k, true);
      }
      return;
    }

    // 2. A NO-* class ON the worker that actually overruled its item (vetoed_by) is
    //    part of the verdict, so it is ALWAYS drawn — the operator has to be able to
    //    see why a row went NG when the positive was right there in green.
    const negLabel = negOf[d.name];
    const negRow = negLabel ? res.items.find(i => i.label === negLabel) : null;
    const fired = !!(negRow && negRow.vetoed_by === d.name && onWorker);
    if (fired){
      ctx.setLineDash([6*k, 5*k]);
      ctx.strokeStyle = COL.ng;
      ctx.strokeRect(x1, y1, x2-x1, y2-y1);
      ctx.setLineDash([]);
      tagBox(`${d.name} ${fmtScore(d.score)} · veto`, x1, y2, COL.ng, k, true);
      return;
    }

    // 3. Every other detected person: drawn so the operator can see who else is at the
    //    gate, filtered by the slider so a busy background doesn't bury the primary box
    //    — unless "show all" is on, which is exactly the "I want to see everyone the
    //    model found" case the slider would otherwise hide.
    if (d.name === personClass){
      if (d.score < personConf) return;
      if (di === primaryIdx) return;
      const a = area(d.box);
      if (!SHOW_ALL && a < AREA_MIN) return;
      ctx.setLineDash([]);
      ctx.strokeStyle = COL.bystander;
      ctx.strokeRect(x1, y1, x2-x1, y2-y1);
      tagBox(`${d.name} ${fmtScore(d.score)} ${fmtArea(a)}`, x1, y1, COL.bystander, k);
      return;
    }

    // 4. Everything the checklist did not judge — PPE on a bystander, a NO-* class
    //    that did not veto, a class the checklist has no row for. Prediction only:
    //    dashed blue, never a verdict colour.
    if (!SHOW_ALL) return;
    if (d.score < thresholdFor(d.name)) return;
    ctx.setLineDash([6*k, 5*k]);
    ctx.strokeStyle = COL.pred;
    ctx.strokeRect(x1, y1, x2-x1, y2-y1);
    ctx.setLineDash([]);
    tagBox(`${d.name} ${fmtScore(d.score)}`, x1, y2, COL.pred, k, true);
  });
}

// ── phase-1 test input ──────────────────────────────────────────────────
async function runFiles(files){
  $("err").textContent = ""; $("busy").classList.add("on");
  try{
    const ids = [];
    for (const f of files){
      const buf = await f.arrayBuffer();
      const r = await fetch("/api/frame", {method:"POST",
        headers:{"Content-Type":"application/octet-stream"}, body: buf});
      const d = await r.json().catch(() => ({error:`HTTP ${r.status}`}));
      if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
      ids.push(d.id);
    }
    $("burstInfo").textContent = `${ids.length} frame(s)`;
    const r2 = await fetch("/api/check", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({frames: ids})});
    const res = await r2.json().catch(() => ({error:`HTTP ${r2.status}`}));
    if (!r2.ok || res.error) throw new Error(res.error || `HTTP ${r2.status}`);
    show(res);
  }catch(e){ $("err").textContent = "Check failed: " + e.message; }
  finally{ $("busy").classList.remove("on"); }
}

// ── folder playback: a directory of images, inferred one by one, shown like video ──
// The folder is picked in the BROWSER (<input webkitdirectory>), never typed as a
// server path: that way there is no "infer any file on the Jetson's disk" endpoint on
// an unauthenticated LAN-facing server, and it works identically from a remote laptop.
// Each frame reuses the existing upload path — /api/frame (detect) then /api/check
// with preview:true (evaluate only: no MP3, no captures.csv, doesn't become the
// kiosk's "last result"). The picture is drawn from the local file the browser already
// has, not fetched back. Sequential by design: the next upload waits for this frame's
// result, so a slow model just plays slower — it never queues (see the live-view
// streaming-delay note for why that matters on this box).
const PB = {active: false, playing: false, files: [], i: 0, timer: null, busy: false,
            prevLast: null, url: null};

const IMG_EXT = /\.(jpe?g|png|bmp|webp)$/i;
function pickFolder(fileList){
  const files = [...fileList]
    .filter(f => IMG_EXT.test(f.name) || (f.type || "").startsWith("image/"))
    .sort((a, b) => (a.webkitRelativePath || a.name)
                    .localeCompare(b.webkitRelativePath || b.name, undefined, {numeric: true}));
  if (!files.length){ $("err").textContent = "No images in that folder."; return; }
  startPlayback(files);
}

function startPlayback(files){
  if (RT_ON) setRealtime(false);
  PB.active = true; PB.files = files; PB.i = 0; PB.prevLast = LAST;
  $("err").textContent = "";
  $("liveToggleBar").classList.add("hidden");
  $("pbBar").classList.remove("hidden");
  clearTimeout(LIVE_TIMER); LIVE_TIMER = null; LIVE_BUSY = false;   // live feed off for the duration
  pbSetPlaying(true);
  pbTick();
}

function stopPlayback(){
  pbSetPlaying(false);
  PB.active = false; PB.files = []; PB.i = 0;
  if (PB.url){ URL.revokeObjectURL(PB.url); PB.url = null; }
  $("pbBar").classList.add("hidden");
  $("liveToggleBar").classList.remove("hidden");
  $("pbName").textContent = ""; $("pbPos").textContent = "";
  // Put LAST (and the side panel) back to the real gate result, so pollLast() sees the
  // same frame_id it did before and doesn't immediately re-show that result over the
  // live feed — and so the kiosk isn't left displaying a folder frame's verdict.
  LAST = PB.prevLast;
  paintSummary(CFG.camera ? null : LAST);   // live gate: standby, not an old verdict
  if (CFG.camera) startLive();
}

function pbSetPlaying(on){
  PB.playing = on;
  $("pbPlay").innerHTML = on ? "&#9208;" : "&#9654;";
  $("pbPlay").title = on ? "Pause (space)" : "Play (space)";
  if (!on){ clearTimeout(PB.timer); PB.timer = null; }
}

async function pbShow(i){
  if (PB.busy || !PB.active) return false;
  if (i < 0 || i >= PB.files.length) return false;
  PB.busy = true;
  const f = PB.files[i];
  PB.i = i;
  $("pbPos").textContent = `${i + 1} / ${PB.files.length}`;
  $("pbName").textContent = f.webkitRelativePath || f.name;
  try{
    const buf = await f.arrayBuffer();
    const r = await fetch("/api/frame", {method:"POST",
      headers:{"Content-Type":"application/octet-stream"}, body: buf});
    const d = await r.json().catch(() => ({error:`HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    const r2 = await fetch("/api/check", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({worker_id: "", frames: [d.id], preview: true})});
    const res = await r2.json().catch(() => ({error:`HTTP ${r2.status}`}));
    if (!r2.ok || res.error) throw new Error(res.error || `HTTP ${r2.status}`);
    if (!PB.active) return false;                 // stopped while this frame was in flight

    // Decode the local file for the canvas; object URLs must be revoked or the tab's
    // memory grows by one image per frame for as long as playback runs.
    const local = new Image();
    const url = URL.createObjectURL(f);
    await new Promise((ok, no) => { local.onload = ok; local.onerror = no; local.src = url; });
    if (PB.url) URL.revokeObjectURL(PB.url);
    PB.url = url;
    show(res, local);
    $("err").textContent = "";
    return true;
  }catch(e){
    $("err").textContent = `Frame ${i + 1} (${f.name}): ${e.message}`;
    return false;                                  // skip it; playback keeps going
  }finally{ PB.busy = false; }
}

async function pbTick(){
  if (!PB.active || !PB.playing) return;
  const t0 = performance.now();
  await pbShow(PB.i);
  if (!PB.active || !PB.playing) return;
  if (PB.i + 1 >= PB.files.length){ pbSetPlaying(false); return; }   // end: stay on last frame
  PB.i += 1;
  const fps = Number($("pbFps").value);
  // Interval measured from the START of this frame, so inference time is absorbed into
  // the period rather than added to it; 0 = no wait beyond what inference itself took.
  const wait = fps > 0 ? Math.max(0, 1000 / fps - (performance.now() - t0)) : 0;
  PB.timer = setTimeout(pbTick, wait);
}

function pbStep(delta){
  if (!PB.active) return;
  pbSetPlaying(false);
  pbShow(Math.min(PB.files.length - 1, Math.max(0, PB.i + delta)));
}

$("playFolder").onclick = () => $("folder").click();
$("folder").onchange = e => { const fl = e.target.files; e.target.value = ""; if (fl.length) pickFolder(fl); };
$("pbPlay").onclick = () => {
  if (PB.playing){ pbSetPlaying(false); return; }
  if (PB.i + 1 >= PB.files.length && !PB.busy) PB.i = 0;       // replay from the top
  pbSetPlaying(true); pbTick();
};
$("pbPrev").onclick = () => pbStep(-1);
$("pbNext").onclick = () => pbStep(+1);
$("pbStop").onclick = stopPlayback;

// ── live capture (Phase 2): burst straight off the camera ────────────────
async function runLive(){
  $("err").textContent = ""; $("busy").classList.add("on");
  $("trigger").disabled = true; $("cap").disabled = true;
  const label = $("trigger").textContent; $("trigger").textContent = "CHECKING…";
  try{
    const r = await fetch("/api/capture", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({})});
    const res = await r.json().catch(() => ({error:`HTTP ${r.status}`}));
    if (!r.ok || res.error) throw new Error(res.error || `HTTP ${r.status}`);
    $("burstInfo").textContent = `${res.frames} live frame(s)`;
    showCheck(res);
  }catch(e){ $("err").textContent = "Live check failed: " + e.message; }
  finally{
    $("busy").classList.remove("on");
    $("trigger").disabled = false; $("cap").disabled = false;
    $("trigger").textContent = label;
  }
}
$("cap").onclick = runLive;
$("trigger").onclick = runLive;
$("toLive").onclick = () => {
  if (PB.active) stopPlayback();          // "back to live" during a folder = stop it
  else if (RT_ON) setRealtime(false);
  else startLive();
};
$("rtBtn").onclick = () => setRealtime(!RT_ON);

// ── model browser ───────────────────────────────────────────────────────
async function openModels(){
  $("modelModal").classList.add("on");
  $("modelErr").textContent = "";
  await renderModels();
}
function closeModels(){ $("modelModal").classList.remove("on"); }

async function renderModels(){
  const el = $("modelList");
  el.innerHTML = "<div class='mrow'>loading&hellip;</div>";
  try{
    const d = await (await fetch("/api/models")).json();
    if (d.error) throw new Error(d.error);
    $("modelDir").textContent = d.dir;
    $("modelName").textContent = d.active;
    el.innerHTML = "";
    if (!d.models.length){
      el.innerHTML = "<div class='mrow'>No .pt / .engine / .onnx files in this directory.</div>";
    }
    d.models.forEach(m => {
      const row = document.createElement("div");
      row.className = "mrow" + (m.active ? " active" : "");
      const classesTxt = m.classes
        ? m.classes.map(c => `<span class="cls">${esc(c)}</span>`).join(" ")
        : `<span style="font-style:italic">unknown &mdash; attach a class-list yaml to preview or check</span>`;
      row.innerHTML = `<span class="fmt">${esc(m.format)}</span>
        <span class="f">${esc(m.file)}</span>
        <span class="meta">${m.size_mb} MB &middot; ${esc(m.modified)}${m.active ? " &middot; in use" : ""}</span>
        <div class="yamlRow">
          <span>${m.yaml ? `yaml: <b>${esc(m.yaml)}</b>` : "no class-list yaml"}</span>
          ${m.yaml_warning ? `<span class="yamlWarn">&#9888; ${esc(m.yaml_warning)}</span>` : ""}
          ${classesTxt}
          ${CFG.can_switch
            ? `<button class="yamlBtn" data-model="${esc(m.file)}">${m.yaml ? "Replace" : "Attach"} class list (.yaml)&hellip;</button>`
            : ""}
        </div>`;
      if (!m.active && CFG.can_switch) row.onclick = () => pickModel(m.file);
      const yb = row.querySelector(".yamlBtn");
      if (yb) yb.onclick = e => { e.stopPropagation(); pickYaml(m.file); };
      el.appendChild(row);
    });
    // Say what actually decides compatibility, since the obvious guess (architecture)
    // is the wrong one and a mismatched class list fails silently on the screen.
    $("modelNote").innerHTML = CFG.can_switch
      ? `Loading takes a second or two while the server is warm; checks are refused until
         it is serving. Any ultralytics detector loads &mdash; v3 to v11, RT-DETR &mdash; but it must provide
         every class this checklist needs: <code>${d.required_classes.join("</code> <code>")}</code>.
         A model missing one is rejected rather than silently marking that item NG.`
      : "Switching is disabled on this server (<code>--lock-model</code>).";
    $("modelPick").style.display = CFG.can_switch ? "" : "none";
  }catch(e){ el.innerHTML = ""; $("modelErr").textContent = "Could not list models: " + e.message; }
}

async function pickModel(file, force){
  if (!force && !confirm(`Switch the gate to ${file}?\n\nThe next check will use it.`)) return;
  $("modelErr").textContent = "";
  $("modelNote").textContent = `Loading ${file}\u2026`;
  document.querySelectorAll(".mrow").forEach(r => r.classList.add("busy"));
  try{
    const r = await fetch("/api/model", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({file, force: !!force})});
    const d = await r.json().catch(() => ({error:`HTTP ${r.status}`}));

    // A 409 here is the class-name mismatch, and it is the ONE refusal the operator can
    // actually resolve \u2014 by switching anyway and then repointing each item with the
    // dropdowns. Without this the two features deadlock: the model can't go live until
    // the checklist matches it, and the checklist can't be remapped until it is live.
    if (r.status === 409 && !force){
      document.querySelectorAll(".mrow").forEach(x => x.classList.remove("busy"));
      $("modelNote").textContent = "";
      const ok = confirm(`${d.error}\n\n`
        + `Switch to ${file} anyway?\n\n`
        + `Every item will read NG until you repoint it with the Detect/Veto dropdowns `
        + `on the main screen \u2014 they will list this model's own class names once it is running.`);
      if (!ok){ $("modelErr").textContent = d.error; return; }
      return pickModel(file, true);
    }
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);

    CFG = await (await fetch("/api/config")).json();
    await renderModels();
    renderItems(PANEL);        // dropdowns now offer the new model's classes
    $("modelNote").innerHTML = `<b>Now running ${d.file}</b> &mdash; loaded and warm in ${d.load_seconds}s.`
      + (d.missing && d.missing.length
          ? `<br><span style="color:#b45309">&#9888; Forced. These checklist classes are not in this
             model: <code>${d.missing.map(esc).join("</code> <code>")}</code> &mdash; repoint those items
             with the Detect/Veto dropdowns, or attach a class-list yaml that renames them.</span>`
          : "")
      + (d.yaml_warning ? `<br><span style="color:#b45309">&#9888; ${esc(d.yaml_warning)}</span>` : "");
  }catch(e){
    document.querySelectorAll(".mrow").forEach(r => r.classList.remove("busy"));
    $("modelErr").textContent = e.message;
  }
}

// Upload a model chosen from this machine. A file input hands the page the file's
// BYTES and never its path — browsers refuse to disclose that — so choosing a model
// necessarily uploads it rather than pointing the server at it. XHR, not fetch,
// because fetch still cannot report upload progress and these are 50-100 MB files.
function uploadModel(f){
  const mb = (f.size / 1048576).toFixed(1);
  if (!confirm(`Upload ${f.name} (${mb} MB) to the gate and switch to it?`)) return;
  $("modelErr").textContent = "";
  document.querySelectorAll(".mrow").forEach(r => r.classList.add("busy"));
  $("modelPick").disabled = true;

  const done = () => {
    document.querySelectorAll(".mrow").forEach(r => r.classList.remove("busy"));
    $("modelPick").disabled = false;
  };

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/model_upload?name=" + encodeURIComponent(f.name));
  xhr.upload.onprogress = ev => {
    if (!ev.lengthComputable) return;
    const pct = Math.round(ev.loaded / ev.total * 100);
    $("modelNote").innerHTML = `Uploading <code>${f.name}</code> &mdash; ${pct}% of ${mb} MB`;
  };
  xhr.onload = async () => {
    let d = {};
    try{ d = JSON.parse(xhr.responseText); }catch(e){}
    done();
    CFG = await (await fetch("/api/config")).json();
    await renderModels();
    if (xhr.status === 200 && d.switched){
      $("modelNote").innerHTML = `<b>Now running ${d.file}</b> &mdash; ${d.size_mb} MB uploaded`
        + (d.replaced ? " (replaced the existing file)" : "")
        + `, live in ${d.load_seconds}s.`;
    } else {
      $("modelErr").textContent = (d.saved ? `Saved ${d.file}, but it is not active:\n` : "")
        + (d.error || `Upload failed — HTTP ${xhr.status}`);
    }
  };
  xhr.onerror = () => { done(); $("modelErr").textContent = "Upload failed — connection lost."; };
  xhr.onabort = () => { done(); $("modelErr").textContent = "Upload cancelled."; };
  xhr.send(f);
}

$("modelPick").onclick = () => $("modelFile").click();
$("modelFile").onchange = e => {
  if (e.target.files.length) uploadModel(e.target.files[0]);
  e.target.value = "";          // so re-picking the same file fires onchange again
};

// ── class-list yaml attachment ──────────────────────────────────────────
// A yaml is small (a few KB) so this is a plain fetch, no upload-progress bar needed
// the way the multi-MB model upload above has one.
let YAML_TARGET = null;         // which model's row triggered the picker

function pickYaml(modelFile){
  YAML_TARGET = modelFile;
  $("yamlFile").click();
}

async function uploadYaml(f){
  $("modelErr").textContent = "";
  document.querySelectorAll(".mrow").forEach(r => r.classList.add("busy"));
  try{
    const r = await fetch("/api/model_yaml_upload?model=" + encodeURIComponent(YAML_TARGET), {
      method: "POST", body: f});
    const d = await r.json().catch(() => ({error:`HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    if (d.applied_live) CFG = await (await fetch("/api/config")).json();
    await renderModels();
    $("modelNote").innerHTML = `<b>${esc(d.yaml)}</b> attached to ${esc(d.model)} `
      + `(${d.classes.length} classes)`
      + (d.applied_live ? " &mdash; applied immediately, it is the active model." : ".")
      + (d.warning ? `<br><span style="color:#b45309">&#9888; ${esc(d.warning)}</span>` : "");
    if (d.applied_live) renderItems(PANEL);
  }catch(e){
    $("modelErr").textContent = "Could not attach that class list: " + e.message;
  }finally{
    document.querySelectorAll(".mrow").forEach(r => r.classList.remove("busy"));
  }
}

$("yamlFile").onchange = e => {
  if (e.target.files.length) uploadYaml(e.target.files[0]);
  e.target.value = "";
};

// ── records viewer ──────────────────────────────────────────────────────
// worker_id can originate off-network (rfid.py's TCP/UDP/serial listeners, or a raw
// POST to /api/capture) and lands here as arbitrary text, so every field is escaped
// before going into innerHTML rather than trusted the way a local config value would be.
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => (
  {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

let RECS = null;      // last /api/records payload, so filtering never re-fetches
let REC_PAGE = 0;
const REC_PER_PAGE = 100;   // rendering thousands of <tr> at once locks up the kiosk

async function openRecords(){
  $("recordsModal").classList.add("on");
  $("recErr").textContent = "";
  $("recSearch").value = "";      // start each open showing everything, not a stale filter
  await loadRecords();
}
function closeRecords(){ $("recordsModal").classList.remove("on"); }

async function loadRecords(){
  $("recBody").innerHTML = "";
  $("recErr").textContent = "";
  REC_PAGE = 0;
  try{
    RECS = await (await fetch("/api/records")).json();
    if (RECS.error) throw new Error(RECS.error);
    $("recCsvPath").textContent = RECS.csv;
    applyRecordFilter();
  }catch(e){ $("recErr").textContent = "Could not load records: " + e.message; }
}

// Filters the already-fetched rows by worker ID — no round trip, so it updates as the
// operator types. Matches anywhere in the ID (not just the start), case-insensitively,
// since a partial card number is the natural thing to search on at a gate.
function applyRecordFilter(){
  if (!RECS) return;
  const q = $("recSearch").value.trim().toLowerCase();
  // Every badge heard counts too: a 「檢測口請淨空」 row has no single worker, but the
  // badges that were there are in rfid_tags.
  const rows = q ? RECS.rows.filter(r => (r.worker_id || "").toLowerCase().includes(q)
                                      || (r.rfid_tags || "").toLowerCase().includes(q))
                 : RECS.rows;

  // Only ever build one page of <tr>. The table is unbounded now that --record-max
  // defaults to no cap, and a few thousand rows of innerHTML is enough to lock up the
  // kiosk browser on this device.
  const pages = Math.max(1, Math.ceil(rows.length / REC_PER_PAGE));
  if (REC_PAGE >= pages) REC_PAGE = pages - 1;
  const start = REC_PAGE * REC_PER_PAGE;
  const slice = rows.slice(start, start + REC_PER_PAGE);
  renderRecords(RECS.items || [], slice, q);

  const total = RECS.count;
  const cap = RECS.record_max ? ` · capped at ${RECS.record_max} (--record-max)` : " · no cap";
  $("recCount").textContent = q
    ? `${rows.length} of ${total} match "${$("recSearch").value.trim()}"`
    : (total ? `${total} record(s)${cap}` : "");

  const showing = rows.length
    ? `${start + 1}–${Math.min(start + REC_PER_PAGE, rows.length)} of ${rows.length}`
    : "0";
  $("recPage").textContent = `  ${showing}  ·  page ${REC_PAGE + 1} / ${pages}  `;
  $("recPager").style.visibility = pages > 1 ? "visible" : "hidden";
  $("recFirst").disabled = $("recPrev").disabled = REC_PAGE === 0;
  $("recNext").disabled = $("recLast").disabled = REC_PAGE >= pages - 1;
}

function recGoto(page){
  REC_PAGE = Math.max(0, page);
  applyRecordFilter();
  document.querySelector(".recWrap").scrollTop = 0;   // new page starts at the top
}

// The announcement a row made, in the words the worker heard. Green only for a pass;
// every other announcement — a failed check or a refusal — is something the worker was
// told to fix, so it reads red. Rows written before the column existed show a dash.
function alarmCell(r){
  if (!r.alarm) return "&mdash;";
  return `<span class="alarmPill ${r.status === "PASS" ? "ok" : "ng"}">${esc(r.alarm)}</span>`;
}

// The badges the reader heard when the row was written, one per line, strongest first:
// "EPC -58 dBm x12", greyed when under the one-person floor at that moment ("(弱)").
function tagsCell(text){
  if (!text) return "&mdash;";
  return `<div class="tagList">${text.split("; ").map(t => {
    const weak = t.endsWith(" (弱)");
    return `<div${weak ? ' class="weak"' : ""}>${esc(t)}</div>`;
  }).join("")}</div>`;
}

// The highest score the model gave the item (and, in red, its NO- class) on the worker
// over the burst — whatever the threshold, so an NG shows how close it came.
function scoreLine(r, l){
  const s = r[l + "_score"], n = r[l + "_neg_score"];
  if (!s && !n) return "";
  return `<span class="score">${s ? esc(Number(s).toFixed(2)) : "–"}`
    + (n ? ` <span class="neg">NO ${esc(Number(n).toFixed(2))}</span>` : "") + "</span>";
}

// The judged person's box: area (what the area threshold compares) and detection score
// (what Score / Tracking compare). Rows from before 2026-10-03 have the area only.
function personCell(r){
  if (!r.person_area && !r.person_score) return "&mdash;";
  return `<span class="mono">${r.person_area ? esc(fmtArea(Number(r.person_area))) : "–"}`
    + `${r.person_score ? " · " + esc(Number(r.person_score).toFixed(2)) : ""}</span>`;
}

function renderRecords(items, rows, filtered){
  // RFID heard sits next to Alarm: for a 「檢測口請淨空」 row it is the answer to "who
  // was there", which is the first thing an operator reviewing that alarm asks.
  $("recHead").innerHTML = "<th>Time</th><th>Worker ID (RFID)</th><th>Status</th><th>Alarm</th>"
    + "<th>RFID heard (peak dBm)</th><th>Direction</th><th>Person (px² · score)</th>"
    + items.map(l => `<th>${esc(l)}</th>`).join("")
    + "<th>Extra people</th><th>Boxes</th><th>Image</th>";

  const body = $("recBody");
  if (!rows.length){
    const msg = filtered ? "No records match that worker ID."
                         : "No captures yet — trigger a check to see one here.";
    body.innerHTML = `<tr><td colspan="${10 + items.length}" id="recEmpty">${esc(msg)}</td></tr>`;
    return;
  }
  const pill = (v, votes) => v
    ? `<span class="pill ${v === "OK" ? "ok" : "ng"}">${esc(v)}</span>`
      + (votes ? `<span class="votes">${esc(votes)}</span>` : "")
    : "&mdash;";

  body.innerHTML = rows.map(r => `<tr>
      <td>${esc(r.timestamp)}</td>
      <td>${r.worker_id ? esc(r.worker_id) : '<span class="tag">unread</span>'}</td>
      <td>${esc(r.status)}</td>
      <td>${alarmCell(r)}</td>
      <td>${tagsCell(r.rfid_tags)}</td>
      <td>${r.direction ? esc(r.direction) : "&mdash;"}</td>
      <td>${personCell(r)}</td>
      ${items.map(l => `<td>${pill(r[l], r[l + "_votes"])}${scoreLine(r, l)}</td>`).join("")}
      <td>${r.extra_people && r.extra_people !== "0" ? esc(r.extra_people) : "&mdash;"}</td>
      <td>${esc(r.boxes)}</td>
      <td>${r.image
        ? `<button class="viewBtn" data-img="${esc(r.image)}">View</button>`
        : "&mdash;"}</td>
    </tr>`).join("");

  body.querySelectorAll(".viewBtn").forEach(b => {
    b.onclick = () => openLightbox(b.dataset.img);
  });
}

function openLightbox(rel){
  $("lightboxImg").src = "/api/record_image?file=" + encodeURIComponent(rel);
  $("lightbox").classList.add("on");
}
function closeLightbox(){ $("lightbox").classList.remove("on"); $("lightboxImg").src = ""; }

// ── RFID reads viewer ───────────────────────────────────────────────────
// One row per tag the reader has heard within its rolling horizon (~30 s), strongest
// first — the same summary the image trigger's one-person rule counts, so "At gate?"
// here is literally whether that row would count. Auto-refreshes once a second while
// open: an operator holding a badge up wants to watch the number move.
const RFID_POLL_MS = 1000;
let RFID_ROWS = null, RFID_TIMER = null;

async function openRfid(){
  $("rfidModal").classList.add("on");
  loadRfidSettings();
  $("rfidErr").textContent = "";
  await loadRfid();
  scheduleRfid();
}
function closeRfid(){
  $("rfidModal").classList.remove("on");
  if (RFID_TIMER){ clearTimeout(RFID_TIMER); RFID_TIMER = null; }
}
function scheduleRfid(){
  if (RFID_TIMER) clearTimeout(RFID_TIMER);
  RFID_TIMER = setTimeout(async () => {
    if (!$("rfidModal").classList.contains("on")) return;
    if ($("rfidAuto").checked) await loadRfid();
    scheduleRfid();
  }, RFID_POLL_MS);
}

async function loadRfid(){
  try{
    const d = await (await fetch("/api/rfid_reads")).json();
    if (d.error) throw new Error(d.error);
    RFID_ROWS = d;
    $("rfidErr").textContent = "";
    renderRfid(d);
  }catch(e){ $("rfidErr").textContent = "Could not load RFID reads: " + e.message; }
}

function renderRfid(d){
  const body = $("rfidBody");
  if (!d.enabled){
    $("rfidSub").textContent = "no RFID reader configured on this server (--rfid-host)";
    $("rfidCount").textContent = "";
    body.innerHTML = `<tr><td colspan="9" id="recEmpty">No reader.</td></tr>`;
    return;
  }
  const thr = Number(d.min_rssi);
  const multiAnt = (d.antennas || []).length > 1;
  $("rfidSub").textContent = `${d.connected ? "reader connected" : "READER NOT CONNECTED"}`
    + (d.antennas ? ` · antenna ${d.antennas.join(" + ")}` : "")
    + ` · last ${d.horizon_s}s · ${d.total_reads.toLocaleString()} reads since start`
    + ` · threshold ${fmtRssi(thr)} dBm (strongest above it = the worker)`
    + ` · ${whitelistSummary(d.whitelist)}`;
  const near = d.rows.filter(r => r.rssi >= thr).length;
  $("rfidCount").textContent = d.rows.length
    ? `${d.rows.length} tag(s) heard · ${near} at gate`
    : (d.connected ? "no tags in range" : "");
  if (!d.rows.length){
    body.innerHTML = `<tr><td colspan="9" id="recEmpty">${d.connected
      ? "No tags heard in the last " + d.horizon_s + " s."
      : "Reader not connected — nothing can be heard."}</td></tr>`;
    return;
  }
  body.innerHTML = d.rows.map(r => `<tr>
      <td class="epc">${esc(r.epc)}</td>
      <td class="num">${fmtRssi(r.rssi)} dBm${multiAnt && r.ants
        ? "<br><small>" + Object.entries(r.ants).map(([a, v]) => `A${esc(a)} ${fmtRssi(v)}`).join(" · ")
          + "</small>" : ""}</td>
      <td class="num">${fmtRssi(r.last_rssi)} dBm</td>
      <td class="num">${esc(r.reads)}</td>
      <td>${esc(r.first_seen)}</td>
      <td>${esc(r.last_seen)}</td>
      <td class="num">${r.age < 1 ? "now" : r.age.toFixed(0) + "s ago"}</td>
      <td>${r.rssi >= thr ? '<span class="pill near">YES</span>' : '<span class="pill far">weak</span>'}</td>
      <td>${r.registered === true ? '<span class="pill near">listed</span>'
          : r.registered === false ? '<span class="pill unreg">ID未登入</span>' : "&mdash;"}</td>
    </tr>`).join("");
}

// One line on the popup: is the whitelist in force, and from which file. The file is
// edited by hand and re-read on change, so this is where a typo shows up first.
function whitelistSummary(w){
  if (!w || !w.exists) return "no whitelist file — every badge allowed";
  if (!w.enabled) return "whitelist switched off — every badge allowed";
  if (w.error) return `WHITELIST UNREADABLE — every badge refused (${w.error})`;
  return `whitelist: ${w.count} ID(s)`;
}

// Transmit power and receive mode PER ANTENNA (2026-10-07), set live on the reader (and
// saved): one row for each antenna in use. The reader is the judge of what it supports:
// a refused value comes back as an error and that antenna keeps its previous settings.
async function loadRfidSettings(){
  const box = $("rfidSets");
  try{
    const d = await (await fetch("/api/rfid_settings")).json();
    if (!d.available){
      box.innerHTML = `<div class="sheetActions rfidSet"><span class="tag">no reader</span></div>`;
      return;
    }
    const opts = d.modes.map(m =>
      `<option value="${m.value}">${esc(m.label)}（可聽到 ${esc(m.sensitivity)}）</option>`).join("");
    box.innerHTML = d.antennas.map(a => `<div class="sheetActions rfidSet">
        <span class="tag rfidAnt">天線 ${a.antenna}</span>
        <label for="rfidPower${a.antenna}">發射功率</label>
        <input type="number" id="rfidPower${a.antenna}" min="${d.power_range[0]}"
               max="${d.power_range[1]}" step="0.5" value="${a.power_dbm}">
        <span>dBm</span>
        <label for="rfidMode${a.antenna}">接收模式</label>
        <select id="rfidMode${a.antenna}">${opts}</select>
        <button id="rfidApply${a.antenna}">套用</button>
        <span class="tag" id="rfidSetState${a.antenna}">目前 ${a.power_dbm} dBm</span>
      </div>`).join("");
    d.antennas.forEach(a => {
      $(`rfidMode${a.antenna}`).value = a.rf_mode;
      $(`rfidApply${a.antenna}`).onclick = () => applyRfidSettings(a.antenna);
    });
  }catch(e){
    box.innerHTML = `<div class="sheetActions rfidSet"><span class="tag">✗ ${esc(e.message)}</span></div>`;
  }
}
async function applyRfidSettings(ant){
  const st = $(`rfidSetState${ant}`), btn = $(`rfidApply${ant}`);
  btn.disabled = true; st.textContent = "套用中…";
  try{
    const r = await fetch("/api/rfid_settings", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({antenna: ant, power_dbm: Number($(`rfidPower${ant}`).value),
                            rf_mode: Number($(`rfidMode${ant}`).value)})});
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    st.textContent = `✓ 已套用 ${d.power_dbm} dBm`;
  }catch(e){ st.textContent = "✗ " + e.message; }
  finally{ btn.disabled = false; loadRfidSettingsQuiet(); }
}
// Put back what the reader is really running on (after a refusal, the old values).
async function loadRfidSettingsQuiet(){
  try{
    const d = await (await fetch("/api/rfid_settings")).json();
    if (!d.available) return;
    d.antennas.forEach(a => {
      const p = $(`rfidPower${a.antenna}`), m = $(`rfidMode${a.antenna}`);
      if (p) p.value = a.power_dbm;
      if (m) m.value = a.rf_mode;
    });
  }catch(e){}
}

$("rfidBtn").onclick = openRfid;
$("rfidClose").onclick = closeRfid;
$("rfidRefresh").onclick = loadRfid;
$("rfidModal").onclick = e => { if (e.target.id === "rfidModal") closeRfid(); };

// ── Speaker test: the PATLITE tower's voice and lamps, on demand ─────────────
// Each button is one request; the server answers with what the tower said back, so a
// refusal (e.g. Chinese on the English/Japanese-only sample unit) shows here verbatim.
async function openTower(){
  $("towerModal").classList.add("on");
  loadTowerVolume();
  $("towerMsg").textContent = ""; $("towerMsg").className = "";
  $("towerSub").textContent = "checking the tower…";
  $("towerText").focus();
  try{
    const d = await (await fetch("/api/tower")).json();
    $("towerSub").textContent = !d.enabled
      ? "no signal tower configured on this server (--tower-host)"
      : d.reachable
        ? `PATLITE tower ${d.host} · connected · firmware ${d.version}`
        : `PATLITE tower ${d.host} · NOT REACHABLE — ${d.error}`;
  }catch(e){ $("towerSub").textContent = "Could not ask the gate: " + e.message; }
}
function closeTower(){ $("towerModal").classList.remove("on"); }

// Speaker volume lives in the tower's own settings (0-15), reached through its web login,
// so both reading and saving take a moment. Saved on release, not while dragging: each
// save is a full login round trip on a unit that allows one web session at a time.
async function loadTowerVolume(){
  const sl = $("towerVol"), st = $("towerVolState");
  sl.disabled = true; st.className = "tag"; st.textContent = "reading…";
  try{
    const r = await fetch("/api/tower_volume");
    const d = await r.json().catch(() => ({error:`HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    sl.max = d.max; sl.value = d.volume; sl.disabled = false;
    $("towerVolVal").textContent = `${d.volume} / ${d.max}`;
    $("towerMute").checked = !!d.mute; $("towerMute").disabled = false;
    showMute(d.mute, "");
  }catch(e){ $("towerVolVal").textContent = "—"; st.textContent = "✗ " + e.message; }
}
// While the tower is muted that is the one thing worth saying — every voice is silent —
// so it stays on screen after a save instead of being replaced by "✓ saved".
function showMute(muted, msg){
  const st = $("towerVolState");
  st.className = "tag" + (muted ? " muted" : "");
  st.textContent = muted ? "MUTED on the tower — no voice will play" + (msg ? " · " + msg : "") : msg;
}
async function saveTowerVolume(body){
  const sl = $("towerVol"), mu = $("towerMute");
  sl.disabled = mu.disabled = true;
  $("towerVolState").className = "tag"; $("towerVolState").textContent = "saving…";
  try{
    const r = await fetch("/api/tower_volume", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    const d = await r.json().catch(() => ({error:`HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    mu.checked = !!d.mute;
    showMute(d.mute, `✓ saved at ${d.volume}` + (d.restart_needed ? " — the tower asks for a restart" : ""));
  }catch(e){ $("towerVolState").textContent = "✗ " + e.message; loadTowerVolume(); return; }
  finally{ sl.disabled = mu.disabled = false; }
}
$("towerVol").oninput = () => { $("towerVolVal").textContent = `${$("towerVol").value} / ${$("towerVol").max}`; };
$("towerVol").onchange = () => saveTowerVolume({volume: Number($("towerVol").value)});
$("towerMute").onchange = () => saveTowerVolume({volume: Number($("towerVol").value),
                                                 mute: $("towerMute").checked});

async function towerSend(body, doing){
  const msg = $("towerMsg"), btns = document.querySelectorAll("#towerModal .sheetActions button");
  msg.className = ""; msg.textContent = doing + "…";
  btns.forEach(b => b.disabled = true);
  try{
    const r = await fetch("/api/tower", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    const d = await r.json().catch(() => ({error:`HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    msg.className = "ok"; msg.textContent = "✓ " + d.message;
  }catch(e){ msg.className = "bad"; msg.textContent = "✗ " + e.message; }
  finally{ btns.forEach(b => b.disabled = false); }
}

$("towerBtn").onclick = openTower;
$("towerClose").onclick = closeTower;
$("towerModal").onclick = e => { if (e.target.id === "towerModal") closeTower(); };
$("towerSpeak").onclick = () => {
  const text = $("towerText").value.trim();
  if (!text){ $("towerMsg").className = "bad"; $("towerMsg").textContent = "✗ Type something to say first."; return; }
  towerSend({action:"speak", text, lang:$("towerLang").value}, "Sending to the speaker");
};
$("towerText").onkeydown = e => { if (e.key === "Enter"){ e.preventDefault(); $("towerSpeak").onclick(); } };
document.querySelectorAll("#towerModal .lampBtn").forEach(b =>
  b.onclick = () => towerSend({action:"flash", lamp:b.dataset.lamp}, `Flashing ${b.textContent}`));
$("towerOff").onclick = () => towerSend({action:"off"}, "Switching everything off");

$("recordsBtn").onclick = openRecords;
$("recordsClose").onclick = closeRecords;
$("recordsRefresh").onclick = loadRecords;
$("recSearch").oninput = () => { REC_PAGE = 0; applyRecordFilter(); };
$("recFirst").onclick = () => recGoto(0);
$("recPrev").onclick  = () => recGoto(REC_PAGE - 1);
$("recNext").onclick  = () => recGoto(REC_PAGE + 1);
$("recLast").onclick  = () => recGoto(1e9);
$("recordsModal").onclick = e => { if (e.target.id === "recordsModal") closeRecords(); };
$("lightbox").onclick = closeLightbox;

$("modelBtn").onclick = openModels;
$("modelClose").onclick = closeModels;
$("modelModal").onclick = e => { if (e.target.id === "modelModal") closeModels(); };

// Stops the gate_server process. There is no in-app undo — systemd (Restart=always)
// is what brings it back, typically within a few seconds; if the service isn't
// installed, the gate stays down until someone runs run_gate_native.sh by hand. That
// asymmetry is why this is the one testbar button with a confirm() in front of it.
$("shutdownBtn").onclick = async () => {
  if (!confirm("Shut down the gate server?\n\nThe camera check stops immediately. " +
               "systemd should restart it within a few seconds; if it doesn't come back, " +
               "someone needs terminal access to the Jetson.")) return;
  $("shutdownBtn").disabled = true;
  $("shutdownBtn").textContent = "Shutting down…";
  try {
    // do_POST requires a non-empty body for every route (Content-Length: 0 is rejected
    // before dispatch), so this sends a trivial one like every other POST in this file.
    await fetch("/api/shutdown", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: "{}"});
  } catch (e) {
    // The process exits before the response finishes sending in the common case —
    // a network error here is the expected outcome, not a failure to report.
  }
};

$("pick").onclick = () => { if (PB.active) stopPlayback(); $("file").click(); };
$("file").onchange = e => {
  if (e.target.files.length) runFiles([...e.target.files]);
  e.target.value = "";
};
document.addEventListener("keydown", e => {
  if (e.key === "Escape"){ closeModels(); closeRecords(); closeLightbox(); closeTower(); }
  // Folder playback transport — only while playing, and never while typing in a box.
  if (PB.active && !(document.activeElement && /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName))){
    if (e.key === " "){ e.preventDefault(); $("pbPlay").onclick(); return; }
    if (e.key === "ArrowLeft"){ e.preventDefault(); pbStep(-1); return; }
    if (e.key === "ArrowRight"){ e.preventDefault(); pbStep(+1); return; }
    if (e.key === "Escape"){ stopPlayback(); return; }
  }
  if (e.key === "h" || e.key === "H"){
    if (document.activeElement && document.activeElement.tagName === "INPUT") return;
    $("testbar").classList.toggle("hidden");
    $("areaBar").classList.toggle("hidden");
    $("rssiBar").classList.toggle("hidden");
    if (LAST) draw(LAST);
  }
});

// ── on-site recording (scripts/recorder.py) ──────────────────────────────────
// The camera stream as it arrives plus a log of what the gate decided every tick, for
// replaying a real day at the gate. The server owns the recording; this only starts,
// stops and shows it — another kiosk sees the same state within a few seconds.
let VID = null, VID_BUSY = false;
const fmtMB = b => b >= 1024 ** 3 ? (b / 1024 ** 3).toFixed(2) + " GB" : Math.round(b / 1024 ** 2) + " MB";
const fmtDur = s => { s = Math.round(s); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const fmtStamp = n => `${n.slice(0, 4)}-${n.slice(4, 6)}-${n.slice(6, 8)} ${n.slice(9, 11)}:${n.slice(11, 13)}:${n.slice(13, 15)}`;
function paintVid(d){
  VID = d;
  const b = $("vidBtn");
  if (!d || !d.available){ b.disabled = true; b.textContent = "● 錄影"; return; }
  b.disabled = VID_BUSY;
  b.classList.toggle("on", !!d.active);
  b.textContent = d.active ? `■ 停止錄影 ${fmtDur(d.elapsed || 0)} · ${fmtMB(d.bytes || 0)}` : "● 錄影";
}
async function pollVid(){
  try{ paintVid(await (await fetch("/api/recording")).json()); }catch(e){}
  setTimeout(pollVid, VID && VID.active ? 1000 : 5000);
}
$("vidBtn").onclick = async () => {
  if (!VID || VID_BUSY) return;
  VID_BUSY = true;
  const b = $("vidBtn");
  b.disabled = true; b.textContent = VID.active ? "停止中…" : "開始中…";
  try{
    const r = await fetch("/api/recording", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({on: !VID.active})});
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    VID_BUSY = false; paintVid(d);
  }catch(e){ $("err").textContent = "Recording: " + e.message; VID_BUSY = false; paintVid(VID); }
};
async function openVid(){
  $("vidModal").classList.add("on");
  $("vidErr").textContent = "";
  try{
    const d = await (await fetch("/api/recordings")).json();
    if (!d.available){ $("vidSub").textContent = "no camera on this server"; $("vidBody").innerHTML = ""; return; }
    $("vidSub").textContent = `${d.dir} · 影片 .mkv 用 VLC 播 · .jsonl 是每 0.1 秒的辨識框與判斷`;
    $("vidBody").innerHTML = d.rows.length ? d.rows.map(r => `<tr>
        <td>${esc(fmtStamp(r.name))}${r.recording ? ' <span class="pill unreg">錄影中</span>' : ""}</td>
        <td class="num">${r.seconds != null ? fmtDur(r.seconds) : "&mdash;"}</td>
        <td class="num">${fmtMB(r.bytes)}</td>
        <td><a href="/recordings/${esc(r.video)}" download>${esc(r.video)}</a></td>
        <td>${r.log ? `<a href="/recordings/${esc(r.log)}" download>${esc(r.log)}</a>` : "&mdash;"}</td>
      </tr>`).join("") : `<tr><td colspan="5" id="recEmpty">還沒有錄影。</td></tr>`;
  }catch(e){ $("vidErr").textContent = "Could not list recordings: " + e.message; }
}
$("vidListBtn").onclick = openVid;
$("vidClose").onclick = () => $("vidModal").classList.remove("on");
pollVid();

boot();
