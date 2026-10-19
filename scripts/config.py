#!/usr/bin/env python3
"""Shared config loader for moneymarket.

Reads moneymarket.config (KEY=VALUE, '#' comments) from the project root, falling
back to built-in defaults. load() re-reads the file every call on purpose: the
scheduler calls it each tick so an edited interval takes effect without a restart.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("MONEYMARKET_CONFIG", ROOT / "moneymarket.config"))

DEFAULTS = {
    "REPORT_INTERVAL": "24",
    "RUN_IF_MISSING": "true",
    "SCHEDULER": "true",
    "LANGS": "en,zh",
    "LANES": "stock,money,tech,politics",
    "COLLECT_HOURS": "48",
    "EDITOR": "hermes",
    "HERMES_BIN": str(Path.home() / ".local" / "bin" / "hermes"),
    "EDITOR_TIMEOUT": "2400",
    "EDITOR_TOOLSETS": "",
    "EDITOR_NOTES": "",
    "WEB_HOST": "0.0.0.0",
    "WEB_PORT": "9100",
    "ALLOW_TRIGGER": "true",
    "TRIGGER_TOKEN": "",
    "PYTHON_BIN": sys.executable or "/usr/bin/python3",
}

TRUE = {"1", "true", "yes", "on"}


def _parse(text: str) -> dict:
    cfg = dict(DEFAULTS)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        # strip trailing ' # comment' but keep '#' inside quoted values
        if not value.lstrip().startswith(('"', "'")):
            value = value.split("#", 1)[0]
        value = value.strip().strip('"').strip("'")
        cfg[key.strip()] = value
    return cfg


def load() -> dict:
    """Current config: defaults, overlaid by the config file, overlaid by env."""
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(_parse(CONFIG_PATH.read_text(encoding="utf-8")))
    for key in cfg:
        env = os.environ.get(f"MONEYMARKET_{key}")
        if env is not None:
            cfg[key] = env
    return cfg


def get_int(cfg: dict, key: str, fallback: int) -> int:
    try:
        return int(str(cfg.get(key, "")).strip())
    except ValueError:
        return fallback


def get_float(cfg: dict, key: str, fallback: float) -> float:
    try:
        return float(str(cfg.get(key, "")).strip())
    except ValueError:
        return fallback


def get_bool(cfg: dict, key: str, fallback: bool = False) -> bool:
    value = str(cfg.get(key, "")).strip().lower()
    if not value:
        return fallback
    return value in TRUE


def get_list(cfg: dict, key: str, fallback: list[str] | None = None) -> list[str]:
    raw = str(cfg.get(key, "").strip())
    if not raw:
        return list(fallback or [])
    return [part.strip() for part in raw.split(",") if part.strip()]


def report_date(offset_days: int = 0) -> str:
    """The report date is the *local* calendar date — the convention the dated files
    are named by. Both generate.py and serve.py must use this: when one used UTC and
    the other local time, the scheduler saw 'today missing' for 7 hours every day and
    triggered a run each tick."""
    return (datetime.now() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    import json

    print(json.dumps(load(), indent=2, sort_keys=True))
