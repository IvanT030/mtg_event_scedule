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


def load_fb_cookies() -> list[dict]:
    """Read FB_COOKIES env: Cookie-Editor JSON export, or 'c_user=..; xs=..' string."""
    import os
    raw = (os.environ.get("FB_COOKIES") or "").strip()
    if not raw:
        return []
    pairs = []
    if raw.startswith("["):
        try:
            for c in json.loads(raw):
                if c.get("name") and c.get("value") is not None:
                    pairs.append((c["name"], str(c["value"])))
        except json.JSONDecodeError:
            logging.error("FB_COOKIES looks like JSON but could not be parsed")
    else:
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                pairs.append((k.strip(), v.strip()))
    names = {k for k, _ in pairs}
    if not {"c_user", "xs"} <= names:
        logging.error("FB_COOKIES is missing c_user and/or xs; ignoring it")
        return []
    return [{"name": k, "value": v, "domain": ".facebook.com", "path": "/",
             "secure": True, "httpOnly": k in ("xs", "fr", "datr", "sb")} for k, v in pairs]


def add_fb_cookies(ctx):
    cookies = load_fb_cookies()
    if cookies:
        ctx.add_cookies(cookies)
        logging.info("logged-in session: using %d cookies from FB_COOKIES", len(cookies))


# ── browser step: capture initial GraphQL payload ────────────────────────────

def capture_init_payload(slug: str, scrolls: int = 30):
    """Return (doc_id, variables, captured_bodies) or (None, None, bodies).

    captured_bodies includes the GraphQL responses the browser received while
    scrolling, plus the JSON embedded in the page HTML (first posts are
    server-rendered there).
    """
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
        add_fb_cookies(ctx)
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
            # server-rendered JSON blobs (contain the first few posts)
            try:
                blobs = page.eval_on_selector_all(
                    'script[type="application/json"]', "els => els.map(e => e.textContent)")
                ssr = [b for b in blobs if b and "post_id" in b]
                bodies.extend(ssr)
                logging.info("[%s] page html: %d json blobs, %d mention post_id", slug, len(blobs), len(ssr))
            except Exception as e:  # noqa: BLE001
                logging.warning("[%s] could not read page json: %s", slug, e)
            dismiss_login(page)
            for i in range(scrolls):
                page.mouse.wheel(0, 2500)
                time.sleep(1.2)
                if i % 6 == 5:
                    dismiss_login(page)
            time.sleep(2)
            DEBUG.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(DEBUG / f"{slug}_screen.png"), full_page=False)
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


# ── photos step: open each photo page (server-rendered, not rate-limited) ───

def find_photo_post(obj, fbid: str):
    """From a photo page's JSON blobs, pull caption, time and full image."""
    text = creation = img = None
    owner_post = None
    for path, k, v in walk(obj):
        if text is None and k == "message" and isinstance(v, dict) and isinstance(v.get("text"), str):
            text = v["text"]
        elif creation is None and k in ("created_time", "creation_time") and isinstance(v, int):
            creation = v
        elif img is None and k == "image" and isinstance(v, dict) and "fbcdn" in str(v.get("uri", "")) \
                and (v.get("width") or 0) >= 400:
            img = v["uri"]
        elif owner_post is None and k == "post_id" and isinstance(v, str):
            owner_post = v
    return text, creation, img, owner_post


def scrape_photos(slug: str, max_photos: int, days: int) -> dict:
    from playwright.sync_api import sync_playwright

    posts = {}
    cutoff = time.time() - days * 86400
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            locale="zh-TW", viewport={"width": 1280, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
        )
        add_fb_cookies(ctx)
        page = ctx.new_page()
        try:
            page.goto(f"https://www.facebook.com/{slug}/photos", timeout=60000, wait_until="domcontentloaded")
            time.sleep(3)
            dismiss_login(page)
            for _ in range(6):
                page.mouse.wheel(0, 2500)
                time.sleep(1.2)
            html = page.content()
            fbids = []
            for m in re.finditer(r"(?:photo/\?fbid=|photo\.php\?fbid=|\"fbid\":\")(\d{8,})", html):
                if m.group(1) not in fbids:
                    fbids.append(m.group(1))
            logging.info("[%s] photos tab: %d photo ids", slug, len(fbids))
            DEBUG.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(DEBUG / f"{slug}_photos_screen.png"))

            for n, fbid in enumerate(fbids[:max_photos]):
                try:
                    page.goto(f"https://www.facebook.com/photo/?fbid={fbid}", timeout=45000,
                              wait_until="domcontentloaded")
                    time.sleep(2)
                    blobs = page.eval_on_selector_all(
                        'script[type="application/json"]', "els => els.map(e => e.textContent)")
                    text = creation = img = owner = None
                    for b in blobs:
                        if fbid not in b:
                            continue
                        try:
                            obj = json.loads(b)
                        except json.JSONDecodeError:
                            continue
                        t, c, i, o = find_photo_post(obj, fbid)
                        text, creation, img, owner = text or t, creation or c, img or i, owner or o
                    if not img:  # fall back to the rendered <img>
                        try:
                            img = page.locator('img[data-visualcompletion="media-vc-image"]').first \
                                .get_attribute("src", timeout=3000)
                        except Exception:  # noqa: BLE001
                            pass
                    if not text:
                        try:
                            meta = page.locator('meta[property="og:description"]').first \
                                .get_attribute("content", timeout=2000)
                            text = meta
                        except Exception:  # noqa: BLE001
                            pass
                    if n == 0:
                        (DEBUG / f"{slug}_photo_page.html").write_text(page.content()[:500000], encoding="utf-8")
                    when = dt.datetime.fromtimestamp(creation, TZ_TW).isoformat() if creation else None
                    logging.info("[%s] photo %d/%d fbid=%s time=%s img=%s text=%r", slug, n + 1,
                                 min(len(fbids), max_photos), fbid, when, bool(img), (text or "")[:50])
                    if not (text or img or creation):
                        logging.info("[%s] photo page is login-walled; stopping photos step", slug)
                        break
                    pid = owner or fbid
                    posts[pid] = {
                        "post_id": pid, "photo_fbid": fbid,
                        "post_url": f"https://www.facebook.com/photo/?fbid={fbid}",
                        "time": when, "timestamp": creation, "text": text,
                        "image_urls": [img] if img else [],
                    }
                    if creation and creation < cutoff:
                        logging.info("[%s] reached photos older than %d days", slug, days)
                        break
                    time.sleep(2)
                except Exception as e:  # noqa: BLE001
                    logging.warning("[%s] photo %s failed: %s", slug, fbid, e)
        finally:
            ctx.close()
            browser.close()
    return posts


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
        if '"errors"' in r.text[:300] and "post_id" not in r.text:
            logging.warning("[%s] api error: %s", slug, r.text[:200])
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
    logging.info("[%s] browser session: %d bodies, %d posts", slug, len(bodies), len(found))
    if not found:
        DEBUG.mkdir(parents=True, exist_ok=True)
        lines = []
        for i, b in enumerate(bodies):
            err = re.search(r'"errors":\[\{"message":"([^"]+)', b)
            lines.append(f"#{i} len={len(b)} post_id={'post_id' in b} "
                         f"creation_time={'creation_time' in b} err={err.group(1) if err else None} "
                         f"head={b[:200]!r}")
        (DEBUG / f"{slug}_bodies_summary.txt").write_text("\n".join(lines), encoding="utf-8")
        with_posts = [b for b in bodies if "post_id" in b]
        if with_posts:
            (DEBUG / f"{slug}_body_with_post_id.txt").write_text(
                max(with_posts, key=len)[:400000], encoding="utf-8")

    if doc_id:
        logging.info("[%s] timeline doc_id=%s id=%s", slug, doc_id, variables.get("id"))
        found.update(api_paginate(slug, doc_id, variables, days))
    else:
        logging.error("[%s] could not capture the timeline GraphQL request (login wall / blocked?)", slug)

    try:
        for pid, post in scrape_photos(slug, max_photos=25, days=days).items():
            found.setdefault(pid, post)
            if not found[pid].get("image_urls") and post["image_urls"]:
                found[pid]["image_urls"] = post["image_urls"]
    except Exception as e:  # noqa: BLE001
        logging.exception("[%s] photos step failed: %s", slug, e)

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
