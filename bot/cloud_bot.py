"""Paper-trading quant bot for the Micro E-mini Nasdaq (MNQ). Standard library only.

What it does on every run:
  1. Downloads recent price bars from Yahoo Finance (only fully *closed* bars are used).
  2. Turns each bar into 7 math features (momentum, trend, RSI, volatility, range position).
  3. Uses a logistic-regression model to estimate P(next bar closes higher).
     The model is trained on the oldest 70% of the data and graded on the newest 30%
     it has never seen. It only trades if that unseen test beats a "no skill" guess.
  4. Reads NASDAQ headlines and scores them bullish / bearish. The news score gives a
     separate "news-adjusted" probability, which is logged and graded live so you can
     see whether the news actually helps.
  5. Paper trades 1 contract with a stop loss, take profit and time limit, after costs.
  6. Saves everything to a JSON file that the tracker page shows.

Paper money only - this never connects to a broker. Not financial advice.

    python bot/cloud_bot.py                       # one run (used by GitHub Actions)
    SYMBOL=MNQ=F TIMEFRAME=1m python bot/cloud_bot.py
"""
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TF_SECONDS = {"1m": 60, "2m": 120, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "1d": 86400}
TF_RANGE = {"1m": "5d", "2m": "1mo", "5m": "1mo", "15m": "1mo", "30m": "1mo", "1h": "6mo", "1d": "2y"}
POINT_VALUE = {"MNQ=F": 2.0, "NQ=F": 20.0}

WARMUP = 30          # bars needed before features are valid
MIN_BARS = 150       # bars needed before the model is trained
MAX_SAMPLES = 3000   # most recent samples used for training
MAX_REPLAY = 500     # most missed bars replayed in one run

FEATURES = [
    ("mom_1", "Last bar move"),
    ("mom_3", "3-bar momentum"),
    ("mom_12", "12-bar momentum"),
    ("ema_gap", "Distance from 20-bar average"),
    ("rsi", "RSI (overbought / oversold)"),
    ("vol_regime", "Volatility rising / falling"),
    ("range_pos", "Position in 20-bar range"),
]

NEWS_FEEDS = {
    "Yahoo Finance": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EIXIC,%5ENDX,QQQ,NQ%3DF,MNQ%3DF&region=US&lang=en-US",
    "CNBC Markets": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "CNBC Tech": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
    "Nasdaq.com": "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
}
BULL_WORDS = [
    "rally", "rallies", "rallied", "surge", "surges", "surged", "soar", "soars", "soared", "jump", "jumps",
    "jumped", "gain", "gains", "gained", "climb", "climbs", "climbed", "rise", "rises", "rising", "rebound",
    "rebounds", "record high", "all-time high", "beat", "beats", "tops estimates", "upgrade", "upgraded",
    "bullish", "rate cut", "rate cuts", "cools", "cooling inflation", "strong demand", "optimism", "boost",
]
BEAR_WORDS = [
    "plunge", "plunges", "plunged", "slump", "slumps", "sink", "sinks", "sank", "tumble", "tumbles",
    "tumbled", "fall", "falls", "fell", "drop", "drops", "dropped", "slide", "slides", "selloff", "sell-off",
    "crash", "miss", "misses", "missed", "downgrade", "downgraded", "bearish", "recession", "layoffs",
    "tariff", "tariffs", "rate hike", "hot inflation", "fears", "warns", "warning", "slowdown", "weak",
]
BULL_RE = re.compile(r"\b(" + "|".join(map(re.escape, BULL_WORDS)) + r")\b", re.I)
BEAR_RE = re.compile(r"\b(" + "|".join(map(re.escape, BEAR_WORDS)) + r")\b", re.I)
UA = {"User-Agent": "Mozilla/5.0 (quant.bot paper trader)"}


# ----------------------------------------------------------------------------- config
def config():
    e = os.environ.get
    sym = e("SYMBOL", "MNQ=F")
    tf = e("TIMEFRAME", "5m")
    if tf not in TF_SECONDS:
        raise SystemExit(f"TIMEFRAME must be one of {', '.join(TF_SECONDS)}")
    return {
        "symbol": sym,
        "timeframe": tf,
        "start_cash": float(e("START_CASH", "10000")),
        "point_value": float(e("POINT_VALUE", str(POINT_VALUE.get(sym, 1.0)))),
        "commission": float(e("COMMISSION", "1.00")),      # $ per contract, per side
        "tick": float(e("TICK_SIZE", "0.25")),
        "slippage_ticks": float(e("SLIPPAGE_TICKS", "1")),  # per side
        "enter_long": float(e("ENTER_LONG", "0.55")),
        "enter_short": float(e("ENTER_SHORT", "0.45")),
        "stop_atr": float(e("STOP_ATR", "1.5")),
        "target_atr": float(e("TARGET_ATR", "2.0")),
        "max_hold": int(e("MAX_HOLD_BARS", "6")),
        "retrain_bars": int(e("RETRAIN_BARS", "100")),
        "require_edge": e("REQUIRE_EDGE", "1") != "0",
        "news_tilt": float(e("NEWS_TILT", "0.02")),
    }


# ----------------------------------------------------------------------------- data
def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_bars(symbol, tf):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol)
           + f"?interval={tf}&range={TF_RANGE[tf]}&includePrePost=false")
    data = json.loads(http_get(url))
    chart = data.get("chart") or {}
    res = chart.get("result") or []
    if not res:
        raise RuntimeError(f"Yahoo returned no data: {chart.get('error')}")
    r = res[0]
    q = (r.get("indicators") or {}).get("quote", [{}])[0]
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close")]
    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        try:
            o, h, l, c = (col[i] for col in cols)
        except IndexError:
            continue
        if None in (o, h, l, c):
            continue
        if bars and t <= bars[-1]["t"]:
            continue
        bars.append({"t": int(t), "o": float(o), "h": float(h), "l": float(l), "c": float(c)})
    return bars, r.get("meta") or {}


def score_headline(title):
    b, s = len(BULL_RE.findall(title)), len(BEAR_RE.findall(title))
    return 1 if b > s else -1 if s > b else 0


def fetch_news(max_age_h=6):
    items, seen, errors = [], set(), []
    now = time.time()
    for name, url in NEWS_FEEDS.items():
        try:
            root = ET.fromstring(http_get(url, timeout=10))
        except Exception as e:  # one bad feed shouldn't stop the bot
            errors.append(f"{name}: {e.__class__.__name__}")
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            key = title.lower()
            if not title or key in seen:
                continue
            seen.add(key)
            try:
                ts = parsedate_to_datetime(it.findtext("pubDate") or "").timestamp()
            except Exception:
                ts = None
            items.append({"title": title, "link": (it.findtext("link") or "").strip(),
                          "source": name, "t": int(ts) if ts else None, "tag": score_headline(title)})
    items.sort(key=lambda x: x["t"] or 0, reverse=True)
    recent = [x for x in items if x["t"] and now - x["t"] <= max_age_h * 3600]
    tagged = [x["tag"] for x in recent if x["tag"]]
    score = sum(tagged) / len(tagged) if tagged else 0.0
    return {
        "score": round(score, 3),
        "bullish": sum(1 for t in tagged if t > 0),
        "bearish": sum(1 for t in tagged if t < 0),
        "recent": len(recent),
        "window_hours": max_age_h,
        "headlines": items[:20],
        "errors": errors,
        "fetched": int(now),
    }


# ----------------------------------------------------------------------------- math
def _ema(vals, n):
    k, out, e = 2 / (n + 1), [], None
    for v in vals:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


def _wilder(vals, n):
    out, a = [], None
    for v in vals:
        a = v if a is None else a + (v - a) / n
        out.append(a)
    return out


def indicators(bars):
    c = [b["c"] for b in bars]
    tr, gains, losses = [], [], []
    for i, b in enumerate(bars):
        pc = c[i - 1] if i else b["c"]
        tr.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
        d = c[i] - pc
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = _wilder(gains, 14), _wilder(losses, 14)
    rsi = [50.0 if l == 0 and g == 0 else 100.0 if l == 0 else 100 - 100 / (1 + g / l)
           for g, l in zip(ag, al)]
    return {"c": c, "atr": _wilder(tr, 14), "atr_fast": _wilder(tr, 5), "ema": _ema(c, 20), "rsi": rsi}


def features(bars, ind, i):
    """Feature vector for bar i, using only bars 0..i (no peeking ahead)."""
    if i < WARMUP:
        return None
    a = ind["atr"][i]
    if not a or a <= 0:
        return None
    c = ind["c"]
    window = bars[i - 19:i + 1]
    hi, lo = max(b["h"] for b in window), min(b["l"] for b in window)
    return [
        (c[i] - c[i - 1]) / a,
        (c[i] - c[i - 3]) / (a * math.sqrt(3)),
        (c[i] - c[i - 12]) / (a * math.sqrt(12)),
        (c[i] - ind["ema"][i]) / a,
        (ind["rsi"][i] - 50) / 50,
        ind["atr_fast"][i] / a - 1,
        (c[i] - lo) / (hi - lo) - 0.5 if hi > lo else 0.0,
    ]


def _sigmoid(z):
    if z < -30:
        return 1e-13
    if z > 30:
        return 1 - 1e-13
    return 1 / (1 + math.exp(-z))


def _solve(A, b):
    """Gaussian elimination with partial pivoting."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        if abs(M[col][col]) < 1e-12:
            continue
        for r in range(col + 1, n):
            f = M[r][col] / M[col][col]
            if f:
                for k in range(col, n + 1):
                    M[r][k] -= f * M[col][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        if abs(M[r][r]) < 1e-12:
            continue
        x[r] = (M[r][n] - sum(M[r][k] * x[k] for k in range(r + 1, n))) / M[r][r]
    return x


def _standardize(model, x):
    return [1.0] + [max(-5.0, min(5.0, (v - m) / s)) for v, m, s in zip(x, model["mean"], model["std"])]


def fit_logistic(X, y, lam=2.0, iters=8):
    """Ridge-regularised logistic regression fitted with Newton's method (IRLS)."""
    d = len(X[0])
    n = len(X)
    mean = [sum(x[j] for x in X) / n for j in range(d)]
    std = [math.sqrt(sum((x[j] - mean[j]) ** 2 for x in X) / n) or 1.0 for j in range(d)]
    model = {"mean": mean, "std": std, "w": [0.0] * (d + 1)}
    Z = [_standardize(model, x) for x in X]
    w = model["w"]
    D = d + 1
    for _ in range(iters):
        H = [[0.0] * D for _ in range(D)]
        g = [0.0] * D
        for z, yy in zip(Z, y):
            p = _sigmoid(sum(wi * zi for wi, zi in zip(w, z)))
            err, wt = p - yy, p * (1 - p)
            for a in range(D):
                za = z[a]
                g[a] += err * za
                wa = wt * za
                Ha = H[a]
                for b in range(a, D):
                    Ha[b] += wa * z[b]
        for a in range(D):
            for b in range(a):
                H[a][b] = H[b][a]
            if a:
                H[a][a] += lam
                g[a] += lam * w[a]
        step = _solve(H, g)
        w = [wi - si for wi, si in zip(w, step)]
        if max(abs(s) for s in step) < 1e-6:
            break
    model["w"] = w
    return model


def predict(model, x):
    z = _standardize(model, x)
    contrib = [wi * zi for wi, zi in zip(model["w"][1:], z[1:])]
    return _sigmoid(model["w"][0] + sum(contrib)), contrib


def dataset(bars, ind, end):
    """Samples (i, x, y) for i < end, label = did bar i+1 close above bar i (ties skipped)."""
    out = []
    for i in range(WARMUP, end):
        x = features(bars, ind, i)
        if x is None or ind["c"][i + 1] == ind["c"][i]:
            continue
        out.append((i, x, 1 if ind["c"][i + 1] > ind["c"][i] else 0))
    return out[-MAX_SAMPLES:]


def grade(ps, ys, base_rate):
    n = len(ys)
    if not n:
        return {}
    brier = sum((p - y) ** 2 for p, y in zip(ps, ys)) / n
    base = sum((base_rate - y) ** 2 for y in ys) / n
    ll = -sum(math.log(max(1e-9, p if y else 1 - p)) for p, y in zip(ps, ys)) / n
    return {
        "accuracy": round(sum((p >= 0.5) == bool(y) for p, y in zip(ps, ys)) / n, 4),
        "brier": round(brier, 5),
        "baseline_brier": round(base, 5),
        "skill_pct": round(100 * (1 - brier / base), 2) if base else 0.0,
        "log_loss": round(ll, 5),
    }


# ----------------------------------------------------------------------------- paper broker
class Book:
    def __init__(self, d, cfg):
        self.d, self.cfg = d, cfg

    def _slip(self):
        return self.cfg["slippage_ticks"] * self.cfg["tick"]

    def _round(self, px):
        t = self.cfg["tick"]
        return round(round(px / t) * t, 4)

    def on_bar(self, bar):
        """Check stop / target / time limit against this bar's open, high, low, close."""
        pos = self.d.get("position")
        if not pos:
            return None
        pos["bars"] += 1
        s, o, h, l = pos["side"], bar["o"], bar["h"], bar["l"]
        price = reason = None
        if s == 1:
            if l <= pos["stop"]:          # if both were touched, assume the stop hit first
                price, reason = min(o, pos["stop"]), "stop"
            elif h >= pos["target"]:
                price, reason = max(o, pos["target"]), "target"
        else:
            if h >= pos["stop"]:
                price, reason = max(o, pos["stop"]), "stop"
            elif l <= pos["target"]:
                price, reason = min(o, pos["target"]), "target"
        if price is None and pos["bars"] >= self.cfg["max_hold"]:
            price, reason = bar["c"], "time"
        return None if price is None else self._close(bar["t"], price, reason)

    def _close(self, t, price, reason):
        pos, cfg = self.d["position"], self.cfg
        s = pos["side"]
        fill = price - s * self._slip()
        pnl = s * (fill - pos["entry"]) * cfg["point_value"] - 2 * cfg["commission"]
        self.d["cash"] = round(self.d["cash"] + pnl, 2)
        self.d["position"] = None
        return {"side": "long" if s == 1 else "short", "entry_t": pos["t"], "exit_t": t,
                "entry": pos["entry"], "exit": round(fill, 4), "pnl": round(pnl, 2),
                "reason": reason, "p": pos["p"], "bars": pos["bars"]}

    def maybe_enter(self, bar, p, atr):
        cfg = self.cfg
        if self.d.get("position") or not atr:
            return None
        side = 1 if p >= cfg["enter_long"] else -1 if p <= cfg["enter_short"] else 0
        if not side:
            return None
        c = bar["c"]
        self.d["position"] = {
            "side": side, "t": bar["t"], "p": round(p, 4), "bars": 0,
            "entry": round(c + side * self._slip(), 4),
            "stop": self._round(c - side * cfg["stop_atr"] * atr),
            "target": self._round(c + side * cfg["target_atr"] * atr),
        }
        return self.d["position"]

    def equity(self, price):
        pos = self.d.get("position")
        unreal = pos["side"] * (price - pos["entry"]) * self.cfg["point_value"] if pos else 0.0
        return round(self.d["cash"] + unreal, 2)


def trade_stats(trades):
    n = len(trades)
    if not n:
        return {"trades": 0, "net_pnl": 0.0, "win_rate": None, "avg_pnl": None, "max_drawdown": 0.0}
    cum = peak = dd = 0.0
    for t in trades:
        cum += t["pnl"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"trades": n, "net_pnl": round(cum, 2),
            "win_rate": round(sum(t["pnl"] > 0 for t in trades) / n, 4),
            "avg_pnl": round(cum / n, 2), "max_drawdown": round(dd, 2)}


# ----------------------------------------------------------------------------- training
def train(bars, ind, end, cfg):
    """Fit on the oldest 70%, grade on the unseen newest 30% (and paper-trade it), then refit on all."""
    data = dataset(bars, ind, end)
    if len(data) < 100:
        return None, {"error": "not enough data to train"}
    cut = int(len(data) * 0.7)
    tr, te = data[:cut], data[cut:]
    m = fit_logistic([x for _, x, _ in tr], [y for _, _, y in tr])
    base_rate = sum(y for _, _, y in tr) / len(tr)
    ps = [predict(m, x)[0] for _, x, _ in te]
    report = grade(ps, [y for _, _, y in te], base_rate)

    # Paper-trade the unseen test period with the train-only model, after costs.
    book = Book({"cash": cfg["start_cash"], "position": None}, cfg)
    trades = []
    for i in range(te[0][0], end + 1):
        if i > te[0][0]:
            t = book.on_bar(bars[i])
            if t:
                trades.append(t)
        x = features(bars, ind, i)
        if x is not None and i < end:
            book.maybe_enter(bars[i], predict(m, x)[0], ind["atr"][i])
    report["backtest"] = trade_stats(trades)

    # Edge = beats the no-skill guess on unseen data AND made money after costs there.
    report["has_edge"] = bool(report["brier"] < report["baseline_brier"]
                              and report["backtest"]["net_pnl"] > 0)
    report.update({"n_train": len(tr), "n_test": len(te), "base_rate": round(base_rate, 4),
                   "trained_t": bars[end]["t"],
                   "test_from_t": bars[te[0][0]]["t"], "test_to_t": bars[te[-1][0]]["t"]})

    final = fit_logistic([x for _, x, _ in data], [y for _, _, y in data])
    report["weights"] = {name: round(w, 4) for (name, _), w in zip(FEATURES, final["w"][1:])}
    return final, report


def pred_stats(preds):
    n = len(preds)
    if not n:
        return {"n": 0}
    ups = [p["up"] for p in preds]
    base = sum(ups) / n
    model = grade([p["p"] for p in preds], ups, base)
    news = grade([p["pn"] for p in preds], ups, base)
    buckets = []
    for lo, hi in ((0, .45), (.45, .5), (.5, .55), (.55, .6), (.6, 1.01)):
        sel = [p for p in preds if lo <= p["p"] < hi]
        buckets.append({"range": f"{int(lo * 100)}-{min(100, int(hi * 100))}%", "n": len(sel),
                        "avg_p": round(sum(p["p"] for p in sel) / len(sel), 4) if sel else None,
                        "up_rate": round(sum(p["up"] for p in sel) / len(sel), 4) if sel else None})
    conf = [p for p in preds if abs(p["p"] - 0.5) >= 0.05]
    return {"n": n, "up_rate": round(base, 4), "model": model, "news_adjusted": news, "buckets": buckets,
            "confident_n": len(conf),
            "confident_accuracy": round(sum((p["p"] >= .5) == bool(p["up"]) for p in conf) / len(conf), 4)
            if conf else None}


# ----------------------------------------------------------------------------- run
def new_state(cfg):
    return {"version": 1, "config": cfg, "cash": cfg["start_cash"], "position": None, "trades": [],
            "equity": [], "predictions": [], "pending": None, "model": None, "report": None,
            "bars_since_train": 0, "last_bar_t": None, "runs": 0, "started": int(time.time())}


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(path, state):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, path)


def run_once(cfg, path, now=None, bars_fn=fetch_bars, news_fn=fetch_news):
    now = now or time.time()
    state = load_state(path)
    if state and any(state.get("config", {}).get(k) != cfg[k] for k in ("symbol", "timeframe")):
        state = None  # symbol or timeframe changed -> start fresh
    fresh = state is None
    if fresh:
        state = new_state(cfg)
    state["config"] = cfg
    state["runs"] = state.get("runs", 0) + 1
    state["updated"] = int(now)

    try:
        state["news"] = news_fn()
    except Exception as e:
        state.setdefault("news", {})["errors"] = [f"news: {e}"]
    news_score = (state.get("news") or {}).get("score", 0.0)

    try:
        bars, meta = bars_fn(cfg["symbol"], cfg["timeframe"])
    except Exception as e:
        state["status"] = f"Price data error: {e}"
        save_state(path, state)
        return state

    tf = TF_SECONDS[cfg["timeframe"]]
    bars = [b for b in bars if b["t"] + tf <= now]  # closed bars only
    if len(bars) < MIN_BARS:
        state["status"] = f"Waiting for data ({len(bars)}/{MIN_BARS} bars)"
        save_state(path, state)
        return state
    ind = indicators(bars)
    book = Book(state, cfg)
    last = len(bars) - 1

    def retrain(end):
        model, report = train(bars, ind, end, cfg)
        if model:
            state["model"], state["report"] = model, report
        state["bars_since_train"] = 0

    def decide(i):
        x = features(bars, ind, i)
        if x is None or not state.get("model"):
            return
        p, contrib = predict(state["model"], x)
        pn = min(0.99, max(0.01, p + cfg["news_tilt"] * news_score))
        state["pending"] = {"t": bars[i]["t"], "c": bars[i]["c"], "p": round(p, 4), "pn": round(pn, 4)}
        edge_ok = (not cfg["require_edge"]) or (state.get("report") or {}).get("has_edge", False)
        if edge_ok:
            book.maybe_enter(bars[i], p, ind["atr"][i])
        if p >= cfg["enter_long"]:
            signal = "LONG" if edge_ok else "LONG bias (not traded: no tested edge)"
        elif p <= cfg["enter_short"]:
            signal = "SHORT" if edge_ok else "SHORT bias (not traded: no tested edge)"
        else:
            signal = "FLAT (too close to 50/50)"
        reasons = sorted(({"name": label, "effect": round(c, 3)} for (_, label), c in zip(FEATURES, contrib)),
                         key=lambda r: -abs(r["effect"]))
        state["latest"] = {"t": bars[i]["t"], "close": bars[i]["c"], "p_up": round(p, 4), "p_news": round(pn, 4),
                           "signal": signal, "edge_ok": edge_ok, "atr": round(ind["atr"][i], 2),
                           "reasons": reasons[:4]}

    if fresh or not state.get("model") or state.get("last_bar_t") is None:
        retrain(last)
        new_idx = [last]
        state["last_bar_t"] = bars[last]["t"] - 1
        replay_from_scratch = True
    else:
        new_idx = [i for i, b in enumerate(bars) if b["t"] > state["last_bar_t"]][-MAX_REPLAY:]
        replay_from_scratch = False

    for i in new_idx:
        bar = bars[i]
        if not replay_from_scratch:
            pend = state.get("pending")
            if pend and i > 0 and pend["t"] == bars[i - 1]["t"] and bar["c"] != bars[i - 1]["c"]:
                state["predictions"].append({"t": pend["t"], "p": pend["p"], "pn": pend["pn"],
                                             "up": 1 if bar["c"] > bars[i - 1]["c"] else 0})
            state["pending"] = None
            trade = book.on_bar(bar)
            if trade:
                state["trades"].append(trade)
            state["bars_since_train"] = state.get("bars_since_train", 0) + 1
            if state["bars_since_train"] >= cfg["retrain_bars"]:
                retrain(i)
        decide(i)
        state["equity"].append([bar["t"], book.equity(bar["c"])])
        state["last_bar_t"] = bar["t"]

    state["trades"] = state["trades"][-300:]
    state["equity"] = state["equity"][-1000:]
    state["predictions"] = state["predictions"][-2000:]
    state["stats"] = {"account": trade_stats(state["trades"]), "predictions": pred_stats(state["predictions"])}
    price = meta.get("regularMarketPrice") or bars[last]["c"]
    age = now - (bars[last]["t"] + tf)
    state["market"] = {"price": price, "last_bar_t": bars[last]["t"], "data_age_sec": int(age),
                       "exchange_tz": meta.get("exchangeTimezoneName")}
    state["equity_now"] = book.equity(price)
    if not new_idx:
        state["status"] = "No new bars - market closed or data feed paused"
    elif age > 3 * tf + 900:
        state["status"] = "Data is old - market may be closed"
    else:
        state["status"] = f"OK - processed {len(new_idx)} new bar(s)"
    save_state(path, state)
    return state


def main():
    cfg = config()
    path = os.environ.get("STATE_PATH", os.path.join(ROOT, "state", "bot.json"))
    s = run_once(cfg, path)
    lt = s.get("latest") or {}
    print(f"[{cfg['symbol']} {cfg['timeframe']}] {s.get('status')}")
    if lt:
        print(f"  P(up next bar) = {lt['p_up']:.1%}  news-adjusted = {lt['p_news']:.1%}  -> {lt['signal']}")
    print(f"  equity = ${s.get('equity_now', s.get('cash'))}")


if __name__ == "__main__":
    sys.exit(main())
