"""/api/status 回應的解析與描述。

手冊只列出「可取得的資料」清單、未提供實際封裝範例，因此解析採寬鬆策略：
能對應到的欄位給具名屬性，其餘一律保留在 ``fields``。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from .enums import BuzzerPattern, LightPattern, MultiColor, StatusFormat, TowerLight

__all__ = ["SignalTowerStatus"]

_LIGHT_NAMES = ["紅 R", "黃 Y", "綠 G", "藍 B", "白 C"]

_PATTERN_TEXT = {
    LightPattern.OFF: "熄滅 / 不支援",
    LightPattern.ON: "恆亮",
    LightPattern.FLASH1: "閃爍樣式 1",
    LightPattern.FLASH2: "閃爍樣式 2",
    LightPattern.FLASH3: "閃爍樣式 3",
    LightPattern.FLASH4: "閃爍樣式 4",
    LightPattern.NO_CHANGE: "不控制",
}

_COLOR_TEXT = {
    MultiColor.NONE: "熄滅 / 不支援的顏色",
    MultiColor.RED: "紅 Red",
    MultiColor.AMBER: "黃 Amber",
    MultiColor.GREEN: "綠 Green",
    MultiColor.BLUE: "藍 Blue",
    MultiColor.WHITE: "白 White",
    MultiColor.PURPLE: "紫 Purple",
    MultiColor.CYAN: "淺藍 Light blue",
}

_BUZZER_TEXT = {
    BuzzerPattern.OFF: "停止",
    BuzzerPattern.PATTERN1: "蜂鳴樣式 1",
    BuzzerPattern.PATTERN2: "蜂鳴樣式 2",
    BuzzerPattern.PATTERN3: "蜂鳴樣式 3",
    BuzzerPattern.PATTERN4: "蜂鳴樣式 4",
    BuzzerPattern.PATTERN5: "蜂鳴樣式 5",
    BuzzerPattern.NO_CHANGE: "不控制",
}


def _as_enum(enum_cls, value: int):
    try:
        return enum_cls(value)
    except ValueError:
        return None


class SignalTowerStatus:
    """設備狀態快照。"""

    def __init__(self, raw_body: str = "", fmt: StatusFormat = StatusFormat.JSON):
        self.raw_body = raw_body
        self.format = StatusFormat(fmt)
        self.fields: Dict[str, str] = {}
        self.lights: List[Optional[LightPattern]] = [None] * 5
        self.multi_color: Optional[MultiColor] = None
        self.multi_pattern: Optional[LightPattern] = None
        self.buzzer: Optional[BuzzerPattern] = None
        self.sound_channel: Optional[int] = None
        self.digital_outputs: List[bool] = []
        self.digital_inputs: List[bool] = []
        self.software_version: Optional[str] = None
        self.mac_address: Optional[str] = None

    # ── 具名存取 ────────────────────────────────────────────
    @property
    def red(self) -> Optional[LightPattern]:
        return self.lights[0]

    @property
    def amber(self) -> Optional[LightPattern]:
        return self.lights[1]

    @property
    def green(self) -> Optional[LightPattern]:
        return self.lights[2]

    @property
    def blue(self) -> Optional[LightPattern]:
        return self.lights[3]

    @property
    def white(self) -> Optional[LightPattern]:
        return self.lights[4]

    def __getitem__(self, light: TowerLight) -> Optional[LightPattern]:
        return self.lights[int(light)]

    @property
    def any_light_on(self) -> bool:
        return any(p is not None and p is not LightPattern.OFF for p in self.lights)

    @property
    def buzzer_active(self) -> bool:
        return self.buzzer is not None and self.buzzer is not BuzzerPattern.OFF

    # ── 解析 ────────────────────────────────────────────────
    @classmethod
    def parse(cls, body: str, fmt: StatusFormat = StatusFormat.JSON) -> "SignalTowerStatus":
        status = cls(body or "", fmt)
        if not body or not body.strip():
            return status
        if StatusFormat(fmt) is StatusFormat.JSON:
            status._parse_json(body)
        else:
            status._parse_xml(body)
        return status

    def _parse_json(self, body: str) -> None:
        data = json.loads(body)
        if not isinstance(data, dict):
            return

        for name, value in _flatten(data):
            self.fields[name] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            numbers = _as_numbers(value)

            if name == "Unit_Status":
                for i, number in enumerate(numbers[:5]):
                    self.lights[i] = _as_enum(LightPattern, number)
            elif name == "Multi_Color" and len(numbers) == 1:
                self.multi_color = _as_enum(MultiColor, numbers[0])
            elif name == "Multi_Pattern" and len(numbers) == 1:
                self.multi_pattern = _as_enum(LightPattern, numbers[0])
            elif name == "Buzzer_Pattern" and len(numbers) == 1:
                self.buzzer = _as_enum(BuzzerPattern, numbers[0])
            elif name == "Sound_CH" and len(numbers) == 1:
                self.sound_channel = numbers[0]
            elif name == "Digital_Output":
                self.digital_outputs = [n != 0 for n in numbers]
            elif name == "Digital_Input":
                self.digital_inputs = [n != 0 for n in numbers]
            elif name == "Software_Version":
                self.software_version = self.fields[name]
            elif name == "MAC_Address":
                self.mac_address = self.fields[name]

    def _parse_xml(self, body: str) -> None:
        root = ET.fromstring(body)
        outputs: List[bool] = []
        inputs: List[bool] = []

        for node in root.iter():
            if len(node) > 0:  # 非葉節點
                continue
            label = node.attrib.get("name") or node.tag
            text = (node.text or "").strip()
            self.fields[label] = text
            key = label.upper()

            try:
                value = int(text)
            except ValueError:
                if "VERSION" in key:
                    self.software_version = text
                elif "MAC" in key:
                    self.mac_address = text
                continue

            if key.startswith("LED") and key[3:].isdigit() and 1 <= int(key[3:]) <= 5:
                self.lights[int(key[3:]) - 1] = _as_enum(LightPattern, value)
            elif key == "MULTI_COL":
                self.multi_color = _as_enum(MultiColor, value)
            elif key == "MULTI_PAT":
                self.multi_pattern = _as_enum(LightPattern, value)
            elif "BUZZER" in key:
                self.buzzer = _as_enum(BuzzerPattern, value)
            elif "SOUND" in key:
                self.sound_channel = value
            elif key.startswith("DO-"):
                outputs.append(value != 0)
            elif key.startswith("DIN-"):
                inputs.append(value != 0)

        if outputs:
            self.digital_outputs = outputs
        if inputs:
            self.digital_inputs = inputs

    # ── 描述 ────────────────────────────────────────────────
    def describe(self) -> str:
        lines: List[str] = []
        known = set()

        if any(p is not None for p in self.lights):
            lines.append("訊號燈狀態")
            for i, pattern in enumerate(self.lights):
                if pattern is not None:
                    lines.append(f"    {_LIGHT_NAMES[i]} : {int(pattern)} = {_PATTERN_TEXT.get(pattern, '(未定義)')}")
            known.update({"Unit_Status", "LED1", "LED2", "LED3", "LED4", "LED5"})

        if self.multi_color is not None:
            lines.append(f"多色燈顏色 : {int(self.multi_color)} = {_COLOR_TEXT.get(self.multi_color, '(未定義)')}")
            known.update({"Multi_Color", "MULTI_COL"})

        if self.multi_pattern is not None:
            lines.append(
                f"多色燈樣式 : {int(self.multi_pattern)} = {_PATTERN_TEXT.get(self.multi_pattern, '(未定義)')}"
            )
            known.update({"Multi_Pattern", "MULTI_PAT"})

        if self.buzzer is not None:
            lines.append(f"蜂鳴器     : {int(self.buzzer)} = {_BUZZER_TEXT.get(self.buzzer, '(未定義)')}")
            known.update({"Buzzer_Pattern", "BUZZER Sounds"})

        if self.sound_channel is not None:
            text = "0 = 停止播放" if self.sound_channel == 0 else f"{self.sound_channel} ch"
            lines.append(f"音源通道   : {text}")
            known.add("Sound_CH")

        if self.digital_outputs:
            lines.append("數位輸出   : " + _io(self.digital_outputs, "DO"))
            known.add("Digital_Output")

        if self.digital_inputs:
            lines.append("數位輸入   : " + _io(self.digital_inputs, "DIN"))
            known.add("Digital_Input")

        if self.software_version:
            lines.append(f"軟體版本   : {self.software_version}")
            known.add("Software_Version")

        if self.mac_address:
            lines.append(f"MAC 位址   : {self.mac_address}")
            known.add("MAC_Address")

        others = {
            k: v
            for k, v in self.fields.items()
            if k not in known and not k.upper().startswith(("DO-", "DIN-"))
        }
        if others:
            lines.append("")
            lines.append("其他欄位")
            lines.extend(f"    {k} : {v}" for k, v in others.items())

        return "\n".join(lines) if lines else "（回應中沒有可解析的欄位）"

    def pretty_raw(self) -> str:
        """格式化原始回應，解析失敗時原樣回傳。"""
        try:
            if self.format is StatusFormat.JSON:
                return json.dumps(json.loads(self.raw_body), indent=2, ensure_ascii=False)
            root = ET.fromstring(self.raw_body)
            _indent(root)
            return ET.tostring(root, encoding="unicode")
        except Exception:
            return self.raw_body

    def __str__(self) -> str:
        return self.describe()


def _flatten(data: Dict[str, Any], out=None):
    """攤平巢狀物件，讓不同韌體的封裝格式都能讀。"""
    out = [] if out is None else out
    for key, value in data.items():
        if isinstance(value, dict):
            _flatten(value, out)
        else:
            out.append((key, value))
    return out


def _as_numbers(value: Any) -> List[int]:
    if isinstance(value, bool):
        return [int(value)]
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        try:
            return [int(value.strip())]
        except ValueError:
            return []
    if isinstance(value, (list, tuple)):
        numbers: List[int] = []
        for item in value:
            numbers.extend(_as_numbers(item))
        return numbers
    return []


def _io(values: List[bool], prefix: str) -> str:
    return "  ".join(f"{prefix}-{i + 1}={'ON' if v else 'OFF'}" for i, v in enumerate(values))


def _indent(elem, level: int = 0) -> None:
    pad = "\n" + "  " * level
    if len(elem):
        if not (elem.text or "").strip():
            elem.text = pad + "  "
        for child in elem:
            _indent(child, level + 1)
        if not (child.tail or "").strip():
            child.tail = pad
    if level and not (elem.tail or "").strip():
        elem.tail = pad
