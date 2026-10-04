"""
IT reporting — what the gate tells the site access-control service.

Two channels, both HTTP POST, formats taken from the operator's test tool (curl_test.txt):

  heartbeat   JSON every heartbeat_interval_s:
                {time, clientId, fabArea, system_status, device_status}
              device_status: normal | cctv_dead | speaker_dead | cctv_speaker_dead;
              system_status is "abnormal" whenever device_status is not "normal".
  ppe-result  multipart form:
                time, rfid, ppeResult, helmet, harness, bodyCam, fabArea  + photo (JPEG)

Both use the tool's exact request shape — requests.post(..., verify=False, timeout=...) —
so what works from the tool works from the gate. Times are UTC "%Y-%m-%d %H:%M:%S", the
tool's format, NOT the gate's local clock (captures.csv / events.jsonl are local).

What becomes a ppe-result POST:
  * every PASS / FAIL check in a visit, oldest first — a FAIL later fixed still reaches IT
    — except a PASS whose worker then turned back or stood on (did not go through);
  * a worker who went INTO the plant without a PASS (未檢查即進入, 被拒絕仍進入,
    未通過仍進入). The form has no violation field, so it goes as ppeResult=Fail with the
    item fields empty and the photo of the moment they walked in — the operator's call.
    After a FAIL that makes two posts: the check (its items, its photo) and the intrusion.
  * a badge that is not on the whitelist (「ID未登入」), once per badge per visit, as
    ppeResult=Fail with the item fields empty, that badge's ID, and the refusal's photo.
    If the same worker then walks in anyway, that is 被拒絕仍進入 and sent as well.
Exits without a check are not sent (only entering was asked for); they stay in
events.jsonl and See Records.

Results are not sent at the verdict. They are read from events.jsonl, where a visit is
written only once it has resolved — the worker went through, turned back, or stood — so
nothing leaves the gate before it is known what actually happened.

Durable, at-least-once: the sender records how far into events.jsonl it has got
(events.sent, a byte offset) and only moves past a line once the server has accepted
every POST in it. A dead network means lines wait, not that they are lost; a restart
resumes where it stopped. A crash between a successful POST and saving the offset can
resend that one line — acceptable for a dashboard, far better than losing it. A line
the server rejects outright (HTTP 4xx) is set aside in events.rejected.jsonl so one bad
record cannot block everything behind it.

Switching reporting ON from the kiosk starts from that moment: whatever happened while
it was off is not replayed. Restarting the service with it already on is different —
anything still waiting is delivered.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

UTC_FMT = "%Y-%m-%d %H:%M:%S"
RETRY_MIN_S, RETRY_MAX_S = 5.0, 120.0     # backoff while the server is unreachable
DRAIN_POLL_S = 2                          # how soon a newly resolved visit is picked up


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(UTC_FMT)


def local_to_utc(ts: str) -> str:
    """The gate's records are local time; IT wants UTC. A naive datetime's astimezone()
    takes it as local — exactly what these timestamps are."""
    return datetime.strptime(ts, UTC_FMT).astimezone().astimezone(timezone.utc).strftime(UTC_FMT)


def _apply_target(cfg: dict, name: str) -> None:
    """Point result_url / heartbeat_url at one of cfg["targets"] (e.g. the site's
    "tsmc-test", or "mock" — scripts/it_mock.py on this box)."""
    targets = cfg.get("targets") or {}
    if name not in targets:
        raise ValueError(f"no IT target {name!r} (have: {', '.join(targets) or 'none'})")
    cfg["target"] = name
    cfg["result_url"] = targets[name]["result_url"]
    cfg["heartbeat_url"] = targets[name]["heartbeat_url"]


def load_config(path: str) -> dict | None:
    """config/it.json, or None when it is absent (IT reporting simply stays off).

    Either top-level result_url / heartbeat_url, or named "targets" with "target" saying
    which one is live — so the bench can report to the mock and the site to the real
    service without anyone editing URLs by hand (the kiosk switches it)."""
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if cfg.get("targets"):
        _apply_target(cfg, cfg.get("target") or next(iter(cfg["targets"])))
    for key in ("result_url", "heartbeat_url", "client_id", "fab_area", "item_fields"):
        if key not in cfg:
            raise ValueError(f"{path}: missing '{key}'")
    cfg.setdefault("enabled", False)
    cfg.setdefault("heartbeat_interval_s", 30)
    cfg.setdefault("verify_tls", False)       # the tool posts with verify=False
    cfg.setdefault("timeout_s", 10)
    return cfg


def save_enabled(path: str, enabled: bool) -> None:
    """Persist the kiosk's on/off switch into config/it.json, leaving every other key as
    it was, so the choice survives a restart."""
    _save_key(path, "enabled", bool(enabled))


def save_target(path: str, name: str) -> None:
    """Persist the kiosk's target choice the same way."""
    _save_key(path, "target", name)


def _save_key(path: str, key: str, value) -> None:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg[key] = value
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def _is_ppe(c: dict) -> bool:
    return c.get("status") in ("PASS", "FAIL")


# The one refusal that IS reported: a badge the whitelist does not know (gate_server's
# _refuse_unregistered). Crowd / two badges / no badge stay warnings only.
UNREGISTERED = "REFUSED:unregistered"


def check_fields(c: dict, cfg: dict) -> dict:
    """The ppe-result form for one PASS/FAIL check: {ts, status, epc, items{label: ok}}.
    An item the checklist does not judge is sent empty — as the tool sends bodyCam."""
    items = c.get("items") or {}
    fields = {"time": local_to_utc(c["ts"]), "rfid": c.get("epc", ""),
              "ppeResult": "Pass" if c["status"] == "PASS" else "Fail"}
    for label, field in cfg["item_fields"].items():
        fields[field] = ("Pass" if items[label] else "Fail") if label in items else ""
    fields["fabArea"] = cfg["fab_area"]
    return fields


def _already_sent(c: dict) -> bool:
    """Posted at the verdict already (gate_server._post_check_now) — accepted, or set
    aside as rejected. Anything else (pending, failed) still goes through the outbox."""
    return (c.get("it") or {}).get("state") in ("sent", "rejected")


def result_forms(ev: dict, cfg: dict) -> list[tuple[dict, str, str]]:
    """The (form fields, photo path, log label) POSTs one resolved visit becomes, oldest first.

    NO_WORKER (the model saw nobody) and refusals (crowd, badges) are not PPE results.
    An item the checklist does not judge is sent empty — as the tool sends bodyCam.
    """
    forms = []
    unregistered_sent = set()
    for c in ev.get("checks", []):
        if c.get("status") == UNREGISTERED:
            # A badge that is not on the whitelist. No PPE was judged, so like an unchecked
            # entry it goes as Fail with the items empty, carrying the refused badge and the
            # frame of the refusal. Once per badge per visit: a worker standing there is
            # refused again every few seconds, but that is one event, not a stream.
            epc = c.get("epc", "")
            if epc in unregistered_sent:
                continue
            unregistered_sent.add(epc)
            fields = {"time": local_to_utc(c["ts"]), "rfid": epc, "ppeResult": "Fail"}
            for field in cfg["item_fields"].values():
                fields[field] = ""
            fields["fabArea"] = cfg["fab_area"]
            forms.append((fields, c.get("image", ""), "ID未登入"))
            continue
        if not _is_ppe(c) or _already_sent(c):
            continue
        # A PASS for a worker who then did NOT go through (turned back, or stood on) is
        # not sent — 進場/出場檢測通過但沒有進出 (operator, 2026-09-30). A FAIL always is,
        # even one later fixed: 多報比少報好. An "unknown" departure (track lost) still
        # sends, for the same reason. (Checks posted at the verdict never reach here.)
        if c["status"] == "PASS" and ev.get("departure") in ("back", "none"):
            continue
        fields = check_fields(c, cfg)
        forms.append((fields, c.get("image", ""), fields["ppeResult"]))
    if entered_without_pass(ev):
        fields = {
            "time": local_to_utc(ev["ts"]),
            # The strongest badge in range when they walked in, if the reader heard one.
            "rfid": (ev.get("epc") or "").split(" ")[0],
            "ppeResult": "Fail",
        }
        for field in cfg["item_fields"].values():
            fields[field] = ""                  # nothing was judged
        fields["fabArea"] = cfg["fab_area"]
        forms.append((fields, ev.get("image", ""), ev["violation"]))
    return forms


def entered_without_pass(ev: dict) -> bool:
    """Went INTO the plant without a PASS: no check at all, refused, or after a FAIL.

    After a FAIL this is a SECOND post next to the failed check's own (operator,
    2026-09-30): the check says what was missing, at the moment of the check; this one
    says they walked in anyway, with the frame of them crossing into the restricted side."""
    return bool(ev.get("violation")) and ev.get("departure") == "in"


class ITReporter:
    """The outbox sender and the heartbeat. start() / stop() at runtime: each run gets
    its own stop Event, so threads from a previous run can never be mistaken for the
    current one's — toggling off and on quickly cannot leave two senders running."""

    def __init__(self, cfg: dict, record_dir: str, device_status, gate_stopped):
        import requests                        # already a dependency of ultralytics
        import urllib3
        if not cfg["verify_tls"]:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._requests = requests
        self.cfg = cfg
        self.record_dir = record_dir
        self.events_path = os.path.join(record_dir, "events.jsonl")
        self.offset_path = os.path.join(record_dir, "events.sent")
        self.rejected_path = os.path.join(record_dir, "events.rejected.jsonl")
        self.device_status = device_status     # () -> "normal" | "cctv_dead" | ...
        self.gate_stopped = gate_stopped       # () -> bool
        self.lock = threading.Lock()           # stats, and offset moves across runs
        self._evt = threading.Event()
        self._evt.set()                        # set = not running
        self.stat = {"sent": 0, "rejected": 0, "held": 0, "last_ok": None, "last_error": None,
                     "hb_ok": None, "hb_t": None, "hb_status": None, "hb_error": None,
                     "last_reply": None}
        if not os.path.isfile(self.offset_path):
            self._jump_to_end("first run")

    @property
    def running(self) -> bool:
        return not self._evt.is_set()

    def start(self, fresh: bool = False) -> None:
        """fresh = switched on by an operator: report from now on, send a heartbeat now.
        Not fresh = service start with reporting already on: deliver what is waiting, and
        give the camera one interval to connect before the first heartbeat."""
        if self.running:
            return
        if fresh:
            self._jump_to_end("switched on")
        evt = self._evt = threading.Event()
        threading.Thread(target=self._outbox_loop, args=(evt,), name="ITOutbox",
                         daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, args=(evt, not fresh), name="ITHeartbeat",
                         daemon=True).start()

    def stop(self) -> None:
        self._evt.set()

    def set_target(self, name: str) -> None:
        """Switch where results and heartbeats go, live. A running sender is restarted —
        not fresh: whatever is waiting in the outbox goes to the NEW target at once,
        instead of after the old target's retry backoff (up to 2 minutes)."""
        _apply_target(self.cfg, name)
        with self.lock:
            self.stat.update(last_error=None, hb_ok=None, hb_error=None, last_reply=None)
        print(f"[it] target -> {name}: {self.cfg['result_url']}", flush=True)
        if self.running:
            self.stop()
            time.sleep(1.2)          # the old threads notice within a second
            self.start(fresh=False)

    def _halted(self, evt) -> bool:
        return evt.is_set() or self.gate_stopped()

    def _wait(self, seconds: float, evt) -> bool:
        """Sleep in 1 s steps; False as soon as this run is stopped."""
        for _ in range(int(seconds)):
            if self._halted(evt):
                return False
            time.sleep(1)
        return not self._halted(evt)

    # ── outbox ────────────────────────────────────────────────────────────
    def _jump_to_end(self, why: str) -> None:
        """Skip everything already in events.jsonl. On the very first run it is bench and
        commissioning data; when an operator switches reporting back on, it is what
        happened while they had it off — neither belongs in the live dashboard."""
        end = os.path.getsize(self.events_path) if os.path.isfile(self.events_path) else 0
        with self.lock:
            skipped = end - self._load_offset() if os.path.isfile(self.offset_path) else end
            self._save_offset(end)
        print(f"[it] {why}: reporting starts now — {max(0, skipped)} bytes of earlier "
              f"events.jsonl not sent", flush=True)

    def _load_offset(self) -> int:
        try:
            with open(self.offset_path) as fh:
                return int(fh.read().strip() or 0)
        except (OSError, ValueError):
            return 0

    def _save_offset(self, off: int) -> None:
        tmp = self.offset_path + ".part"
        with open(tmp, "w") as fh:
            fh.write(str(off))
        os.replace(tmp, self.offset_path)

    def pending(self) -> int:
        """Bytes of events.jsonl not yet handled — 0 means the outbox is empty."""
        size = os.path.getsize(self.events_path) if os.path.isfile(self.events_path) else 0
        return max(0, size - self._load_offset())

    def post_check(self, fields: dict, photo: bytes | None, photo_name: str):
        """Post ONE check result now, at the verdict, and return (http status, reply).

        The gate needs IT's reply at once: its access_type ("entry" / "leave") decides
        whether the PASS voice says 請進場 or 請出場 (operator, 2026-10-01). The photo is
        the judged frame's JPEG from memory — its file is still being written. Raises on
        a network error; the caller then leaves the check for the outbox to deliver."""
        cfg = self.cfg
        if photo:
            r = self._requests.post(cfg["result_url"], data=fields,
                                    files={"photo": (photo_name, photo, "image/jpeg")},
                                    verify=cfg["verify_tls"], timeout=cfg["timeout_s"])
        else:
            r = self._requests.post(cfg["result_url"], json=fields,
                                    verify=cfg["verify_tls"], timeout=cfg["timeout_s"])
        try:
            reply = r.json()
        except ValueError:
            reply = {"raw": r.text[:120]}
        if r.status_code < 400:
            self._note(sent=1)
            with self.lock:
                self.stat["last_reply"] = reply
        elif r.status_code >= 500 or r.status_code in (408, 429):
            self._note(error=f"HTTP {r.status_code}: {r.text[:200]}")
        else:
            with open(self.rejected_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": utc_now(), "http": r.status_code, "body": r.text[:500],
                                     "fields": fields, "at_verdict": True}, ensure_ascii=False) + "\n")
            self._note(rejected=1, error=f"rejected HTTP {r.status_code}: {r.text[:200]}")
        at = reply.get("access_type") if isinstance(reply, dict) else None
        print(f"[it] ppe-result sent at the verdict: {fields['ppeResult']} "
              f"rfid={fields['rfid'] or '—'} -> HTTP {r.status_code}"
              + (f" access_type={at}" if at else ""), flush=True)
        return r.status_code, reply

    def _post_result(self, fields: dict, photo_rel: str):
        url, cfg = self.cfg["result_url"], self.cfg
        photo = os.path.join(self.record_dir, photo_rel) if photo_rel else ""
        if photo and os.path.isfile(photo):
            with open(photo, "rb") as fh:
                return self._requests.post(url, data=fields,
                                           files={"photo": (os.path.basename(photo), fh, "image/jpeg")},
                                           verify=cfg["verify_tls"], timeout=cfg["timeout_s"])
        # The tool's own no-photo path: the same fields, as JSON.
        return self._requests.post(url, json=fields, verify=cfg["verify_tls"], timeout=cfg["timeout_s"])

    def _drain(self, evt) -> bool:
        """Send every complete, unsent line. False = the server could not be reached (or
        erred on its side): stop here, keep the offset, try again after a backoff."""
        if not os.path.isfile(self.events_path):
            return True
        off = self._load_offset()
        with open(self.events_path, "rb") as fh:
            fh.seek(off)
            data = fh.read()
        for raw in data.splitlines(keepends=True):
            if not raw.endswith(b"\n") or self._halted(evt):
                break                            # still being written, or switched off
            try:
                ev = json.loads(raw)
            except ValueError:
                ev = {}
            if ev.get("it_report"):
                for fields, photo, label in result_forms(ev, self.cfg):
                    try:
                        r = self._post_result(fields, photo)
                    except Exception as e:
                        self._note(error=f"{type(e).__name__}: {e}")
                        return False
                    if r.status_code >= 500 or r.status_code in (408, 429):
                        self._note(error=f"HTTP {r.status_code}: {r.text[:200]}")
                        return False
                    if r.status_code >= 400:
                        self._reject(ev, fields, r)
                        continue
                    self._note(sent=1)
                    # The service answers {"id", "access_type": "entry"|"leave", "message"}
                    # — its own in/out decision. Logged next to ours, not acted on.
                    try:
                        reply = r.json()
                    except ValueError:
                        reply = {"raw": r.text[:120]}
                    with self.lock:
                        self.stat["last_reply"] = reply
                    at = reply.get("access_type") if isinstance(reply, dict) else None
                    print(f"[it] ppe-result sent: {label} rfid={fields['rfid'] or '—'} "
                          f"-> HTTP {r.status_code}"
                          + (f" access_type={at}" if at else "")
                          + (f" id={reply.get('id')}" if isinstance(reply, dict) and reply.get("id") else ""),
                          flush=True)
                if ev.get("violation") and not result_forms(ev, self.cfg):
                    self._note(held=1)            # e.g. 未檢查即出場 — exits are not reported
            with self.lock:
                if self._halted(evt):
                    break                        # switched off mid-line: never move the offset
                off += len(raw)
                self._save_offset(off)
        return True

    def _reject(self, ev: dict, fields: dict, r) -> None:
        with open(self.rejected_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": utc_now(), "http": r.status_code, "body": r.text[:500],
                                 "fields": fields, "event": ev}, ensure_ascii=False) + "\n")
        self._note(rejected=1, error=f"rejected HTTP {r.status_code}: {r.text[:200]}")
        print(f"[it] ppe-result REJECTED HTTP {r.status_code}: {r.text[:200]} "
              f"(kept in events.rejected.jsonl)", flush=True)

    def _note(self, sent=0, rejected=0, held=0, error=None) -> None:
        with self.lock:
            self.stat["sent"] += sent
            self.stat["rejected"] += rejected
            self.stat["held"] += held
            if sent:
                self.stat["last_ok"] = utc_now()
            if error:
                self.stat["last_error"] = f"{utc_now()} {error}"
            elif sent:
                self.stat["last_error"] = None

    def _outbox_loop(self, evt) -> None:
        backoff = RETRY_MIN_S
        while not self._halted(evt):
            try:
                ok = self._drain(evt)
            except Exception as e:               # never let the sender die silently
                print(f"[it] outbox error: {type(e).__name__}: {e}", flush=True)
                ok = False
            if ok:
                backoff = RETRY_MIN_S
                self._wait(DRAIN_POLL_S, evt)
            else:
                print(f"[it] cannot deliver — retrying in {backoff:.0f}s "
                      f"({self.stat['last_error']})", flush=True)
                self._wait(backoff, evt)
                backoff = min(RETRY_MAX_S, backoff * 2)

    # ── heartbeat ─────────────────────────────────────────────────────────
    def _heartbeat_loop(self, evt, grace: bool) -> None:
        cfg = self.cfg
        # At service start this runs before the model has loaded and before the camera
        # thread has connected, so an immediate beat would tell IT "cctv_dead" on every
        # restart. A camera that is genuinely dead is still reported — one interval later.
        if grace and not self._wait(cfg["heartbeat_interval_s"], evt):
            return
        while not self._halted(evt):
            dev = self.device_status()
            body = {"time": utc_now(), "clientId": cfg["client_id"], "fabArea": cfg["fab_area"],
                    "system_status": "normal" if dev == "normal" else "abnormal",
                    "device_status": dev}
            try:
                r = self._requests.post(cfg["heartbeat_url"], json=body,
                                        verify=cfg["verify_tls"], timeout=5)
                ok, err = r.status_code < 400, None if r.status_code < 400 else f"HTTP {r.status_code}"
            except Exception as e:
                ok, err = False, f"{type(e).__name__}: {e}"
            with self.lock:
                self.stat.update(hb_ok=ok, hb_t=body["time"], hb_status=dev,
                                 hb_error=err and f"{body['time']} {err}")
            if not ok:
                print(f"[it] heartbeat failed: {err}", flush=True)
            elif dev != "normal":
                print(f"[it] heartbeat reported {dev}", flush=True)
            if not self._wait(cfg["heartbeat_interval_s"], evt):
                return

    def status(self) -> dict:
        with self.lock:
            s = dict(self.stat)
        s.update(enabled=self.running, pending_bytes=self.pending() if self.running else 0,
                 fab_area=self.cfg["fab_area"], client_id=self.cfg["client_id"],
                 target=self.cfg.get("target"), targets=list(self.cfg.get("targets") or {}),
                 result_url=self.cfg["result_url"])
        return s
