"""List posts that have not been analyzed yet.

A post is "analyzed" once data/analysis/<page>/<post_id>.json exists
(for MTG and non-MTG posts alike).

Usage:
    python analysis/pending.py          # human-readable list
    python analysis/pending.py --json   # machine-readable
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POSTS = ROOT / "data" / "posts"
ANALYSIS = ROOT / "data" / "analysis"


def pending():
    out = []
    for f in sorted(POSTS.glob("*.json")):
        page = f.stem
        for p in json.loads(f.read_text(encoding="utf-8") or "[]"):
            pid = str(p.get("post_id"))
            if not (ANALYSIS / page / f"{pid}.json").exists():
                out.append({
                    "page": page,
                    "post_id": pid,
                    "time": p.get("time"),
                    "post_url": p.get("post_url"),
                    "text": p.get("text"),
                    "local_images": p.get("local_images") or [],
                })
    out.sort(key=lambda p: p["time"] or "")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    items = pending()
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    print(f"{len(items)} post(s) waiting for analysis")
    for p in items:
        head = (p["text"] or "").replace("\n", " ")[:50]
        print(f"- {p['page']}/{p['post_id']}  {p['time']}  imgs={len(p['local_images'])}  {head}")


if __name__ == "__main__":
    main()
