"""C1 — stop-hunt signature: pierce-then-recover rate at swing-low-anchored
stop levels vs a random-anchor control, as a function of stop distance (ATR).

Anchored: confirmed swing lows (local min vs SW=2 bars each side).
Control:  3x random bars (seeded), anchor = that bar's low (no swing pattern).
For each anchor with ATR a (mean TR over 14 prior bars): level = low - d*a.
Scan forward from confirm bar (i+SW) up to 200 bars for first bar whose
low <= level ("pierce"). Recovered = any CLOSE > anchor low within 5 bars
of the pierce bar (inclusive). Continued = close at pierce+5 still < level.
"""
import sqlite3, random, sys, json

DB = 'hydra_history.sqlite'
SW = 2
ATR_P = 14
SCAN = 200
RECOV_K = 5
DISTS = [0.05, 0.25, 0.5, 1.0, 2.0]

def load(pair, bar_hours):
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT ts,open,high,low,close FROM ohlc WHERE pair=? AND grain_sec=3600 ORDER BY ts", (pair,)).fetchall()
    grain = 3600*bar_hours
    agg = {}
    for ts,o,h,l,c in rows:
        b = int(ts)//grain*grain
        cur = agg.get(b)
        if cur is None: agg[b]=[o,h,l,c]
        else:
            cur[1]=max(cur[1],h); cur[2]=min(cur[2],l); cur[3]=c
    out = sorted(agg.items())
    return [(b,*v) for b,v in out]

def atr_series(cs):
    n=len(cs); atr=[None]*n
    trs=[None]*n
    for i in range(1,n):
        _,o,h,l,c = cs[i]; pc = cs[i-1][4]
        trs[i]=max(h-l, abs(h-pc), abs(l-pc))
    s=0.0
    for i in range(1,n):
        s+=trs[i]
        if i>=ATR_P+1: s-=trs[i-ATR_P]
        if i>=ATR_P: atr[i]=s/ATR_P
    return atr

def swing_lows(cs):
    lows=[c[3] for c in cs]; out=[]
    for i in range(SW, len(cs)-SW):
        if all(lows[i] < lows[i-k] for k in range(1,SW+1)) and all(lows[i] < lows[i+k] for k in range(1,SW+1)):
            out.append(i)
    return out

def study(cs, anchors, atr):
    lows=[c[3] for c in cs]; closes=[c[4] for c in cs]
    res={d:{'n_anchor':0,'pierce':0,'recover':0,'continue':0} for d in DISTS}
    for i in anchors:
        a=atr[i]
        if a is None or a<=0: continue
        L=lows[i]
        start=i+SW+1
        for d in DISTS:
            lvl=L-d*a
            r=res[d]; r['n_anchor']+=1
            pj=None
            for j in range(start, min(len(cs), start+SCAN)):
                if lows[j]<=lvl: pj=j; break
            if pj is None: continue
            r['pierce']+=1
            end=min(len(cs), pj+RECOV_K+1)
            if any(closes[k]>L for k in range(pj,end)): r['recover']+=1
            k5=min(len(cs)-1, pj+RECOV_K)
            if closes[k5]<lvl: r['continue']+=1
    return res

def main():
    random.seed(42)
    out={}
    for pair,bh in [('BTC/USD',1),('ETH/USD',1),('ZEC/USD',1),('SOL/USD',1),
                    ('BTC/USD',24),('ETH/USD',24),('ZEC/USD',24)]:
        cs=load(pair,bh)
        atr=atr_series(cs)
        sw=swing_lows(cs)
        swset=set(sw)
        pool=[i for i in range(ATR_P+1, len(cs)-SW-1) if i not in swset]
        ctrl=random.sample(pool, min(len(pool), 3*len(sw)))
        key=f"{pair}@{bh}h"
        out[key]={'n_candles':len(cs),'n_swings':len(sw),
                  'anchored':study(cs,sw,atr),'control':study(cs,ctrl,atr)}
        print(f"== {key} candles={len(cs)} swings={len(sw)} ctrl={len(ctrl)}")
        for d in DISTS:
            a=out[key]['anchored'][d]; c=out[key]['control'][d]
            pa=a['pierce']/max(a['n_anchor'],1); pc=c['pierce']/max(c['n_anchor'],1)
            ra=a['recover']/max(a['pierce'],1); rc=c['recover']/max(c['pierce'],1)
            ca=a['continue']/max(a['pierce'],1); cc=c['continue']/max(c['pierce'],1)
            print(f"  d={d:4.2f}ATR anchored: pierce={pa:.3f} recov|pierce={ra:.3f} cont|pierce={ca:.3f} (n={a['pierce']:5d}) | control: pierce={pc:.3f} recov={rc:.3f} cont={cc:.3f} (n={c['pierce']:5d})")
    json.dump(out, open(sys.argv[1],'w'), indent=1, default=str)

if __name__=='__main__':
    main()
