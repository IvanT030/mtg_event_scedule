"""One-shot pipeline for the website backend: scrape → analyze → build schedule.

    python run_pipeline.py --cookies cookies.json

Steps
  1. scrape every page in pages.txt with the logged-in FB session; stops at
     already-saved posts, so only new posts are added (no duplicates)
  2. analyze posts that have no analysis file yet (LLM, see analysis/analyze.py)
  3. rebuild data/events.json, data/schedule.json, data/events.db

Prints one JSON summary on stdout (logs go to stderr and data/last_run.log).
Exit codes: 0 ok · 1 some step had errors (see summary) · 2 FB session blocked
            · 3 another run is already in progress

Options
  --cookies FILE   Cookie-Editor JSON export (or set FB_COOKIES_FILE)
  --days N         look back N days when scraping (default 30)
  --skip-scrape    only analyze + build
  --skip-analyze   only scrape + build
  --provider/--model   LLM selection (default: anthropic / claude-haiku-5-5)
"""

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scraper"))
sys.path.insert(0, str(ROOT / "analysis"))

LOCK = ROOT / "data" / ".pipeline.lock"
TZ_TW = dt.timezone(dt.timedelta(hours=8))


def acquire_lock():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        # stale lock (older than 2 hours) is taken over
        if time.time() - LOCK.stat().st_mtime > 7200:
            LOCK.unlink(missing_ok=True)
            return acquire_lock()
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--skip-scrape", action="store_true")
    ap.add_argument("--skip-analyze", action="store_true")
    ap.add_argument("--provider")
    ap.add_argument("--model")
    args = ap.parse_args()

    import fetch_posts
    fetch_posts.setup_logging()
    # keep stdout clean for the JSON summary
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is sys.stdout:
            h.setStream(sys.stderr)

    if not acquire_lock():
        print(json.dumps({"ok": False, "error": "another run is in progress"}))
        sys.exit(3)

    started = dt.datetime.now(TZ_TW)
    summary = {"ok": True, "started_at": started.isoformat(timespec="seconds"),
               "scrape": None, "analysis": None, "build": None}
    code = 0
    try:
        # 1. scrape
        if not args.skip_scrape:
            cookie_file = args.cookies or os.environ.get("FB_COOKIES_FILE")
            if not cookie_file:
                raise SystemExit("--cookies (or FB_COOKIES_FILE) is required unless --skip-scrape")
            os.environ["FB_COOKIES"] = Path(cookie_file).read_text(encoding="utf-8")
            pages = fetch_posts.read_pages(type("A", (), {"page": None,
                                                          "pages_file": str(ROOT / "pages.txt")}))
            per_store = {}
            for slug, store in pages:
                try:
                    per_store[slug] = fetch_posts.scrape_page(slug, store, args.days)
                except fetch_posts.SessionBlocked as e:
                    logging.error("%s", e)
                    summary.update(ok=False, error=f"FB session blocked: {e}")
                    summary["scrape"] = {"new_posts": per_store, "blocked": True}
                    code = 2
                    break
                except Exception as e:  # noqa: BLE001
                    logging.exception("scrape %s failed", slug)
                    per_store[slug] = f"error: {e}"
                    code = 1
            if code != 2:
                summary["scrape"] = {"new_posts": per_store,
                                     "total_new": sum(v for v in per_store.values() if isinstance(v, int))}

        # 2. analyze
        if not args.skip_analyze and code != 2:
            import analyze
            a = analyze.run(args.provider, args.model)
            summary["analysis"] = a
            if a["failed"]:
                code = code or 1

        # 3. build
        import build_events
        b = build_events.build()
        summary["build"] = {k: v for k, v in b.items()}
        if b["invalid"]:
            code = code or 1
    finally:
        LOCK.unlink(missing_ok=True)

    summary["ok"] = summary["ok"] and code == 0
    summary["finished_at"] = dt.datetime.now(TZ_TW).isoformat(timespec="seconds")
    summary["outputs"] = {"schedule": "data/schedule.json", "events": "data/events.json",
                          "db": "data/events.db"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    sys.exit(code)


if __name__ == "__main__":
    main()
