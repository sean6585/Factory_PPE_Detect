#!/usr/bin/env python3
"""信號燈模擬器：沒有實機時也能測試 console 與程式庫。

    python tools/fake_tower.py            # 監聽 127.0.0.1:8123
    python tools/fake_tower.py 0.0.0.0 80 # 指定位址與埠號

另一個視窗：
    python console_demo.py 127.0.0.1 --port 8123

它只模擬手冊的基本行為（回應 Success. / Error.<code>、維護一份狀態），
實機的細節（restore 倒數、音源播放時間）並未實作。
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

STATE = {
    "Unit_Status": [0, 0, 0, 0, 0],
    "Multi_Color": 0,
    "Multi_Pattern": 0,
    "Buzzer_Pattern": 0,
    "Sound_CH": 0,
    "Digital_Output": [0, 0],
    "Digital_Input": [0, 0, 0, 0],
    "Software_Version": "0.0.0-fake",
    "MAC_Address": "00:1B:44:11:3A:B7",
}

COLOR_INDEX = {"NONE": 0, "Red": 1, "Amber": 2, "Green": 3, "Blue": 4, "White": 5, "Purple": 6, "Cyan": 7}
VALID = {"alert", "led", "output", "color", "c-pat", "b-pat", "sound", "repeat", "restore",
         "stop", "clear", "speech", "lang", "voice", "speed", "tone", "notify", "notifyTail", "lineout"}


def apply_control(query) -> str:
    if not query:
        return "Error.003"
    for key in query:
        if key not in VALID:
            return "Error.002"

    if "led" in query:
        value = query["led"][0]
        if len(value) != 5 or not value.isdigit():
            return "Error.005"
        STATE["Unit_Status"] = [
            int(c) if c != "9" else old for c, old in zip(value, STATE["Unit_Status"])
        ]

    if "alert" in query:
        value = query["alert"][0]
        if len(value) != 6 or not value.isdigit():
            return "Error.005"
        STATE["Unit_Status"] = [
            int(c) if c != "9" else old for c, old in zip(value[:5], STATE["Unit_Status"])
        ]
        if value[5] != "9":
            STATE["Buzzer_Pattern"] = int(value[5])

    if "output" in query:
        value = query["output"][0]
        if len(value) != 2 or not value.isdigit():
            return "Error.005"
        STATE["Digital_Output"] = [
            int(c) if c != "9" else old for c, old in zip(value, STATE["Digital_Output"])
        ]

    if "color" in query:
        STATE["Multi_Color"] = COLOR_INDEX.get(query["color"][0], 0)
        STATE["Multi_Pattern"] = int(query.get("c-pat", ["1"])[0])
        if query.get("b-pat", ["9"])[0] != "9":
            STATE["Buzzer_Pattern"] = int(query["b-pat"][0])

    if "sound" in query:
        channel = int(query["sound"][0])
        if not 1 <= channel <= 71:
            return "Error.005"
        STATE["Sound_CH"] = channel

    if "speech" in query:
        print(f"  [模擬播報] {query['speech'][0]}  (lang={query.get('lang', ['jp'])[0]})")

    if "stop" in query:
        STATE["Sound_CH"] = 0
    if "clear" in query:
        STATE["Unit_Status"] = [0] * 5
        STATE["Buzzer_Pattern"] = 0
        STATE["Sound_CH"] = 0

    return "Success."


def status_xml() -> str:
    names = ["LED1", "LED2", "LED3", "LED4", "LED5"]
    colors = "".join(f'<color name="{n}">{v}</color>' for n, v in zip(names, STATE["Unit_Status"]))
    colors += f'<color name="MULTI_COL">{STATE["Multi_Color"]}</color>'
    colors += f'<color name="MULTI_PAT">{STATE["Multi_Pattern"]}</color>'
    ports = "".join(f'<port name="DO-{i + 1}">{v}</port>' for i, v in enumerate(STATE["Digital_Output"]))
    ports += "".join(f'<port name="DIN-{i + 1}">{v}</port>' for i, v in enumerate(STATE["Digital_Input"]))
    return (
        "<signal-tower-status>"
        f"{colors}"
        f'<buzzer name="BUZZER Sounds">{STATE["Buzzer_Pattern"]}</buzzer>'
        f'<sound name="SOUND Audio Playback">{STATE["Sound_CH"]}</sound>'
        f"{ports}"
        "</signal-tower-status>"
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安靜一點
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        print(f"  → {self.path}")

        if parsed.path == "/api/control":
            self._reply(apply_control(query))
        elif parsed.path == "/api/status":
            fmt = query.get("format", ["json"])[0]
            if fmt == "xml":
                self._reply(status_xml(), "application/xml")
            elif fmt == "json":
                self._reply(json.dumps(STATE, ensure_ascii=False), "application/json")
            else:
                self._reply("Error.")
        else:
            self.send_response(404)
            self.end_headers()

    def _reply(self, text: str, content_type: str = "text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8123
    server = HTTPServer((host, port), Handler)
    print(f"信號燈模擬器啟動：http://{host}:{port}/api/  （Ctrl+C 結束）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n結束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
