import tempfile
from pathlib import Path
import unittest
import app
import engine
import settings

class SettingsTests(unittest.TestCase):
    def tearDown(self):settings.OVERRIDES.clear()
    def test_validation(self):
        for key,value in [('RISK_PERCENT',float('nan')),('MAX_POSITIONS',2.5),('EXIT_DAYS',501),('MIN_POSITION_USDT',101)]:
            data=dict(settings.DEFAULTS);data[key]=value
            with self.assertRaises(ValueError):settings.validate(data)
    def test_settings_persist_and_affect_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            store=app.Store(Path(directory));values=dict(settings.DEFAULTS);values['RISK_PERCENT']=.25
            store.state['settings']=settings.validate(values);store.save();store.db.close()
            restored=app.Store(Path(directory))
            self.assertEqual(engine.setting('RISK_PERCENT',.5),.25)
            restored.db.close()
    def test_unknown_fields_rejected(self):
        with self.assertRaises(ValueError):settings.validate(dict(settings.DEFAULTS,MODE='live'))
