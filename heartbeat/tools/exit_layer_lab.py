"""C3/C5-C12 — exit-layer laboratory: identical entries (harness causal_setups,
b1 = entry at close of bounce+1, resolved setups skipped), risk layer varies.

Arms:
  A0_stop_tgt      stop@L0 touch + tgt3.3 + horizon        (study baseline)
  A1_stop_m{X}     stop@L0-X*ATR + tgt3.3 + horizon        (deductible ladder)
  A2_closeconfirm  stop on CLOSE<L0 (exit at that close) + tgt3.3 + horizon
  A3_nostop_tgt    tgt3.3 + horizon only (no price stop)
  A4_flip          NO stop, NO target: exit at close when daily ensemble<0.6
  A5_gate_flip     A4 + enter only when ensemble>=0.6 (quarantine/abstention)
  A6_gate_stop     A0 + enter only when ensemble>=0.6 (gate with stops kept)
  A7_partial       50% at L0 touch, 50% at L0-2ATR / target / horizon
  A8_taper         A4 but exit 1/3 at flip close + next 2 closes
  A9_noexit        hold to horizon close (200 bars), nothing else
Placebo (RCT) base: entry every 10th bar, exits: stop1.5ATR+h20 / h20 / flip.

Metrics: n, avg%, total compounded %, wr, maxloss, p5, avg hold, equity maxDD.
Ensemble: 0.4*(close>sma200)+0.4*(ema20>ema100)+0.2*don(55 entry/20 exit,
close-based state machine), long >= 0.6. Daily bars use own close
(mark-to-close, engine semantics); 1h bars use last completed UTC day.
"""
import sys, json
from pathlib import Path
HB = Path(r'C:\Users\elamj\Dev\Hydra\heartbeat')
sys.path.insert(0, str(HB/'src')); sys.path.insert(0, str(HB/'tools'))
import paper_bounce_sim as sim
from bounce_geometry_study import candles_from_sqlite
from heartbeat.config import load_config

DB = r'C:\Users\elamj\Dev\Hydra\hydra_history.sqlite'
FEE = 0.0026
sim.FEE = FEE
TGT = sim.TARGET_ATR
HORIZON = sim.HORIZON

def daily_scores(candles_1h_pair):
    """ensemble score per completed UTC day, from 1h source rows."""
    import sqlite3
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT ts, close FROM ohlc WHERE pair=? AND grain_sec=3600 ORDER BY ts",(candles_1h_pair,)).fetchall()
    dayc = {}
    for ts, c in rows:
        dayc[int(ts)//86400] = c   # last close of the day wins
    days = sorted(dayc)
    closes = []
    don = 0
    scores = {}
    ema20 = ema100 = None
    k20, k100 = 2/21, 2/101
    for d in days:
        c = dayc[d]
        # advance donchian with PREVIOUS completed close before appending
        if closes:
            prev = closes[-1]
            prior = closes[:-1]
            if len(prior) >= 55:
                if don == 0 and prev > max(prior[-55:]): don = 1
                elif don == 1 and prev < min(prior[-20:]): don = 0
        closes.append(c)
        ema20 = c if ema20 is None else c*k20 + ema20*(1-k20)
        ema100 = c if ema100 is None else c*k100 + ema100*(1-k100)
        if len(closes) >= 210:
            sma = sum(closes[-200:])/200.0
            s = (0.4 if c > sma else 0.0) + (0.4 if ema20 > ema100 else 0.0) + (0.2 if don==1 else 0.0)
            scores[d] = s
    return scores

def score_at(scores, ts, bar_hours):
    d = int(ts)//86400
    if bar_hours >= 24:
        return scores.get(d)           # own (mark-to-close) day
    return scores.get(d-1)             # last completed day

def run_arm(candles, setups, arm, scores, bar_hours, gate=False, entries='bounce'):
    trades = []
    in_pos_until = -1
    n_bars = len(candles)
    if entries == 'bounce':
        ent = []
        for s in setups:
            e = sim.entry_index(candles, s, 1)
            if e is not None: ent.append((e, s))
    else:  # placebo every 10th bar with ATR proxy
        from c1_atr import atr_series  # not used; compute inline below
        ent = None
    for e, s in ent:
        if e <= in_pos_until: continue
        sc = score_at(scores, candles[e].open_ts, bar_hours)
        if gate and (sc is None or sc < 0.6): continue
        L0, a = s['low_px'], s['atr']
        entry_px = candles[e].close
        tgt_px = L0 + TGT*a
        legs = []  # (fraction, exit_px, k)
        rem = 1.0
        half_done = False
        taper_left = 0
        k_exit = None
        for k in range(e+1, n_bars):
            c = candles[k]
            scb = score_at(scores, c.open_ts, bar_hours)
            flip = (scb is not None and scb < 0.6)
            if arm == 'A0':
                if c.low < L0: legs.append((1.0, min(c.close, L0), k)); break
                if c.high >= tgt_px: legs.append((1.0, tgt_px, k)); break
            elif arm.startswith('A1_'):
                m = float(arm.split('_')[1])
                lvl = L0 - m*a
                if c.low < lvl: legs.append((1.0, min(c.close, lvl), k)); break
                if c.high >= tgt_px: legs.append((1.0, tgt_px, k)); break
            elif arm == 'A2':
                if c.close < L0: legs.append((1.0, c.close, k)); break
                if c.high >= tgt_px: legs.append((1.0, tgt_px, k)); break
            elif arm == 'A3':
                if c.high >= tgt_px: legs.append((1.0, tgt_px, k)); break
            elif arm in ('A4','A5'):
                if flip: legs.append((1.0, c.close, k)); break
            elif arm == 'A6':
                if c.low < L0: legs.append((1.0, min(c.close, L0), k)); break
                if c.high >= tgt_px: legs.append((1.0, tgt_px, k)); break
            elif arm == 'A7':
                if not half_done and c.low < L0:
                    legs.append((0.5, min(c.close, L0), k)); rem = 0.5; half_done = True
                lvl2 = L0 - 2*a
                if c.low < lvl2 and rem > 0:
                    legs.append((rem, min(c.close, lvl2), k)); rem = 0; break
                if c.high >= tgt_px and rem > 0:
                    legs.append((rem, tgt_px, k)); rem = 0; break
            elif arm == 'A8':
                if flip and taper_left == 0: taper_left = 3
                if taper_left > 0:
                    f = min(rem, 1/3)
                    legs.append((f, c.close, k)); rem -= f; taper_left -= 1
                    if rem <= 1e-9: break
            elif arm == 'A9':
                pass
            if k - s['low_idx'] > HORIZON and rem > 0:
                legs.append((rem, c.close, k)); rem = 0; break
        if not legs or sum(f for f,_,_ in legs) < 0.999:
            r = 1.0 - sum(f for f,_,_ in legs)
            if r > 1e-9: legs.append((r, candles[-1].close, n_bars-1))
        k_exit = max(k for _,_,k in legs)
        ret = sum(f * (px/entry_px - 1.0 - 2*FEE) for f, px, _ in legs)
        # adverse excursion while held
        mae = min((candles[k].low for k in range(e+1, k_exit+1)), default=entry_px)/entry_px - 1.0
        trades.append({'ret': ret, 'hold': k_exit - e, 'mae': mae, 'e': e, 'ts': candles[e].open_ts})
        in_pos_until = k_exit
    return trades

def metrics(trades):
    if not trades: return {'n': 0}
    rets = [t['ret'] for t in trades]
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1+r; peak = max(peak, eq); mdd = max(mdd, 1-eq/peak)
    sr = sorted(rets)
    return {'n': len(rets), 'avg%': round(100*sum(rets)/len(rets),3),
            'total%': round(100*(eq-1),1), 'wr': round(sum(1 for r in rets if r>0)/len(rets),2),
            'maxloss%': round(100*sr[0],1), 'p5%': round(100*sr[max(0,int(0.05*len(sr))-1)],1),
            'hold': round(sum(t['hold'] for t in trades)/len(trades),1),
            'mae%': round(100*sum(t['mae'] for t in trades)/len(trades),2),
            'eqMaxDD%': round(100*mdd,1)}

def placebo(candles, scores, bar_hours):
    """entries every 10th bar; ATR = mean TR 14."""
    n = len(candles)
    tr = [0.0]*n
    for i in range(1, n):
        c, p = candles[i], candles[i-1]
        tr[i] = max(c.high-c.low, abs(c.high-p.close), abs(c.low-p.close))
    out = {}
    for arm in ('stop1.5_h20','h20','flip','gate_flip'):
        trades = []
        in_pos_until = -1
        for e in range(220 if bar_hours>=24 else 24*220, n-1, 10):
            if e <= in_pos_until or e < 15: continue
            a = sum(tr[e-14:e])/14
            if a <= 0: continue
            sc = score_at(scores, candles[e].open_ts, bar_hours)
            if arm == 'gate_flip' and (sc is None or sc < 0.6): continue
            entry_px = candles[e].close
            lvl = entry_px - 1.5*a
            exit_px = None; k_exit = None
            for k in range(e+1, min(n, e+1+ (200 if 'flip' in arm else 20))):
                c = candles[k]
                if arm == 'stop1.5_h20' and c.low < lvl:
                    exit_px, k_exit = min(c.close, lvl), k; break
                if 'flip' in arm:
                    scb = score_at(scores, c.open_ts, bar_hours)
                    if scb is not None and scb < 0.6:
                        exit_px, k_exit = c.close, k; break
            if exit_px is None:
                k_exit = min(n-1, e + (200 if 'flip' in arm else 20))
                exit_px = candles[k_exit].close
            trades.append({'ret': exit_px/entry_px - 1 - 2*FEE, 'hold': k_exit-e,
                           'mae': min((candles[k].low for k in range(e+1,k_exit+1)), default=entry_px)/entry_px-1,
                           'e': e, 'ts': candles[e].open_ts})
            in_pos_until = k_exit
        out[arm] = metrics(trades)
    return out

def main():
    cfg = load_config(None)
    ARMS = ['A0','A1_0.5','A1_1','A1_2','A1_4','A2','A3','A4','A5','A6','A7','A8','A9']
    result = {}
    jobs = [('BTC/USD',24),('ETH/USD',24),('ZEC/USD',24),('BTC/USD',1)]
    for pair, bh in jobs:
        candles = candles_from_sqlite(DB, pair, bh)
        scores = daily_scores(pair)
        setups = sim.causal_setups(candles, cfg)
        key = f'{pair}@{bh}h'
        result[key] = {'n_setups': len(setups)}
        print(f'\n==== {key}: {len(candles)} candles, {len(setups)} setups')
        for arm in ARMS:
            gate = arm in ('A5','A6')
            base = 'A4' if arm=='A5' else ('A0' if arm=='A6' else arm)
            tr = run_arm(candles, setups, base, scores, bh, gate=gate)
            m = metrics(tr)
            result[key][arm] = m
            print(f'  {arm:10s} {m}')
        result[key]['placebo'] = placebo(candles, scores, bh)
        print('  placebo:')
        for k, v in result[key]['placebo'].items():
            print(f'    {k:12s} {v}')
    out = Path(sys.argv[1]); out.write_text(json.dumps(result, indent=1))

if __name__ == '__main__':
    main()
