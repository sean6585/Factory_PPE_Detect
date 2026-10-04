# 現場 Wi-Fi 設定（IT 上報用，現場無外網）

以下指令都在 Jetson 終端機執行，`sudo` 密碼即登入密碼。`< >` 內換成實際值。

## 0. 先向現場 IT 要的資料

- SSID、認證方式（共用密碼 / 公司帳號 WPA2-Enterprise / 憑證）
- DHCP 或固定 IP（固定需：IP、遮罩、Gateway、DNS）
- 是否要登記 MAC：本機 Wi-Fi MAC **`00:0e:8e:d8:25:30`**
- IT 中心主機名稱能否用內網 DNS 解析；不行就要一個 IP
- 內網 NTP 伺服器位址（沒外網，時間要靠它）

## 1. 連線

```bash
nmcli dev wifi list                                    # 看得到 SSID 嗎
# 共用密碼
sudo nmcli dev wifi connect "<SSID>" password "<密碼>" ifname wlan0 name site-wifi
# 公司帳號（PEAP/MSCHAPv2，最常見；其他型式照 IT 指示）
sudo nmcli con add type wifi ifname wlan0 con-name site-wifi ssid "<SSID>" \
  wifi-sec.key-mgmt wpa-eap 802-1x.eap peap 802-1x.phase2-auth mschapv2 \
  802-1x.identity "<帳號>" 802-1x.password "<密碼>"
sudo nmcli con up site-wifi
# 隱藏 SSID：再加 802-11-wireless.hidden yes
```

## 2. 固定 IP

```bash
sudo nmcli con mod site-wifi ipv4.method manual ipv4.addresses <IP>/<遮罩位數> \
  ipv4.gateway <Gateway> ipv4.dns "<DNS>"
sudo nmcli con up site-wifi
```

## 3. 穩定性

```bash
sudo nmcli con mod site-wifi 802-11-wireless.powersave 2 connection.autoconnect-priority 10
sudo nmcli con mod GM42 connection.autoconnect no      # 舊的 Wi-Fi 不要自動連
sudo nmcli con mod Chur_19F_5G connection.autoconnect no
sudo nmcli con mod Gifted connection.autoconnect no
```

## 4. 檢查網段不能重疊

```bash
ip -br addr show wlan0
```

Wi-Fi 位址**不可**是 `192.168.1.x`（攝影機）、`192.168.10.x`（三色燈）、`100.206.151.x`（RFID）。
若重疊：先別接設備線，聯絡我改設備端網段。

## 5. 確認連得到 IT 中心

```bash
H=tgaia-siteaccesscontrolservice-central.facility-test.ftest.tsmc.com
ip route | grep default                  # 應該是 dev wlan0
getent hosts $H                          # 有印出 IP = DNS 正常
curl -k -m 10 -o /dev/null -w '%{http_code}\n' https://$H/ppe/device-heartbeat
                                         # 任何數字 = 通；000 = 不通
```

DNS 解析不到、但 IT 給了 IP（網址不用改，HTTPS 名稱照舊）：

```bash
echo "<IT的IP> $H" | sudo tee -a /etc/hosts
```

## 6. 時間同步（上報用 UTC，時間錯會被 IT 端誤判）

```bash
echo "server <內網NTP> iburst" | sudo tee /etc/chrony/sources.d/site.sources
sudo systemctl restart chrony
chronyc sources                          # 有一行開頭是 ^* = 已同步
timedatectl                              # 確認時間正確
```

沒有 NTP 時手動設：`sudo timedatectl set-time "2026-10-15 09:00:00"`（每次重開機都要檢查）

## 7. 閘門端

kiosk 底部：**IT 目標切到 `tsmc-test`** → 按 **IT Report** 打開 → IT 標籤顯示 `heartbeat ok`、sent 數字會增加。

```bash
journalctl -u ppe-gate -f | grep "\[it\]"      # 看上報結果（Ctrl+C 離開）
```

## 出問題時

```bash
nmcli dev status                                 # wlan0 是否 connected
journalctl -u NetworkManager -n 30 --no-pager    # 連線失敗原因
sudo nmcli con up site-wifi                      # 重連
```

上報失敗不會遺失：資料留在本機 outbox，網路恢復後自動補送。
