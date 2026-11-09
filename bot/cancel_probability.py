#!/usr/bin/env python3
"""Cancel-probability engine: P(next order-book event is a CANCEL).

Models the limit order book as a stream of 6 event classes

    limit_bid, limit_ask, cancel_bid, cancel_ask, market_buy, market_sell

and estimates, after every event, the probability distribution of the NEXT event.
P(next cancel) = P(cancel_bid) + P(cancel_ask).

Algorithms (each one is a separate model so you can compare them):

  base_rate  Static class frequencies from the training data (the benchmark to beat).
  ewma       Exponentially weighted frequencies - adapts to the current regime.
  markov     Order-2 Markov chain on event types with hierarchical (Dirichlet) backoff.
  hawkes     Multivariate self-exciting Hawkes process, exponential kernel, fitted by
             maximum likelihood (EM). Gives the exact probability that the next event is
             each class (competing intensities), plus P(at least one cancel within h sec).
  logistic   Multinomial logistic regression on order-book features: queue sizes,
             imbalance, spread, time since last event, last event, recent event mix.
  ensemble   Linear pool of all models, weights fitted by EM on a validation slice.

Evaluation is walk-forward: fit on the first 70% of events, score on the unseen 30%
(log loss, Brier score, AUC, accuracy for the binary "next is cancel" question).

Standard library only.

    python bot/cancel_probability.py                    # synthetic order book demo
    python bot/cancel_probability.py --csv events.csv   # your own data
    python bot/cancel_probability.py --save-sample sample.csv
    python bot/cancel_probability.py --json             # machine-readable output

CSV columns (header required):
    timestamp  seconds as a number, or ISO-8601 time
    event      limit | cancel | market
    side       bid | ask        (for market orders: buy | sell, or the book side hit)
    bid_size   size resting at best bid AFTER the event   (optional, default 0)
    ask_size   size resting at best ask AFTER the event   (optional, default 0)
    spread     spread in ticks AFTER the event            (optional, default 1)

Research / paper-trading tool, not financial advice.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime

CLASSES = ("limit_bid", "limit_ask", "cancel_bid", "cancel_ask", "market_buy", "market_sell")
K = len(CLASSES)
LIMIT_BID, LIMIT_ASK, CANCEL_BID, CANCEL_ASK, MARKET_BUY, MARKET_SELL = range(K)
CANCELS = (CANCEL_BID, CANCEL_ASK)
EPS = 1e-12


@dataclass
class Event:
    t: float          # seconds
    kind: int         # index into CLASSES
    bid_size: float = 0.0
    ask_size: float = 0.0
    spread: float = 1.0


def p_cancel(dist):
    return dist[CANCEL_BID] + dist[CANCEL_ASK]


def normalize(v):
    s = sum(v)
    if s <= 0:
        return [1.0 / len(v)] * len(v)
    return [max(x, 0.0) / s for x in v]


# --------------------------------------------------------------------------- data

def classify(event: str, side: str) -> int:
    event, side = event.strip().lower(), side.strip().lower()
    if event in ("limit", "add", "new", "insert"):
        return LIMIT_BID if side in ("bid", "buy", "b") else LIMIT_ASK
    if event in ("cancel", "delete", "cxl", "cancellation"):
        return CANCEL_BID if side in ("bid", "buy", "b") else CANCEL_ASK
    if event in ("market", "trade", "execution", "exec", "fill"):
        # a buy market order lifts the ask, a sell hits the bid
        if side in ("buy", "ask", "a"):
            return MARKET_BUY
        if side in ("sell", "bid", "b"):
            return MARKET_SELL
    raise ValueError(f"unknown event/side: {event!r}/{side!r}")


def parse_time(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00")).timestamp()


def load_csv(path: str) -> list[Event]:
    events = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            events.append(Event(
                t=parse_time(row["timestamp"]),
                kind=classify(row["event"], row["side"]),
                bid_size=float(row.get("bid_size") or 0),
                ask_size=float(row.get("ask_size") or 0),
                spread=float(row.get("spread") or 1),
            ))
    return clean(events)


def clean(events: list[Event]) -> list[Event]:
    """Sort by time and break exact timestamp ties (Hawkes needs strictly increasing times)."""
    events = sorted(events, key=lambda e: e.t)
    for i in range(1, len(events)):
        if events[i].t <= events[i - 1].t:
            events[i].t = events[i - 1].t + 1e-6
    return events


def save_csv(events: list[Event], path: str):
    names = {LIMIT_BID: ("limit", "bid"), LIMIT_ASK: ("limit", "ask"),
             CANCEL_BID: ("cancel", "bid"), CANCEL_ASK: ("cancel", "ask"),
             MARKET_BUY: ("market", "buy"), MARKET_SELL: ("market", "sell")}
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "event", "side", "bid_size", "ask_size", "spread"])
        for e in events:
            ev, side = names[e.kind]
            w.writerow([f"{e.t:.6f}", ev, side, int(e.bid_size), int(e.ask_size), int(e.spread)])


def simulate(n_events: int = 20000, seed: int = 7) -> list[Event]:
    """Synthetic order book: a 6-d Hawkes process (Ogata thinning) whose cancel
    intensity also grows with queue size, plus a simple best-bid/ask queue model."""
    rng = random.Random(seed)
    beta = 2.0
    mu = [0.9, 0.9, 0.6, 0.6, 0.25, 0.25]
    a = [[0.0] * K for _ in range(K)]          # a[i][j]: effect of event j on intensity of i
    for i in range(K):
        a[i][i] = 0.3
    a[CANCEL_BID][LIMIT_BID] = a[CANCEL_ASK][LIMIT_ASK] = 0.6     # quote flickering
    a[LIMIT_BID][CANCEL_BID] = a[LIMIT_ASK][CANCEL_ASK] = 0.4     # re-quoting
    a[CANCEL_ASK][MARKET_BUY] = a[CANCEL_BID][MARKET_SELL] = 0.5  # makers pull after a hit
    a[MARKET_BUY][MARKET_BUY] = a[MARKET_SELL][MARKET_SELL] = 0.4  # order splitting

    R = [0.0] * K
    t = 0.0
    bid, ask, spread = 10, 10, 1
    out = []

    def mult(i):
        if i == CANCEL_BID:
            return min(2.0, max(0.5, bid / 10))
        if i == CANCEL_ASK:
            return min(2.0, max(0.5, ask / 10))
        if i in (LIMIT_BID, LIMIT_ASK) and spread > 1:
            return 1.6
        return 1.0

    while len(out) < n_events:
        lam_bar = 2.0 * sum(mu[i] + sum(a[i][j] * R[j] for j in range(K)) for i in range(K))
        w = rng.expovariate(lam_bar)
        t += w
        d = math.exp(-beta * w)
        R = [r * d for r in R]
        lam = [(mu[i] + sum(a[i][j] * R[j] for j in range(K))) * mult(i) for i in range(K)]
        total = sum(lam)
        if rng.random() * lam_bar > total:
            continue  # rejected (thinning)
        u, k = rng.random() * total, 0
        while k < K - 1 and u > lam[k]:
            u -= lam[k]
            k += 1
        R[k] += 1.0
        size = rng.randint(1, 5)
        if k == LIMIT_BID:
            if spread > 1 and rng.random() < 0.3:
                spread, bid = spread - 1, size
            else:
                bid += size
        elif k == LIMIT_ASK:
            if spread > 1 and rng.random() < 0.3:
                spread, ask = spread - 1, size
            else:
                ask += size
        elif k in (CANCEL_BID, MARKET_SELL):
            bid -= size
            if bid <= 0:
                spread, bid = min(spread + 1, 5), rng.randint(3, 15)
        else:  # CANCEL_ASK, MARKET_BUY
            ask -= size
            if ask <= 0:
                spread, ask = min(spread + 1, 5), rng.randint(3, 15)
        out.append(Event(t, k, bid, ask, spread))
    return out


# --------------------------------------------------------------------------- models
# Every model: fit(events), reset(), update(event), predict(now=None) -> [K probabilities]
# predict() is the distribution of the NEXT event given everything update()d so far.

class BaseRate:
    name = "base_rate"

    def fit(self, events):
        c = [1.0] * K
        for e in events:
            c[e.kind] += 1
        self.p = normalize(c)
        return self

    def reset(self):
        pass

    def update(self, e):
        pass

    def predict(self, now=None):
        return list(self.p)


class EWMA:
    name = "ewma"

    def __init__(self, halflife: float = 100):
        self.decay = 0.5 ** (1.0 / halflife)

    def fit(self, events):
        self.prior = BaseRate().fit(events).p
        self.reset()
        return self

    def reset(self):
        self.p = list(self.prior)

    def update(self, e):
        d = self.decay
        self.p = [x * d for x in self.p]
        self.p[e.kind] += 1 - d

    def predict(self, now=None):
        return normalize(self.p)


class Markov:
    """Order-n Markov chain with hierarchical Dirichlet backoff:
    P_n(c|ctx) = (count(ctx,c) + kappa * P_{n-1}(c|shorter ctx)) / (count(ctx) + kappa)."""
    name = "markov"

    def __init__(self, order: int = 2, kappa: float = 5.0):
        self.order, self.kappa = order, kappa

    def fit(self, events):
        self.counts = [dict() for _ in range(self.order + 1)]
        hist = deque(maxlen=self.order)
        for e in events:
            for n in range(self.order + 1):
                if len(hist) >= n:
                    ctx = tuple(hist)[len(hist) - n:]
                    self.counts[n].setdefault(ctx, [0] * K)[e.kind] += 1
            hist.append(e.kind)
        self.reset()
        return self

    def reset(self):
        self.hist = deque(maxlen=self.order)

    def update(self, e):
        self.hist.append(e.kind)

    def predict(self, now=None):
        p = [1.0 / K] * K
        h = tuple(self.hist)
        for n in range(self.order + 1):
            if len(h) < n:
                break
            row = self.counts[n].get(h[len(h) - n:])
            if row is None:
                break
            tot = sum(row)
            p = [(row[c] + self.kappa * p[c]) / (tot + self.kappa) for c in range(K)]
        return p


class Hawkes:
    """Multivariate Hawkes process with a shared exponential kernel:

        lambda_i(t) = mu_i + sum_j alpha_ij * sum_{t_k of type j, t_k < t} exp(-beta (t - t_k))

    For a fixed beta the log-likelihood is concave in (mu, alpha); it is maximised with the
    standard EM (branching-structure) update. beta is picked from a grid by likelihood.

    Next-event probability (exact, competing intensities): with M = sum mu, A = sum a_i,
    a_i = excitation of class i now, S(s) = exp(-M s - A (1 - e^{-beta s}) / beta):
        P(next = i) = mu_i * I0 + a_i * I1,   I1 = int e^{-beta s} S ds,  I0 = (1 - A I1) / M
    """
    name = "hawkes"

    def __init__(self, betas=(0.5, 1.5, 4.0), iters: int = 40):
        self.betas, self.iters = betas, iters

    def fit(self, events):
        best = None
        for beta in self.betas:
            mu, alpha, ll = self._fit_beta(events, beta)
            if best is None or ll > best[3]:
                best = (beta, mu, alpha, ll)
        self.beta, self.mu, self.alpha, self.loglik = best
        self.reset()
        return self

    def _fit_beta(self, events, beta):
        t0, t_end = events[0].t, events[-1].t
        T = max(t_end - t0, EPS)
        R = [0.0] * K
        last = t0
        X = [[] for _ in range(K)]                  # per target class: feature rows
        G = [T] + [0.0] * K                          # integrated features (compensator)
        for e in events:
            d = math.exp(-beta * (e.t - last))
            R = [r * d for r in R]
            X[e.kind].append([1.0] + R)
            R[e.kind] += 1.0
            last = e.t
            G[1 + e.kind] += (1.0 - math.exp(-beta * (t_end - e.t))) / beta
        mu, alpha, ll_total = [0.0] * K, [[0.0] * K for _ in range(K)], 0.0
        for i in range(K):
            rows = X[i]
            n_i = len(rows)
            theta = [max(n_i, 1) / T * 0.5] + [0.1 * beta / K] * K
            for _ in range(self.iters):
                num = [0.0] * (K + 1)
                for x in rows:
                    lam = sum(th * xv for th, xv in zip(theta, x))
                    if lam <= 0:
                        continue
                    inv = 1.0 / lam
                    for m in range(K + 1):
                        if x[m]:
                            num[m] += theta[m] * x[m] * inv
                theta = [num[m] / G[m] if G[m] > 0 else 0.0 for m in range(K + 1)]
                theta[0] = max(theta[0], 1e-9)
            ll = sum(math.log(max(sum(th * xv for th, xv in zip(theta, x)), EPS)) for x in rows)
            ll -= sum(th * g for th, g in zip(theta, G))
            ll_total += ll
            mu[i], alpha[i] = theta[0], theta[1:]
        return mu, alpha, ll_total

    def reset(self):
        self.R = [0.0] * K
        self.last_t = None

    def _decay_to(self, t):
        if self.last_t is None:
            return [0.0] * K
        d = math.exp(-self.beta * max(t - self.last_t, 0.0))
        return [r * d for r in self.R]

    def update(self, e):
        self.R = self._decay_to(e.t)
        self.R[e.kind] += 1.0
        self.last_t = e.t

    def excitation(self, now=None):
        R = self.R if now is None else self._decay_to(now)
        return [sum(self.alpha[i][j] * R[j] for j in range(K)) for i in range(K)]

    def intensities(self, now=None):
        a = self.excitation(now)
        return [self.mu[i] + a[i] for i in range(K)]

    def predict(self, now=None):
        a = self.excitation(now)
        M, A, b = sum(self.mu), sum(a), self.beta
        # I1 = (1/beta) * int_0^1 u^(M/beta) * exp(-A (1-u)/beta) du   (substitute u = e^{-beta s})
        n = 200  # Simpson, even
        f = lambda u: (u ** (M / b)) * math.exp(-A * (1 - u) / b)
        h = 1.0 / n
        s = f(0.0) + f(1.0)
        for k in range(1, n):
            s += (4 if k % 2 else 2) * f(k * h)
        I1 = s * h / 3 / b
        I0 = max(1.0 - A * I1, 0.0) / M
        return normalize([self.mu[i] * I0 + a[i] * I1 for i in range(K)])

    def prob_cancel_within(self, horizon: float, now=None) -> float:
        """P(at least one cancel, either side, in the next `horizon` seconds)."""
        a = self.excitation(now)
        decay = (1 - math.exp(-self.beta * horizon)) / self.beta
        integ = sum(self.mu[i] * horizon + a[i] * decay for i in CANCELS)
        return 1.0 - math.exp(-integ)

    def branching_ratio(self) -> float:
        """Spectral radius of alpha/beta (power iteration). < 1 means stationary."""
        v = [1.0] * K
        r = 0.0
        for _ in range(100):
            w = [sum(self.alpha[i][j] * v[j] for j in range(K)) / self.beta for i in range(K)]
            r = max(abs(x) for x in w) or 0.0
            if r == 0:
                return 0.0
            v = [x / r for x in w]
        return r


class Logistic:
    """Multinomial logistic regression (softmax) on order-book state, trained by SGD."""
    name = "logistic"
    WINDOW = 20

    def __init__(self, epochs: int = 6, lr: float = 0.05, l2: float = 1e-4, seed: int = 1):
        self.epochs, self.lr, self.l2, self.seed = epochs, lr, l2, seed

    def reset(self):
        self.win = deque(maxlen=self.WINDOW)
        self.prev_t = None
        self.dt = 0.0
        self.last = None

    def update(self, e):
        self.dt = 0.0 if self.prev_t is None else e.t - self.prev_t
        self.prev_t = e.t
        self.win.append(e.kind)
        self.last = e

    def features(self):
        e = self.last
        x = [0.0] * K
        if e is not None:
            x[e.kind] = 1.0
        mix = [0.0] * K
        for k in self.win:
            mix[k] += 1.0 / self.WINDOW
        if e is None:
            book = [0.0] * 5
        else:
            b, a = max(e.bid_size, 0.0), max(e.ask_size, 0.0)
            book = [math.log1p(b), math.log1p(a), (b - a) / (b + a) if b + a > 0 else 0.0,
                    min(max(e.spread - 1, 0.0), 10.0), math.log1p(max(self.dt, 0.0))]
        return x + mix + book

    def _std(self, x):
        return [(v - m) / s for v, m, s in zip(x, self.mean, self.sd)] + [1.0]

    def _softmax(self, z):
        mx = max(z)
        ex = [math.exp(v - mx) for v in z]
        s = sum(ex)
        return [v / s for v in ex]

    def _raw_predict(self, xs):
        return self._softmax([sum(w * v for w, v in zip(self.W[c], xs)) for c in range(K)])

    def fit(self, events):
        self.reset()
        X, Y = [], []
        for i, e in enumerate(events):
            if i > 0:
                X.append(self.features())
                Y.append(e.kind)
            self.update(e)
        d = len(X[0])
        self.mean = [sum(r[j] for r in X) / len(X) for j in range(d)]
        self.sd = [math.sqrt(sum((r[j] - self.mean[j]) ** 2 for r in X) / len(X)) or 1.0
                   for j in range(d)]
        Xs = [self._std(r) for r in X]
        self.W = [[0.0] * (d + 1) for _ in range(K)]
        rng = random.Random(self.seed)
        idx = list(range(len(Xs)))
        for ep in range(self.epochs):
            rng.shuffle(idx)
            lr = self.lr / (1 + 0.5 * ep)
            for n in idx:
                x, y = Xs[n], Y[n]
                p = self._raw_predict(x)
                for c in range(K):
                    g = p[c] - (1.0 if c == y else 0.0)
                    Wc = self.W[c]
                    for j in range(d + 1):
                        Wc[j] -= lr * (g * x[j] + self.l2 * Wc[j])
        self.reset()
        return self

    def predict(self, now=None):
        return self._raw_predict(self._std(self.features()))


class Ensemble:
    """Linear opinion pool. Weights maximise validation likelihood (EM on mixture weights)."""
    name = "ensemble"

    def __init__(self, factories, val_frac: float = 0.2, iters: int = 200):
        self.factories, self.val_frac, self.iters = factories, val_frac, iters

    def fit(self, events):
        cut = int(len(events) * (1 - self.val_frac))
        fit_part = events[:cut]
        models = [f().fit(fit_part) for f in self.factories]
        preds = [stream_predictions(m, events, cut) for m in models]
        y = [e.kind for e in events[cut:]]
        M = len(models)
        w = [1.0 / M] * M
        for _ in range(self.iters):
            resp = [0.0] * M
            for n, yn in enumerate(y):
                lik = [w[m] * max(preds[m][n][yn], EPS) for m in range(M)]
                s = sum(lik)
                for m in range(M):
                    resp[m] += lik[m] / s
            w = [r / len(y) for r in resp]
        self.weights = {models[m].name: w[m] for m in range(M)}
        self.models = [f().fit(events) for f in self.factories]  # refit on all training data
        self.w = w
        return self

    def reset(self):
        for m in self.models:
            m.reset()

    def update(self, e):
        for m in self.models:
            m.update(e)

    def predict(self, now=None):
        out = [0.0] * K
        for wm, m in zip(self.w, self.models):
            for c, p in enumerate(m.predict(now)):
                out[c] += wm * p
        return normalize(out)


BASE_MODELS = (BaseRate, EWMA, Markov, Hawkes, Logistic)


# --------------------------------------------------------------------------- evaluation

def stream_predictions(model, events, start):
    """Feed all events in order; return the model's prediction for events[start:]
    (each made using only events before it)."""
    model.reset()
    preds = []
    for i, e in enumerate(events):
        if i >= start:
            preds.append(model.predict() if i > 0 else [1.0 / K] * K)
        model.update(e)
    return preds


def auc(scores, labels):
    pairs = sorted(zip(scores, labels))
    n_pos = sum(labels)
    n_neg = len(labels) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j + 1) / 2.0          # 1-based average rank for ties
        rank_sum += avg_rank * sum(l for _, l in pairs[i:j])
        i = j
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def score(preds, events):
    y = [1 if e.kind in CANCELS else 0 for e in events]
    p = [min(max(p_cancel(d), 1e-9), 1 - 1e-9) for d in preds]
    n = len(y)
    return {
        "log_loss": -sum(yi * math.log(pi) + (1 - yi) * math.log(1 - pi) for yi, pi in zip(y, p)) / n,
        "brier": sum((pi - yi) ** 2 for yi, pi in zip(y, p)) / n,
        "auc": auc(p, y),
        "accuracy": sum((pi >= 0.5) == bool(yi) for yi, pi in zip(y, p)) / n,
        "multiclass_log_loss": -sum(math.log(max(d[e.kind], EPS)) for d, e in zip(preds, events)) / n,
    }


def run(events, train_frac=0.7, horizons=(0.5, 1.0, 5.0)):
    if len(events) < 200:
        raise ValueError("need at least 200 events")
    cut = int(len(events) * train_frac)
    train, test = events[:cut], events[cut:]
    models = [cls().fit(train) for cls in BASE_MODELS]
    models.append(Ensemble(BASE_MODELS).fit(train))

    results = {}
    for m in models:
        results[m.name] = score(stream_predictions(m, events, cut), test)
        m.reset()
        for e in events:              # bring state up to the latest event for the live forecast
            m.update(e)

    live = {}
    for m in models:
        d = m.predict()
        live[m.name] = {"p_cancel": p_cancel(d), "p_cancel_bid": d[CANCEL_BID],
                        "p_cancel_ask": d[CANCEL_ASK],
                        "distribution": dict(zip(CLASSES, d))}
    hawkes = next(m for m in models if isinstance(m, Hawkes))
    ens = next(m for m in models if isinstance(m, Ensemble))
    best = min(results, key=lambda k: results[k]["log_loss"])
    return {
        "n_events": len(events), "n_train": len(train), "n_test": len(test),
        "test_cancel_rate": sum(e.kind in CANCELS for e in test) / len(test),
        "metrics": results,
        "best_model": best,
        "live": live,
        "hawkes": {
            "beta": hawkes.beta,
            "branching_ratio": hawkes.branching_ratio(),
            "mu": dict(zip(CLASSES, hawkes.mu)),
            "p_cancel_within": {str(h): hawkes.prob_cancel_within(h) for h in horizons},
            "intensities_now": dict(zip(CLASSES, hawkes.intensities())),
        },
        "ensemble_weights": ens.weights,
    }


def report(r):
    print(f"Events: {r['n_events']}  (train {r['n_train']}, unseen test {r['n_test']})")
    print(f"Cancel share of test events: {r['test_cancel_rate']:.1%}\n")
    print("Out-of-sample: predicting 'next event is a cancel'")
    print(f"{'model':<10} {'logloss':>8} {'brier':>7} {'AUC':>6} {'acc':>6} {'6-class LL':>11}")
    for name, s in r["metrics"].items():
        mark = "  <- best" if name == r["best_model"] else ""
        print(f"{name:<10} {s['log_loss']:8.4f} {s['brier']:7.4f} {s['auc']:6.3f} "
              f"{s['accuracy']:6.1%} {s['multiclass_log_loss']:11.4f}{mark}")
    print("\nLive forecast for the NEXT event")
    print(f"{'model':<10} {'P(cancel)':>10} {'bid side':>9} {'ask side':>9}")
    for name, v in r["live"].items():
        print(f"{name:<10} {v['p_cancel']:10.1%} {v['p_cancel_bid']:9.1%} {v['p_cancel_ask']:9.1%}")
    h = r["hawkes"]
    print(f"\nHawkes: beta={h['beta']}  branching ratio={h['branching_ratio']:.2f}")
    for hz, p in h["p_cancel_within"].items():
        print(f"  P(at least one cancel within {hz}s) = {p:.1%}")
    w = ", ".join(f"{k} {v:.2f}" for k, v in r["ensemble_weights"].items())
    print(f"Ensemble weights: {w}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Probability that the next order-book event is a cancel.")
    ap.add_argument("--csv", help="order-book event CSV (see module docstring)")
    ap.add_argument("--events", type=int, default=20000, help="synthetic events if no --csv")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--train-frac", type=float, default=0.7)
    ap.add_argument("--save-sample", metavar="PATH", help="write the synthetic data to CSV and exit")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = ap.parse_args(argv)

    events = load_csv(args.csv) if args.csv else simulate(args.events, args.seed)
    if args.save_sample:
        save_csv(events, args.save_sample)
        print(f"wrote {len(events)} events to {args.save_sample}")
        return 0
    r = run(events, args.train_frac)
    if args.json:
        json.dump(r, sys.stdout, indent=2)
        print()
    else:
        report(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
