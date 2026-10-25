"""Deterministic paper portfolio. All monetary amounts are USDT."""
import math
import os
import settings

def setting(name, default):
    value=float(settings.OVERRIDES.get(name,os.environ.get(name,default)))
    if not math.isfinite(value) or value<=0:
        raise ValueError(name+" must be positive and finite")
    return value

def periods():
    values=[setting("ENTRY_DAYS",55),setting("EXIT_DAYS",10),setting("ATR_DAYS",20)]
    if any(v!=int(v) or v>500 for v in values):
        raise ValueError("Periods must be integers from 1 to 500")
    return [int(v) for v in values]


FEE = 0.002
SLIP = 0.0005
DAY = 86_400_000


def indicators(bars):
    """Closed Binance daily bars; Wilder ATR20, prior-bar channels."""
    entry_days,exit_days,atr_days=periods()
    if len(bars) < max(entry_days+1,exit_days+1,atr_days):
        raise ValueError('At least 56 closed daily bars required')
    for i, b in enumerate(bars):
        values = [float(b[j]) for j in (1, 2, 3, 4)]
        if not all(math.isfinite(v) and v > 0 for v in values):
            raise ValueError('Invalid OHLC')
        if values[2] > min(values[0], values[3]) or values[1] < max(values[0], values[3]):
            raise ValueError('Inconsistent OHLC')
        if i and int(b[0]) - int(bars[i-1][0]) != DAY:
            raise ValueError('Missing or duplicate daily candle')
    tr = []
    for i, b in enumerate(bars):
        h, l = float(b[2]), float(b[3])
        previous = float(bars[i-1][4]) if i else float(b[4])
        tr.append(max(h-l, abs(h-previous), abs(l-previous)))
    atr = sum(tr[:atr_days])/atr_days
    for value in tr[atr_days:]:
        atr = (atr*(atr_days-1) + value)/atr_days
    high = max(float(b[2]) for b in bars[-entry_days-1:-1])
    low = min(float(b[3]) for b in bars[-exit_days-1:-1])
    close = float(bars[-1][4])
    return dict(high=high, low=low, atr=atr, close=close,
                buy=close > high, sell=close < low)


def equity(state, prices):
    return state['cash'] + sum(p['qty']*prices[s] for s,p in state['positions'].items())


def size(state, prices, market, atr):
    eq = equity(state, prices)
    fill = market*(1+SLIP)
    stop = fill-setting("ATR_MULTIPLIER",2)*atr
    if not all(math.isfinite(v) for v in (eq, fill, stop)) or eq <= 0 or stop <= 0 or atr <= 0:
        return None, 'invalid_equity_or_stop'
    if len(state['positions']) >= setting("MAX_POSITIONS",5):
        return None, 'five_position_limit'
    loss = fill*(1+FEE)-stop*(1-SLIP)*(1-FEE)
    # Frozen initial stop-loss risk, inclusive of modeled entry/exit costs.
    committed = sum(p['initial_risk'] for p in state['positions'].values())
    budget = min(eq*setting("RISK_PERCENT",0.5)/100, max(0, eq*setting("PORTFOLIO_RISK_PERCENT",2)/100-committed))
    cash_cap = min(setting("MAX_POSITION_USDT",100), eq*setting("MAX_POSITION_EQUITY_PERCENT",50)/100, state['cash'])
    qty = min(budget/loss, cash_cap/(fill*(1+FEE)))
    if qty*fill < setting("MIN_POSITION_USDT",10):
        return None, 'below_10_usdt_minimum_or_risk_cap'
    return dict(qty=qty, entry=fill, stop=stop, atr=atr, initial_risk=qty*loss,
                cost=qty*fill*(1+FEE)), 'accepted'


def enter(state, symbol, order, timestamp):
    if symbol in state['positions']:
        raise ValueError('Duplicate position')
    state['cash'] -= order['cost']
    state['positions'][symbol] = dict(order, symbol=symbol, entered_at=timestamp,
                                      stop_cursor=timestamp, mark=order['entry'])


def close(state, symbol, market, timestamp, reason):
    p = state['positions'].pop(symbol)
    fill = market*(1-SLIP)
    proceeds = p['qty']*fill*(1-FEE)
    state['cash'] += proceeds
    trade = dict(p, exit=fill, exited_at=timestamp, reason=reason,
                 pnl=proceeds-p['cost'], exit_fee=p['qty']*fill*FEE)
    state['closed'].append(trade)
    return trade
