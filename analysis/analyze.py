"""Analyze pending posts with an LLM (text + images) and write analysis files.

For every post without data/stores/<slug>/analysis/<post_id>.json:
  - send the post text, links and images to the model, with the rules from
    CLAUDE.md ("分析規範" section) as instructions
  - validate the returned JSON (same checks as build_events.py); retry once
  - write data/stores/<slug>/analysis/<post_id>.json

Providers (pick with --provider or LLM_PROVIDER; default claude-code):
  claude-code  runs `claude -p` (Claude Code CLI) signed in with your Claude plan,
               so usage comes from your subscription, not an API key.
               model: LLM_MODEL (optional, passed as --model)
  anthropic    needs ANTHROPIC_API_KEY   model: LLM_MODEL (default claude-haiku-5-5)
  gemini       needs GEMINI_API_KEY      model: LLM_MODEL (default gemini-flash-lite-latest)

Usage:
    python analysis/analyze.py              # analyze all pending posts
    python analysis/analyze.py --limit 5    # at most 5
    python analysis/analyze.py --dry-run    # show what would be sent
"""

import argparse
import base64
import datetime as dt
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import shutil
import subprocess

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_events import validate  # noqa: E402
from pending import pending  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TZ_TW = dt.timezone(dt.timedelta(hours=8))
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 4_500_000


def load_rules() -> str:
    """The analysis rules live in CLAUDE.md so there is one source of truth."""
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    m = re.search(r"## 分析規範.*?(?=\n### 分析完之後)", text, re.S)
    if not m:
        raise RuntimeError("could not find the 分析規範 section in CLAUDE.md")
    return m.group(0)


SYSTEM_TMPL = """你是卡牌店貼文分析器。依照下面的規範判斷貼文是不是 MTG 賽事，並輸出一個 JSON 物件。

{rules}

輸出要求：
- 只輸出一個 JSON 物件，不要有任何說明文字或 ```。
- post_id、post_url、post_time 照抄輸入值；analyzed_at 填 {now}。
- 非 MTG 時只需 post_id、post_url、post_time、is_mtg、reason、analyzed_at。
- 「用 Read 工具打開圖片」這句在這裡不適用：圖片已直接附在訊息裡，請仔細看每一張。
"""


def build_user_text(post: dict) -> str:
    return json.dumps({
        "post_id": post["post_id"],
        "post_url": post["post_url"],
        "post_time": post["time"],
        "store_page": post["store"],
        "text": post["text"],
        "links": post["links"],
        "image_count": len(post["images"]),
    }, ensure_ascii=False, indent=2)


def load_images(paths):
    imgs = []
    for p in paths[:MAX_IMAGES]:
        f = ROOT / p
        if f.exists() and f.stat().st_size <= MAX_IMAGE_BYTES:
            raw = f.read_bytes()
            mime = "image/png" if raw[:4] == b"\x89PNG" else "image/jpeg"
            imgs.append((mime, base64.b64encode(raw).decode()))
    return imgs


# ── providers ────────────────────────────────────────────────────────────────

def call_anthropic(system: str, user_text: str, images, model: str) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    content = [{"type": "image", "source": {"type": "base64", "media_type": m, "data": d}}
               for m, d in images]
    content.append({"type": "text", "text": user_text})
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": 2000, "system": system,
              "messages": [{"role": "user", "content": content}]},
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"anthropic HTTP {r.status_code}: {r.text[:300]}")
    return "".join(b.get("text", "") for b in r.json().get("content", []))


def call_gemini(system: str, user_text: str, images, model: str) -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    parts = [{"inline_data": {"mime_type": m, "data": d}} for m, d in images]
    parts.append({"text": user_text})
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": key, "content-type": "application/json"},
        json={"system_instruction": {"parts": [{"text": system}]},
              "contents": [{"role": "user", "parts": parts}],
              "generationConfig": {"response_mime_type": "application/json"}},
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"gemini HTTP {r.status_code}: {r.text[:300]}")
    cands = r.json().get("candidates") or [{}]
    return "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))


CLAUDE_CODE_PROMPT = """依照 CLAUDE.md 的「分析規範」，分析所有待分析的貼文。

步驟：
1. 執行 `python analysis/pending.py --json` 取得待分析清單（每篇有 analysis_file 路徑）。
2. 逐篇讀 text 與 links，並用 Read 工具打開 local_images 的每一張圖。
3. 依規範把結果寫到該篇的 analysis_file（非 MTG 也要寫）。analyzed_at 用 {now}。
4. 全部寫完後執行 `python analysis/build_events.py`，若驗證失敗就修正分析檔直到通過。
5. 不要修改 data/stores/*/posts.json，不要執行 git，不要動其他檔案。
最後用一行回報：分析了幾篇、其中幾篇是 MTG。"""


def run_claude_code(model: str | None, now: str, timeout: int = 1800) -> dict:
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("找不到 claude 指令：請先安裝 Claude Code 並用 `claude` 登入你的 Claude 帳號")
    before = {p["post_id"] for p in pending()}
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)   # make sure it bills the plan, not the API
    py = sys.executable
    cmd = [exe, "-p", CLAUDE_CODE_PROMPT.format(now=now).replace("`python ", f"`{py} "),
           "--allowedTools",
           f"Read,Write,Bash({py} analysis/pending.py:*),Bash({py} analysis/build_events.py:*),"
           "Bash(python analysis/pending.py:*),Bash(python analysis/build_events.py:*)",
           "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    logging.info("analysis: running Claude Code on %d pending post(s)", len(before))
    if not before:
        return {"pending": 0, "analyzed": 0, "mtg_posts": 0, "failed": []}
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    result_text = proc.stdout
    try:
        result_text = json.loads(proc.stdout).get("result", proc.stdout)
    except (json.JSONDecodeError, AttributeError):
        pass
    logging.info("claude code exit=%s: %s", proc.returncode, str(result_text).strip()[:300])
    if proc.returncode != 0:
        logging.error("claude code stderr: %s", proc.stderr[-1000:])

    still = {p["post_id"] for p in pending()}
    done = before - still
    mtg = 0
    for f in (ROOT / "data" / "stores").glob("*/analysis/*.json"):
        if f.stem in done:
            try:
                mtg += int(bool(json.loads(f.read_text(encoding="utf-8")).get("is_mtg")))
            except json.JSONDecodeError:
                pass
    failed = [{"post_id": pid, "error": "not analyzed by Claude Code"} for pid in sorted(still & before)]
    if proc.returncode != 0 and not failed:
        failed = [{"post_id": None, "error": f"claude exited {proc.returncode}: {proc.stderr[-300:]}"}]
    return {"pending": len(before), "analyzed": len(done), "mtg_posts": mtg, "failed": failed,
            "claude_code_result": str(result_text).strip()[:500]}


PROVIDERS = {
    "anthropic": (call_anthropic, "claude-haiku-5-5"),
    "gemini": (call_gemini, "gemini-flash-lite-latest"),
}


def parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


# ── main loop ────────────────────────────────────────────────────────────────

def analyze_post(post, call, model, system) -> dict:
    images = load_images(post["images"])
    user = build_user_text(post)
    out_path = ROOT / post["analysis_file"]
    last_err = None
    for attempt in range(2):
        msg = user if attempt == 0 else (
            user + f"\n\n上一次的輸出不符合規範：{last_err}\n請修正後重新輸出完整 JSON。")
        raw = call(system, msg, images, model)
        try:
            rec = parse_json(raw)
        except (json.JSONDecodeError, ValueError) as e:
            last_err = f"不是合法 JSON：{e}"
            continue
        rec["post_id"] = post["post_id"]
        rec.setdefault("post_url", post["post_url"])
        rec.setdefault("post_time", post["time"])
        rec["model"] = model
        errs = validate(rec, out_path)
        if not errs:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return rec
        last_err = "; ".join(errs)
    raise RuntimeError(f"invalid output after retry: {last_err}")


def run(provider: str = None, model: str = None, limit: int = None, dry_run: bool = False) -> dict:
    provider = provider or os.environ.get("LLM_PROVIDER", "claude-code")
    now = dt.datetime.now(TZ_TW).isoformat(timespec="seconds")
    if provider == "claude-code":
        if dry_run:
            return {"pending": len(pending()), "analyzed": 0, "mtg_posts": 0, "failed": []}
        return run_claude_code(model or os.environ.get("LLM_MODEL"), now)
    call, default_model = PROVIDERS[provider]
    model = model or os.environ.get("LLM_MODEL") or default_model
    system = SYSTEM_TMPL.format(rules=load_rules(), now=now)

    items = pending()
    for p in items:
        p["images"] = p.pop("local_images")
    if limit:
        items = items[:limit]
    summary = {"pending": len(items), "analyzed": 0, "mtg_posts": 0, "failed": []}
    logging.info("analysis: %d pending post(s), provider=%s model=%s", len(items), provider, model)

    for post in items:
        if dry_run:
            logging.info("[dry-run] %s %s imgs=%d", post["page"], post["post_id"], len(post["images"]))
            continue
        try:
            rec = analyze_post(post, call, model, system)
            summary["analyzed"] += 1
            summary["mtg_posts"] += int(bool(rec.get("is_mtg")))
            logging.info("analyzed %s/%s is_mtg=%s %s", post["page"], post["post_id"],
                         rec.get("is_mtg"), rec.get("reason"))
        except Exception as e:  # noqa: BLE001
            summary["failed"].append({"post_id": post["post_id"], "error": str(e)[:300]})
            logging.error("analysis failed %s/%s: %s", post["page"], post["post_id"], e)
        time.sleep(1)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["claude-code", *PROVIDERS])
    ap.add_argument("--model")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    summary = run(args.provider, args.model, args.limit, args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    sys.exit(1 if summary["failed"] else 0)


if __name__ == "__main__":
    main()
