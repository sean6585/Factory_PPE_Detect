// ── 大字報 (demo) mode ──────────────────────────────────────────────────────
// A full-screen view for visitors: the live camera, each judged item as a big OK / NG,
// the badge (Tag ID) and one banner saying what the gate announced. Loaded after app.js
// and fed by it through three hooks (window.demoResult from paintSummary,
// window.demoAlarm from paintAlarm, window.DEMO_ON read by liveTick) — so it shows
// exactly what the developer page shows, with the same hold times, and adds no logic
// of its own to the gate. Everything on the developer page keeps running underneath.
(function(){
  const D = id => document.getElementById(id);

  // The checklist's labels are English model-side names; visitors read Chinese. An item
  // not listed here shows its own label.
  const ITEM_ZH = {"Hardhat": "安全帽", "Helmet": "安全帽", "Safety Harness": "安全吊帶",
                   "Harness": "安全吊帶", "Bodycam": "密錄器", "Vest": "反光背心", "Mask": "口罩"};
  const WARN = new Set(["crowd", "multi"]);       // yellow on the tower too: a warning
  const QUIET = new Set(["no_worker"]);           // nothing was said; not an alarm
  const STORE_KEY = "ppeDemoMode";

  let result = null;          // the check the developer panel is holding (null = standby)
  let alarm = {key: null, s: null};
  let liveBusy = false, liveTimer = null;
  window.DEMO_ON = false;

  // ── what is judged: the ENABLED checklist items, in checklist order ─────────
  function items(){
    const cfgItems = (typeof CFG !== "undefined" && CFG && CFG.cfg && CFG.cfg.items) || [];
    return cfgItems.filter(i => i.enabled !== false).map(i => i.label);
  }

  let rendered = "";          // the item labels the rows were last built for
  function buildRows(labels){
    const box = D("demoItems");
    box.textContent = "";
    labels.forEach((l, i) => {
      const row = document.createElement("div"); row.className = "dItem";
      const name = document.createElement("span"); name.className = "dName";
      name.textContent = ITEM_ZH[l] || l;
      const badge = document.createElement("span"); badge.className = "dBadge";
      badge.id = "demoBadge" + i; badge.textContent = "—";
      row.append(name, badge); box.append(row);
    });
    const tag = document.createElement("div"); tag.className = "dTag";
    tag.append("Tag ID : ");
    const b = document.createElement("b"); b.id = "demoTag"; b.textContent = "—";
    tag.append(b); box.append(tag);
    rendered = JSON.stringify(labels);
  }

  function paintItems(){
    const labels = items();
    if (JSON.stringify(labels) !== rendered) buildRows(labels);   // config arrived / changed
    labels.forEach((l, i) => {
      const r = result && result.status !== "NO_WORKER" && result.items
        ? result.items.find(x => x.label === l) : null;
      const b = D("demoBadge" + i);
      b.className = "dBadge" + (r ? (r.ok ? " ok" : " ng") : "");
      b.textContent = r ? (r.ok ? "OK" : "NG") : "—";
    });
    D("demoTag").textContent = tagText();
  }

  // The badge of the check on screen; failing that, the badge(s) the alarm is about
  // (an unregistered badge, two badges at once).
  function tagText(){
    if (result && result.worker_id) return result.worker_id;
    const ev = alarm.s && alarm.s.alarm_active ? alarm.s.last_event : null;
    if (alarm.key && ev && ev.epcs && ev.epcs.length) return ev.epcs.join(" / ");
    return "—";
  }

  function paintBanner(){
    const el = D("demoBanner"), s = alarm.s || {};
    // No announcement running but a check held on screen (e.g. Check Now, which raises
    // no trigger alarm): the banner states its verdict, as the badges above it do.
    const key = alarm.key || (result ? {PASS: "pass", FAIL: "fail"}[result.status] || null : null);
    const t = key && typeof ALARM_TEXT !== "undefined" && ALARM_TEXT ? ALARM_TEXT[key] : null;
    let cls = "", main = "待命中", sub = "";
    if (t && !QUIET.has(key)){
      // 「檢測通過」 plus 請進場 / 請出場 when IT (or the track) said which way.
      main = t[0];
      if ((key === "pass_entry" || key === "pass_leave") && t[1]) sub = t[1];
      cls = key.startsWith("pass") ? "ok" : WARN.has(key) ? "warn" : "ng";
    } else if (s.light && s.light.light === "red"){
      main = "暫停開放"; sub = s.light.reason || ""; cls = "closed";
    } else if (s.enabled && s.watching && s.dwell > 0){
      main = "檢測中…"; cls = "busy";
    }
    el.className = "dBanner" + (cls ? " " + cls : "");
    el.textContent = main;
    if (sub){ const small = document.createElement("small"); small.textContent = sub; el.append(small); }
  }

  // ── hooks the developer page calls ──────────────────────────────────────────
  window.demoResult = res => {
    result = res || null;
    if (window.DEMO_ON){ paintItems(); paintBanner(); }
  };
  window.demoAlarm = (key, s) => {
    alarm = {key: key || null, s: s || null};
    if (window.DEMO_ON){ paintBanner(); paintItems(); paintBoxes(); }
  };

  // The trigger's boxes over the picture — person (ID, 進場/出場, score, area) and the
  // checklist's items — drawn by the developer view's own drawBoxes / drawPpe (app.js),
  // so both screens show the same boxes. Redrawn on every status poll (~150 ms).
  function paintBoxes(){
    const cv = D("demoBoxes"), cam = D("demoCam");
    const W = cam.clientWidth, H = cam.clientHeight;
    if (!W || !H) return;
    if (cv.width !== W || cv.height !== H){ cv.width = W; cv.height = H; }
    const g = cv.getContext("2d");
    g.clearRect(0, 0, W, H);
    if (D("demoImg").hidden || !alarm.s) return;
    drawZone(g, alarm.s, W, H);
    if (typeof drawBoxes === "function") drawBoxes(g, alarm.s, W, H);
    if (typeof drawPpe === "function") drawPpe(g, alarm.s, W, H);
  }

  // The door zone (ROI) as the developer view shows it: shade outside the two edges,
  // dashed orange edges with a marker on top, and 「門 · 管制區」 on the door's side.
  // From the same status (s.zone = gate.json trigger_zone, s.door_side), so moving the
  // edges on the developer page moves them here on the next poll.
  function drawZone(g, s, W, H){
    const z = s.zone;
    if (!Array.isArray(z) || z.length !== 2) return;
    const [l, r] = [Number(z[0]), Number(z[1])];
    if (l <= 0 && r >= 1) return;                     // whole frame = no zone to show
    const k = Math.max(1, Math.min(W, H) / 640);
    g.save();
    g.fillStyle = "rgba(15,15,15,.28)";
    g.fillRect(0, 0, l * W, H);
    g.fillRect(r * W, 0, W - r * W, H);
    g.strokeStyle = "#f59e0b"; g.fillStyle = "#f59e0b";
    g.lineWidth = Math.max(2, 3 * k);
    g.setLineDash([8 * k, 6 * k]);
    for (const x of [l * W, r * W]){
      g.beginPath(); g.moveTo(x, 0); g.lineTo(x, H); g.stroke();
      g.fillRect(x - 6 * k, 0, 12 * k, 12 * k);
    }
    g.setLineDash([]);
    const left = s.door_side !== "right";
    const text = left ? "◀ 門 · 管制區" : "門 · 管制區 ▶";
    g.font = `700 ${Math.round(14 * k)}px "Noto Sans TC","Noto Sans CJK TC",sans-serif`;
    const tw = g.measureText(text).width + 16 * k, th = 24 * k, pad = 8 * k;
    const x = left ? pad : W - pad - tw;
    g.fillStyle = "rgba(185,28,28,.85)";
    g.fillRect(x, pad, tw, th);
    g.fillStyle = "#fff"; g.textBaseline = "middle";
    g.fillText(text, x + 8 * k, pad + th / 2);
    g.restore();
  }

  // ── its own live picture, same endpoint and pacing as the developer view ─────
  // (whose own poll stands down while this one runs — see liveTick in app.js).
  function liveTick(){
    if (!window.DEMO_ON){ liveTimer = null; return; }
    if (!liveBusy){
      liveBusy = true;
      const next = new Image();
      next.onload = () => {
        liveBusy = false;
        if (!window.DEMO_ON) return;
        // The box takes the camera's own shape, so the frame fills it with nothing cut off
        // and box coordinates (fractions of the frame) map straight onto it.
        const box = D("demo");
        if (next.naturalWidth && box.style.getPropertyValue("--ar") === ""){
          box.style.setProperty("--ar", `${next.naturalWidth} / ${next.naturalHeight}`);
          box.style.setProperty("--arn", String(next.naturalWidth / next.naturalHeight));
        }
        D("demoImg").src = next.src; D("demoImg").hidden = false;
        D("demoNoCam").hidden = true;
      };
      next.onerror = () => { liveBusy = false; };
      next.src = "/api/live_frame?t=" + Date.now();
    }
    liveTimer = setTimeout(liveTick, 250);
  }

  // ── open / close ────────────────────────────────────────────────────────────
  function remember(on){ try{ localStorage.setItem(STORE_KEY, on ? "1" : "0"); }catch(e){} }

  function paintFsButton(){ D("demoFs").hidden = !!document.fullscreenElement; }

  function open(fullscreen){
    window.DEMO_ON = true;
    remember(true);
    D("demo").classList.add("on");
    paintItems(); paintBanner();
    if (!liveTimer) liveTick();
    if (fullscreen && document.documentElement.requestFullscreen){
      document.documentElement.requestFullscreen().catch(() => {}).finally(paintFsButton);
    }
    paintFsButton();
  }
  function close(){
    window.DEMO_ON = false;
    remember(false);
    D("demo").classList.remove("on");
    clearTimeout(liveTimer); liveTimer = null; liveBusy = false;
    if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(() => {});
  }

  D("demoBtn").onclick = () => open(true);
  D("demoExit").onclick = close;
  D("demoFs").onclick = () => {
    document.documentElement.requestFullscreen?.().catch(() => {}).finally(paintFsButton);
  };
  document.addEventListener("fullscreenchange", paintFsButton);

  // A kiosk that was left in demo mode comes back in it after a reload. Full screen
  // needs a click (browsers refuse it without one), hence the 全螢幕 button.
  let saved = null;
  try{ saved = localStorage.getItem(STORE_KEY); }catch(e){}
  if (saved === "1") open(false);
})();
