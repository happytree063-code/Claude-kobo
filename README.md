# Kobo 99元週特賣書單

每週四自動從 [Kobo 台灣部落格](https://www.kobo.com/zh/blog) 抓取本週 99 元特賣電子書，並彙整 Google Books、博客來、讀冊生活的評分資訊。

## 功能

- 每週四 10:00（台北時間）自動爬取 Kobo 部落格最新 99 元特賣書單
- 同步抓取各大平台評分（Google Books・博客來・讀冊生活）
- 支援手動點「立即更新書單」按鈕即時更新
- 保存歷史週次書單，可切換查看
- 響應式設計，手機、桌機皆適用

## 快速開始

```bash
pip install -r requirements.txt
python run.py
```

開啟瀏覽器前往 http://localhost:8000

## 架構

```
app/
  main.py       # FastAPI 應用
  scraper.py    # Kobo 部落格爬蟲
  ratings.py    # 各平台評分抓取
  database.py   # SQLite 資料庫
  scheduler.py  # 週四自動更新排程
templates/
  index.html    # 前端介面
static/
  style.css     # 樣式
books.db        # SQLite 資料（自動產生）
```

## API

| 路徑 | 說明 |
|------|------|
| `GET /` | 本週書單首頁 |
| `GET /week/YYYY-MM-DD` | 指定週次書單 |
| `POST /api/refresh` | 手動觸發更新 |
| `GET /api/books` | 本週書單 JSON |
