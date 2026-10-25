import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
import engine
from binance_live import D, ExchangeError, Filters, Journal, UncertainOrder
from live_agent import LiveAgent

INFO=dict(symbol='BTCUSDT',baseAsset='BTC',quoteAsset='USDT',quoteAssetPrecision=8,
          status='TRADING',isSpotTradingAllowed=True,quoteOrderQtyMarketAllowed=True,
          orderTypes=['MARKET','STOP_LOSS'],filters=[
    dict(filterType='PRICE_FILTER',minPrice='0.01',maxPrice='1000000',tickSize='0.01'),
    dict(filterType='LOT_SIZE',minQty='0.00001',maxQty='10000',stepSize='0.00001'),
    dict(filterType='MARKET_LOT_SIZE',minQty='0',maxQty='10000',stepSize='0'),
    dict(filterType='MIN_NOTIONAL',minNotional='5',applyToMarket=True)])


class FakeExchange:
    key='fake-not-a-real-api-key'
    def __init__(self):
        self.orders={};self.trades={};self.sent=[];self.cash=D(200);self.base=D(0)
        self.price=D(100);self.fail=None;self.cancel_race=False;self.other_orders=[]
    def sync_time(self):return 80*engine.DAY+1000
    def query(self,symbol,cid):return copy.deepcopy(self.orders.get(cid))
    def fill(self,o):
        qty=D(o['origQty']);quote=qty*self.price
        commission=qty*D('.001') if o['side']=='BUY' else quote*D('.001')
        if o['side']=='BUY':self.cash-=quote;self.base+=qty-commission
        else:self.cash+=quote-commission;self.base-=qty
        o.update(status='FILLED',executedQty=str(qty),cummulativeQuoteQty=str(quote))
        self.trades[o['orderId']]=[dict(id=o['orderId'],orderId=o['orderId'],qty=str(qty),
            quoteQty=str(quote),commission=str(commission),commissionAsset='BTC' if o['side']=='BUY' else 'USDT',price=str(self.price))]
    def call(self,method,path,params=None,signed=True):
        p=params or {}
        if path=='exchangeInfo':return {'symbols':[copy.deepcopy(INFO)]}
        if path=='ticker/price':return {'price':str(self.price)}
        if path=='account':return dict(canTrade=True,accountType='SPOT',balances=[
            dict(asset='USDT',free=str(self.cash),locked='0'),dict(asset='BTC',free=str(self.base),locked='0')])
        if path=='openOrders':return [copy.deepcopy(o) for o in self.orders.values() if o['status']=='NEW']+self.other_orders
        if path=='myTrades':return copy.deepcopy(self.trades.get(p['orderId'],[]))
        if method=='DELETE':
            o=self.orders[p['origClientOrderId']]
            if self.cancel_race:self.fill(o)
            else:o['status']='CANCELED'
            return copy.deepcopy(o)
        if method=='POST':
            self.sent.append(copy.deepcopy(p))
            if self.fail=='unknown_buy' and p['side']=='BUY':raise UncertainOrder('timeout')
            if self.fail=='reject_stop' and p['type']=='STOP_LOSS':raise ExchangeError(-1013,400)
            if self.fail=='unknown_stop' and p['type']=='STOP_LOSS':raise UncertainOrder('stop timeout')
            qty=D(p['quoteOrderQty'])/self.price if p['side']=='BUY' else D(p['quantity'])
            o=dict(orderId=len(self.orders)+1,clientOrderId=p['newClientOrderId'],symbol=p['symbol'],
                side=p['side'],type=p['type'],origQty=str(qty),executedQty='0',cummulativeQuoteQty='0',status='NEW')
            self.orders[o['clientOrderId']]=o
            if p['type']=='MARKET':self.fill(o)
            if self.fail=='filled_then_timeout' and p['side']=='BUY':raise UncertainOrder('timeout after exchange fill')
            return copy.deepcopy(o)
        raise AssertionError((method,path))


class JournalTests(unittest.TestCase):
    def test_intent_persisted_before_post(self):
        c=FakeExchange();events=[]
        real=c.call
        def call(*a,**kw):events.append('request');return real(*a,**kw)
        c.call=call;j=Journal(c,lambda:events.append('save'))
        i=j.intent('BTCUSDT','BUY','MARKET',quoteOrderQty=20)
        j.order(i);self.assertEqual(events[:2],['save','request'])
    def test_unknown_post_not_retried(self):
        c=FakeExchange();c.fail='unknown_buy';j=Journal(c,lambda:None)
        i=j.intent('BTCUSDT','BUY','MARKET',quoteOrderQty=20)
        for _ in range(3):
            with self.assertRaises(UncertainOrder):j.order(i)
        self.assertEqual(len(c.sent),1)
    def test_timeout_after_fill_recovers_original_order(self):
        c=FakeExchange();c.fail='filled_then_timeout';j=Journal(c,lambda:None)
        i=j.intent('BTCUSDT','BUY','MARKET',quoteOrderQty=20)
        self.assertEqual(j.order(i)['status'],'FILLED')
        self.assertEqual(j.order(i)['status'],'FILLED');self.assertEqual(len(c.sent),1)
    def test_incomplete_fill_history_rejected(self):
        c=FakeExchange();j=Journal(c,lambda:None);i=j.intent('BTCUSDT','BUY','MARKET',quoteOrderQty=20)
        o=j.order(i);c.trades={}
        with self.assertRaises(RuntimeError):j.fills(i,o)
    def test_native_stop_capability_required(self):
        i=copy.deepcopy(INFO);i['orderTypes']=['MARKET']
        with self.assertRaises(ValueError):Filters(i)
    def test_rounding_never_increases_quantity_or_widens_stop(self):
        f=Filters(INFO)
        self.assertEqual(f.quantity('0.123456'),D('0.12345'))
        self.assertEqual(f.stop('96.001'),D('96.01'))
    def test_intersection_of_market_and_regular_lot_steps(self):
        i=copy.deepcopy(INFO);i['filters'][1]['stepSize']='0.002';i['filters'][2]['stepSize']='0.003'
        self.assertEqual(Filters(i).quantity('.01'),D('.006'))
    def test_mode_requires_explicit_opt_in(self):
        with patch.dict(os.environ,{'MODE':'live','LIVE_TRADING_ACK':'','DASHBOARD_TOKEN':'x'}):
            with self.assertRaises(ValueError):app.execution_mode()
        with patch.dict(os.environ,{'MODE':'live','LIVE_TRADING_ACK':'I_ACCEPT_REAL_MONEY_RISK','DASHBOARD_TOKEN':'x'}):
            self.assertEqual(app.execution_mode(),'live')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        self.store=app.Store(self.path,'testnet');self.client=FakeExchange()
        self.agent=LiveAgent(self.store,'testnet',self.client);self.agent.account()
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def buy(self):
        order,_=engine.size(self.store.state,{},100,2)
        self.agent.buy('BTCUSDT',order,self.client.sync_time(),order['initial_risk'])
        return self.store.state['positions']['BTCUSDT']
    def test_buy_attaches_stop_and_accounts_base_commission(self):
        p=self.buy();self.assertEqual(p['phase'],'OPEN')
        self.assertEqual(self.client.sent[1]['type'],'STOP_LOSS')
        self.assertLess(D(self.client.sent[1]['quantity']),D(self.client.orders[p['buy']['newClientOrderId']]['executedQty']))
        self.agent.account()
    def test_stop_survives_restart_without_new_order(self):
        self.buy();self.store.save();other=app.Store(self.path,'testnet')
        try:
            agent=LiveAgent(other,'testnet',self.client);agent.reconcile('BTCUSDT',self.client.sync_time())
            self.assertEqual(len(self.client.sent),2);agent.account()
        finally:other.db.close()
    def test_native_stop_fill_closes_ledger(self):
        p=self.buy();self.client.price=D(96)
        self.client.fill(self.client.orders[p['stop_order']['newClientOrderId']])
        self.agent.reconcile('BTCUSDT',self.client.sync_time())
        self.assertFalse(self.store.state['positions']);self.assertEqual(len(self.store.state['closed']),1)
        self.assertEqual(len(self.client.sent),2);self.agent.account()
    def test_channel_exit_cancels_stop_before_market_sell(self):
        p=self.buy();p.update(exit_requested=True,exit_reason='DONCHIAN_10_CLOSE')
        self.agent.reconcile('BTCUSDT',self.client.sync_time())
        self.assertFalse(self.store.state['positions']);self.assertEqual(len(self.client.sent),3)
        self.assertEqual(self.client.orders[p['stop_order']['newClientOrderId']]['status'],'CANCELED')
        self.agent.account()
    def test_cancel_fill_race_does_not_sell_twice(self):
        p=self.buy();p.update(exit_requested=True,exit_reason='DONCHIAN_10_CLOSE');self.client.cancel_race=True
        self.agent.reconcile('BTCUSDT',self.client.sync_time())
        self.assertFalse(self.store.state['positions']);self.assertEqual(len(self.client.sent),2)
    def test_stop_rejection_triggers_emergency_exit(self):
        self.client.fail='reject_stop';order,_=engine.size(self.store.state,{},100,2)
        self.agent.buy('BTCUSDT',order,self.client.sync_time(),order['initial_risk'])
        self.assertFalse(self.store.state['positions'])
        self.assertEqual(self.store.state['closed'][0]['reason'],'STOP_REJECTED')
    def test_unknown_buy_survives_restart_without_retry(self):
        self.client.fail='unknown_buy';order,_=engine.size(self.store.state,{},100,2)
        with self.assertRaises(UncertainOrder):self.agent.buy('BTCUSDT',order,self.client.sync_time(),order['initial_risk'])
        other=app.Store(self.path,'testnet')
        try:
            agent=LiveAgent(other,'testnet',self.client)
            with self.assertRaises(UncertainOrder):agent.reconcile('BTCUSDT',self.client.sync_time())
            self.assertEqual(len(self.client.sent),1)
        finally:other.db.close()
    def test_unknown_stop_never_sells_blindly_or_duplicates_stop(self):
        self.client.fail='unknown_stop';order,_=engine.size(self.store.state,{},100,2)
        with self.assertRaises(UncertainOrder):self.agent.buy('BTCUSDT',order,self.client.sync_time(),order['initial_risk'])
        with self.assertRaises(UncertainOrder):self.agent.reconcile('BTCUSDT',self.client.sync_time())
        self.assertEqual(len(self.client.sent),2)
        self.assertEqual(self.store.state['positions']['BTCUSDT']['phase'],'PROTECTING')
    def test_cancel_timeout_does_not_place_a_second_sell(self):
        p=self.buy();p.update(exit_requested=True,exit_reason='DONCHIAN_10_CLOSE')
        original=self.client.call
        def call(method,*args,**kwargs):
            if method=='DELETE':raise UncertainOrder('cancel timeout')
            return original(method,*args,**kwargs)
        self.client.call=call
        with self.assertRaises(RuntimeError):self.agent.reconcile('BTCUSDT',self.client.sync_time())
        self.assertEqual(len(self.client.sent),2)
    def test_pause_entries_does_not_cancel_existing_protection(self):
        self.buy();self.agent.selection=lambda _:['ETHUSDT']
        self.agent.daily=lambda *_:dict(sell=False)
        with patch.dict(os.environ,{'PAUSE_ENTRIES':'true'}):self.agent.cycle()
        self.assertEqual(len(self.client.sent),2)
        self.assertIn('BTCUSDT',self.store.state['positions'])
    def test_unmanaged_holdings_prevent_entries(self):
        self.client.base=D(1)
        with self.assertRaises(RuntimeError):self.agent.account()
    def test_unmanaged_orders_prevent_entries(self):
        self.client.other_orders=[{'clientOrderId':'manual-order'}]
        with self.assertRaises(RuntimeError):self.agent.account()
    def test_key_change_refused_before_reconciliation(self):
        self.client.key='a-different-key'
        with self.assertRaises(ValueError):LiveAgent(self.store,'testnet',self.client)
        self.assertFalse(self.client.sent)
    def test_separate_mode_databases(self):
        paper=app.Store(self.path,'paper')
        self.buy();self.assertFalse(paper.state['positions']);paper.db.close()
    def test_cycle_places_entry_once(self):
        self.agent.selection=lambda _:['BTCUSDT']
        self.agent.daily=lambda *_:dict(buy=True,sell=False,atr=2,high=99,low=90,close=100)
        self.agent.cycle();self.agent.cycle()
        self.assertEqual(len(self.client.sent),2)
        self.assertEqual(self.store.state['errors'],[])

if __name__=='__main__':unittest.main()
