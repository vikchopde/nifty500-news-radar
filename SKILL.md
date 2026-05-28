---
name: nifty500-news-radar
description: Scan the latest Indian market news across the Nifty 500, surface stocks with material good/bad catalysts, and produce a risks-first BUY / AVOID report. Use for a daily news-driven buy/avoid scan or when asked "what's the news telling us to buy or avoid in the Nifty 500".
---

# Nifty 500 News Radar

Turn the day's market news into a short, defensible **BUY / AVOID** list for the
Nifty 500 — and the *reasons* behind each call.

## Two-layer design (important)

This skill is deliberately split so each layer does what it's good at:

| Layer | Does | Strength / weakness |
|-------|------|---------------------|
| **1. Script** (`scripts/nifty500_news_radar.py`) | Fetches RSS news, maps headlines to Nifty 500 constituents, scores sentiment + impact, emits candidate BUY/AVOID/WATCH + an event-catalyst table | Fast, deterministic, precise *candidate surfacing* — but keyword sentiment is shallow (e.g. it can mislabel a broker "Reduce" call) |
| **2. You (Claude)** | Read the script's JSON, verify each candidate, confirm upcoming events, apply judgment, write the final report | This is where nuance, cross-checking, and the final verdict live |

**Never ship the script's raw output as the verdict.** It is the candidate list.
Your job is to confirm and judge.

## Workflow

### Step 1 — Run the screener (get candidates)

```bash
python3 scripts/nifty500_news_radar.py --format json --output reports/signals.json
```

This returns `buy`, `avoid`, and `watch` arrays. Each entry has the symbol, name,
industry, a `net_score`, detected `catalysts`, and the supporting `headlines`
(with per-headline sentiment, event type, impact, and source link).

### Step 2 — Verify each candidate (do NOT trust keyword sentiment)

For every name in `avoid` and `buy` (and the strongest `watch` names), confirm the
story before you keep it. **Run these FOUR searches IN PARALLEL** (single message,
multiple WebSearch calls) — this is a hard rule:

```
Search A: <Company> stock news last 7 days
Search B: <Company> Q4 FY26 results / latest results
Search C: <Company> broker target price upgrade downgrade 2026
Search D: <Company> breaking news today
```

Use the results to:
- Correct the script's sentiment (e.g. "Reduce/Accumulate/Add" broker calls, or a
  market-wide headline that isn't really about this stock — drop those).
- Confirm whether the catalyst is **fresh and material** or stale/priced-in.
- Catch **upcoming** events the RSS missed (board meeting / results date, ex-date,
  regulatory deadline). Add a results/earnings-calendar check:
  `WebSearch: "NSE results calendar this week"` and
  `WebSearch: "<Company> board meeting date"`.

### Step 3 — Write the report (RED FLAGS FIRST)

Always present the bear case / red flags **before** the bull case. Order the report:

1. **⚠️ AVOID** — negative catalysts. For each: the red flag, why it matters, and
   the specific condition that would change the view.
2. **✅ BUY candidates** — positive catalysts, with the bull case **and** the
   residual risk for each (never a one-sided pitch).
3. **📅 Upcoming catalysts** — earnings/board-meeting/ex-dates in the next ~5
   trading days for Nifty 500 names, flagged as potential good/bad triggers.
4. **Verdict line per name**: BUY / AVOID / WATCH with a one-line "re-evaluate if…".

Every claim must cite a source. State explicitly when a search found nothing
material. End with: *"Educational only — not investment advice."*

## Running as a scheduled routine

The script is web/RSS based, so it runs fine unattended in the cloud (no broker
login needed — see the note below). A routine prompt should:

1. Clone this repo, `pip install -r requirements.txt`.
2. Run Step 1 to produce `reports/signals.json`.
3. Do Step 2 (parallel verification) for the top ~10 candidates (cap to stay in
   the runtime budget; deepest |net_score| first).
4. Post the Step 3 report to the chosen channel (Slack/email).

Suggested cadence: once after market close (e.g. `0 11 * * 1-5` UTC ≈ 16:30 IST)
for an end-of-day "news → tomorrow's buy/avoid" brief. Routines have a **1-hour
minimum** cron interval.

### Mandatory rules to inline in any routine prompt
- **RULE 1 — red flags before bull case** (Step 3 ordering above).
- **RULE 2 — four parallel news searches per name** (Step 2).
These do not propagate from local config to a remote routine, so paste them in.

## Live price (optional, interactive only)
Zerodha Kite MCP can confirm the price reaction to a catalyst, but its session is
**per-connection and expires daily** — it cannot be kept logged in for an
unattended routine. Use Kite only in an interactive run; otherwise rely on the
news + (optionally) yfinance for price context.

## Limitations
- Coverage = whatever the RSS feeds carry that day; a quiet name simply won't appear.
- Headline→stock matching is precision-first (ticker, full name phrase, or a
  curated alias). It can miss a stock referred to only by a nickname.
- Keyword sentiment is a triage signal, not truth — Step 2 is what makes it reliable.
