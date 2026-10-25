"""Validated dashboard overrides; credentials and execution mode are never editable."""
import math
import os
DEFAULTS=dict(ENTRY_DAYS=55,EXIT_DAYS=10,ATR_DAYS=20,ATR_MULTIPLIER=2,RISK_PERCENT=.5,PORTFOLIO_RISK_PERCENT=2,MAX_POSITIONS=5,MAX_POSITION_USDT=100,MIN_POSITION_USDT=10,MAX_POSITION_EQUITY_PERCENT=50)
INTEGER={'ENTRY_DAYS','EXIT_DAYS','ATR_DAYS','MAX_POSITIONS'}
OVERRIDES={}

def current():
    return {k:float(OVERRIDES.get(k,os.environ.get(k,v))) for k,v in DEFAULTS.items()}

def validate(values):
    if not isinstance(values,dict) or set(values)!=set(DEFAULTS):
        raise ValueError('Provide all supported settings, and no extra fields')
    result={}
    for k,v in values.items():
        if isinstance(v,bool):raise ValueError(k+' must be numeric')
        v=float(v)
        if not math.isfinite(v) or v<=0:raise ValueError(k+' must be positive and finite')
        if k in INTEGER and (v!=int(v) or v>(5 if k=='MAX_POSITIONS' else 500)):
            raise ValueError(k+' must be an integer within the allowed range')
        if ('PERCENT' in k and v>100) or (k=='ATR_MULTIPLIER' and v>100):
            raise ValueError(k+' must be at most 100')
        result[k]=v
    if result['RISK_PERCENT']>result['PORTFOLIO_RISK_PERCENT']:raise ValueError('Per-trade risk cannot exceed portfolio risk')
    if result['MIN_POSITION_USDT']>result['MAX_POSITION_USDT']:raise ValueError('Minimum position cannot exceed maximum')
    return result
