# mtg_event_scedule

從 FB 卡店粉專抓貼文（文字＋圖片），之後篩出 MTG 賽事並整理時間地點。

## 結構

- `pages.txt`：要爬的粉專清單（`#` 後面寫店名）
- `scraper/fetch_posts.py`：爬蟲（Playwright，不需登入）
- `.github/workflows/scrape.yml`：GitHub Actions，每 3 小時自動跑，也可手動執行
- `data/stores/<粉專>/posts.json`：抓到的貼文（含貼文連結與內文中的外部連結）
- `data/stores/<粉專>/images/`：貼文圖片
- `data/stores/<粉專>/analysis/`：分析結果
- `data/events.json`、`data/events.db`：彙整後的 MTG 賽事
- `data/last_run.log`：最近一次執行紀錄

## 手動執行

GitHub repo → Actions → Scrape FB posts → Run workflow，填入粉專網址與天數。

## 原理

用 Playwright 開粉專頁面，攔截 FB 自己發出的 GraphQL 請求（doc_id、粉專 id），再直接翻頁呼叫 GraphQL API。
參考 [KarkiMilan/facebook-scrapper](https://github.com/KarkiMilan/facebook-scrapper) 的方法，以 Python 自行實作。

## 正式流程：網站後端每天執行

GitHub Actions 已暫停（GitHub 的 IP 會被 FB 限流，用 cookies 登入會被鎖帳號）。
正式流程在你的電腦（家用網路）跑，由網站後端每天呼叫一次。

### 一次性安裝（WSL）

```bash
git clone https://github.com/IvanT030/mtg_event_scedule.git
cd mtg_event_scedule
python3 -m venv .venv && source .venv/bin/activate
pip install -r scraper/requirements.txt
python -m playwright install --with-deps chromium
```

1. 用電腦的瀏覽器登入 FB **小號**，裝 Cookie-Editor → Export（JSON），存成 repo 根目錄的 `cookies.json`（已 gitignore）
2. 分析用的語言模型（預設用你的 Claude 方案額度）：
   ```bash
   npm install -g @anthropic-ai/claude-code   # 安裝 Claude Code
   claude                                     # 第一次執行，用你的 Claude 帳號登入，然後 /exit
   ```
   之後 pipeline 會用 `claude -p` 分析，用量算在你的方案裡，不用 API key。
   **不要設定 `ANTHROPIC_API_KEY`**（pipeline 會自動忽略它，但你自己手動跑 `claude` 時它會改走 API 計費）。

   想改用 API 的話：`export LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=...`
   或 `export LLM_PROVIDER=gemini GEMINI_API_KEY=...`；`LLM_MODEL` 可換模型。

### 執行

```bash
python run_pipeline.py --cookies cookies.json
```

stdout 會印出一個 JSON 摘要（新貼文數、分析結果、賽事數、需要人工確認的項目），
後端可以直接解析。前端讀 `data/schedule.json`。

| Exit code | 意思 |
|---|---|
| 0 | 成功 |
| 1 | 有步驟出錯（看摘要的 `analysis.failed`、`build.invalid`） |
| 2 | FB session 被擋：用瀏覽器重新登入小號、重新匯出 `cookies.json` |
| 3 | 已有另一次執行中 |

後端呼叫範例（Node.js）：

```js
const { execFile } = require("child_process");
execFile(".venv/bin/python", ["run_pipeline.py", "--cookies", "cookies.json"],
  { cwd: "/path/to/mtg_event_scedule", timeout: 30 * 60 * 1000 },
  (err, stdout) => {
    const summary = JSON.parse(stdout);   // err.code 是 exit code
    console.log(summary.build.upcoming, "upcoming events");
  });
```

其他選項：`--skip-scrape`（只分析＋彙整）、`--skip-analyze`（只爬＋彙整）、`--days N`、`--provider`、`--model`。

**注意**：網站後端若部署到雲端主機（VPS 等），爬蟲那一步一樣會被 FB 擋或鎖帳號，要留在家裡的電腦跑。
