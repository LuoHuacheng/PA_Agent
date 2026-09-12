# -*- coding: utf-8 -*-
"""扫描 pending 记录: 可回放样本池规模与 K 线覆盖。"""
import json, glob, datetime, collections, os
TZ = datetime.timezone(datetime.timedelta(hours=8))
orders = collections.Counter()
per_sym = collections.defaultdict(lambda: {"n": 0, "orders": collections.Counter(), "bars": {}})
files = sorted(glob.glob('records/pending/*.json'))
print('pending 文件数', len(files), flush=True)
for i, f in enumerate(files):
    base = os.path.basename(f)
    parts = base.split('_')
    sym = parts[2] if len(parts) > 2 else '?'
    try:
        d = json.load(open(f))
    except Exception:
        continue
    dec = (d.get('stage2_decision') or {}).get('decision') or {}
    ot = dec.get('order_type') or 'missing'
    orders[ot] += 1
    e = per_sym[sym]
    e['n'] += 1
    e['orders'][ot] += 1
    for b in (d.get('kline_data') or []):
        ts = b.get('ts_open')
        if ts:
            e['bars'][ts] = None
    if i % 500 == 0:
        print('  ...', i, flush=True)
print()
print('=== order_type 全局 ===', dict(orders))
print()
print('%-10s %6s %6s %6s %6s %8s %s' % ('symbol','recs','限价单','不下单','others','bars','coverage'))
tot_plan = 0
for sym in sorted(per_sym):
    e = per_sym[sym]
    ks = sorted(e['bars'])
    cov = ''
    if ks:
        cov = (datetime.datetime.fromtimestamp(ks[0]/1000,TZ).strftime('%m-%d %H:%M') + '~' +
               datetime.datetime.fromtimestamp(ks[-1]/1000,TZ).strftime('%m-%d %H:%M'))
    plan = e['orders'].get('限价单',0) + e['orders'].get('突破单',0)
    tot_plan += plan
    others = e['n'] - e['orders'].get('限价单',0) - e['orders'].get('不下单',0)
    print('%-10s %6d %6d %6d %6d %8d %s' % (sym, e['n'], e['orders'].get('限价单',0),
          e['orders'].get('不下单',0), others, len(ks), cov))
print()
print('可回放计划单总量(限价单+突破单):', tot_plan)
