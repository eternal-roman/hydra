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

const APEX_WS = "ws://localhost:8766";
const APEX_DAILY_CAP_USD = 30;
const POSITION_SIZE_USD = 300;

const SEED_PAIRS = [
  // Meme tokens
  "WIF/USD", "POPCAT/USD", "BONK/USD", "PEPE/USD", "PLAY/USD", "LION/USD",
  // Gaming / metaverse
  "SAND/USD", "MANA/USD", "ENJ/USD", "CHZ/USD",
  // Newer ecosystem tokens Kraken actively promotes
  "NEAR/USD", "APT/USD", "OP/USD", "ARB/USD", "INJ/USD",
  "TIA/USD", "SEI/USD", "PYTH/USD",
];

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

function CandleChart({ bars, height = 160 }) {
  const [containerRef, cw] = useContainerWidth(CHART_FALLBACK_W);

  if (!bars || bars.length === 0) {
    return (
      <div ref={containerRef} style={{ width: "100%", height, display: "flex",
                    alignItems: "center", justifyContent: "center" }}>
        <span style={{ fontFamily: C.mono, fontSize: 11, color: C.muted }}>
          Waiting for candles… (warmup: 15 bars)
        </span>
      </div>
    );
  }
  const priceGutter = 54;
  const pad = { top: 10, bottom: 10, left: 6, right: priceGutter };
  const innerW = cw - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const n = bars.length;
  const slotW = innerW / n;
  const candleW = Math.max(4, Math.min(slotW * 0.78, 20));
  const minP = Math.min(...bars.map(b => b.low));
  const maxP = Math.max(...bars.map(b => b.high));
  const range = maxP - minP || minP * 0.01 || 1;

  function py(p) { return pad.top + innerH - ((p - minP) / range) * innerH; }

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
    </svg>
    </div>
  );
}

function VolumeHistogram({ bars, height = 44 }) {
  const [containerRef, cw] = useContainerWidth(CHART_FALLBACK_W);

  if (!bars || bars.length === 0) return <div ref={containerRef} />;
  const priceGutter = 54;
  const drawW = cw - priceGutter;
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
        const cx = 6 + (i + 0.5) * slotW;
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

// ─── Gate Dot ─────────────────────────────────────────────────────────────────

function GateDot({ pass, label, value }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "5px 0",
                  borderBottom: `1px solid ${C.border}20` }}>
      <div style={{
        width: 9, height: 9, borderRadius: "50%",
        background: pass ? C.accent : "#3f3f46",
        boxShadow: pass ? `0 0 6px ${C.accent}80` : "none",
        flexShrink: 0, transition: "all 0.3s",
      }} />
      <span style={{ fontFamily: C.mono, fontSize: 11, color: pass ? C.text : C.muted,
                     minWidth: 90, flex: 1 }}>{label}</span>
      <span style={{ fontFamily: C.mono, fontSize: 11,
                     color: pass ? C.accent : C.muted }}>{value ?? "—"}</span>
    </div>
  );
}

function SignalBanner({ allPass, engineState }) {
  if (engineState === "warmup") {
    return (
      <div style={{
        padding: "12px 0", borderRadius: 8, textAlign: "center",
        background: "#1c1917", border: `1px solid ${C.warn}30`,
        fontFamily: C.mono, fontSize: 12, color: C.warn, marginTop: 10,
      }}>
        ⏳ WARMING UP ({15} bars req.)
      </div>
    );
  }
  if (engineState === "halted") {
    return (
      <div style={{
        padding: "12px 0", borderRadius: 8, textAlign: "center",
        background: "#1c0a0a", border: `1px solid ${C.danger}30`,
        fontFamily: C.mono, fontSize: 12, color: C.danger, marginTop: 10,
      }}>
        ⛔ DAILY CAP HIT
      </div>
    );
  }
  return (
    <div style={{
      padding: "12px 0", borderRadius: 8, textAlign: "center",
      background: allPass ? `${C.accent}12` : "#18181b",
      border: `1px solid ${allPass ? C.accent + "50" : C.border}`,
      fontFamily: C.mono, fontSize: 16, fontWeight: 700,
      color: allPass ? C.accent : C.muted,
      marginTop: 10, transition: "all 0.3s ease",
      boxShadow: allPass ? `0 0 20px ${C.accent}20` : "none",
    }}>
      {allPass ? "⚡ BUY SIGNAL" : "— HOLD —"}
    </div>
  );
}

// ─── Position Panel ───────────────────────────────────────────────────────────

function PositionPanel({ position, midPrice }) {
  if (!position) {
    return (
      <div style={{ padding: 16, background: C.panel, borderRadius: 8,
                    border: `1px solid ${C.border}`, height: "100%",
                    display: "flex", flexDirection: "column", justifyContent: "center",
                    alignItems: "center", gap: 6 }}>
        <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted,
                      textTransform: "uppercase", letterSpacing: "0.1em" }}>Position</div>
        <div style={{ fontFamily: C.mono, fontSize: 13, color: C.muted }}>No open position</div>
        <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted + "80" }}>
          Waiting for all 5 gates to pass
        </div>
      </div>
    );
  }
  const entryPct = ((midPrice - position.entry_price) / position.entry_price) * 100;
  const progress = Math.max(0, Math.min(100,
    ((entryPct - (-1.3)) / (2.5 - (-1.3))) * 100));
  const pnlColor = entryPct >= 0 ? C.accent : C.danger;
  const candles = position.candles_held ?? 0;

  return (
    <div style={{ padding: 16, background: C.panel, borderRadius: 8,
                  border: `1px solid ${C.purple}50` }}>
      <div style={{ fontFamily: C.mono, fontSize: 10, color: C.purple, marginBottom: 12,
                    textTransform: "uppercase", letterSpacing: "0.1em" }}>Open Position</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 14 }}>
        {[
          ["Entry", position.entry_price.toFixed(6)],
          ["Mid", midPrice.toFixed(6)],
          ["Qty", position.qty?.toFixed(2) ?? "—"],
          ["Notional", `$${(position.notional_usd ?? 300).toFixed(0)}`],
        ].map(([l, v]) => (
          <div key={l}>
            <div style={{ fontFamily: C.mono, fontSize: 9, color: C.muted }}>{l}</div>
            <div style={{ fontFamily: C.mono, fontSize: 12, color: C.text }}>{v}</div>
          </div>
        ))}
      </div>
      <div style={{ textAlign: "center", marginBottom: 14 }}>
        <span style={{ fontFamily: C.mono, fontSize: 22, fontWeight: 700, color: pnlColor }}>
          {entryPct >= 0 ? "+" : ""}{entryPct.toFixed(2)}%
        </span>
      </div>
      {/* Exit watch levels */}
      <div style={{ display: "flex", justifyContent: "space-between",
                    padding: "6px 10px", background: C.bg, borderRadius: 6,
                    marginBottom: 10, fontFamily: C.mono, fontSize: 10 }}>
        <span style={{ color: C.danger }}>▼ stop {(position.entry_price * 0.987).toFixed(6)}</span>
        <span style={{ color: C.accent }}>▲ target {(position.entry_price * 1.025).toFixed(6)}</span>
      </div>
      {/* Progress bar */}
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
        <span style={{ fontFamily: C.mono, fontSize: 9, color: C.danger }}>−1.3%</span>
        <span style={{ fontFamily: C.mono, fontSize: 9, color: C.muted }}>progress to target</span>
        <span style={{ fontFamily: C.mono, fontSize: 9, color: C.accent }}>+2.5%</span>
      </div>
      <div style={{ height: 8, background: "#27272a", borderRadius: 4, overflow: "hidden",
                    marginBottom: 8 }}>
        <div style={{
          height: "100%", width: `${progress}%`,
          background: entryPct >= 0 ? C.accent : C.danger,
          transition: "width 0.5s ease",
        }} />
      </div>
      <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, textAlign: "center" }}>
        {candles} candle{candles !== 1 ? "s" : ""} held
        {" · "}time stop in {Math.max(0, 3 - candles)} more
      </div>
    </div>
  );
}

// ─── Session Stats ────────────────────────────────────────────────────────────

function SessionStats({ stats, dailyCap }) {
  const remaining = dailyCap + (stats?.daily_loss ?? 0);
  const usedPct = dailyCap > 0 ? Math.max(0, Math.min(100,
    ((dailyCap - remaining) / dailyCap) * 100)) : 0;
  return (
    <div style={{ padding: 16, background: C.panel, borderRadius: 8,
                  border: `1px solid ${C.border}`, marginTop: 8 }}>
      <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginBottom: 10,
                    textTransform: "uppercase", letterSpacing: "0.1em" }}>Session</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 10 }}>
        {[
          ["Net P&L", `$${(stats?.session_pnl ?? 0).toFixed(2)}`, (stats?.session_pnl ?? 0) >= 0 ? C.accent : C.danger],
          ["Win Rate", `${((stats?.win_rate ?? 0) * 100).toFixed(0)}%`, C.text],
          ["Trades", String(stats?.trade_count ?? 0), C.text],
          ["Cap Left", `$${remaining.toFixed(2)}`, remaining > 10 ? C.accent : C.danger],
        ].map(([label, val, color]) => (
          <div key={label}>
            <div style={{ fontFamily: C.mono, fontSize: 9, color: C.muted }}>{label}</div>
            <div style={{ fontFamily: C.mono, fontSize: 14, fontWeight: 700, color }}>{val}</div>
          </div>
        ))}
      </div>
      <div style={{ height: 5, background: "#27272a", borderRadius: 3, overflow: "hidden" }}>
        <div style={{ height: "100%", width: `${usedPct}%`, background: C.danger,
                      transition: "width 0.5s" }} />
      </div>
      <div style={{ fontFamily: C.mono, fontSize: 9, color: C.muted, marginTop: 3 }}>
        daily cap: ${dailyCap.toFixed(0)} · {usedPct.toFixed(0)}% used
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

function TradingView({ state, dailyCap, connected, pair, onStop }) {
  const { gates, position, midPrice, obi, engineState, sessionStats, trades, bars, spreadBps } = state;

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

  return (
    <div>
      {/* Control row */}
      <div style={{
        display: "flex", alignItems: "center", gap: 16, marginBottom: 16,
        padding: "10px 16px", background: C.panel, borderRadius: 8,
        border: `1px solid ${C.border}`,
      }}>
        <span style={{ fontFamily: C.mono, fontSize: 15, fontWeight: 700, color: C.text }}>
          {pair ?? "—"}
        </span>
        <span style={{ fontFamily: C.mono, fontSize: 18, fontWeight: 700,
                       color: midPrice > 0 ? C.text : C.muted }}>
          {midPrice > 0 ? `$${midPrice.toFixed(6)}` : "—"}
        </span>
        {spreadBps > 0 && (
          <span style={{ fontFamily: C.mono, fontSize: 11, color: C.muted }}>
            spread {spreadBps.toFixed(1)} bps
          </span>
        )}
        <div style={{
          display: "flex", alignItems: "center", gap: 6,
          padding: "3px 10px", borderRadius: 20,
          background: engineState === "running" ? `${C.accent}15`
                    : engineState === "warmup" ? `${C.warn}15`
                    : engineState === "halted" ? `${C.danger}15` : "#27272a",
        }}>
          <div style={{
            width: 7, height: 7, borderRadius: "50%",
            background: engineState === "running" ? C.accent
                       : engineState === "warmup" ? C.warn
                       : engineState === "halted" ? C.danger : C.muted,
          }} />
          <span style={{ fontFamily: C.mono, fontSize: 10,
                         color: engineState === "running" ? C.accent
                               : engineState === "warmup" ? C.warn
                               : engineState === "halted" ? C.danger : C.muted }}>
            {engineState}
          </span>
        </div>
        <div style={{ marginLeft: "auto" }}>
          <button
            onClick={onStop}
            disabled={!["running", "warmup"].includes(engineState)}
            style={{
              padding: "6px 18px", borderRadius: 6,
              border: `1px solid ${C.danger}50`, background: "transparent",
              color: C.danger, fontFamily: C.mono, fontSize: 12, fontWeight: 700,
              cursor: ["running", "warmup"].includes(engineState) ? "pointer" : "not-allowed",
              opacity: ["running", "warmup"].includes(engineState) ? 1 : 0.3,
            }}
          >
            ■ STOP
          </button>
        </div>
      </div>

      {/* Main 2-column grid — chart left, panels right */}
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
            <span>5-min Candles</span>
            {bars && bars.length > 0 && (
              <span style={{ fontSize: 9, opacity: 0.6 }}>{bars.length} bars</span>
            )}
          </div>
          <CandleChart bars={bars} height={340} />
          <VolumeHistogram bars={bars} height={72} />
          <div style={{ padding: "6px 14px 10px" }}>
            <OBIGauge obi={obi ?? 0} />
          </div>
        </div>

        {/* RIGHT — position, session, gates */}
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <PositionPanel position={position} midPrice={midPrice ?? 0} />
          <SessionStats stats={sessionStats ?? {}} dailyCap={dailyCap} />
          <div style={{ padding: 16, background: C.panel, borderRadius: 8,
                        border: `1px solid ${C.border}`, flex: 1 }}>
            <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginBottom: 8,
                          textTransform: "uppercase", letterSpacing: "0.1em" }}>Entry Gates</div>
            <GateDot pass={gates?.volume_spike} label="Vol 1.8×"
                     value={gates?.vol_ema_value ? `${(gates.vol_ema_value / 1000).toFixed(1)}k` : null} />
            <GateDot pass={gates?.obi} label="OBI >0.20"
                     value={obi != null ? obi.toFixed(3) : null} />
            <GateDot pass={gates?.vwap_align} label="VWAP align"
                     value={gates?.vwap_value ? `$${parseFloat(gates.vwap_value).toFixed(5)}` : null} />
            <GateDot pass={gates?.rsi_window} label="RSI 45–78"
                     value={gates?.rsi_value != null ? String(gates.rsi_value) : null} />
            <GateDot pass={gates?.ask_wall_clear} label="Ask wall <$500"
                     value={null} />
            <SignalBanner allPass={gates?.all_pass ?? false} engineState={engineState} />
          </div>
        </div>
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

// ─── Discover View ────────────────────────────────────────────────────────────

function DiscoverView({ tokens, onStartEngine, enginePair, connected, lastScanTs, wsRef, sessionStats, dailyCap, scanInProgress }) {
  const [levers, setLevers] = useState({});
  const [activeToken, setActiveToken] = useState(enginePair);

  useEffect(() => { setActiveToken(enginePair); }, [enginePair]);

  function handleToggle(pair, posSize) {
    if (activeToken === pair) {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: "stop_engine" }));
      }
      setActiveToken(null);
    } else {
      setActiveToken(pair);
      onStartEngine(pair, posSize);
    }
  }

  function handleScanNow() {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "scan_now" }));
    }
  }

  function ratioColor(r) {
    if (!r || r < 3) return C.muted;
    if (r >= 7) return C.danger;
    if (r >= 4) return C.warn;
    return C.blue;
  }

  function fmtVol(v) {
    if (!v) return "—";
    if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
    if (v >= 1_000) return `${(v / 1_000).toFixed(1)}k`;
    return v.toFixed(0);
  }

  // Capital summary data
  const capRemaining = dailyCap + (sessionStats?.daily_loss ?? 0);
  const capUsed = Math.max(0, dailyCap - capRemaining);
  // Gate on daily-loss budget (capRemaining > 0), not account balance
  const hasCapital = !connected || capRemaining > 0;

  // Sort by anomaly ratio when connected; preserve seed order otherwise
  const displayTokens = connected
    ? [...tokens].sort((a, b) => (b.anomaly_ratio ?? 0) - (a.anomaly_ratio ?? 0))
    : tokens;

  return (
    <div>
      {/* Scan header */}
      {connected ? (
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

      {/* Competition table */}
      <div style={{ background: C.panel, borderRadius: 8, border: `1px solid ${C.border}`,
                    marginBottom: 12, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: C.mono }}>
          <thead>
            <tr style={{ borderBottom: `1px solid ${C.border}` }}>
              {["Token", "Vol 24h", "7d Baseline", "Anomaly", "Type", "Capital", "Trade"].map(h => (
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
              const isAnomaly = (token.anomaly_ratio ?? 0) >= 5;
              const isActive = activeToken === token.pair;
              const isEnginePair = enginePair === token.pair;
              const capOk = hasCapital;
              const canToggle = connected && capOk && isEnginePair;
              const evenRow = idx % 2 === 0;

              return (
                <tr key={token.pair} style={{
                  borderBottom: `1px solid ${C.border}25`,
                  background: isActive ? `${C.accent}12`
                            : isAnomaly ? `${C.warn}0c`
                            : evenRow ? "#1a1a1f" : C.panel,
                  borderLeft: isAnomaly ? `3px solid ${C.warn}` : isActive ? `3px solid ${C.accent}` : "3px solid transparent",
                  boxShadow: isActive ? `inset 0 1px 0 ${C.accent}15, inset 0 -1px 0 ${C.accent}15`
                           : `inset 0 1px 0 #ffffff04`,
                  transition: "background 0.3s",
                }}>
                  <td style={{ padding: "12px 14px" }}>
                    <div style={{ fontFamily: C.mono, fontSize: 16, fontWeight: 700,
                                  color: C.text }}>{token.pair}</div>
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
                    {token.anomaly_ratio >= 3 ? (
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
                  <td style={{ padding: "12px 14px" }}>
                    <div style={{
                      fontFamily: C.mono, fontSize: 14,
                      color: !connected ? C.muted : capOk ? C.accent : C.danger,
                    }}>
                      {connected ? (capOk ? `$${POSITION_SIZE_USD} ✓` : `$${POSITION_SIZE_USD} ✗`) : "—"}
                    </div>
                  </td>
                  <td style={{ padding: "10px 12px" }}>
                    <button
                      onClick={() => canToggle && handleToggle(token.pair, levers[token.pair] ?? POSITION_SIZE_USD)}
                      disabled={!canToggle}
                      title={!connected ? "Connect APEX engine first"
                           : !isEnginePair ? `Restart APEX with --pair ${token.pair} to trade`
                           : !capOk ? "Insufficient capital"
                           : isActive ? "Stop trading this token"
                           : "Start trading this token"}
                      style={{
                        display: "inline-flex", alignItems: "center", justifyContent: "center",
                        gap: 8, width: 90, padding: "8px 0", borderRadius: 6, border: "none",
                        background: isActive ? `${C.accent}20`
                                  : canToggle ? `${C.purple}20`
                                  : "#27272a",
                        color: isActive ? C.accent : canToggle ? C.purple : C.muted,
                        fontFamily: C.mono, fontSize: 13, fontWeight: 700,
                        cursor: canToggle ? "pointer" : "not-allowed",
                        transition: "all 0.2s",
                      }}
                    >
                      {/* Toggle switch — square style */}
                      <div style={{
                        width: 36, height: 20, borderRadius: 4, position: "relative",
                        background: isActive ? C.accent : canToggle ? "#3f3f46" : "#27272a",
                        border: `1px solid ${isActive ? C.accent : canToggle ? C.purple + "60" : C.border}`,
                        transition: "all 0.2s", flexShrink: 0,
                      }}>
                        <div style={{
                          position: "absolute", top: 3,
                          left: isActive ? 19 : 3,
                          width: 12, height: 12, borderRadius: 2,
                          background: isActive ? "#fff" : canToggle ? C.purple : C.muted,
                          transition: "left 0.2s",
                        }} />
                      </div>
                      {isActive ? "ON" : !isEnginePair ? "—" : "OFF"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Position size lever (only for anomalous tokens) */}
      {connected && tokens.filter(t => (t.anomaly_ratio ?? 0) >= 5).map(token => (
        <div key={`lever-${token.pair}`} style={{
          padding: 16, background: C.panel, borderRadius: 8,
          border: `1px solid ${C.purple}30`, marginBottom: 8,
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
            <span style={{ fontFamily: C.mono, fontSize: 12, fontWeight: 700,
                           color: C.text }}>{token.pair} — position lever</span>
            <span style={{ fontFamily: C.mono, fontSize: 11, color: C.muted }}>
              ${levers[token.pair] ?? POSITION_SIZE_USD}/trade ·{" "}
              ${((levers[token.pair] ?? POSITION_SIZE_USD) * 5 * 2).toLocaleString()}/day proj.
            </span>
          </div>
          <input
            type="range" min={300} max={3000} step={100}
            value={levers[token.pair] ?? POSITION_SIZE_USD}
            onChange={e => setLevers(l => ({ ...l, [token.pair]: Number(e.target.value) }))}
            style={{ width: "100%", accentColor: C.purple, marginBottom: 6 }}
          />
          <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>
            Competition type:{" "}
            <span style={{ color: C.warn }}>
              {token.competition_type || "volume (inferred)"}
              {!token.competition_type_confirmed ? " *" : ""}
            </span>
            {" — "}
            <a href="https://www.kraken.com/promotions" target="_blank" rel="noopener noreferrer"
               style={{ color: C.blue }}>verify on Kraken</a>
          </div>
        </div>
      ))}

      {/* Capital summary */}
      {connected && (
        <div style={{
          display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10,
          padding: 16, background: C.panel, borderRadius: 8, border: `1px solid ${C.border}`,
        }}>
          {[
            ["Available", `$${capRemaining.toFixed(2)}`, capRemaining > 0 ? C.accent : C.danger],
            ["Locked", activeToken ? `$${POSITION_SIZE_USD}` : "$0", C.text],
            ["Daily Cap", `$${dailyCap.toFixed(0)}`, C.muted],
            ["Used Today", `$${capUsed.toFixed(2)}`, capUsed > 0 ? C.danger : C.muted],
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

      {/* Offline seed note */}
      {!connected && (
        <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted + "80",
                      textAlign: "center", marginTop: 12 }}>
          {tokens.length} seed pairs · volume data appears once APEX connects and scans
        </div>
      )}
    </div>
  );
}

// ─── Competition Modal ────────────────────────────────────────────────────────

function CompetitionModal({ alert, onStart, onDismiss }) {
  if (!alert) return null;
  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.8)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 9999,
    }}>
      <div style={{
        background: C.panel, border: `1px solid ${C.purple}60`, borderRadius: 12,
        padding: 28, maxWidth: 500, width: "90%",
        boxShadow: `0 0 40px ${C.purple}20`,
      }}>
        <div style={{ fontFamily: C.mono, fontSize: 11, color: C.purple, marginBottom: 6,
                      textTransform: "uppercase", letterSpacing: "0.1em" }}>
          ⚡ Competition Detected
        </div>
        <div style={{ fontFamily: C.sans, fontSize: 22, fontWeight: 700, color: C.text,
                      marginBottom: 16 }}>{alert.pair}</div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10,
                      marginBottom: 16 }}>
          {[
            ["24h Volume", `${(alert.volume / 1_000_000).toFixed(1)}M tokens`],
            ["7d Baseline", `${(alert.baseline / 1_000_000).toFixed(1)}M tokens`],
            ["Anomaly Ratio", `${alert.ratio?.toFixed(1)}× baseline`],
            ["Competition Type", `${alert.competition_type || "unknown"}${!alert.competition_type_confirmed ? " (inferred)" : ""}`],
          ].map(([l, v]) => (
            <div key={l} style={{ padding: 10, background: C.bg, borderRadius: 8 }}>
              <div style={{ fontFamily: C.mono, fontSize: 9, color: C.muted,
                            textTransform: "uppercase", letterSpacing: "0.07em",
                            marginBottom: 3 }}>{l}</div>
              <div style={{ fontFamily: C.mono, fontSize: 13, color: C.text }}>{v}</div>
            </div>
          ))}
        </div>
        <div style={{ padding: "10px 12px", background: "#0f1923", borderRadius: 8,
                      marginBottom: 16, fontFamily: C.mono, fontSize: 11, color: C.muted }}>
          Strategy: $300 position · +2.5% target · −1.3% stop · 5-min candles · max 3 candles held
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <button
            onClick={() => onStart(alert.pair)}
            style={{
              flex: 1, padding: "12px 0", borderRadius: 8, border: "none",
              background: C.purple, color: C.text,
              fontFamily: C.mono, fontSize: 13, fontWeight: 700, cursor: "pointer",
              boxShadow: `0 0 20px ${C.purple}40`,
            }}
          >
            Start APEX Engine
          </button>
          <button
            onClick={() => onDismiss(alert.pair)}
            style={{
              padding: "12px 20px", borderRadius: 8,
              border: `1px solid ${C.border}`, background: "transparent",
              color: C.muted, fontFamily: C.mono, fontSize: 12, cursor: "pointer",
            }}
          >
            Dismiss (2h)
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Root ─────────────────────────────────────────────────────────────────────

export default function MemeTab() {
  const [subView, setSubView] = useState("trading");
  const [connected, setConnected] = useState(false);
  const [engineState, setEngineState] = useState("idle");
  const [enginePair, setEnginePair] = useState(null);
  const [gates, setGates] = useState(null);
  const [position, setPosition] = useState(null);
  const [midPrice, setMidPrice] = useState(0);
  const [spreadBps, setSpreadBps] = useState(0);
  const [obi, setObi] = useState(0);
  const [sessionStats, setSessionStats] = useState(null);
  const [trades, setTrades] = useState([]);
  const [tokens, setTokens] = useState(() => SEED_PAIRS.map(p => ({ pair: p })));
  const [scanInProgress, setScanInProgress] = useState(false);
  const [bars, setBars] = useState([]);
  const [lastScanTs, setLastScanTs] = useState(null);
  const [pendingAlert, setPendingAlert] = useState(null);
  const wsRef = useRef(null);
  const reconnectRef = useRef(null);
  const connectRef = useRef(null);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;
    const ws = new WebSocket(APEX_WS);
    wsRef.current = ws;
    ws.onopen = () => setConnected(true);
    ws.onclose = () => {
      setConnected(false);
      reconnectRef.current = setTimeout(() => connectRef.current?.(), 5000);
    };
    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        switch (msg.type) {
          case "initial_state":
            setEngineState(msg.engine_state ?? "idle");
            if (msg.pair) setEnginePair(msg.pair);
            if (msg.position) setPosition(msg.position);
            if (msg.trades) setTrades(msg.trades);
            if (msg.session_pnl != null || msg.trade_count != null) {
              setSessionStats({
                session_pnl: msg.session_pnl ?? 0,
                daily_loss: msg.daily_loss ?? 0,
                trade_count: msg.trade_count ?? 0,
                win_rate: msg.win_rate ?? 0,
              });
            }
            break;
          case "warmup_progress":
            setEngineState("warmup");
            break;
          case "candle_history":
            setBars(msg.bars ?? []);
            break;
          case "bar_update":
            if (msg.bar) setBars(prev => {
              const deduped = prev.filter(b => b.ts !== msg.bar.ts);
              return [...deduped, msg.bar].slice(-100);
            });
            break;
          case "ticker":
            setMidPrice(msg.price ?? 0);
            setObi(msg.obi ?? 0);
            setSpreadBps(msg.spread_bps ?? 0);
            break;
          case "signal_state":
            setGates(msg.gates);
            setEngineState("running");
            if (msg.gates?.all_pass) setSubView("trading");
            break;
          case "position_update":
            setMidPrice(msg.price ?? 0);
            setObi(msg.obi ?? 0);
            setSpreadBps(msg.spread_bps ?? 0);
            if (msg.entry && typeof msg.entry === "object") {
              setPosition(p => p ? { ...p, ...msg.entry } : msg.entry);
            }
            break;
          case "order_placed":
            if (msg.side === "buy") {
              setPosition({ entry_price: msg.price, qty: msg.qty,
                            notional_usd: POSITION_SIZE_USD,
                            entry_ts: Date.now() / 1000, candles_held: 0 });
              setSubView("trading");
            }
            break;
          case "trade_closed":
            setPosition(null);
            setTrades(prev => [...prev, {
              ...msg,
              entry_price: msg.entry_price ?? msg.entry,
              exit_price: msg.exit_price ?? msg.exit,
            }]);
            break;
          case "session_stats":
            setSessionStats(msg);
            break;
          case "engine_halted":
            setEngineState("halted");
            setPosition(null);
            break;
          case "engine_state":
            setEngineState(msg.state ?? "idle");
            break;
          case "competition_alert":
            setPendingAlert(msg);
            break;
          case "scan_started":
            setScanInProgress(true);
            break;
          case "token_update":
            if (msg.pair) {
              setTokens(prev => {
                const idx = prev.findIndex(t => t.pair === msg.pair);
                if (idx >= 0) {
                  const next = [...prev];
                  next[idx] = { ...next[idx], ...msg };
                  return next;
                }
                return [...prev, msg];
              });
            }
            break;
          case "watchlist_update":
            setTokens(msg.tokens ?? []);
            setLastScanTs(Date.now() / 1000);
            setScanInProgress(false);
            break;
          default:
            break;
        }
      } catch (e) {
        console.error("[APEX] parse error", e);
      }
    };
  }, []);

  useEffect(() => {
    connectRef.current = connect;
    connect();
    return () => {
      clearTimeout(reconnectRef.current);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [connect]);

  function handleStartEngine(pair) {
    setPendingAlert(null);
    setEnginePair(pair);
    setEngineState("warmup");
    setSubView("trading");
  }

  function handleStop() {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "stop_engine" }));
    }
    setEngineState("idle");
    setPosition(null);
  }

  function handleDismiss(pair) {
    setPendingAlert(null);
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "dismiss_alert", pair }));
    }
  }

  const tradingState = { gates, position, midPrice, obi, engineState,
                          sessionStats, trades, bars, spreadBps };

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
            background: connected ? C.accent : C.danger,
            boxShadow: connected ? `0 0 6px ${C.accent}` : "none",
          }} />
          <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>
            {connected ? `APEX ${enginePair ? `· ${enginePair}` : "· idle"}` : "disconnected"}
          </span>
        </div>
      </div>

      {subView === "trading" && (
        <TradingView
          state={tradingState}
          dailyCap={APEX_DAILY_CAP_USD}
          connected={connected}
          pair={enginePair}
          onStop={handleStop}
        />
      )}
      {subView === "discover" && (
        <DiscoverView
          tokens={tokens}
          onStartEngine={handleStartEngine}
          enginePair={enginePair}
          connected={connected}
          lastScanTs={lastScanTs}
          wsRef={wsRef}
          sessionStats={sessionStats}
          dailyCap={APEX_DAILY_CAP_USD}
          scanInProgress={scanInProgress}
        />
      )}

      <CompetitionModal
        alert={pendingAlert}
        onStart={handleStartEngine}
        onDismiss={handleDismiss}
      />
    </div>
  );
}
