# Run the paper bot 24/7 (even when your PC is off)

The cloud bot runs on GitHub's servers every 5 minutes, for free. It trades **paper money only**
(1 MNQ contract, $10,000 starting balance, 5-minute bars) and never connects to a broker.

## One-time setup (about 10 minutes)

1. **Get these files onto your repo's `main` branch.** They are on the branch
   `claude/phone-nasdaq-quant-trading-qnf6nb`: open a pull request and merge it. GitHub only runs
   scheduled workflows from the default branch.
2. **Let the bot save its state:** repo **Settings → Actions → General → Workflow permissions** →
   **Read and write permissions** → **Save**.
3. **Start it:** **Actions** tab → enable workflows if asked → **paper-bot** → **Run workflow**.
   After the first run, `state/bot.json` appears in the repo. From then on it runs about every 5 minutes.
   - Free unlimited Actions minutes need a **public** repo. Only paper trades are stored, no secrets.

## Watch it on your phone

1. Repo **Settings → Pages** → Source: **Deploy from a branch** → Branch **main**, folder **/ (root)** → **Save**.
2. After a minute, open `https://YOUR-NAME.github.io/quant.bot/live-quant-tracker.html` on your phone.
   It finds `state/bot.json` automatically. In Safari/Chrome use **Share → Add to Home Screen** to get an app icon.

The page refreshes every minute. GitHub's raw links can lag up to about 5 minutes.
To use another source, paste a `bot.json` link into the **Bot data source** box at the bottom.

## Changing settings

Edit `.github/workflows/paper-bot.yml` in the repo (under `env:`):

| Setting | Default | Meaning |
|---|---|---|
| `SYMBOL` | `MNQ=F` | also `NQ=F`, `^NDX`, `QQQ` |
| `TIMEFRAME` | `5m` | `5m`, `15m`, `1h`, `1d` (use `server.py` for `1m`) |
| `START_CASH` | `10000` | starting paper balance |
| `ENTER_LONG` / `ENTER_SHORT` | `0.55` / `0.45` | probability needed to buy / sell |
| `STOP_ATR` / `TARGET_ATR` | `1.5` / `2.0` | stop and target size, in average bar ranges |
| `MAX_HOLD_BARS` | `6` | close the trade after this many bars |
| `COMMISSION` / `SLIPPAGE_TICKS` | `1.00` / `1` | cost per side |
| `REQUIRE_EDGE` | `1` | `0` lets it trade even when the honest test failed (not recommended) |
| `NEWS_TILT` | `0.02` | how far the news mood can move the news-adjusted probability |

To restart from scratch, delete `state/bot.json` in the repo.

## Good to know

- **Market hours:** MNQ futures trade Sunday 6pm – Friday 5pm ET with a 1-hour break each day.
  The bot does nothing while the market is closed.
- **Timing:** GitHub schedules can run a few minutes late. The bot only acts on *closed* bars
  and replays every bar it missed, so stop losses and take profits are still checked against each bar's high/low.
- **Re-optimizes itself** every 100 bars using a 70% train / 30% unseen-test split, and logs the honest test result.
- **News:** each run saves the latest NASDAQ headlines (Yahoo Finance, CNBC, MarketWatch, Nasdaq.com) with a bullish/bearish tag.
- GitHub pauses scheduled workflows in repos with no activity for 60 days — the bot's own commits count as activity.
- This is a test tool, not financial advice. Futures are leveraged: 1 MNQ point = $2, and a normal day can move 300+ points.
