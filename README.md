# Nifty 500 News Radar

A news-driven **BUY / AVOID** screener for the NSE Nifty 500. It pulls the latest
market news from Indian financial RSS feeds, maps each headline to the Nifty 500
constituent(s) it mentions, scores sentiment and market-impact, and emits a
risks-first report of which names have material good/bad catalysts.

> Two layers: a deterministic script that **surfaces candidates**, and an LLM
> "analysis layer" (see [`SKILL.md`](SKILL.md)) that **verifies and judges** them.
> The script output alone is a triage signal, not a verdict. Not investment advice.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Usage

```bash
# Markdown report to stdout
.venv/bin/python scripts/nifty500_news_radar.py

# JSON (for the analysis layer / a routine to consume)
.venv/bin/python scripts/nifty500_news_radar.py --format json --output reports/signals.json

# Save a markdown report
.venv/bin/python scripts/nifty500_news_radar.py --output reports/today.md

# Force-refresh the Nifty 500 constituent list
.venv/bin/python scripts/nifty500_news_radar.py --refresh-universe

# Use your own universe CSV (needs "Company Name" and "Symbol" columns)
.venv/bin/python scripts/nifty500_news_radar.py --universe-file my500.csv
```

## How it works

1. **Universe** — downloads the Nifty 500 list from niftyindices (cached to
   `reports/nifty500_universe.csv`).
2. **News** — fetches RSS from MoneyControl, Economic Times, LiveMint, Business
   Standard, NDTV Profit.
3. **Classify** — each item gets an event type (Earnings, M&A, Rating, Regulatory,
   …), a sentiment (🟢/🔴/🟡), and a 1–10 impact score.
4. **Map** — headlines are matched to constituents by ticker (case-sensitive),
   full company-name phrase, or a curated alias — precision-first to avoid
   false attributions.
5. **Aggregate** — per stock, sentiment is summed (impact-weighted) into a
   `net_score`, then bucketed into **AVOID / BUY / WATCH**.
6. **Report** — red-flags (AVOID) first, then BUY, then WATCH, plus an
   event-catalyst table.

## Output sections
- **⚠️ AVOID** — names with negative catalysts / red flags
- **✅ BUY candidates** — names with positive catalysts
- **👀 WATCH** — mixed or low-conviction
- **📅 Event catalysts** — earnings / ratings / M&A / regulatory items mapped to names

## Run as a scheduled routine
See [`SKILL.md`](SKILL.md) → "Running as a scheduled routine". The script is
RSS/web based, so it runs unattended (no broker login). Suggested cadence: once
after market close. Remote routines enforce a 1-hour minimum cron interval.

## Notes / limitations
- Coverage = whatever the feeds carry that day.
- Keyword sentiment is shallow by design — the analysis layer (`SKILL.md`)
  verifies each name with parallel news searches before issuing a verdict.
- Live Zerodha Kite price data only works in an interactive session (its login
  expires daily and can't be automated for unattended runs).
