"""PATLITE 網路型信號燈 HTTP 控制程式庫。

依《Network Signal Tower Control / Network Signal Tower with Voice Annunciator
Control》手冊 5.3.13 HTTP Command Reception Function 實作，僅使用標準函式庫。
"""

from .client import SignalTowerClient
from .command import SignalTowerCommand
from .enums import (
    BuzzerPattern,
    DigitalOutputState,
    LightPattern,
    MultiColor,
    MultiColorPattern,
    SpeechLanguage,
    StatusFormat,
    TowerLight,
    TowerProtocol,
    VoiceGender,
)
from .options import SignalTowerOptions, SpeechOptions
from .response import ERROR_CODES, SignalTowerError, SignalTowerResponse
from .status import SignalTowerStatus

__version__ = "1.0.0"

__all__ = [
    "SignalTowerClient",
    "SignalTowerCommand",
    "SignalTowerOptions",
    "SpeechOptions",
    "SignalTowerResponse",
    "SignalTowerStatus",
    "SignalTowerError",
    "ERROR_CODES",
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
    "__version__",
]
