import unittest,tempfile
from pathlib import Path
import app,controls
class ControlsTests(unittest.TestCase):
 def test_pause_persists(self):
  with tempfile.TemporaryDirectory() as d:
   store=app.Store(Path(d));agent=app.Agent(store)
   controls.execute(agent,{'action':'pause'});self.assertTrue(agent.state['automation_paused'])
   controls.execute(agent,{'action':'resume'});self.assertFalse(agent.state['automation_paused']);store.db.close()
 def test_unknown_action_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   store=app.Store(Path(d));agent=app.Agent(store)
   with self.assertRaises(ValueError):controls.execute(agent,{'action':'withdraw','symbol':'BTCUSDT'})
   store.db.close()

from test_live import FakeExchange
from live_agent import LiveAgent
class LiveControlTests(unittest.TestCase):
 def test_manual_buy_sell_and_balance(self):
  with tempfile.TemporaryDirectory() as d:
   store=app.Store(Path(d),'testnet');client=FakeExchange();agent=LiveAgent(store,'testnet',client)
   agent.account();agent.daily=lambda *_:dict(atr=2,buy=False,sell=False)
   controls.execute(agent,dict(action='buy',symbol='BTCUSDT'))
   self.assertEqual(agent.state['positions']['BTCUSDT']['phase'],'OPEN')
   controls.execute(agent,dict(action='sell',symbol='BTCUSDT'))
   self.assertFalse(agent.state['positions'])
   client.cash+=100
   controls.execute(agent,dict(action='balance'))
   self.assertEqual(agent.state['cash'],float(client.cash))
   self.assertTrue(agent.state['balance_auto_sync']);store.db.close()
