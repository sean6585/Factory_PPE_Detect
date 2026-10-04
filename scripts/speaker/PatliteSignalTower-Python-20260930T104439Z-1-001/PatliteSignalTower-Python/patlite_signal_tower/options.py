"""連線設定與 speech 附加參數。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .enums import SpeechLanguage, TowerProtocol, VoiceGender

__all__ = ["SignalTowerOptions", "SpeechOptions"]


@dataclass
class SignalTowerOptions:
    """連線設定。可在執行期間直接修改，下一次請求即生效。"""

    host: str = "192.168.10.1"
    protocol: TowerProtocol = TowerProtocol.HTTP
    port: Optional[int] = None  # None = 依協定使用 80 / 443
    timeout_ms: int = 3000
    ignore_certificate_errors: bool = False
    raise_on_transport_error: bool = False
    raise_on_device_error: bool = False

    @property
    def scheme(self) -> str:
        return TowerProtocol(self.protocol).value

    @property
    def effective_port(self) -> int:
        if self.port is not None:
            return self.port
        return 443 if TowerProtocol(self.protocol) is TowerProtocol.HTTPS else 80


@dataclass
class SpeechOptions:
    """speech 命令的附加參數（lang / voice / speed / tone / notify / notifyTail）。

    未指定的項目不會送出，設備端預設為 jp / male / 0 / 0。
    """

    language: Optional[SpeechLanguage] = None
    voice: Optional[VoiceGender] = None
    speed: Optional[int] = None       # -5 ~ 5
    tone: Optional[int] = None        # -5 ~ 5
    notify: Optional[int] = None      # 0 ~ 10，播報前提示音
    notify_tail: Optional[int] = None  # 0 ~ 10，播報後提示音

    def validate(self) -> None:
        _check(self.speed, -5, 5, "speed")
        _check(self.tone, -5, 5, "tone")
        _check(self.notify, 0, 10, "notify")
        _check(self.notify_tail, 0, 10, "notify_tail")


def _check(value: Optional[int], low: int, high: int, name: str) -> None:
    if value is not None and not (low <= value <= high):
        raise ValueError(f"{name} 必須介於 {low} 至 {high}，目前為 {value}。")
