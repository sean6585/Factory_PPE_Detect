# mpk_rfid — MPK-R-9504 RFID 讀取(精簡版)

單一檔案 `mpk_rfid.py`,零第三方相依,只做「讀取 RFID tag」。
依照 MPK-R-9504 Protocol User Guide v1.0 實作,TCP 連線。

## 檔案

| 檔案 | 說明 |
|---|---|
| **`mpk_rfid.py`** | 函式庫,全部功能都在這裡。複製這一個檔案到你的專案就能用。 |
| **`rfid_console.py`** | 互動式測試軟體(S 開始 / C 清除 / E 停止 / G 查 GPIO)。 |
| `simulator.py` | 假讀取器(沒有實機時測試用)。不需要就刪掉,`mpk_rfid.py` 不依賴它。 |
| `test_mpk_rfid.py` | 函式庫測試(42 項)。 |
| `test_rfid_console.py` | 測試軟體測試(11 項,重點在自動重啟與 GPIO)。 |

## 互動式測試軟體

```bash
python rfid_console.py --host 192.168.1.200 --port 8888
python rfid_console.py --sim                    # 沒有實機時先試跑
```

熱鍵**直接按,不用按 Enter**:

| 鍵 | 動作 |
|---|---|
| **S** | 開始持續讀取 |
| **C** | 清除目前 buffer 中的 RFID 資料 |
| **E** | 停止讀取 |
| **G** | 立刻查一次 GPIO input |
| **Q** | 離開(Esc 或 Ctrl-C 也可以) |

畫面每 0.25 秒原地更新一次(不會一直往下捲):

```
====================================================================================
 MPK-R-9504 RFID 讀取測試                            192.168.1.200:8888  [已連線]
====================================================================================
 狀態: ● 讀取中       經過:   12.4 s
 唯一 Tag: 5      總讀取: 3,485      速率:    281 /s    封包錯誤: 0
 GPIO: IN1 ● HIGH    IN2 ○ LOW     IN3 ○ LOW      (更新於 0.3s 前)
------------------------------------------------------------------------------------
   # EPC                                        次數    RSSI     Max ANT    最後
   1 E28011700000020F00005678                    748   -43.0   -38.1   1    0.0s
   2 E28011700000020F00001234                    745   -41.3   -39.4   1    0.0s
   3 E28011700000020F0000ABCD                    701   -68.4   -38.3   1    0.0s
------------------------------------------------------------------------------------
 10:45:29  韌體版本: MPK-R-9504 v1.0.3
 10:45:29  Inventory 開始 (ANT=1, RFMode=103, 20.0 dBm, 連續模式)
====================================================================================
 [S] 開始讀取   [C] 清除 buffer   [E] 停止讀取   [G] 查詢 GPIO   [Q] 離開
```

其他參數:`--antenna 1`、`--power 20`、`--rf-mode 103`。

自動化/展示可以用腳本自動按鍵:

```bash
python rfid_console.py --sim --script "s:3,c,s:2,e:1,q"
```
（`鍵:秒數` 表示按下該鍵後等幾秒再按下一個。）

**視窗越高顯示越多筆 tag**;超出的會顯示「… 還有 N 筆」。
按 E 停止後 buffer 會保留,要清空請按 C。

### 持續讀取(自動重啟)

實機就算下了連續模式 (`time=0, runs=0`),仍可能自己把盤點結束掉
(回 `STATUS_INVENTORY_TIMEOUT`、`STATUS_STOP_CONDITION` 之類)。
按 S 之後程式會**一直維持在讀取狀態**:偵測到讀取器停了就自動重下 0x6D,
直到你按 E 為止。

* 狀態列會顯示 `自動重啟: 12   上次結束: STATUS_INVENTORY_TIMEOUT`
* 重啟空檔約 20 ms(主迴圈輪詢間隔),經過時間從按 S 起算不歸零
* 重啟的例行訊息不進訊息區(否則 4 行馬上被洗掉),只有結束狀態
  「換了一種」時才會提示一次
* 想看讀取器原本的行為,加 `--no-auto-restart` 就不會自動接回去

模擬實機會自己停的情況:

```bash
python rfid_console.py --sim --sim-auto-stop 1.5     # 模擬器每 1.5 秒自己停一次
```

## GPIO input

本機型有 **3 個 GPIO input**,走的是**同一條 TCP 連線**,但用的是跟 RFID
完全不同的 **ASCII 文字協定** —— 一樣 `0x0A` 開頭、`0x0D 0x0A` 結尾,
中間卻是純文字,而且**沒有長度欄位、沒有 checksum**:

```
0x0A '@' <ASCII 文字> 0x0D 0x0A
```

| 用途 | 文字 | 位元組 |
|---|---|---|
| 查詢全部 input | `@InputPort` | `0A 40 49 6E 70 75 74 50 6F 72 74 0D 0A` |
| Pin1 = LOW(接地) | `@Input Pin1,0` | `0A 40 49 6E 70 75 74 20 50 69 6E 31 2C 30 0D 0A` |
| Pin1 = HIGH | `@Input Pin1,1` | `0A 40 49 6E 70 75 74 20 50 69 6E 31 2C 31 0D 0A` |

注意 `0x40` 就是 `'@'`,剛好落在二進位協定的 Message Type 欄位位置
(二進位只可能是 `0x80/0x81/0x82`),所以解析器是用**第 2 個位元組是不是
`0x40`** 來決定走文字還是二進位路徑。文字框沒有 checksum 可驗,改用
「內文必須是可列印 ASCII + 長度上限 200 + CRLF 收尾」當防呆。

程式介面:

```python
from mpk_rfid import RfidReader

with RfidReader("192.168.1.200", 8888) as r:
    # 1) 主動查詢:送 @InputPort,等所有 pin 回報完
    print(r.query_gpio())              # → {1: 0, 2: 1, 3: 0}

    # 2) 被動監看:讀取器主動送 @Input PinN,V 時觸發
    r.on_gpio = lambda pin, level, prev: print(f"IN{pin} = {level}")

    # 3) 隨時讀目前狀態(執行緒安全的快照)
    print(r.gpio, r.gpio_updated_at)
```

| 項目 | 說明 |
|---|---|
| `.query_gpio(timeout=1.0)` | 送 `@InputPort` 並**等**回報完,回傳 `{pin: 0/1}` |
| `.request_gpio()` | 只送查詢**不等**回覆 —— 給輪詢用,不會阻塞 |
| `.gpio` | 目前狀態快照 `{pin: 0/1}` |
| `.gpio_updated_at` | 最後一次收到 GPIO 訊息的時間(`time.time()`) |
| `.on_gpio(pin, level, prev)` | 準位**改變**時觸發(在 RX 執行緒上) |
| `parse_gpio_text(text)` | `"Input Pin1,0"` → `(1, 0)` |

測試軟體預設**每 1 秒**送一次 `@InputPort`,同時也會接收讀取器主動送來的
狀態。`--gpio-interval 0` 可以關掉輪詢只被動接收,`--gpio-interval 0.3`
可以問得更密。輪詢用的是 `request_gpio()`(不等回覆),所以**不會拖慢畫面
或按鍵反應**。

沒有實機時可以這樣模擬 GPIO 變化:

```bash
python rfid_console.py --sim --sim-gpio-toggle 1.0
```

## 三種用法

**1. 一行搞定** — 掃描 5 秒拿結果:

```python
from mpk_rfid import scan

for t in scan("192.168.1.200", 8888, seconds=5):
    print(t.epc, t.count, f"{t.rssi:.1f} dBm")
```

**2. 想控制連線** — 同一條連線做多次掃描:

```python
from mpk_rfid import RfidReader

with RfidReader("192.168.1.200", 8888) as r:
    print(r.get_firmware_version())
    for t in r.read_tags(seconds=5, power_dbm=20.0):
        print(t.epc, t.count, t.rssi, t.max_rssi)
```

**3. 即時處理每一筆讀取** — 邊讀邊做事:

```python
import time
from mpk_rfid import RfidReader, TagStore, DF_DEFAULT

store = TagStore()
with RfidReader("192.168.1.200", 8888) as r:
    r.on_tag = store.add          # ← 在 RX 執行緒上被呼叫
    r.set_data_format(DF_DEFAULT)
    r.set_inventory_parameter(rf_mode=103)
    r.start_inventory(antenna=1, power_dbm=20.0)   # 連續模式
    while ...:
        time.sleep(1)
        print(store.unique_count, store.total_reads)
    r.abort()
```

## 命令列

```bash
python mpk_rfid.py --sim                                  # 用模擬器試跑
python mpk_rfid.py --host 192.168.1.200 --port 8888 --seconds 10
python mpk_rfid.py --sim --trace                          # 印出封包 hex
```

輸出:

```
[16:10:48] 韌體版本: MPK-R-9504 v1.0.3
[16:10:49] 唯一 Tag=5  總讀取=284  速率=284/s

  #  EPC                           PC     次數  ANT     RSSI  MaxRSSI     通道(kHz)
  1  E28011700000020F00005678   3000    177    1   -70.21   -38.52      920000
  ...
唯一 Tag: 5   總讀取次數: 845   封包錯誤: 0
```

## API

| 項目 | 說明 |
|---|---|
| `scan(host, port, seconds=5, antenna=1, rf_mode=103, power_dbm=20)` | 連線→掃描→關閉,回傳 `List[TagRecord]` |
| `RfidReader(host, port)` | 支援 `with`;離開時自動 abort + close |
| `.read_tags(seconds, ...)` | 阻塞式掃描,回傳 `List[TagRecord]` |
| `.start_inventory(...)` / `.abort()` | 非阻塞式,搭配 `on_tag` 使用 |
| `.set_data_format(fmt)` | `DF_ANTENNA \| DF_CHANNEL \| DF_TIMESTAMP \| DF_RSSI \| DF_PHASE \| DF_TID`,`DF_DEFAULT`=0x1F |
| `.set_inventory_parameter(rf_mode=, session=, target=, ...)` | RF Mode / Q / Session / Target |
| `.get_firmware_version()` / `.get_temperature()` | 讀取器資訊 |
| `.tag_read(memory_bank=, word_address=, word_length=)` | 讀單一 tag 的記憶體 (0x70) |
| `.send_command(mc, payload)` | 想自己送任何命令時的低階入口 |

`TagRecord` 欄位:`epc` `tid` `pc` `antenna` `count` `rssi` `max_rssi` `channel_khz` `first_seen` `last_seen`

RF Mode 常數:`RF_ULTRA_FAST`(103, 1000+ tags/s)、`RF_FAST`(302)、
`RF_NORMAL`(345)、`RF_ULTRA_SENSITIVE`(285, -83 dBm)

## 執行緒

`open()` 會開一條 daemon 接收執行緒。**`on_tag` / `on_log` /
`on_inventory_finished` 都在那條執行緒上被呼叫**,不是你的主執行緒 ——
要更新 GUI 請自行 marshal(tkinter 用 `after`,PyQt 用 signal)。
`TagStore` 本身執行緒安全,所以最省事就是 `r.on_tag = store.add`,
主執行緒定期 `store.snapshot()`。

`read_tags()` 和 `scan()` 已經把這件事包好,不用自己處理。

## 協定實作重點

* 封包 = `0x0A | MT | MC | PL | Payload | Checksum | 0x0D 0x0A`,checksum = 全部 XOR
* **Payload Length 是 1 byte。** 文件 §1.2.3 寫「2 bytes」,但 Figure 1 標示
  header 共 3 bytes,且文件中 10 個範例封包的 checksum 都只有在 PL=1 byte 時
  才吻合 → 依範例實作(測試裡逐一驗算)
* **RSSI 用有號 int16 解析**(RSSI 是負值,用無號會變成 +600 多),單位 0.01 dBm
* Tag 通知的欄位由 payload 第 1 個 byte 的 bitmask 決定,依 bitmask 動態解析
* 解析器擋掉不合法的 Message Type(只接受 0x80/0x81/0x82),避免雜訊造成的
  假 Preamble 讀到超大 PL 而永遠卡住
* `TagReport.parse()` 對任何截斷/畸形資料一律回 `None` 不丟例外

## 已知行為

**按 E 停止後,log 會出現 `盤點結束: STATUS_ABORT (ANT=1)`** —— 這是正常的。
讀取器收到 Abort 之後,會用 §2.3.7.3 的「錯誤/中止型」payload 回報盤點已結束:
`[Status 1][ANT_ID 1]`,例如 `03 01` = STATUS_ABORT + RF port 1。
它跟正常結束的 `[Status][Total 4][Elapsed 4]` (PL=9) 是兩種不同形狀,程式兩種都會解析。

## 實機注意

* **TCP port 文件沒寫**,依實機設定。
* **盤點中送其他命令**可能回 `STATUS_BUSY (0x04)` 或不回應。這版沒有背景輪詢,
  所以只要不在盤點中呼叫其他命令就沒事。
* `get_firmware_version()` / `get_temperature()` 的回應欄位文件未細列,
  程式已做容錯,對照實機後可再收斂。
* 需要 Serial(RS-232 / USB COM)的話,把 `_Tcp` 換成 pyserial 的
  `Serial` 包裝即可,`RfidReader` 只用到 `open/close/read/write/is_open` 五個方法。

## 測試

```bash
python test_mpk_rfid.py       # 函式庫,42 項
python test_rfid_console.py   # 測試軟體,11 項
# 或直接 pytest -q
```

涵蓋:文件 10 個範例封包的 checksum 與 frame 版面、`Run Inventory` 與
`Set Inventory parameter` 的 payload 對照、串流解析(逐 byte / 每個切點 /
雜訊重新同步 / 壞 checksum / 壞 end mark / 不合法 MT 不卡死)、TagReport 各種
bitmask 組合與截斷容錯、RSSI 負值、TagStore 多執行緒,以及對模擬器的端對端
測試(命令回應、連續盤點、`read_tags`、`scan`、逾時後命令鎖正確釋放)。

`test_rfid_console.py` 另外涵蓋:讀取器自己停掉時能持續讀取、`--no-auto-restart`
確實不重啟、按 E 之後不會被自動重啟接回去、**重啟與命令並行不會死鎖**
(重啟若寫在 RX 執行緒裡就會卡死)、訊息區不被洗版、經過時間不被重啟歸零、
RX 執行緒寫訊息時 render 不會拋例外。

GPIO 部分另外涵蓋:三個實機封包的位元組完全比對與 round-trip、`parse_gpio_text`
的空白/大小寫容錯、**文字框與二進位封包混在同一條串流時互不干擾**(含在每一個
位元組邊界切開)、逐 byte 餵入、含控制字元或收不到 CRLF 時能重新同步不卡死、
對模擬器的查詢與主動回報、盤點進行中同時收 GPIO,以及輪詢不會拖慢主迴圈。
