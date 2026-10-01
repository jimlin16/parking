# 桃園機場 P4 停車場車位監控與自動預約系統

由 Antigravity 封包監聽與 API 逆向分析產出之輕量化自動程式。

---

## 🌟 專案特色

1. **零瀏覽器常駐**：查詢車位、訂單查詢與退訂皆走純 HTTP API，毫秒級回傳，極省系統資源。
2. **持久化 Profile**：登入工具與預約程式共用 Chrome 登入狀態；平台是否要求額外驗證，取決於平台判定，程式無法取得或保證 reCAPTCHA 評分。Profile 含登入資料，請勿分享或上傳。
3. **reCAPTCHA v2 驗證頁**：若平台要求額外驗證，系統會開啟官方頁面；圖形驗證可能需要在瀏覽器中手動完成。
4. **自動搶位（Auto-Book）**：可設定監控日期區間，一旦偵測到有人取消退訂釋出車位，立即秒殺送單。

---

## 📁 檔案結構

* `youparking_client.py`：核心 API Client SDK（查車位、查訂單、取消訂單、下單預約、持久化 v3/v2 處理）。
* `login_google.cmd` / `login_google.py`：Google 帳號持久化登入與權重維護工具（雙擊即可登入）。
* `monitor.py`：車位即時監控與自動搶單主程式。
* `dashboard.py`、`web/`：本機網頁控制台與操作通知、Log。
* `config.json`：預約常用設定檔（車牌、聯絡人、預設輪詢間隔等）。
* `requirements.txt`：依賴套件清單。

---

## 🚀 快速上手

### 1. 安裝環境依賴
本專案需 Python 3.10+ 與 Playwright：
```bash
pip install -r requirements.txt
```

### 2. 設定共用 Chrome 登入狀態（選用）
雙擊執行 `login_google.cmd`（或執行 `python login_google.py`），在開啟的 Chrome 視窗登入您的個人 Google 帳號一次並關閉。
登入狀態會保存在本機 Profile；同一份 Profile 只能由一個程式開啟 Chrome。登入不保證免除 v2 驗證。

### 3. 測試即時查詢車位
直接執行 SDK 測試：
```bash
python youparking_client.py
```

### 4. 使用網頁控制台

Windows 可直接雙擊 `start_dashboard.bat`（或執行 `start_dashboard.cmd`）；啟動時會辨識並結束占用 8765 埠的舊 Dashboard 程序，再開啟新版控制台。若舊程序正在送單或該埠屬於其他程式，啟動會停止並顯示原因。也可以手動啟動：

```bash
python dashboard.py
```

在同一台電腦開啟 `http://127.0.0.1:8765`。控制台可選擇停車場（預設為原本 P4）、修改掃描間隔、入場與離場日期、姓名、電話與車牌；設定會儲存到 `config.json`，操作紀錄會寫入 `logs/dashboard.jsonl`。

每次開啟控制台時，上一輪的執行結果、通知與 log 會清空；預約資料與掃描設定會保留。

按「啟動監控」才會開始查詢。手動送出預約與啟用空位自動預約前，介面會要求確認。自動預約每次監控最多嘗試送單一次，無論結果如何都會停止監控。若平台要求 reCAPTCHA v2，手動「立即預約」會開啟官方驗證頁。平台明確回覆成功或回傳訂單編號時，程式會顯示預約成功；若官方交易紀錄尚未查到，訊息會註明仍待核對。沒有明確成功或失敗回覆時，會保留 `.booking-state/` 防重複紀錄並阻止相同預約再次送出，請先到官方交易紀錄核對。

若官網確認該次預約確實不存在，先停止 Dashboard 與命令列監控，再依錯誤訊息列出的檔名，移除 `.booking-state/` 中該筆 `.pending` 檔後才能重試。不要在未核對官網訂單前移除防重複檔。

此控制台只綁定 `127.0.0.1`，不對外公開。關閉執行 `dashboard.py` 的終端機即可關閉控制台。

### 5. 使用命令列自動監控

#### 模式 A：純監控（有車位時發出提示音）
```bash
python monitor.py --start 2026-10-11 --end 2026-10-13
```

#### 模式 B：全自動搶位（有空位時立即背景自動下單）
```bash
python monitor.py --start 2026-10-11 --end 2026-10-13 --auto-book
```

---

## ⚙️ 參數設定 (`config.json`)

可以直接編輯 `config.json` 更改您的常用資訊：
```json
{
  "parking_lot": "p4",          // p4: 原本 P4；peace: 日月亭平安
  "space_id": "1",               // 1: 第二航廈 P4 停車場
  "site_code": 84416,            // 站點代碼
  "car_no": "ABC-"1234,          // 車牌號碼
  "contact_name": "王小明",       // 聯絡人姓名
  "phone": "",                   // 手機號碼 (選填)
  "inv_type": "紙本",             // 發票 ("紙本", "手機載具", "統一編號")
  "check_interval_seconds": 10   // 輪詢間隔 (秒)
}
```
