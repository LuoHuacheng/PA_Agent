# -*- coding: utf-8 -*-
"""近 N 天账户级盈亏对账(income 原始账本口径)。"""
import sys, json, datetime
from pathlib import Path
from collections import defaultdict
sys.path.insert(0, 'tools'); sys.path.insert(0, '.')
from _pa_sim_common import fetch_account_data, make_client

SYMBOLS = ["BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT","ADAUSDT","DOGEUSDT","LINKUSDT","TRXUSDT","ZECUSDT"]
TZ = datetime.timezone(datetime.timedelta(hours=8))
DAYS = 10
now = datetime.datetime.now(TZ)
start = int((now - datetime.timedelta(days=DAYS)).timestamp() * 1000)
cache = Path('logs/_pnl_cache_recent'); cache.mkdir(exist_ok=True)
settings, client = make_client()
print('fetching since', datetime.datetime.fromtimestamp(start/1000, TZ), flush=True)
fetch_account_data(client, SYMBOLS, start, cache)
inc = json.load(open(cache/'income.json'))
inc = [x for x in inc if x['time'] >= start]
byday = defaultdict(lambda: defaultdict(float))
for x in inc:
    dk = datetime.datetime.fromtimestamp(x['time']/1000, TZ).strftime('%Y-%m-%d')
    byday[dk][x['incomeType']] += float(x['income']); byday[dk]['_n'] += 1
print()
print('=== income by day (ALL symbols, raw ledger) ===')
print('%-12s %10s %10s %10s %10s %6s' % ('date','realized','commission','net(r+c)','funding','n'))
for dk in sorted(byday):
    r = byday[dk]
    print('%-12s %+10.2f %+10.2f %+10.2f %+10.2f %6d' % (dk, r['REALIZED_PNL'], r['COMMISSION'], r['REALIZED_PNL']+r['COMMISSION'], r['FUNDING_FEE'], r['_n']))
print()
bysym = defaultdict(lambda: defaultdict(float))
for x in inc: bysym[x['symbol']][x['incomeType']] += float(x['income'])
print('=== by symbol over window ===')
tot = 0.0
for s in sorted(bysym):
    r = bysym[s]; net = r['REALIZED_PNL']+r['COMMISSION']; tot += net
    print('%-10s realized %+9.2f comm %+8.2f net %+9.2f funding %+8.3f' % (s, r['REALIZED_PNL'], r['COMMISSION'], net, r['FUNDING_FEE']))
print('TOTAL net %+.2f' % tot)
pos = json.load(open(cache/'positions.json'))
print()
print('=== open positions ===')
for p in pos:
    amt = float(p.get('positionAmt', 0))
    if abs(amt) > 0:
        print(p['symbol'], amt, 'entry', p['entryPrice'], 'upnl', p['unRealizedProfit'], 'lev', p.get('leverage'))
print()
acct = client._request('GET', '/fapi/v2/account', {}, signed=True)
print('wallet', acct.get('totalWalletBalance'), 'upnl', acct.get('totalUnrealizedProfit'), 'margin', acct.get('totalMarginBalance'))
