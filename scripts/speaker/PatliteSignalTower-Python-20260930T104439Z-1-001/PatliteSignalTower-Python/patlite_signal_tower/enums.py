"""手冊 5.3.13 定義的參數值列舉。數值即為實際送出的參數值。"""

from __future__ import annotations

from enum import Enum, IntEnum

__all__ = [
    "TowerProtocol",
    "TowerLight",
    "LightPattern",
    "BuzzerPattern",
    "DigitalOutputState",
    "MultiColor",
    "MultiColorPattern",
    "SpeechLanguage",
    "VoiceGender",
    "StatusFormat",
]


class TowerProtocol(str, Enum):
    HTTP = "http"
    HTTPS = "https"


class TowerLight(IntEnum):
    """訊號燈燈色（對應 alert / led 參數的位置順序）。"""

    RED = 0
    AMBER = 1
    GREEN = 2
    BLUE = 3
    WHITE = 4


class LightPattern(IntEnum):
    OFF = 0
    ON = 1
    FLASH1 = 2
    FLASH2 = 3
    FLASH3 = 4
    FLASH4 = 5
    NO_CHANGE = 9  # 維持現狀


class BuzzerPattern(IntEnum):
    OFF = 0
    PATTERN1 = 1
    PATTERN2 = 2
    PATTERN3 = 3
    PATTERN4 = 4
    PATTERN5 = 5
    NO_CHANGE = 9


class DigitalOutputState(IntEnum):
    OFF = 0
    ON = 1
    NO_CHANGE = 9


class MultiColor(IntEnum):
    """多色燈顏色。送出時轉成字串（None → NONE）。"""

    NONE = 0
    RED = 1
    AMBER = 2
    GREEN = 3
    BLUE = 4
    WHITE = 5
    PURPLE = 6
    CYAN = 7

    def to_parameter(self) -> str:
        return "NONE" if self is MultiColor.NONE else self.name.capitalize()


class MultiColorPattern(IntEnum):
    """c-pat，沒有「不控制」選項。"""

    ON = 1
    FLASH1 = 2
    FLASH2 = 3
    FLASH3 = 4
    FLASH4 = 5


class SpeechLanguage(str, Enum):
    JAPANESE = "jp"
    ENGLISH = "en"
    CHINESE = "cn"


class VoiceGender(str, Enum):
    MALE = "male"
    FEMALE = "female"


class StatusFormat(str, Enum):
    JSON = "json"
    XML = "xml"
