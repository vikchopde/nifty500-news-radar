#!/usr/bin/env python3
"""
Nifty 500 News Radar — news-driven BUY / AVOID screener.

Pipeline:
  1. Load the Nifty 500 universe (niftyindices CSV, cached locally).
  2. Pull latest market news from Indian financial RSS feeds.
  3. Classify each item: event type, sentiment, market-impact score.
  4. Map each item to the Nifty 500 constituents it mentions.
  5. Aggregate per stock into a net signal and emit a risks-first
     BUY / AVOID / WATCH report plus an upcoming-catalysts section.

This is the DETERMINISTIC layer. It surfaces *candidates* with their
supporting headlines; the final buy/avoid judgement (and confirmation via
web search of upcoming events) is meant to be done by the analysis layer
described in SKILL.md. Nothing here is investment advice.

Usage:
    python3 nifty500_news_radar.py                       # markdown report, last 2 days
    python3 nifty500_news_radar.py --days 3 --format json
    python3 nifty500_news_radar.py --output reports/today.md
    python3 nifty500_news_radar.py --universe-file my500.csv
    python3 nifty500_news_radar.py --refresh-universe     # force re-download
"""

import argparse
import csv
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


# ─────────────────────────────────────────────────────────────────────────────
# Universe (Nifty 500)
# ─────────────────────────────────────────────────────────────────────────────

NIFTY500_CSV_URL = "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv"
CACHE_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "reports", "nifty500_universe.csv")

# Only TRUE corporate-form suffixes are stripped. Geographic / descriptive words
# (India, Bank, Oil, Industries...) are kept so a name never collapses to a
# generic word like "bank" that matches unrelated headlines.
SUFFIX_TOKENS = {
    "ltd", "limited", "ltd.", "plc", "pvt", "private",
    "corporation", "corp", "company", "the",
}

# A single-token name-phrase this short or this generic is too risky to match on
# its own — require the ticker symbol instead.
GENERIC_NAME_WORDS = {
    "bank", "india", "oil", "power", "steel", "motors", "finance", "cement",
    "energy", "gas", "auto", "life", "insurance", "petroleum", "chemicals",
    "industries", "international", "national", "general", "first", "new",
}

# Common headline abbreviations -> NSE symbol (boosts recall without false hits).
ALIASES = {
    "sbi": "SBIN", "state bank": "SBIN", "hul": "HINDUNILVR", "l&t": "LT",
    "larsen": "LT", "m&m": "M&M", "bob": "BANKBARODA", "bank of baroda": "BANKBARODA",
    "tcs": "TCS", "infosys": "INFY", "hdfc bank": "HDFCBANK", "icici bank": "ICICIBANK",
    "axis bank": "AXISBANK", "kotak": "KOTAKBANK", "airtel": "BHARTIARTL",
    "maruti": "MARUTI", "sun pharma": "SUNPHARMA", "dr reddy": "DRREDDY",
    "tata motors": "TATAMOTORS", "tata steel": "TATASTEEL", "jsw steel": "JSWSTEEL",
    "coal india": "COALINDIA", "bajaj finance": "BAJFINANCE", "ultratech": "ULTRACEMCO",
}


@dataclass
class Stock:
    symbol: str
    name: str
    industry: str
    name_phrase: str  # normalised name (suffixes removed) used for matching


def _normalise(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9& ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _name_phrase(company_name: str) -> str:
    """Drop corporate suffixes so 'Reliance Industries Ltd' -> 'reliance industries'."""
    tokens = [t for t in _normalise(company_name).split() if t and t not in SUFFIX_TOKENS]
    return " ".join(tokens)


def load_universe(universe_file: Optional[str] = None, refresh: bool = False) -> list[Stock]:
    """Load Nifty 500 constituents. Prefers an explicit file, then cache, then download."""
    raw = None

    if universe_file and os.path.exists(universe_file):
        with open(universe_file, encoding="utf-8-sig") as f:
            raw = f.read()
    elif not refresh and os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8-sig") as f:
            raw = f.read()

    if raw is None:
        if not HAS_REQUESTS:
            print("ERROR: requests not installed and no cached universe.", file=sys.stderr)
            sys.exit(1)
        resp = requests.get(NIFTY500_CSV_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        resp.raise_for_status()
        raw = resp.text
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(raw)

    stocks: list[Stock] = []
    reader = csv.DictReader(io.StringIO(raw))
    for row in reader:
        # niftyindices headers: "Company Name","Industry","Symbol","Series","ISIN Code"
        symbol = (row.get("Symbol") or row.get("symbol") or "").strip().upper()
        name = (row.get("Company Name") or row.get("company name") or "").strip()
        industry = (row.get("Industry") or row.get("industry") or "").strip()
        if not symbol or not name:
            continue
        stocks.append(Stock(symbol=symbol, name=name, industry=industry,
                            name_phrase=_name_phrase(name)))
    return stocks


# ─────────────────────────────────────────────────────────────────────────────
# News sources + classification (engine reused from india-news-tracker)
# ─────────────────────────────────────────────────────────────────────────────

RSS_FEEDS = [
    ("MoneyControl", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("MoneyControl", "https://www.moneycontrol.com/rss/latestnews.xml"),
    ("MoneyControl", "https://www.moneycontrol.com/rss/business.xml"),
    ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Economic Times", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
    ("LiveMint", "https://www.livemint.com/rss/markets"),
    ("LiveMint", "https://www.livemint.com/rss/companies"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("NDTV Profit", "https://feeds.feedburner.com/ndtvprofit-latest"),
]

# Catalyst event types — these are the "big good/bad news" the user cares about.
CATALYST_EVENTS = {"Earnings", "M&A", "Rating", "Corporate Action", "Regulatory", "Order Win"}

EVENT_KEYWORDS = {
    "Earnings": ["quarterly results", "q1", "q2", "q3", "q4", "earnings", "profit",
                 "revenue", "pat", "ebitda", "results", "guidance", "margin"],
    "Corporate Action": ["dividend", "bonus", "stock split", "buyback", "rights issue",
                         "record date", "ex-date"],
    "M&A": ["acquisition", "merger", "demerger", "takeover", "stake sale", "buyout",
            "amalgamation", "joint venture", "acquires", "acquire"],
    "Order Win": ["order win", "bags order", "wins order", "order book", "contract win",
                  "secures contract", "bags contract", "letter of award"],
    "Management": ["ceo", "chairman", "appointed", "resigned", "cfo", "steps down"],
    "Regulatory": ["sebi", "rbi", "circular", "penalty", "probe", "investigation",
                   "show cause", "ban", "raid", "enforcement directorate",
                   "income tax", "gst notice"],
    "Institutional": ["fii", "fpi", "dii", "bulk deal", "block deal", "promoter", "pledge",
                      "stake buy", "stake sell"],
    "Rating": ["upgrade", "downgrade", "target price", "outperform", "underperform",
               "buy rating", "sell rating", "hold rating", "rating cut", "rating raised"],
    "Legal": ["court", "nclt", "arbitration", "lawsuit", "verdict", "insolvency"],
}

BULLISH_KEYWORDS = [
    "rally", "rallies", "surge", "surges", "surged", "soar", "soars", "gain", "gains",
    "gained", "jump", "jumps", "jumped", "rise", "rises", "rose", "bullish",
    "record high", "breakout", "upgrade", "upgraded", "outperform", "beat", "beats",
    "strong results", "boom", "recovery", "robust", "profit jumps", "profit rises",
    "order win", "bags order", "wins", "all-time high", "multibagger", "buy rating",
    "stake buy", "raises guidance", "margin expansion", "accumulate",
]

BEARISH_KEYWORDS = [
    "crash", "crashes", "plunge", "plunges", "plunged", "sink", "sinks", "fall",
    "falls", "fell", "drop", "drops", "dropped", "decline", "declines", "bearish",
    "slump", "slumps", "breakdown", "downgrade", "downgraded", "downgrades",
    "underperform", "miss", "misses", "weak results", "contraction", "slowdown",
    "profit falls", "profit drops", "loss", "probe", "penalty", "ban", "raid",
    "fraud", "default", "52-week low", "pledge", "resigns", "cuts guidance",
    "margin pressure", "show cause", "sell rating", "reduce",
]


@dataclass
class NewsItem:
    title: str
    source: str
    published: str
    link: str
    summary: str = ""
    event_type: str = "General"
    sentiment: str = "Neutral"
    impact_score: int = 3
    stocks: list = field(default_factory=list)


def _kw_hits(text: str, kws: list[str]) -> int:
    """Count keywords present as whole words ('ban' won't match 'bank')."""
    return sum(1 for kw in kws if re.search(rf"\b{re.escape(kw)}\b", text))


def classify_event(text: str) -> str:
    scores = {}
    for event_type, kws in EVENT_KEYWORDS.items():
        hits = _kw_hits(text, kws)
        if hits:
            scores[event_type] = hits
    return max(scores, key=scores.get) if scores else "General"


def detect_sentiment(text: str) -> str:
    bull = _kw_hits(text, BULLISH_KEYWORDS)
    bear = _kw_hits(text, BEARISH_KEYWORDS)
    if bull > bear and bull >= 1:
        return "Bullish"
    if bear > bull and bear >= 1:
        return "Bearish"
    return "Neutral"


def score_impact(item: "NewsItem") -> int:
    score = 3
    if item.event_type in ("M&A", "Regulatory", "Legal"):
        score += 2
    elif item.event_type in ("Earnings", "Rating", "Order Win", "Institutional"):
        score += 1
    if item.sentiment in ("Bullish", "Bearish"):
        score += 1
    if len(item.stocks) >= 1:
        score += 1
    return min(10, max(1, score))


def match_stocks(text_norm: str, raw_text: str, universe: list[Stock],
                 valid_symbols: set) -> list[str]:
    """Return Nifty 500 symbols mentioned in the headline text.

    Three signals, precision-first:
      1. Ticker as a whole word, matched CASE-SENSITIVELY against the original
         text (real tickers are upper-case; the word "Oil" won't match "OIL").
      2. Full normalised company-name phrase (e.g. "bank of india"), unless it
         collapsed to a single generic word.
      3. A curated alias (e.g. "sbi" -> SBIN).
    """
    found = set()
    for s in universe:
        if len(s.symbol) >= 3 and re.search(rf"\b{re.escape(s.symbol)}\b", raw_text):
            found.add(s.symbol)
            continue
        phrase = s.name_phrase
        if len(phrase) < 4:
            continue
        tokens = phrase.split()
        if len(tokens) == 1 and (len(phrase) < 5 or phrase in GENERIC_NAME_WORDS):
            continue  # too generic to match on the name alone
        if re.search(rf"\b{re.escape(phrase)}\b", text_norm):
            found.add(s.symbol)

    for alias, sym in ALIASES.items():
        if sym in valid_symbols and re.search(rf"\b{re.escape(alias)}\b", text_norm):
            found.add(sym)

    return list(found)


def fetch_news(universe: list[Stock], days_back: int, per_feed: int = 30) -> list[NewsItem]:
    if not HAS_FEEDPARSER:
        print("ERROR: feedparser not installed. Run: pip install feedparser", file=sys.stderr)
        sys.exit(1)

    items: list[NewsItem] = []
    seen = set()
    valid_symbols = {s.symbol for s in universe}
    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:  # noqa: BLE001
            print(f"Warning: failed feed {url}: {e}", file=sys.stderr)
            continue
        for entry in feed.entries[:per_feed]:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            summary = re.sub(r"<[^>]+>", "", entry.get("summary", "")).strip()[:300]
            link = entry.get("link", "")
            published = entry.get("published", entry.get("updated", ""))

            norm = re.sub(r"[^a-z0-9& ]", " ", (title + " " + summary).lower())
            dedup_key = re.sub(r"[^a-z0-9]", "", title.lower())[:60]
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            item = NewsItem(title=title, source=source, published=published,
                            link=link, summary=summary)
            item.stocks = match_stocks(norm, title + " " + summary, universe, valid_symbols)
            item.event_type = classify_event(norm)
            item.sentiment = detect_sentiment(norm)
            item.impact_score = score_impact(item)
            items.append(item)
    return items


# ─────────────────────────────────────────────────────────────────────────────
# Per-stock aggregation → BUY / AVOID / WATCH
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StockSignal:
    symbol: str
    name: str
    industry: str
    net_score: float = 0.0
    bull_items: list = field(default_factory=list)
    bear_items: list = field(default_factory=list)
    neutral_items: list = field(default_factory=list)
    catalysts: set = field(default_factory=set)

    @property
    def verdict(self) -> str:
        has_catalyst = bool(self.catalysts)
        if self.net_score >= 1.5 and self.bull_items:
            return "BUY" if has_catalyst else "WATCH"
        if self.net_score <= -1.5 and self.bear_items:
            return "AVOID"
        return "WATCH"

    @property
    def all_items(self) -> list:
        return self.bear_items + self.bull_items + self.neutral_items


def build_signals(items: list[NewsItem], universe: list[Stock]) -> dict[str, StockSignal]:
    by_symbol = {s.symbol: s for s in universe}
    signals: dict[str, StockSignal] = {}
    for item in items:
        for sym in item.stocks:
            meta = by_symbol.get(sym)
            if not meta:
                continue
            sig = signals.get(sym)
            if sig is None:
                sig = StockSignal(symbol=sym, name=meta.name, industry=meta.industry)
                signals[sym] = sig
            weight = item.impact_score / 5.0
            if item.sentiment == "Bullish":
                sig.net_score += weight
                sig.bull_items.append(item)
            elif item.sentiment == "Bearish":
                sig.net_score -= weight
                sig.bear_items.append(item)
            else:
                sig.neutral_items.append(item)
            if item.event_type in CATALYST_EVENTS:
                sig.catalysts.add(item.event_type)
    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_items(items: list[NewsItem], limit: int = 4) -> list[str]:
    out = []
    for it in items[:limit]:
        icon = {"Bullish": "🟢", "Bearish": "🔴"}.get(it.sentiment, "🟡")
        out.append(f"    - {icon} [{it.event_type} · {it.impact_score}/10] {it.title} "
                   f"_(via {it.source})_")
    return out


def format_markdown(signals: dict[str, StockSignal], items: list[NewsItem],
                    universe_size: int) -> str:
    now = datetime.now()
    buys = sorted([s for s in signals.values() if s.verdict == "BUY"],
                  key=lambda s: -s.net_score)
    avoids = sorted([s for s in signals.values() if s.verdict == "AVOID"],
                    key=lambda s: s.net_score)
    watch = sorted([s for s in signals.values() if s.verdict == "WATCH"],
                   key=lambda s: -abs(s.net_score))

    L = []
    L.append("# Nifty 500 News Radar — Buy / Avoid")
    L.append(f"\n**Generated:** {now.strftime('%A, %d %B %Y %H:%M IST')}  ")
    L.append(f"**Universe:** {universe_size} stocks · **News scanned:** {len(items)} · "
             f"**Stocks with news:** {len(signals)}")
    L.append("\n> Signal layer only — verify each name and any upcoming event before acting. "
             "Not investment advice.\n")

    # RULE 1: red flags / avoid FIRST.
    L.append("---\n## ⚠️ AVOID — negative catalysts / red flags")
    if avoids:
        for s in avoids:
            cat = ", ".join(sorted(s.catalysts)) or "sentiment"
            L.append(f"\n**{s.symbol}** — {s.name}  ")
            L.append(f"  score `{s.net_score:+.1f}` · {cat} · {s.industry}")
            L.extend(_fmt_items(s.bear_items + s.bull_items))
    else:
        L.append("\n_No clear negative-catalyst names in this window._")

    L.append("\n---\n## ✅ BUY candidates — positive catalysts")
    if buys:
        for s in buys:
            cat = ", ".join(sorted(s.catalysts)) or "sentiment"
            L.append(f"\n**{s.symbol}** — {s.name}  ")
            L.append(f"  score `{s.net_score:+.1f}` · {cat} · {s.industry}")
            L.extend(_fmt_items(s.bull_items + s.bear_items))
    else:
        L.append("\n_No strong positive-catalyst names in this window._")

    L.append("\n---\n## 👀 WATCH — mixed / developing")
    if watch:
        for s in watch[:15]:
            cat = ", ".join(sorted(s.catalysts)) or "sentiment"
            L.append(f"- **{s.symbol}** ({cat}) score `{s.net_score:+.1f}` — "
                     f"{len(s.all_items)} item(s)")
    else:
        L.append("\n_None._")

    # Upcoming / event catalysts mapped to constituents.
    catalyst_items = [it for it in items
                      if it.event_type in CATALYST_EVENTS and it.stocks]
    L.append("\n---\n## 📅 Event catalysts in the news")
    if catalyst_items:
        L.append("\n| Stock(s) | Event | Sentiment | Headline |")
        L.append("|---|---|---|---|")
        for it in sorted(catalyst_items, key=lambda x: -x.impact_score)[:25]:
            syms = ", ".join(it.stocks[:3])
            icon = {"Bullish": "🟢", "Bearish": "🔴"}.get(it.sentiment, "🟡")
            title = it.title.replace("|", "/")[:90]
            L.append(f"| {syms} | {it.event_type} | {icon} | {title} |")
    else:
        L.append("\n_No event-type catalysts mapped to Nifty 500 names in this window._")

    L.append("\n---\n*Sources: MoneyControl, Economic Times, LiveMint, Business Standard, "
             "NDTV Profit (RSS). Educational use only — not investment advice.*")
    return "\n".join(L)


def format_json(signals: dict[str, StockSignal], items: list[NewsItem]) -> str:
    def sig_dict(s: StockSignal):
        return {
            "symbol": s.symbol, "name": s.name, "industry": s.industry,
            "net_score": round(s.net_score, 2), "verdict": s.verdict,
            "catalysts": sorted(s.catalysts),
            "headlines": [{"title": it.title, "sentiment": it.sentiment,
                           "event_type": it.event_type, "impact": it.impact_score,
                           "source": it.source, "link": it.link} for it in s.all_items],
        }
    return json.dumps({
        "generated_at": datetime.now().isoformat(),
        "news_scanned": len(items),
        "stocks_with_news": len(signals),
        "buy": [sig_dict(s) for s in signals.values() if s.verdict == "BUY"],
        "avoid": [sig_dict(s) for s in signals.values() if s.verdict == "AVOID"],
        "watch": [sig_dict(s) for s in signals.values() if s.verdict == "WATCH"],
    }, indent=2, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser(description="Nifty 500 news-driven Buy/Avoid screener")
    ap.add_argument("--days", type=int, default=2, help="(informational) lookback window")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--output", type=str, default=None, help="write report to file")
    ap.add_argument("--universe-file", type=str, default=None,
                    help="CSV with a Nifty 500 list (Company Name, Symbol columns)")
    ap.add_argument("--refresh-universe", action="store_true",
                    help="force re-download of the Nifty 500 list")
    ap.add_argument("--per-feed", type=int, default=30, help="max items per RSS feed")
    args = ap.parse_args()

    print("Loading Nifty 500 universe...", file=sys.stderr)
    universe = load_universe(args.universe_file, args.refresh_universe)
    print(f"  {len(universe)} constituents loaded.", file=sys.stderr)

    print("Fetching news from RSS feeds...", file=sys.stderr)
    items = fetch_news(universe, args.days, args.per_feed)
    print(f"  {len(items)} unique news items.", file=sys.stderr)

    signals = build_signals(items, universe)
    print(f"  {len(signals)} Nifty 500 names matched to news.", file=sys.stderr)

    if args.format == "json":
        output = format_json(signals, items)
    else:
        output = format_markdown(signals, items, len(universe))

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
