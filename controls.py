"""Authenticated operator controls. Invoked under the worker lock."""
import engine

def execute(agent, command):
    state=agent.state
    action=command.get('action')
    if action in ('pause','stop','resume'):
        state['automation_paused']=action in ('pause','stop')
        state['automation_stopped']=action=='stop'
        agent.store.save()
        return {'message':{'pause':'Automatic entries paused; exits and stops remain managed.','stop':'Strategy entries and channel exits stopped; protective stops and reconciliation remain active.','resume':'Strategy automation resumed.'}[action]}
    if action=='balance':
        if not hasattr(agent,'client'):raise ValueError('Balance synchronization requires live/testnet mode')
        agent.account(sync_balance=True)
        return {'message':'Free USDT synchronized from exchange.'}
    symbol=str(command.get('symbol','')).upper()
    if not symbol.isalnum() or not symbol.endswith('USDT'):raise ValueError('Use a Spot USDT symbol such as BTCUSDT')
    if action=='sell':
        if symbol not in state['positions']:raise ValueError('No managed position for this symbol')
        now=agent.client.sync_time() if hasattr(agent,'client') else int(agent.api('time')['serverTime'])
        if hasattr(agent,'client'):
            p=state['positions'][symbol]
            if p['phase']!='OPEN':raise ValueError('Wait for pending order reconciliation')
            p.update(exit_requested=True,exit_reason='MANUAL_SELL');agent.store.save();agent.reconcile(symbol,now)
        else:
            price=float(agent.api('ticker/price',{'symbol':symbol})['price'])
            engine.close(state,symbol,price,now,'MANUAL_SELL');state['processed'][symbol]=now//engine.DAY*engine.DAY
        agent.record(symbol,now,'SELL','MANUAL_SELL');agent.store.save()
        return {'message':'Sell processed. Inspect positions and order status.'}
    if action!='buy':raise ValueError('Unknown control action')
    if symbol in state['positions']:raise ValueError('Symbol already held')
    if any(p.get('phase','OPEN')!='OPEN' for p in state['positions'].values()):raise ValueError('Order reconciliation pending')
    if hasattr(agent,'client'):
        agent.account()
        if state.get('live_halt'):raise ValueError('Resolve the account halt before buying')
    now=agent.client.sync_time() if hasattr(agent,'client') else int(agent.api('time')['serverTime'])
    day=now//engine.DAY*engine.DAY
    ind=agent.daily(symbol,day)
    prices={}
    for held in [*state['positions'],symbol]:
        price=float(agent.client.call('GET','ticker/price',{'symbol':held},signed=False)['price']) if hasattr(agent,'client') else float(agent.api('ticker/price',{'symbol':held})['price'])
        prices[held]=price
    order,reason=engine.size(state,prices,prices[symbol],ind['atr'])
    if not order:raise ValueError(reason)
    # Manual override bypasses breakout/monthly membership/time window only.
    # It retains risk/cash/position limits and frozen ATR protection.
    if hasattr(agent,'client'):agent.buy(symbol,order,now,order['initial_risk'])
    else:engine.enter(state,symbol,order,now)
    state['processed'][symbol]=day
    agent.record(symbol,now,'BUY','MANUAL_BUY');agent.store.save()
    return {'message':'Buy processed with risk sizing and ATR stop. Inspect order status.'}
