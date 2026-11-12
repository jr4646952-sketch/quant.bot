# quant.bot

A paper-trading quant bot for the **Micro E-mini Nasdaq (MNQ)** that you can watch from your phone.

Every bar it:

1. Turns recent price action into 7 math features: momentum, distance from the 20-bar average,
   RSI, volatility regime and position in the 20-bar range.
2. Uses a logistic-regression model to give the **probability the next bar closes higher**.
3. Scores NASDAQ headlines (Yahoo Finance, CNBC, MarketWatch, Nasdaq.com) bullish/bearish and shows a
   separate news-adjusted probability. Both are graded live, so you can see whether news actually helps.
4. Tests itself honestly. It trains on the oldest 70% of the data and grades on the newest 30% it never
   saw, including a paper-trading test **after fees and slippage**. It only trades if that test beat a
   no-skill guess and made money.
5. Paper trades 1 contract with a stop loss, take profit and time limit. It never connects to a broker.

| File | What it is |
|---|---|
| `bot/cloud_bot.py` | The bot (Python standard library only) |
| `.github/workflows/paper-bot.yml` | Runs the bot on GitHub every ~5 min, free |
| `live-quant-tracker.html` | Phone-friendly bot dashboard |
| `news-app.html` | **News Radar:** live headlines scored bullish / bearish for MNQ, with impact level |
| `bot/news.py` | The headline scorer (topics, impact, direction) used by the bot, the server and the page |
| `server.py` | Runs the dashboard **and** a 1-minute bot locally (PC or Android phone) |

## News Radar

`news-app.html` pulls headlines every minute from Yahoo Finance, CNBC, MarketWatch, Nasdaq.com and Google
News (Reuters, Bloomberg, ...) and marks each one:

- **BULL / BEAR** from the words in the headline. Movement words flip with the subject (*stocks rise* is
  bullish, *yields rise* or *inflation jumps* is bearish); fixed phrases like *rate cut*, *tariffs*,
  *ceasefire*, *misses estimates* always mean the same thing. Questions are marked *unsure*.
- **Impact `!!!` / `!!` / `!`** from the topic: Fed, CPI, jobs, tariffs, Nvidia and big tech usually move
  the Nasdaq a lot; oil and crypto usually don't.
- A **news mood** gauge (impact-weighted, last 6 hours) and filters: high impact, bullish, bearish,
  Fed / data, tech / chips, tariffs / geopolitics.

It is keyword matching, not understanding, and news is usually priced in within seconds. Use it to know
*what* is moving the market, then open the story. Open it on your phone the same way as the tracker
(`.../quant.bot/news-app.html`), or at `http://localhost:8765/news-app.html` with `server.py`.

## Three ways to run it

- **Cloud + phone (easiest):** see [SETUP-CLOUD-BOT.md](SETUP-CLOUD-BOT.md). The bot runs on GitHub on 5-minute
  bars and you open the dashboard on your phone through GitHub Pages.
- **On an Android phone, 1-minute bars:** install **Termux** from F-Droid, then:
  ```
  pkg install python git
  git clone https://github.com/jr4646952-sketch/quant.bot
  cd quant.bot
  python server.py --no-browser
  ```
  Open `http://localhost:8765/live-quant-tracker.html` in Chrome. Keep Termux open (use
  *Acquire wakelock* in its notification) or Android will pause it. iPhone can't keep it running in the
  background, so use the cloud option there.
- **On a PC:** double-click `start.bat` (Windows) or run `python server.py`.

## Be realistic

- **Free data is late.** Yahoo futures prices are delayed about 10 minutes, and the dashboard shows the
  data age. Real-time CME data needs a broker or data vendor (Tradovate, NinjaTrader, IBKR, Databento).
- **1-minute moves are close to a coin flip.** 52–55% accuracy is already good, and fees plus slippage
  (about $3 a round trip per MNQ here) can erase that. Watch the *Honest test* and *Live track record*
  cards, not single predictions.
- **News is mostly priced in** by the time it reaches an RSS feed. That's why it gets only a small,
  separately graded tilt.

This is a test tool, not financial advice. Futures are leveraged: 1 MNQ point = $2.
