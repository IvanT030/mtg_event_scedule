"""Fetch recent posts (text + images) from public Facebook pages.

Uses kevinzg/facebook-scraper. Results are merged into data/posts/<page>.json
and images saved to data/images/<page>/. A run log is written to
data/last_run.log so the outcome can be inspected from the repo.

Usage:
    python scraper/fetch_posts.py --pages-file pages.txt --pages 3
    python scraper/fetch_posts.py --page cardmaster --pages 3
"""

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOG_FILE = DATA / "last_run.log"


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
    """Turn a URL / username / id into a short identifier."""
    page = page.strip()
    m = re.search(r"facebook\.com/(?:profile\.php\?id=)?([^/?&#]+)", page)
    if m:
        page = m.group(1)
    return page.strip("/")


def read_pages(args) -> list[str]:
    pages = []
    if args.page:
        pages += [p for p in re.split(r"[,\s]+", args.page) if p]
    if args.pages_file and Path(args.pages_file).exists():
        for line in Path(args.pages_file).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                pages.append(line)
    slugs = []
    for p in pages:
        s = page_slug(p)
        if s in ("share", "sharer", "sharer.php", "story.php", "permalink.php", "groups", "watch"):
            logging.error("無法使用的網址：%s —— 這是分享/貼文連結，請改用粉專首頁網址，"
                          "例如 https://www.facebook.com/cardmaster.tw/", p)
            continue
        slugs.append(s)
    return slugs


def to_jsonable(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


KEEP_FIELDS = [
    "post_id", "post_url", "time", "timestamp", "text", "post_text",
    "shared_text", "images", "image", "images_description", "link",
    "username", "page_id", "w3_fb_url",
]


def download_image(url: str, dest: Path) -> bool:
    if dest.exists():
        return True
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        dest.write_bytes(r.content)
        return True
    except Exception as e:  # noqa: BLE001
        logging.warning("image download failed %s: %s", url[:80], e)
        return False


def scrape_page(slug: str, n_pages: int, cookies: str | None) -> int:
    from facebook_scraper import get_posts

    out_file = DATA / "posts" / f"{slug}.json"
    img_dir = DATA / "images" / slug
    out_file.parent.mkdir(parents=True, exist_ok=True)
    img_dir.mkdir(parents=True, exist_ok=True)

    existing = {}
    if out_file.exists():
        for p in json.loads(out_file.read_text(encoding="utf-8")):
            existing[str(p.get("post_id"))] = p

    kwargs = {"pages": n_pages, "options": {"allow_extra_requests": True}}
    if cookies:
        kwargs["cookies"] = cookies

    new_count = 0
    try:
        for post in get_posts(slug, **kwargs):
            pid = str(post.get("post_id"))
            row = {k: to_jsonable(post.get(k)) for k in KEEP_FIELDS if post.get(k) is not None}
            row["page"] = slug
            row["scraped_at"] = dt.datetime.now(dt.timezone.utc).isoformat()

            local_imgs = []
            urls = post.get("images") or ([post["image"]] if post.get("image") else [])
            for i, url in enumerate(urls):
                dest = img_dir / f"{pid}_{i}.jpg"
                if download_image(url, dest):
                    local_imgs.append(str(dest.relative_to(ROOT)))
            row["local_images"] = local_imgs

            if pid not in existing:
                new_count += 1
            existing[pid] = {**existing.get(pid, {}), **row}
            logging.info("[%s] post %s  time=%s  imgs=%d  text=%r",
                         slug, pid, row.get("time"), len(local_imgs),
                         (row.get("text") or "")[:60])
            time.sleep(2)
    except Exception as e:  # noqa: BLE001
        logging.error("[%s] scrape error: %s: %s", slug, type(e).__name__, e)

    posts = sorted(existing.values(), key=lambda p: p.get("time") or "", reverse=True)
    out_file.write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[%s] done: %d new, %d total", slug, new_count, len(posts))
    return new_count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", help="page username/URL/id; comma or space separated")
    ap.add_argument("--pages-file", default=str(ROOT / "pages.txt"))
    ap.add_argument("--pages", type=int, default=3, help="how many result pages to fetch per FB page")
    args = ap.parse_args()

    setup_logging()
    cookies_path = None
    if os.environ.get("FB_COOKIES"):
        cookies_path = str(ROOT / ".fb_cookies.txt")
        Path(cookies_path).write_text(os.environ["FB_COOKIES"], encoding="utf-8")
        logging.info("using cookies from FB_COOKIES secret")

    pages = read_pages(args)
    if not pages:
        logging.warning("no pages given (use --page or pages.txt); nothing to do")
        return

    total = 0
    for slug in pages:
        logging.info("=== scraping %s ===", slug)
        total += scrape_page(slug, args.pages, cookies_path)
    logging.info("ALL DONE: %d new posts across %d pages", total, len(pages))


if __name__ == "__main__":
    main()
