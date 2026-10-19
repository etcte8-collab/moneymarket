#!/usr/bin/env python3
"""moneymarket snapshot: index / FX / crypto / commodity / rate levels.

Free, key-free sources verified on this host 2026-10-16:
  - Yahoo chart v8 (query2)   works, but IP rate-limits -> 2s spacing + backoff
  - FRED fredgraph.csv        works (Treasury yields, no key)
  - api.frankfurter.app       works (ECB daily FX)
  - api.coingecko.com         works (crypto 24h change)
Known dead here: Yahoo v7 /finance/quote (429), stooq CSV (404), CNBC restQuote (denied).

Stdlib only. Usage: python3 scripts/quotes.py --out cache/quotes.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

UA = "Mozilla/5.0 (X11; Linux x86_64)"

YAHOO = [
    ("^GSPC", "S&P 500", "index"),
    ("^IXIC", "Nasdaq Composite", "index"),
    ("^DJI", "Dow Jones", "index"),
    ("^RUT", "Russell 2000", "index"),
    ("^VIX", "VIX", "index"),
    ("GC=F", "Gold", "commodity"),
    ("CL=F", "WTI crude", "commodity"),
    ("HG=F", "Copper", "commodity"),
    ("SI=F", "Silver", "commodity"),
    ("NG=F", "Natural gas", "commodity"),
    ("DX-Y.NYB", "Dollar index (DXY)", "fx"),
]

FRED = [
    ("DGS10", "US 10Y Treasury", "rates"),
    ("DGS5", "US 5Y Treasury", "rates"),
    ("DGS2", "US 2Y Treasury", "rates"),
]


def get(url: str, timeout: float = 25.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, text/csv, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_json(url: str, retries: int = 3, timeout: float = 25.0):
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return json.loads(get(url, timeout).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 429:
                time.sleep(4 * (attempt + 1))
                continue
            if attempt < retries - 1:
                time.sleep(1.5)
                continue
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries - 1:
                time.sleep(1.5)
                continue
    raise last if last else RuntimeError("request failed")


def yahoo(symbol: str) -> dict | None:
    """Daily change = last two *daily closes* in the series.

    NOTE: meta.chartPreviousClose with range=5d is the close before the whole
    5-day window, i.e. a WEEKLY baseline. Using it silently reported weekly
    moves as daily ones, so the closes are read from the series instead.
    """
    url = (
        "https://query2.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(symbol)}?range=5d&interval=1d"
    )
    try:
        data = get_json(url)
        result = data["chart"]["result"][0]
        meta = result["meta"]
        price = meta.get("regularMarketPrice")
        closes = [c for c in (result.get("indicators", {}).get("quote", [{}])[0].get("close") or []) if c is not None]
        prev = closes[-2] if len(closes) >= 2 else (meta.get("previousClose") or meta.get("chartPreviousClose"))
        # If the series' last close is stale vs the live price, keep the live level.
        if price is None and closes:
            price = closes[-1]
        change = pct = None
        if price is not None and prev:
            change = price - prev
            pct = change / prev * 100.0
        if price is None:
            return None
        return {
            "level": round(price, 4),
            "prev_close": round(prev, 4) if prev else None,
            "change": round(change, 4) if change is not None else None,
            "change_pct": round(pct, 3) if pct is not None else None,
            "as_of_utc": datetime.fromtimestamp(meta["regularMarketTime"], timezone.utc).isoformat()
            if meta.get("regularMarketTime")
            else None,
            "currency": meta.get("currency"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:140]}


def fred(series: str) -> dict | None:
    """Latest two observations from FRED's public CSV (no key)."""
    try:
        raw = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}").decode("utf-8")
        rows = []
        for line in raw.splitlines()[1:]:
            date, _, value = line.partition(",")
            value = value.strip()
            if value and value != ".":
                try:
                    rows.append((date.strip(), float(value)))
                except ValueError:
                    continue
        if not rows:
            return None
        latest_date, latest = rows[-1]
        prev_val = rows[-2][1] if len(rows) > 1 else None
        return {
            "level": round(latest, 4),
            "prev_close": round(prev_val, 4) if prev_val is not None else None,
            "change": round(latest - prev_val, 4) if prev_val is not None else None,
            "change_pct": round((latest - prev_val) / prev_val * 100.0, 3) if prev_val else None,
            "as_of_utc": f"{latest_date}T00:00:00+00:00",
            "currency": "pct",
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:140]}


def coingecko() -> dict:
    try:
        data = get_json(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_24hr_change=true"
        )
        return {
            k.upper(): {
                "level": v.get("usd"),
                "change_pct": round(v.get("usd_24h_change", 0.0), 2),
                "unit": "USD",
            }
            for k, v in data.items()
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:140]}


def fx() -> dict:
    try:
        data = get_json("https://api.frankfurter.app/latest?from=USD&to=EUR,JPY,CNY,GBP,CHF")
        return {"base": data.get("base"), "as_of": data.get("date"), "rates": data.get("rates"), "source": "ECB via frankfurter"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:140]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    rows = []
    for symbol, label, group in YAHOO:
        row = yahoo(symbol)
        if row and row.get("level") is not None:
            row.update({"symbol": symbol, "label": label, "group": group, "source": "yahoo-chart-v8"})
            rows.append(row)
        else:
            rows.append({"symbol": symbol, "label": label, "group": group, "error": (row or {}).get("error", "no data")})
        time.sleep(2.2)

    for series, label, group in FRED:
        row = fred(series)
        if row and row.get("level") is not None:
            row.update({"symbol": series, "label": label, "group": group, "source": "FRED"})
            rows.append(row)
        else:
            rows.append({"symbol": series, "label": label, "group": group, "error": (row or {}).get("error", "no data")})
        time.sleep(0.8)

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
        "crypto_24h": coingecko(),
        "fx": fx(),
    }
    text = json.dumps(payload, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        ok = [r for r in rows if r.get("level") is not None]
        print(f"wrote {args.out}: {len(ok)}/{len(rows)} quotes ok")
        for r in rows:
            if r.get("level") is None:
                print(f"  FAIL {r['label']:<20} {r.get('error')}")
            else:
                pct = r.get("change_pct")
                print(f"  {r['label']:<20} {r['level']:>11} {('%+.2f%%' % pct) if pct is not None else 'n/a':>9}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
