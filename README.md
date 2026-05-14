# Claude-kobo

Kobo app — 包含每日 99 元書籍 LINE 提醒功能。

---

## 每日 Kobo LINE 提醒

每天早上 9 點自動傳送 LINE 訊息，告訴你今日 Kobo 台灣每日 99 元特惠書是哪一本，以及 Goodreads、Google Books、Open Library 的評分。

### 訊息格式範例

```
📚 今日 Kobo 每日 99 元好書
─────────────────
📖 書名：原子習慣
✍️  作者：James Clear
💰 價格：NT$99

⭐ 各平台評分
• Goodreads：4.37/5 (1,234,567 則評分)
• Google Books：4.5/5 (8,902 則評分)
• Open Library：4.12/5 (345 則評分)

🔗 https://www.kobo.com/tw/zh/ebook/...
```

---

## 設定步驟

### 1. 建立 LINE Bot（Messaging API）

> ⚠️ LINE Notify 已於 2025 年 3 月停止服務，請改用 LINE Messaging API。

1. 前往 [LINE Developers Console](https://developers.line.biz/console/)
2. 建立一個 **Provider**（若已有可跳過）
3. 新增 **Messaging API** Channel
4. 進入 Channel → **Messaging API** 頁籤
5. 滾到底部，點「Issue」產生 **Channel Access Token（長效型）**
6. 將 Bot 加入好友（掃 QR Code）

### 2. 取得你的 LINE User ID

加好友後，傳任意訊息給 Bot，然後到 LINE Developers Console →
**Messaging API** → **Webhook URL** 設定一個暫時的 webhook（可用 [webhook.site](https://webhook.site)），傳訊息後即可看到 `source.userId`（格式：`Uxxxxxxxxxxxxxxxxx`）。

或直接用以下指令取得（需先傳訊息給 Bot）：

```bash
curl -X GET https://api.line.me/v2/bot/profile/<YOUR_USER_ID> \
  -H "Authorization: Bearer <YOUR_CHANNEL_ACCESS_TOKEN>"
```

### 3. 設定 GitHub Secrets

在你的 GitHub repo → **Settings** → **Secrets and variables** → **Actions** → 新增：

| Secret 名稱 | 說明 |
|---|---|
| `LINE_CHANNEL_ACCESS_TOKEN` | 步驟 1 取得的 Channel Access Token |
| `LINE_USER_ID` | 步驟 2 取得的 User ID（`U` 開頭） |

### 4. 啟用 GitHub Actions

推送程式碼後，GitHub Actions 會自動依排程執行。  
也可到 **Actions** → **Kobo Daily LINE Reminder** → **Run workflow** 手動測試。

---

## 本機測試

```bash
pip install -r requirements.txt

export LINE_CHANNEL_ACCESS_TOKEN="your_token"
export LINE_USER_ID="Uxxxxxxxxxxxxxxxxx"

python kobo_daily_reminder.py
```

---

## 排程時間調整

修改 `.github/workflows/daily_reminder.yml` 中的 `cron` 設定：

```yaml
- cron: '0 1 * * *'   # UTC 01:00 = 台灣時間 09:00
```

常用對照：

| 台灣時間 | UTC cron |
|---|---|
| 08:00 | `0 0 * * *` |
| 09:00 | `0 1 * * *` |
| 12:00 | `0 4 * * *` |
| 20:00 | `0 12 * * *` |
