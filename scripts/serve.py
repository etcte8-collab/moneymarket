#!/usr/bin/env python3
"""moneymarket web service: browse generated reports + run the scheduler.

  python3 scripts/serve.py            # binds moneymarket.config WEB_HOST:WEB_PORT
  python3 scripts/serve.py --no-scheduler --port 9101

Routes
  GET  /              index: every generated report, EN and ZH, plus run status
  GET  /reports/<f>   a report file (served only from ./reports, traversal-blocked)
  GET  /status        machine-readable status JSON
  GET  /healthz       liveness probe
  POST /generate      trigger a run now (ALLOW_TRIGGER, optional bearer token)

Stdlib only. The scheduler runs in a thread in this process and re-reads
moneymarket.config every tick, so REPORT_INTERVAL changes apply without a restart.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as cfgmod  # noqa: E402

ROOT = cfgmod.ROOT
REPORTS = ROOT / "reports"
LOGS = ROOT / "logs"
STATUS = LOGS / "last_run.json"
LAST_TRIGGER = LOGS / "last_trigger"
GENERATE = ROOT / "scripts" / "generate.py"
TICK_SECONDS = 60

REPORT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(-zh)?\.html$")
STARTED = datetime.now(timezone.utc)
RUNNING = threading.Lock()
STATE = {"running": False, "last_check_utc": None, "next_due_utc": None, "interval_h": None}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def read_status() -> dict:
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def list_reports() -> dict:
    """{date: {'en': name|None, 'zh': name|None, 'bytes': {...}, 'mtime': iso}}"""
    found: dict = {}
    if not REPORTS.is_dir():
        return found
    for path in sorted(REPORTS.glob("*.html")):
        m = REPORT_RE.match(path.name)
        if not m:
            continue
        date, zh = m.group(1), bool(m.group(2))
        entry = found.setdefault(date, {"en": None, "zh": None, "bytes": {}, "mtime": 0})
        entry["zh" if zh else "en"] = path.name
        entry["bytes"]["zh" if zh else "en"] = path.stat().st_size
        entry["mtime"] = max(entry["mtime"], path.stat().st_mtime)
    return found


def next_due(cfg: dict, status: dict) -> datetime | None:
    """Due when the interval has elapsed since the later of the last finished run and
    the last trigger. The trigger floor is what stops a run that writes no status (a
    lock collision, a crash) from making this fire again on the very next tick."""
    interval = cfgmod.get_float(cfg, "REPORT_INTERVAL", 24.0)
    if interval <= 0:
        return None
    anchors = []
    for key in ("finished_utc", "started_utc"):
        raw = status.get(key)
        if raw:
            try:
                anchors.append(datetime.fromisoformat(raw))
                break
            except ValueError:
                continue
    try:
        anchors.append(datetime.fromisoformat(LAST_TRIGGER.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        pass
    if not anchors:
        return now_utc()  # never ran and never triggered: due immediately
    return max(anchors) + timedelta(hours=interval)


def trigger(cfg: dict, reason: str) -> tuple[int, str]:
    """Run generate.py once. Returns (http_status, message)."""
    if not RUNNING.acquire(blocking=False):
        return 409, "a run is already in progress"
    STATE["running"] = True
    try:
        log = LOGS / "service.log"
        LOGS.mkdir(parents=True, exist_ok=True)
        LAST_TRIGGER.write_text(now_utc().isoformat(timespec="seconds"), encoding="utf-8")
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{now_utc():%Y-%m-%d %H:%M:%SZ} triggered: {reason}\n")
            proc = subprocess.run(
                [cfg["PYTHON_BIN"], str(GENERATE)],
                cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT, timeout=7200,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        return (200 if proc.returncode == 0 else 500), f"generate.py exit {proc.returncode}"
    except subprocess.TimeoutExpired:
        return 500, "generate.py timed out after 7200s"
    except Exception as exc:  # noqa: BLE001 — surface any failure to the caller
        return 500, f"generate.py failed: {exc}"
    finally:
        STATE["running"] = False
        RUNNING.release()


def scheduler_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            cfg = cfgmod.load()
            STATE["last_check_utc"] = now_utc().isoformat(timespec="seconds")
            STATE["interval_h"] = cfgmod.get_float(cfg, "REPORT_INTERVAL", 24.0)
            if cfgmod.get_bool(cfg, "SCHEDULER", True):
                status = read_status()
                due = next_due(cfg, status)
                STATE["next_due_utc"] = due.isoformat(timespec="seconds") if due else None
                today = cfgmod.report_date()  # local date, same basis as generate.py
                missing_today = today not in list_reports()
                if due and now_utc() >= due and (not missing_today or cfgmod.get_bool(cfg, "RUN_IF_MISSING", True)):
                    reason = "interval elapsed"
                    if missing_today:
                        reason += " (today's report missing)"
                    code, msg = trigger(cfg, reason)
                    with (LOGS / "service.log").open("a", encoding="utf-8") as fh:
                        fh.write(f"{now_utc():%Y-%m-%d %H:%M:%SZ} scheduler: {code} {msg}\n")
            else:
                STATE["next_due_utc"] = None
        except Exception as exc:  # a bad config must not kill the thread
            try:
                LOGS.mkdir(parents=True, exist_ok=True)
                with (LOGS / "service.log").open("a", encoding="utf-8") as fh:
                    fh.write(f"{now_utc():%Y-%m-%d %H:%M:%SZ} scheduler error: {exc}\n")
            except OSError:
                pass
        stop.wait(TICK_SECONDS)


CSS = """
:root{--fg:#15202b;--mut:#5b6b7b;--line:#dfe5ec;--bg:#f7f9fb;--acc:#0b5fff;--ok:#0a7d38;--bad:#b3261e}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px;background:var(--bg);color:var(--fg);
font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:860px;margin:0 auto}
h1{font-size:26px;margin:0 0 4px}
.sub{color:var(--mut);font-size:14px;margin:0 0 24px}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-bottom:16px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 12px}
table{width:100%;border-collapse:collapse;font-size:15px}
th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line)}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
tr:last-child td{border-bottom:0}
a{color:var(--acc);text-decoration:none;font-weight:600}
a:hover{text-decoration:underline}
.mut{color:var(--mut);font-size:13px}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}
code{background:#eef2f7;padding:1px 5px;border-radius:4px;font-size:13px}
form{display:inline}
button{font:inherit;font-weight:600;color:#fff;background:var(--acc);border:0;border-radius:7px;padding:8px 14px;cursor:pointer}
button:disabled{background:#9fb0c2;cursor:default}
footer{color:var(--mut);font-size:13px;margin-top:24px}
"""


def human(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def index_page(cfg: dict) -> bytes:
    status = read_status()
    reports = list_reports()
    started = STARTED

    rows = []
    for date in sorted(reports, reverse=True):
        entry = reports[date]
        en = f"<a href='/reports/{entry['en']}'>English</a>" if entry["en"] else "<span class='mut'>—</span>"
        zh = f"<a href='/reports/{entry['zh']}'>中文</a>" if entry["zh"] else "<span class='mut'>—</span>"
        size = " / ".join(f"{lang} {entry['bytes'].get(lang, 0):,}B" for lang in ("en", "zh"))
        rows.append(f"<tr><td><strong>{date}</strong></td><td>{en}</td><td>{zh}</td>"
                    f"<td class='mut'>{size}</td><td class='mut'>{human(datetime.fromtimestamp(entry['mtime'], timezone.utc))}</td></tr>")
    if not rows:
        rows.append("<tr><td colspan='5' class='mut'>No reports yet. The scheduler generates one each "
                    "REPORT_INTERVAL hours, or POST /generate.</td></tr>")

    ok = status.get("ok")
    last = ("<span class='ok'>ok</span>" if ok else "<span class='bad'>failed</span>") if ok is not None else "<span class='mut'>never</span>"
    steps_html = ""
    if status.get("steps"):
        steps_html = " · ".join(
            f"<span class='{ 'ok' if s.get('ok') else 'bad'}'>{html.escape(str(s.get('step')))}</span>"
            for s in status["steps"])
    next_due = STATE.get("next_due_utc")
    allow = cfgmod.get_bool(cfg, "ALLOW_TRIGGER", True)
    button = ("" if not allow else
              "<form method='post' action='/generate' enctype='text/plain'>"
              "<button type='submit' " + ("disabled" if STATE["running"] else "") + ">Generate now</button></form>")

    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>moneymarket — reports</title><style>{CSS}</style></head><body><div class="wrap">
<h1>moneymarket</h1>
<p class="sub">24-hour market intelligence brief · English and 中文 · one self-contained HTML file per run</p>

<div class="card"><h2>Reports</h2><table>
<tr><th>Date</th><th>EN</th><th>ZH</th><th>Size</th><th>Written</th></tr>
{"".join(rows)}
</table></div>

<div class="card"><h2>Run status</h2><table>
<tr><td>Last run</td><td>{last} {html.escape(str(status.get('date', '—')))} · {html.escape(str(status.get('finished_utc', '—')))} · {status.get('seconds', '—')}s</td></tr>
<tr><td>Steps</td><td class="mut">{steps_html or '—'}</td></tr>
<tr><td>Interval</td><td>{cfgmod.get_float(cfg, 'REPORT_INTERVAL', 24.0):g} h <span class="mut">(moneymarket.config: REPORT_INTERVAL)</span></td></tr>
<tr><td>Scheduler</td><td>{'on' if cfgmod.get_bool(cfg, 'SCHEDULER', True) else 'off'} · next due {html.escape(next_due or '—')} · last check {html.escape(STATE.get('last_check_utc') or '—')}</td></tr>
<tr><td>Languages</td><td>{html.escape(cfg.get('LANGS', ''))} · lanes {html.escape(cfg.get('LANES', ''))}</td></tr>
<tr><td>Editorial</td><td>{html.escape(cfg.get('EDITOR', ''))} <span class="mut">(headless hermes -z)</span></td></tr>
<tr><td>Service</td><td>up since {human(started)} · serving {html.escape(cfg['WEB_HOST'])}:{cfg['WEB_PORT']}</td></tr>
</table>{button}</div>

<footer>Reports are static files under <code>~/proj.agent/moneymarket/reports/</code>.
Status <a href='/status'>/status</a> · health <a href='/healthz'>/healthz</a> · trigger <code>POST /generate</code></footer>
</div></body></html>"""
    return body.encode("utf-8")


def status_json(cfg: dict) -> bytes:
    payload = {
        "service": {"host": cfg["WEB_HOST"], "port": int(cfg["WEB_PORT"]),
                    "running": STATE["running"], "last_check_utc": STATE["last_check_utc"],
                    "next_due_utc": STATE["next_due_utc"], "interval_hours": STATE["interval_h"],
                    "scheduler": cfgmod.get_bool(cfg, "SCHEDULER", True)},
        "config": {k: cfg[k] for k in ("REPORT_INTERVAL", "LANGS", "LANES", "COLLECT_HOURS",
                                        "EDITOR", "ALLOW_TRIGGER", "RUN_IF_MISSING")},
        "last_run": read_status(),
        "reports": {d: {k: v for k, v in e.items() if k != "mtime"} for d, e in list_reports().items()},
        "now_utc": now_utc().isoformat(timespec="seconds"),
    }
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "moneymarket/1.0"
    cfg: dict = {}

    def log_message(self, fmt: str, *args) -> None:  # compact access log
        line = f"{now_utc():%Y-%m-%d %H:%M:%SZ} {self.address_string()} {fmt % args}"
        try:
            LOGS.mkdir(parents=True, exist_ok=True)
            with (LOGS / "access.log").open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def resolve_report(self, name: str) -> Path | None:
        """Only files matching the report pattern, only directly inside reports/."""
        name = unquote(name)
        if not REPORT_RE.match(Path(name).name):
            return None
        path = (REPORTS / Path(name).name).resolve()
        return path if path.parent == REPORTS.resolve() and path.is_file() else None

    def authorized(self, cfg: dict) -> bool:
        token = cfg.get("TRIGGER_TOKEN", "").strip()
        if not token:
            return True
        got = str(self.headers.get("Authorization", "")).strip()
        supplied = got[7:].strip() if got.lower().startswith("bearer ") else got
        return secrets.compare_digest(supplied, token)

    def do_GET(self) -> None:
        cfg = cfgmod.load()
        Handler.cfg = cfg
        path = urlparse(self.path).path
        if path == "/":
            self.send(200, index_page(cfg), "text/html; charset=utf-8")
        elif path == "/status":
            self.send(200, status_json(cfg), "application/json; charset=utf-8")
        elif path == "/healthz":
            self.send(200, b"ok\n", "text/plain; charset=utf-8")
        elif path.startswith("/reports/"):
            report = self.resolve_report(path[len("/reports/"):])
            if report is None:
                self.send(404, b"not found\n", "text/plain; charset=utf-8")
                return
            self.send(200, report.read_bytes(), "text/html; charset=utf-8")
        else:
            self.send(404, b"not found\n", "text/plain; charset=utf-8")

    do_HEAD = do_GET

    def do_POST(self) -> None:
        cfg = cfgmod.load()
        path = urlparse(self.path).path
        if path != "/generate":
            self.send(404, b"not found\n", "text/plain; charset=utf-8")
            return
        if not cfgmod.get_bool(cfg, "ALLOW_TRIGGER", True):
            self.send(403, b"triggering disabled (ALLOW_TRIGGER=false in moneymarket.config)\n",
                      "text/plain; charset=utf-8")
            return
        if not self.authorized(cfg):
            self.send(401, b"missing or wrong bearer token\n", "text/plain; charset=utf-8",
                      {"WWW-Authenticate": "Bearer"})
            return
        # drain any body so keep-alive stays clean
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(min(length, 65536))
        code, msg = trigger(cfg, "manual POST /generate")
        body = (msg + "\n").encode("utf-8")
        if "text/html" in str(self.headers.get("Content-Type", "")):
            self.send(code, f"<html><body><p>{html.escape(msg)}</p><p><a href='/'>back</a></p></body></html>".encode(),
                      "text/html; charset=utf-8")
        else:
            self.send(code, body, "text/plain; charset=utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-scheduler", action="store_true")
    args = ap.parse_args()

    cfg = cfgmod.load()
    if args.no_scheduler:
        os.environ["MONEYMARKET_SCHEDULER"] = "false"
    host = args.host or cfg["WEB_HOST"]
    port = args.port or cfgmod.get_int(cfg, "WEB_PORT", 9100)

    stop = threading.Event()
    if not cfgmod.get_bool(cfgmod.load(), "SCHEDULER", True):
        print("scheduler disabled", flush=True)
    else:
        threading.Thread(target=scheduler_loop, args=(stop,), daemon=True, name="scheduler").start()

    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"moneymarket serving http://{host}:{port}/ "
          f"(scheduler={'on' if not args.no_scheduler else 'off'}, "
          f"interval={cfgmod.get_float(cfg, 'REPORT_INTERVAL', 24.0):g}h, root={REPORTS})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
