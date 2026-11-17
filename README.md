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
