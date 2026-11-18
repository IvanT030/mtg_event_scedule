# mtg_event_scedule

從 FB 卡店粉專抓貼文（文字＋圖片），之後篩出 MTG 賽事並整理時間地點。

## 結構

- `pages.txt`：要爬的粉專清單
- `scraper/fetch_posts.py`：爬蟲（Playwright，不需登入）
- `.github/workflows/scrape.yml`：GitHub Actions，每天自動跑，也可手動執行
- `data/posts/<粉專>.json`：抓到的貼文
- `data/images/<粉專>/`：貼文圖片
- `data/last_run.log`：最近一次執行紀錄

## 手動執行

GitHub repo → Actions → Scrape FB posts → Run workflow，填入粉專網址與天數。

## 原理

用 Playwright 開粉專頁面，攔截 FB 自己發出的 GraphQL 請求（doc_id、粉專 id），再直接翻頁呼叫 GraphQL API。
參考 [KarkiMilan/facebook-scrapper](https://github.com/KarkiMilan/facebook-scrapper) 的方法，以 Python 自行實作。

## 在自己電腦上跑（登入版，建議）

GitHub 的主機 IP 會被 FB 限流，用 cookies 登入也會被鎖帳號。
登入版請在自己電腦（家用網路）跑。Windows 用 WSL：

```bash
git clone https://github.com/IvanT030/mtg_event_scedule.git
cd mtg_event_scedule
python3 -m venv .venv && source .venv/bin/activate
pip install -r scraper/requirements.txt
python -m playwright install --with-deps chromium
```

1. 用電腦的瀏覽器登入 FB **小號**，裝 Cookie-Editor 擴充功能 → Export（JSON），
   存成 repo 根目錄的 `cookies.json`（已在 .gitignore，不會被上傳）
2. 執行：

```bash
python scraper/fetch_posts.py --cookies cookies.json            # 用 pages.txt
python scraper/fetch_posts.py --cookies cookies.json --headful  # 顯示瀏覽器視窗
git add data && git commit -m "data: local scrape" && git push
```

- 遇到已存過的貼文、或超過 `--days`（預設 30 天）就停止往下捲
- FB 要求重新登入／驗證時程式會直接停止，請用瀏覽器重新登入小號後再匯出 cookies
