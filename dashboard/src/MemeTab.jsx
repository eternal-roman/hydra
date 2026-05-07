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

const SEED_PAIRS = [
  "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD",
  "DOT/USD", "LINK/USD", "AVAX/USD", "ATOM/USD", "NEAR/USD",
  "FIL/USD", "APT/USD", "OP/USD", "ARB/USD", "INJ/USD",
  "TIA/USD", "SEI/USD", "PYTH/USD", "WIF/USD", "POPCAT/USD",
  "BONK/USD", "PEPE/USD", "PLAY/USD", "LION/USD",
  "MATIC/USD", "SAND/USD", "MANA/USD", "ENJ/USD", "CHZ/USD",
];

function GateDot({ pass, label, value }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0" }}>
      <div style={{
        width: 8, height: 8, borderRadius: "50%",
        background: pass ? C.accent : C.danger,
        flexShrink: 0,
      }} />
      <span style={{ fontFamily: C.mono, fontSize: 11, color: C.muted, minWidth: 100 }}>{label}</span>
      <span style={{ fontFamily: C.mono, fontSize: 11, color: C.text }}>{value ?? "—"}</span>
    </div>
  );
}

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

function WatchlistSeed() {
  return (
    <div>
      <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginBottom: 12,
                    textTransform: "uppercase", letterSpacing: "0.1em" }}>
        Watching {SEED_PAIRS.length} pairs · live anomaly scan on connect
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(120px, 1fr))", gap: 8 }}>
        {SEED_PAIRS.map(pair => {
          const base = pair.split("/")[0];
          return (
            <div key={pair} style={{
              padding: "8px 12px", background: C.panel, borderRadius: 6,
              border: `1px solid ${C.border}`,
            }}>
              <div style={{ fontFamily: C.mono, fontSize: 13, fontWeight: 700, color: C.text }}>
                {base}
              </div>
              <div style={{ fontFamily: C.mono, fontSize: 9, color: C.muted, marginTop: 1 }}>
                {pair}
              </div>
              <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginTop: 6 }}>
                vol: —
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function OBIGauge({ obi = 0 }) {
  const pct = ((obi + 1) / 2) * 100;
  const color = obi > 0.2 ? C.accent : obi < -0.2 ? C.danger : C.warn;
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
        <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>SELL</span>
        <span style={{ fontFamily: C.mono, fontSize: 11, color, fontWeight: 700 }}>
          OBI {obi >= 0 ? "+" : ""}{obi.toFixed(3)}
        </span>
        <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>BUY</span>
      </div>
      <div style={{ height: 6, background: "#27272a", borderRadius: 3, overflow: "hidden" }}>
        <div style={{
          height: "100%", width: `${pct}%`,
          background: `linear-gradient(90deg, ${C.danger}, ${color})`,
          transition: "width 0.5s ease",
        }} />
      </div>
    </div>
  );
}

function SignalBanner({ allPass, engineState }) {
  if (engineState === "warmup") {
    return (
      <div style={{
        padding: "10px 16px", borderRadius: 8, textAlign: "center",
        background: "#1c1917", border: `1px solid ${C.warn}40`,
        fontFamily: C.mono, fontSize: 13, color: C.warn,
      }}>
        ⏳ WARMING UP
      </div>
    );
  }
  const color = allPass ? C.accent : C.muted;
  const label = allPass ? "⚡ BUY SIGNAL" : "— HOLD —";
  return (
    <div style={{
      padding: "10px 16px", borderRadius: 8, textAlign: "center",
      background: allPass ? `${C.accent}18` : "#18181b",
      border: `1px solid ${allPass ? C.accent + "60" : C.border}`,
      fontFamily: C.mono, fontSize: 15, fontWeight: 700, color,
      transition: "all 0.3s ease",
    }}>
      {label}
    </div>
  );
}

function PositionPanel({ position, midPrice }) {
  if (!position) {
    return (
      <div style={{ padding: 12, background: C.panel, borderRadius: 8, border: `1px solid ${C.border}` }}>
        <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginBottom: 8,
                      textTransform: "uppercase", letterSpacing: "0.1em" }}>Position</div>
        <div style={{ fontFamily: C.mono, fontSize: 12, color: C.muted, textAlign: "center",
                      padding: "20px 0" }}>No open position</div>
      </div>
    );
  }
  const entryPct = ((midPrice - position.entry_price) / position.entry_price) * 100;
  const targetPct = 2.5;
  const stopPct = -1.3;
  const progress = Math.max(0, Math.min(100, ((entryPct - stopPct) / (targetPct - stopPct)) * 100));
  const pnlColor = entryPct >= 0 ? C.accent : C.danger;
  return (
    <div style={{ padding: 12, background: C.panel, borderRadius: 8, border: `1px solid ${C.purple}40` }}>
      <div style={{ fontFamily: C.mono, fontSize: 10, color: C.purple, marginBottom: 8,
                    textTransform: "uppercase", letterSpacing: "0.1em" }}>Open Position</div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
        <span style={{ fontFamily: C.mono, fontSize: 11, color: C.muted }}>Entry</span>
        <span style={{ fontFamily: C.mono, fontSize: 11, color: C.text }}>{position.entry_price.toFixed(6)}</span>
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
        <span style={{ fontFamily: C.mono, fontSize: 11, color: C.muted }}>Mid</span>
        <span style={{ fontFamily: C.mono, fontSize: 11, color: C.text }}>{midPrice.toFixed(6)}</span>
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
        <span style={{ fontFamily: C.mono, fontSize: 12, color: C.muted }}>Unrealised</span>
        <span style={{ fontFamily: C.mono, fontSize: 13, fontWeight: 700, color: pnlColor }}>
          {entryPct >= 0 ? "+" : ""}{entryPct.toFixed(2)}%
        </span>
      </div>
      <div style={{ marginBottom: 4, display: "flex", justifyContent: "space-between" }}>
        <span style={{ fontFamily: C.mono, fontSize: 10, color: C.danger }}>▼ −1.3%</span>
        <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>progress</span>
        <span style={{ fontFamily: C.mono, fontSize: 10, color: C.accent }}>▲ +2.5%</span>
      </div>
      <div style={{ height: 6, background: "#27272a", borderRadius: 3, overflow: "hidden" }}>
        <div style={{
          height: "100%", width: `${progress}%`,
          background: entryPct >= 0 ? C.accent : C.danger,
          transition: "width 0.5s ease",
        }} />
      </div>
      <div style={{ marginTop: 8, fontFamily: C.mono, fontSize: 10, color: C.muted }}>
        {position.candles_held ?? 0} candle{(position.candles_held ?? 0) !== 1 ? "s" : ""} held · time stop at {3 - (position.candles_held ?? 0)} more
      </div>
    </div>
  );
}

function SessionStats({ stats, dailyCap }) {
  const remaining = dailyCap + (stats?.daily_loss ?? 0);
  const usedPct = dailyCap > 0 ? Math.max(0, Math.min(100, ((dailyCap - remaining) / dailyCap) * 100)) : 0;
  return (
    <div style={{ padding: 12, background: C.panel, borderRadius: 8, border: `1px solid ${C.border}`,
                  marginTop: 8 }}>
      <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginBottom: 8,
                    textTransform: "uppercase", letterSpacing: "0.1em" }}>Session</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginBottom: 8 }}>
        {[
          ["Net P&L", `$${(stats?.session_pnl ?? 0).toFixed(2)}`, (stats?.session_pnl ?? 0) >= 0 ? C.accent : C.danger],
          ["Win Rate", `${((stats?.win_rate ?? 0) * 100).toFixed(0)}%`, C.text],
          ["Trades", stats?.trade_count ?? 0, C.text],
          ["Cap Left", `$${remaining.toFixed(2)}`, remaining > 10 ? C.accent : C.danger],
        ].map(([label, val, color]) => (
          <div key={label}>
            <div style={{ fontFamily: C.mono, fontSize: 9, color: C.muted }}>{label}</div>
            <div style={{ fontFamily: C.mono, fontSize: 13, fontWeight: 700, color }}>{val}</div>
          </div>
        ))}
      </div>
      <div style={{ height: 4, background: "#27272a", borderRadius: 2, overflow: "hidden" }}>
        <div style={{ height: "100%", width: `${usedPct}%`, background: C.danger,
                      transition: "width 0.5s ease" }} />
      </div>
      <div style={{ fontFamily: C.mono, fontSize: 9, color: C.muted, marginTop: 2 }}>
        daily cap: ${dailyCap.toFixed(0)}
      </div>
    </div>
  );
}

function TradeLog({ trades }) {
  if (!trades || trades.length === 0) {
    return (
      <div style={{ fontFamily: C.mono, fontSize: 11, color: C.muted, padding: "16px 0",
                    textAlign: "center" }}>
        No closed trades this session
      </div>
    );
  }
  const cols = ["TIME", "ENTRY", "EXIT", "NET P&L", "REASON", "HOLD"];
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: C.mono, fontSize: 11 }}>
        <thead>
          <tr>
            {cols.map(c => (
              <th key={c} style={{ padding: "4px 8px", textAlign: "left", color: C.muted,
                                   borderBottom: `1px solid ${C.border}`, whiteSpace: "nowrap" }}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {[...trades].reverse().map((t) => (
            <tr key={t.exit_ts} style={{ borderBottom: `1px solid ${C.border}20` }}>
              <td style={{ padding: "4px 8px", color: C.muted }}>
                {new Date(t.exit_ts * 1000).toLocaleTimeString()}
              </td>
              <td style={{ padding: "4px 8px", color: C.text }}>{t.entry_price.toFixed(6)}</td>
              <td style={{ padding: "4px 8px", color: C.text }}>{t.exit_price.toFixed(6)}</td>
              <td style={{ padding: "4px 8px", fontWeight: 700,
                           color: t.net_pnl >= 0 ? C.accent : C.danger }}>
                {t.net_pnl >= 0 ? "+" : ""}${t.net_pnl.toFixed(2)}
              </td>
              <td style={{ padding: "4px 8px", color: C.muted }}>{t.exit_reason}</td>
              <td style={{ padding: "4px 8px", color: C.muted }}>{t.hold_candles}c</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TradingView({ state, dailyCap, connected }) {
  const { gates, position, midPrice, obi, engineState, sessionStats, trades } = state;
  if (!connected) {
    return (
      <div>
        <OfflineBanner />
        <div style={{ padding: 32, textAlign: "center", fontFamily: C.mono, fontSize: 12,
                      color: C.muted, background: C.panel, borderRadius: 8,
                      border: `1px solid ${C.border}` }}>
          Live signals, gates, and position tracking appear here once APEX is connected.
        </div>
      </div>
    );
  }
  return (
    <div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12, marginBottom: 16 }}>
        {/* Left — OBI gauge */}
        <div style={{ padding: 12, background: C.panel, borderRadius: 8,
                      border: `1px solid ${C.border}` }}>
          <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginBottom: 8,
                        textTransform: "uppercase", letterSpacing: "0.1em" }}>Market</div>
          <div style={{ fontFamily: C.mono, fontSize: 22, fontWeight: 700, color: C.text }}>
            {midPrice > 0 ? `$${midPrice.toFixed(6)}` : "—"}
          </div>
          <OBIGauge obi={obi ?? 0} />
        </div>

        {/* Middle — gates + signal */}
        <div style={{ padding: 12, background: C.panel, borderRadius: 8,
                      border: `1px solid ${C.border}` }}>
          <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginBottom: 8,
                        textTransform: "uppercase", letterSpacing: "0.1em" }}>Entry Gates</div>
          <GateDot pass={gates?.volume_spike} label="Vol Spike"
                   value={gates?.vol_ema_value ? `${(gates.vol_ema_value / 1000).toFixed(1)}k` : null} />
          <GateDot pass={gates?.obi} label="OBI >0.20"
                   value={obi != null ? obi.toFixed(3) : null} />
          <GateDot pass={gates?.vwap_align} label="VWAP Align"
                   value={gates?.vwap_value ? `$${parseFloat(gates.vwap_value).toFixed(5)}` : null} />
          <GateDot pass={gates?.rsi_window} label="RSI 45–78"
                   value={gates?.rsi_value} />
          <GateDot pass={gates?.ask_wall_clear} label="Ask Wall" value="<$500" />
          <div style={{ marginTop: 10 }}>
            <SignalBanner allPass={gates?.all_pass ?? false} engineState={engineState} />
          </div>
        </div>

        {/* Right — position + stats */}
        <div>
          <PositionPanel position={position} midPrice={midPrice ?? 0} />
          <SessionStats stats={sessionStats} dailyCap={dailyCap} />
        </div>
      </div>

      {/* Trade log */}
      <div style={{ padding: 12, background: C.panel, borderRadius: 8,
                    border: `1px solid ${C.border}` }}>
        <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginBottom: 8,
                      textTransform: "uppercase", letterSpacing: "0.1em" }}>Trade Log</div>
        <TradeLog trades={trades} />
      </div>
    </div>
  );
}

function TierBar({ sharePct }) {
  const tiers = [
    { label: "Top 5%", pct: 5, color: C.accent },
    { label: "Top 10%", pct: 10, color: C.blue },
    { label: "Top 25%", pct: 25, color: C.warn },
  ];
  // sharePct is fraction of total market volume; *500 maps 0.2% share → top-100% (rough rank heuristic)
  const userPos = Math.min(sharePct * 500, 100);
  const color = userPos <= 5 ? C.accent : userPos <= 10 ? C.blue : userPos <= 25 ? C.warn : C.danger;
  return (
    <div>
      <div style={{ position: "relative", height: 20, background: "#27272a", borderRadius: 4,
                    overflow: "visible", marginBottom: 4 }}>
        {tiers.map(t => (
          <div key={t.label} style={{
            position: "absolute", left: `${t.pct}%`, top: 0, bottom: 0,
            width: 1, background: t.color + "60",
          }}>
            <span style={{
              position: "absolute", top: -16, left: 2,
              fontFamily: C.mono, fontSize: 9, color: t.color, whiteSpace: "nowrap",
            }}>{t.label}</span>
          </div>
        ))}
        <div style={{
          position: "absolute", top: 2, bottom: 2,
          left: `${Math.min(userPos, 98)}%`, width: 3, borderRadius: 2,
          background: color, transition: "left 0.3s ease",
        }} />
      </div>
      <div style={{ fontFamily: C.mono, fontSize: 10, color }}>
        Est. rank: top {userPos.toFixed(0)}%
      </div>
    </div>
  );
}

function DiscoverView({ tokens, onStartEngine, onDismiss, enginePair, connected }) {
  const [levers, setLevers] = useState({});

  function getShares(token, posSize) {
    const baseline = token.baseline_volume_7d ?? 3_200_000;
    const price = token.price ?? 0.165; // fallback: PLAY/USD snapshot price
    const marketUsd = baseline * 6 * price;
    const tradesPerDay = 5;
    const userUsd = tradesPerDay * posSize * 2;
    return userUsd / (marketUsd || 1);
  }

  function ratioColor(ratio) {
    if (!ratio) return C.muted;
    if (ratio >= 7) return C.danger;
    if (ratio >= 4) return C.warn;
    return C.blue;
  }

  const anomalous = tokens.filter(t => t.anomaly_ratio && t.anomaly_ratio >= 3);

  return (
    <div>
      {!connected && <OfflineBanner />}
      {anomalous.length === 0 && connected && (
        <div style={{ padding: 24, textAlign: "center", fontFamily: C.mono,
                      fontSize: 12, color: C.muted, marginBottom: 16 }}>
          No anomalies detected. Next scan in progress...
        </div>
      )}
      {tokens.length === 0 && <WatchlistSeed />}
      {anomalous.map(token => {
        const posSize = levers[token.pair] ?? 600;
        const sharePct = getShares(token, posSize) * 100;
        const canTrade = !enginePair || enginePair === token.pair;
        return (
          <div key={token.pair} style={{
            padding: 16, background: C.panel, borderRadius: 8,
            border: `1px solid ${C.border}`, marginBottom: 12,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
              <div>
                <span style={{ fontFamily: C.mono, fontSize: 14, fontWeight: 700,
                               color: C.text }}>{token.pair}</span>
                <span style={{
                  marginLeft: 8, padding: "2px 8px", borderRadius: 4,
                  fontFamily: C.mono, fontSize: 10, fontWeight: 700,
                  background: ratioColor(token.anomaly_ratio) + "20",
                  color: ratioColor(token.anomaly_ratio),
                }}>
                  {token.anomaly_ratio?.toFixed(1)}× baseline
                </span>
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  onClick={() => onDismiss(token.pair)}
                  style={{
                    padding: "4px 10px", borderRadius: 6, border: `1px solid ${C.border}`,
                    background: "transparent", color: C.muted, fontFamily: C.mono,
                    fontSize: 11, cursor: "pointer",
                  }}
                >
                  Dismiss 2h
                </button>
                <button
                  onClick={() => canTrade && onStartEngine(token.pair, posSize)}
                  disabled={!canTrade}
                  style={{
                    padding: "4px 14px", borderRadius: 6, border: "none",
                    background: canTrade ? C.purple : C.border,
                    color: canTrade ? C.text : C.muted,
                    fontFamily: C.mono, fontSize: 11, fontWeight: 700,
                    cursor: canTrade ? "pointer" : "not-allowed",
                  }}
                >
                  {enginePair === token.pair ? "Running ✓" : canTrade ? "Start APEX" : "Engine Busy"}
                </button>
              </div>
            </div>

            <div style={{ marginTop: 12 }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>
                  Position size: <strong style={{ color: C.text }}>${posSize}</strong>
                  {" "}→ ${(5 * posSize * 2).toLocaleString()}/day projected
                </span>
                <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>
                  {sharePct.toFixed(3)}% market share
                </span>
              </div>
              <input
                type="range" min={600} max={3000} step={100}
                value={posSize}
                onChange={e => setLevers(l => ({ ...l, [token.pair]: Number(e.target.value) }))}
                style={{ width: "100%", accentColor: C.purple, marginBottom: 8 }}
              />
              <TierBar sharePct={sharePct / 100} />
            </div>

            <div style={{ marginTop: 8, padding: 8, background: "#0f1923", borderRadius: 6,
                          fontFamily: C.mono, fontSize: 10, color: C.muted }}>
              Competition type: <span style={{ color: C.warn }}>
                {token.competition_type || "unknown"}{!token.competition_type_confirmed ? " (inferred)" : ""}
              </span>
              {" — "}
              <a href="https://www.kraken.com/promotions" target="_blank" rel="noopener noreferrer"
                 style={{ color: C.blue }}>verify on Kraken</a>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function CompetitionModal({ alert, onStart, onDismiss }) {
  if (!alert) return null;
  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.75)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 9999,
    }}>
      <div style={{
        background: C.panel, border: `1px solid ${C.purple}60`, borderRadius: 12,
        padding: 24, maxWidth: 480, width: "90%",
      }}>
        <div style={{ fontFamily: C.mono, fontSize: 11, color: C.purple, marginBottom: 4,
                      textTransform: "uppercase", letterSpacing: "0.1em" }}>
          ⚡ Competition Detected
        </div>
        <div style={{ fontFamily: C.sans, fontSize: 18, fontWeight: 700, color: C.text,
                      marginBottom: 12 }}>{alert.pair}</div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginBottom: 16 }}>
          {[
            ["Volume", `${(alert.volume / 1_000_000).toFixed(1)}M`],
            ["Baseline", `${(alert.baseline / 1_000_000).toFixed(1)}M`],
            ["Ratio", `${alert.ratio?.toFixed(1)}× baseline`],
            ["Type", `${alert.competition_type || "unknown"}${!alert.competition_type_confirmed ? " (inferred)" : ""}`],
          ].map(([l, v]) => (
            <div key={l} style={{ padding: 8, background: C.bg, borderRadius: 6 }}>
              <div style={{ fontFamily: C.mono, fontSize: 9, color: C.muted }}>{l}</div>
              <div style={{ fontFamily: C.mono, fontSize: 12, color: C.text }}>{v}</div>
            </div>
          ))}
        </div>
        <div style={{ padding: 8, background: "#0f1923", borderRadius: 6, marginBottom: 16,
                      fontFamily: C.mono, fontSize: 10, color: C.muted }}>
          Strategy: $600 position · +2.5% target · −1.3% stop · 5-min candles
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            onClick={() => onStart(alert.pair)}
            style={{
              flex: 1, padding: "10px 0", borderRadius: 8, border: "none",
              background: C.purple, color: C.text,
              fontFamily: C.mono, fontSize: 13, fontWeight: 700, cursor: "pointer",
            }}
          >
            Start APEX Engine
          </button>
          <button
            onClick={() => onDismiss(alert.pair)}
            style={{
              padding: "10px 16px", borderRadius: 8,
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

export default function MemeTab() {
  const [subView, setSubView] = useState("discover");
  const [connected, setConnected] = useState(false);
  const [engineState, setEngineState] = useState("idle");
  const [enginePair, setEnginePair] = useState(null);
  const [gates, setGates] = useState(null);
  const [position, setPosition] = useState(null);
  const [midPrice, setMidPrice] = useState(0);
  const [obi, setObi] = useState(0);
  const [sessionStats, setSessionStats] = useState(null);
  const [trades, setTrades] = useState([]);
  const [tokens, setTokens] = useState([]);
  const [pendingAlert, setPendingAlert] = useState(null);
  const wsRef = useRef(null);
  const reconnectRef = useRef(null);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;
    const ws = new WebSocket(APEX_WS);
    wsRef.current = ws;
    ws.onopen = () => setConnected(true);
    ws.onclose = () => {
      setConnected(false);
      reconnectRef.current = setTimeout(() => connect(), 5000);
    };
    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        switch (msg.type) {
          case "signal_state":
            setGates(msg.gates);
            if (msg.gates?.all_pass) setSubView("trading");
            break;
          case "position_update":
            setMidPrice(msg.price ?? 0);
            setObi(msg.obi ?? 0);
            // msg.entry is a full position object {entry_price, qty, candles_held, ...}
            if (msg.entry && typeof msg.entry === "object") {
              setPosition(p => p ? { ...p, ...msg.entry } : msg.entry);
            }
            break;
          case "order_placed":
            if (msg.side === "buy") {
              setPosition({ entry_price: msg.price, qty: msg.qty,
                            notional_usd: 600, entry_ts: Date.now() / 1000, candles_held: 0 });
              setSubView("trading");
            }
            break;
          case "trade_closed":
            setPosition(null);
            // backend sends entry_price/exit_price; alias for TradeLog which reads entry_price/exit_price
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
            break;
          case "competition_alert":
            setPendingAlert(msg);
            break;
          case "watchlist_update":
            setTokens(msg.tokens ?? []);
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

  function handleDismiss(pair) {
    setPendingAlert(null);
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "dismiss_alert", pair }));
    }
  }

  const tradingState = { gates, position, midPrice, obi, engineState, sessionStats, trades };

  return (
    <div style={{ padding: "16px 24px", background: C.bg, minHeight: "100%" }}>
      <div style={{ display: "flex", gap: 0, marginBottom: 20, borderBottom: `1px solid ${C.border}` }}>
        {[["trading", "⚡ Trading"], ["discover", "🔍 Discover"]].map(([key, label]) => (
          <button
            key={key}
            onClick={() => setSubView(key)}
            style={{
              padding: "8px 18px", border: "none", background: "transparent",
              fontFamily: C.mono, fontSize: 12, fontWeight: 700,
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
            width: 6, height: 6, borderRadius: "50%",
            background: connected ? C.accent : C.danger,
          }} />
          <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>
            {connected ? `APEX ${enginePair ?? "idle"}` : "disconnected"}
          </span>
        </div>
      </div>

      {subView === "trading" && (
        <TradingView state={tradingState} dailyCap={APEX_DAILY_CAP_USD} connected={connected} />
      )}
      {subView === "discover" && (
        <DiscoverView
          tokens={tokens}
          onStartEngine={handleStartEngine}
          onDismiss={handleDismiss}
          enginePair={enginePair}
          connected={connected}
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
