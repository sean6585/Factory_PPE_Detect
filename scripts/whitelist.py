"""Local badge whitelist: which RFID badge IDs (EPCs) may go through the gate.

Lives next to gate.json as config/whitelist.json:

    {"enabled": true, "ids": ["202609290008", "202609290009"]}

Re-read whenever the file changes, so adding or removing a badge takes effect at the next
worker without restarting the gate. Unlike gate.json, nothing in the UI writes this file:
it is edited by hand (or, later, synced from a site system).

The states, and why each fails the way it does:
  no file, or "enabled": false   OFF. Every badge is allowed — how the gate ran before the
                                 list existed, so a site without one is unaffected.
  file present but unusable      FAIL CLOSED. Every badge counts as unregistered until the
    (bad JSON, "ids" not a list)  file is fixed: a broken list must never quietly become
                                 "let everyone in".
  otherwise                      a badge is allowed iff its ID is in "ids". An empty list
                                 therefore lets nobody through, deliberately.

IDs are compared trimmed and upper-cased: EPCs are hex, and a list typed by hand should
not fail on case or a stray space. They must be JSON strings — a bare number would lose
any leading zeros before this code ever saw it.
"""

from __future__ import annotations

import json
import os
import threading


def _norm(epc) -> str:
    return str(epc).strip().upper()


class Whitelist:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._stamp = object()          # never equal to a real stamp: first call loads
        self._enabled = False
        self._ids: frozenset[str] = frozenset()
        self._error: str | None = None

    def _refresh(self) -> None:
        """Reload if the file appeared, vanished or changed since the last look. One stat()
        per call — the gate asks once per worker, not per frame."""
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            stamp = None
        if stamp == self._stamp:
            return
        self._stamp = stamp
        if stamp is None:
            self._enabled, self._ids, self._error = False, frozenset(), None
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict) or not isinstance(doc.get("ids"), list):
                raise ValueError('expected {"enabled": true, "ids": ["...", ...]}')
            bad = [x for x in doc["ids"] if not isinstance(x, str)]
            if bad:
                raise ValueError(f"every ID must be a quoted string; not: {bad[:3]}")
            self._enabled = doc.get("enabled", True) is not False
            self._ids = frozenset(_norm(x) for x in doc["ids"] if _norm(x))
            self._error = None
        except Exception as e:                       # noqa: BLE001 — any failure fails closed
            self._enabled, self._ids = True, frozenset()
            self._error = f"{type(e).__name__}: {e}"

    def allowed(self, epc) -> bool | None:
        """True / False when the whitelist is in force; None when it is off (no file, or
        "enabled": false), meaning "not applicable — let the badge through"."""
        with self._lock:
            self._refresh()
            if not self._enabled:
                return None
            if self._error:
                return False
            return _norm(epc) in self._ids

    def state(self) -> dict:
        with self._lock:
            self._refresh()
            return {"path": self.path,
                    "exists": self._stamp is not None,
                    "enabled": self._enabled,
                    "count": len(self._ids),
                    "ids": sorted(self._ids),
                    "error": self._error}
