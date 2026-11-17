"""Fetch recent posts (text + images) from public Facebook pages, no login.

Approach (same idea as KarkiMilan/facebook-scrapper, reimplemented here):
  1. Open the page in headless Chromium (Playwright), dismiss the login popup,
     scroll so Facebook's own web app fires its timeline GraphQL request.
  2. Capture that request's doc_id + variables (page id, cursor).
  3. Replay/paginate https://www.facebook.com/api/graphql/ with a plain
     HTTP/1.1 client (python-requests UA), which is not login-walled.
  4. Walk each JSON response to pull out posts: id, time, text, image URLs.

Outputs:
  data/posts/<page>.json         merged post list (newest first)
  data/images/<page>/<id>_N.jpg  downloaded images
  data/last_run.log              run log
  data/debug/                    screenshots / raw samples when something fails

Usage:
  python scraper/fetch_posts.py --page cardmaster.tw --days 30
  python scraper/fetch_posts.py            # uses pages.txt
"""

import argparse
import datetime as dt
import json
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DEBUG = DATA / "debug"
LOG_FILE = DATA / "last_run.log"
GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
TZ_TW = dt.timezone(dt.timedelta(hours=8))

BAD_SLUGS = {"share", "sharer", "sharer.php", "story.php", "permalink.php",
             "groups", "watch", "photo", "photo.php", "events"}


# ── setup ────────────────────────────────────────────────────────────────────

def setup_logging():
    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def page_slug(page: str) -> str:
    page = page.strip()
    m = re.search(r"facebook\.com/profile\.php\?id=(\d+)", page)
    if m:
        return m.group(1)
    m = re.search(r"facebook\.com/([^/?&#]+)", page)
    if m:
        page = m.group(1)
    return page.strip("/")


def read_pages(args) -> list[str]:
    raw = []
    if args.page:
        raw += [p for p in re.split(r"[,\s]+", args.page) if p]
    elif Path(args.pages_file).exists():
        for line in Path(args.pages_file).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                raw.append(line)
    slugs = []
    for p in raw:
        s = page_slug(p)
        if s.lower() in BAD_SLUGS:
            logging.error("無法使用的網址：%s —— 這是分享/貼文連結，請改用粉專首頁網址，"
                          "例如 https://www.facebook.com/cardmaster.tw/", p)
            continue
        slugs.append(s)
    return slugs


# ── JSON walking helpers ─────────────────────────────────────────────────────

def walk(obj, path=()):
    """Yield (path, key, value) for every dict entry, depth first."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path, k, v
            yield from walk(v, path + (k,))
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, path)


def find_stories(obj):
    """Yield dicts that look like a feed story (have post_id + comet_sections)."""
    if isinstance(obj, dict):
        if "post_id" in obj and ("comet_sections" in obj or "attachments" in obj):
            yield obj
            return
        for v in obj.values():
            yield from find_stories(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_stories(v)


def find_page_info(obj):
    for _, k, v in walk(obj):
        if k == "page_info" and isinstance(v, dict) and "end_cursor" in v:
            return v
    return None


def extract_post(story: dict) -> dict | None:
    post_id = str(story.get("post_id") or "")
    if not post_id:
        return None

    text = None
    creation = None
    images, seen = [], set()
    url = None
    for path, k, v in walk(story):
        if text is None and k == "message" and isinstance(v, dict) and isinstance(v.get("text"), str):
            text = v["text"]
        elif creation is None and k == "creation_time" and isinstance(v, int):
            creation = v
        elif url is None and k in ("wwwURL", "url") and isinstance(v, str) and post_id in v:
            url = v
        elif k in ("photo_image", "viewer_image", "image", "large_share_image") and isinstance(v, dict):
            uri = v.get("uri")
            if (isinstance(uri, str) and "fbcdn" in uri
                    and any(p in ("attachments", "attachment", "media", "all_subattachments")
                            for p in path)
                    and (v.get("width") or 999) >= 200):
                key = uri.split("?")[0].rsplit("/", 1)[-1]
                if key not in seen:
                    seen.add(key)
                    images.append(uri)

    when = dt.datetime.fromtimestamp(creation, TZ_TW).isoformat() if creation else None
    return {
        "post_id": post_id,
        "post_url": url or f"https://www.facebook.com/{post_id}",
        "time": when,
        "timestamp": creation,
        "text": text,
        "image_urls": images,
    }


def parse_lines(body: str):
    """Facebook GraphQL bodies are newline-separated JSON objects."""
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("for (;;);"):
            line = line[len("for (;;);"):]
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


# ── browser step: capture initial GraphQL payload ────────────────────────────

def capture_init_payload(slug: str):
    """Return (doc_id, variables, captured_bodies) or (None, None, bodies)."""
    from playwright.sync_api import sync_playwright

    requests_seen, bodies = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            locale="zh-TW",
            viewport={"width": 1280, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
        )
        page = ctx.new_page()

        def on_request(req):
            if req.url.startswith(GRAPHQL_URL) and req.method == "POST":
                requests_seen.append(req.post_data or "")

        def on_response(resp):
            if resp.url.startswith(GRAPHQL_URL):
                try:
                    bodies.append(resp.text())
                except Exception:  # noqa: BLE001
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            page.goto("https://www.facebook.com/", timeout=45000, wait_until="domcontentloaded")
            time.sleep(1)
            page.goto(f"https://www.facebook.com/{slug}", timeout=60000, wait_until="domcontentloaded")
            time.sleep(3)
            dismiss_login(page)
            for _ in range(40):
                if pick_timeline_payload(requests_seen):
                    break
                page.mouse.wheel(0, 3000)
                time.sleep(0.7)
                if _ % 8 == 7:
                    dismiss_login(page)
            time.sleep(2)
            if not pick_timeline_payload(requests_seen):
                DEBUG.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(DEBUG / f"{slug}_screen.png"), full_page=False)
                (DEBUG / f"{slug}_page.html").write_text(page.content()[:400000], encoding="utf-8")
                logging.warning("[%s] saved debug screenshot/html; graphql requests seen=%d",
                                slug, len(requests_seen))
        finally:
            ctx.close()
            browser.close()

    found = pick_timeline_payload(requests_seen)
    if not found:
        return None, None, bodies
    return found[0], found[1], bodies


def pick_timeline_payload(raw_posts: list[str]):
    """Find the timeline-feed request (variables contain id + cursor)."""
    for raw in raw_posts:
        q = parse_qs(raw)
        doc = (q.get("doc_id") or [None])[0]
        var_raw = (q.get("variables") or [None])[0]
        if not doc or not var_raw:
            continue
        try:
            variables = json.loads(var_raw)
        except json.JSONDecodeError:
            continue
        if variables.get("id") and "cursor" in variables:
            return doc, variables
    return None


def dismiss_login(page):
    for sel in ['div[aria-label="關閉"]', 'div[aria-label="Close"]',
                '[role="dialog"] [aria-label="關閉"]', '[role="dialog"] [aria-label="Close"]']:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=500):
                loc.click(timeout=1000)
                time.sleep(0.5)
        except Exception:  # noqa: BLE001
            pass
    try:
        page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass


# ── API step: paginate GraphQL directly ──────────────────────────────────────

def api_paginate(slug, doc_id, variables, days, max_rounds=40):
    session = requests.Session()
    session.headers.update({"User-Agent": "python-requests/2.31.0", "Accept": "*/*"})
    cutoff = time.time() - days * 86400
    cursor, before = None, None
    posts = {}
    saved_sample = False

    for rnd in range(max_rounds):
        v = dict(variables)
        v["cursor"] = cursor
        if "beforeTime" in v:
            v["beforeTime"] = before
        form = {"variables": json.dumps(v, separators=(",", ":")), "doc_id": doc_id}
        try:
            r = session.post(GRAPHQL_URL, data=form, timeout=60)
        except requests.RequestException as e:
            logging.warning("[%s] api round %d failed: %s", slug, rnd, e)
            time.sleep(3)
            continue
        if r.status_code != 200 or not r.text.strip():
            logging.warning("[%s] api HTTP %s, %d bytes; stopping", slug, r.status_code, len(r.text))
            break

        new_here, oldest, page_info = 0, None, None
        for obj in parse_lines(r.text):
            page_info = find_page_info(obj) or page_info
            for story in find_stories(obj):
                post = extract_post(story)
                if not post:
                    continue
                if post["post_id"] not in posts:
                    new_here += 1
                posts[post["post_id"]] = post
                if post["timestamp"]:
                    oldest = min(oldest or post["timestamp"], post["timestamp"])

        if rnd == 0 and new_here == 0 and not saved_sample:
            DEBUG.mkdir(parents=True, exist_ok=True)
            (DEBUG / f"{slug}_api_sample.txt").write_text(r.text[:200000], encoding="utf-8")
            saved_sample = True

        logging.info("[%s] api round %d: +%d posts (total %d) oldest=%s", slug, rnd, new_here,
                     len(posts), dt.datetime.fromtimestamp(oldest, TZ_TW).date() if oldest else None)

        if oldest:
            before = oldest
        if page_info:
            cursor = page_info.get("end_cursor") or cursor
            if page_info.get("has_next_page") is False:
                break
        if oldest and oldest < cutoff:
            break
        if new_here == 0 and rnd > 0:
            break
        time.sleep(1.5)
    return posts


# ── orchestration ────────────────────────────────────────────────────────────

def download_image(url: str, dest: Path) -> bool:
    if dest.exists():
        return True
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        dest.write_bytes(r.content)
        return True
    except Exception as e:  # noqa: BLE001
        logging.warning("image download failed: %s", e)
        return False


def scrape_page(slug: str, days: int) -> int:
    out_file = DATA / "posts" / f"{slug}.json"
    img_dir = DATA / "images" / slug
    out_file.parent.mkdir(parents=True, exist_ok=True)

    existing = {}
    if out_file.exists():
        for p in json.loads(out_file.read_text(encoding="utf-8") or "[]"):
            existing[str(p.get("post_id"))] = p

    doc_id, variables, bodies = capture_init_payload(slug)

    found = {}
    # posts that came back during the browser session itself
    for body in bodies:
        for obj in parse_lines(body):
            for story in find_stories(obj):
                post = extract_post(story)
                if post:
                    found[post["post_id"]] = post
    logging.info("[%s] browser session: %d graphql responses, %d posts", slug, len(bodies), len(found))

    if doc_id:
        logging.info("[%s] timeline doc_id=%s id=%s", slug, doc_id, variables.get("id"))
        found.update(api_paginate(slug, doc_id, variables, days))
    else:
        logging.error("[%s] could not capture the timeline GraphQL request (login wall / blocked?)", slug)

    new_count = 0
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for pid, post in found.items():
        local = []
        if post["image_urls"]:
            img_dir.mkdir(parents=True, exist_ok=True)
        for i, url in enumerate(post["image_urls"][:10]):
            dest = img_dir / f"{pid}_{i}.jpg"
            if download_image(url, dest):
                local.append(str(dest.relative_to(ROOT)))
        post = {**post, "page": slug, "local_images": local, "scraped_at": now}
        post.pop("image_urls", None)
        if pid not in existing:
            new_count += 1
        existing[pid] = {**existing.get(pid, {}), **post}

    posts = sorted(existing.values(), key=lambda p: p.get("timestamp") or 0, reverse=True)
    out_file.write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[%s] done: %d new, %d total", slug, new_count, len(posts))
    for p in posts[:5]:
        logging.info("   %s | imgs=%d | %s", p.get("time"), len(p.get("local_images") or []),
                     (p.get("text") or "").replace("\n", " ")[:60])
    return new_count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", help="page username/URL/id; comma or space separated")
    ap.add_argument("--pages-file", default=str(ROOT / "pages.txt"))
    ap.add_argument("--days", type=int, default=30, help="how many days back to collect")
    args = ap.parse_args()

    setup_logging()
    pages = read_pages(args)
    if not pages:
        logging.warning("no pages given (use --page or pages.txt); nothing to do")
        return

    total = 0
    for slug in pages:
        logging.info("=== scraping %s ===", slug)
        try:
            total += scrape_page(slug, args.days)
        except Exception as e:  # noqa: BLE001
            logging.exception("[%s] failed: %s", slug, e)
    logging.info("ALL DONE: %d new posts across %d pages", total, len(pages))


if __name__ == "__main__":
    main()
