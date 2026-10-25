"""Opt-in live/testnet runner, with persistent order intents and exchange stops."""
import hashlib
import os
import time
from decimal import Decimal, ROUND_DOWN

import engine
from app import Agent
from binance_live import Client, Filters, Journal, D, TERMINAL


class LiveAgent(Agent):
    def __init__(self, store, mode, client=None):
        super().__init__(store)
        self.mode=mode
        self.client=client or Client(mode)
        fingerprint=hashlib.sha256(self.client.key.encode()).hexdigest()
        if self.state.get('key_fingerprint',fingerprint)!=fingerprint:
            raise ValueError('API key identity changed; refusing all exchange operations')
        self.journal=Journal(self.client,self.store.save)
        self.state.setdefault('dust',{})
        self.state.setdefault('live_halt',None)
        self.state.setdefault('bootstrapped',False)
        self.filters={}

    def rules(self,symbol):
        if symbol not in self.filters:
            info=self.client.call('GET','exchangeInfo',{'symbol':symbol},signed=False)
            self.filters[symbol]=Filters(info['symbols'][0])
        return self.filters[symbol]

    def account(self):
        result=self.client.call('GET','account')
        if not result.get('canTrade') or result.get('accountType')!='SPOT':
            raise RuntimeError('Account must have Spot trading permission')
        fingerprint=hashlib.sha256(self.client.key.encode()).hexdigest()
        if self.state.get('key_fingerprint',fingerprint)!=fingerprint:
            raise RuntimeError('API key identity changed; operator reconciliation required')
        self.state['key_fingerprint']=fingerprint
        balances={b['asset']:(D(b['free']),D(b['locked'])) for b in result['balances']}
        orders=self.client.call('GET','openOrders')
        known={item['newClientOrderId'] for p in self.state['positions'].values()
               for item in [p['buy'],p.get('stop_order'),*p.get('exit_orders',[])] if item}
        if any(o['clientOrderId'] not in known for o in orders):
            raise RuntimeError('Unmanaged open orders detected; new entries paused')
        expected=dict(self.state['dust'])
        for p in self.state['positions'].values():
            asset=p['base_asset'];expected[asset]=str(D(expected.get(asset,0))+D(p['qty']))
        for asset,(free,locked) in balances.items():
            if asset=='USDT':continue
            # Dedicated clean Spot account: do not adopt or sell unrelated holdings.
            if free+locked != D(expected.get(asset,0)):
                raise RuntimeError(f'{asset}: account holdings differ from ledger; new entries paused')
        if not self.state['bootstrapped']:
            if balances.get('USDT',(D(0),D(0)))[0]<D(self.state['initial_capital']):
                raise RuntimeError('Insufficient free USDT for INITIAL_CAPITAL allocation')
            self.state['bootstrapped']=True
            self.store.save()
        if balances.get('USDT',(D(0),D(0)))[0]+D('0.00000001')<D(self.state['cash']):
            raise RuntimeError('Free USDT below strategy cash; withdrawals/manual activity or fees require reconciliation')
        return balances

    def totals(self,p,item,order):
        records=self.journal.fills(item,order)
        qty=sum((D(r['qty']) for r in records),D(0))
        quote=sum((D(r['quoteQty']) for r in records),D(0))
        base_fee=sum((D(r['commission']) for r in records if r['commissionAsset']==p['base_asset']),D(0))
        quote_fee=sum((D(r['commission']) for r in records if r['commissionAsset']=='USDT'),D(0))
        other={r['commissionAsset'] for r in records if D(r['commission']) and r['commissionAsset'] not in (p['base_asset'],'USDT')}
        if other:
            self.state['live_halt']='Third-asset commissions detected: '+','.join(sorted(other))+'. P&L excludes these fees; reconcile manually.'
        item['fills']=records
        return qty,quote,base_fee,quote_fee

    def buy(self,symbol,order,now,risk_budget):
        rule=self.rules(symbol)
        if symbol in self.state['positions']:raise RuntimeError('Duplicate symbol entry')
        quote=D(order['qty'])*D(order['entry'])
        precision=int(rule.info.get('quoteAssetPrecision',8))
        quote=quote.quantize(Decimal(1).scaleb(-precision),rounding=ROUND_DOWN)
        # Reserve maximum modeled base commission before checking stop sellability.
        estimated_qty=rule.quantity(D(order['qty'])*(1-D(engine.FEE)))
        stop=rule.stop(order['stop'])
        rule.validate(estimated_qty,stop)
        if quote<D(10):raise ValueError('Below strategy minimum entry')
        item=self.journal.intent(symbol,'BUY','MARKET',quoteOrderQty=quote)
        self.state['positions'][symbol]=dict(symbol=symbol,base_asset=rule.info['baseAsset'],
            phase='BUYING',buy=item,stop_order=None,exit_orders=[],qty=0,entry=0,stop=0,
            mark=order['entry'],initial_risk=risk_budget,cost=0,atr=order['atr'],
            entered_at=now,risk_budget=risk_budget,exit_requested=False,exit_reason=None)
        self.state['processed'][symbol]=now//engine.DAY*engine.DAY
        self.store.save()
        self.reconcile(symbol,now)

    def reconcile(self,symbol,now):
        p=self.state['positions'][symbol]
        rule=self.rules(symbol)
        if p['phase']=='BUYING':
            buy=self.journal.order(p['buy'])
            if buy['status'] not in TERMINAL:
                buy=self.journal.cancel(p['buy'])
                if buy['status'] not in TERMINAL:raise RuntimeError(f'{symbol}: buy still active; entries paused')
            qty,quote,base_fee,quote_fee=self.totals(p,p['buy'],buy)
            if qty==0:
                del self.state['positions'][symbol]
                self.record(symbol,now,'SKIP','LIVE_BUY_NO_FILL');self.store.save();return
            net=qty-base_fee
            entry=quote/qty
            # Freeze relative stop to actual VWAP. Tick rounds UP, never widens risk.
            raw_stop=entry-2*D(p['atr'])
            try:stop=rule.stop(raw_stop) if raw_stop>0 else D(0)
            except ValueError:stop=D(0)
            p.update(qty=float(net),entry=float(entry),stop=float(stop),cost=float(quote+quote_fee),phase='PROTECTING')
            self.state['cash']-=p['cost']
            loss=(quote+quote_fee)-rule.quantity(net)*stop*(1-D(engine.SLIP))*(1-D(engine.FEE))
            p['initial_risk']=float(max(D(0),loss))
            self.state['errors']=[f'{symbol}: buy filled; exchange stop not yet acknowledged']
            self.store.save()
            if stop<=0 or loss>D(p['risk_budget'])*D('1.001'):
                p.update(exit_requested=True,exit_reason='POST_FILL_RISK_CHECK',phase='EXITING')
                self.store.save()
        if p['phase']=='PROTECTING':
            qty=rule.quantity(p['qty'])
            if not p['stop_order']:
                try:
                    rule.validate(qty,p['stop'])
                    p['stop_order']=self.journal.intent(symbol,'SELL','STOP_LOSS',quantity=qty,stopPrice=p['stop'])
                    self.store.save()
                except ValueError:
                    p.update(exit_requested=True,exit_reason='STOP_FILTER_FAILURE',phase='EXITING')
                    self.store.save()
            if p['phase']=='PROTECTING':
                stop=self.journal.order(p['stop_order'])
                if stop['status']=='REJECTED':
                    p.update(exit_requested=True,exit_reason='STOP_REJECTED',phase='EXITING')
                else:p['phase']='OPEN'
                self.store.save()
        if p['phase']=='OPEN':
            stop=self.journal.order(p['stop_order'])
            p['protection_status']=stop['status']
            if p['exit_requested'] and stop['status'] not in TERMINAL:
                stop=self.journal.cancel(p['stop_order'])
            if stop['status'] in TERMINAL:
                p.update(phase='EXITING',exit_requested=True,
                    exit_reason=p['exit_reason'] or ('ATR_STOP' if D(stop['executedQty'])>0 else 'STOP_MISSING_OR_CANCELED'))
                self.store.save()
            elif stop['status']!='NEW':
                raise RuntimeError(f'{symbol}: stop partially filled/pending; awaiting exchange execution')
            elif p['exit_requested']:
                raise RuntimeError(f'{symbol}: stop cancellation not confirmed; market exit blocked')
        if p['phase']=='EXITING':
            self.exit_remaining(p,now,rule)

    def exit_remaining(self,p,now,rule):
        symbol=p['symbol']
        sold=D(0);proceeds=D(0);sell_fees=D(0)
        # Query every bot-owned sell; never duplicate a possibly active sell.
        for item in [p.get('stop_order'),*p['exit_orders']]:
            if not item:continue
            order=self.journal.order(item)
            if order['status'] not in TERMINAL:
                order=self.journal.cancel(item)
                if order['status'] not in TERMINAL:
                    raise RuntimeError(f'{symbol}: existing sell is active; cannot submit another')
            qty,quote,base_fee,quote_fee=self.totals(p,item,order)
            sold+=qty+base_fee;proceeds+=quote-quote_fee;sell_fees+=quote_fee
        remaining=D(p['qty'])-sold
        if remaining<0:raise RuntimeError('Sell reconciliation exceeds owned quantity')
        qty=rule.quantity(remaining)
        price=D(self.client.call('GET','ticker/price',{'symbol':symbol},signed=False)['price'])
        if qty>0:
            try:rule.validate(qty,price)
            except ValueError:
                self.state['live_halt']=f'{symbol}: residual cannot be sold under exchange filters. Manual action required.'
                self.store.save()
                raise RuntimeError(self.state['live_halt'])
            if p['exit_orders'] and p['exit_orders'][-1].get('rejected'):
                raise RuntimeError(f'{symbol}: emergency/channel sell rejected; operator must reconcile')
            item=self.journal.intent(symbol,'SELL','MARKET',quantity=qty)
            p['exit_orders'].append(item);self.store.save()
            order=self.journal.order(item)
            if order['status']=='REJECTED':raise RuntimeError(f'{symbol}: sell rejected; position requires attention')
            # Re-enter once on completed fills; partial fill handled on next cycle.
            if order['status']=='FILLED':return self.exit_remaining(p,now,rule)
            raise RuntimeError(f'{symbol}: market exit pending; new entries paused')
        self.state['cash']+=float(proceeds)
        if remaining:
            self.state['dust'][p['base_asset']]=str(D(self.state['dust'].get(p['base_asset'],0))+remaining)
        closed=dict(p,exited_at=now,reason=p['exit_reason'],exit=float(proceeds/sold) if sold else 0,
                    pnl=float(proceeds)-p['cost'],exit_fee=float(sell_fees),dust=str(remaining))
        self.state['closed'].append(closed)
        del self.state['positions'][symbol]
        self.state['processed'][symbol]=now//engine.DAY*engine.DAY
        self.record(symbol,now,'SELL',p['exit_reason']);self.store.save()

    def cycle(self):
        now=self.client.sync_time();day=now//engine.DAY*engine.DAY
        self.filters={};self.daily_cache={k:v for k,v in self.daily_cache.items() if k[1]==day}
        self.state['last_cycle']=now;errors=[];prices={}
        # Reconcile/protect owned positions before account consistency checks.
        for symbol in list(self.state['positions']):
            try:
                self.reconcile(symbol,now)
                if symbol not in self.state['positions']:continue
                p=self.state['positions'][symbol]
                price=float(self.client.call('GET','ticker/price',{'symbol':symbol},signed=False)['price'])
                if not __import__('math').isfinite(price) or price<=0:raise ValueError('Invalid mark')
                prices[symbol]=price;p['mark']=price
                if p['phase']=='OPEN' and self.daily(symbol,day)['sell']:
                    p.update(exit_requested=True,exit_reason='DONCHIAN_10_CLOSE')
                    self.store.save();self.reconcile(symbol,now)
            except Exception as exc:errors.append(f'{symbol}: {exc}')
        try:self.account()
        except Exception as exc:errors.append(str(exc))
        selected=self.selection(now)
        if self.state['live_halt']:errors.append(self.state['live_halt'])
        if os.environ.get('PAUSE_ENTRIES','false').lower()=='true':errors.append('New entries paused by operator; existing positions remain managed')
        if any(p['phase']!='OPEN' or p.get('protection_status','NEW')!='NEW' for p in self.state['positions'].values()):
            errors.append('Order reconciliation pending; new entries paused')
        for symbol in selected:
            if errors:break
            if symbol in self.state['positions'] or self.state['processed'].get(symbol)==day:continue
            try:
                if now-day>300_000:
                    self.record(symbol,now,'SKIP','entry_window_expired')
                else:
                    ind=self.daily(symbol,day)
                    if ind['buy']:
                        price=float(self.client.call('GET','ticker/price',{'symbol':symbol},signed=False)['price'])
                        order,reason=engine.size(self.state,prices,price,ind['atr'])
                        fresh=self.client.sync_time()
                        if order and fresh-day<=300_000:
                            # Preserve the actual estimated initial risk as post-fill ceiling.
                            self.buy(symbol,order,fresh,order['initial_risk'])
                            self.record(symbol,fresh,'BUY','LIVE_ORDER_SUBMITTED',**ind)
                            if symbol in self.state['positions']:prices[symbol]=price
                            self.account()
                        else:self.record(symbol,fresh,'SKIP',reason if not order else 'entry_window_expired')
                    else:self.record(symbol,now,'PASS','no_breakout',**ind)
                self.state['processed'][symbol]=day;self.store.save()
            except ValueError as exc:
                self.record(symbol,now,'SKIP',str(exc));self.state['processed'][symbol]=day;self.store.save()
            except Exception as exc:errors.append(f'{symbol}: {exc}')
        self.state['errors']=errors
        if not errors:self.state['last_success']=now
        self.store.save()
