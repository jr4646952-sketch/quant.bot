# Run the paper bot 24/7 (even when your PC is off)

The cloud bot runs on GitHub's servers every 5 minutes, for free. It trades **paper money only**
(1 MNQ contract, $10,000 starting balance) and never connects to a broker.

## One-time setup (about 10 minutes)

1. **Make a GitHub account** at github.com if you don't have one.
2. **Create a new repository**: click **+** → **New repository** → name it e.g. `quant-bot` →
   choose **Public** → **Create repository**.
   - Public is needed for unlimited free Actions minutes. Only your paper trades are visible — no secrets are stored.
3. **Upload the files**: on the new repo page click **uploading an existing file** and drag in:
   - the `bot` folder (`bot/cloud_bot.py`)
   - the `.github` folder (`.github/workflows/paper-bot.yml`)
     - Tip: Windows hides folders starting with a dot. If dragging `.github` doesn't work, click
       **Add file → Create new file**, type the name `.github/workflows/paper-bot.yml`, and paste the file's contents.
   - optionally `live-quant-tracker.html`, `server.py`, `start.bat`
   Then click **Commit changes**.
4. **Allow the bot to save its state**: repo **Settings → Actions → General → Workflow permissions** →
   select **Read and write permissions** → **Save**.
5. **Start it**: **Actions** tab → enable workflows if asked → click **paper-bot** → **Run workflow**.
   After the first run a file `state/bot.json` appears in the repo. From then on it runs every 5 minutes by itself.

## Watch it live on the tracker page

Open `state/bot.json` in your repo, click **Raw**, and copy the link. It looks like:

```
https://raw.githubusercontent.com/YOUR-NAME/quant-bot/main/state/bot.json
```

Paste it into the **Cloud bot** box on the tracker page and press **Connect**. The panel refreshes every
minute (GitHub's raw links can lag up to ~5 minutes).

## Changing settings

Edit `.github/workflows/paper-bot.yml` in the repo:

| Setting | Default | Options |
|---|---|---|
| `SYMBOL` | `MNQ=F` | `NQ=F`, `^NDX`, `^IXIC`, `QQQ` |
| `TIMEFRAME` | `15m` | `5m`, `15m`, `1h`, `1d` |
| `START_CASH` | `10000` | any amount |

To restart from scratch, delete `state/bot.json` in the repo.

## Good to know

- **Market hours:** MNQ futures trade Sunday 6pm – Friday 5pm ET with a 1-hour break each day.
  The bot does nothing while the market is closed.
- **Timing:** GitHub schedules can run a few minutes late. The bot only acts on *closed* 15-minute bars
  and replays every bar it missed, so stop losses and take profits are still checked against each bar's high/low.
- **Re-optimizes itself** every 100 bars using a 70% train / 30% unseen-test split, and logs the honest test result.
- **News:** each run saves the latest NASDAQ headlines (Yahoo Finance, CNBC, MarketWatch, Nasdaq.com) with a bullish/bearish tag.
- GitHub pauses scheduled workflows in repos with no activity for 60 days — the bot's own commits count as activity.
- This is a test tool, not financial advice. Futures are leveraged: 1 MNQ point = $2, and a normal day can move 300+ points.
