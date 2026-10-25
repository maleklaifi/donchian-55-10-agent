# Donchian 55/10 agent

An independent, standard-library Python service based on the supplied **Donchian_55_10_Trading_Tools(1).zip**. Includes monthly selection, daily signals, a shared simulated portfolio, frozen stops, durable state, a dashboard, Docker packaging and tests. No connection to, shared files with, or changes to the NR7 agent.

**Paper is the default. Optional Binance Spot live/testnet execution is now implemented; see [LIVE_TRADING.md](LIVE_TRADING.md) for activation, account requirements and limitations. Live execution has not been authenticated or exchange-tested here.** The original files are preserved byte-for-byte in `reference/`.

The following execution description and cost model describe **paper mode**. Live mode uses actual exchange fills and native stops as explained in LIVE_TRADING.md.

## Strategy mapping

| Rule | Implementation |
| --- | --- |
| Market | Long-only Binance spot USDT, no leverage |
| Entry | Last confirmed UTC daily close strictly above highest high of the **preceding 55** daily candles |
| Channel exit | Last confirmed daily close strictly below lowest low of the **preceding 10** daily candles |
| Stop | Paper entry fill minus 2 × Wilder ATR(20) from signal candle, frozen for life of position |
| Universe | Top five by **previous complete UTC calendar month's quote volume**; original stable/fiat/metal/leveraged exclusions and minimum 60-day listing age |
| Membership change | Only new entries restricted; existing positions retain stop and channel exits |
| Per-trade risk | 0.5% of marked portfolio equity, inclusive of modeled entry and stop-exit costs |
| Portfolio risk | At most 2% equity in aggregate remaining positions' frozen initial stop-loss budgets at entry; no forced deleveraging if equity subsequently falls |
| Positions | Five maximum, one per symbol, no pyramiding |
| Entry cost cap | Minimum of $100, 50% equity and available shared cash, including entry fees |
| Minimum | $10 entry notional before fees |
| Costs | 0.20% fee each side; 0.05% adverse slippage each side |
| Default capital | $200; INITIAL_CAPITAL affects only creation of a new portfolio |

At $200 equity the per-trade risk budget is **$1**, not the older NR7 $2 setting. Position cost may be well below $100 because risk sizing can bind first.

For market price P and signal ATR A: `fill=P*1.0005`, `stop=fill-2*A`, `loss_per_unit=fill*1.002-stop*0.9995*0.998`. Quantity is the smaller of available risk budget divided by loss_per_unit and entry cost cap divided by `fill*1.002`.

## What “same strategy” means here

The source ZIP contains a single-chart Pine template and a forward selector, **not the audited v3.20 portfolio engine**. Signal lookbacks, selection policy and default sizing inputs are preserved. This is not a certified identical reproduction of that engine or TradingView's fill sequence. Decisions not specified by the ZIP are explicit here:

- Shared equity is cash plus held quantities marked at their latest fetched prices. Simultaneous entries use monthly volume-rank order (symbol as tie-breaker, inherited from selector). Portfolio risk means the sum of initial cost-inclusive stop budgets, not mark-to-stop exposure.
- Sizing uses the observed paper fill, not Pine's pre-fill estimate. Stops use exact 2×ATR without Pine's nearest-tick rounding; simulated quantities are fractional and do not enforce exchange lot filters. The optional live adapter applies exchange filters separately.
- ATR is seeded with the first 20 true ranges in up to 1,000 closed daily candles, then uses Wilder smoothing. A different history seed may produce tiny differences from TradingView.
- UTC daily close is checked on a 15-second loop, around **01:00 Tunisia time**. Network requests add latency. Entry window is five minutes after midnight UTC; late startup, unavailable data or a slow month-boundary ranking can skip that day's entry. No retrospective entry fills are invented.
- Monthly ranking uses the original scanner in a separate process so it does not block stops. Its only code change is atomic publication of selection.json. The original candidate pool uses the current exchange snapshot, not historical delisted membership. Failed rankings are retried after five minutes; no old monthly list is substituted. This is forward operation, not a historical backtest.
- Stops use polled quotes plus completed one-minute low checks, including recovery after downtime. Gap fills use the lower of minute open and stop, then adverse slippage. The partial minute containing entry is excluded from historical low checks to avoid a low that occurred before entry. A transient touch in that partial minute between polls can be missed. These are simulated stops, not exchange protection.
- After an outage, historical minute stop recovery occurs before the latest available channel-exit check. Missed daily channel exits during a multi-day outage are not replayed; results can diverge from uninterrupted operation. Data gaps pause new entries and remain visible as errors.
- Healthcheck means the web process is running; the dashboard separately shows stale market data/errors. It does not mean trading or market connectivity has been validated.

## Deploy as another service in your existing Railway project

1. Use the existing private repository `maleklaifi/donchian-55-10-agent` (or create your own copy).
2. Open the Railway project that currently contains NR7. Add a **new service from this GitHub repository**. Name it `donchian-55-10-agent`. Do not change the NR7 service's source or settings.
3. Attach a **new, dedicated persistent volume** to the new service, mounted at `/data`. Do not reuse the NR7 volume. The agent refuses to start on Railway without a matching volume mount.
4. Set these variables on the new service:

   ```text
   MODE=paper
   INITIAL_CAPITAL=200
   DATA_DIR=/data
   BINANCE_BASE_URL=https://data-api.binance.vision
   DASHBOARD_TOKEN=<your own long unique password>
   ```

5. Use the repository's root `Dockerfile`. Start command is already `python app.py`; no build command or pip installation is needed. Set healthcheck path `/healthz`, timeout 120 seconds, **one replica**, restart on failure. Keep the service continuously running; disable sleeping/serverless and do not use cron.
6. Deploy, then generate a public domain for the dashboard. Railway supplies PORT. Log in with username **admin** and your DASHBOARD_TOKEN password. Use the HTTPS domain.
7. Confirm logs and dashboard show a completed monthly selection, fresh market updates and no errors. The first ranking can take several minutes. Deployment outside the five-minute entry window starts monitoring immediately but waits for the next daily entry evaluation.
8. Redeploy once and verify balance and any trades persist. Back up the volume before upgrades. Changing INITIAL_CAPITAL does not reset existing balances or positions.

Railway configuration is described as dashboard steps because current Railway docs deprecate railway.toml/railway.json for new services. No project-wide infrastructure apply is included, avoiding changes to the existing project's other services.

Official documentation checked during preparation:
- https://docs.railway.com/infrastructure-as-code
- https://docs.railway.com/volumes
- https://docs.railway.com/overview/advanced-concepts
- https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md

A running separate service uses its own Railway resources. Deployment and Binance connectivity from your chosen region have not been tested here.

## Create and push the GitHub repository

If GitHub CLI is installed on your laptop, from this extracted directory:

```bash
gh auth login
git init -b main
git add .
git commit -m "Add Donchian 55/10 paper agent"
gh repo create donchian-55-10-agent --private --source=. --remote=origin --push
```

If a Git repository already exists (for example after restoring the included bundle), skip init/add/commit. Alternatively create an empty private repository through GitHub, then use the remote URL GitHub supplies:

```bash
git remote add origin <your-new-repository-URL>
git push -u origin main
```

Do not push to the existing NR7 repository. No `.env`, database, API keys or runtime data should be committed.

## Run locally on Ubuntu

Python 3.10+; no third-party packages.

```bash
python3 -m unittest discover -s tests -v
INITIAL_CAPITAL=200 DATA_DIR=./data python3 app.py
```

Visit http://localhost:8080. Optional: export DASHBOARD_TOKEN before launching to enable authentication locally. `.env.example` is documentation; the application does not load a .env file automatically.

## Operations and files

- `engine.py`: signals, sizing, paper entries/exits and accounting.
- `binance_live.py` / `live_agent.py`: opt-in Spot execution, filters and order reconciliation.
- `LIVE_TRADING.md`: real-money setup, explicit activation and recovery limits.
- `app.py`: worker, persistence, health endpoint and read-only dashboard API.
- `monthly_top5.py`: original selector plus atomic selection publishing.
- `dashboard.html`: equity/cash, monthly universe, holdings, closed trades and recent decisions.
- `/data/agent.sqlite3`: transactional durable state; `/data/monthly_rankings/`: frozen monthly selections and audits; `/data/ranking.log`: scanner progress/errors.
- Closed trades are retained; the last 2,000 decisions are retained. Dashboard displays the most recent 100 of each. No public write/reset endpoint exists.
- One process per DATA_DIR is enforced with an OS file lock. Never scale replicas or copy a running portfolio into two services.
- Public Binance access errors are exposed rather than replaced by fabricated data. No sample trade is preloaded.
