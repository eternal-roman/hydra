"""C6 (loop-back) — is the gate+flip 'risk layer' just the trend overlay?
Pure overlay strategy: long at close of first day score>=0.6, flat at close of
first day score<0.6. No bounce entries, no stops. Fees 26 bps/side.
Compare total, maxDD, n vs B&H over the same daily tape (post-warmup).
Also 1h variant (act at first 1h close after day-score flip, prev-day score).
"""
import sys
from pathlib import Path
sys.path.insert(0, r'C:\Users\elamj\AppData\Local\Temp\claude\C--Users-elamj-Dev-Hydra\ba835567-fa30-4c0a-b900-77433d84f85f\scratchpad')
HB = Path(r'C:\Users\elamj\Dev\Hydra\heartbeat')
sys.path.insert(0, str(HB/'src')); sys.path.insert(0, str(HB/'tools'))
from bounce_geometry_study import candles_from_sqlite
import importlib.util
spec = importlib.util.spec_from_file_location('c3', r'C:\Users\elamj\AppData\Local\Temp\claude\C--Users-elamj-Dev-Hydra\ba835567-fa30-4c0a-b900-77433d84f85f\scratchpad\c3_exits.py')
c3 = importlib.util.module_from_spec(spec); spec.loader.exec_module(c3)

DB = r'C:\Users\elamj\Dev\Hydra\hydra_history.sqlite'
FEE = 0.0026

for pair in ['BTC/USD','ETH/USD','ZEC/USD']:
    candles = candles_from_sqlite(DB, pair, 24)
    scores = c3.daily_scores(pair)
    # post-warmup segment only
    seg = [c for c in candles if c3.score_at(scores, c.open_ts, 24) is not None]
    eq, peak, mdd = 1.0, 1.0, 0.0
    in_pos = False; entry = None; n = 0; rets = []
    for c in seg:
        s = c3.score_at(scores, c.open_ts, 24)
        if not in_pos and s >= 0.6:
            in_pos, entry = True, c.close; n += 1
        elif in_pos and s < 0.6:
            r = c.close/entry - 1 - 2*FEE
            rets.append(r); eq *= 1+r; peak = max(peak, eq); mdd = max(mdd, 1-eq/peak)
            in_pos = False
        elif in_pos:
            # mark-to-market dd tracking
            m = eq * (c.close/entry) # gross
            peak = max(peak, m); mdd = max(mdd, 1 - m/peak)
    if in_pos:
        r = seg[-1].close/entry - 1 - 2*FEE
        rets.append(r); eq *= 1+r
    bh = seg[-1].close/seg[0].close - 1
    # B&H maxDD on same segment
    pk, bdd = seg[0].close, 0.0
    for c in seg:
        pk = max(pk, c.close); bdd = max(bdd, 1 - c.close/pk)
    wr = sum(1 for r in rets if r>0)/max(len(rets),1)
    print(f"{pair}: overlay n={n} total={100*(eq-1):.1f}% wr={wr:.2f} maxDD={100*mdd:.1f}% "
          f"| B&H total={100*bh:.1f}% maxDD={100*bdd:.1f}% | seg={len(seg)}d")
