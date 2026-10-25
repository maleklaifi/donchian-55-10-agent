#!/usr/bin/env python3
"""Read-only Binance monthly USDT volume scanner. Python 3.10+, standard library only.
No API keys, no orders. Current snapshot candidate pool: NOT historical coverage proof.
"""
import argparse, csv, datetime as dt, hashlib, json, pathlib, time, urllib.parse, urllib.request, urllib.error
UTC = dt.timezone.utc
EXCLUDE = set('USDT USDC BUSD FDUSD TUSD USDP PAX DAI UST USTC USDD USDE USD1 RLUSD PYUSD EURI EUR AEUR EURC GBP AUD BRL TRY RUB UAH NGN BIDR IDRT BKRW VAI SUSD GUSD USDS USDSB PAXG XAUT'.split())
# Explicit leveraged underlyings avoids excluding legitimate names such as JUP/SYRUP.
LEVERAGED = {b+s for b in 'BTC ETH BNB XRP EOS TRX ADA LINK XTZ DOT YFI SUSHI FIL UNI LTC BCH AAVE SXP 1INCH'.split() for s in ('UP','DOWN','BULL','BEAR')}
def month_bounds(month):
    y,m = map(int, month.split('-')); start=dt.datetime(y,m,1,tzinfo=UTC)
    prev=(start-dt.timedelta(days=1)).replace(day=1)
    return prev,start

def request(base, path, params=None):
    url=base.rstrip('/')+path+('?' + urllib.parse.urlencode(params) if params else '')
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'DonchianMonthlyScanner/1.0'}),timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429,500,502,503,504): raise
            delay=max(float(e.headers.get('Retry-After', 0)),2**attempt)
            time.sleep(min(delay,60))
        except (urllib.error.URLError, TimeoutError):
            if attempt==4: raise
            time.sleep(2**attempt)
    raise RuntimeError('Request retries exhausted')

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--month',help='Selection month YYYY-MM; default current UTC month')
    ap.add_argument('--out',default='monthly_rankings')
    ap.add_argument('--base-url',default='https://data-api.binance.vision')
    ap.add_argument('--refresh',action='store_true',help='Explicitly replace a saved selection')
    ap.add_argument('--exclude',default='',help='Additional comma-separated BASE assets to exclude')
    args=ap.parse_args()
    now=dt.datetime.now(UTC);month=args.month or now.strftime('%Y-%m')
    prev,start=month_bounds(month)
    if start>now: ap.error('Future selection month: preceding month is incomplete')
    out=pathlib.Path(args.out)/month
    if (out/'selection.json').exists() and not args.refresh:
        saved=json.loads((out/'selection.json').read_text());print('Saved selection (not refreshed):', ', '.join(x['symbol'] for x in saved['top5']));return
    out.mkdir(parents=True,exist_ok=True)
    info=request(args.base_url,'/api/v3/exchangeInfo')
    (out/'exchange_snapshot.json').write_text(json.dumps(info))
    excludes=EXCLUDE|set(args.exclude.upper().split(','))
    candidates=[s for s in info['symbols'] if s['quoteAsset']=='USDT' and s.get('isSpotTradingAllowed',False) and s['status']=='TRADING' and s['baseAsset'] not in excludes and s['baseAsset'] not in LEVERAGED]
    rows=[];errors=[];audit=[]
    for i,s in enumerate(candidates,1):
        sym=s['symbol']
        try:
            # Monthly history from inception: one request provides both volume and earliest month.
            bars=request(args.base_url,'/api/v3/klines',{'symbol':sym,'interval':'1M','startTime':0,'endTime':int(start.timestamp()*1000)-1,'limit':1000})
            if not bars: audit.append({'symbol':sym,'status':'no_history'});continue
            # Monthly first timestamp is month start, not exact listing date. Query first DAILY bar.
            first=request(args.base_url,'/api/v3/klines',{'symbol':sym,'interval':'1d','startTime':0,'limit':1})
            if not first: raise ValueError('No first daily candle')
            age=(start-dt.datetime.fromtimestamp(first[0][0]/1000,UTC)).days
            prior=[b for b in bars if int(b[0])==int(prev.timestamp()*1000)]
            if age<60 or not prior:
                audit.append({'symbol':sym,'status':'too_young_or_no_prior_month','age_days':age});continue
            qv=float(prior[0][7])
            if qv<=0: audit.append({'symbol':sym,'status':'zero_volume'});continue
            rows.append({'symbol':sym,'prior_month':prev.strftime('%Y-%m'),'quote_volume_usdt':qv,'age_days':age})
            audit.append({'symbol':sym,'status':'ranked','age_days':age})
        except Exception as e: errors.append({'symbol':sym,'error':str(e)})
        finally: time.sleep(0.15)
        if i%50==0: print(f'Checked {i}/{len(candidates)}',flush=True)
    rows.sort(key=lambda r:(-r['quote_volume_usdt'],r['symbol']))
    for rank,row in enumerate(rows,1):row['rank']=rank
    (out/'coverage_audit.json').write_text(json.dumps({'audit':audit,'errors':errors},indent=2))
    if errors: raise SystemExit(f'{len(errors)} symbols failed. No selection published; see {out}/coverage_audit.json. Rerun after resolving errors.')
    if len(rows)<5: raise SystemExit('Fewer than 5 eligible symbols; no selection published')
    with (out/'rankings.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['rank','symbol','prior_month','quote_volume_usdt','age_days']);w.writeheader();w.writerows(rows)
    result={'selection_month':month,'generated_at':now.isoformat(),'source':args.base_url,'candidate_policy':'current TRADING spot USDT snapshot; not survivorship-free historical rankings','listing_age_days':60,'excluded_bases':sorted(excludes-{''}),'excluded_leveraged_bases':sorted(LEVERAGED),'snapshot_sha256':hashlib.sha256((out/'exchange_snapshot.json').read_bytes()).hexdigest(),'candidate_count':len(candidates),'ranked_count':len(rows),'top5':rows[:5]}
    (out/'selection.json').write_text(json.dumps(result,indent=2))
    (out/'tradingview_watchlist.txt').write_text(','.join('BINANCE:'+r['symbol'] for r in rows[:5])+'\n')
    print('\nTOP FIVE FOR',month,'(previous month volume)')
    for r in rows[:5]:print(f"{r['rank']}. {r['symbol']:14} {r['quote_volume_usdt']:,.0f} USDT")
    print('Saved:',out)
    if month!=now.strftime('%Y-%m'):print('WARNING: past month uses current candidates, not historical exchange membership.')
if __name__=='__main__':main()
