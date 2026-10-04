# mpk_rfid — MPK-R-9504 RFID 讀取(精簡版)

單一檔案 `mpk_rfid.py`,零第三方相依,只做「讀取 RFID tag」。
依照 MPK-R-9504 Protocol User Guide v1.0 實作,TCP 連線。

## 檔案

| 檔案 | 說明 |
|---|---|
| **`mpk_rfid.py`** | 函式庫,全部功能都在這裡。複製這一個檔案到你的專案就能用。 |
| **`rfid_console.py`** | 互動式測試軟體(S 開始 / C 清除 / E 停止)。 |
| `simulator.py` | 假讀取器(沒有實機時測試用)。不需要就刪掉,`mpk_rfid.py` 不依賴它。 |
| `test_mpk_rfid.py` | 測試(26 項)。 |

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
| **Q** | 離開(Esc 或 Ctrl-C 也可以) |

畫面每 0.25 秒原地更新一次(不會一直往下捲):

```
====================================================================================
 MPK-R-9504 RFID 讀取測試                            192.168.1.200:8888  [已連線]
====================================================================================
 狀態: ● 讀取中       經過:   12.4 s
 唯一 Tag: 5      總讀取: 3,485      速率:    281 /s    封包錯誤: 0
------------------------------------------------------------------------------------
   # EPC                                        次數    RSSI     Max ANT    最後
   1 E28011700000020F00005678                    748   -43.0   -38.1   1    0.0s
   2 E28011700000020F00001234                    745   -41.3   -39.4   1    0.0s
   3 E28011700000020F0000ABCD                    701   -68.4   -38.3   1    0.0s
------------------------------------------------------------------------------------
 10:45:29  韌體版本: MPK-R-9504 v1.0.3
 10:45:29  Inventory 開始 (ANT=1, RFMode=103, 20.0 dBm, 連續模式)
====================================================================================
 [S] 開始讀取    [C] 清除 buffer    [E] 停止讀取    [Q] 離開
```

其他參數:`--antenna 1`、`--power 20`、`--rf-mode 103`。

自動化/展示可以用腳本自動按鍵:

```bash
python rfid_console.py --sim --script "s:3,c,s:2,e:1,q"
```
（`鍵:秒數` 表示按下該鍵後等幾秒再按下一個。）

**視窗越高顯示越多筆 tag**;超出的會顯示「… 還有 N 筆」。
按 E 停止後 buffer 會保留,要清空請按 C。

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
python test_mpk_rfid.py     # 或 pytest -q
```

涵蓋:文件 10 個範例封包的 checksum 與 frame 版面、`Run Inventory` 與
`Set Inventory parameter` 的 payload 對照、串流解析(逐 byte / 每個切點 /
雜訊重新同步 / 壞 checksum / 壞 end mark / 不合法 MT 不卡死)、TagReport 各種
bitmask 組合與截斷容錯、RSSI 負值、TagStore 多執行緒,以及對模擬器的端對端
測試(命令回應、連續盤點、`read_tags`、`scan`、逾時後命令鎖正確釋放)。
