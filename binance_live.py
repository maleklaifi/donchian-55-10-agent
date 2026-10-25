"""Binance Spot execution primitives. Never retries a POST after unknown outcome."""
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import hashlib
import hmac
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

D = lambda x: Decimal(str(x))
TERMINAL = {'FILLED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'}


class ExchangeError(RuntimeError):
    def __init__(self, code, status=0):
        self.code = code
        super().__init__(f'Binance request failed (HTTP {status}, code {code}); inspect account/order status')


class UncertainOrder(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, mode):
        self.base = 'https://api.binance.com' if mode == 'live' else 'https://testnet.binance.vision'
        self.key = os.environ.get('BINANCE_API_KEY', '')
        self.secret = os.environ.get('BINANCE_API_SECRET', '')
        if not self.key or not self.secret:
            raise ValueError('BINANCE_API_KEY and BINANCE_API_SECRET are required')
        self.offset = 0
        self.cooldown = 0

    def call(self, method, path, params=None, signed=True):
        if time.monotonic() < self.cooldown:
            raise RuntimeError('Binance rate-limit cooldown active')
        if path not in {'time','exchangeInfo','ticker/price','avgPrice','account','openOrders','order','myTrades'}:
            raise ValueError('Endpoint not allowed by this Spot-only adapter')
        data = dict(params or {})
        if signed:
            data.update(timestamp=int(time.time()*1000)+self.offset, recvWindow=5000)
        query = urllib.parse.urlencode(data)
        if signed:
            query += '&signature='+hmac.new(self.secret.encode(),query.encode(),hashlib.sha256).hexdigest()
        url = self.base+'/api/v3/'+path
        headers = {'X-MBX-APIKEY': self.key} if signed else {}
        body = None
        if method == 'POST':
            body = query.encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif query:
            url += '?'+query
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=10) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            try:code = json.loads(exc.read()).get('code', 0)
            except (ValueError, OSError):code = 0
            if exc.code in (418,429):
                try:delay = float(exc.headers.get('Retry-After', '60'))
                except ValueError:delay = 60
                self.cooldown = time.monotonic()+max(1,delay)
            raise ExchangeError(code, exc.code) from None
        except (OSError, ValueError):
            # Do not leak signed request URLs, request bodies or credentials in logs.
            raise UncertainOrder('Transport/response failure; reconcile before another order') from None

    def sync_time(self):
        before = int(time.time()*1000)
        server = int(self.call('GET','time',signed=False)['serverTime'])
        after = int(time.time()*1000)
        self.offset = server-(before+after)//2
        return server

    def query(self, symbol, client_id):
        try:
            return self.call('GET','order',dict(symbol=symbol,origClientOrderId=client_id))
        except ExchangeError as exc:
            if exc.code == -2013:return None
            raise


class Filters:
    def __init__(self, info):
        if info.get('status') != 'TRADING' or info.get('quoteAsset') != 'USDT' or not info.get('isSpotTradingAllowed'):
            raise ValueError('Symbol is not active USDT Spot')
        if not {'MARKET','STOP_LOSS'}.issubset(set(info['orderTypes'])):
            raise ValueError('Symbol must support native STOP_LOSS market orders')
        if not info.get('quoteOrderQtyMarketAllowed',False):
            raise ValueError('Symbol must support quoteOrderQty market entries')
        self.info = info
        self.filters = {f['filterType']:f for f in info['filters']}
        self.tick = D(self.filters['PRICE_FILTER']['tickSize'])
        steps = [D(self.filters[k]['stepSize']) for k in ('LOT_SIZE','MARKET_LOT_SIZE') if k in self.filters and D(self.filters[k]['stepSize'])>0]
        scale = max(-v.as_tuple().exponent for v in steps)
        multiple = 1
        for v in steps:multiple = math.lcm(multiple,int(v*10**scale))
        self.step = D(multiple)/D(10**scale)

    def quantity(self, qty):
        return (D(qty)/self.step).to_integral_value(rounding=ROUND_DOWN)*self.step

    def stop(self, price):
        if self.tick <= 0:raise ValueError('Invalid exchange price tick')
        result = (D(price)/self.tick).to_integral_value(rounding=ROUND_UP)*self.tick
        f = self.filters['PRICE_FILTER']
        if result <= 0 or (D(f['minPrice'])>0 and result<D(f['minPrice'])) or (D(f['maxPrice'])>0 and result>D(f['maxPrice'])):
            raise ValueError('Stop violates PRICE_FILTER')
        return result

    def validate(self, qty, price):
        qty,price = D(qty),D(price)
        if qty<=0 or price<=0 or not qty.is_finite() or not price.is_finite():
            raise ValueError('Invalid quantity/price')
        for name in ('LOT_SIZE','MARKET_LOT_SIZE'):
            f = self.filters.get(name)
            if not f:continue
            if qty<D(f['minQty']) or (D(f['maxQty'])>0 and qty>D(f['maxQty'])):
                raise ValueError('Quantity outside exchange limits')
            if D(f['stepSize'])>0 and qty%D(f['stepSize']) != 0:
                raise ValueError('Quantity does not match lot step')
        for name in ('MIN_NOTIONAL','NOTIONAL'):
            f = self.filters.get(name)
            if not f:continue
            # Conservative: enforce notional limits even if market exemption exists.
            if qty*price < D(f['minNotional']):raise ValueError('Below exchange minimum notional')
            if name=='NOTIONAL' and D(f['maxNotional'])>0 and qty*price>D(f['maxNotional']):
                raise ValueError('Above exchange maximum notional')


def number(value):
    return format(D(value), 'f')


class Journal:
    def __init__(self, client, save):
        self.client,self.save = client,save

    def intent(self, symbol, side, kind, **params):
        return dict(symbol=symbol,side=side,type=kind,newClientOrderId='dc5510_'+uuid.uuid4().hex[:26],
                    params={k:number(v) for k,v in params.items()},attempted=False)

    def order(self, item):
        if item.get('rejected'):
            return dict(status='REJECTED',executedQty='0',cummulativeQuoteQty='0')
        if not item['attempted']:
            item['attempted']=True
            self.save()  # Durable intent BEFORE send, including client ID.
            try:
                result=self.client.call('POST','order',dict(symbol=item['symbol'],side=item['side'],
                    type=item['type'],newClientOrderId=item['newClientOrderId'],newOrderRespType='FULL',**item['params']))
            except ExchangeError as exc:
                result=self.client.query(item['symbol'],item['newClientOrderId'])
                if result is None and (exc.code in {-1013,-1111,-1121,-2010} or -1136<=exc.code<=-1100):
                    item['rejected']=True;self.save()
                    return dict(status='REJECTED',executedQty='0',cummulativeQuoteQty='0')
                if result is None:raise UncertainOrder('Order outcome unknown; no automatic resubmission') from None
            except UncertainOrder:
                result=self.client.query(item['symbol'],item['newClientOrderId'])
                if result is None:raise UncertainOrder('Order outcome unknown; no automatic resubmission') from None
        else:
            result=self.client.query(item['symbol'],item['newClientOrderId'])
            if result is None:
                raise UncertainOrder('Journaled order not found; entries paused. Reconcile in Binance; never delete journal.')
        item['snapshot']=result
        self.save()
        return result

    def cancel(self, item):
        order=self.order(item)
        if order['status'] in TERMINAL:return order
        try:
            self.client.call('DELETE','order',dict(symbol=item['symbol'],origClientOrderId=item['newClientOrderId']))
        except (ExchangeError,UncertainOrder):
            pass  # Never sell until the subsequent query proves cancellation/fill.
        return self.order(item)

    def fills(self, item, order):
        if D(order['executedQty'])==0:return []
        records=[];params=dict(symbol=item['symbol'],orderId=order['orderId'],limit=1000)
        for _ in range(20):
            batch=self.client.call('GET','myTrades',params)
            records.extend(batch)
            if len(batch)<1000:break
            params['fromId']=int(batch[-1]['id'])+1
        if len({r['id'] for r in records})!=len(records) or any(r['orderId']!=order['orderId'] for r in records):
            raise RuntimeError('Inconsistent fill records; reconcile account')
        if sum((D(r['qty']) for r in records),D(0))!=D(order['executedQty']):
            raise RuntimeError('Fill history incomplete; retry reconciliation')
        return records
