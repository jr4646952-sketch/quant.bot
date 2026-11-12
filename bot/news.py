"""Live news scorer for the Micro E-mini Nasdaq (MNQ). Standard library only.

Pulls headlines from public RSS feeds and, for each one, works out:
  * category  - what the story is about (Fed, inflation, jobs, tariffs, big tech, ...)
  * impact    - how much that kind of story usually moves the Nasdaq (3 = high, 1 = low)
  * direction - +1 bullish, -1 bearish, 0 unclear, from the words in the headline
  * why       - the words that triggered the call, so you can judge it yourself

This is rule-based (keyword matching), not AI. It is fast, free and needs no API key, but it
reads headlines literally: sarcasm, questions and "stocks fall despite good news" can fool it.
Treat it as a fast first read, then open the story.

    python bot/news.py            # print the current scored feed
"""
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

UA = {"User-Agent": "Mozilla/5.0 (quant.bot news reader)"}

FEEDS = {
    "Yahoo Finance": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EIXIC,%5ENDX,QQQ,NQ%3DF,MNQ%3DF&region=US&lang=en-US",
    "CNBC Top News": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "CNBC Economy": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "CNBC Tech": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "CNBC Investing": "https://www.cnbc.com/id/15839069/device/rss/rss.html",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
    "MarketWatch Top": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "Nasdaq.com": "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
    "Reuters (via Google)": "https://news.google.com/rss/search?q=when:1d+(Nasdaq+OR+%22Federal+Reserve%22+OR+inflation+OR+tariffs+OR+Nvidia)&hl=en-US&gl=US&ceid=US:en",
}

# --- categories: (name, impact 1-3, regex of trigger words, fixed direction or None) -----------
# A fixed direction means the topic itself is bullish/bearish regardless of verbs.
CATEGORIES = [
    ("Fed / rates", 3, r"\b(fed|fomc|powell|federal reserve|rate cut|rate cuts|rate hike|rate hikes|interest rates?|treasury yields?|10-year|bond yields?)\b", None),
    ("Inflation", 3, r"\b(cpi|pce|ppi|inflation|consumer prices|producer prices|price index)\b", None),
    ("Jobs", 3, r"\b(jobs report|payrolls|nonfarm|unemployment|jobless claims|labor market|job openings|jolts)\b", None),
    ("Tariffs / trade", 3, r"\b(tariffs?|trade war|trade deal|trade talks|export controls?|china trade|import duties)\b", None),
    ("Nvidia / chips", 3, r"\b(nvidia|nvda|amd|broadcom|tsmc|micron|intel|chipmakers?|chip stocks?|semiconductors?)\b", None),
    ("Big tech", 3, r"\b(apple|microsoft|amazon|alphabet|google|meta|tesla|netflix|magnificent seven|mag 7|big tech)\b", None),
    ("AI", 2, r"\b(ai|artificial intelligence|openai|chatgpt|data centers?|ai spending|ai bubble)\b", None),
    ("Earnings", 2, r"\b(earnings|quarterly results|guidance|revenue|profit|beats? estimates|misses? estimates|outlook)\b", None),
    ("Economy", 2, r"\b(gdp|recession|consumer spending|retail sales|consumer confidence|ism|pmi|manufacturing|housing starts|economic growth)\b", None),
    ("Geopolitics", 2, r"\b(war|missile|strikes?|attack|invasion|sanctions|ceasefire|iran|russia|ukraine|taiwan|middle east|north korea)\b", None),
    ("Washington", 2, r"\b(white house|trump|congress|senate|shutdown|debt ceiling|government funding|executive order|supreme court)\b", None),
    ("Oil / energy", 1, r"\b(oil prices?|crude|opec|brent|wti|gasoline|energy prices)\b", None),
    ("Crypto", 1, r"\b(bitcoin|crypto|ethereum|coinbase)\b", None),
    ("Market", 1, r"\b(nasdaq|s&p|dow|stocks?|wall street|futures|stock market|tech stocks|market rally|selloff|sell-off)\b", None),
]
CATEGORIES = [(n, w, re.compile(rx, re.I), d) for n, w, rx, d in CATEGORIES]

# --- direction words -----------------------------------------------------------------------
# Movement verbs flip meaning with the subject: "stocks rise" is bullish, "yields rise" is bearish.
MOVE_UP = r"""rally|rallies|rallied|surge|surges|surged|soar|soars|soared|jump|jumps|jumped|gain|gains|gained|climb|climbs|climbed|rise|rises|rose|rising|rebound|rebounds|rebounded|higher|up|advance|advances|advanced|boost|boosts|boosted|record high|all-time high|record close|green|hotter|hot|accelerates?|spikes?"""
MOVE_DOWN = r"""plunge|plunges|plunged|slump|slumps|slumped|sink|sinks|sank|tumble|tumbles|tumbled|fall|falls|fell|falling|drop|drops|dropped|slide|slides|slid|lower|down|decline|declines|declined|retreat|retreats|cooler|cools|cooling|eases|easing|slows|slowing|red|rout|crash|crashes|collapses?"""
# Fixed phrases mean the same thing for the Nasdaq whatever the subject.
FIXED_BULL = r"""beat|beats|tops estimates|upgrade|upgrades|upgraded|bullish|rate cut|rate cuts|cut rates|cuts rates|cutting rates|strong demand|strong sales|optimism|outperform|buy rating|ceasefire|trade deal|deal reached|truce|stimulus|dovish|resilient|better than expected|upbeat|soft landing|tariff relief|tariffs? paused|tariffs? delayed|exemption|raises guidance|raises forecast|record profit|record revenue|record sales|approval|approved|partnership|buyback|dividend hike|strong quarter|blowout"""
FIXED_BEAR = r"""miss|misses|missed|downgrade|downgrades|downgraded|bearish|recession|layoffs|rate hike|rate hikes|hike rates|hikes rates|sticky inflation|fears|fear|warns|warning|slowdown|weak|weaker|weakness|loss|losses|worse than expected|sell rating|tariffs?|sanctions|attack|strikes?|war|shutdown|probe|lawsuit|antitrust|bubble|hawkish|cuts guidance|cuts forecast|jitters|turmoil|selloff|sell-off|default|bankruptcy|ban|bans|crackdown|resigns?|resignation|steps down|stepping down|ousted|fired|quits|blow to|reckoning|scandal|investigation|recall|halt|outage|hack|breach|delays?|disappoints?|disappointing|cautious|caution|uncertainty"""
BULL_RE = re.compile(r"\b(" + MOVE_UP + "|" + FIXED_BULL + r")\b", re.I)  # used by the bot's simple tag
BEAR_RE = re.compile(r"\b(" + MOVE_DOWN + "|" + FIXED_BEAR + r")\b", re.I)
MOVE_UP_RE, MOVE_DOWN_RE = re.compile(r"\b(" + MOVE_UP + r")\b", re.I), re.compile(r"\b(" + MOVE_DOWN + r")\b", re.I)
FIXED_BULL_RE, FIXED_BEAR_RE = re.compile(r"\b(" + FIXED_BULL + r")\b", re.I), re.compile(r"\b(" + FIXED_BEAR + r")\b", re.I)
# "despite", "shrugs off" etc.: judge only the part before them ("stocks rise despite weak jobs data").
PIVOT_RE = re.compile(r"\b(despite|even as|shrugs? off|brushes? off|ignores?|in spite of)\b", re.I)
NEGATE_RE = re.compile(r"\b(no|not|won't|doesn't|isn't|unlikely|fails? to|halts?|pauses?|delays?|scraps?|drops? plan)\b", re.I)
QUESTION_RE = re.compile(r"\?\s*$|^\s*(why|how|what|is|are|will|can|should|could|would)\b", re.I)
# Subjects whose rise is bad for the Nasdaq (and whose fall is good).
INVERSE_RE = re.compile(r"\b(yields?|inflation|cpi|pce|ppi|oil|crude|unemployment|jobless claims|prices|rates?|dollar|vix|volatility|bond)\b", re.I)
STOCK_RE = re.compile(r"\b(stocks?|nasdaq|s&p|dow|shares|futures|wall street|tech|equities|market|nvidia|apple|microsoft|amazon|alphabet|google|meta|tesla)\b", re.I)


def score(title):
    """Return (direction, impact, categories, why) for a headline."""
    text = title.strip()
    cats = [(n, w) for n, w, rx, _ in CATEGORIES if rx.search(text)]
    impact = max((w for _, w in cats), default=1)
    cat_names = [n for n, _ in cats] or ["Other"]

    head = PIVOT_RE.split(text, maxsplit=1)[0]        # judge the main clause only
    ups = [m.group(0).lower() for m in MOVE_UP_RE.finditer(head)]
    downs = [m.group(0).lower() for m in MOVE_DOWN_RE.finditer(head)]
    fb = [m.group(0).lower() for m in FIXED_BULL_RE.finditer(head)]
    fs = [m.group(0).lower() for m in FIXED_BEAR_RE.finditer(head)]
    move = len(ups) - len(downs)
    # "Inflation jumps" / "yields rise" / "oil surges" are bearish for stocks, and vice versa,
    # unless the headline is clearly about stocks themselves.
    if move and INVERSE_RE.search(head) and not STOCK_RE.search(head):
        move = -move
    neg_words = [w for w in fs if NEGATE_RE.fullmatch(w)]   # "delays" is both a negation and bearish
    fixed = len(fb) - (len(fs) - len(neg_words))
    if fixed and NEGATE_RE.search(head):   # "no rate cut", "tariffs paused"
        fixed = -fixed
    fixed -= len(neg_words)
    total = fixed if fixed else move        # fixed phrases outrank movement verbs
    d = 1 if total > 0 else -1 if total < 0 else 0
    bulls, bears = ups + fb, downs + fs
    why = bulls + bears
    confidence = "low" if QUESTION_RE.search(text) or not why else "ok"
    return d, impact, cat_names, why, confidence


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _parse_feed(raw):
    root = ET.fromstring(raw)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items = list(root.iter("item")) or root.findall(".//a:entry", ns)
    for it in items:
        title = (it.findtext("title") or it.findtext("a:title", namespaces=ns) or "").strip()
        link = (it.findtext("link") or "").strip()
        if not link:
            l = it.find("a:link", ns)
            link = (l.get("href") if l is not None else "") or ""
        date = it.findtext("pubDate") or it.findtext("a:updated", namespaces=ns) or it.findtext("a:published", namespaces=ns) or ""
        yield title, link, date


def _ts(date):
    try:
        return int(parsedate_to_datetime(date).timestamp())
    except Exception:
        pass
    try:  # ISO 8601 (Atom)
        from datetime import datetime
        return int(datetime.fromisoformat(date.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def fetch_news(max_age_h=6, limit=60, feeds=None):
    """Fetch, de-duplicate and score headlines. Never raises because of one bad feed."""
    items, seen, errors = [], set(), []
    now = int(time.time())
    for name, url in (feeds or FEEDS).items():
        try:
            raw = http_get(url)
            parsed = list(_parse_feed(raw))
        except Exception as e:
            errors.append(f"{name}: {e.__class__.__name__}")
            continue
        for title, link, date in parsed:
            # Google News appends " - Source"; keep the real source name.
            source = name
            if name.startswith("Reuters") and " - " in title:
                title, source = title.rsplit(" - ", 1)
            key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            d, impact, cats, why, conf = score(title)
            items.append({"title": title, "link": link, "source": source, "t": _ts(date),
                          "tag": d, "impact": impact, "cats": cats, "why": why, "conf": conf})
    items.sort(key=lambda x: x["t"] or 0, reverse=True)
    recent = [x for x in items if x["t"] and now - x["t"] <= max_age_h * 3600]
    # Mood: impact-weighted average direction of recent headlines that have a direction.
    wsum = sum(x["impact"] for x in recent if x["tag"])
    mood = sum(x["tag"] * x["impact"] for x in recent) / wsum if wsum else 0.0
    high = [x for x in recent if x["impact"] >= 3 and x["tag"]]
    return {
        "score": round(mood, 3),
        "bullish": sum(1 for x in recent if x["tag"] > 0),
        "bearish": sum(1 for x in recent if x["tag"] < 0),
        "neutral": sum(1 for x in recent if x["tag"] == 0),
        "high_bullish": sum(1 for x in high if x["tag"] > 0),
        "high_bearish": sum(1 for x in high if x["tag"] < 0),
        "recent": len(recent),
        "window_hours": max_age_h,
        "headlines": items[:limit],
        "errors": errors,
        "fetched": now,
    }


if __name__ == "__main__":
    if len(sys.argv) > 1:  # score headlines given on the command line
        for h in sys.argv[1:]:
            print(score(h), "<-", h)
        sys.exit()
    n = fetch_news()
    print(f"mood {n['score']:+.2f}  bull {n['bullish']}  bear {n['bearish']}  errors {n['errors']}")
    for h in n["headlines"][:30]:
        tag = {1: "BULL", -1: "BEAR", 0: "----"}[h["tag"]]
        print(f"{tag} {'!' * h['impact']:<3} {', '.join(h['cats']):<18} {h['title'][:80]}  [{h['source']}]")
