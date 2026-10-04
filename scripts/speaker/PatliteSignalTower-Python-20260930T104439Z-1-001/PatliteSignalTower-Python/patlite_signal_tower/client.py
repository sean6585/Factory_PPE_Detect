"""PATLITE 網路型信號燈的控制物件（手冊 5.3.13 HTTP Command Reception Function）。

只用標準函式庫（urllib），沒有第三方相依。

>>> tower = SignalTowerClient("192.168.10.1")
>>> tower.set_light(TowerLight.RED, LightPattern.ON)
>>> tower.announce("設備 A 加工完成", green=LightPattern.ON)
>>> status = tower.get_status()
"""

from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from .command import Parameters, SignalTowerCommand
from .enums import (
    BuzzerPattern,
    DigitalOutputState,
    LightPattern,
    MultiColor,
    MultiColorPattern,
    StatusFormat,
    TowerLight,
    TowerProtocol,
)
from .options import SignalTowerOptions, SpeechOptions
from .response import SignalTowerError, SignalTowerResponse
from .status import SignalTowerStatus

__all__ = ["SignalTowerClient"]

_NC = LightPattern.NO_CHANGE


class SignalTowerClient:
    """信號燈控制物件。"""

    def __init__(self, host_or_options="192.168.10.1", **kwargs):
        if isinstance(host_or_options, SignalTowerOptions):
            self.options = host_or_options
        else:
            self.options = SignalTowerOptions(host=host_or_options, **kwargs)

        #: 每次請求完成後呼叫（成功與失敗都會），適合接到記錄視窗
        self.request_completed: List[Callable[[SignalTowerResponse], None]] = []

    # ==========================================================
    # 燈號控制
    # ==========================================================
    def set_light(self, light: TowerLight, pattern: LightPattern) -> SignalTowerResponse:
        """控制單一顆燈，其餘維持現狀。"""
        return self.send_command(SignalTowerCommand().light(light, pattern))

    def set_led(
        self,
        red: LightPattern = _NC,
        amber: LightPattern = _NC,
        green: LightPattern = _NC,
        blue: LightPattern = _NC,
        white: LightPattern = _NC,
    ) -> SignalTowerResponse:
        """一次控制五顆燈（led 參數）。"""
        return self.send_command(SignalTowerCommand().led(red, amber, green, blue, white))

    def set_alert(
        self,
        red: LightPattern = _NC,
        amber: LightPattern = _NC,
        green: LightPattern = _NC,
        blue: LightPattern = _NC,
        white: LightPattern = _NC,
        buzzer: BuzzerPattern = BuzzerPattern.NO_CHANGE,
        restore_seconds: Optional[int] = None,
    ) -> SignalTowerResponse:
        """一次控制燈號與蜂鳴器（alert），可指定 restore 控制時間。"""
        return self.send_command(
            SignalTowerCommand().alert(red, amber, green, blue, white, buzzer, restore_seconds)
        )

    def set_buzzer(
        self, pattern: BuzzerPattern, restore_seconds: Optional[int] = None
    ) -> SignalTowerResponse:
        return self.send_command(SignalTowerCommand().buzzer(pattern, restore_seconds))

    def set_color(
        self,
        color: MultiColor,
        pattern: MultiColorPattern = MultiColorPattern.ON,
        buzzer: BuzzerPattern = BuzzerPattern.NO_CHANGE,
    ) -> SignalTowerResponse:
        return self.send_command(SignalTowerCommand().color(color, pattern, buzzer))

    def all_off(self) -> SignalTowerResponse:
        """關閉所有燈與蜂鳴器（alert=000000）。"""
        off = LightPattern.OFF
        return self.send_command(
            SignalTowerCommand().alert(off, off, off, off, off, BuzzerPattern.OFF)
        )

    def clear(self) -> SignalTowerResponse:
        """clear=1：回到正常運轉狀態。"""
        return self.send_command(SignalTowerCommand().clear())

    # ==========================================================
    # 數位輸出
    # ==========================================================
    def set_digital_output(
        self,
        do1: DigitalOutputState = DigitalOutputState.NO_CHANGE,
        do2: DigitalOutputState = DigitalOutputState.NO_CHANGE,
    ) -> SignalTowerResponse:
        return self.send_command(SignalTowerCommand().output(do1, do2))

    def set_line_out(self, on: bool) -> SignalTowerResponse:
        return self.send_command(SignalTowerCommand().line_out(on))

    # ==========================================================
    # 音源與語音
    # ==========================================================
    def play_sound(self, channel: int, repeat: Optional[int] = None) -> SignalTowerResponse:
        """播放內建音源 1 ~ 71 ch。repeat：0 = 單次，255 = 無限循環。"""
        return self.send_command(SignalTowerCommand().sound(channel, repeat))

    def stop_sound(self) -> SignalTowerResponse:
        """stop=1：停止音源播放或跳曲（依設備設定）。"""
        return self.send_command(SignalTowerCommand().stop())

    def speak(
        self,
        text: str,
        options: Optional[SpeechOptions] = None,
        repeat: Optional[int] = None,
    ) -> SignalTowerResponse:
        """文字轉語音播報，最多 400 字。"""
        return self.send_command(SignalTowerCommand().speech(text, options, repeat))

    def announce(
        self,
        text: str,
        red: LightPattern = _NC,
        amber: LightPattern = _NC,
        green: LightPattern = _NC,
        blue: LightPattern = _NC,
        white: LightPattern = _NC,
        options: Optional[SpeechOptions] = None,
        repeat: Optional[int] = None,
    ) -> SignalTowerResponse:
        """亮燈 + 播報一句話，一次請求送出（手冊 Point 明訂 led 可與 speech 併送）。"""
        return self.send_command(
            SignalTowerCommand().led(red, amber, green, blue, white).speech(text, options, repeat)
        )

    def announce_light(
        self,
        light: TowerLight,
        pattern: LightPattern,
        text: str,
        options: Optional[SpeechOptions] = None,
        repeat: Optional[int] = None,
    ) -> SignalTowerResponse:
        """亮一顆燈 + 播報一句話，一次請求送出。"""
        return self.send_command(
            SignalTowerCommand().light(light, pattern).speech(text, options, repeat)
        )

    def announce_sound(
        self,
        channel: int,
        red: LightPattern = _NC,
        amber: LightPattern = _NC,
        green: LightPattern = _NC,
        blue: LightPattern = _NC,
        white: LightPattern = _NC,
        repeat: Optional[int] = None,
    ) -> SignalTowerResponse:
        """亮燈 + 播放內建音源，一次請求送出。"""
        return self.send_command(
            SignalTowerCommand().led(red, amber, green, blue, white).sound(channel, repeat)
        )

    def alarm(
        self,
        text: str,
        buzzer: BuzzerPattern = BuzzerPattern.PATTERN1,
        red: LightPattern = _NC,
        amber: LightPattern = _NC,
        green: LightPattern = _NC,
        blue: LightPattern = _NC,
        white: LightPattern = _NC,
        options: Optional[SpeechOptions] = None,
        repeat: Optional[int] = None,
    ) -> List[SignalTowerResponse]:
        """蜂鳴 + 亮燈 + 播報。

        手冊只保證 led 可與 speech 併送，alert（含蜂鳴器）與 speech 的組合沒有背書，
        因此分成兩次請求：先 alert，再 speech。
        """
        return self.send_sequence(
            [
                SignalTowerCommand().alert(red, amber, green, blue, white, buzzer),
                SignalTowerCommand().speech(text, options, repeat),
            ]
        )

    # ==========================================================
    # 狀態
    # ==========================================================
    def get_status(self, fmt: StatusFormat = StatusFormat.JSON) -> SignalTowerStatus:
        """讀取設備狀態並解析；通訊失敗時丟出 SignalTowerError。"""
        response = self.send_raw("status", [("format", StatusFormat(fmt).value)])
        if response.transport_error is not None:
            raise SignalTowerError(f"讀取狀態失敗：{response.transport_error}", response)
        return SignalTowerStatus.parse(response.body, fmt)

    def try_get_status(
        self, fmt: StatusFormat = StatusFormat.JSON
    ) -> Tuple[bool, Optional[SignalTowerStatus], SignalTowerResponse]:
        """讀取狀態，失敗時回傳 (False, None, response) 而不丟例外（適合輪詢）。"""
        response = self.send_raw("status", [("format", StatusFormat(fmt).value)])
        if response.transport_error is not None:
            return False, None, response
        try:
            return True, SignalTowerStatus.parse(response.body, fmt), response
        except Exception:
            return False, None, response

    def test_connection(self) -> bool:
        return self.send_raw("status", [("format", "json")]).transport_error is None

    # ==========================================================
    # 低階 API
    # ==========================================================
    def send_command(self, command: SignalTowerCommand) -> SignalTowerResponse:
        """送出自行組裝的複合命令。"""
        params = command.to_parameters()
        if not params:
            raise ValueError("命令沒有任何參數，不需送出。")
        return self.send_raw("control", params)

    def send_sequence(
        self, commands: Iterable[SignalTowerCommand], stop_on_failure: bool = True
    ) -> List[SignalTowerResponse]:
        """依序送出多個命令，回傳每一次的結果。"""
        results: List[SignalTowerResponse] = []
        for command in commands:
            response = self.send_command(command)
            results.append(response)
            if stop_on_failure and not response.is_success:
                break
        return results

    def build_url(self, command: str, parameters: Sequence[Tuple[str, str]] = ()) -> str:
        """組出完整 URL（不送出），可用於預覽或除錯。"""
        host = (self.options.host or "").strip()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"  # IPv6

        port = self.options.effective_port
        scheme = self.options.scheme
        default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        authority = host if default_port else f"{host}:{port}"

        url = f"{scheme}://{authority}/api/{command}"
        if parameters:
            query = "&".join(
                f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(str(v), safe='')}"
                for k, v in parameters
            )
            url = f"{url}?{query}"
        return url

    def send_raw(self, command: str, parameters: Parameters) -> SignalTowerResponse:
        """組 URL 並送出。"""
        return self.send(self.build_url(command, parameters))

    def send(self, url: str) -> SignalTowerResponse:
        """直接送出指定的 URL（GET）。"""
        started = time.monotonic()
        timeout = max(0.5, self.options.timeout_ms / 1000.0)

        try:
            context = None
            if url.lower().startswith("https") and self.options.ignore_certificate_errors:
                context = ssl._create_unverified_context()  # noqa: S323 - 自簽憑證的現場設備

            with urllib.request.urlopen(url, timeout=timeout, context=context) as raw:
                body = raw.read().decode("utf-8", errors="replace")
                result = SignalTowerResponse(
                    url=url,
                    http_status=raw.status if hasattr(raw, "status") else raw.getcode(),
                    body=body,
                    elapsed_ms=_elapsed(started),
                )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            result = SignalTowerResponse(
                url=url, http_status=exc.code, body=body, elapsed_ms=_elapsed(started)
            )
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            message = f"連線逾時（>{self.options.timeout_ms} ms）" if _is_timeout(reason) else str(reason)
            result = SignalTowerResponse(url=url, transport_error=message, elapsed_ms=_elapsed(started))
        except (TimeoutError, OSError) as exc:
            message = (
                f"連線逾時（>{self.options.timeout_ms} ms）" if _is_timeout(exc) else str(exc)
            )
            result = SignalTowerResponse(url=url, transport_error=message, elapsed_ms=_elapsed(started))

        for callback in list(self.request_completed):
            try:
                callback(result)
            except Exception:
                pass  # 記錄用的回呼不應影響控制流程

        if self.options.raise_on_transport_error and result.transport_error is not None:
            raise SignalTowerError(f"通訊失敗：{result.transport_error}", result)
        if self.options.raise_on_device_error and result.error_code is not None:
            raise SignalTowerError(result.describe(), result)

        return result

    # ── 其他 ────────────────────────────────────────────────
    def __enter__(self) -> "SignalTowerClient":
        return self

    def __exit__(self, *exc_info) -> None:
        return None

    def __repr__(self) -> str:
        return (
            f"SignalTowerClient({self.options.scheme}://{self.options.host}"
            f":{self.options.effective_port})"
        )


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _is_timeout(error) -> bool:
    return isinstance(error, TimeoutError) or "timed out" in str(error).lower()
