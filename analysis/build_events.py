"""Validate data/stores/*/analysis/*.json and build the event list.

Outputs:
    data/events.json   all MTG events, sorted by start time
    data/events.db     SQLite (tables: posts_analyzed, events)

Usage:
    python analysis/build_events.py
Exit code 1 if any analysis file is invalid (errors are printed).
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORES = ROOT / "data" / "stores"
EVENTS_JSON = ROOT / "data" / "events.json"
EVENTS_DB = ROOT / "data" / "events.db"

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?\+08:00)?$")
FORMATS = {"Standard", "Pioneer", "Modern", "Legacy", "Vintage", "Pauper", "Commander",
           "Duel Commander", "cEDH", "Draft", "Sealed", "Prerelease", "Two-Headed Giant",
           "Alchemy", "Historic", "Other", None}
EVENT_FIELDS = ["title", "store", "address", "format", "event_type", "start", "end",
                "registration_start", "fee", "prizes", "capacity", "links", "notes", "confidence", "evidence"]


def validate(rec: dict, path: Path) -> list[str]:
    errs = []
    if "post_id" not in rec or "is_mtg" not in rec:
        errs.append("missing post_id / is_mtg")
    if rec.get("post_id") and path.stem != str(rec["post_id"]):
        errs.append(f"file name {path.stem} != post_id {rec['post_id']}")
    if not rec.get("is_mtg"):
        return errs
    events = rec.get("events")
    if not isinstance(events, list):
        return errs + ["is_mtg=true but events is not a list"]
    for i, e in enumerate(events):
        for k in ("start", "end", "registration_start"):
            v = e.get(k)
            if v is not None and not ISO_RE.match(v):
                errs.append(f"events[{i}].{k} not ISO with +08:00: {v!r}")
        if not e.get("start"):
            errs.append(f"events[{i}].start is required")
        if e.get("format") not in FORMATS:
            errs.append(f"events[{i}].format not in allowed list: {e.get('format')!r}")
        c = e.get("confidence")
        if not isinstance(c, (int, float)) or not 0 <= c <= 1:
            errs.append(f"events[{i}].confidence must be 0..1")
        if not isinstance(e.get("links", []), list):
            errs.append(f"events[{i}].links must be a list")
        if e.get("fee") is not None and not isinstance(e["fee"], int):
            errs.append(f"events[{i}].fee must be an integer (TWD) or null")
    return errs


def main():
    records, problems = [], []
    store_names = {}
    for meta in STORES.glob("*/store.json"):
        store_names[meta.parent.name] = json.loads(meta.read_text(encoding="utf-8")).get("name")
    for f in sorted(STORES.glob("*/analysis/*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            problems.append(f"{f.relative_to(ROOT)}: invalid JSON: {e}")
            continue
        errs = validate(rec, f)
        problems += [f"{f.relative_to(ROOT)}: {e}" for e in errs]
        rec["_page"] = f.parent.parent.name
        records.append(rec)

    events = []
    for rec in records:
        if not rec.get("is_mtg"):
            continue
        for i, e in enumerate(rec.get("events") or []):
            events.append({"event_id": f"{rec['post_id']}#{i}", "post_id": rec["post_id"],
                           "page": rec["_page"], "store_page_name": store_names.get(rec["_page"]),
                           "source_url": rec.get("post_url"),
                           **{k: e.get(k) for k in EVENT_FIELDS}})
    events.sort(key=lambda e: e["start"] or "")
    EVENTS_JSON.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")

    if EVENTS_DB.exists():
        EVENTS_DB.unlink()
    db = sqlite3.connect(EVENTS_DB)
    db.execute("""CREATE TABLE posts_analyzed (post_id TEXT PRIMARY KEY, page TEXT, post_url TEXT,
                  post_time TEXT, is_mtg INTEGER, reason TEXT, analyzed_at TEXT)""")
    db.execute(f"""CREATE TABLE events (event_id TEXT PRIMARY KEY, post_id TEXT, page TEXT,
                  store_page_name TEXT, source_url TEXT, {', '.join(k + ' TEXT' for k in EVENT_FIELDS)})""")
    db.executemany("INSERT INTO posts_analyzed VALUES (?,?,?,?,?,?,?)", [
        (r["post_id"], r["_page"], r.get("post_url"), r.get("post_time"), int(bool(r.get("is_mtg"))),
         r.get("reason"), r.get("analyzed_at")) for r in records])
    cols = ["event_id", "post_id", "page", "store_page_name", "source_url"] + EVENT_FIELDS
    db.executemany(f"INSERT INTO events VALUES ({','.join('?' * len(cols))})", [
        tuple(json.dumps(e[c], ensure_ascii=False) if isinstance(e[c], (list, dict)) else e[c]
              for c in cols) for e in events])
    db.commit()
    db.close()

    mtg = sum(1 for r in records if r.get("is_mtg"))
    print(f"analysis files: {len(records)} (MTG {mtg}, non-MTG {len(records) - mtg})")
    print(f"events: {len(events)} -> {EVENTS_JSON.relative_to(ROOT)}, {EVENTS_DB.relative_to(ROOT)}")
    low = [e for e in events if (e["confidence"] or 0) < 0.7]
    if low:
        print(f"low-confidence events to review: {len(low)}")
        for e in low:
            print(f"  - {e['event_id']} {e['start']} {e['title']} (confidence {e['confidence']})")
    if problems:
        print("\nINVALID:")
        for p in problems:
            print("  " + p)
        sys.exit(1)


if __name__ == "__main__":
    main()
