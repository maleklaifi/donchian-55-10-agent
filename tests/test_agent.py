import tempfile
import unittest
from pathlib import Path
import engine
import app
import monthly_top5


def state(capital=200):
    return dict(cash=capital, positions={}, closed=[])


def bars(count=80):
    return [[i*engine.DAY, '100', '102', '98', '100', '1', (i+1)*engine.DAY-1] for i in range(count)]


class StrategyTests(unittest.TestCase):
    def test_channels_exclude_signal_bar_and_wilder_atr(self):
        data=bars();data[-1][2]='110';data[-1][4]='109'
        x=engine.indicators(data)
        self.assertEqual(x['high'],102);self.assertEqual(x['low'],98)
        self.assertAlmostEqual(x['atr'],4.4);self.assertTrue(x['buy'])
    def test_strict_breakout_and_exit(self):
        data=bars();data[-1][4]='102'
        self.assertFalse(engine.indicators(data)['buy'])
        data[-1][3]='96';data[-1][4]='97'
        self.assertTrue(engine.indicators(data)['sell'])
    def test_missing_daily_bar_rejected(self):
        data=bars();del data[40]
        with self.assertRaises(ValueError):engine.indicators(data)
    def test_risk_includes_costs(self):
        s=state();p,reason=engine.size(s,{},100,2)
        self.assertEqual(reason,'accepted');self.assertAlmostEqual(p['initial_risk'],1)
        self.assertLessEqual(p['cost'],100);self.assertAlmostEqual(p['stop'],p['entry']-4)
        engine.enter(s,'BTCUSDT',p,1000)
        t=engine.close(s,'BTCUSDT',p['stop'],2000,'STOP')
        self.assertAlmostEqual(t['pnl'],-1);self.assertAlmostEqual(s['cash'],199)
    def test_gap_stop_loses_more_than_budget(self):
        s=state();p,_=engine.size(s,{},100,2);engine.enter(s,'BTC',p,1000)
        self.assertLess(engine.close(s,'BTC',80,2000,'GAP')['pnl'],-p['initial_risk'])
    def test_cash_notional_minimum_and_caps(self):
        s=state();p,_=engine.size(s,{},100,.01);self.assertAlmostEqual(p['cost'],100)
        s['cash']=9;self.assertIsNone(engine.size(s,{},100,2)[0])
        self.assertIsNone(engine.size(state(),{},100,60)[0])
    def test_portfolio_limit_and_five_positions(self):
        s=state(1000);prices={}
        for i in range(5):
            symbol=str(i);p,_=engine.size(s,prices,100,.01)
            engine.enter(s,symbol,p,1000);prices[symbol]=100
        self.assertEqual(engine.size(s,prices,100,.01)[1],'five_position_limit')
        s=state();s['positions']['OLD']={'qty':1,'initial_risk':4}
        self.assertIsNone(engine.size(s,{'OLD':1},100,2)[0])
    def test_month_boundary(self):
        prev,start=monthly_top5.month_bounds('2026-01')
        self.assertEqual(prev.strftime('%Y-%m'),'2025-12')
        self.assertEqual(start.strftime('%Y-%m'),'2026-01')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        self.store=app.Store(self.path);self.agent=app.Agent(self.store)
    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()
    def position(self, timestamp=1):
        p,_=engine.size(self.store.state,{},100,2)
        engine.enter(self.store.state,'BTCUSDT',p,timestamp)
        return p
    def test_persistent_balance_positions_closed_and_dedup(self):
        self.position();self.store.state['processed']['BTCUSDT']=42;self.store.save()
        other=app.Store(self.path);self.assertEqual(other.state,self.store.state);other.db.close()
        engine.close(self.store.state,'BTCUSDT',110,2000,'CHANNEL');self.store.save()
        other=app.Store(self.path)
        self.assertEqual(len(other.state['closed']),1)
        self.assertEqual(other.state['positions'],{});other.db.close()
    def test_intraminute_dip_recovered_after_price_rebounds(self):
        self.position();self.agent.api=lambda *a,**k:[[60000,'100','101','90','100','1',119999]]
        self.assertTrue(self.agent.stops('BTCUSDT',120001,101))
        self.assertEqual(self.store.state['closed'][0]['reason'],'ATR_STOP_MINUTE')
        self.assertAlmostEqual(self.store.state['closed'][0]['pnl'],-1)
    def test_entry_partial_minute_low_does_not_stop_position(self):
        self.position(1000)
        self.agent.api=lambda *a,**k:self.fail('Should not fetch entry partial minute')
        self.assertFalse(self.agent.stops('BTCUSDT',59000,100))
    def test_gap_fill_uses_lower_minute_open(self):
        self.position();self.agent.api=lambda *a,**k:[[60000,'80','85','79','82','1',119999]]
        self.agent.stops('BTCUSDT',120001,100)
        self.assertAlmostEqual(self.store.state['closed'][0]['exit'],80*(1-engine.SLIP))
    def test_missing_stop_history_fails_closed(self):
        self.position();self.agent.api=lambda *a,**k:[]
        with self.assertRaises(ValueError):self.agent.stops('BTCUSDT',120001,100)
        self.assertIn('BTCUSDT',self.store.state['positions'])
    def test_cycle_deduplicates_and_respects_outgoing_holdings(self):
        now=80*engine.DAY+1000
        self.agent.selection=lambda _:['BTCUSDT']
        self.agent.api=lambda path,params=None: {'serverTime':now} if path=='time' else {'price':'100'}
        self.agent.daily=lambda *_:dict(buy=True,sell=False,atr=2,high=99,low=95,close=100)
        self.agent.cycle();self.agent.cycle()
        self.assertEqual(len(self.store.state['positions']),1)
        self.assertEqual(len(self.store.state['decisions']),1)
        self.agent.selection=lambda _:[];self.agent.cycle()
        self.assertIn('BTCUSDT',self.store.state['positions'])
        self.agent.daily=lambda *_:dict(buy=False,sell=True,atr=2,high=99,low=95,close=90)
        self.agent.cycle();self.assertNotIn('BTCUSDT',self.store.state['positions'])
        self.assertEqual(self.store.state['closed'][0]['reason'],'DONCHIAN_10_CLOSE')
    def test_stale_entry_is_not_backfilled(self):
        now=80*engine.DAY+app.GRACE+1
        self.agent.selection=lambda _:['BTCUSDT']
        self.agent.api=lambda path,params=None:{'serverTime':now}
        self.agent.cycle();self.assertFalse(self.store.state['positions'])
        self.assertEqual(self.store.state['decisions'][0]['reason'],'entry_window_expired')
    def test_monthly_failure_keeps_exit_management(self):
        self.position(80*engine.DAY);now=80*engine.DAY+1000
        self.agent.api=lambda path,params=None: {'serverTime':now} if path=='time' else {'price':'90'}
        self.agent.selection=lambda _:[];self.agent.cycle()
        self.assertFalse(self.store.state['positions'])
        self.assertEqual(self.store.state['closed'][0]['reason'],'ATR_STOP_TICK')

if __name__=='__main__':unittest.main()
