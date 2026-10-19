# moneymarket

Personal 24-hour market intelligence brief for investment reference.

## Purpose

Collect and summarize the last ~24 hours of information across four lanes and render a
self-contained HTML report that can later be delivered over chat channels.

| Lane | Coverage |
|---|---|
| Stock market | US indices, sector moves, notable single-name moves, weekly framing |
| Money / rates | Treasury yields, Fed path, dollar, credit, inflation prints |
| Technology | AI/semiconductor capex, big-tech earnings, regulation of tech |
| Politics | Washington policy, elections, sanctions/trade actions with market impact |

## Confirmed settings

- **Scope:** US markets first (Asia/Europe/crypto added only as market-moving context).
- **Language:** English and Chinese, both generated every run. English is the reference
  edition; the Chinese file is written as Chinese financial prose with byte-identical
  numbers, URLs, source names and timestamps (see *Two languages, one renderer*).
- **Cadence:** every 24 hours by default, from `moneymarket.config: REPORT_INTERVAL=24`.
  The human can also ask for a report at any time. No schedule existed before 2026-10-18.
- **Output:** `reports/YYYY-MM-DD.html`, one file, inline CSS only, no scripts, no external
  assets, safe to email or attach.
- **Delivery:** HTML files, browsable from any host on the LAN via the web service on
  `:9100`. Telegram / WhatsApp / Slack remain future work (see below).

## How a report is produced

1. **Collect** — `python3 scripts/collect.py --lane stock --lane money --lane tech
   --lane politics --hours 48 --out cache/collect.json`. Polls 38 RSS feeds plus per-lane
   Google News queries, filters to the window, dedupes. Output is *candidates only*.
   `web_search` (provider: local SearxNG at `http://127.0.0.1:8888`) is still used for
   targeted follow-ups on fast-moving numbers.
2. **Quotes** — `python3 scripts/quotes.py --out cache/quotes.json`. Key-free live levels:
   Yahoo chart v8 (indices, commodities, DXY, index futures), FRED (2/5/10Y Treasuries),
   Frankfurter/ECB (FX), CoinGecko (crypto). Stdlib only, rate-limit backoff.
3. **Deepen** — read the best 2–4 items **in a real browser**. This host's `web_extract`
   backend is search-only and cannot fetch URLs, Bloomberg serves a bot-wall, and WSJ/NYT are
   paywalled, so anything not opened in the browser rests on its feed headline and is labelled
   as such. Google News redirect links need following, and RSS links can be stale.
4. **Cross-check** — every number in the report needs at least one source. A figure from a
   single low-credibility source is kept but explicitly flagged `single source`.
5. **Curate** — a human/agent writes `reports/data/<date>.json`: chosen items, summaries,
   confidence, exec summary, watchlist, sources. `collect.py` never writes this file.
6. **Render** — `python3 scripts/render.py --data reports/data/<date>.json --quotes
   cache/quotes.json --out reports/<date>.html`. Add `--template template.zh.html` (and the
   `-zh` data file) for the Chinese edition. The renderer warns on unfilled placeholders.
7. **Report back** — path, headline takeaways, and what still needs verification.

## Confidence labels used in reports

| English | 中文 | Meaning |
|---|---|---|
| `confirmed` | 已证实 | matches across two or more independent outlets |
| `reported` | 据报道 | one reputable outlet, not yet corroborated |
| `single source` | 单一来源 | one outlet of weaker editorial standing; do not trade on it alone |

## Two languages, one renderer

`render.py` reads optional `tag_labels` and `ui_labels` dicts from the selection JSON, so
the Chinese edition reuses the same renderer instead of forking it:

```bash
python3 scripts/render.py --data reports/data/<date>-zh.json --quotes cache/quotes.json \
    --template template.zh.html --out reports/<date>-zh.html
```

`template.zh.html` carries `lang="zh-CN"`, a CJK font stack and Chinese chrome text with
identical placeholders and classes. The `-zh` JSON sets 已证实 / 据报道 / 单一来源 and
每日定价 / 24小时变动 / 实时 / 无 / （24小时）. Adding a language means a template plus two
label dicts, not a new renderer.

## Automation: daily generation and a browsing endpoint

`moneymarket.config` at the project root drives both the generator and the web service.
It is re-read on every scheduler tick, so cadence changes apply without a restart.

```ini
REPORT_INTERVAL=24      # hours between runs; fractional allowed (0.5 = every 12h)
SCHEDULER=true          # run the scheduler inside the web service process
RUN_IF_MISSING=true     # generate at once if today's report does not exist
LANGS=en,zh
EDITOR=hermes           # hermes | none  (none = refresh cache only, no curation)
WEB_HOST=0.0.0.0
WEB_PORT=9100
ALLOW_TRIGGER=true
TRIGGER_TOKEN=          # if set, POST /generate needs "Authorization: Bearer <token>"
```

`REPORT_INTERVAL`, `RUN_IF_MISSING`, `SCHEDULER`, `EDITOR_NOTES`, `ALLOW_TRIGGER` and
`TRIGGER_TOKEN` are hot-reloaded. `WEB_HOST`, `WEB_PORT` and `PYTHON_BIN` need a restart.
Every key can also be overridden per-process with `MONEYMARKET_<KEY>=...`.

### scripts/generate.py — one run, end to end

```bash
python3 scripts/generate.py                      # today, both languages
python3 scripts/generate.py --date 2026-10-18 --force
python3 scripts/generate.py --skip-collect       # reuse cache, re-curate and re-render
```

quotes -> collect -> **curation** -> render -> verify. Only the curation step needs
judgement, so it is delegated to a headless `hermes -z` run with the `moneymarket-brief`
skill preloaded (`-z` auto-bypasses approvals, which is what an unattended run needs).
Everything around it stays deterministic Python: the renderer is invoked by the script, and
each output must pass `verify_html` — no script/link/img tags, no external refs, no
unfilled `{{...}}`, card count equal to item count, per-lane tag counts equal to the
selection JSON, minimum size. A run that fails any check is reported failed and the
previous report stays published. `logs/last_run.json` records every step with its status
and duration; `logs/generate.log` is the human-readable trail; `logs/editorial-<date>.txt`
keeps the headless run's output. A lock file stops overlapping runs.

### scripts/serve.py — browse reports from any host

```bash
python3 scripts/serve.py                 # binds WEB_HOST:WEB_PORT from config
python3 scripts/serve.py --no-scheduler  # serve only
```

| Route | Purpose |
|---|---|
| `GET /` | index: every report by date, English and 中文, plus run status and cadence |
| `GET /reports/<file>` | the report itself (only files matching `YYYY-MM-DD[-zh].html` directly inside `reports/`) |
| `GET /status` | JSON: config, last run with per-step status, next due, report inventory |
| `GET /healthz` | liveness |
| `POST /generate` | run now; honours `ALLOW_TRIGGER` and `TRIGGER_TOKEN` |

The scheduler is a thread in this process: each 60s tick it re-reads the config and runs
`generate.py` once the interval since the last finished run has elapsed. Reports are plain
files, so the service is stateless — stopping it never loses a report.

### Run it as a service

`~/.config/systemd/user/moneymarket-web.service` is installed; user linger is already on,
so it survives logout and reboot.

```bash
systemctl --user daemon-reload
systemctl --user enable --now moneymarket-web
systemctl --user status moneymarket-web
journalctl --user -u moneymarket-web -f
```

Then browse `http://<host>:9100/` — on this machine `http://192.168.11.9:9100/`.

**Exposure:** `0.0.0.0` publishes the reports to every host that can route here, and
`POST /generate` costs an LLM run. Set `TRIGGER_TOKEN`, or firewall 9100, before leaving it
on a shared network. The unit mounts `/usr` read-only, uses a private `/tmp`, and grants
writes only to the project and `~/.hermes`.

## Known gaps / future work

- **JS-gated and paywalled sources.** AP, TheStreet and Bloomberg block non-browser fetches.
  A browser tool is available here and is used for the deepening step, but Bloomberg's
  bot-wall and the WSJ/NYT paywalls still cap how many exact figures can be verified per run.
- **Bond futures and implied rates are not collected.** So a "live" yield cannot be quoted
  when FRED has not published (weekends), and no fed funds probability can be stated.
- **Change-basis ambiguity.** Snapshot percentage changes for futures/commodities are computed
  against the last settled close in Yahoo's intraday series, which can differ from a same-day
  quote elsewhere (WTI showed -3.83% in the table vs -1.56% against Friday's settlement).
  Worth emitting the anchor explicitly rather than footnoting it.
- **Feed links rot.** CNBC's feed URL carried a `4point6percent` slug while the published
  headline and body both said 4.5%. Verify against headline/body, never the slug.
- **Channel delivery.** Telegram/WhatsApp/Slack need channel config plus tokens. Nothing in
  this project sends a message without explicit per-channel approval.
- ~~No market data API~~ — closed by `scripts/quotes.py`.
- ~~RSS polling / `blogwatcher` skill / OpenClaw browser server~~ — `scripts/collect.py` *is*
  the RSS poller (38 feeds), and those notes referred to a different agent setup.

## Layout

```
moneymarket/
  README.md            this file
  moneymarket.config   cadence, languages, editor, web host/port  (hot-reloaded)
  template.html        structure + styling contract (English)
  template.zh.html     same contract, Chinese chrome + CJK font stack
  scripts/config.py    config loader shared by generate.py and serve.py
  scripts/collect.py   stage 1: RSS/Google News candidate collection
  scripts/quotes.py    stage 2: key-free live market levels
  scripts/generate.py  one run end to end: collect, quotes, curate, render, verify
  scripts/serve.py     web service on :9100 + the interval scheduler thread
  scripts/render.py    selection JSON + quotes -> HTML
  reports/             dated HTML reports, one file per run per language
  reports/data/        curated selection JSON (agent written, never by collect.py)
  cache/               collect.json, quotes.json, run lock and their logs
  logs/                generate.log, last_run.json, service.log, access.log
```

## Status

- 2026-10-16 — project created; the first report was generated in a *different* environment.
  `reports/2026-10-16.html` is not present on this host, so this line no longer claims it is.
- 2026-10-18 — `reports/2026-10-18.html` (English, 35,669 bytes) and
  `reports/2026-10-18-zh.html` (Chinese, 34,224 bytes) generated from
  `reports/data/2026-10-18.json` / `-zh.json`. 48h window, 540 raw -> 531 deduped items,
  14/14 quotes, 23 items across four lanes (14 confirmed / 6 reported / 3 single source).
  Two articles read in full (CNBC China GDP, TechCrunch Anthropic); all index, futures and
  volatility figures pulled directly from Yahoo's chart API rather than from press prose.
- 2026-10-18 — `scripts/render.py` now takes optional `tag_labels` / `ui_labels` from the
  selection JSON, so one renderer serves both languages. Verified byte-identical output for
  the English edition after the change.
- 2026-10-18 — template fix: the snapshot table's fourth column header read `Confidence`
  while the renderer emits the as-of date there; now `As of` in `template.html` and
  `数据时点` in `template.zh.html`. The shipped `reports/2026-10-18.html` predates the fix and
  still shows the old header, because it was kept unchanged on request; the next render
  corrects it.
- 2026-10-18 — automation added: `moneymarket.config` (cadence, languages, editor, bind
  address), `scripts/config.py`, `scripts/generate.py` (collect -> quotes -> headless
  curation -> render -> verify, with per-step status in `logs/last_run.json`),
  `scripts/serve.py` (report browser + interval scheduler on `0.0.0.0:9100`) and the
  `moneymarket-web` systemd user unit. Default cadence 24h, both languages per run.
- 2026-10-18 — automation verified live. Self-tests in a throwaway copy: happy path
  renders both languages and passes `verify_html`; a second run skips; deleting the
  selection JSON fails the run with `reports=[]` and publishes nothing; editing
  `REPORT_INTERVAL` 24 -> 6 was picked up with no restart. A re-render of the 2026-10-18
  selection differs from the shipped file by only the header fix and the generator stamp,
  and `reports/2026-10-18.html` still hashes `efefc1c9...` — untouched.
  `moneymarket-web` is enabled and running: `/healthz` ok, both reports served
  (35,669 B / 34,224 B), `/status` valid JSON, `/reports/../../etc/passwd` -> 404, and
  `http://192.168.11.9:9100/` answers from the LAN address.
- 2026-10-18 — headless curation tested end to end for the first time (31 min, exit 0,
  24 items, exec summary and watchlist written, four articles read in full, honest
  degradation on paywalled sources). It also exposed the failure that matters: **4 of its
  24 source URLs were invented or truncated slugs.** Added `check_links()` — every item
  URL is fetched before render, a 404/410/451 fails the run so nothing is published, and
  403/429 counts as bot-walled rather than dead. The checker agreed with a real browser on
  6 of 6 probes.
- **Known defect, left in place deliberately:** the published 2026-10-18 reports carry
  **5 dead source links** (CNBC China GDP, CNBC France protests, TechCrunch Anthropic, two
  NYT Russia-sanctions pieces), all confirmed "Page Not Found" in a browser. The stories
  are real; the URLs were written from feed metadata rather than from the address a browser
  landed on. They are not repaired because the instruction was to keep that report as
  published — `check_links` now stops any future run from shipping one.
- 2026-10-18 — two scheduler bugs found by running it, both fixed:
  **a skip wrote no status**, so `next_due` stayed in the past and the scheduler spawned
  `generate.py` every 60 seconds (six cheap skip-runs before the fix landed; no LLM spend);
  and **serve compared today's date in UTC while generate names reports by local date**, so
  for seven hours a day it believed today's report was missing. Both now share
  `config.report_date()` (local), and a trigger-time floor in `logs/last_trigger` caps
  firing to once per interval whatever the status file says.
