#!/usr/bin/env python3
"""A stand-in for the site IT service, to see exactly what the gate uploads.

    python3 scripts/it_mock.py            # http://0.0.0.0:8900/  (the page)

Same two paths as the real service (config/it.json):
    POST /access-control-dashboard/ppe-result   multipart: fields + photo (or JSON)
    POST /ppe/device-heartbeat                  JSON
and the real service's reply, as captured from it on 2026-09-30:
    201  {"id": "<uuid v7>", "access_type": "leave" | "entry", "message": "success"}

The real service decides access_type itself (the gate sends no in/out field). Here it
alternates per badge by default — a badge's first result is "entry", the next "leave" —
or is fixed, or the service can be made to fail (500, 400, a hang past the gate's 10 s
timeout) to watch the gate's retry and reject paths. The mode is set from the page.

Everything received is kept under it_mock_data/ next to the scripts folder (packets.jsonl,
the photos), so a restart does not lose it. Test data only: it holds worker photos,
the same as See Records — do not copy it off the box.
"""

import argparse
import email
import email.policy
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PKG, "it_mock_data")
RESULT_PATH = "/access-control-dashboard/ppe-result"
HEARTBEAT_PATH = "/ppe/device-heartbeat"
MODES = ("toggle", "entry", "leave", "http500", "http400", "timeout")

_lock = threading.Lock()
_packets = []           # newest last; each has a monotonically increasing "seq"
_state = {"mode": "toggle", "last_type": {}}


def uuid7() -> str:
    """Time-ordered UUID, the kind the real service returns (01a0eb35-d4c8-7…)."""
    ms = int(time.time() * 1000)
    rnd = int.from_bytes(os.urandom(10), "big")
    v = (ms << 80) | (0x7 << 76) | (((rnd >> 62) & 0xFFF) << 64) | (0b10 << 62) | (rnd & ((1 << 62) - 1))
    return str(uuid.UUID(int=v))


def _load() -> None:
    os.makedirs(os.path.join(DATA, "photos"), exist_ok=True)
    try:
        with open(os.path.join(DATA, "state.json"), encoding="utf-8") as fh:
            _state.update(json.load(fh))
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(DATA, "packets.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                try:
                    _packets.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass


def _save_state() -> None:
    tmp = os.path.join(DATA, "state.json.part")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_state, fh, ensure_ascii=False)
    os.replace(tmp, os.path.join(DATA, "state.json"))


def _record(p: dict) -> dict:
    with _lock:
        p["seq"] = (_packets[-1]["seq"] + 1) if _packets else 1
        _packets.append(p)
        with open(os.path.join(DATA, "packets.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    return p


def _parse_multipart(ctype: str, body: bytes):
    """Fields and files of a multipart/form-data body, via the stdlib email parser."""
    msg = email.message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body,
                                   policy=email.policy.HTTP)
    fields, files = {}, []
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        if part.get_filename():
            files.append({"field": name, "filename": part.get_filename(),
                          "type": part.get_content_type(), "data": data})
        else:
            fields[name] = data.decode("utf-8", errors="replace")
    return fields, files


class Handler(BaseHTTPRequestHandler):
    server_version = "it-mock/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/json")) else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ── what the gate calls ────────────────────────────────────────────────
    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if u.path in ("/api/mode", "/api/clear"):
            return self._control(u.path, body)
        ctype = self.headers.get("Content-Type", "")
        kind = {RESULT_PATH: "ppe-result", HEARTBEAT_PATH: "heartbeat"}.get(u.path, "unknown")
        fields, photos, parse_error = {}, [], None
        try:
            if ctype.startswith("multipart/form-data"):
                fields, files = _parse_multipart(ctype, body)
                for f in files:
                    name = f"{uuid.uuid4().hex[:12]}.jpg"
                    with open(os.path.join(DATA, "photos", name), "wb") as fh:
                        fh.write(f["data"])
                    photos.append({"field": f["field"], "filename": f["filename"],
                                   "type": f["type"], "bytes": len(f["data"]), "saved": name})
            elif body:
                fields = json.loads(body)
        except Exception as e:
            parse_error = f"{type(e).__name__}: {e}"

        mode = _state["mode"]
        if mode == "timeout":
            time.sleep(15)                          # past the gate's 10 s timeout
        if mode == "http500":
            code, reply = 500, {"message": "mock: internal error (test mode)"}
        elif mode == "http400":
            code, reply = 400, {"message": "mock: bad request (test mode)"}
        elif kind == "ppe-result":
            rfid = str(fields.get("rfid", "")) if isinstance(fields, dict) else ""
            if mode in ("entry", "leave"):
                access = mode
            else:
                # Only a Pass moves a badge in or out: a Fail (still outside, fixing a
                # strap) or an intrusion record answers the direction the badge WOULD go,
                # without flipping it — otherwise a failed check would turn the next pass
                # around.
                with _lock:
                    access = "leave" if _state["last_type"].get(rfid) == "entry" else "entry"
                    if fields.get("ppeResult") == "Pass":
                        _state["last_type"][rfid] = access
                        _save_state()
            code, reply = 201, {"id": uuid7(), "access_type": access, "message": "success"}
        elif kind == "heartbeat":
            code, reply = 201, {"message": "success"}
        else:
            code, reply = 404, {"message": f"mock: no such path {u.path}"}

        _record({"received": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "path": u.path,
                 "from": self.client_address[0], "content_type": ctype.split(";")[0],
                 "bytes": len(body), "headers": {k: v for k, v in self.headers.items()},
                 "fields": fields, "photos": photos, "parse_error": parse_error,
                 "mode": mode, "status": code, "reply": reply})
        print(f"[it-mock] {kind:10} from {self.client_address[0]} -> {code} "
              f"{json.dumps(reply, ensure_ascii=False)}", flush=True)
        if mode != "timeout":
            self._send(code, reply)

    def _control(self, path, body):
        try:
            req = json.loads(body or b"{}")
        except ValueError:
            return self._send(400, {"error": "Body must be JSON"})
        if path == "/api/mode":
            if req.get("mode") not in MODES:
                return self._send(400, {"error": f"mode must be one of {', '.join(MODES)}"})
            with _lock:
                _state["mode"] = req["mode"]
                _save_state()
            print(f"[it-mock] mode -> {req['mode']}", flush=True)
            return self._send(200, {"mode": _state["mode"]})
        with _lock:                                   # /api/clear
            _packets.clear()
            _state["last_type"] = {}
            _save_state()
            open(os.path.join(DATA, "packets.jsonl"), "w").close()
            for f in os.listdir(os.path.join(DATA, "photos")):
                os.remove(os.path.join(DATA, "photos", f))
        print("[it-mock] cleared", flush=True)
        return self._send(200, {"cleared": True})

    # ── the page ───────────────────────────────────────────────────────────
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, PAGE, "text/html")
        if u.path == "/api/packets":
            after = int((parse_qs(u.query).get("after") or ["0"])[0] or 0)
            with _lock:
                new = [p for p in _packets if p["seq"] > after][-300:]
                counts = {"ppe-result": sum(p["kind"] == "ppe-result" for p in _packets),
                          "heartbeat": sum(p["kind"] == "heartbeat" for p in _packets)}
            return self._send(200, {"packets": new, "counts": counts, "mode": _state["mode"],
                                    "modes": MODES})
        if u.path.startswith("/photo/"):
            name = os.path.basename(u.path)
            path = os.path.join(DATA, "photos", name)
            if not os.path.isfile(path):
                return self._send(404, {"error": "no such photo"})
            with open(path, "rb") as fh:
                return self._send(200, fh.read(), "image/jpeg")
        return self._send(404, {"error": "not found"})


PAGE = r"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IT 模擬接收器</title>
<style>
  :root{ --bg:#f3f5f7; --panel:#fff; --line:#d3d9e0; --ink:#1d242b; --muted:#66717d;
         --ok:#1b7f35; --okf:#e5f5ea; --ng:#c0322b; --ngf:#fbe9e7; --acc:#24578e; --accf:#e8f0f9;
         --warn:#9a6200; --warnf:#fcf1dc; }
  @media (prefers-color-scheme: dark){ :root{ --bg:#121619; --panel:#1b2127; --line:#333d47;
         --ink:#e3e8ec; --muted:#98a3ae; --ok:#5cc985; --okf:#13271b; --ng:#f2887c; --ngf:#2b1917;
         --acc:#8ab8e8; --accf:#15233a; --warn:#ecaa4f; --warnf:#2c2214; color-scheme:dark } }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.5 "Noto Sans TC",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
  .wrap{max-width:1100px;margin:0 auto;padding:20px 16px 48px;display:flex;flex-direction:column;gap:16px}
  h1{font-size:22px;margin:0}
  .sub{color:var(--muted);font-size:13.5px}
  code,.mono{font-family:ui-monospace,Menlo,"Noto Sans Mono CJK TC",monospace;font-size:12.5px}
  .bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;background:var(--panel);
       border:1px solid var(--line);border-radius:10px;padding:12px 14px}
  .bar label{display:inline-flex;gap:6px;align-items:center;font-size:14px}
  select,button{font:inherit;font-size:14px;border:1px solid var(--line);border-radius:7px;
       background:var(--panel);color:var(--ink);padding:6px 10px}
  button{cursor:pointer}
  button.danger{border-color:var(--ng);color:var(--ng)}
  .count{font-variant-numeric:tabular-nums;background:var(--accf);color:var(--acc);
         border-radius:6px;padding:2px 9px;font-size:13px}
  .urls{display:grid;gap:4px}
  .pkt{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--acc);
       border-radius:9px;padding:12px 14px;display:grid;gap:10px}
  .pkt.hb{border-left-color:var(--line);padding:8px 14px}
  .pkt.bad{border-left-color:var(--ng)}
  .head{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:13.5px}
  .pill{border-radius:5px;padding:1px 8px;font-weight:700;font-size:12.5px}
  .pill.res{background:var(--accf);color:var(--acc)} .pill.hb{background:var(--bg);color:var(--muted)}
  .pill.s2{background:var(--okf);color:var(--ok)} .pill.s4,.pill.s5{background:var(--ngf);color:var(--ng)}
  .pill.entry{background:var(--okf);color:var(--ok)} .pill.leave{background:var(--warnf);color:var(--warn)}
  .body{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:14px;align-items:start}
  @media (max-width:640px){ .body{grid-template-columns:minmax(0,1fr)} }
  table{border-collapse:collapse;font-size:13.5px;width:100%}
  td{padding:3px 10px 3px 0;vertical-align:top;border-bottom:1px dotted var(--line)}
  td.k{color:var(--muted);white-space:nowrap;width:1%}
  .Pass{color:var(--ok);font-weight:700} .Fail{color:var(--ng);font-weight:700}
  .photo img{max-width:260px;width:100%;border-radius:6px;border:1px solid var(--line);cursor:zoom-in}
  .reply{background:var(--bg);border-radius:6px;padding:6px 9px;overflow-x:auto;white-space:pre}
  details summary{cursor:pointer;color:var(--muted);font-size:13px}
  .empty{color:var(--muted);text-align:center;padding:40px 0}
  #lb{position:fixed;inset:0;background:rgba(0,0,0,.8);display:none;align-items:center;justify-content:center;padding:20px}
  #lb.on{display:flex} #lb img{max-width:100%;max-height:100%}
</style></head><body>
<div class="wrap">
  <div>
    <h1>IT 模擬接收器</h1>
    <div class="sub">收到閘門上拋的封包就列在這裡（最新在上），回覆格式與真正的 IT 相同：201 + id / access_type / message。</div>
  </div>
  <div class="bar urls">
    <div class="sub">閘門要打的網址（<code>config/it.json</code> 的 mock target）：</div>
    <div class="mono" id="u1"></div><div class="mono" id="u2"></div>
  </div>
  <div class="bar">
    <span class="count" id="cRes">ppe-result 0</span>
    <span class="count" id="cHb">heartbeat 0</span>
    <label>回覆模式
      <select id="mode">
        <option value="toggle">access_type 依卡號輪流（entry → leave）</option>
        <option value="entry">access_type 固定 entry</option>
        <option value="leave">access_type 固定 leave</option>
        <option value="http500">回 500（測試閘門重送）</option>
        <option value="http400">回 400（測試閘門另存拒收）</option>
        <option value="timeout">不回應 15 s（測試逾時）</option>
      </select></label>
    <label><input type="checkbox" id="hideHb" checked> 隱藏心跳</label>
    <span style="flex:1"></span>
    <button class="danger" id="clear">清除全部紀錄</button>
  </div>
  <div id="list"><div class="empty">還沒收到任何封包。</div></div>
</div>
<div id="lb"><img id="lbImg" alt="photo"></div>
<script>
const $ = id => document.getElementById(id);
let SEQ = 0, ALL = [];
const origin = location.protocol + "//" + location.hostname + ":" + location.port;
$("u1").textContent = "POST " + origin.replace(location.hostname, "it-mock.test") + "/access-control-dashboard/ppe-result";
$("u2").textContent = "POST " + origin.replace(location.hostname, "it-mock.test") + "/ppe/device-heartbeat";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function card(p){
  const hb = p.kind === "heartbeat";
  const s = String(p.status)[0];
  const bad = p.status >= 400 || p.parse_error;
  const at = p.reply && p.reply.access_type;
  let head = `<div class="head"><span class="pill ${hb ? "hb" : "res"}">${esc(p.kind)}</span>`
    + `<span class="pill s${s}">HTTP ${p.status}</span>`
    + (at ? `<span class="pill ${at}">access_type: ${at}</span>` : "")
    + `<span>收到 ${esc(p.received)}</span><span class="sub">來自 ${esc(p.from)} · ${esc(p.content_type || "—")} · ${p.bytes} B</span></div>`;
  const f = p.fields || {};
  const rows = Object.keys(f).map(k => `<tr><td class="k">${esc(k)}</td><td class="${k === "ppeResult" || /helmet|harness|bodyCam/.test(k) ? esc(f[k]) : ""}">${esc(f[k]) || '<span class="sub">（空白）</span>'}</td></tr>`).join("");
  const photos = (p.photos || []).map(ph => `<div class="photo"><img src="/photo/${esc(ph.saved)}" alt="photo" data-full="/photo/${esc(ph.saved)}"><div class="sub">${esc(ph.field)} · ${esc(ph.filename)} · ${ph.bytes} B</div></div>`).join("");
  const noPhoto = !hb && !(p.photos || []).length ? '<div class="sub">（這個封包沒有照片）</div>' : "";
  return `<div class="pkt ${hb ? "hb" : ""} ${bad ? "bad" : ""}" data-kind="${esc(p.kind)}">${head}`
    + `<div class="body"><div><table>${rows}</table>`
    + (p.parse_error ? `<div class="Fail">解析失敗：${esc(p.parse_error)}</div>` : "")
    + `<div class="sub" style="margin-top:6px">回覆（模式 ${esc(p.mode)}）</div><div class="reply mono">${esc(JSON.stringify(p.reply))}</div>`
    + `<details><summary>請求標頭</summary><div class="reply mono">${esc(Object.entries(p.headers || {}).map(([k, v]) => k + ": " + v).join("\n"))}</div></details>`
    + `</div>${photos || noPhoto}</div></div>`;
}
function render(){
  const hide = $("hideHb").checked;
  const shown = ALL.filter(p => !(hide && p.kind === "heartbeat"));
  $("list").innerHTML = shown.length ? shown.slice().reverse().map(card).join("")
    : `<div class="empty">還沒收到${hide ? "（心跳以外的）" : ""}封包。</div>`;
}
async function poll(){
  try{
    const d = await (await fetch("/api/packets?after=" + SEQ)).json();
    if (d.packets.length){ ALL = ALL.concat(d.packets).slice(-300); SEQ = ALL[ALL.length - 1].seq; render(); }
    $("cRes").textContent = "ppe-result " + d.counts["ppe-result"];
    $("cHb").textContent = "heartbeat " + d.counts.heartbeat;
    if (document.activeElement !== $("mode")) $("mode").value = d.mode;
  }catch(e){}
  setTimeout(poll, 2000);
}
$("hideHb").onchange = render;
$("mode").onchange = async () => {
  await fetch("/api/mode", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({mode: $("mode").value})});
};
$("clear").onclick = async () => {
  if (!$("clear").dataset.armed){ $("clear").dataset.armed = "1"; $("clear").textContent = "再按一次確認清除";
    setTimeout(() => { delete $("clear").dataset.armed; $("clear").textContent = "清除全部紀錄"; }, 4000); return; }
  await fetch("/api/clear", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
  ALL = []; SEQ = 0; render(); delete $("clear").dataset.armed; $("clear").textContent = "清除全部紀錄";
};
$("list").onclick = e => { const im = e.target.closest("img[data-full]"); if (im){ $("lbImg").src = im.dataset.full; $("lb").classList.add("on"); } };
$("lb").onclick = () => $("lb").classList.remove("on");
poll();
</script>
</body></html>
"""


def main():
    global DATA
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--data-dir", default=DATA,
                    help="where packets and photos are kept (default: it_mock_data/)")
    args = ap.parse_args()
    DATA = os.path.abspath(args.data_dir)
    _load()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[it-mock] listening on http://{args.host}:{args.port}/  "
          f"({len(_packets)} packet(s) kept, mode {_state['mode']})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
