#!/usr/bin/env python3
"""moneymarket renderer: curated selection JSON + quotes JSON -> self-contained HTML.

  python3 scripts/render.py --data reports/data/2026-10-16.json \
      --quotes cache/quotes.json --out reports/2026-10-16.html

The selection JSON is written by a human/agent (editorial judgement), never by the
collector: collect.py supplies candidates, this script only formats what was chosen.
Output is one HTML file with inline CSS and no scripts, so it can be emailed,
attached, or pushed to a chat channel unchanged.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

CONF_CLASS = {"confirmed": "confirmed", "reported": "reported", "single": "single"}


# Default English labels. A selection JSON may override them with "tag_labels"
# (keyed by confidence class) and "ui_labels" (snapshot-table strings), which is
# how the Chinese edition reuses this renderer without forking it.
TAG_LABELS = {"confirmed": "confirmed", "reported": "reported", "single": "single source"}
UI_LABELS = {
    "na": "n/a",
    "daily_fix": "daily fix",
    "change_24h": "24h change",
    "live": "live",
    "suffix_24h": " (24h)",
}


def tag(confidence: str, labels: dict | None = None) -> str:
    cls = CONF_CLASS.get(confidence, "reported")
    lab = {**TAG_LABELS, **(labels or {})}
    return f'<span class="tag {cls}">{escape(lab.get(cls, lab["reported"]))}</span>'


def fmt_num(value, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{digits}f}"


def change_cell(row: dict, ui: dict | None = None) -> str:
    pct = row.get("change_pct")
    if pct is None:
        return f'<td class="sub">{escape((ui or UI_LABELS)["na"])}</td>'
    cls = "up" if pct > 0 else "dn" if pct < 0 else ""
    arrow = "&#9650;" if pct > 0 else "&#9660;" if pct < 0 else "&#9644;"
    return f'<td class="{cls}">{arrow} {pct:+.2f}%</td>'


def snapshot_rows(quotes: dict, ui: dict | None = None) -> str:
    ui = {**UI_LABELS, **(ui or {})}
    order = ["index", "rates", "commodity", "fx"]
    rows = sorted(
        [r for r in quotes.get("rows", []) if r.get("level") is not None],
        key=lambda r: (order.index(r.get("group", "index")) if r.get("group") in order else 9),
    )
    out = []
    for row in rows:
        level = row["level"]
        digits = 2 if level >= 100 else 4
        out.append(
            "<tr>"
            f"<td>{escape(row['label'])}<div class='src'>{escape(row.get('symbol',''))} &middot; {escape(row.get('source',''))}</div></td>"
            f"<td>{fmt_num(level, digits)}</td>"
            f"{change_cell(row, ui)}"
            f"<td class='src'>{escape((row.get('as_of_utc') or '')[:10])}</td>"
            "</tr>"
        )
    for sym, blk in (quotes.get("crypto_24h") or {}).items():
        if not isinstance(blk, dict) or blk.get("level") is None:
            continue
        row = {"label": f"{sym}{ui['suffix_24h']}", "level": blk["level"], "change_pct": blk.get("change_pct")}
        out.append(
            "<tr>"
            f"<td>{escape(row['label'])}<div class='src'>CoinGecko &middot; {escape(ui['change_24h'])}</div></td>"
            f"<td>{fmt_num(row['level'], 2)}</td>"
            f"{change_cell(row, ui)}"
            f"<td class='src'>{escape(ui['live'])}</td>"
            "</tr>"
        )
    fx = quotes.get("fx") or {}
    for pair, rate in sorted((fx.get("rates") or {}).items()):
        row = {"label": f"USD/{pair}", "level": rate, "change_pct": None}
        out.append(
            "<tr>"
            f"<td>{escape(row['label'])}<div class='src'>{escape(fx.get('source','ECB'))}</div></td>"
            f"<td>{fmt_num(rate, 4)}</td>"
            f"<td class='sub'>{escape(ui['daily_fix'])}</td>"
            f"<td class='src'>{escape(str(fx.get('as_of','')))}</td>"
            "</tr>"
        )
    return "\n".join(out)


def lane_html(lane: dict, labels: dict | None = None) -> str:
    parts = []
    for i, item in enumerate(lane.get("items", []), 1):
        src = item.get("url")
        src_html = (
            f"<a href='{escape(src, quote=True)}'>{escape(item.get('source','source'))}</a>"
            if src
            else escape(item.get("source", "source"))
        )
        when = item.get("time") or ""
        parts.append(
            "<div class='card'>"
            f"<h3 style='margin-top:0'>{i}. {escape(item['title'])} {tag(item.get('confidence','reported'), labels)}</h3>"
            f"<p>{escape(item['summary'])}</p>"
            f"<p class='src'>{src_html} &middot; {escape(when)}</p>"
            "</div>"
        )
    intro = lane.get("intro")
    if intro:
        parts.insert(0, f"<p>{escape(intro)}</p>")
    return "\n".join(parts)


def render(template: str, data: dict, quotes: dict) -> str:
    generated = datetime.now(timezone.utc)
    fills = {
        "{{DATE}}": data.get("date", ""),
        "{{WINDOW}}": data.get("window", ""),
        "{{EXEC_SUMMARY}}": data.get("exec_summary_html", ""),
        "{{SNAPSHOT_ROWS}}": snapshot_rows(quotes, data.get("ui_labels")),
        "{{WATCHLIST}}": data.get("watchlist_html", ""),
        "{{SOURCES}}": data.get("sources_html", ""),
    }
    for lane in data.get("lanes", []):
        key = f"{{{{LANE_{lane['id'].upper()}}}}}"
        fills[key] = lane_html(lane, data.get("tag_labels"))
    out = template
    for key, value in fills.items():
        out = out.replace(key, value)
    leftover = [tok for tok in ("{{DATE}}", "{{EXEC_SUMMARY}}", "{{SNAPSHOT_ROWS}}", "{{LANE_") if tok in out]
    if leftover:
        print(f"WARNING unfilled placeholders: {leftover}", file=sys.stderr)
    stamp = f"<!-- generated {generated.isoformat()} by moneymarket/scripts/render.py -->"
    return out.replace("</html>", f"{stamp}\n</html>")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--quotes", required=True)
    ap.add_argument("--template", default="template.html")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    quotes = json.loads(Path(args.quotes).read_text(encoding="utf-8"))
    template = Path(args.template).read_text(encoding="utf-8")

    html = render(template, data, quotes)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    counts = {lane["id"]: len(lane.get("items", [])) for lane in data.get("lanes", [])}
    print(f"wrote {out_path} ({len(html):,} bytes) lanes={counts} quotes={len(quotes.get('rows', []))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
