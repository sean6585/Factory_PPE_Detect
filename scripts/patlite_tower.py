"""PATLITE NHV-series signal tower (tri-colour light + voice) over its HTTP API.

The gate's own client, standard library only. The vendor's fuller Python library sits
under scripts/speaker/ in a folder named after its download timestamp; this carries just
the few commands the gate uses, so nothing imports from that folder. Parameter values are
the manual's (5.3.13 "HTTP Command Reception Function"), cross-checked against that
library's enums.

The unit answers HTTP 200 whether a command worked or not — the body is the only signal:
"Success." or "Error. [NNN]". A factory-fresh unit returns Error [002] to EVERY command,
even an empty one, until two things are done once in its web UI (port 80):
  1. the first-access page has created an account, and
  2. Enable Feature → "HTTP Command Control" is switched on.
/api/control and /api/status need no credentials after that.
"""

import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

# Lamp order inside the 5-digit led/alert codes: red, amber, green, blue, white. The
# NHV6-3 has only the first three; the other two digits are still sent.
LAMPS = ("red", "amber", "green")
SPEECH_LANGS = ("en", "jp", "cn")     # the sample unit speaks en/jp only; cn = newer units
SPEECH_MAX = 400                      # the unit truncates beyond this
FLASH1 = 2                            # lamp pattern: flashing (0 off, 1 on, 9 no change)

ERROR_CODES = {
    "002": "invalid command — on a fresh unit this means HTTP Command Control is "
           "disabled (web UI → Enable Feature)",
    "003": "no command given",
    "004": "a parameter has no value",
    "005": "invalid value",
}


class TowerError(Exception):
    pass


class Tower:
    def __init__(self, host, timeout=3.0):
        self.host, self.timeout = host, timeout

    def _get(self, path, params):
        # quote, not quote_plus: the unit's parser takes %20 for a space in speech text,
        # and the vendor library encodes it the same way.
        query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        url = f"http://{self.host}/api/{path}" + (f"?{query}" if query else "")
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                return r.read().decode("utf-8", errors="replace").strip()
        except urllib.error.HTTPError as e:
            raise TowerError(f"HTTP {e.code} from {self.host}") from e
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise TowerError(f"cannot reach {self.host}: {reason}") from e

    def control(self, **params):
        body = self._get("control", params)
        if body.startswith("Success"):
            return body
        m = re.search(r"\[(\d+)\]", body)
        code = m.group(1) if m else None
        raise TowerError(f"tower refused {params}: Error [{code}] "
                         f"{ERROR_CODES.get(code, body[:80])}")

    def status(self):
        body = self._get("status", {"format": "json"})
        try:
            return json.loads(body)
        except ValueError:
            # Not JSON = the error text, e.g. "Error. [002]" on an unconfigured unit.
            raise TowerError(f"status unreadable: {body[:80]}") from None

    def flash(self, lamp, seconds=5):
        """Flash ONE lamp, the others dark, then let the unit restore by itself.

        alert + restore, not led: restore makes the tower revert to whatever it was
        showing after `seconds` on its own, so a test can never leave a lamp stuck on
        even if nobody presses "off" (measured: red went 0 → 2 → back to 0 after 4 s).
        """
        if lamp not in LAMPS:
            raise ValueError(f"lamp must be one of {', '.join(LAMPS)}")
        if not 1 <= int(seconds) <= 99:
            raise ValueError("restore time must be 1-99 s")
        digits = ["0"] * 5
        digits[LAMPS.index(lamp)] = str(FLASH1)
        return self.control(alert="".join(digits) + "0", restore=int(seconds))

    def set_base(self, pattern):
        """The state the unit falls back to whenever an alert's restore timer runs out
        (5 digits, red/amber/green/blue/white). The gate keeps this red steady."""
        return self.control(led=pattern)

    def show(self, pattern, restore_s):
        """Show a lamp pattern for restore_s seconds, then fall back to the base — the
        gate re-sends it every second, so it only falls back if the gate stops."""
        return self.control(alert=pattern + "0", restore=int(restore_s))

    def play(self, channel):
        """Play one registered voice channel (1-60; the gate's are 1-5)."""
        return self.control(sound=int(channel))

    def speak(self, text, lang="en"):
        text = (text or "").strip()
        if not text:
            raise ValueError("nothing to say")
        if lang not in SPEECH_LANGS:
            raise ValueError(f"lang must be one of {', '.join(SPEECH_LANGS)}")
        return self.control(speech=text[:SPEECH_MAX], lang=lang)

    def all_off(self):
        """Every lamp and the buzzer off, then stop any sound or speech still playing."""
        self.control(alert="000000")
        return self.control(stop=1)

    # ── settings the HTTP command API does not cover: the web UI, logged in ──────
    # The speaker volume is only on the unit's Basic Settings page (/setting). That form
    # posts EVERY field on it, so a volume change re-sends all the others exactly as they
    # are: the ones the page's own script fills from /config.json, plus the two the server
    # renders into the HTML (unitVol, the mute checkbox). The unit allows ONE web login at
    # a time — an operator logged in from a browser blocks this until they log out.
    VOLUME_MAX = 15

    def _web_session(self, user, password):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        opener.addheaders = [("User-Agent", "Mozilla/5.0")]
        body, ctype = _multipart([("username", user), ("pass", password)])
        answer = self._web(opener, "/login", body, ctype)
        if answer != "0":
            raise TowerError({"-1": "tower web login refused: wrong user or password",
                              "-2": "tower web UI is in use — someone is logged in from a "
                                    "browser; log out there and try again"}
                             .get(answer, f"tower web login failed ({answer[:40]})"))
        return opener

    def _web(self, opener, path, body=None, ctype=None):
        req = urllib.request.Request(f"http://{self.host}{path}", data=body)
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with opener.open(req, timeout=max(self.timeout, 10)) as r:
                return r.read().decode("utf-8", errors="replace").strip()
        except (urllib.error.URLError, OSError) as e:
            raise TowerError(f"tower web UI {path}: {getattr(e, 'reason', e)}") from e

    def _logout(self, opener):
        try:
            self._web(opener, "/logout")
        except TowerError:
            pass                 # the unit times the session out on its own anyway

    def get_volume(self, user, password):
        opener = self._web_session(user, password)
        try:
            page = self._web(opener, "/setting")
        finally:
            self._logout(opener)
        return _volume_from_page(page)

    def set_volume(self, volume, user, password, mute=None):
        """mute: None keeps the unit's current Mute box as it is; True / False sets it.
        A muted unit accepts every sound command, reports the channel as playing in
        /api/status, and makes no sound — so the kiosk shows and sets it (2026-10-03:
        the tower had been muted from its own web page and the gate went silent)."""
        volume = int(volume)
        if not 0 <= volume <= self.VOLUME_MAX:
            raise ValueError(f"volume must be 0-{self.VOLUME_MAX}")
        opener = self._web_session(user, password)
        try:
            page = self._web(opener, "/setting")
            unit = json.loads(self._web(opener, "/config.json"))["NH_VALUE"]["unit"]
            now = _volume_from_page(page)
            muted = now["mute"] if mute is None else bool(mute)
            # The page posts a hidden mute=0 and, when the box is ticked, mute=1 after it.
            fields = [("sysName", unit.get("sys_name", "")),
                      ("sysLocation", unit.get("sys_location", "")),
                      ("sysContact", unit.get("sys_contact", "")),
                      ("unitVol", str(volume)),
                      ("mute", "0")]
            if muted:
                fields.append(("mute", "1"))
            fields += [("lineoutVol", str(unit.get("volume_lo", 12))),
                       ("mp3_play_mode", str(unit.get("play_mode", 0))),
                       ("addon_unit", str(unit.get("addon_unit", 0))),
                       ("dimming", str(unit.get("dimming", 4))),
                       ("dimming", str(unit.get("dimming", 4))),
                       ("normal", str(unit.get("normalAction", 0)))]
            body, ctype = _multipart(fields)
            answer = self._web(opener, "/setting", body, ctype)
        finally:
            self._logout(opener)
        if answer not in ("0", "1"):
            raise TowerError(f"tower refused the setting ({answer[:60]})")
        return {"volume": volume, "mute": muted, "restart_needed": answer == "1"}


def _volume_from_page(page):
    m = re.search(r'name="unitVol"[^>]*value="(\d+)"', page)
    if not m:
        raise TowerError("tower settings page has no speaker volume (logged out?)")
    mute = re.search(r'<input type="checkbox" name="mute"[^>]*>', page)
    return {"volume": int(m.group(1)), "max": Tower.VOLUME_MAX,
            "mute": bool(mute and re.search(r"\bchecked\b", mute.group(0)))}


def _multipart(fields):
    """multipart/form-data, which is what the unit's own pages send (jQuery FormData)."""
    boundary = "----gate" + uuid.uuid4().hex
    out = []
    for name, value in fields:
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                   f"{value}\r\n")
    out.append(f"--{boundary}--\r\n")
    return "".join(out).encode("utf-8"), f"multipart/form-data; boundary={boundary}"
