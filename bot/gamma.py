"""Dealer gamma exposure (GEX) levels for the Nasdaq, from free CBOE delayed option chains. Standard library only.

From the QQQ and NDX option chains (open interest + implied volatility) it works out:
  * net GEX     - dollars of stock dealers must trade per 1% move. Positive: they sell rallies and
                  buy dips (calmer, mean-reverting market). Negative: they chase moves (faster, trending).
  * gamma flip  - the price where net GEX changes sign (also called zero gamma)
  * call wall   - the level with the most call gamma, usually resistance
  * put wall    - the level with the most put gamma, usually support
  * key levels  - the other biggest net-gamma levels
and maps everything onto the chart you trade (MNQ / NQ futures) with the current price ratio, so the
levels can be pasted straight into the TradingView indicator (tradingview/gamma_orderflow_signal.pine).

Model assumption, the usual one for public GEX numbers: customers buy puts and sell calls, so dealers
are long the calls and short the puts. Real dealer books differ; treat levels as zones, not lines.
Open interest updates once a day (overnight), and CBOE data is delayed about 15 minutes.

    python bot/gamma.py                 # print levels mapped to MNQ futures
    python bot/gamma.py 24950           # map to this futures price instead
"""
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request

URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"
UA = {"User-Agent": "Mozilla/5.0 (quant.bot gamma levels)"}
SOURCES = ("QQQ", "_NDX")          # both hedge into the Nasdaq-100
OPT_RE = re.compile(r"^([A-Z]+?)(\d{6})([CP])(\d{8})$")
YEAR = 365.0 * 86400
MIN_T = 1.0 / (365 * 24)           # 1 hour: keeps 0DTE gamma finite near the close
CONTRACT = 100                     # shares per contract


# ----------------------------------------------------------------------------- data
def fetch_chain(symbol, timeout=30):
    req = urllib.request.Request(URL.format(urllib.parse.quote(symbol)), headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _us_dst(t):
    """True if US daylight saving time is in effect at UTC timestamp t (2nd Sun of Mar - 1st Sun of Nov)."""
    y = time.gmtime(t).tm_year

    def nth_sunday(month, n):
        first = time.gmtime(_utc(y, month, 1)).tm_wday  # Monday = 0
        return 1 + (6 - first) % 7 + 7 * (n - 1)
    start = _utc(y, 3, nth_sunday(3, 2), 7)    # 2am EST = 07:00 UTC
    end = _utc(y, 11, nth_sunday(11, 1), 6)    # 2am EDT = 06:00 UTC
    return start <= t < end


def _utc(y, m, d, h=0, mi=0):
    import calendar
    return calendar.timegm((y, m, d, h, mi, 0, 0, 0, 0))


def et_offset_hours(t):
    return -4 if _us_dst(t) else -5


def _expiry_ts(yymmdd, root):
    y, m, d = 2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:])
    noon = _utc(y, m, d, 12)
    off = et_offset_hours(noon)
    hh, mm = (9, 30) if root == "NDX" else (16, 0)   # monthly NDX settles at the open (AM), the rest at 4pm
    return _utc(y, m, d, hh, mm) - off * 3600


def parse_chain(js, now, max_days=60):
    """-> (spot, [(strike, is_call, open_interest, iv, years_to_expiry), ...])"""
    data = js.get("data") or {}
    spot = data.get("current_price") or data.get("close")
    if not spot:
        raise ValueError("no underlying price in CBOE data")
    out = []
    for o in data.get("options") or []:
        m = OPT_RE.match(o.get("option") or "")
        if not m:
            continue
        oi, iv = o.get("open_interest") or 0, o.get("iv") or 0
        if oi <= 0 or not (0.01 < iv < 5):
            continue
        root, ymd, cp, k = m.groups()
        T = (_expiry_ts(ymd, root) - now) / YEAR
        if T <= 0 or T > max_days / 365.0:
            continue
        strike = int(k) / 1000.0
        if not (0.7 * spot < strike < 1.3 * spot):
            continue
        out.append((strike, cp == "C", float(oi), float(iv), max(T, MIN_T)))
    return float(spot), out


# ----------------------------------------------------------------------------- math
def bs_gamma(S, K, T, iv, r=0.04):
    sd = iv * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / sd
    return math.exp(-0.5 * d1 * d1) / (math.sqrt(2 * math.pi) * S * sd)


def dollar_gamma(opts, S, r=0.04):
    """Net, call and put $ gamma per 1% move at price S (calls +, puts -)."""
    calls = puts = 0.0
    for K, is_call, oi, iv, T in opts:
        g = bs_gamma(S, K, T, iv, r) * oi * CONTRACT * S * S * 0.01
        if is_call:
            calls += g
        else:
            puts += g
    return calls - puts, calls, puts


def nice_bucket(price):
    """A round level spacing of about 0.1% of price (25 points on NQ, 0.5 on QQQ)."""
    step = price * 0.001
    return min((0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 25, 50, 100), key=lambda b: abs(math.log(b / step)))


def zone_peak(levels, idx, width):
    """Level with the most gamma (idx 0 = calls, 1 = puts) counting neighbours within `width`, returned as the
    gamma-weighted centre of that zone. Merges QQQ and NDX strikes that land a few points apart."""
    keys = sorted(levels)
    best, best_sum = None, -1.0
    for k in keys:
        tot = sum(levels[j][idx] for j in keys if abs(j - k) <= width)
        if tot > best_sum:
            best, best_sum = k, tot
    zone = [j for j in keys if abs(j - best) <= width]
    w = sum(levels[j][idx] for j in zone)
    return sum(j * levels[j][idx] for j in zone) / w if w else best


def find_flip(curve, x0=0.0):
    """Zero crossing of [(x, gex)] closest to x0, linearly interpolated; None if GEX never changes sign."""
    best = None
    for (xa, ga), (xb, gb) in zip(curve, curve[1:]):
        if ga == 0:
            x = xa
        elif ga * gb < 0:
            x = xa + (xb - xa) * ga / (ga - gb)
        else:
            continue
        if best is None or abs(x - x0) < abs(best - x0):
            best = x
    return best


def compute(target_price=None, sources=SOURCES, now=None, fetch=fetch_chain, r=0.04, max_days=60,
            span=0.08, steps=64, n_levels=5):
    """Gamma levels for all sources combined, mapped onto `target_price` (default: the first source's price)."""
    now = now or time.time()
    chains, errors = {}, []
    for sym in sources:
        try:
            chains[sym] = parse_chain(fetch(sym), now, max_days)
        except Exception as e:
            errors.append(f"{sym}: {e.__class__.__name__}: {e}"[:160])
    if not chains:
        raise RuntimeError("no option data: " + "; ".join(errors))
    first_spot = next(iter(chains.values()))[0]
    F = float(target_price or first_spot)

    # Net GEX if the whole market moved by x (all sources move together): find where it flips sign.
    xs = [-span + 2 * span * i / steps for i in range(steps + 1)]
    curve = []
    for x in xs:
        curve.append((x, sum(dollar_gamma(o, S * (1 + x), r)[0] for S, o in chains.values())))
    fx = find_flip(curve)

    # Per-level call and put gamma at today's price, with strikes mapped onto the target chart.
    bucket = nice_bucket(F)
    levels, srcinfo = {}, {}
    net_total = 0.0
    for sym, (S, opts) in chains.items():
        ratio = F / S
        calls = puts = 0.0
        for K, is_call, oi, iv, T in opts:
            g = bs_gamma(S, K, T, iv, r) * oi * CONTRACT * S * S * 0.01
            lv = round(K * ratio / bucket) * bucket
            c, p = levels.get(lv, (0.0, 0.0))
            levels[lv] = (c + g, p) if is_call else (c, p + g)
            if is_call:
                calls += g
            else:
                puts += g
        net = calls - puts
        net_total += net
        srcinfo[sym.lstrip("_")] = {"spot": round(S, 2), "ratio": round(ratio, 5), "options": len(opts),
                                    "net_gex_bn": round(net / 1e9, 3)}
    if not levels:
        raise RuntimeError("option chains had no usable contracts")

    tick = 0.25 if F > 1000 else 0.01
    rnd = lambda v: round(round(v / tick) * tick, 2)
    width = 1.5 * bucket
    call_wall = zone_peak(levels, 0, width)
    put_wall = zone_peak(levels, 1, width)
    near = {k: v for k, v in levels.items() if abs(k / F - 1) <= span}
    ranked = sorted(near, key=lambda k: -abs(near[k][0] - near[k][1]))
    keys = []
    for k in ranked:   # biggest net-gamma levels, at least one zone apart from the walls and each other
        if all(abs(k - j) > width for j in keys + [call_wall, put_wall]):
            keys.append(k)
        if len(keys) >= n_levels:
            break
    profile = sorted((k, round(v[0] / 1e9, 4), round(-v[1] / 1e9, 4)) for k, v in near.items()
                     if abs(k / F - 1) <= 0.04 and (v[0] + v[1]) > 0)
    flip = rnd(F * (1 + fx)) if fx is not None else None
    return {
        "t": int(now),
        "price": rnd(F),
        "net_gex_bn": round(net_total / 1e9, 3),
        "regime": "positive" if net_total > 0 else "negative",
        "flip": flip,
        "flip_note": None if flip else f"GEX does not change sign within ±{span:.0%}",
        "call_wall": rnd(call_wall),
        "put_wall": rnd(put_wall),
        "levels": [rnd(k) for k in sorted(keys)],
        "profile": [[rnd(k), c, p] for k, c, p in profile],
        "sources": srcinfo,
        "errors": errors,
    }


def tv_levels(g):
    """The `G=` part of the TradingView feed: flip, call wall, put wall, key levels (0 = unknown)."""
    if not g or g.get("error") and not g.get("call_wall"):
        return ""
    nums = [g.get("flip") or 0, g.get("call_wall") or 0, g.get("put_wall") or 0] + list(g.get("levels") or [])
    return "G=" + ",".join(f"{x:g}" for x in nums)


if __name__ == "__main__":
    price = float(sys.argv[1]) if len(sys.argv) > 1 else None
    if price is None:  # map onto the live MNQ price if Yahoo answers
        try:
            req = urllib.request.Request("https://query1.finance.yahoo.com/v8/finance/chart/MNQ%3DF?interval=1d&range=1d", headers=UA)
            with urllib.request.urlopen(req, timeout=10) as r:
                price = json.loads(r.read())["chart"]["result"][0]["meta"]["regularMarketPrice"]
        except Exception:
            pass
    g = compute(price)
    print(f"mapped to {g['price']}  net GEX ${g['net_gex_bn']:+.2f}bn per 1%  ({g['regime']} gamma)")
    print(f"gamma flip {g['flip'] or g['flip_note']}   call wall {g['call_wall']}   put wall {g['put_wall']}")
    print("key levels", g["levels"])
    for s, i in g["sources"].items():
        print(f"  {s}: spot {i['spot']}  ratio {i['ratio']}  {i['options']} options  net ${i['net_gex_bn']:+.2f}bn")
    if g["errors"]:
        print("errors:", g["errors"])
    print("\nTradingView feed:", tv_levels(g))
