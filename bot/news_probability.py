#!/usr/bin/env python3
"""News-based probability: reads live headlines from many outlets and turns them into
P(market goes up next).

How it works
  1. Pulls recent headlines (Yahoo Finance news search: Investor's Business Daily, Barron's,
     Investing.com, Motley Fool, Zacks, MT Newswires, ...) for several market queries.
  2. Scores each headline with a finance word list: bullish words (rally, surge, beat, ...)
     vs. bearish words (plunge, selloff, miss, ...), with simple negation handling.
  3. Weights each headline by how fresh it is (half-life in hours) and how market-relevant.
  4. Net sentiment (-1..+1) -> probability. The mapping is deliberately capped at 40%-60%:
     headline sentiment is a weak signal and has NOT been calibrated on long history.
  5. Sanity check: over the few days of headlines available, did hourly news sentiment agree
     with the next hour's price move? (Small sample, read it as a rough check only.)

Standard library only.

    python bot/news_probability.py                  # MNQ=F
    python bot/news_probability.py --symbol QQQ --halflife 3 --json

Research tool, not financial advice.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request

from candle_probability import fetch

QUERIES = ("Nasdaq", "Nasdaq 100 futures", "QQQ", "stock market today", "Wall Street stocks",
           "Federal Reserve", "inflation", "Treasury yields", "Nvidia", "tech stocks", "S&P 500")

# word stems: a headline word matches if it starts with one of these
BULL = ("rall", "surg", "soar", "jump", "gain", "climb", "rise", "rises", "rose", "rising", "rebound",
        "recover", "record", "beat", "tops", "topped", "upgrad", "bull", "boost", "strong", "optimis",
        "outperform", "upbeat", "breakout", "higher", "boom", "advance", "upside", "cool", "eases",
        "easing", "win", "wins", "buy", "growth", "accelerat", "exceed", "lift")
BEAR = ("fall", "fell", "drop", "slip", "slid", "plung", "tumbl", "sink", "sank", "selloff", "sell-off",
        "loss", "lose", "miss", "downgrad", "bear", "weak", "fear", "recession", "crash", "slump",
        "warn", "worr", "concern", "risk", "tariff", "layoff", "scandal", "probe", "lawsuit", "lower",
        "hot", "hike", "decline", "cut", "pressure", "turmoil", "panic", "default", "bankrupt", "fraud",
        "slow", "disappoint", "down", "retreat", "rout", "bubble", "costlier", "shed")
NEGATE = {"not", "no", "never", "despite", "without", "hasn't", "hasnt", "isn't", "isnt", "won't", "wont"}
# phrases whose plain reading is misleading
PHRASES = {"rate cut": 1, "rate cuts": 1, "cuts rates": 1, "cut rates": 1, "rate hike": -1,
           "hot inflation": -1, "hot cpi": -1, "cooling inflation": 1, "inflation cools": 1,
           "all-time high": 1, "record high": 1, "record low": -1, "price target raised": 1,
           "raises guidance": 1, "cuts guidance": -1, "tick higher": 1, "shrugs off": 1}
MARKET = ("nasdaq", "stock", "futures", "wall st", "s&p", "dow", "fed", "inflation", "cpi", "nvidia",
          "tech", "rate", "treasur", "yield", "econom", "market", "qqq", "earnings", "jobs", "tariff")


def fetch_news(queries=QUERIES, count=50):
    seen, items = set(), []
    for q in queries:
        url = ("https://query1.finance.yahoo.com/v1/finance/search?q="
               f"{urllib.parse.quote(q)}&newsCount={count}&quotesCount=0")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                news = json.load(r).get("news", [])
        except Exception as e:  # one failed query should not stop the rest
            print(f"news query {q!r} failed: {e}", file=sys.stderr)
            continue
        for n in news:
            key = n.get("uuid") or n.get("title")
            if key in seen or not n.get("title") or not n.get("providerPublishTime"):
                continue
            seen.add(key)
            items.append({"t": n["providerPublishTime"], "title": n["title"],
                          "publisher": n.get("publisher", "?")})
    return sorted(items, key=lambda x: x["t"])


def score_headline(title: str) -> float:
    """-1 (bearish) .. +1 (bullish), 0 if no signal words."""
    text = title.lower().replace("’", "'")
    pos = neg = 0
    for phrase, s in PHRASES.items():
        if phrase in text:
            pos += s > 0
            neg += s < 0
            text = text.replace(phrase, " ")
    words = re.findall(r"[a-z&'-]+", text)
    for i, w in enumerate(words):
        s = 1 if w.startswith(BULL) else -1 if w.startswith(BEAR) else 0
        if s and any(p in NEGATE for p in words[max(0, i - 2):i]):
            s = -s
        pos += s > 0
        neg += s < 0
    return (pos - neg) / (pos + neg) if pos + neg else 0.0


def relevance(title: str) -> float:
    t = title.lower()
    return 1.0 if any(k in t for k in MARKET) else 0.3


def net_sentiment(items, now, halflife_h=6.0, window_h=24.0):
    """Recency- and relevance-weighted mean headline score using only items before `now`."""
    num = den = 0.0
    n = bull = bear = 0
    for it in items:
        age_h = (now - it["t"]) / 3600
        if age_h < 0 or age_h > window_h:
            continue
        s = it.get("score")
        if s is None:
            s = it["score"] = score_headline(it["title"])
        w = 0.5 ** (age_h / halflife_h) * relevance(it["title"])
        num += w * s
        den += w
        n += 1
        bull += s > 0
        bear += s < 0
    return (num / den if den else 0.0), n, bull, bear


def to_probability(net: float) -> float:
    """Capped at 40%-60%: uncalibrated, so it is kept deliberately modest."""
    return 0.5 + 0.10 * math.tanh(2.0 * net)


def sanity_check(items, bars, halflife_h, horizon_s=3600, min_news=5):
    """Hourly: sign of news sentiment vs. the next hour's price move, on available history."""
    if not items or not bars:
        return None
    times = [b["t"] for b in bars]
    pairs = []
    t = (items[0]["t"] // 3600 + 2) * 3600
    end = bars[-1]["t"] - horizon_s
    j = 0
    while t <= end:
        while j < len(times) and times[j] < t:
            j += 1
        if j < len(times) and times[j] - t < 900:           # market open at t
            k = j
            while k < len(times) and times[k] < t + horizon_s:
                k += 1
            if k < len(times) and times[k] - (t + horizon_s) < 900:
                net, n, _, _ = net_sentiment(items, t, halflife_h)
                if n >= min_news and abs(net) > 0.02:
                    pairs.append((net, bars[k]["c"] / bars[j]["c"] - 1))
        t += 3600
    if len(pairs) < 5:
        return {"n": len(pairs)}
    hits = sum((s > 0) == (r > 0) for s, r in pairs)
    ms, mr = sum(s for s, _ in pairs) / len(pairs), sum(r for _, r in pairs) / len(pairs)
    cov = sum((s - ms) * (r - mr) for s, r in pairs)
    vs, vr = sum((s - ms) ** 2 for s, _ in pairs), sum((r - mr) ** 2 for _, r in pairs)
    return {"n": len(pairs), "hit_rate": hits / len(pairs),
            "correlation": cov / math.sqrt(vs * vr) if vs > 0 and vr > 0 else 0.0}


def main(argv=None):
    ap = argparse.ArgumentParser(description="P(up) from live news headlines.")
    ap.add_argument("--symbol", default="MNQ=F", help="symbol used for the sanity check")
    ap.add_argument("--halflife", type=float, default=6.0, help="headline half-life in hours")
    ap.add_argument("--show", type=int, default=12, help="how many scored headlines to print")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    items = fetch_news()
    if not items:
        print("no headlines fetched", file=sys.stderr)
        return 1
    now = time.time()
    net, n, bull, bear = net_sentiment(items, now, args.halflife)
    p = to_probability(net)
    try:
        check = sanity_check(items, fetch(args.symbol, "5m"), args.halflife)
    except Exception as e:
        print(f"price fetch failed: {e}", file=sys.stderr)
        check = None
    recent = [it for it in items if it.get("score") and now - it["t"] < 24 * 3600]
    recent.sort(key=lambda it: -it["t"])
    result = {"symbol": args.symbol, "time": int(now), "headlines_total": len(items),
              "headlines_last_24h": n, "bullish": bull, "bearish": bear, "neutral": n - bull - bear,
              "publishers": len({it["publisher"] for it in items}),
              "net_sentiment": net, "p_up": p, "sanity_check": check,
              "headlines": [{"time": it["t"], "publisher": it["publisher"], "score": it["score"],
                             "title": it["title"]} for it in recent[:args.show]]}
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
        return 0

    print(f"Headlines: {len(items)} from {result['publishers']} outlets; {n} in the last 24h "
          f"({bull} bullish, {bear} bearish, {n - bull - bear} neutral)")
    print(f"Net news sentiment: {net:+.2f}  (-1 bearish .. +1 bullish)")
    lean = "leans UP" if p > 0.52 else "leans DOWN" if p < 0.48 else "no clear lean"
    print(f"News-based P(up next): {p:.1%}  -> {lean}")
    if check and check.get("hit_rate") is not None:
        print(f"Sanity check on {args.symbol}: {check['n']} hours tested, news agreed with the next "
              f"hour's move {check['hit_rate']:.0%} of the time, correlation {check['correlation']:+.2f}")
    else:
        print(f"Sanity check: not enough overlapping news and price history "
              f"({(check or {}).get('n', 0)} hours)")
    print("\nLatest scored headlines:")
    for it in recent[:args.show]:
        tag = "BULL" if it["score"] > 0 else "BEAR"
        print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(it['t']))} {tag} {it['score']:+.1f} "
              f"[{it['publisher']}] {it['title'][:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
