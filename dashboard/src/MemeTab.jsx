import { useState, useEffect, useRef, useCallback } from "react";

const C = {
  bg: "#09090b",
  panel: "#18181b",
  border: "#27272a",
  accent: "#10b981",
  purple: "#8b5cf6",
  warn: "#f59e0b",
  danger: "#ef4444",
  text: "#f4f4f5",
  muted: "#71717a",
  blue: "#3b82f6",
  mono: "JetBrains Mono, monospace",
  sans: "Space Grotesk, system-ui, sans-serif",
};

const APEX_WS_BASE = 8770;
const APEX_DAILY_CAP_USD = 30;

const SEED_PAIRS = [
  "NIGHT/USD", "AAVE/USD", "AAVE/BTC",
];

const PAIR_PROFILES = {
  "NIGHT/USD": { atr: 0.3, volMult: 2.0, obi: 0.12, rsiLo: 30, rsiHi: 72,
                 ext: 8, wall: 500, profitMom: 2.5, stopMom: -1.5,
                 profitBounce: 1.8, stopBounce: -1.2, timeStopMom: 12, timeStopBounce: 10,
                 trailActivate: 1.0, trailOffset: 0.6 },
  "AAVE/USD": { atr: 0.2, volMult: 1.5, obi: 0.08, rsiLo: 32, rsiHi: 70,
                ext: 6, wall: 2000, profitMom: 1.5, stopMom: -1.2,
                profitBounce: 1.2, stopBounce: -1.0, timeStopMom: 14, timeStopBounce: 12,
                trailActivate: 0.6, trailOffset: 0.4 },
  "AAVE/BTC": { atr: 0.1, volMult: 1.8, obi: 0.10, rsiLo: 32, rsiHi: 68,
                ext: 5, wall: 1500, profitMom: 1.2, stopMom: -1.0,
                profitBounce: 1.0, stopBounce: -0.8, timeStopMom: 16, timeStopBounce: 14,
                trailActivate: 0.5, trailOffset: 0.35 },
};
const DEFAULT_PAIR_PROFILE = { atr: 0.3, volMult: 1.5, obi: 0.20, rsiLo: 35, rsiHi: 72,
                               ext: 8, wall: 500, profitMom: 2.0, stopMom: -1.2,
                               profitBounce: 1.5, stopBounce: -1.0, timeStopMom: 12, timeStopBounce: 10,
                               trailActivate: 0.8, trailOffset: 0.5 };
function pairProfile(pair) { return PAIR_PROFILES[pair] || DEFAULT_PAIR_PROFILE; }

// ─── Candle Chart (hero element) ─────────────────────────────────────────────

const CHART_FALLBACK_W = 600;

function useContainerWidth(fallback) {
  const ref = useRef(null);
  const [w, setW] = useState(fallback);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.round(e.contentRect.width)));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

const EXIT_REASON_LABELS = {
  hard_stop: { label: "STOP", color: C.danger },
  trailing_stop: { label: "TRAIL", color: C.warn },
  profit_target: { label: "TARGET", color: C.accent },
  time_stop: { label: "TIME", color: C.muted },
  volume_death: { label: "VOL", color: C.muted },
  stale_profit: { label: "STALE", color: C.warn },
  end_of_data: { label: "EOD", color: C.muted },
  test_fire: { label: "TEST", color: C.blue },
};

// Boolean entry gates (btc_risk_off is inverted: true = failing)
const GATE_KEYS = [
  "macro_trend", "vol_regime", "trend_aligned", "not_bleeding",
  "volume_spike", "obi", "rsi_window", "vwap_align", "not_extended", "ask_wall_clear",
];
const TOTAL_GATES = GATE_KEYS.length + 1; // +1 for btc_risk_off (inverted)

function countPassingGates(g) {
  if (!g) return 0;
  let passing = GATE_KEYS.filter(k => g[k] === true).length;
  if (g.btc_risk_off === false) passing += 1;
  return passing;
}

function findBarIndex(bars, ts, maxDist = 3600) {
  if (!ts || !bars || bars.length === 0) return -1;
  let best = -1, bestDist = Infinity;
  for (let i = 0; i < bars.length; i++) {
    const d = Math.abs(bars[i].ts - ts);
    if (d < bestDist) { bestDist = d; best = i; }
  }
  return bestDist <= maxDist ? best : -1;
}

function CandleChart({ bars, height = 160, trades, position, pair }) {
  const [containerRef, cw] = useContainerWidth(CHART_FALLBACK_W);

  if (!bars || bars.length === 0) {
    return (
      <div ref={containerRef} style={{ width: "100%", height, display: "flex",
                    alignItems: "center", justifyContent: "center" }}>
        <span style={{ fontFamily: C.mono, fontSize: 11, color: C.muted }}>
          Loading candle history…
        </span>
      </div>
    );
  }
  const priceGutter = 54;
  const pad = { top: 18, bottom: 18, left: 6, right: priceGutter };
  const innerW = cw - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const n = bars.length;
  const slotW = innerW / n;
  const candleW = Math.max(4, Math.min(slotW * 0.78, 20));

  // Compute price range — extend to include position levels if open
  let minP = Math.min(...bars.map(b => b.low));
  let maxP = Math.max(...bars.map(b => b.high));
  if (position && position.entry_price > 0) {
    const pp = pairProfile(pair);
    const isBounce = (position.entry_mode ?? "momentum") === "bounce";
    const stopPct = isBounce ? pp.stopBounce : pp.stopMom;
    const targetPct = isBounce ? pp.profitBounce : pp.profitMom;
    const stopPrice = position.entry_price * (1 + stopPct / 100);
    const targetPrice = position.entry_price * (1 + targetPct / 100);
    minP = Math.min(minP, stopPrice);
    maxP = Math.max(maxP, targetPrice);
  }
  const range = maxP - minP || minP * 0.01 || 1;

  function py(p) { return pad.top + innerH - ((p - minP) / range) * innerH; }

  // Match trades to bar indices
  const tradeMarkers = [];
  if (trades && trades.length > 0 && bars.length > 0) {
    trades.forEach((t, ti) => {
      const entryIdx = findBarIndex(bars, t.entry_ts);
      const exitIdx = findBarIndex(bars, t.exit_ts);
      if (entryIdx >= 0) {
        tradeMarkers.push({ type: "entry", barIdx: entryIdx, price: t.entry_price, trade: t, ti });
      }
      if (exitIdx >= 0) {
        tradeMarkers.push({ type: "exit", barIdx: exitIdx, price: t.exit_price, trade: t, ti });
      }
    });
  }

  // Position level lines
  let posLevels = null;
  if (position && position.entry_price > 0) {
    const pp = pairProfile(pair);
    const isBounce = (position.entry_mode ?? "momentum") === "bounce";
    const stopPct = isBounce ? pp.stopBounce : pp.stopMom;
    const targetPct = isBounce ? pp.profitBounce : pp.profitMom;
    const peakPct = position.peak_price > 0
      ? ((position.peak_price - position.entry_price) / position.entry_price) * 100 : 0;
    const trailActive = peakPct >= pp.trailActivate;
    posLevels = {
      entry: position.entry_price,
      stop: position.entry_price * (1 + stopPct / 100),
      target: position.entry_price * (1 + targetPct / 100),
      trail: trailActive ? position.peak_price * (1 - pp.trailOffset / 100) : null,
    };
  }

  const gridSteps = [0, 0.25, 0.5, 0.75, 1];
  return (
    <div ref={containerRef} style={{ width: "100%" }}>
    <svg viewBox={`0 0 ${cw} ${height}`} width={cw} height={height}
         style={{ display: "block" }}>
      {gridSteps.map(f => {
        const y = pad.top + innerH * (1 - f);
        const price = minP + range * f;
        return (
          <g key={f}>
            <line x1={pad.left} y1={y} x2={pad.left + innerW} y2={y}
                  stroke={C.border} strokeWidth={0.4} strokeDasharray="2,3" />
            <text x={cw - 4} y={y + 3}
                  fontFamily={C.mono} fontSize={9} fill={C.muted} textAnchor="end"
                  opacity={0.7}>
              {price.toFixed(5)}
            </text>
          </g>
        );
      })}

      {/* Position level lines — drawn behind candles */}
      {posLevels && (
        <g>
          <line x1={pad.left} y1={py(posLevels.entry)} x2={pad.left + innerW} y2={py(posLevels.entry)}
                stroke={C.purple} strokeWidth={1} strokeDasharray="4,3" opacity={0.7} />
          <text x={pad.left + 4} y={py(posLevels.entry) - 3}
                fontFamily={C.mono} fontSize={8} fill={C.purple} opacity={0.9}>ENTRY</text>

          <line x1={pad.left} y1={py(posLevels.stop)} x2={pad.left + innerW} y2={py(posLevels.stop)}
                stroke={C.danger} strokeWidth={1} strokeDasharray="4,3" opacity={0.6} />
          <text x={pad.left + 4} y={py(posLevels.stop) + 10}
                fontFamily={C.mono} fontSize={8} fill={C.danger} opacity={0.9}>STOP</text>

          <line x1={pad.left} y1={py(posLevels.target)} x2={pad.left + innerW} y2={py(posLevels.target)}
                stroke={C.accent} strokeWidth={1} strokeDasharray="4,3" opacity={0.6} />
          <text x={pad.left + 4} y={py(posLevels.target) - 3}
                fontFamily={C.mono} fontSize={8} fill={C.accent} opacity={0.9}>TARGET</text>

          {posLevels.trail && (
            <>
              <line x1={pad.left} y1={py(posLevels.trail)} x2={pad.left + innerW} y2={py(posLevels.trail)}
                    stroke={C.warn} strokeWidth={1} strokeDasharray="2,2" opacity={0.6} />
              <text x={pad.left + 4} y={py(posLevels.trail) - 3}
                    fontFamily={C.mono} fontSize={8} fill={C.warn} opacity={0.9}>TRAIL</text>
            </>
          )}
        </g>
      )}

      {/* Candles */}
      {bars.map((b, i) => {
        const cx = pad.left + (i + 0.5) * slotW;
        const x = cx - candleW / 2;
        const bullish = b.close >= b.open;
        const color = bullish ? C.accent : C.danger;
        const bodyTop = py(Math.max(b.open, b.close));
        const bodyH = Math.max(1, py(Math.min(b.open, b.close)) - bodyTop);
        return (
          <g key={b.ts}>
            <line x1={cx} y1={py(b.high)} x2={cx} y2={py(b.low)}
                  stroke={color} strokeWidth={1} opacity={0.5} />
            <rect x={x} y={bodyTop} width={candleW} height={bodyH}
                  fill={color} opacity={0.9} rx={1} />
          </g>
        );
      })}

      {/* Trade entry/exit markers — drawn on top of candles */}
      {tradeMarkers.map((m, mi) => {
        const cx = pad.left + (m.barIdx + 0.5) * slotW;
        const bar = bars[m.barIdx];
        if (m.type === "entry") {
          const markerY = py(bar.low) + 10;
          return (
            <g key={`entry-${m.ti}`}>
              <polygon
                points={`${cx - 5},${markerY + 8} ${cx + 5},${markerY + 8} ${cx},${markerY}`}
                fill={C.accent} opacity={0.9} />
              <text x={cx} y={markerY + 18} textAnchor="middle"
                    fontFamily={C.mono} fontSize={7} fill={C.accent} fontWeight="700">
                BUY
              </text>
            </g>
          );
        } else {
          const reason = m.trade.exit_reason ?? m.trade.reason ?? "exit";
          const meta = EXIT_REASON_LABELS[reason] ?? { label: reason.toUpperCase().slice(0, 6), color: C.muted };
          const pnlColor = (m.trade.net_pnl ?? 0) >= 0 ? C.accent : C.danger;
          const markerY = py(bar.high) - 10;
          return (
            <g key={`exit-${m.ti}`}>
              <polygon
                points={`${cx - 5},${markerY - 8} ${cx + 5},${markerY - 8} ${cx},${markerY}`}
                fill={pnlColor} opacity={0.9} />
              <text x={cx} y={markerY - 12} textAnchor="middle"
                    fontFamily={C.mono} fontSize={7} fill={meta.color} fontWeight="700">
                {meta.label}
              </text>
              <text x={cx} y={markerY - 21} textAnchor="middle"
                    fontFamily={C.mono} fontSize={7} fill={pnlColor} fontWeight="700">
                {(m.trade.net_pnl ?? 0) >= 0 ? `+$${(m.trade.net_pnl ?? 0).toFixed(2)}` : `-$${Math.abs(m.trade.net_pnl ?? 0).toFixed(2)}`}
              </text>
            </g>
          );
        }
      })}
    </svg>
    </div>
  );
}

function VolumeHistogram({ bars, height = 44 }) {
  const [containerRef, cw] = useContainerWidth(CHART_FALLBACK_W);

  if (!bars || bars.length === 0) return <div ref={containerRef} />;
  const padLeft = 6, priceGutter = 54;
  const drawW = cw - padLeft - priceGutter;
  const maxVol = Math.max(...bars.map(b => b.volume)) || 1;
  const n = bars.length;
  const slotW = drawW / n;
  const candleW = Math.max(4, Math.min(slotW * 0.78, 20));
  return (
    <div ref={containerRef} style={{ width: "100%" }}>
    <svg viewBox={`0 0 ${cw} ${height}`} width={cw} height={height}
         style={{ display: "block" }}>
      <line x1={0} y1={0.5} x2={cw} y2={0.5}
            stroke={C.border} strokeWidth={0.5} />
      {bars.map((b, i) => {
        const cx = padLeft + (i + 0.5) * slotW;
        const x = cx - candleW / 2;
        const barH = Math.max(1, (b.volume / maxVol) * (height - 6));
        return (
          <rect key={b.ts}
            x={x} y={height - barH - 2} width={candleW} height={barH}
            fill={b.close >= b.open ? C.accent : C.danger} opacity={0.35} rx={1} />
        );
      })}
    </svg>
    </div>
  );
}

// ─── Gate Health Strip — thin timeline showing gate readiness per bar ────────

function GateHealthStrip({ gateHistory, bars, height = 20 }) {
  const [containerRef, cw] = useContainerWidth(CHART_FALLBACK_W);

  if (!bars || bars.length === 0 || !gateHistory || gateHistory.length === 0) {
    return <div ref={containerRef} />;
  }
  const padLeft = 6, priceGutter = 54;
  const drawW = cw - padLeft - priceGutter;
  const n = bars.length;
  const slotW = drawW / n;
  const candleW = Math.max(4, Math.min(slotW * 0.78, 20));

  const barGates = bars.map(b => {
    let best = null, bestDist = Infinity;
    for (const snap of gateHistory) {
      const d = Math.abs(snap.ts - b.ts);
      if (d < bestDist) { bestDist = d; best = snap; }
    }
    return best && bestDist < 1800 ? best : null;
  });

  return (
    <div ref={containerRef} style={{ width: "100%" }}>
      <svg viewBox={`0 0 ${cw} ${height}`} width={cw} height={height}
           style={{ display: "block" }}>
        <line x1={0} y1={0.5} x2={cw} y2={0.5}
              stroke={C.border} strokeWidth={0.3} />
        {bars.map((b, i) => {
          const snap = barGates[i];
          if (!snap) return null;
          const cx = padLeft + (i + 0.5) * slotW;
          const x = cx - candleW / 2;
          const ratio = snap.passing / TOTAL_GATES;
          const color = snap.allPass ? C.accent
                      : ratio >= 0.75 ? C.warn
                      : ratio >= 0.5 ? `${C.warn}80`
                      : `${C.muted}40`;
          const barH = Math.max(2, ratio * (height - 4));
          return (
            <rect key={b.ts}
              x={x} y={height - barH - 1} width={candleW} height={barH}
              fill={color} rx={1} />
          );
        })}
        <text x={4} y={height - 3} fontFamily={C.mono} fontSize={7}
              fill={C.muted} opacity={0.6}>GATES</text>
      </svg>
    </div>
  );
}

// ─── OBI Gauge ────────────────────────────────────────────────────────────────

function OBIGauge({ obi = 0 }) {
  const pct = ((obi + 1) / 2) * 100;
  const color = obi > 0.2 ? C.accent : obi < -0.2 ? C.danger : C.warn;
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
        <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>SELL PRESSURE</span>
        <span style={{ fontFamily: C.mono, fontSize: 12, color, fontWeight: 700 }}>
          OBI {obi >= 0 ? "+" : ""}{obi.toFixed(3)}
        </span>
        <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>BUY PRESSURE</span>
      </div>
      <div style={{ height: 8, background: "#27272a", borderRadius: 4, overflow: "hidden" }}>
        <div style={{
          height: "100%", width: `${pct}%`,
          background: `linear-gradient(90deg, ${C.danger}80, ${color})`,
          transition: "width 0.5s ease",
        }} />
      </div>
    </div>
  );
}

// ─── Blocking Reason (human-readable gate failure) ───────────────────────────

function BlockingReason({ text, detail, closest }) {
  return (
    <div style={{
      display: "flex", alignItems: "flex-start", gap: 8, padding: "6px 0",
      borderBottom: `1px solid ${C.border}20`,
    }}>
      <span style={{ color: closest ? C.warn : C.danger, fontSize: 10, marginTop: 1,
                     flexShrink: 0 }}>{closest ? "◎" : "✕"}</span>
      <div style={{ flex: 1 }}>
        <div style={{ fontFamily: C.mono, fontSize: 11,
                      color: closest ? C.warn : C.text }}>{text}</div>
        {detail && (
          <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginTop: 1 }}>{detail}</div>
        )}
      </div>
    </div>
  );
}

// ─── Exit Condition Row ──────────────────────────────────────────────────────

function ExitCondition({ label, progress, detail, color }) {
  const pct = Math.max(0, Math.min(100, progress));
  return (
    <div style={{ padding: "6px 0", borderBottom: `1px solid ${C.border}20` }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 3 }}>
        <span style={{ fontFamily: C.mono, fontSize: 11, color: C.text }}>{label}</span>
        <span style={{ fontFamily: C.mono, fontSize: 10, color: color ?? C.muted }}>{detail}</span>
      </div>
      <div style={{ height: 4, background: "#27272a", borderRadius: 2, overflow: "hidden" }}>
        <div style={{
          height: "100%", width: `${pct}%`,
          background: color ?? C.muted,
          borderRadius: 2, transition: "width 0.4s ease",
        }} />
      </div>
    </div>
  );
}

// ─── Adaptive Sidebar ────────────────────────────────────────────────────────
// Single panel that changes based on engine state:
//   No position → why not trading (blocking gates as sentences)
//   Position open → position details + exit condition proximity

function buildBlockingReasons(gates, pp, obi) {
  if (!gates) return [];
  const reasons = [];

  if (!gates.macro_trend) {
    reasons.push({ text: "Macro downtrend", detail: "EMA50 declining — pair in bearish regime", key: "macro" });
  }
  if (gates.btc_risk_off) {
    reasons.push({ text: "BTC risk-off",
      detail: gates.btc_rsi != null ? `BTC RSI ${gates.btc_rsi} — dumping` : "BTC selling pressure",
      key: "btc" });
  }
  if (!gates.vol_regime) {
    const cur = gates.atr_pct != null ? (gates.atr_pct * 100).toFixed(2) : "?";
    reasons.push({ text: "Market too quiet",
      detail: `ATR ${cur}% < ${pp.atr}% min — not enough volatility to trade`, key: "atr" });
  }
  if (!gates.trend_aligned) {
    reasons.push({ text: "Trend not aligned", detail: "Fast EMA below slow EMA", key: "trend" });
  }
  if (!gates.not_bleeding) {
    reasons.push({ text: "Consecutive red bars", detail: "Recent candles all selling — active bleed", key: "bleed" });
  }
  if (!gates.volume_spike) {
    const cur = gates.vol_ema_value ? `${(gates.vol_ema_value / 1000).toFixed(1)}k` : "?";
    reasons.push({ text: "Volume too low",
      detail: `Need ${pp.volMult}x baseline spike (baseline: ${cur})`, key: "vol",
      closest: gates.vol_ema_value > 0 });
  }
  if (!gates.obi) {
    reasons.push({ text: "Buy pressure weak",
      detail: `OBI ${obi != null ? obi.toFixed(3) : "?"} < ${pp.obi.toFixed(2)} threshold`, key: "obi" });
  }
  if (!gates.rsi_window) {
    const rsi = gates.rsi_value;
    if (rsi != null) {
      if (rsi > pp.rsiHi) reasons.push({ text: "RSI overbought",
        detail: `RSI ${rsi} > ${pp.rsiHi} — too hot to enter`, key: "rsi" });
      else if (rsi < pp.rsiLo) reasons.push({ text: "RSI too low for momentum",
        detail: `RSI ${rsi} < ${pp.rsiLo} — may qualify for bounce mode`, key: "rsi" });
      else reasons.push({ text: "RSI outside window",
        detail: `RSI ${rsi} not in ${pp.rsiLo}–${pp.rsiHi}`, key: "rsi" });
    }
  }
  if (!gates.vwap_align) {
    reasons.push({ text: "Below VWAP", detail: "Price below session VWAP — no momentum confirmation", key: "vwap" });
  }
  if (!gates.not_extended) {
    const ext = gates.extension_pct != null ? (gates.extension_pct * 100).toFixed(1) : "?";
    reasons.push({ text: "Overextended",
      detail: `${ext}% above slow EMA (limit: ${pp.ext}%)`, key: "ext" });
  }
  if (!gates.ask_wall_clear) {
    reasons.push({ text: "Ask wall detected", detail: `Sell wall > $${pp.wall} blocking upside`, key: "wall" });
  }

  return reasons;
}

function AdaptiveSidebar({ position, midPrice, pair, gates, obi, confidence, kellySize,
                           trades, engineState, enabled }) {
  const pp = pairProfile(pair);

  // ── Position open: show trade + exit proximity ──
  if (position) {
    const mode = position.entry_mode ?? "momentum";
    const isBounce = mode === "bounce";
    const stopPct = isBounce ? pp.stopBounce : pp.stopMom;
    const targetPct = isBounce ? pp.profitBounce : pp.profitMom;
    const timeStop = isBounce ? pp.timeStopBounce : pp.timeStopMom;

    const entryPct = ((midPrice - position.entry_price) / position.entry_price) * 100;
    const progress = Math.max(0, Math.min(100,
      ((entryPct - stopPct) / (targetPct - stopPct)) * 100));
    const pnlColor = entryPct >= 0 ? C.accent : C.danger;
    const candles = position.candles_held ?? 0;
    const peakPrice = position.peak_price ?? 0;
    const peakPct = peakPrice > 0 ? ((peakPrice - position.entry_price) / position.entry_price) * 100 : 0;
    const trailActivatePct = pp.trailActivate;
    const trailActive = peakPct >= trailActivatePct;
    const trailLevel = trailActive ? peakPrice * (1 - pp.trailOffset / 100) : null;
    const stopPrice = position.entry_price * (1 + stopPct / 100);
    const targetPrice = position.entry_price * (1 + targetPct / 100);

    // Exit condition proximity calculations
    const timeProgress = (candles / timeStop) * 100;
    const stopDist = Math.abs(entryPct - stopPct);
    const targetDist = Math.abs(targetPct - entryPct);
    const trailArmProgress = trailActivatePct > 0 ? (peakPct / trailActivatePct) * 100 : 0;

    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 0, height: "100%" }}>
        {/* Position header */}
        <div style={{ padding: "14px 16px", background: C.panel, borderRadius: "8px 8px 0 0",
                      border: `1px solid ${C.purple}50`, borderBottom: "none" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
                        marginBottom: 10 }}>
            <span style={{ fontFamily: C.mono, fontSize: 10, color: C.purple,
                           textTransform: "uppercase", letterSpacing: "0.1em" }}>Open Position</span>
            <span style={{
              fontFamily: C.mono, fontSize: 10, fontWeight: 700,
              padding: "2px 8px", borderRadius: 4,
              background: isBounce ? `${C.blue}20` : `${C.accent}20`,
              color: isBounce ? C.blue : C.accent,
            }}>{mode.toUpperCase()}</span>
          </div>

          {/* Big PnL */}
          <div style={{ textAlign: "center", marginBottom: 12 }}>
            <span style={{ fontFamily: C.mono, fontSize: 26, fontWeight: 700, color: pnlColor }}>
              {entryPct >= 0 ? "+" : ""}{entryPct.toFixed(2)}%
            </span>
          </div>

          {/* Entry/Mid/Qty compact row */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 6, marginBottom: 12 }}>
            {[
              ["Entry", position.entry_price.toFixed(6)],
              ["Now", midPrice.toFixed(6)],
              ["Qty", position.qty?.toFixed(2) ?? "—"],
            ].map(([l, v]) => (
              <div key={l}>
                <div style={{ fontFamily: C.mono, fontSize: 9, color: C.muted }}>{l}</div>
                <div style={{ fontFamily: C.mono, fontSize: 11, color: C.text }}>{v}</div>
              </div>
            ))}
          </div>

          {/* Progress bar: stop → target */}
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 3 }}>
            <span style={{ fontFamily: C.mono, fontSize: 9, color: C.danger }}>{stopPct}%</span>
            <span style={{ fontFamily: C.mono, fontSize: 9, color: C.accent }}>+{targetPct}%</span>
          </div>
          <div style={{ height: 8, background: "#27272a", borderRadius: 4, overflow: "hidden" }}>
            <div style={{
              height: "100%", width: `${progress}%`,
              background: entryPct >= 0 ? C.accent : C.danger,
              transition: "width 0.5s ease",
            }} />
          </div>
        </div>

        {/* Exit conditions */}
        <div style={{ padding: "12px 16px", background: C.panel, borderRadius: "0 0 8px 8px",
                      border: `1px solid ${C.purple}50`, borderTop: `1px solid ${C.border}`,
                      flex: 1 }}>
          <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginBottom: 8,
                        textTransform: "uppercase", letterSpacing: "0.1em" }}>Exit Watch</div>

          <ExitCondition
            label="Time stop"
            progress={timeProgress}
            detail={`${candles}/${timeStop} bars`}
            color={timeProgress >= 80 ? C.danger : timeProgress >= 50 ? C.warn : C.muted}
          />
          <ExitCondition
            label={trailActive ? "Trail armed" : "Trail activation"}
            progress={trailArmProgress}
            detail={trailActive
              ? `trailing at ${trailLevel?.toFixed(6)}`
              : `peak ${peakPct.toFixed(2)}% / ${trailActivatePct}%`}
            color={trailActive ? C.warn : C.muted}
          />
          <ExitCondition
            label="Distance to stop"
            progress={stopDist > 0 ? Math.max(0, 100 - (stopDist / Math.abs(stopPct)) * 100) : 0}
            detail={`${stopDist.toFixed(2)}% away`}
            color={stopDist < 0.3 ? C.danger : stopDist < 0.6 ? C.warn : C.muted}
          />
          <ExitCondition
            label="Distance to target"
            progress={targetDist > 0 ? Math.max(0, 100 - (targetDist / targetPct) * 100) : 0}
            detail={`${targetDist.toFixed(2)}% away`}
            color={targetDist < 0.5 ? C.accent : C.muted}
          />

          {/* Level reference */}
          <div style={{ display: "flex", flexDirection: "column", gap: 3,
                        padding: "8px 0", marginTop: 6,
                        fontFamily: C.mono, fontSize: 10 }}>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: C.danger }}>▼ {stopPrice.toFixed(6)}</span>
              <span style={{ color: C.accent }}>▲ {targetPrice.toFixed(6)}</span>
            </div>
            {trailLevel && (
              <div style={{ textAlign: "center", color: C.warn }}>
                ◆ trail {trailLevel.toFixed(6)}
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  // ── No position: show why not ──
  const blockingReasons = buildBlockingReasons(gates, pp, obi);
  const passingCount = countPassingGates(gates);
  const totalGates = TOTAL_GATES;
  const lastTrade = trades && trades.length > 0 ? trades[trades.length - 1] : null;

  // BTC risk-off or halted — special states
  const btcRiskOff = gates?.btc_risk_off ?? false;
  const isHalted = engineState === "halted";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 0, height: "100%" }}>
      {/* Status header */}
      <div style={{ padding: "14px 16px", background: C.panel, borderRadius: "8px 8px 0 0",
                    border: `1px solid ${C.border}`, borderBottom: "none" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
                      marginBottom: 8 }}>
          <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted,
                         textTransform: "uppercase", letterSpacing: "0.1em" }}>
            {isHalted ? "Halted" : btcRiskOff ? "BTC Risk-Off" : "Watching"}
          </span>
          <span style={{ fontFamily: C.mono, fontSize: 10,
                         color: blockingReasons.length === 0 ? C.accent : C.muted }}>
            {passingCount}/{totalGates} gates
          </span>
        </div>

        {/* Gate progress bar */}
        <div style={{ height: 6, background: "#27272a", borderRadius: 3, overflow: "hidden",
                      marginBottom: 10 }}>
          <div style={{
            height: "100%",
            width: `${(passingCount / totalGates) * 100}%`,
            background: passingCount >= 10 ? C.accent : passingCount >= 7 ? C.warn : C.muted,
            borderRadius: 3, transition: "width 0.4s ease",
          }} />
        </div>

        {/* All pass → ready to fire */}
        {blockingReasons.length === 0 && gates?.all_pass && (
          <div style={{
            padding: "10px 0", borderRadius: 6, textAlign: "center",
            background: `${C.accent}12`, border: `1px solid ${C.accent}50`,
            fontFamily: C.mono, fontSize: 14, fontWeight: 700, color: C.accent,
            boxShadow: `0 0 20px ${C.accent}20`,
          }}>
            BUY SIGNAL
          </div>
        )}

        {/* Confidence + Kelly when close */}
        {confidence > 0 && (
          <div style={{ display: "flex", gap: 12, padding: "8px 0" }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontFamily: C.mono, fontSize: 9, color: C.muted }}>Confidence</div>
              <div style={{ fontFamily: C.mono, fontSize: 14, fontWeight: 700,
                            color: confidence >= 0.7 ? C.accent : confidence >= 0.5 ? C.warn : C.muted }}>
                {(confidence * 100).toFixed(0)}%
              </div>
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ fontFamily: C.mono, fontSize: 9, color: C.muted }}>Kelly Size</div>
              <div style={{ fontFamily: C.mono, fontSize: 14, fontWeight: 700, color: C.purple }}>
                {kellySize > 0 ? `$${kellySize.toFixed(0)}` : "—"}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Blocking reasons */}
      <div style={{ padding: "10px 16px", background: C.panel, borderRadius: "0 0 8px 8px",
                    border: `1px solid ${C.border}`, borderTop: `1px solid ${C.border}`,
                    flex: 1, overflowY: "auto" }}>
        {isHalted ? (
          <div style={{ padding: "16px 0", textAlign: "center" }}>
            <div style={{ fontFamily: C.mono, fontSize: 13, color: C.danger, fontWeight: 700,
                          marginBottom: 4 }}>Daily cap reached</div>
            <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>
              Engine paused until next session
            </div>
          </div>
        ) : blockingReasons.length > 0 ? (
          <>
            <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginBottom: 6,
                          textTransform: "uppercase", letterSpacing: "0.1em" }}>
              Why not trading
            </div>
            {blockingReasons.map(r => (
              <BlockingReason key={r.key} text={r.text} detail={r.detail} closest={r.closest} />
            ))}
          </>
        ) : !gates ? (
          <div style={{ padding: "16px 0", textAlign: "center",
                        fontFamily: C.mono, fontSize: 11, color: C.muted }}>
            Waiting for first bar close…
          </div>
        ) : null}

        {/* Last trade summary */}
        {lastTrade && (
          <div style={{ marginTop: 10, padding: "8px 10px", background: C.bg, borderRadius: 6 }}>
            <div style={{ fontFamily: C.mono, fontSize: 9, color: C.muted, marginBottom: 4,
                          textTransform: "uppercase" }}>Last Trade</div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span style={{ fontFamily: C.mono, fontSize: 12, fontWeight: 700,
                             color: (lastTrade.net_pnl ?? 0) >= 0 ? C.accent : C.danger }}>
                {(lastTrade.net_pnl ?? 0) >= 0 ? "+" : ""}${(lastTrade.net_pnl ?? 0).toFixed(2)}
              </span>
              <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>
                {lastTrade.exit_reason ?? "—"}
              </span>
              <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>
                {lastTrade.hold_candles ?? 0}c
              </span>
            </div>
          </div>
        )}

        {/* Disabled notice */}
        {!enabled && (
          <div style={{ marginTop: 10, padding: "8px", borderRadius: 6,
                        background: `${C.warn}10`, border: `1px solid ${C.warn}30`,
                        fontFamily: C.mono, fontSize: 10, color: C.warn, textAlign: "center" }}>
            Pair disabled — entries blocked
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Trade Log ────────────────────────────────────────────────────────────────

function TradeLog({ trades }) {
  if (!trades || trades.length === 0) {
    return (
      <div style={{ fontFamily: C.mono, fontSize: 11, color: C.muted,
                    padding: "20px 0", textAlign: "center" }}>
        No closed trades this session
      </div>
    );
  }
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: C.mono, fontSize: 11 }}>
        <thead>
          <tr>
            {["TIME", "ENTRY", "EXIT", "NET P&L", "REASON", "HOLD"].map(c => (
              <th key={c} style={{ padding: "5px 10px", textAlign: "left", color: C.muted,
                                   borderBottom: `1px solid ${C.border}`, whiteSpace: "nowrap",
                                   fontWeight: 400 }}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {[...trades].reverse().map(t => (
            <tr key={t.exit_ts} style={{ borderBottom: `1px solid ${C.border}15` }}>
              <td style={{ padding: "5px 10px", color: C.muted }}>
                {new Date((t.exit_ts ?? 0) * 1000).toLocaleTimeString()}
              </td>
              <td style={{ padding: "5px 10px", color: C.text }}>{(t.entry_price ?? 0).toFixed(6)}</td>
              <td style={{ padding: "5px 10px", color: C.text }}>{(t.exit_price ?? 0).toFixed(6)}</td>
              <td style={{ padding: "5px 10px", fontWeight: 700,
                           color: (t.net_pnl ?? 0) >= 0 ? C.accent : C.danger }}>
                {(t.net_pnl ?? 0) >= 0 ? "+" : ""}${(t.net_pnl ?? 0).toFixed(2)}
              </td>
              <td style={{ padding: "5px 10px", color: C.muted }}>{t.exit_reason ?? "—"}</td>
              <td style={{ padding: "5px 10px", color: C.muted }}>{t.hold_candles ?? 0}c</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Trading View ─────────────────────────────────────────────────────────────

function TradingView({ state, connected, pair, onDisable, candleInterval }) {
  const { gates, position, midPrice, obi, engineState, sessionStats, trades, bars, spreadBps,
          enabled, confidence, kellySize, gateHistory } = state;

  if (!connected) {
    return (
      <div>
        <OfflineBanner />
        <div style={{ padding: 40, textAlign: "center", fontFamily: C.mono, fontSize: 12,
                      color: C.muted, background: C.panel, borderRadius: 8,
                      border: `1px solid ${C.border}` }}>
          Live candles, signals, and position tracking appear here once APEX is connected.
        </div>
      </div>
    );
  }

  const intervalLabel = candleInterval ? `${candleInterval}-min` : "15-min";
  const pnl = sessionStats?.session_pnl ?? 0;
  const tradeCount = sessionStats?.trade_count ?? 0;
  const winRate = sessionStats?.win_rate ?? 0;

  return (
    <div>
      {/* Control row — pair, price, session stats, state, disable */}
      <div style={{
        display: "flex", alignItems: "center", gap: 12, marginBottom: 16,
        padding: "10px 16px", background: C.panel, borderRadius: 8,
        border: `1px solid ${C.border}`, flexWrap: "wrap",
      }}>
        <span style={{ fontFamily: C.mono, fontSize: 15, fontWeight: 700, color: C.text }}>
          {pair ?? "—"}
        </span>
        <span style={{ fontFamily: C.mono, fontSize: 18, fontWeight: 700,
                       color: midPrice > 0 ? C.text : C.muted }}>
          {midPrice > 0 ? `$${midPrice.toFixed(6)}` : "—"}
        </span>
        {spreadBps > 0 && (
          <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>
            {spreadBps.toFixed(1)} bps
          </span>
        )}
        <div style={{ width: 1, height: 16, background: C.border, flexShrink: 0 }} />
        {/* Inline session stats */}
        <span style={{ fontFamily: C.mono, fontSize: 12, fontWeight: 700,
                       color: pnl >= 0 ? C.accent : C.danger }}>
          {pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}
        </span>
        <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>
          {tradeCount}t · {(winRate * 100).toFixed(0)}%w
        </span>
        <div style={{ width: 1, height: 16, background: C.border, flexShrink: 0 }} />
        <div style={{
          display: "flex", alignItems: "center", gap: 6,
          padding: "3px 10px", borderRadius: 20,
          background: engineState === "running" ? `${C.accent}15`
                    : engineState === "switching" ? `${C.warn}15`
                    : engineState === "halted" ? `${C.danger}15` : "#27272a",
        }}>
          <div style={{
            width: 7, height: 7, borderRadius: "50%",
            background: engineState === "running" ? C.accent
                       : engineState === "switching" ? C.warn
                       : engineState === "halted" ? C.danger : C.muted,
          }} />
          <span style={{ fontFamily: C.mono, fontSize: 10,
                         color: engineState === "running" ? C.accent
                               : engineState === "switching" ? C.warn
                               : engineState === "halted" ? C.danger : C.muted }}>
            {engineState}
          </span>
        </div>
        <div style={{ marginLeft: "auto" }}>
          <button
            onClick={onDisable}
            disabled={!enabled}
            style={{
              padding: "6px 18px", borderRadius: 6,
              border: `1px solid ${C.warn}50`, background: "transparent",
              color: C.warn, fontFamily: C.mono, fontSize: 12, fontWeight: 700,
              cursor: enabled ? "pointer" : "not-allowed",
              opacity: enabled ? 1 : 0.3,
            }}
          >
            DISABLE
          </button>
        </div>
      </div>

      {/* Main 2-column grid — chart left, adaptive sidebar right */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 280px", gap: 12,
                    marginBottom: 12 }}>
        {/* LEFT — hero chart */}
        <div style={{ background: "#0d0d0f", borderRadius: 8,
                      border: `1px solid ${C.border}`, overflow: "hidden",
                      display: "flex", flexDirection: "column" }}>
          <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted,
                        padding: "10px 14px 6px",
                        textTransform: "uppercase", letterSpacing: "0.1em",
                        display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span>{intervalLabel} Candles</span>
            {bars && bars.length > 0 && (
              <span style={{ fontSize: 9, opacity: 0.6 }}>{bars.length} bars</span>
            )}
          </div>
          <CandleChart bars={bars} height={340} trades={trades} position={position} pair={pair} />
          <VolumeHistogram bars={bars} height={72} />
          <GateHealthStrip gateHistory={gateHistory} bars={bars} height={20} />
          <div style={{ padding: "6px 14px 10px" }}>
            <OBIGauge obi={obi ?? 0} />
          </div>
        </div>

        {/* RIGHT — adaptive sidebar */}
        <AdaptiveSidebar
          position={position}
          midPrice={midPrice ?? 0}
          pair={pair}
          gates={gates}
          obi={obi}
          confidence={confidence ?? 0}
          kellySize={kellySize ?? 0}
          trades={trades}
          engineState={engineState}
          enabled={enabled}
        />
      </div>

      {/* Trade log */}
      <div style={{ padding: 16, background: C.panel, borderRadius: 8,
                    border: `1px solid ${C.border}` }}>
        <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginBottom: 10,
                      textTransform: "uppercase", letterSpacing: "0.1em" }}>Trade Log</div>
        <TradeLog trades={trades} />
      </div>
    </div>
  );
}

// ─── Offline Banner ───────────────────────────────────────────────────────────

function OfflineBanner() {
  return (
    <div style={{
      padding: "12px 16px", borderRadius: 8, marginBottom: 16,
      background: "#1c0a0a", border: `1px solid ${C.danger}40`,
      display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
    }}>
      <span style={{ fontFamily: C.mono, fontSize: 11, fontWeight: 700,
                     color: C.danger, flexShrink: 0 }}>APEX OFFLINE</span>
      <span style={{ fontFamily: C.mono, fontSize: 11, color: C.muted }}>
        Run{" "}
        <code style={{ color: C.text, background: "#27272a", padding: "1px 6px",
                       borderRadius: 4 }}>start_meme.bat</code>
        {" "}to connect the competition scanner.
      </span>
    </div>
  );
}

// ─── Scan Countdown ───────────────────────────────────────────────────────────

function ScanCountdown({ lastScanTs }) {
  const [secsLeft, setSecsLeft] = useState(null);
  useEffect(() => {
    if (!lastScanTs) return;
    const tick = () => {
      const elapsed = Date.now() / 1000 - lastScanTs;
      setSecsLeft(Math.max(0, Math.ceil(900 - elapsed)));
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [lastScanTs]);
  if (secsLeft === null) return <span style={{ fontFamily: C.mono, fontSize: 15, color: C.muted }}>scanning…</span>;
  const m = Math.floor(secsLeft / 60);
  const s = secsLeft % 60;
  return (
    <span style={{ fontFamily: C.mono, fontSize: 15, color: C.muted }}>
      next scan in {m}:{String(s).padStart(2, "0")}
    </span>
  );
}

// ─── Confidence Bar ───────────────────────────────────────────────────────────

function ConfidenceBar({ value }) {
  const pct = Math.max(0, Math.min(100, (value ?? 0) * 100));
  const color = pct >= 70 ? C.accent : pct >= 50 ? C.warn : C.muted;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <div style={{ flex: 1, height: 6, background: "#27272a", borderRadius: 3, overflow: "hidden" }}>
        <div style={{
          height: "100%", width: `${pct}%`,
          background: color, borderRadius: 3,
          transition: "width 0.4s ease",
        }} />
      </div>
      <span style={{ fontFamily: C.mono, fontSize: 11, color, minWidth: 32, textAlign: "right" }}>
        {pct.toFixed(0)}%
      </span>
    </div>
  );
}

// ─── Discover View ────────────────────────────────────────────────────────────

function DiscoverView({ tokens, pairStates, onToggle, anyConnected, lastScanTs,
                        wsRefs, scanInProgress }) {
  function handleScanNow() {
    SEED_PAIRS.forEach(pair => {
      const ws = wsRefs.current[pair];
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "scan_now" }));
      }
    });
  }

  function ratioColor(r) {
    if (!r || r < 2) return C.muted;
    if (r >= 5) return C.danger;
    if (r >= 4) return C.warn;
    return C.blue;
  }

  function fmtVol(v) {
    if (!v) return "—";
    if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
    if (v >= 1_000) return `${(v / 1_000).toFixed(1)}k`;
    return v.toFixed(0);
  }

  const totalKelly = SEED_PAIRS.reduce((sum, pair) => {
    const ps = pairStates[pair];
    return sum + (ps?.enabled && ps?.connected ? (ps.kellySize ?? 0) : 0);
  }, 0);
  const totalPnl = SEED_PAIRS.reduce((sum, pair) => {
    return sum + (pairStates[pair]?.sessionStats?.session_pnl ?? 0);
  }, 0);
  const totalTrades = SEED_PAIRS.reduce((sum, pair) => {
    return sum + (pairStates[pair]?.sessionStats?.trade_count ?? 0);
  }, 0);

  const displayTokens = anyConnected
    ? [...tokens].sort((a, b) => (b.anomaly_ratio ?? 0) - (a.anomaly_ratio ?? 0))
    : tokens;

  return (
    <div>
      {anyConnected ? (
        <div style={{
          display: "flex", alignItems: "center", gap: 12, marginBottom: 16,
          padding: "10px 16px", background: C.panel, borderRadius: 8,
          border: `1px solid ${C.border}`,
        }}>
          <span style={{ fontFamily: C.mono, fontSize: 15, color: C.text }}>
            {tokens.filter(t => t.current_volume != null).length}/{tokens.length} tokens scanned
          </span>
          <span style={{ color: C.border }}>·</span>
          <ScanCountdown lastScanTs={lastScanTs} />
          {tokens.filter(t => (t.anomaly_ratio ?? 0) >= 5).length > 0 && (
            <>
              <span style={{ color: C.border }}>·</span>
              <span style={{ fontFamily: C.mono, fontSize: 15, color: C.warn, fontWeight: 700 }}>
                {tokens.filter(t => (t.anomaly_ratio ?? 0) >= 5).length} anomal{tokens.filter(t => (t.anomaly_ratio ?? 0) >= 5).length === 1 ? "y" : "ies"}
              </span>
            </>
          )}
          <button
            onClick={handleScanNow}
            disabled={scanInProgress}
            style={{
              marginLeft: "auto", padding: "5px 14px", borderRadius: 6,
              border: `1px solid ${C.border}`, background: "transparent",
              color: scanInProgress ? C.muted : C.text,
              fontFamily: C.mono, fontSize: 14, cursor: scanInProgress ? "default" : "pointer",
            }}
          >
            {scanInProgress ? "Scanning…" : "Scan Now"}
          </button>
        </div>
      ) : (
        <OfflineBanner />
      )}

      {anyConnected && (
        <div style={{
          fontFamily: C.mono, fontSize: 11, color: C.muted, marginBottom: 10,
          padding: "6px 16px", display: "flex", gap: 16, alignItems: "center",
        }}>
          <span>Vol Surge = 24h volume / 7-day baseline</span>
          <span style={{ color: C.border }}>·</span>
          <span><span style={{ color: C.blue }}>2-4×</span> elevated</span>
          <span><span style={{ color: C.warn }}>4-5×</span> warming</span>
          <span><span style={{ color: C.danger }}>≥5×</span> competition alert</span>
        </div>
      )}

      <div style={{ background: C.panel, borderRadius: 8, border: `1px solid ${C.border}`,
                    marginBottom: 12, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: C.mono }}>
          <thead>
            <tr style={{ borderBottom: `1px solid ${C.border}` }}>
              {["Token", "Vol 24h", "7d Baseline", "Vol Surge", "Type", "Confidence", "Kelly $", "Status"].map(h => (
                <th key={h} style={{
                  padding: "12px 14px", textAlign: "left", fontSize: 13,
                  color: C.muted, fontWeight: 400, textTransform: "uppercase",
                  letterSpacing: "0.07em", whiteSpace: "nowrap",
                }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {displayTokens.map((token, idx) => {
              const ps = pairStates[token.pair] ?? {};
              const isAnomaly = (token.anomaly_ratio ?? 0) >= 5;
              const isEnabled = ps.enabled ?? true;
              const isConnected = ps.connected ?? false;
              const canToggle = isConnected;
              const evenRow = idx % 2 === 0;

              return (
                <tr key={token.pair} style={{
                  borderBottom: `1px solid ${C.border}25`,
                  background: isEnabled && isConnected ? `${C.accent}0a`
                            : isAnomaly ? `${C.warn}0c`
                            : evenRow ? "#1a1a1f" : C.panel,
                  borderLeft: isAnomaly ? `3px solid ${C.warn}`
                            : isEnabled && isConnected ? `3px solid ${C.accent}`
                            : "3px solid transparent",
                  transition: "background 0.3s",
                }}>
                  <td style={{ padding: "12px 14px" }}>
                    <div style={{ fontFamily: C.mono, fontSize: 16, fontWeight: 700,
                                  color: C.text }}>{token.pair}</div>
                    {isConnected && (
                      <div style={{ fontFamily: C.mono, fontSize: 9, color: C.muted, marginTop: 2 }}>
                        port {ps.port ?? "—"}
                      </div>
                    )}
                  </td>
                  <td style={{ padding: "12px 14px", fontFamily: C.mono, fontSize: 15,
                                color: C.text }}>
                    {token.current_volume != null ? fmtVol(token.current_volume)
                      : scanInProgress ? <span style={{ color: C.muted, fontSize: 13 }}>scanning…</span>
                      : "—"}
                  </td>
                  <td style={{ padding: "12px 14px", fontFamily: C.mono, fontSize: 15,
                                color: C.muted }}>{fmtVol(token.baseline_volume_7d)}</td>
                  <td style={{ padding: "12px 14px" }}>
                    {token.anomaly_ratio != null ? (
                      <span style={{
                        padding: "3px 10px", borderRadius: 4, fontFamily: C.mono,
                        fontSize: 14, fontWeight: 700,
                        background: ratioColor(token.anomaly_ratio) + "20",
                        color: ratioColor(token.anomaly_ratio),
                      }}>
                        {token.anomaly_ratio.toFixed(1)}×
                      </span>
                    ) : (
                      <span style={{ fontFamily: C.mono, fontSize: 14, color: C.muted }}>—</span>
                    )}
                  </td>
                  <td style={{ padding: "12px 14px", fontFamily: C.mono, fontSize: 14,
                                color: C.muted }}>
                    {token.competition_type
                      ? <>{token.competition_type}{!token.competition_type_confirmed && <span style={{ color: C.muted + "80" }}> *</span>}</>
                      : "—"}
                  </td>
                  <td style={{ padding: "12px 14px", minWidth: 110 }}>
                    {isConnected
                      ? <ConfidenceBar value={ps.confidence ?? 0} />
                      : <span style={{ fontFamily: C.mono, fontSize: 13, color: C.muted }}>—</span>}
                  </td>
                  <td style={{ padding: "12px 14px" }}>
                    <div style={{ fontFamily: C.mono, fontSize: 14,
                                  color: isConnected ? C.purple : C.muted }}>
                      {isConnected && (ps.kellySize ?? 0) > 0
                        ? `$${(ps.kellySize ?? 0).toFixed(0)}`
                        : "—"}
                    </div>
                  </td>
                  <td style={{ padding: "10px 12px" }}>
                    <button
                      onClick={() => canToggle && onToggle(token.pair)}
                      disabled={!canToggle}
                      title={!isConnected ? "Pair not connected"
                           : isEnabled ? "Disable entries for this pair"
                           : "Enable entries for this pair"}
                      style={{
                        display: "inline-flex", alignItems: "center", justifyContent: "center",
                        gap: 8, width: 110, padding: "8px 0", borderRadius: 6, border: "none",
                        background: isEnabled && isConnected ? `${C.accent}20`
                                  : canToggle ? `${C.purple}20`
                                  : "#27272a",
                        color: isEnabled && isConnected ? C.accent
                             : canToggle ? C.purple : C.muted,
                        fontFamily: C.mono, fontSize: 13, fontWeight: 700,
                        cursor: canToggle ? "pointer" : "not-allowed",
                        transition: "all 0.2s",
                      }}
                    >
                      <div style={{
                        width: 36, height: 20, borderRadius: 4, position: "relative",
                        background: isEnabled && isConnected ? C.accent
                                  : canToggle ? "#3f3f46" : "#27272a",
                        border: `1px solid ${isEnabled && isConnected ? C.accent
                                            : canToggle ? C.purple + "60" : C.border}`,
                        transition: "all 0.2s", flexShrink: 0,
                      }}>
                        <div style={{
                          position: "absolute", top: 3,
                          left: isEnabled && isConnected ? 19 : 3,
                          width: 12, height: 12, borderRadius: 2,
                          background: isEnabled && isConnected ? "#fff"
                                    : canToggle ? C.purple : C.muted,
                          transition: "left 0.2s",
                        }} />
                      </div>
                      {isEnabled && isConnected ? "ENABLED" : "DISABLED"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {anyConnected && (
        <div style={{
          display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10,
          padding: 16, background: C.panel, borderRadius: 8, border: `1px solid ${C.border}`,
        }}>
          {[
            ["Net P&L", `${totalPnl >= 0 ? "+" : ""}$${totalPnl.toFixed(2)}`, totalPnl >= 0 ? C.accent : C.danger],
            ["Kelly Alloc", totalKelly > 0 ? `$${totalKelly.toFixed(0)}` : "—", C.purple],
            ["Trades", String(totalTrades), C.text],
          ].map(([l, v, color]) => (
            <div key={l} style={{ textAlign: "center" }}>
              <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted,
                            textTransform: "uppercase", letterSpacing: "0.07em",
                            marginBottom: 4 }}>{l}</div>
              <div style={{ fontFamily: C.mono, fontSize: 17, fontWeight: 700, color }}>{v}</div>
            </div>
          ))}
        </div>
      )}

      {!anyConnected && (
        <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted + "80",
                      textAlign: "center", marginTop: 12 }}>
          {tokens.length} seed pairs · volume data appears once APEX connects and scans
        </div>
      )}
    </div>
  );
}

// ─── Root ─────────────────────────────────────────────────────────────────────

function buildInitialPairState() {
  const init = {};
  SEED_PAIRS.forEach((pair, i) => {
    init[pair] = {
      port: APEX_WS_BASE + i,
      connected: false,
      enabled: true,
      gates: null,
      position: null,
      midPrice: 0,
      bars: [],
      obi: 0,
      engineState: "idle",
      sessionStats: null,
      trades: [],
      spreadBps: 0,
      confidence: 0,
      kellySize: 0,
      siblings: [],
      candleInterval: 15,
      gateHistory: [],
    };
  });
  return init;
}

export default function MemeTab() {
  const [subView, setSubView] = useState("trading");
  const [selectedPair, setSelectedPair] = useState(SEED_PAIRS[0]);
  const [pairStates, setPairStates] = useState(buildInitialPairState);
  const [tokens, setTokens] = useState(() => SEED_PAIRS.map(p => ({ pair: p })));
  const [scanInProgress, setScanInProgress] = useState(false);
  const [lastScanTs, setLastScanTs] = useState(null);
  const [sellAlerts, setSellAlerts] = useState({});

  const wsRefs = useRef({});
  const selectedPairRef = useRef(selectedPair);
  useEffect(() => { selectedPairRef.current = selectedPair; }, [selectedPair]);

  const anyConnected = Object.values(pairStates).some(s => s.connected);
  const connectedCount = Object.values(pairStates).filter(s => s.connected).length;

  function updatePair(pair, updates) {
    setPairStates(prev => ({
      ...prev,
      [pair]: { ...prev[pair], ...updates },
    }));
  }

  const handlePairMessage = useCallback((pair, evt) => {
    try {
      const msg = JSON.parse(evt.data);
      switch (msg.type) {
        case "initial_state":
          updatePair(pair, {
            engineState: msg.engine_state ?? "idle",
            position: msg.position ?? null,
            trades: msg.trades ?? [],
            enabled: msg.enabled !== undefined ? msg.enabled : true,
            candleInterval: msg.candle_interval ?? 15,
            ...(msg.session_pnl != null || msg.trade_count != null ? {
              sessionStats: {
                session_pnl: msg.session_pnl ?? 0,
                daily_loss: msg.daily_loss ?? 0,
                trade_count: msg.trade_count ?? 0,
                win_rate: msg.win_rate ?? 0,
              }
            } : {}),
          });
          break;
        case "warmup_progress":
          break;
        case "candle_history":
          updatePair(pair, { bars: msg.bars ?? [] });
          break;
        case "bar_update":
          if (msg.bar) {
            setPairStates(prev => {
              const ps = prev[pair];
              const deduped = ps.bars.filter(b => b.ts !== msg.bar.ts);
              return { ...prev, [pair]: { ...ps, bars: [...deduped, msg.bar].slice(-100) } };
            });
          }
          break;
        case "ticker":
          setPairStates(prev => {
            const ps = prev[pair];
            const gateUpdates = msg.btc_risk_off !== undefined && ps.gates ? {
              gates: {
                ...ps.gates,
                btc_risk_off: msg.btc_risk_off,
                ...(msg.btc_rsi != null ? { btc_rsi: msg.btc_rsi } : {}),
                ...(msg.btc_1h_chg != null ? { btc_1h_chg: msg.btc_1h_chg } : {}),
              }
            } : {};
            return { ...prev, [pair]: {
              ...ps,
              midPrice: msg.price ?? ps.midPrice,
              obi: msg.obi ?? ps.obi,
              spreadBps: msg.spread_bps ?? ps.spreadBps,
              ...gateUpdates,
            }};
          });
          break;
        case "signal_state":
          setPairStates(prev => {
            const ps = prev[pair];
            const g = msg.gates ?? {};
            const passing = countPassingGates(g);
            const snap = {
              ts: Date.now() / 1000,
              passing,
              allPass: !!g.all_pass,
              rsi: g.rsi_value ?? null,
              atrPct: g.atr_pct ?? null,
              confidence: g.confidence ?? 0,
            };
            const history = [...ps.gateHistory, snap].slice(-100);
            return { ...prev, [pair]: {
              ...ps,
              gates: msg.gates,
              engineState: "running",
              confidence: g.confidence ?? 0,
              kellySize: g.kelly_size ?? 0,
              enabled: g.enabled !== undefined ? g.enabled : ps.enabled,
              siblings: msg.siblings ?? [],
              gateHistory: history,
            }};
          });
          if (msg.gates?.all_pass && selectedPairRef.current === pair) setSubView("trading");
          break;
        case "pair_enabled":
          updatePair(pair, { enabled: msg.enabled !== undefined ? msg.enabled : true });
          break;
        case "position_update":
          setPairStates(prev => {
            const ps = prev[pair];
            const entryUpdate = msg.entry && typeof msg.entry === "object"
              ? { position: ps.position ? { ...ps.position, ...msg.entry } : msg.entry }
              : {};
            return { ...prev, [pair]: {
              ...ps,
              midPrice: msg.price ?? ps.midPrice,
              obi: msg.obi ?? ps.obi,
              spreadBps: msg.spread_bps ?? ps.spreadBps,
              ...entryUpdate,
            }};
          });
          break;
        case "order_placed":
          if (msg.side === "buy") {
            updatePair(pair, {
              position: {
                entry_price: msg.price,
                qty: msg.qty,
                entry_ts: Date.now() / 1000,
                candles_held: 0,
                entry_mode: msg.entry_mode ?? "momentum",
                peak_price: msg.price,
              },
            });
            if (selectedPairRef.current === pair) setSubView("trading");
          }
          break;
        case "trade_closed":
          setPairStates(prev => {
            const ps = prev[pair];
            return { ...prev, [pair]: {
              ...ps,
              position: null,
              trades: [...ps.trades, {
                ...msg,
                entry_price: msg.entry_price ?? msg.entry,
                exit_price: msg.exit_price ?? msg.exit,
                entry_ts: msg.entry_ts ?? ps.position?.entry_ts,
                exit_ts: msg.exit_ts ?? Date.now() / 1000,
              }],
            }};
          });
          setSellAlerts(prev => { const n = { ...prev }; delete n[pair]; return n; });
          break;
        case "session_stats":
          updatePair(pair, { sessionStats: msg });
          break;
        case "engine_halted":
          updatePair(pair, { engineState: "halted", position: null });
          break;
        case "engine_state":
          updatePair(pair, { engineState: msg.state ?? "idle" });
          break;
        case "sell_failed":
          setSellAlerts(prev => ({ ...prev, [pair]: { level: "retry", reason: msg.reason,
                                                       pair: msg.pair, retry: msg.retry } }));
          break;
        case "sell_abandoned":
          setSellAlerts(prev => ({ ...prev, [pair]: { level: "abandoned", reason: msg.reason,
                                                       pair: msg.pair, retries: msg.retries } }));
          break;
        case "scan_started":
          setScanInProgress(true);
          break;
        case "token_update":
          if (msg.pair && SEED_PAIRS.includes(msg.pair)) {
            setTokens(prev => {
              const idx = prev.findIndex(t => t.pair === msg.pair);
              if (idx >= 0) {
                const next = [...prev];
                next[idx] = { ...next[idx], ...msg };
                return next;
              }
              return prev;
            });
          }
          break;
        case "watchlist_update":
          setTokens((msg.tokens ?? []).filter(t => SEED_PAIRS.includes(t.pair)));
          setLastScanTs(Date.now() / 1000);
          setScanInProgress(false);
          break;
        default:
          break;
      }
    } catch (e) {
      console.error(`[APEX][${pair}] parse error`, e);
    }
  }, []);

  useEffect(() => {
    const reconnectTimers = {};

    function connect(pair, port) {
      const ws = new WebSocket(`ws://127.0.0.1:${port}`);
      ws.onopen = () => {
        wsRefs.current[pair] = ws;
        updatePair(pair, { connected: true });
      };
      ws.onclose = () => {
        if (wsRefs.current[pair] === ws) {
          wsRefs.current[pair] = null;
        }
        updatePair(pair, { connected: false });
        reconnectTimers[pair] = setTimeout(() => connect(pair, port), 3000);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (evt) => handlePairMessage(pair, evt);
    }

    SEED_PAIRS.forEach((pair, idx) => {
      connect(pair, APEX_WS_BASE + idx);
    });

    return () => {
      Object.values(reconnectTimers).forEach(t => clearTimeout(t));
      Object.values(wsRefs.current).forEach(ws => ws?.close());
      wsRefs.current = {};
    };
  }, [handlePairMessage]);

  function handleToggle(pair) {
    const ws = wsRefs.current[pair];
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const currentlyEnabled = pairStates[pair]?.enabled ?? true;
    if (currentlyEnabled) {
      ws.send(JSON.stringify({ type: "disable_pair" }));
      updatePair(pair, { enabled: false });
    } else {
      ws.send(JSON.stringify({ type: "enable_pair" }));
      updatePair(pair, { enabled: true });
    }
  }

  function handleDisable(pair) {
    const ws = wsRefs.current[pair];
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "disable_pair" }));
    }
    updatePair(pair, { enabled: false });
  }

  const selectedState = pairStates[selectedPair] ?? buildInitialPairState()[SEED_PAIRS[0]];

  return (
    <div style={{ padding: "16px 12px", background: C.bg, minHeight: "100%" }}>
      {/* Sub-nav */}
      <div style={{ display: "flex", gap: 0, marginBottom: 20,
                    borderBottom: `1px solid ${C.border}` }}>
        {[["trading", "TRADING"], ["discover", "DISCOVER"]].map(([key, label]) => (
          <button
            key={key}
            onClick={() => setSubView(key)}
            style={{
              padding: "10px 28px", border: "none", background: "transparent",
              fontFamily: C.mono, fontSize: 15, fontWeight: 700, letterSpacing: "0.08em",
              color: subView === key ? C.purple : C.muted,
              borderBottom: `2px solid ${subView === key ? C.purple : "transparent"}`,
              cursor: "pointer", transition: "all 0.2s",
            }}
          >
            {label}
          </button>
        ))}
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6,
                      paddingBottom: 8 }}>
          <div style={{
            width: 7, height: 7, borderRadius: "50%",
            background: anyConnected ? C.accent : C.danger,
            boxShadow: anyConnected ? `0 0 6px ${C.accent}` : "none",
          }} />
          <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>
            {anyConnected ? `APEX · ${connectedCount}/${SEED_PAIRS.length} pairs` : "disconnected"}
          </span>
        </div>
      </div>

      {/* Sell alerts */}
      {Object.entries(sellAlerts).map(([pair, alert]) => (
        <div key={pair} style={{
          padding: "10px 16px", marginBottom: 8, borderRadius: 8,
          background: alert.level === "abandoned" ? `${C.danger}20` : `${C.warn}20`,
          border: `1px solid ${alert.level === "abandoned" ? C.danger : C.warn}60`,
          display: "flex", justifyContent: "space-between", alignItems: "center",
        }}>
          <span style={{ fontFamily: C.mono, fontSize: 12,
                         color: alert.level === "abandoned" ? C.danger : C.warn }}>
            {alert.level === "abandoned"
              ? `SELL ABANDONED after ${alert.retries} retries — close ${alert.pair} manually`
              : `Sell retry ${alert.retry} — ${alert.reason} (${pair})`}
          </span>
          <button onClick={() => setSellAlerts(prev => { const n = { ...prev }; delete n[pair]; return n; })}
                  style={{ background: "transparent", border: "none", color: C.muted,
                           cursor: "pointer", fontSize: 14 }}>✕</button>
        </div>
      ))}

      {subView === "trading" && (
        <div>
          {/* Pair selector chips */}
          <div style={{
            display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap",
          }}>
            {SEED_PAIRS.map(pair => {
              const ps = pairStates[pair];
              const isSelected = selectedPair === pair;
              const conf = ps?.confidence ?? 0;
              const confColor = conf >= 0.7 ? C.accent : conf >= 0.5 ? C.warn : C.muted;
              return (
                <button
                  key={pair}
                  onClick={() => setSelectedPair(pair)}
                  style={{
                    display: "flex", alignItems: "center", gap: 8,
                    padding: "7px 14px", borderRadius: 8,
                    border: `1px solid ${isSelected ? C.purple : C.border}`,
                    background: isSelected ? `${C.purple}20` : C.panel,
                    cursor: "pointer", transition: "all 0.2s",
                  }}
                >
                  <div style={{
                    width: 6, height: 6, borderRadius: "50%",
                    background: ps?.connected ? (ps.enabled ? C.accent : C.warn) : C.danger,
                    flexShrink: 0,
                  }} />
                  <span style={{ fontFamily: C.mono, fontSize: 13, fontWeight: 700,
                                 color: isSelected ? C.purple : C.text }}>{pair}</span>
                  {ps?.connected && conf > 0 && (
                    <span style={{ fontFamily: C.mono, fontSize: 10, color: confColor }}>
                      {(conf * 100).toFixed(0)}%
                    </span>
                  )}
                  {ps?.position && (
                    <span style={{ fontFamily: C.mono, fontSize: 9, color: C.purple,
                                   background: `${C.purple}20`, padding: "1px 5px",
                                   borderRadius: 3 }}>POS</span>
                  )}
                </button>
              );
            })}
          </div>

          <TradingView
            state={selectedState}
            connected={selectedState.connected}
            pair={selectedPair}
            onDisable={() => handleDisable(selectedPair)}
            candleInterval={selectedState.candleInterval}
          />
        </div>
      )}

      {subView === "discover" && (
        <DiscoverView
          tokens={tokens}
          pairStates={pairStates}
          onToggle={handleToggle}
          anyConnected={anyConnected}
          lastScanTs={lastScanTs}
          wsRefs={wsRefs}
          scanInProgress={scanInProgress}
        />
      )}
    </div>
  );
}
