"""control 命令的組裝器。"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .enums import (
    BuzzerPattern,
    DigitalOutputState,
    LightPattern,
    MultiColor,
    MultiColorPattern,
    TowerLight,
)
from .options import SpeechOptions

__all__ = ["SignalTowerCommand"]

Parameters = List[Tuple[str, str]]


class SignalTowerCommand:
    """可串接的命令組裝器。

    >>> cmd = (SignalTowerCommand()
    ...        .light(TowerLight.RED, LightPattern.FLASH1)
    ...        .speech("異常停機", SpeechOptions(language=SpeechLanguage.CHINESE))
    ...        .repeat(2))
    >>> str(cmd)
    'led=20000&speech=異常停機&lang=cn&repeat=2'
    """

    def __init__(self) -> None:
        self._alert_lights: Optional[List[LightPattern]] = None
        self._alert_buzzer: BuzzerPattern = BuzzerPattern.NO_CHANGE
        self._restore: Optional[int] = None

        self._led: Optional[List[LightPattern]] = None

        self._do1: Optional[DigitalOutputState] = None
        self._do2: Optional[DigitalOutputState] = None

        self._color: Optional[MultiColor] = None
        self._color_pattern: MultiColorPattern = MultiColorPattern.ON
        self._color_buzzer: BuzzerPattern = BuzzerPattern.NO_CHANGE

        self._sound: Optional[int] = None
        self._repeat: Optional[int] = None
        self._speech: Optional[str] = None
        self._speech_options: Optional[SpeechOptions] = None

        self._line_out: Optional[bool] = None
        self._stop = False
        self._clear = False

    # ── 燈號 ────────────────────────────────────────────────
    def alert(
        self,
        red: LightPattern = LightPattern.NO_CHANGE,
        amber: LightPattern = LightPattern.NO_CHANGE,
        green: LightPattern = LightPattern.NO_CHANGE,
        blue: LightPattern = LightPattern.NO_CHANGE,
        white: LightPattern = LightPattern.NO_CHANGE,
        buzzer: BuzzerPattern = BuzzerPattern.NO_CHANGE,
        restore_seconds: Optional[int] = None,
    ) -> "SignalTowerCommand":
        """alert：一次控制五顆燈與蜂鳴器。restore 只能與 alert 同時指定。"""
        self._alert_lights = [red, amber, green, blue, white]
        self._alert_buzzer = buzzer
        if restore_seconds is not None:
            self._set_restore(restore_seconds)
        return self

    def led(
        self,
        red: LightPattern = LightPattern.NO_CHANGE,
        amber: LightPattern = LightPattern.NO_CHANGE,
        green: LightPattern = LightPattern.NO_CHANGE,
        blue: LightPattern = LightPattern.NO_CHANGE,
        white: LightPattern = LightPattern.NO_CHANGE,
    ) -> "SignalTowerCommand":
        """led：控制五顆燈，未指定者維持現狀。"""
        self._led = [red, amber, green, blue, white]
        return self

    def light(self, light: TowerLight, pattern: LightPattern) -> "SignalTowerCommand":
        """只控制單一顆燈，可連續呼叫累加。"""
        if self._led is None:
            self._led = [LightPattern.NO_CHANGE] * 5
        self._led[int(light)] = pattern
        return self

    def buzzer(
        self, pattern: BuzzerPattern, restore_seconds: Optional[int] = None
    ) -> "SignalTowerCommand":
        """只控制蜂鳴器（透過 alert，燈號全部維持現狀）。"""
        if self._alert_lights is None:
            self._alert_lights = [LightPattern.NO_CHANGE] * 5
        self._alert_buzzer = pattern
        if restore_seconds is not None:
            self._set_restore(restore_seconds)
        return self

    def color(
        self,
        color: MultiColor,
        pattern: MultiColorPattern = MultiColorPattern.ON,
        buzzer: BuzzerPattern = BuzzerPattern.NO_CHANGE,
    ) -> "SignalTowerCommand":
        """color：多色燈，附帶 c-pat 與 b-pat。"""
        self._color = color
        self._color_pattern = pattern
        self._color_buzzer = buzzer
        return self

    # ── 輸出 ────────────────────────────────────────────────
    def output(
        self,
        do1: DigitalOutputState = DigitalOutputState.NO_CHANGE,
        do2: DigitalOutputState = DigitalOutputState.NO_CHANGE,
    ) -> "SignalTowerCommand":
        self._do1 = do1
        self._do2 = do2
        return self

    def line_out(self, on: bool) -> "SignalTowerCommand":
        self._line_out = on
        return self

    # ── 音源與語音 ──────────────────────────────────────────
    def sound(self, channel: int, repeat: Optional[int] = None) -> "SignalTowerCommand":
        if not 1 <= channel <= 71:
            raise ValueError(f"音源通道必須介於 1 至 71，目前為 {channel}。")
        self._sound = channel
        if repeat is not None:
            self.repeat(repeat)
        return self

    def speech(
        self,
        text: str,
        options: Optional[SpeechOptions] = None,
        repeat: Optional[int] = None,
    ) -> "SignalTowerCommand":
        """speech：文字轉語音，超過 400 字自動截斷。"""
        if not text:
            raise ValueError("speech 內容不可為空。")
        if options is not None:
            options.validate()
        self._speech = text[:400]
        self._speech_options = options
        if repeat is not None:
            self.repeat(repeat)
        return self

    def repeat(self, times: int) -> "SignalTowerCommand":
        """sound 與 speech 共用的重複次數。0 = 單次，255 = 無限循環。"""
        if not 0 <= times <= 255:
            raise ValueError(f"repeat 必須介於 0 至 255，目前為 {times}。")
        self._repeat = times
        return self

    # ── 其他 ────────────────────────────────────────────────
    def stop(self) -> "SignalTowerCommand":
        """stop=1：停止音源播放或跳曲（依設備設定）。"""
        self._stop = True
        return self

    def clear(self) -> "SignalTowerCommand":
        """clear=1：回到正常運轉狀態。"""
        self._clear = True
        return self

    # ── 輸出參數 ────────────────────────────────────────────
    def to_parameters(self) -> Parameters:
        """轉成實際要送出的參數清單，同時做手冊規定的檢查。"""
        if self._restore is not None and self._alert_lights is None:
            raise ValueError("restore 只能與 alert 同時指定（手冊 5.3.13 CAUTION）。")

        params: Parameters = []

        if self._alert_lights is not None:
            digits = "".join(str(int(p)) for p in self._alert_lights)
            params.append(("alert", digits + str(int(self._alert_buzzer))))
            if self._restore is not None:
                params.append(("restore", str(self._restore)))

        if self._led is not None:
            params.append(("led", "".join(str(int(p)) for p in self._led)))

        if self._do1 is not None or self._do2 is not None:
            do1 = self._do1 if self._do1 is not None else DigitalOutputState.NO_CHANGE
            do2 = self._do2 if self._do2 is not None else DigitalOutputState.NO_CHANGE
            params.append(("output", f"{int(do1)}{int(do2)}"))

        if self._color is not None:
            params.append(("color", MultiColor(self._color).to_parameter()))
            params.append(("c-pat", str(int(self._color_pattern))))
            params.append(("b-pat", str(int(self._color_buzzer))))

        if self._sound is not None:
            params.append(("sound", str(self._sound)))

        if self._speech is not None:
            params.append(("speech", self._speech))
            opts = self._speech_options
            if opts is not None:
                if opts.language is not None:
                    params.append(("lang", opts.language.value))
                if opts.voice is not None:
                    params.append(("voice", opts.voice.value))
                if opts.speed is not None:
                    params.append(("speed", str(opts.speed)))
                if opts.tone is not None:
                    params.append(("tone", str(opts.tone)))
                if opts.notify:
                    params.append(("notify", str(opts.notify)))
                if opts.notify_tail:
                    params.append(("notifyTail", str(opts.notify_tail)))

        if self._repeat is not None and (self._sound is not None or self._speech is not None):
            params.append(("repeat", str(self._repeat)))

        if self._line_out is not None:
            params.append(("lineout", "1" if self._line_out else "0"))
        if self._stop:
            params.append(("stop", "1"))
        if self._clear:
            params.append(("clear", "1"))

        return params

    def is_empty(self) -> bool:
        return not self.to_parameters()

    def _set_restore(self, seconds: int) -> None:
        if not 1 <= seconds <= 99:
            raise ValueError(f"restore 必須介於 1 至 99 秒，目前為 {seconds}。")
        self._restore = seconds

    def __str__(self) -> str:
        return "&".join(f"{k}={v}" for k, v in self.to_parameters())

    def __repr__(self) -> str:
        return f"SignalTowerCommand({self})"
