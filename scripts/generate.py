#!/usr/bin/env python3
"""moneymarket generator: collect -> quotes -> editorial curation -> render -> verify.

  python3 scripts/generate.py                 # today's run, both languages
  python3 scripts/generate.py --date 2026-10-18 --force
  python3 scripts/generate.py --skip-collect  # reuse cache, re-curate and re-render

The editorial step is the only part that needs judgement, so it is delegated to a
headless `hermes -z` run with the moneymarket-brief skill preloaded. Everything
around it — fetching, rendering, verification, logging — stays deterministic Python
so a bad editorial run fails loudly instead of publishing quietly-broken HTML.

Writes logs/generate.log (append) and logs/last_run.json (machine-readable status
for the web service).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as cfgmod  # noqa: E402

ROOT = cfgmod.ROOT
REPORTS = ROOT / "reports"
DATA = REPORTS / "data"
CACHE = ROOT / "cache"
LOGS = ROOT / "logs"
LOCK = CACHE / "generate.lock"
STATUS = LOGS / "last_run.json"

TEMPLATE = {"en": ROOT / "template.html", "zh": ROOT / "template.zh.html"}
LANG_NAME = {"en": "English", "zh": "Chinese"}

SCHEMA = """Selection JSON (one file per language) must be valid JSON with exactly:
{
  "date": "YYYY-MM-DD",
  "window": "human readable coverage window with the as-of UTC timestamp",
  "exec_summary_html": "<p>...</p><ul><li>...</li></ul>",
  "lanes": [
    {"id": "stock", "intro": "one sentence",
     "items": [{"title": "...", "summary": "2-4 sentences",
                "source": "outlet name(s)", "url": "https://...",
                "time": "ISO-ish UTC timestamp or as-of note",
                "confidence": "confirmed | reported | single"}]}
    // one object per lane, ids exactly: stock, money, tech, politics
  ],
  "watchlist_html": "<ul><li>...</li></ul>",
  "sources_html": "<p>...</p>"
}
The Chinese file additionally sets
  "tag_labels": {"confirmed": "已证实", "reported": "据报道", "single": "单一来源"}
and
  "ui_labels": {"na": "无", "daily_fix": "每日定价", "change_24h": "24小时变动",
                "live": "实时", "suffix_24h": "（24小时）"}
"""

RULES = """Hard rules for this brief:
- Four lanes, ids exactly stock / money / tech / politics. 4-7 items each.
- Every number carries a source. Never invent, interpolate, or carry a figure over
  from a previous report.
- Index, futures, vol and crypto levels/percentages must be measured directly (Yahoo
  chart API v8, FRED, ECB, CoinGecko), not copied from press prose.
- Tag each item: confirmed (2+ independent outlets), reported (one reputable outlet),
  single (unconfirmed single outlet).
- Check the as-of date on any Treasury/FRED figure; on a weekend it lags and the
  report must say so.
- Feed URL slugs can be stale; verify figures against headline and body text.
- exec_summary_html: 5-7 bullets, the market-moving facts first.
- sources_html: state plainly which articles were read in full and which rest on
  feed headlines, plus known data defects for this run.
- The Chinese file is written as Chinese financial prose, not a literal translation,
  and keeps every number, URL, source name and timestamp byte-identical to English.
"""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log_line(text: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    stamp = now_utc().strftime("%Y-%m-%d %H:%M:%SZ")
    line = f"{stamp} {text}"
    with (LOGS / "generate.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def run(cmd: list[str], timeout: int, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), timeout=timeout, capture_output=True, text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


class Step:
    def __init__(self, name: str, started: float):
        self.name, self.started = name, started
        self.ok, self.detail = False, ""

    def finish(self, ok: bool, detail: str = "") -> "Step":
        self.ok, self.detail = ok, detail.strip()[:2000]
        log_line(f"[{self.name}] {'ok' if ok else 'FAIL'} {self.detail[:300]}")
        return self

    @property
    def seconds(self) -> float:
        return round(time.time() - self.started, 1)

    def as_dict(self) -> dict:
        return {"step": self.name, "ok": self.ok, "seconds": self.seconds,
                "detail": self.detail}


def step(name: str) -> Step:
    return Step(name, time.time())


def collect_and_quotes(cfg: dict, hours: int) -> list[dict]:
    steps = []
    py = cfg["PYTHON_BIN"]

    s = step("quotes")
    r = run([py, "scripts/quotes.py", "--out", "cache/quotes.json"], 600)
    steps.append(s.finish(r.returncode == 0, r.stdout or r.stderr).as_dict())

    s = step("collect")
    cmd = [py, "scripts/collect.py", "--hours", str(hours), "--out", "cache/collect.json"]
    for lane in cfgmod.get_list(cfg, "LANES"):
        cmd += ["--lane", lane]
    r = run(cmd, 900)
    steps.append(s.finish(r.returncode == 0, r.stdout or r.stderr).as_dict())
    return steps


def editorial_prompt(cfg: dict, date: str, langs: list[str]) -> str:
    files = ", ".join(f"reports/data/{date}{'' if lang == 'en' else '-zh'}.json" for lang in langs)
    notes = f"\nAdditional instructions from the human:\n{cfg['EDITOR_NOTES']}\n" if cfg.get("EDITOR_NOTES") else ""
    return f"""Generate the moneymarket brief for {date} in {', '.join(LANG_NAME[l] for l in langs)}.

Work in {ROOT}. Follow the moneymarket-brief skill.

Already done for you (do not re-run): cache/quotes.json and cache/collect.json are
fresh. Read both. cache/collect.json holds deduped candidate items per lane; it is
candidates only, not a report.

Your job is the editorial step: write {files} — the curated selection JSON — and
nothing else. Do NOT run scripts/render.py; the caller renders and verifies.

{SCHEMA}
{RULES}
Read the 2-4 most important articles in full with the browser tool and say in
sources_html which ones you read. web_extract has no fetch backend on this host;
Bloomberg is bot-walled and WSJ/NYT are paywalled, so label anything you could not
open as resting on its feed headline. If the browser tool is unavailable in this
unattended run, do not stall or fail: write the brief from feed headlines, state
plainly in sources_html that no article could be opened, and lower the confidence
of anything that needed a body-text check.

Verify your own output before finishing: each file parses as JSON, has all four lane
ids, every item has title/summary/source/url/time/confidence, and confidence is one
of confirmed/reported/single.

URLs are checked mechanically before the report is published, and a 404 fails the run.
So copy each item's url verbatim from the "link" field of cache/collect.json — never
retype, shorten, prettify or invent a slug, and never submit a URL you are unsure is
complete. If you opened the story in the browser at a different address, use exactly
the address the browser shows.
{notes}"""


def curate(cfg: dict, date: str, langs: list[str]) -> dict:
    s = step("curate")
    editor = cfg["EDITOR"].strip().lower()
    if editor in {"none", "off", ""}:
        return s.finish(True, "EDITOR=none; curation skipped").as_dict()

    hermes = Path(cfg["HERMES_BIN"]).expanduser()
    if not shutil.which(str(hermes)):
        return s.finish(False, f"hermes not found at {hermes}").as_dict()

    cmd = [
        str(hermes), "-z", editorial_prompt(cfg, date, langs),
        "--in", str(ROOT),
        "-s", "moneymarket-brief",
        "--usage-file", str(LOGS / f"usage-{date}.json"),
    ]
    if cfg.get("EDITOR_TOOLSETS"):
        cmd += ["-t", cfg["EDITOR_TOOLSETS"]]

    LOGS.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), timeout=cfgmod.get_int(cfg, "EDITOR_TIMEOUT", 2400),
                           capture_output=True, text=True,
                           env={**os.environ, "PYTHONUNBUFFERED": "1"})
    except subprocess.TimeoutExpired:
        return s.finish(False, "editorial run timed out").as_dict()

    (LOGS / f"editorial-{date}.txt").write_text(
        f"$ hermes -z <prompt {len(cmd[2])} chars>\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}\n",
        encoding="utf-8")

    if r.returncode != 0:
        return s.finish(False, f"exit {r.returncode}: {(r.stderr or r.stdout)[-300:]}").as_dict()

    missing = [f for f in expected_files(date, langs) if not Path(f).exists() or Path(f).stat().st_size < 500]
    if missing:
        return s.finish(False, f"editor produced no usable JSON for: {', '.join(missing)}").as_dict()
    return s.finish(True, f"{len(expected_files(date, langs))} selection files written; "
                          f"reply {len(r.stdout)} chars").as_dict()


def expected_files(date: str, langs: list[str]) -> list[Path]:
    suffix = {"en": "", "zh": "-zh"}
    return [DATA / f"{date}{suffix[lang]}.json" for lang in langs]


def validate_selection(path: Path, date: str) -> list[str]:
    """Return a list of contract violations (empty means the file is renderable)."""
    problems = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"invalid JSON: {exc}"]
    for key in ("date", "window", "exec_summary_html", "lanes", "watchlist_html", "sources_html"):
        if key not in data:
            problems.append(f"missing key {key}")
    ids = [lane.get("id") for lane in data.get("lanes", [])]
    if sorted(ids) != sorted(["stock", "money", "tech", "politics"]):
        problems.append(f"lane ids {ids} != stock/money/tech/politics")
    for lane in data.get("lanes", []):
        for i, item in enumerate(lane.get("items", []), 1):
            for key in ("title", "summary", "source", "url", "time", "confidence"):
                if not str(item.get(key, "")).strip():
                    problems.append(f"{lane.get('id')} item {i} missing {key}")
            if item.get("confidence") not in ("confirmed", "reported", "single"):
                problems.append(f"{lane.get('id')} item {i} bad confidence {item.get('confidence')!r}")
    if data.get("date") != date:
        problems.append(f"date {data.get('date')!r} != {date!r}")
    if sum(len(l.get("items", [])) for l in data.get("lanes", [])) < 12:
        problems.append("fewer than 12 items across all lanes")
    return problems


def render(cfg: dict, date: str, langs: list[str]) -> tuple[list[dict], dict]:
    steps, built = [], {}
    py = cfg["PYTHON_BIN"]
    for lang in langs:
        s = step(f"render:{lang}")
        data_path = DATA / f"{date}{'-zh' if lang == 'zh' else ''}.json"
        out_path = REPORTS / f"{date}{'-zh' if lang == 'zh' else ''}.html"
        problems = validate_selection(data_path, date) if data_path.exists() else ["selection JSON missing"]
        if problems:
            steps.append(s.finish(False, "; ".join(problems[:8])).as_dict())
            continue
        cmd = [py, "scripts/render.py", "--data", str(data_path.relative_to(ROOT)),
               "--quotes", "cache/quotes.json", "--template", str(TEMPLATE[lang].relative_to(ROOT)),
               "--out", str(out_path.relative_to(ROOT))]
        r = run(cmd, 120)
        if r.returncode != 0:
            steps.append(s.finish(False, (r.stderr or r.stdout)[-300:]).as_dict())
            continue
        problems = verify_html(out_path, data_path)
        steps.append(s.finish(not problems, (r.stdout.strip() + (" | " + "; ".join(problems) if problems else ""))).as_dict())
        if not problems:
            built[lang] = str(out_path)
    return steps, built


def verify_html(html_path: Path, data_path: Path) -> list[str]:
    """Self-containment + parity checks. Empty list means the report is publishable."""
    problems = []
    if not html_path.exists():
        return ["output file missing"]
    html = html_path.read_text(encoding="utf-8")
    data = json.loads(data_path.read_text(encoding="utf-8"))

    if re.search(r"<script|<link|<iframe|<img", html, re.I):
        problems.append("external/embedded asset tag present")
    if re.search(r'src="http|url\(http', html):
        problems.append("external resource reference present")
    if re.search(r"\{\{[A-Z_0-9]+\}\}", html):
        problems.append("unfilled placeholder present")

    want_items = sum(len(lane.get("items", [])) for lane in data.get("lanes", []))
    got_cards = len(re.findall(r"<div class='card'>", html))
    if got_cards != want_items:
        problems.append(f"card count {got_cards} != {want_items} items")

    want_tags = {}
    for lane in data.get("lanes", []):
        for item in lane.get("items", []):
            cls = {"confirmed": "confirmed", "reported": "reported", "single": "single"}.get(
                item.get("confidence"), "reported")
            want_tags[cls] = want_tags.get(cls, 0) + 1
    # Count only the tags attached to item titles: the template's legend card also
    # carries one <span class="tag ..."> per class, which is chrome, not a claim.
    titles = re.findall(r"<h3[^>]*>(.*?)</h3>", html, re.S)
    got_tags = {}
    for block in titles:
        for cls in re.findall(r"<span class=\"tag (\w+)\">", block):
            got_tags[cls] = got_tags.get(cls, 0) + 1
    if want_tags != got_tags:
        problems.append(f"tag counts {got_tags} != {want_tags}")

    if len(html.encode("utf-8")) < 8000:
        problems.append(f"suspiciously small output ({len(html.encode('utf-8'))} bytes)")
    return problems


# A 404 proves the link is wrong; a 403 or 429 only proves the outlet dislikes robots.
DEAD = {404, 410, 451}
WALLED = {401, 402, 403, 405, 407, 429}
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def link_status(url: str, timeout: int = 25) -> tuple[str, int | str]:
    """'ok' | 'dead' | 'walled' | 'unreachable' for one URL, via HEAD then GET."""
    import urllib.error
    import urllib.request
    headers = {"User-Agent": BROWSER_UA,
               "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9"}
    last: int | str = 0
    for method in ("HEAD", "GET"):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers, method=method),
                                        timeout=timeout) as resp:
                return "ok", resp.status
        except urllib.error.HTTPError as exc:
            last = exc.code
            if exc.code in DEAD:
                return "dead", exc.code
            if method == "HEAD" and exc.code in (400, 405, 501):
                continue  # outlet rejects HEAD outright; retry as GET
            break
        except Exception as exc:  # DNS, TLS, timeout
            return "unreachable", type(exc).__name__
    return ("walled", last) if last in WALLED or last >= 400 else ("dead", last)


def check_links(cfg: dict, date: str, langs: list[str]) -> dict:
    """Gate publication on every item URL resolving. The brief's whole claim is that
    each number carries a source, so an invented or truncated link is a worse defect
    than a missing one — and an unattended editor does produce them: the first headless
    run shipped 4 dead URLs out of 24, including a slug cut off mid-hyphen."""
    from concurrent.futures import ThreadPoolExecutor
    s = step("links")
    urls: dict[str, list[str]] = {}
    for lang in langs:
        data_path = ROOT / "reports" / "data" / f"{date}{'-zh' if lang == 'zh' else ''}.json"
        if not data_path.exists():
            continue
        try:
            sel = json.loads(data_path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for lane in sel.get("lanes", []):
            for item in lane.get("items", []):
                url = str(item.get("url") or "").strip()
                if url.startswith("http"):
                    urls.setdefault(url, []).append(f"{lang}:{lane.get('id')}")
    if not urls:
        return s.finish(False, "no item URLs found in selection").as_dict()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(link_status, urls))
    dead = [u for u, (kind, _) in zip(urls, results) if kind == "dead"]
    walled = sum(1 for kind, _ in results if kind == "walled")
    lost = sum(1 for kind, _ in results if kind == "unreachable")
    detail = (f"{len(urls)} urls: {len(urls) - len(dead) - walled - lost} ok, {len(dead)} dead, "
              f"{walled} bot-walled, {lost} unreachable")
    if dead:
        detail += " | DEAD: " + "; ".join(dead[:8])
    return s.finish(not dead, detail).as_dict()


def acquire_lock() -> bool:
    CACHE.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            age = time.time() - LOCK.stat().st_mtime
            pid = int(LOCK.read_text().strip() or 0)
        except (ValueError, OSError):
            age, pid = 0, 0
        if age < 5400 and pid and Path(f"/proc/{pid}").exists():
            return False
        LOCK.unlink(missing_ok=True)
    LOCK.write_text(str(os.getpid()))
    return True


def write_status(status: dict, cfg: dict) -> None:
    """Persist run status for the scheduler and the /status endpoint."""
    status.setdefault("next_due_utc", (now_utc() + timedelta(
        hours=cfgmod.get_float(cfg, "REPORT_INTERVAL", 24.0))).isoformat(timespec="seconds"))
    LOGS.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="report date, default today (local)")
    ap.add_argument("--force", action="store_true", help="regenerate even if reports exist")
    ap.add_argument("--skip-collect", action="store_true", help="reuse cache/collect.json + quotes.json")
    ap.add_argument("--langs", default=None, help="override LANGS from config (e.g. 'en')")
    args = ap.parse_args()

    cfg = cfgmod.load()
    requested = [p.strip() for p in (args.langs or "").split(",") if p.strip()] or cfgmod.get_list(cfg, "LANGS")
    langs = [l for l in requested if l in LANG_NAME]
    if not langs:
        print(f"no valid languages in {requested!r} (known: {', '.join(LANG_NAME)})", file=sys.stderr)
        return 2
    date = args.date or cfgmod.report_date()
    started = now_utc()
    steps: list[dict] = []

    if not acquire_lock():
        log_line(f"SKIP {date}: another generate.py holds {LOCK}")
        return 3

    try:
        existing = {lang: (REPORTS / f"{date}{'-zh' if lang == 'zh' else ''}.html") for lang in langs}
        if not args.force and all(p.exists() for p in existing.values()):
            log_line(f"SKIP {date}: all of {', '.join(existing)} exist (use --force)")
            # Still record the attempt: the scheduler reads finished_utc to space runs
            # out, and a skip that leaves no status file makes it re-fire every tick.
            write_status({
                "date": date, "ok": True, "skipped": True, "reason": "reports already exist",
                "started_utc": started.isoformat(timespec="seconds"),
                "finished_utc": now_utc().isoformat(timespec="seconds"),
                "seconds": 0.0, "langs_requested": langs,
                "reports": {lang: (ROOT / p).relative_to(ROOT).as_posix() for lang, p in existing.items()},
                "steps": [],
            }, cfg)
            return 0

        log_line(f"START {date} langs={','.join(langs)} collect={'no' if args.skip_collect else 'yes'}")

        if not args.skip_collect:
            steps += collect_and_quotes(cfg, cfgmod.get_int(cfg, "COLLECT_HOURS", 48))
        elif not (CACHE / "quotes.json").exists():
            steps.append(step("quotes").finish(False, "--skip-collect but cache/quotes.json missing").as_dict())

        steps.append(curate(cfg, date, langs))

        # Check the editor's URLs *before* rendering. render() writes straight to
        # reports/, so a post-render check would report failure on a file that is
        # already published and being served.
        built: dict[str, Path] = {}
        if all(s["ok"] for s in steps):
            steps.append(check_links(cfg, date, langs))
        if all(s["ok"] for s in steps):
            render_steps, built = render(cfg, date, langs)
            steps += render_steps
        else:
            log_line(f"HALT {date}: an earlier step failed; nothing rendered")

        ok = len(built) == len(langs) and all(s["ok"] for s in steps)
        status = {
            "date": date,
            "ok": ok,
            "started_utc": started.isoformat(timespec="seconds"),
            "finished_utc": now_utc().isoformat(timespec="seconds"),
            "seconds": round((now_utc() - started).total_seconds(), 1),
            "langs_requested": langs,
            "reports": {lang: (ROOT / p).relative_to(ROOT).as_posix() for lang, p in built.items()},
            "steps": steps,
        }
        write_status(status, cfg)
        log_line(f"{'DONE' if ok else 'INCOMPLETE'} {date} reports={list(built)} "
                 f"({status['seconds']}s) status={STATUS.relative_to(ROOT)}")
        return 0 if ok else 1
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
