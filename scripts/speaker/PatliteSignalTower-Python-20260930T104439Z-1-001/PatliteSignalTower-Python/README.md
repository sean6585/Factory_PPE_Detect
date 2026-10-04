# PATLITE 網路型信號燈 HTTP 控制程式庫（Python 版）

C# 版 `Patlite.SignalTower` 的 Python 對應實作，依《Network Signal Tower Control /
Network Signal Tower with Voice Annunciator Control》手冊
**5.3.13 HTTP Command Reception Function**（p.54–59）撰寫。

- **只用標準函式庫**（`urllib`、`json`、`xml.etree`），不需 pip install 任何套件
- Python 3.8+
- 附互動式 console 範例與一支信號燈模擬器，沒有實機也能測

```
patlite_signal_tower/      程式庫
  ├─ client.py             SignalTowerClient：控制物件
  ├─ command.py            SignalTowerCommand：複合命令組裝器
  ├─ enums.py              LightPattern / BuzzerPattern / MultiColor…（數值即手冊參數值）
  ├─ options.py            SignalTowerOptions、SpeechOptions
  ├─ response.py           SignalTowerResponse、錯誤碼、SignalTowerError
  └─ status.py             SignalTowerStatus：status 回應解析與描述
console_demo.py            互動式 console 範例
tools/fake_tower.py        信號燈模擬器（測試用）
```

---

## 快速開始

```bash
# 視窗 1：啟動模擬器（有實機的話跳過）
python tools/fake_tower.py

# 視窗 2：開啟控制台
python console_demo.py 127.0.0.1 --port 8123
```

## 程式庫用法

```python
from patlite_signal_tower import *

tower = SignalTowerClient("192.168.10.1")

tower.set_light(TowerLight.RED, LightPattern.ON)                  # 紅燈恆亮
tower.set_led(red=LightPattern.OFF, amber=LightPattern.FLASH1)
tower.set_alert(red=LightPattern.FLASH1, buzzer=BuzzerPattern.PATTERN1, restore_seconds=5)
tower.set_buzzer(BuzzerPattern.PATTERN2)
tower.set_color(MultiColor.PURPLE, MultiColorPattern.FLASH2)
tower.set_digital_output(do1=DigitalOutputState.ON)
tower.play_sound(5, repeat=2)
tower.all_off()
tower.clear()
```

未指定的燈預設是 `LightPattern.NO_CHANGE`（參數值 9，維持現狀），
所以只想動一顆燈時不必把其他四顆的狀態帶進來。

### 亮燈 + 播報一句話

手冊 Point 明訂 `led` 可與 `speech` / `sound` 併送，所以燈號與語音是**同一個請求**：

```python
tower.announce("設備 A 加工完成", green=LightPattern.ON)

tower.announce_light(
    TowerLight.RED, LightPattern.FLASH1,
    "異常停機，請至現場確認",
    SpeechOptions(language=SpeechLanguage.CHINESE, voice=VoiceGender.FEMALE, notify=6),
    repeat=2,
)

tower.announce_sound(channel=5, amber=LightPattern.FLASH1)
```

蜂鳴器要和語音搭配時，手冊沒有保證 `alert` 可與 `speech` 併送，因此分兩次請求：

```python
results = tower.alarm("異常停機", buzzer=BuzzerPattern.PATTERN1, red=LightPattern.FLASH1)
```

任何多步驟流程都能用 `send_sequence`，預設其中一步失敗就停止：

```python
tower.send_sequence([
    SignalTowerCommand().alert(red=LightPattern.FLASH1, buzzer=BuzzerPattern.PATTERN2),
    SignalTowerCommand().speech("請確認第 3 號治具"),
    SignalTowerCommand().clear(),
])
```

### 複合命令

```python
command = (SignalTowerCommand()
           .light(TowerLight.RED, LightPattern.FLASH1)
           .speech("異常停機", SpeechOptions(language=SpeechLanguage.CHINESE))
           .repeat(3))

print(command)            # led=20000&speech=異常停機&lang=cn&repeat=3
tower.send_command(command)
```

`to_parameters()` 會做手冊規定的檢查：`restore` 沒有搭配 `alert`、音源通道超出
1–71、`repeat` 超出 0–255 都會丟 `ValueError`；`speech` 超過 400 字自動截斷。

### 讀取狀態

```python
status = tower.get_status()                 # 預設 json，也可傳 StatusFormat.XML

if status.red is LightPattern.ON: ...
if status.buzzer_active: ...

print(status.sound_channel, status.software_version, status.digital_inputs)
print(status.describe())                    # 人可讀的完整描述
print(status.pretty_raw())                  # 排版後的原始回應

ok, snapshot, response = tower.try_get_status()   # 輪詢用，失敗不丟例外
```

### 錯誤處理

預設不丟例外，由回傳值判斷：

```python
response = tower.set_light(TowerLight.GREEN, LightPattern.ON)

if response.transport_error:   pass   # 逾時或無法連線
elif response.error_code:      pass   # 002~005，見 ERROR_CODES
elif response.is_success:      pass   # 設備回應 Success.

print(response.describe())
response.raise_for_status()            # 或改成失敗即丟例外
```

也可以在 options 打開自動丟例外，並掛上記錄回呼：

```python
tower = SignalTowerClient(SignalTowerOptions(
    host="tower-01.factory.local",
    protocol=TowerProtocol.HTTPS,
    ignore_certificate_errors=True,     # 自簽憑證
    timeout_ms=2000,
    raise_on_transport_error=True,
    raise_on_device_error=True,
))

tower.request_completed.append(lambda r: logging.info("%s → %s", r.url, r.describe()))
```

---

## 互動式 console

```
$ python console_demo.py 192.168.10.1
PATLITE 信號燈控制台　輸入 help 看指令，quit 離開
  位址     : http://192.168.10.1:80
  逾時     : 3000 ms

patlite(192.168.10.1)> led red on
  → http://192.168.10.1/api/control?led=19999
  ← 成功 (Success.)  (12 ms)

patlite(192.168.10.1)> announce green on "設備 A 加工完成" --lang cn --voice female
  → http://192.168.10.1/api/control?led=99199&speech=...&lang=cn&voice=female
  ← 成功 (Success.)  (18 ms)
```

主要指令（完整清單輸入 `help`）：

| 指令 | 說明 |
|---|---|
| `conn <host> [--https] [--port N] [--timeout MS] [--insecure]` | 改連線設定 |
| `dry [on\|off]` | 預覽模式，只印 URL 不送出 |
| `led <燈色> <樣式>` | 例：`led red on`、`led y flash1` |
| `leds <5 碼>` / `alert <6 碼> [--restore N]` | 直接給參數碼 |
| `buzzer <0-5>` / `color <顏色> [樣式]` | 蜂鳴器、多色燈 |
| `out <1\|2> <on\|off>` / `lineout <on\|off>` | 數位輸出、Line-out |
| `sound <1-71> [--repeat N]` / `stop` | 內建音源 |
| `say <文字> [語音選項]` | 文字播報 |
| `announce <燈色> <樣式> <文字> [語音選項]` | **亮燈 + 播報，同一個請求** |
| `alarm <燈色> <樣式> <文字> [--buzzer N]` | 蜂鳴+燈，再播報（兩次請求） |
| `status [json\|xml] [--raw]` / `watch [秒]` | 讀取狀態、持續輪詢 |
| `raw <命令> <key=value> ...` | 直接送任意參數 |
| `off` / `clear` / `test` / `show` / `help` / `quit` | 其他 |

語音選項：`--lang jp|en|cn`、`--voice male|female`、`--speed -5~5`、`--tone -5~5`、
`--notify 0-10`、`--tail 0-10`、`--repeat 0-255`。含空白的文字請用 `"..."` 括起來。

非互動用法（跑完即結束，適合寫進腳本）：

```bash
python console_demo.py 192.168.10.1 -c "led red on" -c "status"
python console_demo.py 192.168.10.1 --dry -c 'announce red flash1 "測試"'   # 只印 URL
```

## 模擬器

`tools/fake_tower.py` 是一支極簡的信號燈模擬器：回應 `Success.` / `Error.<code>`、
維護一份狀態供 `status` 查詢、把 `speech` 內容印在畫面上。
它只模擬基本行為，`restore` 倒數與音源播放時間並未實作。

```bash
python tools/fake_tower.py              # 127.0.0.1:8123
python tools/fake_tower.py 0.0.0.0 80   # 指定位址與埠號
```

## 已知限制

手冊只列出 status「可取得的資料」清單，未提供實際的 XML/JSON 封裝範例，
因此解析採寬鬆策略：JSON 會攤平巢狀物件，`Unit_Status` 為陣列或單一數值皆可；
XML 則讀所有葉節點與其 `name` 屬性。沒對應到具名屬性的欄位一律保留在
`SignalTowerStatus.fields`，實機欄位命名不同時調整 `status.py` 的
`_parse_json` / `_parse_xml` 即可。
