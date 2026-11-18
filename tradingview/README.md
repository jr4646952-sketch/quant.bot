# Gamma + Order Flow Signal (TradingView)

`gamma_orderflow_signal.pine` gives you one BUY / SELL score from **-100 to +100** that combines order flow,
dealer gamma, a few standard quant signals and the news. It also grades itself on your chart so you can see
whether it works on that symbol and timeframe.

## Install (2 minutes)

1. In TradingView open a chart (best: **MNQ1!** or **NQ1!**, 1–15 minute bars).
2. Open the **Pine Editor** at the bottom of the screen, delete what is there, paste the whole
   `gamma_orderflow_signal.pine` file and press **Add to chart**.
3. Open the indicator's **Settings → "quant.bot feed or levels"** and paste the line from the dashboard's
   **Dealer gamma + TradingView feed** card (the **Copy feed** button). Do this each morning.
   It looks like `G=24850,25200,24500,25000;N=0.32;E=1118@0830`.
4. Optional: right-click the BUY/SELL label area → **Add alert** → *GOF BUY* / *GOF SELL*, or "Any alert()
   function call" to get the stop and target in the alert message.

If TradingView shows a compile error, copy the red message and send it back so it can be fixed.

## What you see

- **Price chart:** VWAP with ±1σ/±2σ bands, the developing POC, gamma flip (yellow), call wall (green),
  put wall (red), extra gamma levels (grey), BUY/SELL labels (hover for the reasons, stop and target),
  the stop/target of the last signal, and ⚡ on news-shock bars.
- **Lower pane:** the score as columns, the order-flow line, dashed BUY/SELL thresholds, a teal background in
  positive gamma, red in negative gamma and yellow while signals pause for a scheduled release.
- **Table:** the verdict, each ingredient, the self-test and the edge check.

## How the score is built

| Ingredient | What it measures | Why it matters |
|---|---|---|
| **Order-flow imbalance** | Volume delta from 1-minute (or 1-second) intrabars: each intrabar's volume counts as buying or selling, over the last 10 bars, compared with its own history | Who is hitting the market right now |
| **Absorption / divergence** | Very large delta that fails to move price, and new highs/lows that cumulative delta (CVD) does not confirm | Aggressive traders running into a bigger passive player, which often comes before a turn |
| **Kalman trend** | Velocity from a 2-state Kalman filter on price | A smoother, lower-lag trend than moving averages |
| **VWAP position** | Distance from session VWAP in volume-weighted σ | Where the day's average buyer and seller stand |
| **Gamma walls** | Distance to the call wall and put wall | Dealer hedging makes these support/resistance, or accelerators once broken |
| **News** | Pasted headline mood (±10 points max) and scheduled releases | Tilts the score slightly and pauses signals around CPI, jobs, FOMC… |

**Gamma decides how the pieces are weighted.** Above the gamma flip, dealers are long gamma: they sell rallies
and buy dips, so the indicator leans on mean reversion (fade VWAP stretches, respect the walls). Below the flip
they are short gamma and chase moves, so it leans on momentum (trend and order flow, walls that break run).
Without pasted levels it estimates the regime from the market itself: a **variance ratio** below 1 means moves
have been mean-reverting, and the **VIX/VIX3M** ratio above 1 signals stress.

A signal needs the smoothed score past ±35, at least 60% of the weighted evidence agreeing, no scheduled
release within −10/+15 minutes, and 6 bars since the last signal. Signals appear only on closed bars, so they don't
repaint.

## Is it accurate?

Read the two bottom rows of the table before trusting any signal:

- **Self-test** – every signal on the chart traded with its stop, target and time exit, after costs:
  number of trades, win rate, average R (1R = the stop distance) and profit factor.
- **Edge check** – the correlation (IC) between the score and the next 6 bars' move over the last 1,000 bars,
  with a t-statistic. **t ≥ 2** suggests a real edge on this chart; below that, treat the signals as noise.

Honest expectations, from testing the same logic on a month of NQ and QQQ data (5-minute, 15-minute
and 1-minute bars): each ingredient's IC was between about −0.06 and +0.06, and the sign changed with the
instrument and timeframe. NQ 5-minute bars came out slightly positive (IC +0.05, profit factor ~1.2 before
slippage), while 15-minute NQ and 5-minute QQQ lost money. That's normal for short-term signals: real edges
are small and unstable, and no indicator can be "the most accurate" everywhere. Use the edge check to find the
symbol and timeframe where it works, and treat the score as one input, not an autopilot.

Things that make it more accurate:

- A futures chart with real volume (MNQ1!/NQ1!), not an index or CFD (no volume = no order flow).
- **Intrabar** delta method (the default). With the "bar estimate" method, delta is only a rough guess
  (in testing it agreed with intrabar delta at only 0.66 correlation).
- Fresh gamma levels every morning. Open interest changes overnight, so old levels are wrong levels.
- The *Adaptive weights* option re-weights each ingredient by how well it predicted on this chart. Only turn it
  on with a long history (several thousand bars); on short samples it mostly learns noise.

## Limits

- TradingView scripts can't see the real bid/ask, so "buying" and "selling" volume is inferred from intrabar
  price moves, as in TradingView's own Volume Delta indicator. Your TradingView plan limits how far back intrabar
  data goes; older bars fall back to the bar estimate.
- Gamma levels are a model: they assume customers buy puts and sell calls, and the data is the previous day's
  open interest with ~15-minute delayed prices.
- News mood is keyword matching on headlines (see `bot/news.py`) and is usually already priced in.

This is a research tool, not financial advice. Futures are leveraged: 1 MNQ point = $2.
