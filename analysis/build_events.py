"""Validate data/stores/*/analysis/*.json and build the event list.

Outputs:
    data/events.json     all MTG events (duplicates merged), sorted by start time
    data/schedule.json   upcoming events only, for the schedule frontend
    data/events.db       SQLite (tables: posts_analyzed, events)

The same event announced in several posts (same store page, start, format,
event_type) is merged into one: highest confidence wins, links are combined,
the other posts are listed in also_posted_in.

Usage:
    python analysis/build_events.py
Exit code 1 if any analysis file is invalid (errors are printed).
"""

import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORES = ROOT / "data" / "stores"
EVENTS_JSON = ROOT / "data" / "events.json"
EVENTS_DB = ROOT / "data" / "events.db"
SCHEDULE_JSON = ROOT / "data" / "schedule.json"
TZ_TW = dt.timezone(dt.timedelta(hours=8))
SCHEDULE_FIELDS = ["event_id", "title", "store", "address", "format", "event_type", "start", "end",
                   "registration_start", "fee", "prizes", "capacity", "links", "notes",
                   "source_url", "also_posted_in", "page", "store_page_name"]

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


def dedupe(events):
    merged = {}
    for e in events:
        key = (e["page"], e["start"], e["format"], e["event_type"])
        if key not in merged:
            merged[key] = {**e, "also_posted_in": []}
            continue
        cur = merged[key]
        a, b = (cur, e) if (cur["confidence"] or 0) >= (e["confidence"] or 0) else (e, cur)
        keep = {**a, "also_posted_in": cur["also_posted_in"]}
        keep["links"] = list(dict.fromkeys((a.get("links") or []) + (b.get("links") or [])))
        if b["source_url"] != keep["source_url"] and b["source_url"] not in keep["also_posted_in"]:
            keep["also_posted_in"].append(b["source_url"])
        merged[key] = keep
    return list(merged.values())


def build() -> dict:
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
    raw_count = len(events)
    events = dedupe(events)
    events.sort(key=lambda e: e["start"] or "")
    EVENTS_JSON.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")

    now = dt.datetime.now(TZ_TW)
    today = now.date().isoformat()
    upcoming = [e for e in events if (e["start"] or "")[:10] >= today]
    SCHEDULE_JSON.write_text(json.dumps({
        "generated_at": now.isoformat(timespec="seconds"),
        "count": len(upcoming),
        "events": [{**{k: e.get(k) for k in SCHEDULE_FIELDS},
                    "needs_review": (e.get("confidence") or 0) < 0.7} for e in upcoming],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if EVENTS_DB.exists():
        EVENTS_DB.unlink()
    db = sqlite3.connect(EVENTS_DB)
    db.execute("""CREATE TABLE posts_analyzed (post_id TEXT PRIMARY KEY, page TEXT, post_url TEXT,
                  post_time TEXT, is_mtg INTEGER, reason TEXT, analyzed_at TEXT)""")
    db.execute(f"""CREATE TABLE events (event_id TEXT PRIMARY KEY, post_id TEXT, page TEXT,
                  store_page_name TEXT, source_url TEXT, also_posted_in TEXT,
                  {', '.join(k + ' TEXT' for k in EVENT_FIELDS)})""")
    db.executemany("INSERT INTO posts_analyzed VALUES (?,?,?,?,?,?,?)", [
        (r["post_id"], r["_page"], r.get("post_url"), r.get("post_time"), int(bool(r.get("is_mtg"))),
         r.get("reason"), r.get("analyzed_at")) for r in records])
    cols = ["event_id", "post_id", "page", "store_page_name", "source_url", "also_posted_in"] + EVENT_FIELDS
    db.executemany(f"INSERT INTO events VALUES ({','.join('?' * len(cols))})", [
        tuple(json.dumps(e[c], ensure_ascii=False) if isinstance(e[c], (list, dict)) else e[c]
              for c in cols) for e in events])
    db.commit()
    db.close()

    mtg = sum(1 for r in records if r.get("is_mtg"))
    low = [e for e in events if (e["confidence"] or 0) < 0.7]
    return {"analysis_files": len(records), "mtg_posts": mtg, "events": len(events),
            "duplicates_merged": raw_count - len(events), "upcoming": len(upcoming),
            "needs_review": [{"event_id": e["event_id"], "start": e["start"], "title": e["title"],
                              "confidence": e["confidence"]} for e in low],
            "invalid": problems}


def main():
    r = build()
    print(f"analysis files: {r['analysis_files']} (MTG {r['mtg_posts']}, "
          f"non-MTG {r['analysis_files'] - r['mtg_posts']})")
    print(f"events: {r['events']} (merged {r['duplicates_merged']} duplicates), upcoming {r['upcoming']}"
          f" -> data/events.json, data/schedule.json, data/events.db")
    if r["needs_review"]:
        print(f"low-confidence events to review: {len(r['needs_review'])}")
        for e in r["needs_review"]:
            print(f"  - {e['event_id']} {e['start']} {e['title']} (confidence {e['confidence']})")
    if r["invalid"]:
        print("\nINVALID:")
        for p in r["invalid"]:
            print("  " + p)
        sys.exit(1)


if __name__ == "__main__":
    main()
