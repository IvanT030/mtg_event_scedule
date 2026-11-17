# mtg_event_scedule

從 FB 卡店粉專抓貼文（文字＋圖片），之後篩出 MTG 賽事並整理時間地點。

## 結構

- `pages.txt`：要爬的粉專清單
- `scraper/fetch_posts.py`：爬蟲（使用 [kevinzg/facebook-scraper](https://github.com/kevinzg/facebook-scraper)）
- `.github/workflows/scrape.yml`：GitHub Actions，每天自動跑，也可手動執行
- `data/posts/<粉專>.json`：抓到的貼文
- `data/images/<粉專>/`：貼文圖片
- `data/last_run.log`：最近一次執行紀錄

## 手動執行

GitHub repo → Actions → Scrape FB posts → Run workflow，填入粉專網址。

## 選用：登入 cookies

如果未登入抓不到東西，可以把 FB 的 cookies（Netscape cookies.txt 格式）
存成 repo secret `FB_COOKIES`。建議用小號，不要用主帳號。
