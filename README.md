# quant.bot

## Cancel probability (`bot/cancel_probability.py`)

Estimates the probability that the **next limit-order-book event is a cancellation**
(and on which side: bid or ask), using several quant models side by side:

| Model | Idea |
|---|---|
| `base_rate` | Static class frequencies: the benchmark to beat |
| `ewma` | Exponentially weighted frequencies that adapt to the current regime |
| `markov` | Order-2 Markov chain on event types with Dirichlet backoff |
| `hawkes` | Multivariate self-exciting Hawkes process (MLE via EM). Exact next-event probabilities plus P(cancel within *h* seconds) |
| `logistic` | Softmax regression on queue sizes, imbalance, spread, time since last event and recent event mix |
| `ensemble` | Linear pool of all models, weights fitted on a validation slice |

Models are trained on the first 70% of events and scored on the unseen 30%
(log loss, Brier, AUC, accuracy). Pure Python, no installs needed.

```
python bot/cancel_probability.py                    # demo on a synthetic order book
python bot/cancel_probability.py --csv events.csv   # your own data
python bot/cancel_probability.py --save-sample sample.csv
python bot/cancel_probability.py --json
python -m unittest discover tests
```

CSV columns: `timestamp, event (limit|cancel|market), side (bid|ask, or buy|sell for market),
bid_size, ask_size, spread` (book columns are optional and describe the book *after* the event).

Research tool only, not financial advice.

## TradingView: Next Candle Probability (`tradingview/next_candle_probability.pine`)

Pine Script v6 indicator that estimates **P(next candle closes up)** with five models:
base rate, EWMA, order-2 Markov chain on candle colors, online logistic regression
(RSI, return z-score, close location, EMA distance in ATRs, volume z-score) and a
kernel nearest-neighbour pattern match, combined by a Hedge (exponential-weights) ensemble.
A table scores each model walk-forward over the last N bars (accuracy, Brier score, weight).

Install: TradingView → **Pine Editor** → paste the file → **Add to chart**.
Alerts: "Next candle likely UP / DOWN" (fire on closed bars only).

## TradingView: Multi-Timeframe Candle Probability (`tradingview/mtf_candle_probability.pine`)

Naive Bayes model run separately on 1 min, 5 min and 1 day candles (via `request.security`).
Shows P(current candle closes up) per timeframe, a walk-forward accuracy score and the edge
over always guessing the more common color. Background turns green/red when all three agree.
Use it on a 1-minute chart. Prediction is fixed at the previous candle's close (no repainting).

## TradingView: Quant Day Trader strategy (`tradingview/quant_day_trader.pine`)

Intraday strategy (1m/5m charts) that scores every closed candle from -4 to +4 using trend
(EMA 20/50 + VWAP), volatility-adjusted momentum z-score, opening-range breakout and a Naive
Bayes probability. Filters: relative volume, VWAP z-score, session window. Risk: ATR stop and
target, max trades per day, daily loss limit, flat before the close. Sends JSON `alert()`
messages (buy / sell / close) for webhook automation. Backtest it in the Strategy Tester first.

## TradingView: Quant Buy/Sell Signals (`tradingview/quant_buy_sell_signals.pine`)

Indicator that prints live BUY / SELL / exit labels on the chart with stop and target lines,
BUY / SELL / EXIT alerts and a running win rate of past signals. Same -4..+4 quant score as the
day trader (trend, momentum z-score, N-candle breakout, Naive Bayes probability), but works on
any market and timeframe. Signals are fixed on candle close.
