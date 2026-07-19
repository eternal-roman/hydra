"""C4 — avalanche mechanics: at 20-bar-low breaks (1h), does a heavy
sell-taker burst (slab release) predict faster reversal than a quiet break?
90d sided trades, BTC/ETH/SOL/ZEC vs USD.
burst z = ((sell_vol-buy_vol)/tot volume imbalance) * (tot vol / 30-bar mean vol)
split at pooled median among break bars; forward return 6h and 24h from break close.
Control: non-break bars sampled at same frequency.
"""
import sqlite3, statistics, random

DB = r'C:\Users\elamj\Dev\Hydra\hydra_history.sqlite'
random.seed(7)

def hourly(pair):
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT ts, price, qty, side FROM trades WHERE pair=? ORDER BY ts", (pair,)).fetchall()
    bars = {}
    for ts, px, qty, side in rows:
        b = int(ts)//3600*3600
        cur = bars.setdefault(b, {'o':px,'h':px,'l':px,'c':px,'bv':0.0,'sv':0.0})
        cur['h']=max(cur['h'],px); cur['l']=min(cur['l'],px); cur['c']=px
        if side=='b': cur['bv']+=qty*px
        else: cur['sv']+=qty*px
    return sorted(bars.items())

for pair in ['BTC/USD','ETH/USD','SOL/USD','ZEC/USD']:
    bars = hourly(pair)
    n = len(bars)
    lows = [b[1]['l'] for b in bars]; closes = [b[1]['c'] for b in bars]
    vols = [b[1]['bv']+b[1]['sv'] for b in bars]
    events = []
    for i in range(30, n-24):
        if lows[i] < min(lows[i-20:i]):
            v = vols[i]; mv = statistics.mean(vols[i-30:i]) or 1e-9
            imb = (bars[i][1]['sv']-bars[i][1]['bv'])/max(v,1e-9)
            burst = imb * (v/mv)
            f6 = closes[min(n-1,i+6)]/closes[i]-1
            f24 = closes[min(n-1,i+24)]/closes[i]-1
            events.append((burst, f6, f24, i))
    if not events:
        print(pair, 'no events'); continue
    med = statistics.median(e[0] for e in events)
    hi = [e for e in events if e[0] > med]; lo = [e for e in events if e[0] <= med]
    # non-break control
    evset = {e[3] for e in events}
    pool = [i for i in range(30, n-24) if i not in evset]
    ctrl = random.sample(pool, min(len(pool), len(events)))
    c6 = statistics.mean(closes[min(n-1,i+6)]/closes[i]-1 for i in ctrl)
    c24 = statistics.mean(closes[min(n-1,i+24)]/closes[i]-1 for i in ctrl)
    m = lambda xs, j: statistics.mean(x[j] for x in xs)
    print(f"{pair}: breaks={len(events)} (hours={n}) medburst={med:.2f}")
    print(f"  heavy-burst breaks (n={len(hi)}): fwd6h={100*m(hi,1):+.3f}% fwd24h={100*m(hi,2):+.3f}%")
    print(f"  quiet breaks       (n={len(lo)}): fwd6h={100*m(lo,1):+.3f}% fwd24h={100*m(lo,2):+.3f}%")
    print(f"  non-break control  (n={len(ctrl)}): fwd6h={100*c6:+.3f}% fwd24h={100*c24:+.3f}%")
