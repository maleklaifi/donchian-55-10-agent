import copy
import tempfile
import unittest
from pathlib import Path
from decimal import ROUND_DOWN
from unittest.mock import patch

import app
import engine
from binance_live import D, Filters
from live_agent import LiveAgent
from test_live import FakeExchange, INFO


class ScenarioExchange(FakeExchange):
    def __init__(self, step, partial, fee_asset):
        super().__init__()
        self.step = D(step)
        self.partial = D(partial)
        self.fee_asset = fee_asset

    def fill(self, order):
        if order['side'] != 'BUY':
            return super().fill(order)
        qty = (D(order['origQty']) * self.partial / self.step).to_integral_value(rounding=ROUND_DOWN) * self.step
        quote = qty * self.price
        if self.fee_asset == 'BTC':
            fee = qty * D('.001')
            self.base += qty - fee
            self.cash -= quote
        else:
            fee = quote * D('.001') if self.fee_asset == 'USDT' else D('.00001')
            self.base += qty
            self.cash -= quote + (fee if self.fee_asset == 'USDT' else D(0))
        order.update(status='EXPIRED' if self.partial < 1 else 'FILLED', executedQty=str(qty), cummulativeQuoteQty=str(quote))
        self.trades[order['orderId']] = [dict(id=order['orderId'], orderId=order['orderId'], qty=str(qty), quoteQty=str(quote), commission=str(fee), commissionAsset=self.fee_asset, price=str(self.price))]


class ExecutionStress(unittest.TestCase):
    def run_scenario(self, *, step='.01', partial='1', fee='BTC', fill='100'):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = app.Store(Path(tmp.name), 'testnet')
        self.addCleanup(store.db.close)
        client = ScenarioExchange(step, partial, fee)
        client.price = D(fill)
        agent = LiveAgent(store, 'testnet', client)
        agent.account()
        info = copy.deepcopy(INFO)
        info['filters'][1]['stepSize'] = step
        agent.filters['BTCUSDT'] = Filters(info)
        with patch.dict(engine.settings.OVERRIDES, {'RISK_PERCENT': 1}):
            order, reason = engine.size(store.state, {}, 100, 2)
            self.assertEqual(reason, 'accepted')
            agent.buy('BTCUSDT', order, client.sync_time(), order['initial_risk'])
        return store, client, agent

    def test_base_fee_quantity_boundary_is_protected_or_safely_exited(self):
        store,client,_ = self.run_scenario(step='.01', fee='BTC')
        position = store.state['positions'].get('BTCUSDT')
        if position:
            self.assertEqual(position['phase'], 'OPEN')
            stop = client.orders[position['stop_order']['newClientOrderId']]
            self.assertEqual(stop['status'], 'NEW')
            self.assertLessEqual(D(stop['origQty']), D(position['qty']))
            self.assertLessEqual(D(position['initial_risk']), D(position['risk_budget'])*D('1.001'))
        else:
            self.assertEqual(store.state['closed'][-1]['reason'], 'POST_FILL_RISK_CHECK')

    def test_quote_fee_is_accounted_for(self):
        store,client,_ = self.run_scenario(fee='USDT')
        position=store.state['positions'].get('BTCUSDT')
        check=position['risk_check'] if position else store.state['closed'][-1]['risk_check']
        self.assertGreater(D(check['quote_fee']), 0)
        self.assertEqual(D(check['base_fee']), 0)

    def test_other_asset_fee_halts_new_entries(self):
        store,_,_ = self.run_scenario(fee='BNB')
        self.assertIn('Third-asset commissions', store.state['live_halt'])

    def test_partial_terminal_fill_is_not_rebought_and_has_protection(self):
        store,client,_ = self.run_scenario(partial='.5',fee='USDT')
        position=store.state['positions']['BTCUSDT']
        self.assertEqual(position['phase'],'OPEN')
        self.assertEqual(client.orders[position['buy']['newClientOrderId']]['status'],'EXPIRED')
        self.assertEqual(len([p for p in client.sent if p['side']=='BUY']),1)
        self.assertEqual(client.orders[position['stop_order']['newClientOrderId']]['status'],'NEW')

    def test_tiny_partial_fill_below_exchange_minimum_halts_for_intervention(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        store=app.Store(Path(tmp.name),'testnet');self.addCleanup(store.db.close)
        client=ScenarioExchange('.01','.2','USDT')
        agent=LiveAgent(store,'testnet',client);agent.account()
        info=copy.deepcopy(INFO);info['filters'][1]['stepSize']='0.01'
        agent.filters['BTCUSDT']=Filters(info)
        with patch.dict(engine.settings.OVERRIDES, {'RISK_PERCENT': 1}):
            order,_=engine.size(store.state,{},100,2)
            with self.assertRaisesRegex(RuntimeError,'residual cannot be sold'):
                agent.buy('BTCUSDT',order,client.sync_time(),order['initial_risk'])
        self.assertIn('residual cannot be sold',store.state['live_halt'])
        self.assertEqual(store.state['positions']['BTCUSDT']['phase'],'EXITING')
        self.assertEqual(len([p for p in client.sent if p['side']=='BUY']),1)

    def test_price_gap_never_leaves_an_open_position_over_its_limit(self):
        for fill in ('95','100','105'):
            with self.subTest(fill=fill):
                store,client,_ = self.run_scenario(fill=fill,fee='USDT')
                position=store.state['positions'].get('BTCUSDT')
                if position:
                    self.assertEqual(position['phase'],'OPEN')
                    self.assertLessEqual(D(position['initial_risk']),D(position['risk_budget'])*D('1.001'))
                    self.assertEqual(client.orders[position['stop_order']['newClientOrderId']]['status'],'NEW')
                else:
                    self.assertEqual(store.state['closed'][-1]['reason'],'POST_FILL_RISK_CHECK')

    def test_restart_after_failed_check_does_not_open_overrisk_position(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        path=Path(tmp.name); store=app.Store(path,'testnet'); self.addCleanup(store.db.close)
        client=ScenarioExchange('.01','1','USDT');client.price=D('20')
        agent=LiveAgent(store,'testnet',client);agent.account()
        info=copy.deepcopy(INFO);info['filters'][1]['stepSize']='0.01'
        agent.filters['BTCUSDT']=Filters(info)
        old_save=store.save
        def crash_after_save():
            old_save()
            p=store.state['positions'].get('BTCUSDT',{})
            if p.get('risk_check',{}).get('result')=='EXIT_REQUESTED':
                raise RuntimeError('simulated process crash')
        store.save=crash_after_save
        with patch.dict(engine.settings.OVERRIDES, {'RISK_PERCENT': 1}):
            order,_=engine.size(store.state,{},100,2)
            with self.assertRaisesRegex(RuntimeError,'simulated process crash'):
                agent.buy('BTCUSDT',order,client.sync_time(),order['initial_risk'])
            reopened=app.Store(path,'testnet');self.addCleanup(reopened.db.close)
            restarted=LiveAgent(reopened,'testnet',client);restarted.filters['BTCUSDT']=Filters(info)
            restarted.reconcile('BTCUSDT',client.sync_time())
            self.assertNotIn('BTCUSDT',reopened.state['positions'])
            self.assertEqual(reopened.state['closed'][-1]['reason'],'POST_FILL_RISK_CHECK')

