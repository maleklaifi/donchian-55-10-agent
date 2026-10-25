# Donchian 55/10 tools

## Pine strategy
Open `donchian_55_10.pine`, copy into TradingView Pine Editor, save and add to a BINANCE spot USDT **1D** chart. Initial chart equity defaults to $200; change Strategy Properties to your chosen simulated capital. Risk defaults to 0.5%. Exit lookback can be changed to 20 for the reference.

Run the monthly scanner. Enable the `Allow entries` input only on selected symbols; disable it on symbols leaving the list, while retaining their existing exit/stop management. Do not force-close a held asset solely because it leaves the top five. Avoid deleting/restarting a chart with an open simulated position: changing inputs recalculates historical results. This toggle is a manual forward-use permission, NOT a historical monthly universe backtest.

Create a strategy alert using **alert() calls only** for confirmed entry/channel signals, **order fills only** for emulator fill messages, or both if duplicate kinds of notifications are wanted. Pine alerts/emulator stops do NOT place or protect real Binance orders. Actual trading needs a separate execution system with acknowledged stop protection.

Important limits:
- Pine runs one symbol/account per chart. It cannot enforce the shared 2% portfolio risk cap, five-position maximum, or shared cash across charts. Apply these outside Pine. Chart equity is not your total Binance portfolio equity.
- Market entry quantity must be submitted before the unknown next-open price. Sizing uses signal close plus estimated 0.05% slippage; gaps can change actual notional/risk. This is NOT exact fill-based sizing from the Python portfolio backtest.
- The stop distance uses the signal candle ATR20 and a relative stop bracket submitted with entry. It anchors to the emulator's actual fill, is frozen, and is rounded to the nearest price tick. No trailing stop.
- The emulator charges 0.20% per side. Its execution slippage setting is in TICKS, not percent. The code does NOT claim to reproduce the backtest's 0.05% proportional slippage. Slippage is included only in the sizing estimate; configure tick slippage separately if wanted.
- Pine notional/quantity examples are for spot crypto units; exchange lot sizes, notional limits and actual fees must be checked before an order. No leverage.
- Pine has not been compiled inside TradingView in this environment. Broker emulator intrabar assumptions and chart data can differ from Binance and the audited engine. This is a single-chart strategy template, not a certified clone of the portfolio engine.

## Python monthly selection scanner
Requires Python 3.10+; no packages or API keys. Makes public read-only requests, never submits orders.

```bash
python3 monthly_top5.py
python3 monthly_top5.py --month 2026-10
```

The selected month uses the PRECEDING complete UTC calendar month's quote-asset volume. The current month is selected by default. At the beginning of each new month, rerun the command to obtain that month's selection. A first scan can take several minutes: two requests per eligible symbol with throttling/retries.

Files under `monthly_rankings/YYYY-MM/`:
- `selection.json`: top five, exclusions, timestamps, candidate policy, snapshot hash.
- `rankings.csv`: all eligible ranked symbols.
- `tradingview_watchlist.txt`: five Binance tickers to import into a watchlist.
- `exchange_snapshot.json` and `coverage_audit.json`: discovery and failure evidence.

Existing selections are reused without re-fetching. `--refresh` explicitly replaces them; preserve your original output if already used for trading. Fetch failure for any candidate prevents publishing a potentially incomplete top five. If Binance is inaccessible in your region, the script fails; it does not manufacture results. Use `--base-url https://api.binance.com` only where accessible.

Candidates are CURRENTLY TRADING spot USDT pairs from the exchange snapshot, excluding the explicit stable/fiat/metal and known leveraged lists in the source. Minimum age: 60 days at the selection month's start, measured from first daily bar. Future stable/leveraged products can require reviewed additions; the explicit list is not a guarantee of exhaustive product classification. `--exclude BASE1,BASE2` adds exclusions; record any policy change.

This is a forward monthly selector, NOT the historical archive-universe discovery in v3.20. `--month` for a past month still uses today's candidate snapshot and therefore has survivorship bias. It must not be used to reproduce the 2018–2026 research. Delisted historical pairs require the frozen archive dataset and its separate audit.

Neither file changes your existing agents or the frozen v3.20 research. No automated monthly job or live order execution has been started.
