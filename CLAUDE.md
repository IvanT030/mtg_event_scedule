# mtg_event_scedule

從台灣卡店的 FB 粉專抓貼文，篩出 **MTG（魔法風雲會）賽事**，把文字與海報圖片整理成結構化的賽事資料。

## 整體流程

```
① 爬蟲 ──► data/stores/<粉專>/posts.json + images/ + store.json
② 分析 ──► data/stores/<粉專>/analysis/<post_id>.json   （每篇一個檔，MTG 與非 MTG 都寫）
③ 彙整 ──► data/events.json + data/events.db
```

| 步驟 | 在哪裡跑 | 怎麼跑 | 得到什麼 |
|---|---|---|---|
| ① 爬蟲（不登入） | GitHub Actions | 每 3 小時自動（UTC 的 :17 分）；或改 `pages.txt`／`scraper/**` 推上去；或 Actions → Scrape FB posts → Run workflow | 最新 1～4 篇（GitHub IP 被 FB 限流，時好時壞） |
| ① 爬蟲（登入） | 你的電腦（WSL，家用網路） | `python scraper/fetch_posts.py --cookies cookies.json` 再 `git push` | 30 天內所有新貼文，遇到已存過的貼文就停 |
| 查待分析 | 任何地方 | `python analysis/pending.py` | 列出還沒有分析檔的貼文 |
| ② 分析 | Claude 工作階段（排程任務或手動） | 依照下方「分析規範」逐篇處理 | `data/stores/<粉專>/analysis/<post_id>.json` |
| ③ 彙整 | 任何地方 | `python analysis/build_events.py` | `data/events.json`、`data/events.db`；驗證失敗會列出錯誤並以 exit code 1 結束 |

## 目錄

- `pages.txt` — 要爬的粉專，一行一個，`#` 後面寫店名（會成為 `store` 欄位與 `store.json` 的 `name`）。用粉專首頁網址，不要用 `facebook.com/share/...` 分享連結
- `scraper/fetch_posts.py` — 爬蟲（Playwright；不登入時另試 GraphQL API 與相簿頁）
- `.github/workflows/scrape.yml` — GitHub Actions 排程
- `analysis/pending.py` — 列出待分析貼文
- `analysis/build_events.py` — 驗證分析檔並產生 events.json / events.db
- `data/stores/<粉專>/` — 每家店一個資料夾：
  - `store.json` — 店名、粉專網址、page_id、最後爬取時間
  - `posts.json` — 貼文（爬蟲維護，不要手動改）
  - `images/` — 貼文圖片 `<post_id>_N.jpg`
  - `analysis/` — 分析產出（每篇一個 JSON）
- `data/last_run.log` — 最近一次爬蟲紀錄
- `data/debug/` — 除錯截圖與 HTML，**已 gitignore，不可提交**（可能含帳號資訊）

### posts.json 每篇貼文的欄位

`post_id`、`post_url`（貼文連結 `https://www.facebook.com/<粉專>/posts/<post_id>`）、`time`（ISO，+08:00）、`timestamp`、`text`、`links`（貼文裡的外部連結與 FB 活動連結，例如報名表單）、`page`、`store`、`local_images`（repo 內的圖片路徑）、`scraped_at`

## 分析規範（步驟 ②）

### 要處理哪些貼文
執行 `python analysis/pending.py`（`--json` 會附上要寫入的 `analysis_file` 路徑），對每一篇：讀 `text` 與 `links`，**並用 Read 工具打開 `local_images` 裡的每一張圖**。很多店家把日期、時間、賽制只寫在海報上。

### 什麼算 MTG 賽事
- 遊戲必須是 **Magic: The Gathering**。常見關鍵字：MTG、魔法風雲會、魔風、Magic、FNM、Friday Night Magic、標準、先驅、近代、薪傳、特選、Pauper、指揮官／Commander／EDH、輪抽、現開、售前賽／Prerelease、RCQ、Magic Spotlight、MagicCon、Store Championship（店家冠軍賽）
- 必須是**還沒舉辦、可參加的活動**（比賽、輪抽、售前、聯賽、教學場）
- 一篇貼文同時有多種卡牌遊戲時，**只擷取 MTG 的場次**
- **不算**：產品到貨或預購、賽事結果與回顧、單卡販售、其他遊戲（遊戲王、寶可夢、Vanguard／VG、Shadowverse Evolve／SVE、Union Arena、One Piece、Marvel 等）
- 不確定時：`is_mtg` 依最可能的判斷，並在 `confidence` 反映不確定程度

### 輸出檔案：`data/stores/<粉專>/analysis/<post_id>.json`
**每篇都要寫**（非 MTG 也要寫），這樣才不會重複分析。

非 MTG：
```json
{
  "post_id": "1731572442313765",
  "post_url": "https://www.facebook.com/cardmaster.tw/posts/1731572442313765",
  "post_time": "2026-11-17T19:48:07+08:00",
  "is_mtg": false,
  "reason": "遊戲王 RV02 奪盒賽",
  "analyzed_at": "2026-11-18T12:30:00+08:00"
}
```

MTG（`events` 是陣列，一篇可能有多場，例如一週賽程表）：
```json
{
  "post_id": "...",
  "post_url": "...",
  "post_time": "2026-11-17T19:48:07+08:00",
  "is_mtg": true,
  "reason": "FNM 標準賽公告",
  "analyzed_at": "2026-11-18T12:30:00+08:00",
  "events": [
    {
      "title": "FNM 標準賽",
      "store": "CardMaster 台北門市",
      "address": "台北市中正區南昌路二段47號1樓",
      "format": "Standard",
      "event_type": "FNM",
      "start": "2026-11-21T19:00+08:00",
      "end": null,
      "registration_start": "2026-11-21T18:30+08:00",
      "fee": 200,
      "prizes": "前四名各得補充包 x2",
      "capacity": 32,
      "links": ["https://forms.gle/xxxx"],
      "notes": "需事先報名",
      "confidence": 0.9,
      "evidence": ["text", "image"]
    }
  ]
}
```

### 欄位規則
- **時間**：一律 `YYYY-MM-DDTHH:MM+08:00`；只知道日期時用 `YYYY-MM-DD`。
  - 年份：貼文只寫月/日時，用 `post_time` 的年份；若推出的日期比發文日早超過 30 天，視為隔年
  - 「週六」「本週五」這類寫法，以 `post_time` 為基準推算
  - `start` 必填；沒寫結束時間 → `end: null`（**不要猜**）
  - 「14:00 開始報名、14:30 開賽」→ `registration_start` 14:00、`start` 14:30
  - 每週固定的例行賽（如「每週五 FNM」）：只填貼文提到的那一次或最近的下一次，並在 `notes` 註明「每週」
- **store**：粉專的店名＋分店；貼文寫明分店時用分店名。同一家店名稱保持一致（CardMaster 統一寫 `CardMaster`，分店接在後面）
- **address**：貼文或海報上寫的地址；沒寫就 `null`
- **format**：只能用 `Standard`、`Pioneer`、`Modern`、`Legacy`、`Vintage`、`Pauper`、`Commander`、`Duel Commander`、`cEDH`、`Draft`、`Sealed`、`Prerelease`、`Two-Headed Giant`、`Alchemy`、`Historic`、`Other`，或 `null`
- **event_type**：自由文字，盡量用常見名稱：`FNM`、`Prerelease`、`Store Championship`、`RCQ`、`League`、`Casual`、`Tournament`
- **fee**：新台幣整數；免費寫 `0`；沒寫就 `null`
- **capacity**：人數上限整數，沒寫就 `null`
- **links**：跟這場比賽有關的連結（報名表單、FB 活動頁），從貼文的 `links` 挑；沒有就 `[]`。海報上只有 QR code 時在 `notes` 註明「報名連結僅有 QR code」
- **confidence**：0～1。資訊都明確寫出 ≥ 0.9；需要推算日期或年份約 0.7～0.8；只能靠猜 < 0.6
- **evidence**：資訊從哪裡讀到的，`"text"`、`"image"` 或兩者都有
- **不可編造**貼文與圖片裡沒有的資訊；不知道就 `null`

### 分析完之後
1. `python analysis/build_events.py`，要通過驗證（exit code 0）；失敗就修正分析檔
2. `git pull --rebase` 後提交：`data/stores/*/analysis/`、`data/events.json`、`data/events.db`
3. commit 訊息：`analysis: N posts (M MTG events)`
4. 回報：分析了幾篇、找到哪些 MTG 賽事（日期、店家、賽制）、低信心需要人工確認的項目

## 注意事項

- **不要在 GitHub Actions 或任何雲端環境使用 FB cookies**：會被 FB 判定盜用並鎖住帳號（已經發生過）
- `cookies.json`、`data/debug/` 都在 .gitignore，絕不可提交
- 爬蟲的 `data/` commit 由 github-actions[bot] 自動產生，推送前先 `git pull --rebase`
- `data/stores/*/posts.json` 由爬蟲維護，分析時不要修改
