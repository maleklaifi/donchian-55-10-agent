# Manual settings

Edit these variables in the agent service's environment, then redeploy. Paper remains the default execution mode. These are environment controls, not dashboard editing controls.

| Variable | Default | Meaning |
|---|---:|---|
| ENTRY_DAYS | 55 | Prior daily highs for entry |
| EXIT_DAYS | 10 | Prior daily lows for channel exit |
| ATR_DAYS | 20 | Wilder ATR period |
| ATR_MULTIPLIER | 2 | Initial frozen stop distance in ATR units |
| RISK_PERCENT | 0.5 | Equity risk budget per new trade, percent |
| PORTFOLIO_RISK_PERCENT | 2 | Aggregate initial risk ceiling for new entries, percent |
| MAX_POSITIONS | 5 | Maximum simultaneous positions |
| MAX_POSITION_USDT | 100 | Maximum entry cost |
| MIN_POSITION_USDT | 10 | Minimum entry notional |
| MAX_POSITION_EQUITY_PERCENT | 50 | Maximum entry cost as percent of equity |

Periods must be integers 1–500. Values must be positive and finite. MAX_POSITIONS should be an integer. Settings affect new entry calculations and future channel exits. Existing exchange stops retain their saved price and quantity; existing positions are not forcibly resized or closed merely because you lower a limit. A buy awaiting reconciliation uses the active ATR multiplier; avoid changing parameters while orders are pending.

The monthly selection remains the attached top-five policy. INITIAL_CAPITAL initializes a new ledger only: changing it does not change a running portfolio's balance. MODE, credentials and PAUSE_ENTRIES remain documented in LIVE_TRADING.md.

Changing these defaults changes the strategy; previous research results no longer describe that modified configuration. The dashboard's static rule summary describes defaults; consult configured environment values for overrides.

## Dashboard controls

The dashboard now has a Settings form and Save settings button. Authenticate with the configured dashboard password. Changes are validated and saved to the current mode's SQLite ledger, override environment defaults, and apply without redeployment. The daily indicator cache is cleared. Already processed daily decisions are not replayed. Pending exchange orders block changes until reconciliation finishes. Existing stops and position quantities remain fixed; future channel exits use the new exit period.

The form controls the variables in the table above. Execution mode, API credentials and account allocation are not editable from this form. Requests require dashboard authentication plus a session CSRF token. Paper/testnet/live keep separate saved overrides. The maximum supported position count is five, matching the monthly entry universe.
