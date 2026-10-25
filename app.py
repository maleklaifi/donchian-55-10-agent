"""Donchian service: paper by default, explicitly enabled Spot execution optional."""
import datetime as dt
import fcntl
import hmac
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import engine
import settings
import secrets
from monthly_top5 import request

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('DATA_DIR', './data'))
BASE = os.environ.get('BINANCE_BASE_URL', 'https://data-api.binance.vision')
TOKEN = os.environ.get('DASHBOARD_TOKEN', '')
POLL = 15
GRACE = 300_000
LOCK = threading.RLock()
HALT = threading.Event()
LOG = logging.getLogger('donchian')


class Store:
    def __init__(self, directory, mode="paper"):
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory/('agent.sqlite3' if mode=='paper' else mode+'.sqlite3'), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
        row = self.db.execute('SELECT body FROM state WHERE id=1').fetchone()
        capital = float(os.environ.get('INITIAL_CAPITAL', '200'))
        if capital <= 0 or not __import__('math').isfinite(capital):
            raise ValueError('INITIAL_CAPITAL must be positive and finite')
        self.state = json.loads(row[0]) if row else dict(cash=capital, initial_capital=capital,
            positions={}, closed=[], decisions=[], processed={}, last_cycle=None,
            last_success=None, errors=[], selection=None, mode=mode.upper(), version=1)
        if self.state['mode'] != mode.upper():
            raise ValueError('Stored portfolio mode does not match configured mode')
        settings.OVERRIDES.clear()
        settings.OVERRIDES.update(self.state.get('settings',{}))
        self.save()

    def save(self):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (1, ?)', (json.dumps(self.state, allow_nan=False),))


class Agent:
    def __init__(self, store):
        self.store = store
        self.ranker = None
        self.rank_log = None
        self.retry_rank = 0
        self.daily_cache = {}

    @property
    def state(self):
        return self.store.state

    def api(self, path, params=None):
        return request(BASE, '/api/v3/'+path, params)

    def record(self, symbol, timestamp, action, reason, **details):
        self.state['decisions'].append(dict(symbol=symbol, time=timestamp, action=action, reason=reason, **details))
        self.state['decisions'] = self.state['decisions'][-2000:]
        LOG.info('%s %s %s', symbol, action, reason)

    def selection(self, now):
        month = dt.datetime.fromtimestamp(now/1000, dt.timezone.utc).strftime('%Y-%m')
        path = DATA/'monthly_rankings'/month/'selection.json'
        if self.ranker is not None and self.ranker.poll() is not None:
            if self.ranker.returncode:
                LOG.error('Monthly ranking failed; see ranking.log')
                self.retry_rank = time.monotonic()+300
            self.ranker = None
            self.rank_log.close()
        if path.exists():
            result = json.loads(path.read_text())
            if result['selection_month'] != month or len(result['top5']) != 5:
                raise ValueError('Invalid monthly selection')
            self.state['selection'] = result
            return [x['symbol'] for x in result['top5']]
        # Never reuse an old month's list. Stops/exits continue while ranking runs.
        self.state['selection'] = None
        if self.ranker is None and time.monotonic() >= self.retry_rank:
            self.rank_log = (DATA/'ranking.log').open('a')
            self.ranker = subprocess.Popen([sys.executable, str(ROOT/'monthly_top5.py'),
                '--month', month, '--out', str(DATA/'monthly_rankings'), '--base-url', BASE],
                stdout=self.rank_log, stderr=subprocess.STDOUT)
        return []

    def daily(self, symbol, day):
        key = (symbol, day)
        if key not in self.daily_cache:
            bars = self.api('klines', dict(symbol=symbol, interval='1d', endTime=day-1, limit=1000))
            if not bars or int(bars[-1][0]) != day-engine.DAY:
                raise ValueError(f'{symbol}: latest closed daily candle missing')
            self.daily_cache[key] = engine.indicators(bars)
        return self.daily_cache[key]

    def stops(self, symbol, now, price):
        p = self.state['positions'][symbol]
        cursor = p['stop_cursor']
        # Scan completed minutes after entry, including downtime, before current tick.
        # Entry partial minute is excluded: its low might have preceded entry.
        start = ((cursor+59_999)//60_000)*60_000
        end = now//60_000*60_000
        pages = 0
        while start < end:
            bars = self.api('klines', dict(symbol=symbol, interval='1m', startTime=start, endTime=end-1, limit=1000))
            if not bars or int(bars[0][0]) != start:
                raise ValueError(f'{symbol}: missing stop-monitoring history')
            for b in bars:
                if int(b[0]) != start:
                    raise ValueError(f'{symbol}: gap in minute history')
                if float(b[3]) <= p['stop']:
                    engine.close(self.state, symbol, min(float(b[1]), p['stop']), int(b[6]), 'ATR_STOP_MINUTE')
                    self.state['processed'][symbol] = now//engine.DAY*engine.DAY
                    self.record(symbol, int(b[6]), 'SELL', 'ATR_STOP_MINUTE')
                    self.store.save()
                    return True
                start += 60_000
                p['stop_cursor'] = start
            pages += 1
            if pages >= 100 and start < end:
                self.store.save()
                raise ValueError(f'{symbol}: catching up minute history; entries paused')
        if price <= p['stop']:
            engine.close(self.state, symbol, price, now, 'ATR_STOP_TICK')
            self.state['processed'][symbol] = now//engine.DAY*engine.DAY
            self.record(symbol, now, 'SELL', 'ATR_STOP_TICK')
            self.store.save()
            return True
        p['mark'] = price
        return False

    def cycle(self):
        now = int(self.api('time')['serverTime'])
        day = now//engine.DAY*engine.DAY
        self.daily_cache = {k:v for k,v in self.daily_cache.items() if k[1] == day}
        self.state['last_cycle'] = now
        errors = []
        stopped = set()
        prices = {}
        # Fetch held-symbol quotes first. Missing marks prohibit all new entries.
        for symbol in list(self.state['positions']):
            try:
                price = float(self.api('ticker/price', {'symbol':symbol})['price'])
                if not __import__('math').isfinite(price) or price <= 0:
                    raise ValueError('Invalid quote')
                prices[symbol] = price
                if self.stops(symbol, now, price):
                    stopped.add(symbol)
                    # No same-day re-entry after any stop, including recovered stops.
                    self.state['processed'][symbol] = day
                else:
                    ind = self.daily(symbol, day)
                    if ind['sell']:
                        engine.close(self.state, symbol, price, now, 'DONCHIAN_10_CLOSE')
                        self.state['processed'][symbol] = day
                        self.record(symbol, now, 'SELL', 'DONCHIAN_10_CLOSE', **ind)
                        self.store.save()
            except Exception as exc:
                errors.append(f'{symbol}: {exc}')
        selected = self.selection(now)
        for symbol in selected:
            if symbol in self.state['positions'] or symbol in stopped or self.state['processed'].get(symbol) == day:
                continue
            if errors:
                break
            # Do not invent historical fills on late initial deployment/restart.
            if now-day > GRACE:
                self.record(symbol, now, 'SKIP', 'entry_window_expired')
                self.state['processed'][symbol] = day
                continue
            try:
                ind = self.daily(symbol, day)
                if ind['buy']:
                    price = float(self.api('ticker/price', {'symbol':symbol})['price'])
                    # Recheck clock after network calls: never fill an expired signal.
                    fill_time = int(self.api('time')['serverTime'])
                    if fill_time-day > GRACE:
                        self.record(symbol, fill_time, 'SKIP', 'entry_window_expired')
                    else:
                        order, reason = engine.size(self.state, prices, price, ind['atr'])
                        if order:
                            engine.enter(self.state, symbol, order, fill_time)
                            prices[symbol] = price
                        self.record(symbol, fill_time, 'BUY' if order else 'SKIP', reason, **ind)
                else:
                    self.record(symbol, now, 'PASS', 'no_breakout', **ind)
                self.state['processed'][symbol] = day
                self.store.save()
            except Exception as exc:
                errors.append(f'{symbol}: {exc}')
        self.state['errors'] = errors
        if not errors:
            self.state['last_success'] = now
        self.store.save()

    def run(self):
        while not HALT.is_set():
            try:
                with LOCK:
                    self.cycle()
            except Exception as exc:
                LOG.exception('Cycle failed')
                with LOCK:
                    self.state['errors'] = [str(exc)]
                    self.store.save()
            HALT.wait(POLL)
        if self.ranker and self.ranker.poll() is None:
            self.ranker.terminate()


class Handler(BaseHTTPRequestHandler):
    store = None
    agent = None
    csrf_token = secrets.token_urlsafe(32)

    def authenticated(self):
        if not TOKEN:return False
        import base64
        expected='Basic '+base64.b64encode(('admin:'+TOKEN).encode()).decode()
        return hmac.compare_digest(self.headers.get('Authorization',''),expected)

    def do_POST(self):
        if self.path!='/api/settings':return self.send(404,b'Not found','text/plain')
        if not self.authenticated():return self.send(401,b'Authentication required','text/plain')
        if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),self.csrf_token):
            return self.send(403,b'Reload the dashboard before saving','text/plain')
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=8192:raise ValueError('Invalid request size')
            values=settings.validate(json.loads(self.rfile.read(length)))
            with LOCK:
                if any(p.get('phase','OPEN')!='OPEN' for p in self.store.state['positions'].values()):
                    raise ValueError('Wait for pending orders to finish before changing settings')
                previous=self.store.state.get('settings',{})
                self.store.state['settings']=values
                try:self.store.save()
                except Exception:
                    self.store.state['settings']=previous
                    raise
                settings.OVERRIDES.clear();settings.OVERRIDES.update(values)
                if self.agent:self.agent.daily_cache.clear()
            return self.send(200,b'{"saved":true}','application/json')
        except (ValueError,TypeError) as exc:
            return self.send(400,json.dumps({'error':str(exc)}).encode(),'application/json')

    def do_GET(self):
        if self.path == '/healthz':
            return self.send(200, json.dumps({'status':'up','mode':self.store.state['mode']}).encode(), 'application/json')
        if TOKEN:
            import base64
            expected = 'Basic '+base64.b64encode(('admin:'+TOKEN).encode()).decode()
            if not hmac.compare_digest(self.headers.get('Authorization',''), expected):
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Donchian"')
                self.end_headers()
                return
        if self.path == '/api/settings':
            if not self.authenticated():return self.send(401,b'Authentication required','text/plain')
            return self.send(200,json.dumps({'values':settings.current(),'csrf':self.csrf_token}).encode(),'application/json')
        if self.path == '/':
            return self.send(200, (ROOT/'dashboard.html').read_bytes(), 'text/html; charset=utf-8')
        if self.path == '/api/state':
            # Read committed snapshot through a separate SQLite connection; dashboard
            # remains responsive during network retries and monthly selection.
            filename='agent.sqlite3' if self.store.state['mode']=='PAPER' else self.store.state['mode'].lower()+'.sqlite3'
            with sqlite3.connect(DATA/filename) as db:
                row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
            return self.send(200, row[0].encode(), 'application/json')
        self.send(404, b'Not found', 'text/plain')

    def send(self, status, data, kind):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        if self.path != '/healthz':
            LOG.info(fmt, *args)


def execution_mode():
    mode=os.environ.get('MODE','paper').lower()
    if mode not in ('paper','testnet','live'):
        raise ValueError('MODE must be paper, testnet or live')
    if mode=='live' and os.environ.get('LIVE_TRADING_ACK')!='I_ACCEPT_REAL_MONEY_RISK':
        raise ValueError('Live orders disabled: explicit LIVE_TRADING_ACK is required')
    if mode!='paper' and not os.environ.get('DASHBOARD_TOKEN'):
        raise ValueError('DASHBOARD_TOKEN is required for exchange-connected modes')
    return mode


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    mode=execution_mode()
    if os.environ.get('RAILWAY_SERVICE_ID'):
        if os.environ.get('RAILWAY_VOLUME_MOUNT_PATH') != str(DATA):
            raise SystemExit('Attach a dedicated Railway volume at DATA_DIR before starting.')
        if not TOKEN or TOKEN == 'replace-with-a-long-unique-password':
            raise SystemExit('Set a unique DASHBOARD_TOKEN before starting on Railway.')
    DATA.mkdir(parents=True, exist_ok=True)
    lockfile = (DATA/'agent.lock').open('w')
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another agent already uses this DATA_DIR')
    Handler.store = Store(DATA,mode)
    if mode=='paper':
        agent=Agent(Handler.store)
    else:
        # Ensure the imported runner shares this process's shutdown event.
        sys.modules['app']=sys.modules[__name__]
        from live_agent import LiveAgent
        agent=LiveAgent(Handler.store,mode)
    Handler.agent=agent
    worker = threading.Thread(target=agent.run, daemon=True)
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT','8080'))), Handler)
    def stop(*_):
        HALT.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.start()
    LOG.info('%s agent dashboard listening on %s', mode.upper(), server.server_port)
    server.serve_forever()
    HALT.set()
    if agent.ranker and agent.ranker.poll() is None:
        agent.ranker.terminate()
    server.server_close()

if __name__ == '__main__':
    main()
