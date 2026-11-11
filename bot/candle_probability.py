#!/usr/bin/env python3
"""Next-candle probability on real market data (Yahoo Finance), several timeframes.

For each timeframe it predicts P(next candle closes above the current close) with:

  base_rate    rolling share of up candles (benchmark)
  markov       order-2 Markov chain on candle colors, fading counts
  naive_bayes  Naive Bayes on RSI zone, close location, trend, last 2 colors, volume
  logistic     online logistic regression (SGD) on RSI, return z-score, close location,
               EMA distance in ATRs, volume z-score, last color
  ensemble     Hedge / exponential weights by recent log loss

Everything is walk-forward: each prediction only uses candles before the one it predicts,
and the score is measured on those out-of-sample predictions. Standard library only.

    python bot/candle_probability.py                     # MNQ=F on 1m, 5m, 1d
    python bot/candle_probability.py --symbol QQQ --json

Research tool, not financial advice.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request
from collections import deque

RANGES = {"1m": "7d", "2m": "60d", "5m": "60d", "15m": "60d", "30m": "60d", "1h": "730d", "1d": "5y"}
MODELS = ("base_rate", "markov", "naive_bayes", "logistic")
WARMUP = 150


def fetch(symbol: str, interval: str) -> list[dict]:
    """Closed OHLCV candles from Yahoo Finance, oldest first."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?interval={interval}&range={RANGES[interval]}&includePrePost=false")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.load(r)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):
            continue
        bars.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v or 0})
    step = {"m": 60, "h": 3600, "d": 86400}[interval[-1]] * int(interval[:-1])
    now = res["meta"].get("regularMarketTime", 0)
    if bars and interval[-1] != "d" and bars[-1]["t"] + step > now:
        bars.pop()  # drop the candle that is still forming
    return bars


def clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def sigmoid(z):
    return 1.0 / (1.0 + math.exp(-clamp(z, -30, 30)))


class Features:
    """Streaming indicators; update(bar) then read the attributes."""

    def __init__(self):
        self.prev_c = None
        self.avg_gain = self.avg_loss = 0.0
        self.ema50 = None
        self.atr = None
        self.rets = deque(maxlen=50)
        self.vols = deque(maxlen=50)
        self.n = 0
        self.up = self.up_prev = 0

    def update(self, b):
        c = b["c"]
        self.n += 1
        if self.prev_c is None:
            self.prev_c, self.ema50, self.atr = c, c, b["h"] - b["l"]
        ch = c - self.prev_c
        self.up_prev, self.up = self.up, int(ch > 0)
        self.avg_gain += (max(ch, 0) - self.avg_gain) / 14
        self.avg_loss += (max(-ch, 0) - self.avg_loss) / 14
        tr = max(b["h"] - b["l"], abs(b["h"] - self.prev_c), abs(b["l"] - self.prev_c))
        self.atr += (tr - self.atr) / 14
        self.ema50 += (c - self.ema50) * 2 / 51
        ret = math.log(c / self.prev_c) if self.prev_c > 0 and c > 0 else 0.0
        self.rets.append(ret)
        self.vols.append(b["v"])
        self.prev_c = c

        tot = self.avg_gain + self.avg_loss
        self.rsi = 100 * self.avg_gain / tot if tot > 0 else 50.0
        rng = b["h"] - b["l"]
        self.clv = (2 * c - b["h"] - b["l"]) / rng if rng > 0 else 0.0
        self.trend = (c - self.ema50) / self.atr if self.atr > 0 else 0.0
        self.ret_z = self._z(self.rets, ret)
        self.vol_z = self._z(self.vols, b["v"])

    @staticmethod
    def _z(window, x):
        if len(window) < 10:
            return 0.0
        m = sum(window) / len(window)
        sd = math.sqrt(sum((v - m) ** 2 for v in window) / len(window))
        return (x - m) / sd if sd > 0 else 0.0

    def buckets(self):
        return (0 if self.rsi < 40 else 2 if self.rsi > 60 else 1,
                3 + (0 if self.clv < -0.33 else 2 if self.clv > 0.33 else 1),
                6 + int(self.trend > 0),
                8 + 2 * self.up_prev + self.up,
                12 + int(self.vol_z > 0))

    def vector(self):
        return [1.0, clamp((self.rsi - 50) / 50), clamp(self.ret_z / 3), self.clv,
                clamp(self.trend / 3), clamp(self.vol_z / 3), 1.0 if self.up else -1.0]


def run(bars, base_len=200, decay=0.997, lr=0.02, l2=0.001, eta=2.0, hedge_decay=0.98):
    """Walk-forward over the candles. Returns scores and the forecast for the next candle."""
    f = Features()
    ups = deque(maxlen=base_len)
    mk_up, mk_tot = [0.0] * 4, [0.0] * 4
    nb_cls, nb_cnt, nb_sizes = [0.0, 0.0], [0.0] * 28, (3, 3, 2, 4, 2)
    w = [0.0] * 7
    loss = {m: 0.0 for m in MODELS}
    names = MODELS + ("ensemble",)
    stats = {m: {"n": 0, "hit": 0, "brier": 0.0, "ll": 0.0} for m in names}
    n_up = n_scored = 0
    preds = prev_x = prev_buckets = None
    prev_state = 0
    weights = {m: 1.0 / len(MODELS) for m in MODELS}

    for i, b in enumerate(bars):
        f.update(b)
        y = f.up
        if preds is not None:
            # score the predictions made one candle ago
            if i > WARMUP:
                n_scored += 1
                n_up += y
                for m in names:
                    p = clamp(preds[m], 0.001, 0.999)
                    s = stats[m]
                    s["n"] += 1
                    s["hit"] += int((p >= 0.5) == bool(y))
                    s["brier"] += (p - y) ** 2
                    s["ll"] -= math.log(p if y else 1 - p)
            for m in MODELS:
                p = clamp(preds[m], 0.001, 0.999)
                loss[m] = loss[m] * hedge_decay - math.log(p if y else 1 - p)
            # learn from the candle that just closed
            mk_up[:] = [v * decay for v in mk_up]
            mk_tot[:] = [v * decay for v in mk_tot]
            mk_up[prev_state] += y
            mk_tot[prev_state] += 1
            nb_cls[:] = [v * decay for v in nb_cls]
            nb_cnt[:] = [v * decay for v in nb_cnt]
            nb_cls[y] += 1
            for k in prev_buckets:
                nb_cnt[y * 14 + k] += 1
            g = y - sigmoid(sum(a * x for a, x in zip(w, prev_x)))
            w[:] = [a + lr * (g * x - l2 * a) for a, x in zip(w, prev_x)]
        ups.append(y)

        # predict the next candle
        state = 2 * f.up_prev + f.up
        buckets, x = f.buckets(), f.vector()
        lp = [math.log((nb_cls[c] + 1) / (sum(nb_cls) + 2)) for c in (0, 1)]
        for k, size in zip(buckets, nb_sizes):
            for c in (0, 1):
                lp[c] += math.log((nb_cnt[c * 14 + k] + 1) / (nb_cls[c] + size))
        preds = {
            "base_rate": sum(ups) / len(ups),
            "markov": (mk_up[state] + 1) / (mk_tot[state] + 2),
            "naive_bayes": sigmoid(lp[1] - lp[0]),
            "logistic": sigmoid(sum(a * v for a, v in zip(w, x))),
        }
        lo = min(loss.values())
        raw = {m: math.exp(-eta * (loss[m] - lo)) for m in MODELS}
        tot = sum(raw.values())
        weights = {m: raw[m] / tot for m in MODELS}
        preds["ensemble"] = sum(weights[m] * preds[m] for m in MODELS)
        prev_state, prev_buckets, prev_x = state, buckets, x

    majority = max(n_up, n_scored - n_up) / n_scored if n_scored else float("nan")
    scores = {}
    for m in names:
        s = stats[m]
        n = max(s["n"], 1)
        acc = s["hit"] / n
        # z-score of accuracy vs. a coin flip: above ~2 is unlikely to be luck
        scores[m] = {"accuracy": acc, "brier": s["brier"] / n, "log_loss": s["ll"] / n,
                     "edge_vs_majority": acc - majority,
                     "z_vs_coin": (acc - 0.5) / math.sqrt(0.25 / n)}
    return {"candles": len(bars), "scored": n_scored, "up_share": n_up / n_scored if n_scored else None,
            "majority_accuracy": majority, "scores": scores,
            "next": preds, "weights": weights, "last_close": bars[-1]["c"], "last_time": bars[-1]["t"]}


def report(symbol, tf, r):
    print(f"\n{symbol}  {tf}   {r['candles']} candles, {r['scored']} scored out-of-sample, "
          f"last close {r['last_close']:.2f}")
    print(f"  always guessing the more common color: {r['majority_accuracy']:.1%}")
    print(f"  {'model':<12} {'P(up next)':>10} {'accuracy':>9} {'edge':>7} {'z':>6} {'brier':>7} {'weight':>7}")
    for m, s in r["scores"].items():
        wt = f"{r['weights'][m]:.2f}" if m in r["weights"] else "-"
        print(f"  {m:<12} {r['next'][m]:10.1%} {s['accuracy']:9.1%} {s['edge_vs_majority']:+7.1%} "
              f"{s['z_vs_coin']:6.1f} {s['brier']:7.4f} {wt:>7}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Next-candle probability on real data, several timeframes.")
    ap.add_argument("--symbol", default="MNQ=F")
    ap.add_argument("--timeframes", default="1m,5m,1d")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    out = {}
    for tf in args.timeframes.split(","):
        if tf not in RANGES:
            ap.error(f"unknown timeframe {tf}; choose from {', '.join(RANGES)}")
        bars = fetch(args.symbol, tf)
        if len(bars) < WARMUP + 50:
            print(f"{args.symbol} {tf}: only {len(bars)} candles, skipping", file=sys.stderr)
            continue
        out[tf] = run(bars)
        if not args.json:
            report(args.symbol, tf, out[tf])
    if args.json:
        json.dump({"symbol": args.symbol, "timeframes": out}, sys.stdout, indent=2)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
