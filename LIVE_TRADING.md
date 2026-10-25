# Binance Spot real-money mode

The repository now contains an opt-in Binance Spot execution adapter. **It has been tested with a simulated exchange, not authenticated against Binance or Spot Testnet in this environment.** No real orders were placed, no credentials were accessed, and no Railway service was changed. Local tests do not establish production readiness or profitability.

## What changes from paper mode

- `MODE=live` uses signed HMAC requests to `https://api.binance.com`; `MODE=testnet` uses `https://testnet.binance.vision`. The signed endpoint host is not configurable.
- Market buys specify a USDT spending amount. Quantity and average price come from actual exchange fills. Actual base/USDT commissions are reconciled from trade history.
- After a buy fills, a native Binance `STOP_LOSS` sell is submitted at actual entry VWAP minus 2×signal ATR20, rounded **up** to the price tick. Sell quantity rounds down to valid lot increments after base-asset fees.
- An acknowledged exchange-held stop can trigger while the agent is offline. Channel exits still need the agent online. A stop-market order does not guarantee the stop price or maximum loss.
- The bot cancels and verifies its stop before a channel market exit. A stop filling during cancellation is reconciled before deciding whether any quantity remains to sell.
- Each order intent/client ID is saved before sending. Unknown outcomes are queried, never blindly re-submitted. An unresolved outcome pauses new entries and appears on the dashboard. If an attempted order is permanently absent, manual journal reconciliation is required; automatic retry is deliberately avoided.
- Definite stop rejection triggers an attempted emergency market liquidation. If the stop outcome is **unknown**, it does not blindly liquidate alongside a potentially active stop. Reconciliation must resolve the order first. API failure, missing fill history, rejected emergency sell, or insufficient notional can leave an unprotected balance that requires operator action.
- The post-fill risk check attempts liquidation if modeled stop loss exceeds the entry allocation by more than 0.1% relative tolerance. The check uses the existing estimated cost assumptions for the future stop exit. Fees, price gaps and emergency liquidation losses can exceed the intended budget.

There is an unavoidable interval between the market entry and stop acknowledgement. The adapter does **not** claim atomic buy-and-stop protection. `PROTECTING`, `BUYING`, `EXITING`, stale updates, or any errors need attention. There are dashboard/log alerts, but no external push notifications.

## Account and allocation requirements

Use a dedicated clean Spot account/subaccount, where available, for this bot. At first initialization it expects only free USDT and no open orders or unrelated crypto holdings. It never imports or sells pre-existing manual holdings. Do not manually trade, withdraw its assets, or run another bot in the same account. Binance account eligibility and API permissions must permit Spot trading in your location.

`INITIAL_CAPITAL=200` allocates $200 from actual free USDT. Extra USDT is not automatically added to strategy equity. Deposits do not increase the allocation; withdrawals below ledger cash pause entries. Changing this variable later does not reset the ledger or alter the allocation.

Rules remain 0.5% equity risk per trade, 2% aggregate initial risk, five positions, and $100/50%-equity entry-cost caps. Thus $200 initial equity means approximately **$1 intended risk per trade**, not $2. Exchange minimums may cause trades to be skipped. Do not raise risk just to make a rejected trade pass.

Disable paying commissions in BNB for this dedicated account. Fees in an unexpected third asset halt new entries and flag incomplete P&L accounting. Tiny unsellable lot remainders are tracked as dust, assigned zero portfolio value and conservatively included as a cost in realized P&L.

## Configure yourself

1. Review and test the order lifecycle before funding/activation. `MODE=testnet` supports execution plumbing checks with separate Spot Testnet credentials; it is not a strategy forward test. Public signal/ranking data still use the configured public Binance data host by default, while testnet fills use the testnet market. Testnet prices, listings and balances can differ and its resets invalidate stored testnet state.
2. Use an HMAC API key for the intended account. Enable reading and Spot trading; **disable withdrawals**, and do not enable futures or margin for this task. Use an IP allowlist that matches a verified fixed outbound IP of the hosting service. Do not paste the key/secret into chat or commit them to GitHub. The implementation has no withdrawal, transfer, margin or futures endpoints.
3. In the separate agent service's private environment variables, set:

   ```text
   MODE=live
   LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_RISK
   BINANCE_API_KEY=<your Binance Spot API key>
   BINANCE_API_SECRET=<your Binance HMAC secret>
   INITIAL_CAPITAL=200
   DATA_DIR=/data
   DASHBOARD_TOKEN=<your own long unique password>
   PAUSE_ENTRIES=true
   ```

   Keep the dedicated persistent `/data` volume, one replica, continuous operation and HTTPS dashboard described in README. Never share the NR7 volume. The volume contains financial/order history and needs backups.

4. With entries paused, verify the dashboard says **LIVE**, the expected $200 allocation and fresh cycle timestamps appear, and account validation succeeds. The expected “New entries paused by operator” notice is normal. Any other error needs resolution before activation. No order is sent merely by this clean-account validation.
5. When you deliberately want the agent to submit real orders, set `PAUSE_ENTRIES=false` yourself. It waits for a new eligible daily signal within the five-minute UTC-midnight window; it does not buy simply because you enable live mode. Inspect the first exchange order and its matching native stop directly in Binance. Do not treat a healthy web endpoint as proof of protection.

No automatic activation or deployment is performed by this code change. Existing deployments stay in paper mode unless you change the variables.

## Pause, shutdown and recovery

- To stop **new buys** while managing existing positions: set `PAUSE_ENTRIES=true` and keep MODE and credentials unchanged. It still reconciles and submits required exits.
- Stopping the service leaves acknowledged native stops on Binance, but daily channel exits stop running. It does not cancel exchange orders or liquidate automatically.
- Do not switch an active live portfolio to paper/testnet: separate databases prevent mode mixing but the live positions would no longer be managed. Do not delete/reset `live.sqlite3` while there are orders or holdings.
- Databases are `agent.sqlite3` (paper), `testnet.sqlite3`, and `live.sqlite3`. Monthly ranking evidence is shared; financial ledgers are separate.
- API key changes are blocked before execution to avoid silently controlling another account. Rotation requires deliberately reconciling the account and persisted key fingerprint after backing up state; do not delete the portfolio to bypass it.
- Unknown order errors: check the stored client order ID against Binance orders/trades, including the actual filled quantity, fees, and current protective order. Do not manually submit a replacement until the first order's outcome is known. Resolve ledger/account discrepancies before resuming entries.
- Do not run multiple replicas or two copies with different volumes against the same account. The file lock prevents duplicates only for the same mounted data directory.
- Daily exit signals missed during a multi-day outage are not historically replayed. Native stops remain exchange-managed after acknowledgement, subject to exchange/order availability.

## Verification and documentation

Local tests cover paper strategy behavior, durable order intent, timeouts before/after fills, duplicate prevention, exchange quantity/price rounding, stop rejection and emergency exit, cancellation/fill races, account isolation, separate ledgers, and API key changes. Exchange connectivity, real filters for your selected symbols, real commissions, permissions, order execution, IP allowlisting and deployment remain unverified.

Official Binance references consulted:
- https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/trade
- https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account
- https://developers.binance.com/en/docs/products/spot/filters
- https://developers.binance.com/en/docs/products/spot/faqs/spot_glossary

## Dashboard operator controls

Manual buy/sell controls now submit orders through the same execution adapter when the service is in live mode (simulated in paper mode). Manual buys bypass monthly membership, breakout confirmation and the daily time window; risk/cash/position limits and ATR protection still apply. Sell closes only a bot-managed position. These manual trades change the portfolio and are not part of the historical strategy backtest. Every operation requires authenticated access and a CSRF token; the dashboard asks you to confirm it.

Pause automatic entries prevents automatic new buys but continues daily exits, exchange reconciliation and protection. Manual orders remain available. For a complete process stop, stop the hosting service; acknowledged native stops remain on Binance, and channel exits then cease. The dashboard intentionally does not terminate the web process.

Use actual account balance explicitly opts into sizing with ALL free USDT in the dedicated account. After this selection, free USDT is synchronized each cycle; deposits/withdrawals are recorded as balance adjustments, not trading profits. Existing managed assets remain part of marked equity; unrelated holdings/orders still block activity. This overrides the earlier fixed initial allocation behavior. Until you select it, the fixed allocation remains in force.

Stop strategy automation is also available: it suspends automatic entries AND daily channel exits while retaining protective stop management and order reconciliation. Resume restores both. Pending protective/emergency exits already requested are still completed. Neither stop control liquidates existing holdings. Manual actions remain enabled. To terminate the entire process, use the hosting service stop control; that also disables reconciliation.
