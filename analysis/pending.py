"""List posts that have not been analyzed yet.

A post is "analyzed" once data/stores/<slug>/analysis/<post_id>.json exists
(for MTG and non-MTG posts alike).

Usage:
    python analysis/pending.py          # human-readable list
    python analysis/pending.py --json   # machine-readable
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORES = ROOT / "data" / "stores"


def pending():
    out = []
    for f in sorted(STORES.glob("*/posts.json")):
        page = f.parent.name
        for p in json.loads(f.read_text(encoding="utf-8") or "[]"):
            pid = str(p.get("post_id"))
            out_path = f.parent / "analysis" / f"{pid}.json"
            if not out_path.exists():
                out.append({
                    "page": page,
                    "store": p.get("store"),
                    "analysis_file": str(out_path.relative_to(ROOT)),
                    "post_id": pid,
                    "time": p.get("time"),
                    "post_url": p.get("post_url"),
                    "text": p.get("text"),
                    "links": p.get("links") or [],
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
        print(f"- [{p['store']}] {p['post_id']}  {p['time']}  imgs={len(p['local_images'])}  {head}")


if __name__ == "__main__":
    main()
