#!/usr/bin/env python3
"""PATLITE 信號燈互動式 console 範例。

用法：
    python console_demo.py                      # 使用預設位址 192.168.10.1
    python console_demo.py 192.168.10.5         # 指定位址
    python console_demo.py 10.0.0.8 --https --insecure
    python console_demo.py 10.0.0.8 -c "led red on" -c status   # 執行完即結束

進入互動模式後輸入 help 看指令清單，輸入 quit 離開。
"""

from __future__ import annotations

import argparse
import shlex
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

from patlite_signal_tower import (
    BuzzerPattern,
    DigitalOutputState,
    LightPattern,
    MultiColor,
    MultiColorPattern,
    SignalTowerClient,
    SignalTowerCommand,
    SignalTowerOptions,
    SpeechLanguage,
    SpeechOptions,
    StatusFormat,
    TowerLight,
    TowerProtocol,
    VoiceGender,
)

try:  # 讓方向鍵與歷史紀錄可用（Windows 上可能沒有）
    import readline  # noqa: F401
except ImportError:
    pass


# ──────────────────────────────────────────────────────────────
# 名稱對照
# ──────────────────────────────────────────────────────────────
LIGHTS: Dict[str, TowerLight] = {
    "red": TowerLight.RED, "r": TowerLight.RED, "紅": TowerLight.RED,
    "amber": TowerLight.AMBER, "yellow": TowerLight.AMBER, "y": TowerLight.AMBER, "黃": TowerLight.AMBER,
    "green": TowerLight.GREEN, "g": TowerLight.GREEN, "綠": TowerLight.GREEN,
    "blue": TowerLight.BLUE, "b": TowerLight.BLUE, "藍": TowerLight.BLUE,
    "white": TowerLight.WHITE, "w": TowerLight.WHITE, "c": TowerLight.WHITE, "白": TowerLight.WHITE,
}

PATTERNS: Dict[str, LightPattern] = {
    "off": LightPattern.OFF, "0": LightPattern.OFF, "滅": LightPattern.OFF,
    "on": LightPattern.ON, "1": LightPattern.ON, "亮": LightPattern.ON,
    "flash1": LightPattern.FLASH1, "f1": LightPattern.FLASH1, "2": LightPattern.FLASH1,
    "flash2": LightPattern.FLASH2, "f2": LightPattern.FLASH2, "3": LightPattern.FLASH2,
    "flash3": LightPattern.FLASH3, "f3": LightPattern.FLASH3, "4": LightPattern.FLASH3,
    "flash4": LightPattern.FLASH4, "f4": LightPattern.FLASH4, "5": LightPattern.FLASH4,
    "nc": LightPattern.NO_CHANGE, "9": LightPattern.NO_CHANGE, "keep": LightPattern.NO_CHANGE,
}

COLORS: Dict[str, MultiColor] = {c.name.lower(): c for c in MultiColor}
COLORS["none"] = MultiColor.NONE

LANGS: Dict[str, SpeechLanguage] = {
    "jp": SpeechLanguage.JAPANESE, "ja": SpeechLanguage.JAPANESE,
    "en": SpeechLanguage.ENGLISH,
    "cn": SpeechLanguage.CHINESE, "zh": SpeechLanguage.CHINESE,
}

HELP = """\
指令一覽（參數以空白分隔，含空白的文字請用 "..." 括起來）

  連線
    conn <host> [--https] [--port N] [--timeout MS] [--insecure]
    show                                目前連線設定
    dry [on|off]                        預覽模式：只印 URL 不送出
    test                                測試設備是否可連線

  燈號
    led <燈色> <樣式>                   例：led red on / led y flash1
    leds <5 碼>                         例：leds 10000（紅黃綠藍白）
    alert <6 碼> [--restore N]          例：alert 200001 --restore 5
    buzzer <0-5|off> [--restore N]
    color <顏色> [樣式] [--buzzer N]    例：color purple flash2
    off                                 全部關閉（alert=000000）
    clear                               clear=1 回復正常運轉

  輸出
    out <1|2> <on|off>                  數位輸出
    lineout <on|off>

  音源與語音
    sound <1-71> [--repeat N]
    stop                                停止播放 / 跳曲
    say <文字> [語音選項]
    announce <燈色> <樣式> <文字> [語音選項]     燈號與語音同一個請求送出
    alarm <燈色> <樣式> <文字> [--buzzer N] [語音選項]   蜂鳴+燈，再播報（兩次請求）

    語音選項：--lang jp|en|cn  --voice male|female  --speed -5~5
              --tone -5~5  --notify 0-10  --tail 0-10  --repeat 0-255

  狀態
    status [json|xml] [--raw]
    watch [秒]                          持續輪詢，Ctrl+C 結束

  其他
    raw <命令> <key=value> ...          直接送任意參數，例：raw control led=10000
    help / quit
"""

BANNER = "PATLITE 信號燈控制台　輸入 help 看指令，quit 離開"


# ──────────────────────────────────────────────────────────────
# 小工具
# ──────────────────────────────────────────────────────────────
class CommandError(Exception):
    """使用者輸入的指令有問題。"""


def split_options(tokens: Sequence[str]) -> Tuple[List[str], Dict[str, str]]:
    """把 --key value 拆出來，其餘視為位置參數。"""
    positional: List[str] = []
    options: Dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--"):
            key = token[2:].lower()
            if "=" in key:
                key, _, value = key.partition("=")
                options[key] = value
            elif index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
                index += 1
                options[key] = tokens[index]
            else:
                options[key] = "on"  # 旗標
            index += 1
            continue
        positional.append(token)
        index += 1
    return positional, options


def to_light(text: str) -> TowerLight:
    light = LIGHTS.get(text.lower())
    if light is None:
        raise CommandError(f"未知的燈色：{text}（red / amber / green / blue / white）")
    return light


def to_pattern(text: str) -> LightPattern:
    pattern = PATTERNS.get(text.lower())
    if pattern is None:
        raise CommandError(f"未知的樣式：{text}（off / on / flash1~flash4 / nc）")
    return pattern


def to_int(options: Dict[str, str], key: str) -> Optional[int]:
    if key not in options:
        return None
    try:
        return int(options[key])
    except ValueError:
        raise CommandError(f"--{key} 需要數字，收到 {options[key]}")


def to_bool(text: str) -> bool:
    lowered = text.lower()
    if lowered in ("on", "1", "true", "open", "開"):
        return True
    if lowered in ("off", "0", "false", "close", "關"):
        return False
    raise CommandError(f"請輸入 on 或 off，收到 {text}")


def build_speech_options(options: Dict[str, str]) -> SpeechOptions:
    speech = SpeechOptions(
        speed=to_int(options, "speed"),
        tone=to_int(options, "tone"),
        notify=to_int(options, "notify"),
        notify_tail=to_int(options, "tail"),
    )
    if "lang" in options:
        language = LANGS.get(options["lang"].lower())
        if language is None:
            raise CommandError(f"未知的語言：{options['lang']}（jp / en / cn）")
        speech.language = language
    if "voice" in options:
        voice = options["voice"].lower()
        if voice not in ("male", "female"):
            raise CommandError("--voice 只能是 male 或 female")
        speech.voice = VoiceGender(voice)
    return speech


def digits(text: str, length: int, name: str) -> str:
    if len(text) != length or not text.isdigit():
        raise CommandError(f"{name} 需要 {length} 位數字，例如 {'1' + '0' * (length - 1)}")
    return text


# ──────────────────────────────────────────────────────────────
# 主控台
# ──────────────────────────────────────────────────────────────
class Console:
    def __init__(self, client: SignalTowerClient):
        self.client = client
        self.dry_run = False
        self.running = True

    # ── 送出 ────────────────────────────────────────────────
    def run_command(self, command: SignalTowerCommand) -> None:
        self.run_raw("control", command.to_parameters())

    def run_raw(self, api: str, parameters) -> None:
        url = self.client.build_url(api, parameters)
        print(f"  → {url}")
        if self.dry_run:
            print("  （預覽模式，未實際送出）")
            return
        response = self.client.send(url)
        print(f"  ← {response.describe()}  ({response.elapsed_ms} ms)")
        body = response.body.strip()
        if body and not response.is_success:
            print(f"     {body[:200]}")

    # ── 指令 ────────────────────────────────────────────────
    def cmd_help(self, args, options):
        print(HELP)

    def cmd_quit(self, args, options):
        self.running = False

    def cmd_conn(self, args, options):
        if args:
            self.client.options.host = args[0]
        if "https" in options:
            self.client.options.protocol = TowerProtocol.HTTPS
        if "http" in options:
            self.client.options.protocol = TowerProtocol.HTTP
        port = to_int(options, "port")
        if port is not None:
            self.client.options.port = port
        timeout = to_int(options, "timeout")
        if timeout is not None:
            self.client.options.timeout_ms = timeout
        if "insecure" in options:
            self.client.options.ignore_certificate_errors = True
        self.cmd_show(args, options)

    def cmd_show(self, args, options):
        opts = self.client.options
        print(f"  位址     : {opts.scheme}://{opts.host}:{opts.effective_port}")
        print(f"  逾時     : {opts.timeout_ms} ms")
        print(f"  忽略憑證 : {'是' if opts.ignore_certificate_errors else '否'}")
        print(f"  預覽模式 : {'開啟' if self.dry_run else '關閉'}")

    def cmd_dry(self, args, options):
        self.dry_run = to_bool(args[0]) if args else not self.dry_run
        print(f"  預覽模式 {'開啟' if self.dry_run else '關閉'}")

    def cmd_test(self, args, options):
        print("  可連線" if self.client.test_connection() else "  無法連線")

    # 燈號
    def cmd_led(self, args, options):
        if len(args) < 2:
            raise CommandError("用法：led <燈色> <樣式>")
        self.run_command(SignalTowerCommand().light(to_light(args[0]), to_pattern(args[1])))

    def cmd_leds(self, args, options):
        if not args:
            raise CommandError("用法：leds <5 碼>，例如 leds 10000")
        value = digits(args[0], 5, "leds")
        self.run_command(SignalTowerCommand().led(*[LightPattern(int(d)) for d in value]))

    def cmd_alert(self, args, options):
        if not args:
            raise CommandError("用法：alert <6 碼> [--restore N]")
        value = digits(args[0], 6, "alert")
        lights = [LightPattern(int(d)) for d in value[:5]]
        command = SignalTowerCommand().alert(
            *lights, buzzer=BuzzerPattern(int(value[5])), restore_seconds=to_int(options, "restore")
        )
        self.run_command(command)

    def cmd_buzzer(self, args, options):
        if not args:
            raise CommandError("用法：buzzer <0-5|off> [--restore N]")
        text = args[0].lower()
        pattern = BuzzerPattern.OFF if text == "off" else BuzzerPattern(int(text))
        self.run_command(SignalTowerCommand().buzzer(pattern, to_int(options, "restore")))

    def cmd_color(self, args, options):
        if not args:
            raise CommandError("用法：color <顏色> [樣式] [--buzzer N]")
        color = COLORS.get(args[0].lower())
        if color is None:
            raise CommandError(f"未知的顏色：{args[0]}（{', '.join(COLORS)}）")
        pattern = MultiColorPattern.ON
        if len(args) > 1:
            pattern = MultiColorPattern(int(to_pattern(args[1])))
        buzzer = to_int(options, "buzzer")
        self.run_command(
            SignalTowerCommand().color(
                color, pattern, BuzzerPattern(buzzer) if buzzer is not None else BuzzerPattern.NO_CHANGE
            )
        )

    def cmd_off(self, args, options):
        off = LightPattern.OFF
        self.run_command(SignalTowerCommand().alert(off, off, off, off, off, BuzzerPattern.OFF))

    def cmd_clear(self, args, options):
        self.run_command(SignalTowerCommand().clear())

    # 輸出
    def cmd_out(self, args, options):
        if len(args) < 2:
            raise CommandError("用法：out <1|2> <on|off>")
        state = DigitalOutputState.ON if to_bool(args[1]) else DigitalOutputState.OFF
        if args[0] == "1":
            self.run_command(SignalTowerCommand().output(do1=state))
        elif args[0] == "2":
            self.run_command(SignalTowerCommand().output(do2=state))
        else:
            raise CommandError("數位輸出只有 1 與 2")

    def cmd_lineout(self, args, options):
        if not args:
            raise CommandError("用法：lineout <on|off>")
        self.run_command(SignalTowerCommand().line_out(to_bool(args[0])))

    # 音源與語音
    def cmd_sound(self, args, options):
        if not args:
            raise CommandError("用法：sound <1-71> [--repeat N]")
        self.run_command(SignalTowerCommand().sound(int(args[0]), to_int(options, "repeat")))

    def cmd_stop(self, args, options):
        self.run_command(SignalTowerCommand().stop())

    def cmd_say(self, args, options):
        if not args:
            raise CommandError('用法：say "要播報的文字" [--lang cn --voice female ...]')
        text = " ".join(args)
        self.run_command(
            SignalTowerCommand().speech(text, build_speech_options(options), to_int(options, "repeat"))
        )

    def cmd_announce(self, args, options):
        if len(args) < 3:
            raise CommandError('用法：announce <燈色> <樣式> "要播報的文字"')
        light, pattern, text = to_light(args[0]), to_pattern(args[1]), " ".join(args[2:])
        self.run_command(
            SignalTowerCommand()
            .light(light, pattern)
            .speech(text, build_speech_options(options), to_int(options, "repeat"))
        )

    def cmd_alarm(self, args, options):
        if len(args) < 3:
            raise CommandError('用法：alarm <燈色> <樣式> "要播報的文字" [--buzzer N]')
        light, pattern, text = to_light(args[0]), to_pattern(args[1]), " ".join(args[2:])
        buzzer = to_int(options, "buzzer")
        lights = [LightPattern.NO_CHANGE] * 5
        lights[int(light)] = pattern

        # alert（含蜂鳴器）與 speech 分兩次送：手冊只保證 led 可與 speech 併送
        self.run_command(
            SignalTowerCommand().alert(
                *lights, buzzer=BuzzerPattern(buzzer) if buzzer is not None else BuzzerPattern.PATTERN1
            )
        )
        self.run_command(
            SignalTowerCommand().speech(text, build_speech_options(options), to_int(options, "repeat"))
        )

    # 狀態
    def cmd_status(self, args, options):
        fmt = StatusFormat.XML if args and args[0].lower() == "xml" else StatusFormat.JSON
        url = self.client.build_url("status", [("format", fmt.value)])
        print(f"  → {url}")
        if self.dry_run:
            print("  （預覽模式，未實際送出）")
            return

        ok, status, response = self.client.try_get_status(fmt)
        if not ok or status is None:
            print(f"  ← 讀取失敗：{response.transport_error or '回應內容無法解析'}")
            if response.body.strip():
                print(f"     {response.body.strip()[:200]}")
            return

        print(f"  ← 完成（{response.elapsed_ms} ms）")
        print()
        print(status.pretty_raw() if "raw" in options else status.describe())
        print()

    def cmd_watch(self, args, options):
        interval = float(args[0]) if args else 1.0
        print(f"  每 {interval} 秒輪詢一次，Ctrl+C 結束")
        try:
            while True:
                ok, status, response = self.client.try_get_status()
                stamp = time.strftime("%H:%M:%S")
                if ok and status is not None:
                    lights = " ".join(
                        f"{name}={'-' if pattern is None else int(pattern)}"
                        for name, pattern in zip("RYGBC", status.lights)
                    )
                    buzzer = "-" if status.buzzer is None else int(status.buzzer)
                    sound = "-" if status.sound_channel is None else status.sound_channel
                    print(f"  [{stamp}] {lights}  BZ={buzzer}  SOUND={sound}")
                else:
                    print(f"  [{stamp}] 讀取失敗：{response.transport_error or '回應無法解析'}")
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n  停止輪詢")

    def cmd_raw(self, args, options):
        if not args:
            raise CommandError("用法：raw <命令> <key=value> ...，例：raw control led=10000")
        parameters = []
        for token in args[1:]:
            key, sep, value = token.partition("=")
            if not sep:
                raise CommandError(f"參數需為 key=value 格式：{token}")
            parameters.append((key, value))
        self.run_raw(args[0], parameters)

    # ── 分派 ────────────────────────────────────────────────
    def handlers(self):
        return {
            "help": self.cmd_help, "?": self.cmd_help,
            "quit": self.cmd_quit, "exit": self.cmd_quit, "q": self.cmd_quit,
            "conn": self.cmd_conn, "connect": self.cmd_conn,
            "show": self.cmd_show, "dry": self.cmd_dry, "test": self.cmd_test,
            "led": self.cmd_led, "leds": self.cmd_leds, "alert": self.cmd_alert,
            "buzzer": self.cmd_buzzer, "bz": self.cmd_buzzer, "color": self.cmd_color,
            "off": self.cmd_off, "clear": self.cmd_clear,
            "out": self.cmd_out, "lineout": self.cmd_lineout,
            "sound": self.cmd_sound, "stop": self.cmd_stop,
            "say": self.cmd_say, "speech": self.cmd_say,
            "announce": self.cmd_announce, "alarm": self.cmd_alarm,
            "status": self.cmd_status, "watch": self.cmd_watch, "raw": self.cmd_raw,
        }

    def execute(self, line: str) -> None:
        line = line.strip()
        if not line or line.startswith("#"):
            return
        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            print(f"  指令解析失敗：{exc}")
            return

        handler = self.handlers().get(tokens[0].lower())
        if handler is None:
            print(f"  未知的指令：{tokens[0]}（輸入 help 看清單）")
            return

        args, options = split_options(tokens[1:])
        try:
            handler(args, options)
        except CommandError as exc:
            print(f"  {exc}")
        except ValueError as exc:  # 來自程式庫的參數驗證
            print(f"  參數錯誤：{exc}")
        except Exception as exc:  # noqa: BLE001 - console 不該因單一指令中斷
            print(f"  執行失敗：{type(exc).__name__}: {exc}")

    def loop(self) -> None:
        print(BANNER)
        self.cmd_show([], {})
        while self.running:
            try:
                line = input(f"\npatlite({self.client.options.host})> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            self.execute(line)
        print("結束")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="PATLITE 信號燈互動式控制台")
    parser.add_argument("host", nargs="?", default="192.168.10.1", help="設備位址")
    parser.add_argument("--https", action="store_true", help="使用 HTTPS")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=3000, help="逾時（ms）")
    parser.add_argument("--insecure", action="store_true", help="HTTPS 忽略憑證錯誤")
    parser.add_argument("--dry", action="store_true", help="預覽模式：只印 URL 不送出")
    parser.add_argument("-c", "--command", action="append", default=[],
                        help="直接執行指令後結束，可重複指定")
    parsed = parser.parse_args(argv)

    client = SignalTowerClient(
        SignalTowerOptions(
            host=parsed.host,
            protocol=TowerProtocol.HTTPS if parsed.https else TowerProtocol.HTTP,
            port=parsed.port,
            timeout_ms=parsed.timeout,
            ignore_certificate_errors=parsed.insecure,
        )
    )

    console = Console(client)
    console.dry_run = parsed.dry

    if parsed.command:
        for line in parsed.command:
            print(f"> {line}")
            console.execute(line)
        return 0

    console.loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
