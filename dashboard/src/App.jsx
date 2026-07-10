import { useState, useEffect, useRef, useCallback } from "react";
import "./App.css";
import ResearchTab from "./components/ResearchTab";

// ═══════════════════════════════════════════════════════════════
// HYDRA Live Dashboard — Connects to hydra_agent.py WebSocket
// ═══════════════════════════════════════════════════════════════

// Override at build time with VITE_HYDRA_WS_URL for non-localhost deployments.
const DEFAULT_WS_URL = import.meta.env.VITE_HYDRA_WS_URL || "ws://localhost:8765";

// Constrain any wsUrl that can be influenced by client-side state (localStorage,
// server-provided `start_agent_ack.port`) to `ws[s]://<loopback>[:<port>][/path]`.
// Anything else falls back to DEFAULT_WS_URL. Regex-based allowlist (not URL
// parser) so CodeQL js/request-forgery recognises this as a sanitiser — the
// query trusts `RegExp.test()` on an anchored pattern as a guard, whereas it
// does not follow string round-trips through `new URL(...)`.
const SAFE_WS_URL_RE = /^wss?:\/\/(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?(?:\/[\w\-./]*)?$/;
function sanitizeWsUrl(candidate) {
  if (typeof candidate === "string" && SAFE_WS_URL_RE.test(candidate)) {
    return candidate;
  }
  return DEFAULT_WS_URL;
}
// WS auth token file is written by the agent at startup to
// dashboard/public/hydra_ws_token.json. Vite serves public/ at root,
// so a plain fetch returns the current token. Rotates every agent
// restart — a stale cache returns auth_required and we refetch.
const WS_TOKEN_URL = "/hydra_ws_token.json";

const COLORS = {
  bg: "#09090b",
  panel: "#18181b",
  panelBorder: "#27272a",
  accent: "#10b981",
  danger: "#ef4444",
  warn: "#f59e0b",
  blue: "#3b82f6",
  purple: "#8b5cf6",
  risk: "#a78bfa",
  text: "#f4f4f5",
  textDim: "#a1a1aa",
  textMuted: "#71717a",
  buy: "#10b981",
  sell: "#ef4444",
  hold: "#f59e0b",
  trendUp: "#10b981",
  trendDown: "#ef4444",
  ranging: "#f59e0b",
  volatile: "#8b5cf6",
};

const regimeColor = (r) =>
  ({ TREND_UP: COLORS.trendUp, TREND_DOWN: COLORS.trendDown, RANGING: COLORS.ranging, VOLATILE: COLORS.volatile }[r] || COLORS.textDim);

const getForexSession = () => {
  const h = new Date().getUTCHours();
  if (h >= 12 && h < 16) return { label: "London/NY", color: COLORS.accent };
  if (h >= 7 && h < 12) return { label: "London", color: COLORS.blue };
  if (h >= 16 && h < 21) return { label: "New York", color: COLORS.blue };
  if (h >= 0 && h < 7) return { label: "Asian", color: COLORS.warn };
  return { label: "Dead Zone", color: COLORS.danger };
};

const signalColor = (s) =>
  ({ BUY: COLORS.buy, SELL: COLORS.sell, HOLD: COLORS.hold }[s] || COLORS.textDim);

const mono = "'JetBrains Mono', monospace";
const heading = "'Space Grotesk', 'JetBrains Mono', monospace";

const fmtPrice = (p, prefix = "$") => {
  if (!p || p === 0) return `${prefix}0`;
  if (p < 0.001) return `${prefix}${p.toFixed(8)}`;
  if (p < 0.01) return `${prefix}${p.toFixed(6)}`;
  if (p < 1) return `${prefix}${p.toFixed(4)}`;
  if (p >= 10000) return `${prefix}${p.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return `${prefix}${p.toFixed(2)}`;
};

// Stable-quote membership — mirrors hydra_pair_registry.STABLE_QUOTES.
// Adding a new dollar-equivalent stable quote (e.g. PYUSD) is a one-line
// edit here AND in the engine's STABLE_QUOTES set, in lockstep.
const STABLE_QUOTES = new Set(["USD", "USDC", "USDT"]);

// Determine currency prefix for a pair — "$" for stable-quoted, "" for crypto-quoted.
const pairPrefix = (pair) => {
  if (!pair || !pair.includes("/")) return "";
  const quote = pair.split("/")[1].toUpperCase();
  return STABLE_QUOTES.has(quote) ? "$" : "";
};

const fmtInd = (v) => {
  if (v === undefined || v === null) return "—";
  if (Math.abs(v) < 0.01) return v.toFixed(6);
  if (Math.abs(v) < 1) return v.toFixed(4);
  return v.toFixed(2);
};

// ─── Small Components ───

// QuantumIcon — a static nucleus with three electron dots swirling around it
// along three tilted elliptical orbits. Each orbit is drawn as a faint guide
// ring (static); the electrons move via SVG <animateMotion> on that same
// ellipse path, each with a different period + phase offset so they never
// cluster. Nucleus breathes in scale via index.css keyframe. When `active`
// is false the electrons freeze and the whole thing dims.
//
// Pinned to its parent via a constant viewBox + fixed size, so it always
// occupies the same footprint in the AI Brain pill regardless of which
// electron is currently at the far edge of its orbit.
function QuantumIcon({ active = true, size = 14, color }) {
  const c = color || COLORS.blue;
  const dim = !active;
  // Canonical horizontal ellipse centred at (12,12) with rx=9, ry=3.5 —
  // a closed arc through (3,12) and (21,12). Tilt each orbit by wrapping
  // in a rotated <g> so the same path reuses across all three.
  const orbitPath = "M 3 12 A 9 3.5 0 1 1 21 12 A 9 3.5 0 1 1 3 12";
  const orbits = [
    { tilt:   0, dur: "2.8s", phase: "0s"    },
    { tilt:  60, dur: "3.6s", phase: "-0.9s" },
    { tilt: -60, dur: "3.2s", phase: "-1.8s" },
  ];
  return (
    <svg width={size} height={size} viewBox="0 0 24 24"
         style={{ display: "inline-block", flexShrink: 0,
                  opacity: dim ? 0.5 : 1 }}
         aria-hidden="true">
      {/* Guide rings — faint, static. Give the electrons an orbit the eye
          can follow. Slightly bolder (0.35 opacity, 1px stroke) so the atom
          structure reads cleanly against the AI Brain pill's blue-tinted bg. */}
      {orbits.map((o, i) => (
        <ellipse key={`ring-${i}`}
                 cx="12" cy="12" rx="9" ry="3.5"
                 fill="none" stroke={c} strokeOpacity="0.35" strokeWidth="1"
                 transform={`rotate(${o.tilt} 12 12)`} />
      ))}
      {/* Electrons — one per orbit, traveling its tilted ellipse. Each
          <g> tilts the path frame; <animateMotion> drives the circle along
          the canonical ellipse expressed in that tilted frame. */}
      {orbits.map((o, i) => (
        <g key={`e-${i}`} transform={`rotate(${o.tilt} 12 12)`}>
          <circle r="1.7" fill={c}>
            {!dim && (
              <animateMotion
                dur={o.dur} begin={o.phase} repeatCount="indefinite"
                rotate="auto" path={orbitPath} />
            )}
            {/* When dim, freeze the electron at the leftmost point of its
                orbit so the icon still reads as "three electrons on three
                rings" even when no work is happening. */}
            {dim && <set attributeName="transform" to="translate(-9,0)" />}
          </circle>
        </g>
      ))}
      {/* Nucleus — subtle breath via CSS keyframe. */}
      <circle cx="12" cy="12" r="2.4" fill={c}
              style={{ transformOrigin: "12px 12px", transformBox: "fill-box",
                       animation: dim ? "none" : "q-nucleus 2.4s ease-in-out infinite" }} />
    </svg>
  );
}

function StatCard({ label, value, unit, color = COLORS.text }) {
  return (
    <div style={{ padding: "12px 16px", background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`, borderRadius: 8, flex: "1 1 0" }}>
      <div style={{ fontSize: 10, color: COLORS.textDim, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4, fontFamily: mono }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 700, color, fontFamily: heading, letterSpacing: "-0.02em" }}>
        {value}<span style={{ fontSize: 11, fontWeight: 400, opacity: 0.6, marginLeft: 2 }}>{unit}</span>
      </div>
    </div>
  );
}

function MiniChart({ data, width = 280, height = 60, color = COLORS.accent, filled = false, fill = false }) {
  if (!data || data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const flat = max === min;
  const range = flat ? 1 : (max - min);
  const pts = data.map((v, i) => `${(i / (data.length - 1)) * width},${flat ? height / 2 : height - ((v - min) / range) * (height - 4) - 2}`);
  const pathD = `M${pts.join(" L")}`;
  const svgStyle = fill
    ? { display: "block", width: "100%", height: "100%" }
    : { display: "block" };
  return (
    <svg width="100%" height={fill ? "100%" : undefined}
         viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={svgStyle}>
      {filled && <path d={`${pathD} L${width},${height} L0,${height} Z`} fill={color} opacity={0.1} vectorEffect="non-scaling-stroke" />}
      <path d={pathD} fill="none" stroke={color} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

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

function CandleChart({ candles, height = 140 }) {
  const [containerRef, cw] = useContainerWidth(600);

  if (!candles || candles.length < 2) {
    return <div ref={containerRef} style={{ width: "100%", height }} />;
  }
  const pad = { top: 16, bottom: 16, left: 6, right: 54 };
  const innerW = cw - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const allHigh = Math.max(...candles.map(c => c.h));
  const allLow = Math.min(...candles.map(c => c.l));
  const range = allHigh - allLow || 1;
  const n = candles.length;
  const slotW = innerW / n;
  const candleW = Math.max(3, Math.min(slotW * 0.78, 14));
  const py = (v) => pad.top + innerH * (1 - (v - allLow) / range);

  return (
    <div ref={containerRef} style={{ width: "100%" }}>
    <svg viewBox={`0 0 ${cw} ${height}`} width={cw} height={height} style={{ display: "block" }}>
      {[0, 0.25, 0.5, 0.75, 1].map(f => {
        const y = pad.top + innerH * (1 - f);
        const price = allLow + range * f;
        return (
          <g key={f}>
            <line x1={pad.left} y1={y} x2={pad.left + innerW} y2={y}
                  stroke={COLORS.panelBorder} strokeWidth={0.5} strokeDasharray="2,3" />
            <text x={cw - 4} y={y + 3}
                  fontFamily={mono} fontSize={9} fill={COLORS.textMuted} textAnchor="end"
                  opacity={0.7}>
              {fmtInd(price)}
            </text>
          </g>
        );
      })}
      {candles.map((c, i) => {
        const cx = pad.left + (i + 0.5) * slotW;
        const x = cx - candleW / 2;
        const bullish = c.c >= c.o;
        const color = bullish ? COLORS.buy : COLORS.sell;
        const bodyTop = py(Math.max(c.o, c.c));
        const bodyH = Math.max(1, py(Math.min(c.o, c.c)) - bodyTop);
        return (
          <g key={i}>
            <line x1={cx} y1={py(c.h)} x2={cx} y2={py(c.l)}
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

function ConfidenceMeter({ confidence, signal }) {
  const w = Math.max(5, confidence * 100);
  return (
    <div style={{ padding: "8px 0" }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
        <span style={{ fontSize: 11, color: COLORS.textDim, fontFamily: mono, textTransform: "uppercase" }}>Signal Confidence</span>
        <span style={{ fontSize: 13, fontWeight: 700, color: signalColor(signal), fontFamily: mono }}>{signal} {(confidence * 100).toFixed(0)}%</span>
      </div>
      <div style={{ height: 4, background: COLORS.panelBorder, borderRadius: 2, overflow: "hidden" }}>
        <div style={{ width: `${w}%`, height: "100%", background: signalColor(signal), borderRadius: 2, transition: "width 0.3s", boxShadow: `0 0 8px ${signalColor(signal)}60` }} />
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
// Phase 8 (v2.10.0): Backtest UI primitives
// ═══════════════════════════════════════════════════════════════

// Cap on the number of experiments whose per-pair equity history the
// dashboard keeps in memory. A long-running session otherwise leaks ~60
// floats/tick * pairs * experiments. LRU-ish: newest wins, oldest drop.
const MAX_EQUITY_HISTORY_EXPERIMENTS = 10;
// Cap for per-experiment dicts (progress / results / reviews) so long
// sessions with many backtests don't leak unbounded state.
const MAX_BACKTEST_DICT_ENTRIES = 50;

function lruCapDict(dict, key, value, cap) {
  const merged = { ...dict, [key]: value };
  const keys = Object.keys(merged);
  if (keys.length <= cap) return merged;
  const keep = keys.slice(-cap);
  const trimmed = {};
  for (const k of keep) trimmed[k] = merged[k];
  return trimmed;
}

// Known top-level keys on the legacy raw-state dict (the broadcaster's
// legacy shape, from hydra_agent._build_dashboard_state). Used to
// guard the fallback path from accidentally treating a malformed typed
// message as live state.
const LIVE_STATE_KEYS = [
  "pairs", "order_journal", "journal_stats", "balance", "balance_usd",
  "ai_brain", "timestamp", "running", "mode", "fee_tier",
];

// ─── Companion subsystem (v2.10.4+) ───
// Renders an orb + drawer + chat UI. All WS messages use the `companion.*`
// namespace and do not interfere with LIVE/BACKTEST/COMPARE. When the
// backend subsystem is disabled the orb never receives a `companion.hello`
// and stays invisible.

// Companion themes drawn from the existing Hydra palette so the drawer
// visually belongs to the dashboard. Athena takes the regal purple
// (wise, mystical), Apex the precise blue (professional), Broski the
// fiery amber (high-energy, warm).
const COMPANION_THEMES = {
  athena: { primary: COLORS.purple,  accent: COLORS.purple, glow: COLORS.purple, sigil: "\u26B2" },
  apex:   { primary: COLORS.blue,    accent: COLORS.blue,   glow: COLORS.blue,   sigil: "\u25B2" },
  broski: { primary: COLORS.warn,    accent: COLORS.warn,   glow: COLORS.warn,   sigil: "\u2736" },
};
const COMPANION_ORDER = ["athena", "apex", "broski"];
const COMPANION_NAMES = { athena: "Athena", apex: "Apex", broski: "Broski" };

// Per-soul rhythm + easing. Each companion breathes at their own pace
// and shape — Athena is slow and deep (patient), Apex is steady and
// precise (metronome), Broski is quick and slightly irregular (excited).
// Regime acts as a subtle modulator on top: VOLATILE compresses the
// cycle, RANGING stretches it, so the orb still tracks market state.
const SOUL_RHYTHM = {
  athena: { baseSeconds: 4.2, scaleMax: 1.045, easing: "cubic-bezier(0.4, 0, 0.6, 1)" },
  apex:   { baseSeconds: 2.9, scaleMax: 1.038, easing: "ease-in-out" },
  broski: { baseSeconds: 2.1, scaleMax: 1.060, easing: "cubic-bezier(0.65, 0, 0.35, 1)" },
};

function CompanionOrb({ theme, onClick, regime, hasUnread, visible, soulId }) {
  if (!visible) return null;
  const rhythm = SOUL_RHYTHM[soulId] || SOUL_RHYTHM.apex;
  // Regime modulator: volatile compresses the cycle by ~25%, ranging
  // stretches by ~25%. TREND_* leave it at the soul's base cadence.
  const regimeMult = regime === "VOLATILE" ? 0.75 : regime === "RANGING" ? 1.25 : 1.0;
  const pulseDuration = `${(rhythm.baseSeconds * regimeMult).toFixed(2)}s`;
  // Base glow ring values bumped ~15% vs previous (16px/28px -> 18/32, plus
  // a third outer halo layer for depth). Alpha nudged up too.
  const restInset = `0 0 4px ${theme.primary}dd inset`;
  const peakInset = `0 0 6px ${theme.primary}ff inset`;
  const restGlow  = `0 0 18px ${theme.glow}80, 0 0 34px ${theme.glow}33`;
  const peakGlow  = `0 0 32px ${theme.glow}c0, 0 0 56px ${theme.glow}55`;
  // Broski gets an extra mid-cycle "catch" in the breathing curve so it
  // feels a touch irregular. Apex and Athena are symmetric.
  const breatheKeyframes = soulId === "broski"
    ? `@keyframes hc-breathe-${soulId} { 0%,100% { transform: scale(1.00);} 42% { transform: scale(${rhythm.scaleMax});} 58% { transform: scale(${(1 + (rhythm.scaleMax - 1) * 0.85).toFixed(4)});} }`
    : `@keyframes hc-breathe-${soulId} { 0%,100% { transform: scale(1.00);} 50% { transform: scale(${rhythm.scaleMax});} }`;
  const glowKeyframes =
    `@keyframes hc-glow-${soulId} { 0%,100% { box-shadow: ${restGlow}, ${restInset};} 50% { box-shadow: ${peakGlow}, ${peakInset};} }`;
  return (
    <>
      <style>{breatheKeyframes}{glowKeyframes}</style>
      <button
        onClick={onClick}
        aria-label={`Open companion drawer`}
        title="Click: open \u2022 \u2328 Esc: close"
        style={{
          position: "fixed", right: 24, bottom: 24, zIndex: 9000,
          width: 56, height: 56, borderRadius: "50%",
          background: `radial-gradient(circle at 35% 30%, ${theme.primary}, ${theme.primary}aa 55%, ${COLORS.panel})`,
          border: `2px solid ${theme.primary}`,
          cursor: "pointer", padding: 0,
          animation: `hc-breathe-${soulId} ${pulseDuration} ${rhythm.easing} infinite, hc-glow-${soulId} ${pulseDuration} ${rhythm.easing} infinite`,
          display: "flex", alignItems: "center", justifyContent: "center",
          color: COLORS.text, fontSize: 22, fontFamily: heading, fontWeight: 700,
          textShadow: `0 0 9px ${theme.glow}`,
        }}>
        <span style={{ pointerEvents: "none" }}>{theme.sigil}</span>
        {hasUnread && (
          <span style={{
            position: "absolute", top: 4, right: 4, width: 10, height: 10,
            borderRadius: "50%", background: theme.glow,
            boxShadow: `0 0 8px ${theme.glow}`,
          }} />
        )}
      </button>
    </>
  );
}

function CompanionSwitcher({ active, onSwitch }) {
  // The three IDs are well-known; always enabled. Metadata from the
  // backend just refines the display name / mood; clicking works even
  // before connect_ack lands.
  return (
    <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
      {COMPANION_ORDER.map((cid) => {
        const theme = COMPANION_THEMES[cid];
        const isActive = active === cid;
        return (
          <button
            key={cid}
            onClick={() => onSwitch(cid)}
            title={COMPANION_NAMES[cid]}
            style={{
              width: 30, height: 30, borderRadius: "50%",
              background: isActive
                ? `radial-gradient(circle at 35% 30%, ${theme.primary}, ${theme.primary}88)`
                : "transparent",
              border: isActive
                ? `1px solid ${theme.glow}`
                : `1px solid ${COLORS.panelBorder}`,
              color: isActive ? COLORS.text : COLORS.textDim,
              fontFamily: heading, fontWeight: 700, fontSize: 13,
              cursor: "pointer",
              display: "flex", alignItems: "center", justifyContent: "center",
              transition: "all 180ms ease",
              padding: 0,
            }}>
            {theme.sigil}
          </button>
        );
      })}
    </div>
  );
}

function CompanionMessage({ m, theme }) {
  const isUser = m.role === "user";
  const isProactive = m.proactive === true;
  return (
    <div style={{
      display: "flex", flexDirection: "column",
      alignItems: isUser ? "flex-end" : "flex-start",
      margin: "6px 0",
    }}>
      {!isUser && m.display_name && (
        <div style={{ fontSize: 9, color: theme.accent, fontFamily: mono,
                      letterSpacing: "0.08em", marginBottom: 2, marginLeft: 12, textTransform: "uppercase" }}>
          {m.display_name}{isProactive ? " \u00b7 unprompted" : ""}
        </div>
      )}
      <div style={{
        maxWidth: "85%",
        padding: "8px 12px",
        borderRadius: 8,
        borderLeft: isUser ? "none" : `2px solid ${theme.primary}`,
        background: isUser ? `${COLORS.accent}12` : `${COLORS.panel}`,
        border: isUser ? `1px solid ${COLORS.accent}33` : `1px solid ${COLORS.panelBorder}`,
        color: COLORS.text,
        fontFamily: mono,
        fontSize: 12, lineHeight: 1.5,
        whiteSpace: "pre-wrap", wordBreak: "break-word",
      }}>
        {m.text}
        {m.error && (
          <div style={{ marginTop: 6, fontSize: 10, color: COLORS.red, fontFamily: mono }}>
            {m.error}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Proposal cards (Phase 2+) ───
function fmtPxShort(p) {
  if (p == null) return "—";
  return Number(p) < 100 ? Number(p).toFixed(4) : Number(p).toFixed(2);
}

function ProposalCard({ proposal, kind, theme, onConfirm, onReject, status }) {
  // kind: "trade" | "ladder"
  // status: null | "armed" | "submitting" | "filled" | "rejected" | "failed" | "expired"
  const [now, setNow] = useState(() => Date.now() / 1000);
  const [armed, setArmed] = useState(false);
  const armRef = useRef(null);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), 250);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 5000);
    armRef.current = t;
    return () => clearTimeout(t);
  }, [armed]);

  const ttlTotal = Math.max(1, (proposal.expires_at - proposal.created_at) || 60);
  const remaining = Math.max(0, proposal.expires_at - now);
  const pctLeft = Math.max(0, Math.min(1, remaining / ttlTotal));
  const ttlColor = pctLeft > 0.5 ? theme.primary : pctLeft > 0.2 ? COLORS.warn : COLORS.danger;
  const expired = remaining <= 0;

  const locked = !!status;  // once submitted/filled/rejected, disable buttons

  const handlePrimary = () => {
    if (locked || expired) return;
    if (!armed) {
      setArmed(true);
      return;
    }
    clearTimeout(armRef.current);
    setArmed(false);
    onConfirm();
  };

  const sideColor = proposal.side === "buy" ? COLORS.buy : COLORS.sell;

  return (
    <div style={{
      margin: "8px 0", border: `1px solid ${theme.primary}66`,
      borderRadius: 10, overflow: "hidden",
      background: `${theme.primary}10`,
      opacity: expired && !status ? 0.55 : 1,
      transition: "opacity 240ms",
    }}>
      {/* TTL bar */}
      <div style={{ height: 3, background: `${theme.primary}22` }}>
        <div style={{ height: "100%", width: `${pctLeft * 100}%`, background: ttlColor,
                      transition: "width 250ms linear" }} />
      </div>
      <div style={{ padding: "10px 12px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
          <span style={{
            background: sideColor, color: COLORS.bg, fontWeight: 700,
            padding: "2px 8px", borderRadius: 4, fontFamily: mono,
            fontSize: 10, letterSpacing: "0.08em",
          }}>{proposal.side.toUpperCase()}</span>
          <span style={{ color: COLORS.text, fontFamily: mono, fontSize: 12, fontWeight: 700 }}>
            {proposal.pair}
          </span>
          <span style={{ color: COLORS.textMuted, fontFamily: mono, fontSize: 10, marginLeft: "auto" }}>
            {kind === "ladder" ? `${proposal.rungs.length} rungs` : "1R"}
          </span>
        </div>

        {kind === "trade" ? (
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px 12px",
                        fontSize: 11, fontFamily: mono, color: COLORS.text }}>
            <div><span style={{ color: COLORS.textMuted }}>Size:</span> {proposal.size}</div>
            <div><span style={{ color: COLORS.textMuted }}>Limit:</span> ${fmtPxShort(proposal.limit_price)}</div>
            <div><span style={{ color: COLORS.textMuted }}>Stop:</span> ${fmtPxShort(proposal.stop_loss)}</div>
            <div><span style={{ color: COLORS.textMuted }}>Cost:</span> ${fmtPxShort(proposal.estimated_cost)}</div>
            <div style={{ gridColumn: "1 / span 2" }}>
              <span style={{ color: COLORS.textMuted }}>Risk:</span>{" "}
              ${Number(proposal.risk_usd || 0).toFixed(2)}
              {proposal.risk_pct_equity ? ` (${Number(proposal.risk_pct_equity).toFixed(2)}% equity)` : ""}
            </div>
          </div>
        ) : (
          <div>
            <div style={{ fontSize: 10, color: COLORS.textMuted, marginBottom: 4, fontFamily: mono }}>
              {`total ${proposal.total_size} \u00b7 stop $${fmtPxShort(proposal.stop_loss)} \u00b7 invalidate $${fmtPxShort(proposal.invalidation_price)}`}
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "auto 1fr auto", gap: "2px 8px",
                          fontSize: 10, fontFamily: mono }}>
              {proposal.rungs.flatMap((r, i) => [
                <span key={`l-${i}`} style={{ color: COLORS.textMuted }}>R{i + 1}</span>,
                <span key={`p-${i}`} style={{ color: COLORS.text }}>${fmtPxShort(r.limit_price)}</span>,
                <span key={`w-${i}`} style={{ color: COLORS.textDim }}>{Math.round(r.pct_of_total * 100)}%</span>,
              ])}
            </div>
          </div>
        )}

        {proposal.rationale && (
          <div style={{ marginTop: 8, fontSize: 11, fontStyle: "italic",
                        color: theme.accent, lineHeight: 1.35,
                        borderLeft: `2px solid ${theme.primary}44`, paddingLeft: 8 }}>
            "{proposal.rationale}"
          </div>
        )}

        {status && (
          <div style={{
            marginTop: 8, padding: "4px 8px", borderRadius: 4, display: "inline-block",
            background: status === "filled" ? `${COLORS.accent}22`
                     : status === "failed" || status === "rejected" ? `${COLORS.danger}22`
                     : `${theme.primary}22`,
            color: status === "filled" ? COLORS.accent
                : status === "failed" || status === "rejected" ? COLORS.danger
                : theme.primary,
            fontSize: 10, fontFamily: mono, fontWeight: 700, letterSpacing: "0.08em",
            textTransform: "uppercase",
          }}>{status}</div>
        )}

        {!locked && !expired && (
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            <button onClick={handlePrimary} style={{
              flex: 1, padding: "8px 12px", borderRadius: 4,
              background: armed ? COLORS.danger : theme.primary,
              color: COLORS.bg, border: "none", cursor: "pointer",
              fontFamily: mono, fontSize: 11, fontWeight: 700,
              letterSpacing: "0.08em", textTransform: "uppercase",
              transition: "background 160ms",
            }}>
              {armed ? "\u25B6 send (5s)" : "arm"}
            </button>
            <button onClick={onReject} style={{
              padding: "8px 12px", borderRadius: 5,
              background: "transparent", color: COLORS.textMuted,
              border: `1px solid ${COLORS.panelBorder}`, cursor: "pointer",
              fontFamily: mono, fontSize: 11,
            }}>reject</button>
          </div>
        )}
        {expired && !locked && (
          <div style={{ marginTop: 10, fontSize: 10, color: COLORS.textMuted, fontFamily: mono }}>
            {"expired \u2014 ask again"}
          </div>
        )}
      </div>
    </div>
  );
}

function CompanionTypingBubble({ theme, name }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", margin: "6px 0" }}>
      <div style={{ fontSize: 9, color: theme.accent, fontFamily: mono,
                    letterSpacing: "0.08em", marginBottom: 2, marginLeft: 12, textTransform: "uppercase" }}>
        {name}
      </div>
      <div style={{
        padding: "8px 14px", borderRadius: 8, borderLeft: `2px solid ${theme.primary}`,
        background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`,
        display: "inline-flex", gap: 4,
      }}>
        <style>{`@keyframes hc-dot { 0%,80%,100% { opacity: 0.3; transform: translateY(0);} 40% { opacity: 1; transform: translateY(-3px);} }`}</style>
        {[0, 1, 2].map((i) => (
          <span key={i} style={{
            width: 6, height: 6, borderRadius: "50%", background: theme.primary,
            animation: `hc-dot 1.2s ease-in-out ${i * 0.15}s infinite`,
          }} />
        ))}
      </div>
    </div>
  );
}

function CompanionDrawer({
  open, onClose, active, onSwitch, companions, messages, typing,
  onSend, onProposalConfirm, onProposalReject, connected, drawerWidth, costAlerts,
}) {
  const theme = COMPANION_THEMES[active] || COMPANION_THEMES.apex;
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);

  // Auto-scroll to bottom on new messages / typing
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, typing, active]);

  // Focus composer on open / active-change
  useEffect(() => {
    if (open && inputRef.current) inputRef.current.focus();
  }, [open, active]);

  // Esc closes
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Submit lock via ref so double-presses within the same React tick
  // can't slip past the guard (state updates are async; refs are sync).
  const submitLockRef = useRef(false);
  const submitTimeoutRef = useRef(null);
  useEffect(() => {
    return () => {
      if (submitTimeoutRef.current) clearTimeout(submitTimeoutRef.current);
    };
  }, []);
  const submit = () => {
    if (submitLockRef.current) return;
    const text = draft.trim();
    if (!text) return;
    submitLockRef.current = true;
    setSending(true);
    onSend(text);
    setDraft("");
    submitTimeoutRef.current = setTimeout(() => {
      submitTimeoutRef.current = null;
      submitLockRef.current = false;
      setSending(false);
    }, 350);
  };

  const onKey = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  if (!open) return null;
  const comp = companions[active];
  const name = comp?.display_name || COMPANION_NAMES[active] || "Companion";
  const alert = costAlerts[active];

  return (
    <div style={{
      position: "fixed", top: 0, right: 0, bottom: 0, width: drawerWidth,
      zIndex: 9000, background: `${COLORS.panel}f0`, backdropFilter: "blur(14px)",
      borderLeft: `1px solid ${theme.primary}66`,
      boxShadow: `-8px 0 32px rgba(0,0,0,0.5), inset 2px 0 0 ${theme.primary}44`,
      display: "flex", flexDirection: "column",
      animation: `hc-slide-in 260ms cubic-bezier(0.32, 0.72, 0, 1), hc-drawer-glow-${active} ${(SOUL_RHYTHM[active]?.baseSeconds || 3)}s ${SOUL_RHYTHM[active]?.easing || "ease-in-out"} infinite`,
      fontFamily: mono,
    }}>
      <style>{`
        @keyframes hc-slide-in { from { transform: translateX(100%);} to { transform: translateX(0);} }
        @keyframes hc-drawer-glow-${active} {
          0%,100% {
            box-shadow: -8px 0 32px rgba(0,0,0,0.5),
                        inset 2px 0 0 ${theme.primary}44,
                        inset 0 0 40px ${theme.primary}08;
            border-left-color: ${theme.primary}66;
          }
          50% {
            box-shadow: -8px 0 44px rgba(0,0,0,0.55),
                        inset 3px 0 0 ${theme.primary}88,
                        inset 0 0 70px ${theme.primary}16;
            border-left-color: ${theme.primary}aa;
          }
        }
      `}</style>

      {/* Header */}
      <div style={{
        padding: "12px 14px", display: "flex", alignItems: "center", gap: 10,
        borderBottom: `1px solid ${theme.primary}33`,
        background: `linear-gradient(90deg, ${theme.primary}22, transparent)`,
      }}>
        <div style={{
          width: 32, height: 32, borderRadius: "50%",
          background: `radial-gradient(circle at 35% 30%, ${theme.accent}, ${theme.primary})`,
          display: "flex", alignItems: "center", justifyContent: "center",
          color: "#fff", fontFamily: heading, fontWeight: 700, fontSize: 14,
        }}>{theme.sigil}</div>
        <div style={{ flex: 1 }}>
          <div style={{ fontFamily: heading, fontSize: 14, fontWeight: 700, color: COLORS.text }}>{name}</div>
          <div style={{ fontSize: 9, color: theme.accent, letterSpacing: "0.08em", textTransform: "uppercase" }}>
            {comp?.mood || "calm"}{comp?.serious_mode ? " \u00b7 serious" : ""}
          </div>
        </div>
        <CompanionSwitcher active={active} onSwitch={onSwitch} />
        <button onClick={onClose} aria-label="Close drawer" title="Close" style={{
          background: "transparent", border: `1px solid ${COLORS.panelBorder}`,
          color: COLORS.textMuted, cursor: "pointer", borderRadius: 4,
          padding: "4px 10px", fontFamily: mono, fontSize: 13, lineHeight: 1,
        }}>{"\u00D7"}</button>
      </div>

      {alert && (
        <div style={{
          padding: "6px 14px", fontSize: 10, background: `${theme.glow}22`,
          borderBottom: `1px solid ${theme.glow}44`, color: theme.accent, fontFamily: mono,
        }}>
          budget alert: ${alert.daily_cost_usd} of ${alert.hard_stop_usd} used
        </div>
      )}

      {/* Messages */}
      <div ref={scrollRef} style={{
        flex: 1, overflowY: "auto", padding: "10px 14px",
      }}>
        {messages.length === 0 && !typing && (
          <div style={{ color: COLORS.textMuted, fontSize: 11, marginTop: 20, textAlign: "center" }}>
            {`say hi to ${name.toLowerCase()} \u2014 or type `}
            <code style={{ color: theme.accent }}>/help</code>
          </div>
        )}
        {messages.map((m) => {
          if (m.role === "proposal") {
            return (
              <ProposalCard
                key={m.id}
                proposal={m.proposal}
                kind={m.kind || "trade"}
                theme={theme}
                status={m.status}
                onConfirm={() => onProposalConfirm(m)}
                onReject={() => onProposalReject(m)}
              />
            );
          }
          return <CompanionMessage key={m.id} m={m} theme={theme} />;
        })}
        {typing && <CompanionTypingBubble theme={theme} name={name} />}
      </div>

      {/* Composer */}
      <div style={{
        borderTop: `1px solid ${theme.primary}33`,
        padding: "10px 12px", background: `${COLORS.panel}ee`,
      }}>
        <textarea
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKey}
          placeholder={active === "apex" ? "message apex \u2014"
                     : active === "athena" ? "speak to Athena\u2026"
                     : "yo what's up"}
          disabled={!connected}
          style={{
            // v2.13.7: bumped minHeight 40→72 and maxHeight 140→260 so
            // ~4 lines are visible at rest and ~14 lines fit when typing
            // a longer message without having to scroll a 40px textarea.
            width: "100%", minHeight: 72, maxHeight: 260, resize: "none",
            background: `${COLORS.bg}cc`, color: COLORS.text,
            border: `1px solid ${theme.primary}55`, borderRadius: 6,
            padding: "8px 10px", fontFamily: mono, fontSize: 13, lineHeight: 1.4,
            outline: "none",
          }}
        />
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 6 }}>
          <div style={{ fontSize: 9, color: COLORS.textMuted, fontFamily: mono }}>
            {"\u21B5 send \u00b7 Shift+\u21B5 newline \u00b7 Esc close"}
          </div>
          <button
            onClick={submit}
            disabled={!draft.trim() || !connected || sending}
            style={{
              background: draft.trim() && connected ? theme.primary : `${theme.primary}44`,
              color: "#fff", border: "none", borderRadius: 4,
              padding: "6px 14px", fontFamily: mono, fontSize: 11, fontWeight: 700,
              cursor: draft.trim() && connected ? "pointer" : "default",
              letterSpacing: "0.08em", textTransform: "uppercase",
            }}>
            send
          </button>
        </div>
      </div>
    </div>
  );
}

function TabSwitcher({ activeTab, onChange }) {
  const tabs = [
    { key: "LIVE",     label: "LIVE",     color: COLORS.accent },
    { key: "RESEARCH", label: "RESEARCH", color: COLORS.purple },
    { key: "SETTINGS", label: "SETTINGS", color: COLORS.text },
  ];
  return (
    // Gap: 10 puts visible air between each tab so the row breathes.
    // minHeight: 38 + flex centers content to match the AI Brain pill's
    // icon-bearing height exactly.
    <div style={{ display: "flex", gap: 10, padding: "8px 0" }}>
      {tabs.map(t => {
        const active = activeTab === t.key;
        return (
          <button
            key={t.key}
            onClick={() => onChange(t.key)}
            style={{
              padding: "0 18px",
              minHeight: 38,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 12,
              fontWeight: 700,
              fontFamily: mono,
              letterSpacing: "0.1em",
              textTransform: "uppercase",
              background: active ? `${t.color}18` : "transparent",
              color: active ? t.color : COLORS.textDim,
              border: `1px solid ${active ? t.color + "60" : COLORS.panelBorder}`,
              borderRadius: 4,
              cursor: "pointer",
              outline: "none",
              transition: "all 0.15s ease",
            }}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
// Shared visual primitives (live + observer — prevents drift)
// ═══════════════════════════════════════════════════════════════

// Regime badge: identical coloring + typography in LIVE and observer.
// size: "compact" for the observer dock, "regular" for the LIVE pair panel.
function RegimeBadge({ regime, size = "regular" }) {
  const c = regimeColor(regime);
  const compact = size === "compact";
  return (
    <span style={{
      fontSize: compact ? 9 : 10,
      fontFamily: mono,
      color: c,
      background: `${c}18`,
      padding: compact ? "2px 6px" : "3px 8px",
      borderRadius: 3,
      letterSpacing: "0.08em",
    }}>
      {regime || "—"}
    </span>
  );
}

// Signal chip: same HOLD/BUY/SELL palette everywhere.
function SignalChip({ action, size = "regular" }) {
  const c = signalColor(action);
  const compact = size === "compact";
  return (
    <span style={{
      fontSize: compact ? 9 : 10,
      fontFamily: mono,
      color: c,
      fontWeight: 700,
      letterSpacing: "0.04em",
    }}>
      {action || "HOLD"}
    </span>
  );
}

// ═══════════════════════════════════════════════════════════════
// Phase 9: Dual-state Observer Modal
// ═══════════════════════════════════════════════════════════════

// Stage color map mirrors LIVE signal/regime palette so the observer
// reads at a glance as a variant of the live view.
function stageColor(stage) {
  if (stage === "running") return COLORS.blue;
  if (stage === "started") return COLORS.textDim;
  if (stage === "cancelled") return COLORS.warn;
  if (stage === "failed") return COLORS.danger;
  if (stage === "complete") return COLORS.accent;
  return COLORS.textDim;
}

function ObserverProgressBar({ tick, totalTicks, stage }) {
  const pct = totalTicks > 0 ? Math.min(100, Math.max(0, (tick / totalTicks) * 100)) : 0;
  const color = stageColor(stage);
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10,
                    fontFamily: mono, color: COLORS.textDim, marginBottom: 4 }}>
        <span>
          tick <span style={{ color: COLORS.text }}>{tick}</span>
          {totalTicks > 0 && <> / {totalTicks}</>}
        </span>
        <span style={{ color, textTransform: "uppercase", letterSpacing: "0.1em" }}>
          {stage || "—"}
        </span>
      </div>
      <div style={{ height: 4, background: COLORS.panelBorder, borderRadius: 2, overflow: "hidden" }}>
        <div style={{ height: "100%", width: `${pct}%`, background: color,
                      boxShadow: `0 0 6px ${color}80`, transition: "width 0.2s ease" }} />
      </div>
    </div>
  );
}

// Compact per-pair card for the observer. Intentionally a separate visual
// from LIVE's PairPanel (simpler, smaller) because the observer coexists
// with the LIVE grid on the LIVE tab — we want a distinct affordance.
function ObserverPairCard({ pair, state, equityHistory, expand = false }) {
  if (!state) return null;
  const sig = state.signal || {};
  const port = state.portfolio || {};
  const pos = state.position || {};
  const px = pairPrefix(pair);

  return (
    <div style={{ background: COLORS.bg, border: `1px solid ${COLORS.panelBorder}`,
                  borderRadius: 6, padding: 10,
                  flex: expand ? 1 : "0 0 auto",
                  display: "flex", flexDirection: "column", minHeight: 0 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
                    marginBottom: 8 }}>
        <div style={{ fontSize: 13, fontWeight: 700, fontFamily: mono, color: COLORS.text }}>
          {pair}
        </div>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <RegimeBadge regime={state.regime} size="compact" />
          <SignalChip action={sig.action} size="compact" />
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 5, fontSize: 12,
                    fontFamily: mono }}>
        <span style={{ color: COLORS.textDim }}>Price</span>
        <span style={{ color: COLORS.text, textAlign: "right" }}>{fmtPrice(state.price, px)}</span>
        <span style={{ color: COLORS.textDim }}>Equity</span>
        <span style={{ color: COLORS.text, textAlign: "right" }}>{fmtPrice(port.equity, px)}</span>
        <span style={{ color: COLORS.textDim }}>Position</span>
        <span style={{ color: pos.size > 0 ? COLORS.accent : COLORS.textMuted, textAlign: "right" }}>
          {fmtInd(pos.size)}
        </span>
        <span style={{ color: COLORS.textDim }}>P&L%</span>
        <span style={{ color: (port.pnl_pct || 0) >= 0 ? COLORS.buy : COLORS.sell, textAlign: "right" }}>
          {(port.pnl_pct || 0).toFixed(2)}%
        </span>
      </div>
      {equityHistory && equityHistory.length >= 2 && (
        <div style={{ marginTop: 8,
                      flex: expand ? 1 : "0 0 auto",
                      minHeight: expand ? 80 : 36,
                      display: "flex" }}>
          <MiniChart
            data={equityHistory}
            width={240}
            height={expand ? 160 : 36}
            color={(port.pnl_pct || 0) >= 0 ? COLORS.accent : COLORS.danger}
            filled
            fill={expand}
          />
        </div>
      )}
    </div>
  );
}

function ObserverModal({
  progress,          // latest backtest_progress message: {experiment_id, tick, stage, dashboard_state}
  result,            // backtest_result summary when complete
  equityHistory,     // {pair -> [equity...]}  accumulated from progress stream
  totalTicks,        // best-effort total (from result candles_processed or dashboard_state.max)
  variant = "dock",  // "dock" (fills column) | "floating" (slide-in on LIVE tab)
  onClose,
}) {
  if (!progress && !result) return null;
  const expId = progress?.experiment_id || result?.experiment_id || "—";
  const stage = result ? (result.status || "complete") : (progress?.stage || "running");
  const tick = progress?.tick ?? result?.metrics?.total_trades ?? 0;
  const pairs = progress?.dashboard_state?.pairs || {};
  const pairNames = Object.keys(pairs);
  const summary = result?.metrics;
  const hypothesis = result?.hypothesis || "";
  const shellStyle = variant === "floating"
    ? { position: "fixed", right: 16, top: 80, width: 360, maxHeight: "calc(100vh - 100px)",
        overflowY: "auto", zIndex: 20,
        boxShadow: "0 8px 32px rgba(0,0,0,0.45)" }
    : { flex: 1, display: "flex", flexDirection: "column", minHeight: 0 };

  return (
    <div
      style={{
        ...shellStyle,
        background: COLORS.panel,
        border: `1px solid ${COLORS.panelBorder}`,
        borderRadius: 8,
        padding: 14,
      }}
    >
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start",
                    marginBottom: 10 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 2 }}>
            <span style={{ fontSize: 11, fontFamily: heading, fontWeight: 800,
                           color: COLORS.blue, letterSpacing: "0.04em",
                           textTransform: "uppercase" }}>
              Observer
            </span>
            <span style={{ fontSize: 9, fontFamily: mono, color: COLORS.textMuted }}>
              {expId.slice(0, 16)}…
            </span>
          </div>
          {hypothesis && (
            <div style={{ fontSize: 10, fontFamily: mono, color: COLORS.textDim,
                          fontStyle: "italic", lineHeight: 1.4, marginTop: 2,
                          whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              "{hypothesis}"
            </div>
          )}
        </div>
        {onClose && (
          <button
            onClick={onClose}
            style={{ background: "transparent", color: COLORS.textDim, border: "none",
                     cursor: "pointer", padding: 4, fontSize: 14, lineHeight: 1,
                     fontFamily: mono }}
            title="Close observer"
          >
            ×
          </button>
        )}
      </div>

      {/* Progress bar */}
      <div style={{ marginBottom: 10 }}>
        <ObserverProgressBar tick={tick} totalTicks={totalTicks || 0} stage={stage} />
      </div>

      {/* Terminal summary is rendered in the left control panel (BacktestResultMetrics)
          to give the equity chart more headroom. */}

      {/* Per-pair cards — same visual DNA as LIVE.
          flex: 1 + minHeight: 0 lets the card stack fill the panel height so
          the equity chart stretches down to the bottom of the adjacent
          control panel on the left. */}
      {pairNames.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6,
                      flex: variant === "dock" ? 1 : "0 0 auto", minHeight: 0 }}>
          {pairNames.map(pair => (
            <ObserverPairCard
              key={pair}
              pair={pair}
              state={pairs[pair]}
              equityHistory={equityHistory?.[pair] || []}
              expand={variant === "dock"}
            />
          ))}
        </div>
      )}

      {!pairNames.length && !summary && (
        <div style={{ fontFamily: mono, fontSize: 10, color: COLORS.textMuted,
                      textAlign: "center", padding: "20px 0" }}>
          Waiting for first tick…
        </div>
      )}
    </div>
  );
}

function ConnectionStatus({ connected, tick }) {
  // The colored, optionally-pulsing dot conveys the live/disconnected state
  // visually. The text redundantly saying "LIVE" on top of that competes
  // with the LIVE tab label and the AI/engine pill, so we drop it and show
  // just the tick count — the thing the user actually can't infer from
  // anywhere else.
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}
         title={connected ? "Connected to the agent. Tick number increments each engine tick." : "Disconnected — the agent isn't running or the WebSocket dropped."}>
      <div style={{
        width: 8, height: 8, borderRadius: "50%",
        background: connected ? COLORS.accent : COLORS.danger,
        boxShadow: `0 0 8px ${connected ? COLORS.accent : COLORS.danger}80`,
        animation: connected ? "none" : "pulse 1.5s infinite",
      }} />
      <span style={{ fontSize: 11, fontFamily: mono, color: connected ? COLORS.accent : COLORS.danger,
                     letterSpacing: "0.04em" }}>
        {connected ? `Tick #${tick}` : "DISCONNECTED"}
      </span>
    </div>
  );
}

// ─── Loading Screen ───

function LoadingScreen({ connected }) {
  const [progress, setProgress] = useState(0);
  const [phraseIndex, setPhraseIndex] = useState(0);

  const phrases = [
    "Waking the Hydra...",
    "Confabulating with the blockchain...",
    "Bribing market makers...",
    "Calculating lambo trajectories...",
    "Reticulating splines...",
    "Consulting the astrology charts...",
    "Frontrunning retail...",
    "Pumping the bags...",
    "Reverting to mean..."
  ];

  useEffect(() => {
    const interval = setInterval(() => {
      setProgress(p => {
        const remaining = 99 - p;
        const inc = Math.max(0.05, remaining * 0.015); // Slower ease out
        const next = p + inc;
        return next > 99 ? 99 : next;
      });
    }, 100);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    const interval = setInterval(() => {
      setPhraseIndex(i => (i + 1) % phrases.length);
    }, 2500);
    return () => clearInterval(interval);
  }, [phrases.length]);

  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "80vh", flexDirection: "column", gap: 32 }}>
      <div style={{ position: "relative", width: 140, height: 140, marginBottom: 12 }}>
        <img src="/favicon.png" alt="Hydra" style={{ width: "100%", height: "100%", filter: "drop-shadow(0 0 20px rgba(16, 185, 129, 0.4))", position: "relative", zIndex: 2 }} />
        {/* Pulsing ring */}
        <div style={{ position: "absolute", top: -15, left: -15, right: -15, bottom: -15, borderRadius: "50%", border: `2px solid ${COLORS.accent}`, opacity: 0.5, animation: "hc-ping 2s cubic-bezier(0, 0, 0.2, 1) infinite", zIndex: 1 }} />
        <style>{`@keyframes hc-ping { 75%, 100% { transform: scale(1.4); opacity: 0; } }`}</style>
      </div>
      
      <div style={{ 
        fontSize: 56, fontWeight: 900, fontFamily: heading, letterSpacing: "-0.02em",
        background: `linear-gradient(135deg, ${COLORS.accent}, #0d9488)`,
        WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent",
        marginTop: -16
      }}>HYDRA</div>

      <div style={{ width: 320, display: "flex", flexDirection: "column", gap: 14, alignItems: "center" }}>
        {/* Progress Bar */}
        <div style={{ width: "100%", height: 6, background: COLORS.panelBorder, borderRadius: 3, overflow: "hidden", position: "relative" }}>
          <div style={{ width: `${progress}%`, height: "100%", background: COLORS.accent, transition: "width 0.1s linear", boxShadow: `0 0 10px ${COLORS.accent}80` }} />
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", width: "100%", fontFamily: mono, fontSize: 13 }}>
          <span style={{ color: COLORS.textDim }}>{phrases[phraseIndex]}</span>
          <span style={{ color: COLORS.accent, fontWeight: 700 }}>{Math.floor(progress)}%</span>
        </div>
      </div>

      <div style={{ fontSize: 12, color: COLORS.textMuted, fontFamily: mono, marginTop: 8 }}>
        {connected ? "Connection established. Awaiting first tick..." : `Waiting for agent connection on ${DEFAULT_WS_URL}...`}
      </div>
    </div>
  );
}

// ─── Main App ───

export function HydraDashboard({ jwtToken, onLogout }) {
  const [connected, setConnected] = useState(false);
  const [state, setState] = useState(null);
  const [history, setHistory] = useState([]);
  const [orderJournal, setOrderJournal] = useState([]);
  const [renderLoading, setRenderLoading] = useState(true);
  const [loadingOpacity, setLoadingOpacity] = useState(1);
  // Phase 8: tab switcher + backtest message stash
  const [activeTab, setActiveTab] = useState("LIVE");   // LIVE | RESEARCH | SETTINGS
  // v2.20.0 Research tab state — populated by ws.onmessage cases below.
  const [researchCoverage, setResearchCoverage] = useState(null);
  const [researchLabResult, setResearchLabResult] = useState(null);
  // T30A — param schema from research_params_current handler.
  const [researchParamsSchema, setResearchParamsSchema] = useState(null);
  // T30B — streaming progress + final result for Mode B walk-forward.
  const [researchLabProgress, setResearchLabProgress] = useState(null);
  // v2.20.0 — top StatCards "Hydra-only" toggle. ON excludes journal entries
  // with source='kraken_backfill' (manual / pre-Hydra trades); OFF shows
  // full history. Persisted to localStorage so the user's choice survives
  // refreshes. Right-sidebar per-pair cards always read full history.
  const [hydraOnly, setHydraOnly] = useState(() => {
    try { return localStorage.getItem("hydra.statcards.hydra_only") === "1"; }
    catch { return false; }
  });
  const [btProgress, setBtProgress] = useState({});     // experiment_id -> progress msg
  const [btResults, setBtResults] = useState({});       // experiment_id -> result summary
  // Phase 9: per-experiment rolling equity history for the observer modal.
  // Shape: {experiment_id -> {pair -> [equity...]}}. Bounded to 500 pts/pair.
  const [btEquityHistory, setBtEquityHistory] = useState({});
  const [btActiveExpId, setBtActiveExpId] = useState(null);  // which exp the observer is focused on
  const [observerClosed, setObserverClosed] = useState(false); // user dismissed → hide until a new run
  // ─── Companion state (Phase 1+) ───
  const [companions, setCompanions] = useState({});             // companion_id -> meta
  const [activeCompanion, setActiveCompanion] = useState(() => {
    try { return localStorage.getItem("hydra.companion.active") || "apex"; }
    catch { return "apex"; }
  });
  const [companionDrawerOpen, setCompanionDrawerOpen] = useState(() => {
    try { return localStorage.getItem("hydra.companion.drawer.open") === "1"; }
    catch { return false; }
  });
  // Drawer width read once from localStorage; interactive resize is a
  // future enhancement and will flip this to useState when wired.
  const companionDrawerWidth = (() => {
    try { return parseInt(localStorage.getItem("hydra.companion.drawer.width") || "380", 10); }
    catch { return 380; }
  })();
  // Per-companion state as INDEPENDENT useState hooks so updates to one
  // companion physically cannot leak into another. A prior object-keyed
  // state had a subtle cross-contamination bug where user-echo messages
  // appeared in all three drawers.
  const [athenaMessages, setAthenaMessages] = useState([]);
  const [apexMessages, setApexMessages] = useState([]);
  const [broskiMessages, setBroskiMessages] = useState([]);
  const [athenaTyping, setAthenaTyping] = useState(false);
  const [apexTyping, setApexTyping] = useState(false);
  const [broskiTyping, setBroskiTyping] = useState(false);
  const [athenaUnread, setAthenaUnread] = useState(false);
  const [apexUnread, setApexUnread] = useState(false);
  const [broskiUnread, setBroskiUnread] = useState(false);
  // Unified read/write helpers. The setter IS a single companion's setter,
  // so overlapping state updates are impossible.
  const getMessages = useCallback((cid) =>
    cid === "athena" ? athenaMessages
    : cid === "apex" ? apexMessages
    : broskiMessages,
    [athenaMessages, apexMessages, broskiMessages]
  );
  const getMessageSetter = useCallback((cid) =>
    cid === "athena" ? setAthenaMessages
    : cid === "apex" ? setApexMessages
    : setBroskiMessages,
    []
  );
  const getTypingSetter = useCallback((cid) =>
    cid === "athena" ? setAthenaTyping
    : cid === "apex" ? setApexTyping
    : setBroskiTyping,
    []
  );
  const getUnreadSetter = useCallback((cid) =>
    cid === "athena" ? setAthenaUnread
    : cid === "apex" ? setApexUnread
    : setBroskiUnread,
    []
  );
  const getTyping = (cid) =>
    cid === "athena" ? athenaTyping
    : cid === "apex" ? apexTyping
    : broskiTyping;
  const getUnread = (cid) =>
    cid === "athena" ? athenaUnread
    : cid === "apex" ? apexUnread
    : broskiUnread;
  const [companionCostAlerts, setCompanionCostAlerts] = useState({});
  const [companionVisible, setCompanionVisible] = useState(true);    // optimistic \u2014 orb shows immediately; hides on failed connect
  // Track in-flight message timeouts so we can cancel them when a reply arrives.
  const pendingTimeoutsRef = useRef({});  // { [msgId]: timeoutHandle }
  const wsRef = useRef(null);
  const reconnectRef = useRef(null);
  // Exponential backoff counter for WS reconnect: doubles each failed
  // attempt up to a cap. Reset to 0 on successful onopen so a transient
  // blip doesn't push subsequent disconnects into slow retry.
  const reconnectAttemptsRef = useRef(0);
  // Per-session WS auth token. Fetched lazily on first send (and on
  // auth_required retry); cached across reconnects unless agent
  // restarts rotate it.
  const wsTokenRef = useRef(null);
  // Latest `connect` closure — the setTimeout reconnect callback reads
  // through this ref instead of the stale closure it captured at
  // definition-time (otherwise ESLint flags a use-before-declare and the
  // retry can fire against an outdated applyLiveState handler after HMR).
  const connectRef = useRef(null);
  // mountedRef guards against setState-on-unmounted warnings (noticeable
  // in StrictMode which double-mounts in dev). WS callbacks capture the
  // ref closure and bail out cleanly when the component has unmounted.
  const mountedRef = useRef(true);

  // Shared state applier — invoked by BOTH the legacy raw-state path and
  // the new wrapped {type:"state", data:state} path.
  const applyLiveState = useCallback((data) => {
    setState(data);
    if (data.pairs) {
      const liveTotal = data.balance_usd?.total_usd;
      const engineEquity = Object.values(data.pairs).reduce((sum, p) => sum + (p.portfolio?.equity || 0), 0);
      setHistory((prev) => [...prev, liveTotal != null ? liveTotal : engineEquity].slice(-500));
    }
    if (data.order_journal) setOrderJournal(data.order_journal);
  }, []);

  // Declared before `connect` so `connect`'s deps array can reference it
  // without hitting the const TDZ (ReferenceError blanked the dashboard
  // in v2.15.0 until this was hoisted).
  const refreshWsToken = useCallback(async () => {
    try {
      const r = await fetch(`${WS_TOKEN_URL}?t=${Date.now()}`, { cache: "no-store" });
      if (!r.ok) throw new Error(`status ${r.status}`);
      const j = await r.json();
      wsTokenRef.current = j.token || "";
      return wsTokenRef.current;
    } catch (e) {
      console.error("[HYDRA] WS token fetch failed:", e);
      return "";
    }
  }, []);

  const [wsUrl, setWsUrl] = useState(() => sanitizeWsUrl(localStorage.getItem("hydra_ws_url")));

  const connect = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) return;
    const ws = new WebSocket(sanitizeWsUrl(wsUrl));
    wsRef.current = ws;
    ws.onopen = async () => {
      // v2.16.2: refresh the auth token FIRST, then flip `connected=true`.
      // Prior ordering raced the async token fetch against any
      // useEffect(() => sendMessage(...), [connected]) — notably the
      // companion.connect kickoff — which sent messages with a stale or
      // empty token and got auth_required, hiding the orb permanently.
      await refreshWsToken();
      if (mountedRef.current) {
        reconnectAttemptsRef.current = 0;
        setConnected(true);
      }
    };
    ws.onmessage = (event) => {
      if (!mountedRef.current) return;
      try {
        const msg = JSON.parse(event.data);

        // Phase 6+ wrapped state: {type:"state", data:{...}}
        if (msg && msg.type === "state" && msg.data) {
          applyLiveState(msg.data);
          return;
        }
        // New typed messages (Phase 6+)
        if (msg && typeof msg.type === "string") {
          switch (msg.type) {
            case "backtest_progress":
              setBtProgress((prev) => lruCapDict(prev, msg.experiment_id, msg, MAX_BACKTEST_DICT_ENTRIES));
              // Accumulate per-pair equity for the observer chart. Cap total
              // stored experiments at MAX_EQUITY_HISTORY_EXPERIMENTS (LRU-ish)
              // so long sessions don't leak memory across many runs.
              if (msg.dashboard_state?.pairs) {
                setBtEquityHistory((prev) => {
                  const prior = prev[msg.experiment_id] || {};
                  const next = { ...prior };
                  for (const [p, ps] of Object.entries(msg.dashboard_state.pairs)) {
                    next[p] = [...(prior[p] || []), ps.portfolio?.equity || 0].slice(-500);
                  }
                  const merged = { ...prev, [msg.experiment_id]: next };
                  const keys = Object.keys(merged);
                  if (keys.length <= MAX_EQUITY_HISTORY_EXPERIMENTS) return merged;
                  // Drop oldest by insertion order; the freshly-written key
                  // is last, so slicing preserves it.
                  const keep = keys.slice(-MAX_EQUITY_HISTORY_EXPERIMENTS);
                  const trimmed = {};
                  for (const k of keep) trimmed[k] = merged[k];
                  return trimmed;
                });
              }
              // Freshest run becomes the observer focus; re-open if the user closed it.
              setBtActiveExpId(msg.experiment_id);
              setObserverClosed(false);
              return;
            case "backtest_result":
              setBtResults((prev) => lruCapDict(prev, msg.experiment_id, msg, MAX_BACKTEST_DICT_ENTRIES));
              return;
            case "backtest_start_ack":
              if (msg.experiment_id) {
                setBtActiveExpId(msg.experiment_id);
                setObserverClosed(false);
              }
              return;
            // ─── Companion channel ───
            case "companion.connect_ack": {
              // v2.16.2: if the agent rotated its token between our last
              // fetch and this send, refresh the token and keep the orb
              // visible — the next user interaction (or a reconnect) will
              // use the fresh token. Without this we'd stay hidden forever.
              if (!msg.success && msg.error === "auth_required") {
                refreshWsToken();
                setCompanionVisible(true);
                return;
              }
              if (msg.success) {
                const metas = {};
                for (const c of (msg.all_companions || [])) metas[c.id] = c;
                if (msg.companion) metas[msg.companion.id] = msg.companion;
                setCompanions((prev) => ({ ...prev, ...metas }));
                setCompanionVisible(true);
                // Seed history for the specific companion the server named.
                // (Was previously in the else-branch by mistake, which meant
                // initial-open history never populated.)
                if (msg.companion && Array.isArray(msg.history_tail)) {
                  const seeded = msg.history_tail.map((t, i) => ({
                    id: `seed-${msg.companion.id}-${i}`,
                    role: t.role, text: t.content,
                    display_name: t.role === "assistant" ? msg.companion.display_name : null,
                  }));
                  getMessageSetter(msg.companion.id)(seeded);
                }
              } else {
                setCompanionVisible(false);
              }
              return;
            }
            case "companion.switch_ack": {
              if (msg.success && msg.companion) {
                setCompanions((prev) => ({ ...prev, [msg.companion.id]: msg.companion }));
                if (Array.isArray(msg.history_tail)) {
                  const seeded = msg.history_tail.map((t, i) => ({
                    id: `seed-${msg.companion.id}-${i}`,
                    role: t.role, text: t.content,
                    display_name: t.role === "assistant" ? msg.companion.display_name : null,
                  }));
                  getMessageSetter(msg.companion.id)(seeded);
                }
              }
              return;
            }
            case "companion.typing": {
              const cid = msg.companion_id;
              if (cid) {
                getTypingSetter(cid)(msg.state === "thinking");
              }
              return;
            }
            case "companion.message.complete": {
              const cid = msg.companion_id;
              if (cid) {
                // Cancel the pending 30s timeout for this msg (if any) so we
                // don't append a "(no response in 30s)" note after the fact.
                const originalMsgId = msg.message_id;
                if (originalMsgId && pendingTimeoutsRef.current[originalMsgId]) {
                  clearTimeout(pendingTimeoutsRef.current[originalMsgId]);
                  delete pendingTimeoutsRef.current[originalMsgId];
                }
                getTypingSetter(cid)(false);
                // Use a unique assistant id that does NOT collide with the
                // user echo id (previously we reused msg.message_id which
                // came from the user's msgId, causing key collisions).
                const assistantId = `a-${originalMsgId || Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
                getMessageSetter(cid)((list) => {
                  // Dedup: if this assistant id is already in the list,
                  // don't add a second copy (guards against double-dispatch
                  // from StrictMode effect re-runs or WS reconnect replays).
                  if (list.some((m) => m.id === assistantId)) return list;
                  return [...list, {
                    id: assistantId,
                    role: "assistant",
                    text: msg.text || "",
                    display_name: companions[cid]?.display_name || COMPANION_NAMES[cid],
                    error: msg.error,
                    intent: msg.intent,
                    model_used: msg.model_used,
                    proactive: msg.proactive === true,
                  }].slice(-200);
                });
                if (!companionDrawerOpen || activeCompanion !== cid) {
                  getUnreadSetter(cid)(true);
                }
              }
              return;
            }
            case "companion.cost_alert": {
              const cid = msg.companion_id;
              if (cid) {
                setCompanionCostAlerts((prev) => ({ ...prev, [cid]: msg }));
              }
              return;
            }
            case "companion.trade.proposal":
            case "companion.ladder.proposal": {
              const cid = msg.companion_id;
              const kind = msg.type === "companion.ladder.proposal" ? "ladder" : "trade";
              if (cid) {
                const proposalEntry = {
                  id: msg.proposal_id, role: "proposal", kind,
                  proposal: msg.card, token: msg.confirmation_token,
                  nonce: msg.nonce, ttl: msg.ttl_expires_at, status: null,
                };
                getMessageSetter(cid)((list) => [...list, proposalEntry].slice(-200));
                if (!companionDrawerOpen || activeCompanion !== cid) {
                  getUnreadSetter(cid)(true);
                }
              }
              return;
            }
            case "companion.trade.executed":
            case "companion.ladder.executed": {
              const cid = msg.companion_id;
              if (cid) {
                getMessageSetter(cid)((list) =>
                  list.map((m) => m.id === msg.proposal_id
                    ? { ...m, status: msg.status || "filled" }
                    : m));
              }
              return;
            }
            case "companion.trade.failed": {
              const cid = msg.companion_id;
              if (cid) {
                getMessageSetter(cid)((list) =>
                  list.map((m) => m.id === msg.proposal_id
                    ? { ...m, status: "failed" }
                    : m));
              }
              return;
            }
            case "companion.ladder.invalidation_triggered": {
              const cid = msg.companion_id;
              if (cid) {
                // Flip the ladder card to "invalidated" status + drop a
                // system note so the user sees what happened.
                getMessageSetter(cid)((list) => {
                  const updated = list.map((m) => m.id === msg.proposal_id
                    ? { ...m, status: "invalidated" }
                    : m);
                  const cancelled = (msg.cancelled_userrefs || []).filter((u) => u != null);
                  return [...updated, {
                    id: `inv-${msg.proposal_id}-${Date.now()}`,
                    role: "system",
                    text: `(ladder invalidated @ $${msg.current_price} \u2014 ` +
                          `cancelled ${cancelled.length} unfilled rung${cancelled.length === 1 ? "" : "s"})`,
                  }].slice(-200);
                });
              }
              return;
            }
            case "companion.system_note": {
              // Route by the sender-declared companion when it's a known one;
              // getMessageSetter falls through to Broski for unknown ids, so
              // an unrecognized companion_id must fall back to the active
              // drawer instead (v2.26.2, audit M4).
              const cid = COMPANION_ORDER.includes(msg.companion_id)
                ? msg.companion_id : activeCompanion;
              getMessageSetter(cid)((list) => [...list, {
                id: `sys-${Date.now()}`,
                role: "system", text: msg.text || "", display_name: null,
              }].slice(-200));
              return;
            }
            case "start_agent_ack":
              if (msg.success && Number.isInteger(msg.port) && msg.port > 0 && msg.port < 65536) {
                const newUrl = sanitizeWsUrl(`ws://localhost:${msg.port}`);
                localStorage.setItem("hydra_ws_url", newUrl);
                setWsUrl(newUrl);
              }
              return;
            case "research_dataset_coverage_ack":
              setResearchCoverage(msg);
              return;
            case "research_lab_run_ack":
              setResearchLabResult(msg);
              return;
            case "research_lab_progress":
              setResearchLabProgress((prev) => {
                const arr = prev || [];
                return [...arr, msg];
              });
              return;
            case "research_lab_result":
              setResearchLabResult(msg);
              setResearchLabProgress(null);  // clear progress accumulator
              return;
            case "research_params_current_ack":
              setResearchParamsSchema(msg);
              return;
            default:
              // Unknown typed message → drop silently. Do NOT fall through
              // to applyLiveState: a malformed backtest-side message with
              // a misnamed `type` could otherwise overwrite live fields
              // (e.g., pairs, brain) with partial/stale data. The legacy
              // raw-state shape has no `type` field at all.
              return;
          }
        }
        // Legacy raw live-state dict: only accept payloads WITHOUT a `type`
        // field AND with at least one recognizable top-level live-state key.
        // This guards against typos in new typed-message names corrupting
        // the LIVE view during the one-release compat window.
        if (msg && typeof msg === "object" && msg.type === undefined
            && LIVE_STATE_KEYS.some((k) => k in msg)) {
          applyLiveState(msg);
        }
      } catch (e) { console.error("[HYDRA] Parse error:", e); }
    };
    ws.onclose = () => {
      if (!mountedRef.current) return;
      setConnected(false);
      // Exponential backoff: 3s, 6s, 12s, 24s, capped at 60s. Jittered
      // ±15% to avoid thundering herd if many dashboards reconnect in
      // lockstep after a backend restart.
      const attempt = reconnectAttemptsRef.current;
      const base = Math.min(3000 * Math.pow(2, attempt), 60000);
      const jitter = base * (0.85 + Math.random() * 0.30);
      reconnectAttemptsRef.current = attempt + 1;
      reconnectRef.current = setTimeout(() => connectRef.current?.(), jitter);
    };
    ws.onerror = () => {
      if (wsUrl !== DEFAULT_WS_URL) {
        console.warn(`[HYDRA] Connection failed on ${wsUrl}. Reverting to default ${DEFAULT_WS_URL}`);
        localStorage.removeItem("hydra_ws_url");
        // Update state to trigger re-render and re-connect via useEffect
        setWsUrl(DEFAULT_WS_URL);
      }
      ws.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applyLiveState, refreshWsToken, wsUrl]);

  // Keep `connectRef` pointing at the freshest connect closure
  useEffect(() => { connectRef.current = connect; }, [connect]);

  useEffect(() => { refreshWsToken(); }, [refreshWsToken]);

  // Phase 8: send a typed WS message.
  // v2.15.0: every send carries the per-session auth token.
  const sendMessage = useCallback((msg) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    try {
      ws.send(JSON.stringify({ ...msg, auth: wsTokenRef.current || "" }));
      return true;
    } catch (e) {
      console.error("[HYDRA] WS send error:", e);
      return false;
    }
  }, []);

  // ─── Companion send/switch + connect kickoff ───
  const companionConnect = useCallback(() => {
    sendMessage({ type: "companion.connect", companion_id: activeCompanion });
  }, [sendMessage, activeCompanion]);

  const companionProposalConfirm = useCallback((m) => {
    const type = m.kind === "ladder" ? "companion.ladder.confirm" : "companion.trade.confirm";
    sendMessage({
      type,
      proposal_id: m.id,
      confirmation_token: m.token,
      nonce: m.nonce,
      ttl_expires_at: m.ttl,
    });
    // Optimistic: mark submitting so buttons hide
    getMessageSetter(activeCompanion)((list) =>
      list.map((x) => x.id === m.id ? { ...x, status: "submitting" } : x));
  }, [sendMessage, activeCompanion, getMessageSetter]);

  const companionProposalReject = useCallback((m) => {
    const type = m.kind === "ladder" ? "companion.ladder.reject" : "companion.trade.reject";
    sendMessage({ type, proposal_id: m.id });
    getMessageSetter(activeCompanion)((list) =>
      list.map((x) => x.id === m.id ? { ...x, status: "rejected" } : x));
  }, [sendMessage, activeCompanion, getMessageSetter]);

  const companionSend = useCallback((text) => {
    const msgId = `u-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const cid = activeCompanion;

    // ─── Slash-command interception (no LLM call) ───
    const trimmed = text.trim();
    if (trimmed.startsWith("/")) {
      const [cmd, ...rest] = trimmed.slice(1).split(/\s+/);
      const arg = rest.join(" ").trim();

      const sysNote = (note) => ({
        id: `sys-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
        role: "system", text: note,
      });

      if (cmd === "clear") {
        const scope = arg === "all" ? "all" : "one";
        sendMessage({
          type: "companion.transcript.clear",
          companion_id: cid, scope,
        });
        if (scope === "all") {
          setAthenaMessages([sysNote("(all three transcripts cleared)")]);
          setApexMessages([]);
          setBroskiMessages([]);
        } else {
          getMessageSetter(cid)([sysNote("(transcript cleared)")]);
        }
        return;
      }

      if (cmd === "help") {
        getMessageSetter(cid)((list) => [...list,
          { id: msgId, role: "user", text },
          sysNote(
            "commands:\n" +
            "  /clear         \u2014 clear this companion's transcript\n" +
            "  /clear all     \u2014 clear all three transcripts\n" +
            "  /mute [secs]   \u2014 silence proactive nudges (default 1h)\n" +
            "  /serious [on|off] \u2014 broski: toggle serious mode\n" +
            "  /help          \u2014 show this list"
          ),
        ].slice(-200));
        return;
      }

      if (cmd === "mute") {
        const secs = arg ? parseInt(arg, 10) : 3600;
        const safe = Number.isFinite(secs) && secs > 0 ? secs : 3600;
        sendMessage({ type: "companion.nudge.mute", seconds: safe });
        getMessageSetter(cid)((list) => [...list,
          { id: msgId, role: "user", text },
          sysNote(`(proactive nudges muted for ${safe}s)`),
        ].slice(-200));
        return;
      }

      if (cmd === "serious") {
        const on = arg !== "off";
        sendMessage({ type: "companion.set_serious_mode", companion_id: cid, on });
        getMessageSetter(cid)((list) => [...list,
          { id: msgId, role: "user", text },
          sysNote(on ? "(serious mode on)" : "(serious mode off)"),
        ].slice(-200));
        return;
      }
    }

    // Optimistic: add the user message immediately to the ACTIVE companion only.
    // Dedup by msgId so any duplicate invocation (stale-closure guard miss,
    // double-click race, StrictMode effect re-run) can't double-commit.
    getMessageSetter(cid)((list) => {
      if (list.some((m) => m.id === msgId)) return list;
      return [...list, { id: msgId, role: "user", text }].slice(-200);
    });
    getTypingSetter(cid)(true);
    const ok = sendMessage({
      type: "companion.message",
      companion_id: cid,
      text, message_id: msgId,
    });
    if (!ok) {
      getTypingSetter(cid)(false);
      getMessageSetter(cid)((list) => [...list, {
        id: `err-${msgId}`, role: "system",
        text: "(not connected to agent \u2014 restart Hydra or refresh)",
      }].slice(-200));
      return;
    }
    // 30s timeout \u2014 helpful error if no reply arrives. Cancellable so a
    // successful message.complete kills the timer instead of spamming the
    // "(no response in 30s)" note after the fact.
    const handle = setTimeout(() => {
      delete pendingTimeoutsRef.current[msgId];
      getMessageSetter(cid)((list) => {
        if (list.some((m) => m.id === `timeout-${msgId}`)) return list;
        return [...list, {
          id: `timeout-${msgId}`, role: "system",
          text: "(no response in 30s \u2014 check the agent console for errors; API key may be missing or model rate-limited)",
        }].slice(-200);
      });
      getTypingSetter(cid)(false);
    }, 30000);
    pendingTimeoutsRef.current[msgId] = handle;
  }, [sendMessage, activeCompanion, getMessageSetter, getTypingSetter]);

  const companionSwitch = useCallback((cid) => {
    setActiveCompanion(cid);
    getUnreadSetter(cid)(false);
    try { localStorage.setItem("hydra.companion.active", cid); } catch { /* ignore */ }
    sendMessage({ type: "companion.switch", to_id: cid });
  }, [sendMessage, getUnreadSetter]);

  const companionToggle = useCallback(() => {
    setCompanionDrawerOpen((prev) => {
      const next = !prev;
      try { localStorage.setItem("hydra.companion.drawer.open", next ? "1" : "0"); } catch { /* ignore */ }
      if (next) getUnreadSetter(activeCompanion)(false);
      return next;
    });
  }, [activeCompanion, getUnreadSetter]);

  // On WS connect, probe the companion subsystem. If unmounted server-side,
  // no connect_ack arrives and the orb stays invisible.
  useEffect(() => {
    if (connected) companionConnect();
  }, [connected, companionConnect]);

  useEffect(() => {
    mountedRef.current = true;
    connect();
    return () => {
      mountedRef.current = false;
      clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  const pairs = state?.pairs || {};
  const pairNames = Object.keys(pairs);
  const isLoaded = state && pairNames.length > 0;

  useEffect(() => {
    if (isLoaded) {
      setLoadingOpacity(0);
      const timer = setTimeout(() => setRenderLoading(false), 600);
      return () => clearTimeout(timer);
    } else {
      setRenderLoading(true);
      setLoadingOpacity(1);
    }
  }, [isLoaded]);
  const balance = state?.balance || {};
  const balanceUsd = state?.balance_usd || null;
  const aiBrain = state?.ai_brain || null;
  const tick = state?.tick || 0;
  const elapsed = state?.elapsed || 0;
  const remaining = state?.remaining || 0;

  // Total Balance: use real exchange balance when available, fall back to engine equity
  const totalEquity = balanceUsd?.total_usd != null ? balanceUsd.total_usd : Object.values(pairs).reduce((s, p) => s + (p.portfolio?.equity || 0), 0);
  // P&L journalPnlUsd is computed below alongside the hydra-only-toggle
  // sourced fields so the toggle gate them in lockstep.
  // Engine round-trip win rate (position fully closed)
  const totalWins = Object.values(pairs).reduce((s, p) => s + (p.performance?.win_count || 0), 0);
  const totalLosses = Object.values(pairs).reduce((s, p) => s + (p.performance?.loss_count || 0), 0);
  const engineWinRate = (totalWins + totalLosses) > 0 ? (totalWins / (totalWins + totalLosses) * 100) : 0;
  // Journal fill stats — computed from FULL journal on the backend (not the
  // 20-entry window shown in the order list). Reflects actual exchange activity.
  // v2.20.0 — top StatCards have a "Hydra-only" toggle: ON excludes
  // entries with source='kraken_backfill' (manual / pre-Hydra trades);
  // OFF shows full history including backfill. Right-sidebar per-pair
  // cards always read full-history (`fillsByPair`, `pnl_by_pair`) — they
  // are not affected by the toggle.
  const jStats = state?.journal_stats || {};
  const fillsByPair = jStats.fills_by_pair || {};
  const totalFills = hydraOnly
    ? (jStats.total_fills_hydra_only || 0)
    : (jStats.total_fills || 0);
  const fillWinRate = hydraOnly
    ? (jStats.fill_win_rate_hydra_only || 0)
    : (jStats.fill_win_rate || 0);
  const journalPnlUsd = hydraOnly
    ? (jStats.total_pnl_usd_hydra_only ?? 0)
    : (jStats.total_pnl_usd ?? 0);
  // Win rate: use journal fill-derived rate when available so it reflects partial closes,
  // falling back to engine round-trip rate.
  const overallWinRate = totalFills > 0 ? fillWinRate : engineWinRate;

  return (
    <div style={{ background: COLORS.bg, minHeight: "100vh", color: COLORS.text, padding: 0 }}>
      {/* Header */}
      <div style={{ borderBottom: `1px solid ${COLORS.panelBorder}`, padding: "16px 24px", display: "flex", alignItems: "center", justifyContent: "space-between", background: `${COLORS.panel}cc`, backdropFilter: "blur(12px)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <img src="/favicon.png" alt="Hydra" style={{ width: 62, height: 62, filter: "drop-shadow(0 0 6px rgba(126, 20, 255, 0.4))" }} />
          <div style={{ 
            fontSize: 28, fontWeight: 900, fontFamily: heading, letterSpacing: "-0.02em",
            background: `linear-gradient(135deg, ${COLORS.accent}, #0d9488)`,
            WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent",
          }}>
            HYDRA
          </div>
          <div style={{ fontSize: 10, color: COLORS.textMuted, fontFamily: mono, lineHeight: 1.3, borderLeft: `1px solid ${COLORS.panelBorder}`, paddingLeft: 10, maxWidth: 220 }}>
            Hyper-adaptive Dynamic<br />Regime-switching Universal Agent
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <TabSwitcher
            activeTab={activeTab}
            onChange={setActiveTab}
          />
          {/* Mode pill — indicates whether the AI brain is attached on top of
              the engine. Prior copy was "AI LIVE" / "LIVE TRADING" which
              collided with the LIVE tab and the connection indicator. */}
          {/* Pill dimensions (padding + fontSize + letterSpacing) match the
              TabSwitcher buttons so the whole header reads as one consistent
              row of controls. QuantumIcon uses COLORS.text (near-white) when
              the brain is active so it pops cleanly against the blue-tinted
              pill background instead of blending with the blue border/text. */}
          <div title={aiBrain
                ? "Market Quant (Claude) + Risk Manager (Claude) + Grok Strategist are reasoning over engine signals."
                : "Pure engine execution — no AI brain attached. Signals run straight from the engine to the order layer."}
               style={{ padding: "0 14px", minHeight: 38, borderRadius: 4,
                        fontSize: 12, fontWeight: 700, fontFamily: mono,
                        display: "inline-flex", alignItems: "center", gap: 8,
                        background: aiBrain ? `${COLORS.blue}18` : "transparent",
                        color: aiBrain ? COLORS.blue : COLORS.textDim,
                        border: `1px solid ${aiBrain ? `${COLORS.blue}60` : COLORS.panelBorder}`,
                        textTransform: "uppercase", letterSpacing: "0.1em" }}>
            <QuantumIcon active={!!aiBrain} size={22}
                         color={aiBrain ? COLORS.text : COLORS.textDim} />
            {aiBrain ? "AI Brain" : "Engine Only"}
          </div>
          <ConnectionStatus connected={connected} tick={tick} />
          {elapsed > 0 && (
            <span style={{ fontSize: 11, fontFamily: mono, color: COLORS.textDim }}>
              {Math.floor(elapsed / 60)}m{Math.floor(elapsed % 60)}s{remaining > 0 ? ` / ${Math.floor((elapsed + remaining) / 60)}m` : ""}
            </span>
          )}
        </div>
      </div>

      {/* Phase 8/9: BACKTEST + COMPARE tab content. LIVE falls through to the
          existing grid below. Phase 9: observer modal is also surfaced as a
          floating right-side panel on LIVE when a run is mid-flight. */}
      {(() => {
        // Pick the freshest experiment to observe: the one the user most
        // recently kicked off, or the freshest progress / result in memory.
        const obsId = btActiveExpId
                   || Object.keys(btProgress).slice(-1)[0]
                   || Object.keys(btResults).slice(-1)[0]
                   || null;
        const obsProgress = obsId ? btProgress[obsId] : null;
        const obsResult = obsId ? btResults[obsId] : null;
        const obsEquity = obsId ? btEquityHistory[obsId] : null;
        // Best-effort total-ticks hint: parse data_source_params n_candles
        // from config if we have it on the result; else 0 → indeterminate bar.
        let totalTicks = 0;
        if (obsResult?.config?.data_source_params_json) {
          try { totalTicks = JSON.parse(obsResult.config.data_source_params_json).n_candles || 0; }
          catch { /* ignore */ }
        }

        return (
          <>
            {activeTab === "SETTINGS" && (
              <div style={{ padding: "16px 24px" }}>
                <SettingsSurface wsSend={sendMessage} />
              </div>
            )}
            {activeTab === "RESEARCH" && (
              <ResearchTab
                sendMessage={sendMessage}
                coverageData={researchCoverage}
                labResult={researchLabResult}
                labProgress={researchLabProgress}
                paramsSchema={researchParamsSchema}
                clearLabRunState={() => {
                  setResearchLabResult(null);
                  setResearchLabProgress(null);
                }}
              />
            )}
            {/* Floating observer on LIVE tab — dual-state view. Appears
                whenever a backtest is mid-run or just completed; user can
                dismiss. Shares the exact same ObserverModal component as
                the BACKTEST dock so visuals match. */}
            {activeTab === "LIVE" && !observerClosed && obsId && (obsProgress || obsResult) && (
              <ObserverModal
                progress={obsProgress}
                result={obsResult}
                equityHistory={obsEquity}
                totalTicks={totalTicks}
                variant="floating"
                onClose={() => setObserverClosed(true)}
              />
            )}
          </>
        );
      })()}

      {activeTab === "LIVE" && renderLoading ? (
        <div style={{ opacity: loadingOpacity, transition: "opacity 0.6s ease-in-out", position: isLoaded ? "absolute" : "relative", zIndex: 50, top: 0, left: 0, right: 0, bottom: 0, background: isLoaded ? COLORS.bg : "transparent" }}>
          <LoadingScreen connected={connected} />
        </div>
      ) : null}

      {activeTab === "LIVE" && isLoaded && (
        <div style={{ padding: "16px 24px", animation: "hc-dashboard-fade-in 0.8s ease-out forwards" }}>
          <style>{`@keyframes hc-dashboard-fade-in { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: translateY(0); } }`}</style>
          {/* Full grid — stats span top, then pair panels + sidebar below */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 280px", gap: 12, alignItems: "start" }}>
            {/* Stats Row — spans both columns for edge-to-edge alignment.
                Hydra-only toggle gates Fills / Win Rate / P&L cards. */}
            <div style={{ gridColumn: "1 / -1", display: "flex", gap: 8, alignItems: "stretch" }}>
              <button
                onClick={() => {
                  const next = !hydraOnly;
                  setHydraOnly(next);
                  try { localStorage.setItem("hydra.statcards.hydra_only", next ? "1" : "0"); } catch { /* ignore */ }
                }}
                title={hydraOnly
                  ? "Showing Hydra-placed trades only (excludes kraken_backfill source). Click to show all trades."
                  : "Showing all trades (full history including backfill). Click to filter to Hydra-only."}
                style={{
                  padding: "0 14px",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 8,
                  fontSize: 11,
                  fontWeight: 700,
                  fontFamily: mono,
                  letterSpacing: "0.08em",
                  textTransform: "uppercase",
                  background: hydraOnly ? `${COLORS.accent}18` : "transparent",
                  color: hydraOnly ? COLORS.accent : COLORS.textDim,
                  border: `1px solid ${hydraOnly ? `${COLORS.accent}60` : COLORS.panelBorder}`,
                  borderRadius: 6,
                  cursor: "pointer",
                  outline: "none",
                  whiteSpace: "nowrap",
                }}
              >
                <span
                  style={{
                    width: 7, height: 7, borderRadius: "50%",
                    background: hydraOnly ? COLORS.accent : COLORS.textDim,
                    boxShadow: hydraOnly ? `0 0 8px ${COLORS.accent}80` : "none",
                  }}
                />
                {hydraOnly ? "Hydra-only" : "All trades"}
              </button>
              <StatCard label="Total Balance" value={`$${totalEquity.toFixed(2)}`} color={COLORS.text} />
              <StatCard label="P&L" value={`${journalPnlUsd >= 0 ? "+$" : "-$"}${Math.abs(journalPnlUsd).toFixed(2)}`} color={journalPnlUsd >= 0 ? COLORS.buy : COLORS.sell} />
              <StatCard label="Fills" value={totalFills} color={COLORS.blue} />
              <StatCard label="Win Rate" value={overallWinRate.toFixed(0)} unit="%" color={overallWinRate > 55 ? COLORS.buy : overallWinRate > 0 ? COLORS.warn : COLORS.textDim} />
            </div>
            {/* Max DD card removed in v2.20.0 — portfolio-level DD was session-scoped
                (reset on agent restart) which became inconsistent with the other
                journal-derived top stats. Per-pair Drawdown remains on the right
                sidebar cards. A journal-derived equity-curve drawdown that respects
                the Hydra-only toggle is on the v2.21.0 backlog. */}
            {/* LEFT: Pair panels + equity + trade log */}
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {pairNames.map((pair) => {
                const ps = pairs[pair];
                const sig = ps.signal || {};
                const port = ps.portfolio || {};
                const pos = ps.position || {};
                const ind = ps.indicators || {};

                return (
                  <div key={pair} style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`, borderRadius: 10, padding: 16 }}>
                    {/* Pair header */}
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                      <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
                        <span style={{ fontSize: 16, fontWeight: 700, fontFamily: heading, color: COLORS.text }}>{pair}</span>
                        <span style={{ fontSize: 22, fontWeight: 700, fontFamily: mono, color: COLORS.text }}>{fmtPrice(ps.price || 0, pairPrefix(pair))}</span>
                        {ps.tradable === false && (
                          <span title="Signal-only: the quote currency for this pair isn't held, so orders won't be placed. Signals still feed cross-pair confluence."
                                style={{ fontSize: 9, fontFamily: mono, color: COLORS.warn,
                                         background: `${COLORS.warn}18`, padding: "2px 6px",
                                         borderRadius: 3, letterSpacing: "0.08em", fontWeight: 700 }}>
                            INFO-ONLY
                          </span>
                        )}
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <div style={{ width: 7, height: 7, borderRadius: "50%", background: regimeColor(ps.regime),
                                      boxShadow: `0 0 12px ${regimeColor(ps.regime)}cc, 0 0 4px ${regimeColor(ps.regime)}` }} />
                        <span style={{ fontSize: 11, fontWeight: 700, color: regimeColor(ps.regime), fontFamily: mono,
                                       textTransform: "uppercase", textShadow: `0 0 10px ${regimeColor(ps.regime)}80` }}>
                          {(ps.regime || "").replace("_", " ")}
                        </span>
                        {ps.strategy && (
                          <span style={{ fontSize: 9, fontFamily: mono, color: COLORS.textMuted,
                                         background: `${regimeColor(ps.regime)}40`, padding: "2px 7px",
                                         borderRadius: 3, letterSpacing: "0.05em", textTransform: "uppercase",
                                         boxShadow: `0 0 8px ${regimeColor(ps.regime)}30` }}>
                            {ps.strategy.replace("_", " ")}
                          </span>
                        )}
                      </div>
                    </div>

                    {/* Candlestick Chart */}
                    {(ps.candles && ps.candles.length > 5) && (
                      <div style={{ background: "#0d0d0f", borderRadius: 8, border: `1px solid ${COLORS.panelBorder}`, overflow: "hidden", margin: "0 -4px" }}>
                        <CandleChart candles={ps.candles.slice(-80)} height={254} />
                      </div>
                    )}

                    {/* Signal + Position + Equity row */}
                    <div style={{ display: "flex", gap: 16, marginTop: 10 }}>
                      {/* Signal */}
                      <div style={{ flex: 1 }}>
                        <ConfidenceMeter confidence={sig.confidence || 0} signal={sig.action || "HOLD"} />
                        {ps.cross_pair_override && ps.cross_pair_override.confluence_source && (
                          <div style={{ marginTop: 4, display: "inline-flex", alignItems: "center", gap: 6,
                                        fontSize: 10, fontFamily: mono, color: COLORS.accent,
                                        background: `${COLORS.accent}18`, padding: "2px 6px", borderRadius: 3,
                                        letterSpacing: "0.04em", fontWeight: 700 }}
                               title={`Rule 4 confluence: ${ps.cross_pair_override.confluence_source.source_pair} conf ${ps.cross_pair_override.confluence_source.other_conf}`}>
                            ρ={(ps.cross_pair_override.confluence_source.rho ?? 0).toFixed(2)}
                            &nbsp;↑ +{(ps.cross_pair_override.confluence_source.bonus ?? 0).toFixed(3)}
                          </div>
                        )}
                        <div style={{ fontSize: 11, color: COLORS.textMuted, fontFamily: mono, lineHeight: 1.4 }}>{sig.reason || ""}</div>
                      </div>
                      {/* Position */}
                      <div style={{ minWidth: 170, borderLeft: `1px solid ${COLORS.panelBorder}`, paddingLeft: 16 }}>
                        <div style={{ fontSize: 10, color: COLORS.textDim, textTransform: "uppercase", fontFamily: mono, marginBottom: 4 }}>Position</div>
                        {pos.size > 0 ? (
                          <>
                            <div style={{ fontSize: 14, fontWeight: 700, fontFamily: mono }}>{pos.size.toFixed(8)}</div>
                            <div style={{ fontSize: 10, color: COLORS.textDim, fontFamily: mono }}>@ {fmtPrice(pos.avg_entry || 0, pairPrefix(pair))}</div>
                            <div style={{ fontSize: 12, fontWeight: 700, fontFamily: mono, color: (pos.unrealized_pnl || 0) >= 0 ? COLORS.buy : COLORS.sell, marginTop: 2 }}>
                              {fmtPrice(Math.abs(pos.unrealized_pnl || 0), (pos.unrealized_pnl || 0) >= 0 ? "+" + pairPrefix(pair) : "-" + pairPrefix(pair))}
                            </div>
                          </>
                        ) : (
                          <div style={{ fontSize: 11, color: COLORS.textMuted, fontFamily: mono }}>Flat</div>
                        )}
                      </div>
                      {/* Equity */}
                      <div style={{ minWidth: 110, borderLeft: `1px solid ${COLORS.panelBorder}`, paddingLeft: 16 }}>
                        <div style={{ fontSize: 10, color: COLORS.textDim, textTransform: "uppercase", fontFamily: mono, marginBottom: 4 }}>Balance</div>
                        <div style={{ fontSize: 14, fontWeight: 700, fontFamily: mono }}>{fmtPrice(port.equity || 0, pairPrefix(pair))}</div>
                        <div style={{ fontSize: 11, fontFamily: mono, color: (port.pnl_pct || 0) >= 0 ? COLORS.buy : COLORS.sell }}>
                          {(port.pnl_pct || 0) >= 0 ? "+" : ""}{(port.pnl_pct || 0).toFixed(2)}%
                        </div>
                      </div>
                    </div>

                    {/* Indicators */}
                    {ind.rsi !== undefined && (
                      <div style={{ display: "flex", gap: 16, marginTop: 8, fontSize: 11, fontFamily: mono, color: COLORS.textDim, flexWrap: "wrap" }}>
                        <span>RSI <span style={{ color: ind.rsi > 70 ? COLORS.sell : ind.rsi < 30 ? COLORS.buy : COLORS.text, fontWeight: 600 }}>{ind.rsi}</span></span>
                        <span>MACD <span style={{ color: (ind.macd_histogram || 0) > 0 ? COLORS.buy : COLORS.sell, fontWeight: 600 }}>{fmtInd(ind.macd_histogram)}</span></span>
                        <span>BB <span style={{ color: COLORS.text }}>[{fmtInd(ind.bb_lower)} — {fmtInd(ind.bb_upper)}]</span></span>
                        <span>Width <span style={{ color: (ind.bb_width || 0) > 0.06 ? COLORS.volatile : COLORS.text, fontWeight: 600 }}>{((ind.bb_width || 0) * 100).toFixed(2)}%</span></span>
                        {(() => {
                          const fees = state?.fee_tier?.pair_fees?.[pair];
                          if (!fees) return null;
                          const m = fees.maker_pct;
                          const t = fees.taker_pct;
                          // Only render when at least one side has a real numeric value —
                          // otherwise null would silently collapse to "0.00%" via `?? 0`,
                          // misleading the user into thinking fees are zero.
                          if (m == null && t == null) return null;
                          const fmt = (v) => (v == null ? "—" : v.toFixed(2));
                          return (
                            <span>Fee M/T <span style={{ color: COLORS.text, fontWeight: 600 }}>
                              {fmt(m)}/{fmt(t)}%
                            </span></span>
                          );
                        })()}
                      </div>
                    )}

                    {/* Spread from TickerStream */}
                    {ps.spread && ps.spread.spread_bps != null && (
                      <span style={{ marginLeft: 12, color: COLORS.textMuted, fontSize: 11 }}>
                        Spread <span style={{ color: COLORS.text, fontWeight: 600 }}>{(ps.spread.spread_bps || 0).toFixed(1)}</span> bps
                      </span>
                    )}

                    {/* AI Reasoning — v2.14.2 7-band redesign. Each band
                        surfaces one layer of the brain's structured output
                        (header → QUANT text+chips → quant indicators grid →
                        RISK → GROK → SIZE/rules). The
                        raw payload is produced by hydra_agent.py:3245
                        (ai_decision dict) and hydra_brain.py:662
                        (BrainDecision dataclass); if a field is null/empty
                        the band self-hides. */}
                    {ps.ai_decision && !ps.ai_decision.fallback && (() => {
                      const ai = ps.ai_decision;
                      const actionColor = ai.action === "CONFIRM" ? COLORS.buy : ai.action === "ADJUST" ? COLORS.warn : COLORS.sell;
                      const bias = (ai.positioning_bias || "").toLowerCase();
                      const biasMeta = bias === "crowded_long" ? { label: "CROWDED LONG", color: COLORS.sell }
                        : bias === "crowded_short" ? { label: "CROWDED SHORT", color: COLORS.buy }
                        : bias === "balanced" ? { label: "BALANCED", color: COLORS.textDim }
                        : null;
                      const qi = ai.quant_indicators || null;
                      const tickDelta = ai.cached && typeof ai.generated_at_tick === "number" && typeof state?.tick === "number"
                        ? Math.max(0, state.tick - ai.generated_at_tick) : null;
                      const cachedColor = tickDelta == null ? COLORS.textMuted
                        : tickDelta > 30 ? COLORS.sell
                        : tickDelta > 10 ? COLORS.warn
                        : COLORS.textMuted;
                      const pill = (txt, color, bg = null, extra = {}) => (
                        <span style={{
                          fontSize: 8, fontFamily: mono, fontWeight: 700, letterSpacing: "0.06em",
                          textTransform: "uppercase", padding: "2px 6px", borderRadius: 3,
                          background: bg ?? `${color}18`, color, ...extra,
                        }}>{txt}</span>
                      );
                      const label = (txt, color) => (
                        <span style={{
                          fontSize: 8, fontWeight: 700, fontFamily: mono, color,
                          textTransform: "uppercase", letterSpacing: "0.08em",
                        }}>{txt}</span>
                      );
                      return (
                        <div style={{ marginTop: 8, padding: "10px 12px", background: `${COLORS.purple}10`, border: `1px solid ${COLORS.purple}25`, borderRadius: 6, display: "flex", flexDirection: "column", gap: 8 }}>
                          {/* Band 1 — Header row */}
                          <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                            {pill(`AI ${ai.action}`, actionColor, `${actionColor}20`)}
                            {ai.final_signal && pill(ai.final_signal, signalColor(ai.final_signal))}
                            {ai.portfolio_health && ai.portfolio_health !== "HEALTHY" && pill(
                              ai.portfolio_health,
                              ai.portfolio_health === "DANGER" ? COLORS.sell : COLORS.warn
                            )}
                            {biasMeta && pill(biasMeta.label, biasMeta.color)}
                            {typeof ai.signal_agreement === "boolean" && (
                              <span
                                title={ai.signal_agreement ? "Quant agrees with engine signal" : "Quant disagrees with engine signal"}
                                style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 8, fontFamily: mono, color: ai.signal_agreement ? COLORS.buy : COLORS.warn }}
                              >
                                <span style={{ width: 6, height: 6, borderRadius: "50%", background: ai.signal_agreement ? COLORS.buy : COLORS.warn, display: "inline-block" }} />
                                {ai.signal_agreement ? "AGREE" : "DISAGREE"}
                              </span>
                            )}
                            {typeof ai.confidence_adj === "number" && (
                              <span style={{ fontSize: 8, fontFamily: mono, color: COLORS.textDim }}>
                                conv {(ai.confidence_adj * 100).toFixed(0)}%
                              </span>
                            )}
                            {ai.latency_ms > 0 && (
                              <span style={{ fontSize: 8, fontFamily: mono, color: COLORS.textMuted, marginLeft: "auto" }}>{ai.latency_ms}ms</span>
                            )}
                          </div>

                          {/* Band 2 — QUANT reasoning + key factors + concern */}
                          {(ai.analyst_reasoning || (ai.key_factors && ai.key_factors.length) || ai.concern) && (
                            <div style={{ padding: "6px 8px", background: `${COLORS.blue}10`, borderRadius: 4, display: "flex", flexDirection: "column", gap: 6 }}>
                              <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
                                {pill("QUANT", COLORS.blue)}
                                {ai.analyst_reasoning && (
                                  <span style={{ fontSize: 11, fontFamily: mono, color: COLORS.text, lineHeight: 1.5, flex: 1 }}>
                                    {ai.analyst_reasoning}
                                  </span>
                                )}
                              </div>
                              {ai.key_factors && ai.key_factors.length > 0 && (
                                <div style={{ display: "flex", gap: 4, flexWrap: "wrap", paddingLeft: 50 }}>
                                  {ai.key_factors.map((f, fi) => (
                                    <span key={fi} style={{ fontSize: 8, fontFamily: mono, padding: "1px 6px", borderRadius: 3, background: `${COLORS.blue}25`, color: COLORS.blue, letterSpacing: "0.03em" }}>
                                      {f}
                                    </span>
                                  ))}
                                </div>
                              )}
                              {ai.concern && (
                                <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
                                  {pill("CONCERN", COLORS.warn)}
                                  <span style={{ fontSize: 10, fontFamily: mono, color: COLORS.warn, lineHeight: 1.4, flex: 1 }}>
                                    {ai.concern}
                                  </span>
                                </div>
                              )}
                            </div>
                          )}

                          {/* Band 3 — Quant indicators grid (derivatives signal block) */}
                          {qi && Object.values(qi).some(v => v !== null && v !== undefined) && (
                            <div style={{ display: "flex", gap: 14, flexWrap: "wrap", padding: "6px 8px", background: `${COLORS.blue}08`, borderRadius: 4, alignItems: "baseline" }}>
                              {qi.funding_bps_8h != null && (
                                <span style={{ display: "inline-flex", gap: 4, alignItems: "baseline" }}>
                                  {label("FUNDING 8H", COLORS.textMuted)}
                                  <span style={{ fontSize: 10, fontFamily: mono, color: Math.abs(qi.funding_bps_8h) > 80 ? COLORS.warn : COLORS.text }}>
                                    {qi.funding_bps_8h > 0 ? "+" : ""}{qi.funding_bps_8h.toFixed(1)} bps
                                  </span>
                                </span>
                              )}
                              {qi.oi_delta_1h_pct != null && (
                                <span style={{ display: "inline-flex", gap: 4, alignItems: "baseline" }}>
                                  {label("OI Δ 1H", COLORS.textMuted)}
                                  <span style={{ fontSize: 10, fontFamily: mono, color: COLORS.text }}>
                                    {qi.oi_delta_1h_pct > 0 ? "+" : ""}{qi.oi_delta_1h_pct.toFixed(2)}%
                                  </span>
                                </span>
                              )}
                              {qi.oi_price_regime && qi.oi_price_regime !== "unknown" && (() => {
                                const r = qi.oi_price_regime;
                                const c = r === "trend_confirm_long" ? COLORS.buy
                                  : r === "trend_confirm_short" ? COLORS.sell
                                  : r === "short_squeeze" || r === "liquidation_cascade" ? COLORS.warn
                                  : COLORS.textDim;
                                return (
                                  <span style={{ display: "inline-flex", gap: 4, alignItems: "baseline" }}>
                                    {label("OI REGIME", COLORS.textMuted)}
                                    {pill(r.replace(/_/g, " "), c)}
                                  </span>
                                );
                              })()}
                              {qi.basis_apr_pct != null && (
                                <span style={{ display: "inline-flex", gap: 4, alignItems: "baseline" }}>
                                  {label("BASIS", COLORS.textMuted)}
                                  <span style={{ fontSize: 10, fontFamily: mono, color: qi.basis_apr_pct > 40 ? COLORS.warn : qi.basis_apr_pct < 0 ? COLORS.sell : COLORS.text }}>
                                    {qi.basis_apr_pct > 0 ? "+" : ""}{qi.basis_apr_pct.toFixed(1)}% APR
                                  </span>
                                </span>
                              )}
                              {qi.cvd_divergence_sigma != null && (
                                <span style={{ display: "inline-flex", gap: 4, alignItems: "baseline" }}>
                                  {label("CVD DIV", COLORS.textMuted)}
                                  <span style={{ fontSize: 10, fontFamily: mono, color: Math.abs(qi.cvd_divergence_sigma) > 2 ? COLORS.warn : COLORS.text }}>
                                    {qi.cvd_divergence_sigma > 0 ? "+" : ""}{qi.cvd_divergence_sigma.toFixed(2)}σ
                                  </span>
                                </span>
                              )}
                            </div>
                          )}

                          {/* Band 4 — RISK reasoning + flags */}
                          {(ai.risk_reasoning || (ai.risk_flags && ai.risk_flags.length > 0)) && (
                            <div style={{ padding: "6px 8px", background: `${COLORS.risk}10`, borderRadius: 4, display: "flex", flexDirection: "column", gap: 6 }}>
                              {ai.risk_reasoning && (
                                <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
                                  {pill("RISK", COLORS.risk)}
                                  <span style={{ fontSize: 11, fontFamily: mono, color: COLORS.text, lineHeight: 1.4, flex: 1 }}>
                                    {ai.risk_reasoning}
                                  </span>
                                </div>
                              )}
                              {ai.risk_flags && ai.risk_flags.length > 0 && (
                                <div style={{ display: "flex", gap: 4, flexWrap: "wrap", paddingLeft: 50 }}>
                                  {ai.risk_flags.map((flag, fi) => (
                                    <span key={fi} style={{ fontSize: 8, fontFamily: mono, padding: "1px 6px", borderRadius: 3, background: `${COLORS.risk}25`, color: COLORS.risk, letterSpacing: "0.03em" }}>
                                      {flag}
                                    </span>
                                  ))}
                                </div>
                              )}
                            </div>
                          )}

                          {/* Band 5 — GROK STRATEGIST (only when escalated) */}
                          {ai.escalated && ai.strategist_reasoning && (
                            <div style={{ padding: "6px 8px", background: `${COLORS.warn}10`, borderRadius: 4, display: "flex", alignItems: "flex-start", gap: 8 }}>
                              {pill("GROK STRATEGIST", COLORS.warn)}
                              <span style={{ fontSize: 11, fontFamily: mono, color: COLORS.text, lineHeight: 1.4, flex: 1 }}>
                                {ai.strategist_reasoning}
                              </span>
                            </div>
                          )}

                          {/* Band 6 — SIZE breakdown + rules + cached badge */}
                          {(typeof ai.size_multiplier === "number" ||
                            (ai.rules_triggered && ai.rules_triggered.length > 0) ||
                            ai.rules_force_hold) && (
                            <div style={{ padding: "6px 8px", background: `${COLORS.panelBorder}25`, borderRadius: 4, fontFamily: mono, display: "flex", flexDirection: "column", gap: 4 }}>
                              <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", fontSize: 10 }}>
                                {label("SIZE", COLORS.textMuted)}
                                <span style={{ color: COLORS.text }}>brain ×{(ai.size_multiplier_brain ?? 1).toFixed(2)}</span>
                                <span style={{ color: COLORS.textMuted }}>·</span>
                                <span style={{ color: COLORS.text }}>rules ×{(ai.size_multiplier_rules ?? 1).toFixed(2)}</span>
                                <span style={{ color: COLORS.textMuted }}>=</span>
                                <span style={{ color: COLORS.accent, fontWeight: 700 }}>×{(ai.size_multiplier ?? 1).toFixed(2)}</span>
                                {ai.size_multiplier_clamped && pill(
                                  `CLAMPED (raw ×${(ai.size_multiplier_unclamped ?? 0).toFixed(2)})`,
                                  COLORS.warn
                                )}
                                {ai.rules_force_hold && !ai.qfe_active && pill("RULES FORCE-HOLD", COLORS.sell, `${COLORS.sell}25`)}
                                {ai.qfe_active && pill("QFE PROFIT EXIT", COLORS.buy, `${COLORS.buy}25`)}
                                {ai.cached && (
                                  <span
                                    title={tickDelta != null && tickDelta > 30 ? `Stale — brain has not re-deliberated in ${tickDelta} ticks` : undefined}
                                    style={{ fontSize: 8, color: cachedColor, fontStyle: "italic", marginLeft: "auto" }}
                                  >
                                    cached{tickDelta != null ? ` Δ${tickDelta} tick${tickDelta === 1 ? "" : "s"}` : ""}
                                  </span>
                                )}
                              </div>
                              {ai.rules_triggered && ai.rules_triggered.length > 0 && (
                                <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                                  {ai.rules_triggered.map((r, ri) => {
                                    const effectColor = r.effect === "force_hold" ? COLORS.sell
                                      : r.effect === "boost" ? COLORS.buy
                                      : COLORS.warn;
                                    return (
                                      <span key={ri} title={r.reason || ""} style={{
                                        fontSize: 8, fontFamily: mono, fontWeight: 700,
                                        padding: "1px 6px", borderRadius: 2,
                                        background: `${COLORS.panelBorder}50`, color: COLORS.text,
                                        borderLeft: `2px solid ${effectColor}`,
                                        letterSpacing: "0.04em",
                                      }}>
                                        {r.rule_id}
                                      </span>
                                    );
                                  })}
                                </div>
                              )}
                              {ai.rules_force_hold && ai.rules_force_hold_reason && !ai.qfe_active && (
                                <div style={{ fontSize: 9, color: COLORS.sell, lineHeight: 1.4 }}>
                                  {ai.rules_force_hold_reason}
                                </div>
                              )}
                              {ai.qfe_active && ai.qfe_reason && (
                                <div style={{ fontSize: 9, color: COLORS.buy, lineHeight: 1.4 }}>
                                  {ai.qfe_reason}
                                </div>
                              )}
                            </div>
                          )}

                        </div>
                      );
                    })()}
                  </div>
                );
              })}

              {/* Balance History */}
              {history.length > 5 && (
                <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`, borderRadius: 10, padding: 14 }}>
                  <div style={{ fontSize: 10, fontWeight: 600, color: COLORS.textDim, marginBottom: 6, fontFamily: mono, textTransform: "uppercase", letterSpacing: "0.08em" }}>Balance History</div>
                  <MiniChart data={history} width={700} height={70} color={journalPnlUsd >= 0 ? COLORS.accent : COLORS.danger} filled />
                </div>
              )}

              {/* Order Journal */}
              <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`, borderRadius: 10, overflow: "hidden" }}>
                <div style={{ padding: "8px 14px", borderBottom: `1px solid ${COLORS.panelBorder}`, fontSize: 10, fontWeight: 600, color: COLORS.textDim, fontFamily: mono, textTransform: "uppercase", letterSpacing: "0.08em" }}>
                  Order Journal ({orderJournal.length})
                </div>
                <div style={{ maxHeight: 180, overflowY: "auto" }}>
                  {orderJournal.length === 0 && (
                    <div style={{ color: COLORS.textMuted, fontSize: 10, padding: 12, fontFamily: mono }}>Awaiting first order...</div>
                  )}
                  {orderJournal.filter(e => e?.lifecycle?.state !== "PLACEMENT_FAILED").slice().reverse().map((entry, i) => {
                    const lifecycle = entry.lifecycle || {};
                    const intent = entry.intent || {};
                    const decision = entry.decision || {};
                    // Renamed from `state` to `entryState` to avoid shadowing
                    // the outer `state` component state variable.
                    const entryState = lifecycle.state || "PLACED";
                    const isFilled = entryState === "FILLED";
                    const icon = isFilled ? "\u2713" : (entryState === "PLACED" ? "\u22ef" : "\u2717");
                    const iconColor = isFilled ? COLORS.accent : (entryState === "PLACED" ? COLORS.textDim : COLORS.danger);
                    const amount = intent.amount || 0;
                    const price = lifecycle.avg_fill_price || intent.limit_price || 0;
                    const reasonLine = lifecycle.terminal_reason
                      ? `${entryState}: ${lifecycle.terminal_reason}`
                      : (decision.reason || entryState);
                    return (
                      <div key={i} style={{ display: "flex", alignItems: "center", gap: 6, padding: "5px 12px", borderBottom: `1px solid ${COLORS.panelBorder}`, fontSize: 9, fontFamily: mono }}>
                        <span style={{ width: 14, fontWeight: 700, color: iconColor }}>{icon}</span>
                        <span style={{ width: 30, fontWeight: 700, color: entry.side === "BUY" ? COLORS.buy : COLORS.sell }}>{entry.side}</span>
                        <span style={{ width: 75 }}>{amount.toFixed(6)}</span>
                        <span style={{ width: 65, color: COLORS.textDim }}>{entry.pair}</span>
                        <span style={{ width: 85 }}>{fmtPrice(price, pairPrefix(entry.pair))}</span>
                        <span style={{ flex: 1, color: COLORS.textMuted, fontSize: 8, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{reasonLine}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>

            {/* RIGHT SIDEBAR — aligned with first pair panel */}
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {/* Kraken Account */}
              <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`, borderRadius: 8, padding: 12 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                  <div style={{ fontSize: 10, color: COLORS.textDim, textTransform: "uppercase", letterSpacing: "0.08em", fontFamily: mono }}>Kraken Account</div>
                  {balanceUsd && (
                    <div style={{ fontSize: 11, color: COLORS.text, fontWeight: 700, fontFamily: mono }}>${balanceUsd.total_usd?.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
                  )}
                </div>
                {balanceUsd?.assets?.length > 0 ? balanceUsd.assets.map((a) => (
                  <div key={a.asset} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontFamily: mono, fontSize: 11, padding: "2px 0", opacity: a.staked ? 0.5 : 1 }}>
                    <span style={{ color: COLORS.textDim }}>
                      {a.asset}{a.staked && <span style={{ fontSize: 8, color: COLORS.warn, marginLeft: 4, textTransform: "uppercase" }}>staked</span>}
                    </span>
                    <span style={{ display: "flex", gap: 8 }}>
                      <span style={{ color: COLORS.textMuted }}>{(a.amount ?? 0).toFixed(6)}</span>
                      {a.usd_value > 0 && <span style={{ color: COLORS.text, fontWeight: 600 }}>${a.usd_value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>}
                    </span>
                  </div>
                )) : Object.entries(balance).length > 0 ? Object.entries(balance).map(([asset, amount]) => (
                  <div key={asset} style={{ display: "flex", justifyContent: "space-between", fontFamily: mono, fontSize: 11, padding: "2px 0" }}>
                    <span style={{ color: COLORS.textDim }}>{asset}</span>
                    <span style={{ color: COLORS.text, fontWeight: 600 }}>{typeof amount === "number" ? amount.toFixed(6) : amount}</span>
                  </div>
                )) : (
                  <div style={{ fontSize: 9, color: COLORS.textMuted, fontFamily: mono }}>Loading...</div>
                )}
                {balanceUsd && balanceUsd.staked_usd > 0 && (
                  <div style={{ marginTop: 6, paddingTop: 6, borderTop: `1px solid ${COLORS.panelBorder}`, display: "flex", justifyContent: "space-between", fontFamily: mono, fontSize: 10 }}>
                    <span style={{ color: COLORS.textMuted }}>Tradable</span>
                    <span style={{ color: COLORS.accent, fontWeight: 600 }}>${balanceUsd.tradable_usd?.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                  </div>
                )}
              </div>

              {/* Strategy Matrix */}
              <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`, borderRadius: 8, padding: 12 }}>
                <div style={{ fontSize: 10, color: COLORS.textDim, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8, fontFamily: mono }}>Strategy Matrix</div>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {[
                    { regime: "TREND_UP", strategy: "MOMENTUM" },
                    { regime: "TREND_DOWN", strategy: "DEFENSIVE" },
                    { regime: "RANGING", strategy: "MEAN_REVERSION" },
                    { regime: "VOLATILE", strategy: "GRID" },
                  ].map(({ regime, strategy }) => {
                    const activeForPairs = pairNames.filter(p => pairs[p]?.regime === regime);
                    const active = activeForPairs.length > 0;
                    const rc = regimeColor(regime);
                    return (
                      <div key={regime} style={{
                        background: COLORS.bg,
                        boxShadow: active
                          ? `inset 0 1px 3px rgba(0,0,0,0.65), inset 0 -1px 0 ${rc}1A, inset 0 0 14px ${rc}22, inset 0 0 0 1px ${rc}45`
                          : `inset 0 1px 2px rgba(0,0,0,0.5), inset 0 0 10px ${rc}0C, inset 0 0 0 1px ${rc}20`,
                        borderRadius: 6,
                        padding: "8px 10px",
                        fontFamily: mono,
                      }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <div style={{
                            width: 6, height: 6, borderRadius: "50%", flexShrink: 0,
                            background: active ? rc : "transparent",
                            border: `1px solid ${active ? rc : COLORS.textMuted}`,
                            boxShadow: active ? `0 0 6px ${rc}` : "none"
                          }} />
                          <span style={{ fontSize: 10, color: active ? rc : COLORS.textDim, fontWeight: active ? 700 : 400, letterSpacing: "0.04em" }}>
                            {regime.replace("_", " ")}
                          </span>
                          <span style={{ fontSize: 10, color: active ? COLORS.textDim : COLORS.textMuted, marginLeft: "auto" }}>
                            {strategy.replace("_", " ")}
                          </span>
                        </div>
                        {active && (
                          <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 6 }}>
                            {activeForPairs.map(p => (
                              <span key={p} style={{
                                fontSize: 9, fontFamily: mono, fontWeight: 700,
                                color: rc,
                                background: `${rc}1A`,
                                border: `1px solid ${rc}50`,
                                padding: "2px 6px",
                                borderRadius: 3,
                                letterSpacing: "0.04em"
                              }}>{p}</span>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Per-Pair Stats */}
              {pairNames.map((pair) => {
                const ps = pairs[pair];
                const perf = ps.performance || {};
                const engineWR = ((perf.win_count || 0) + (perf.loss_count || 0)) > 0
                  ? ((perf.win_count || 0) / ((perf.win_count || 0) + (perf.loss_count || 0)) * 100)
                  : 0;
                const pf = fillsByPair[pair] || { buys: 0, sells: 0, sell_wins: 0, sell_losses: 0 };
                const pairSellTotal = (pf.sell_wins || 0) + (pf.sell_losses || 0);
                const pairFillWR = pairSellTotal > 0 ? ((pf.sell_wins || 0) / pairSellTotal * 100) : 0;
                const winRate = pairSellTotal > 0 ? pairFillWR : engineWR;
                const pairFills = pf.buys + pf.sells;
                const pairPnl = (jStats.pnl_by_pair || {})[pair] || {};
                const pairNetUsd = pairPnl.net_usd || 0;
                return (
                  <div key={pair} style={{ background: `${regimeColor(ps.regime)}08`, border: `1px solid ${regimeColor(ps.regime)}25`, borderRadius: 8, padding: 12 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                      <div style={{ width: 8, height: 8, borderRadius: "50%", background: regimeColor(ps.regime), boxShadow: `0 0 10px ${regimeColor(ps.regime)}80` }} />
                      <span style={{ fontSize: 12, fontWeight: 700, color: regimeColor(ps.regime), fontFamily: mono }}>{pair}</span>
                    </div>
                    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4, fontSize: 10, fontFamily: mono }}>
                      <span style={{ color: COLORS.textDim }}>Fills</span>
                      <span style={{ color: COLORS.text, textAlign: "right" }}>{pairFills}{pairFills > 0 ? ` (${pf.buys}B/${pf.sells}S)` : ""}</span>
                      <span style={{ color: COLORS.textDim }}>P&L</span>
                      <span style={{ color: pairNetUsd >= 0 ? COLORS.buy : COLORS.sell, textAlign: "right", fontWeight: 600 }}>
                        {pairNetUsd >= 0 ? "+$" : "-$"}{Math.abs(pairNetUsd).toFixed(2)}
                      </span>
                      <span style={{ color: COLORS.textDim }}>Win Rate</span>
                      <span style={{ color: winRate > 55 ? COLORS.buy : winRate > 0 ? COLORS.warn : COLORS.textMuted, textAlign: "right" }}>{winRate.toFixed(0)}%</span>
                      <span style={{ color: COLORS.textDim }}>Sharpe</span>
                      <span style={{ color: COLORS.text, textAlign: "right" }}>{(perf.sharpe_estimate || 0).toFixed(2)}</span>
                      <span style={{ color: COLORS.textDim }}>Drawdown</span>
                      <span style={{ color: (ps.portfolio?.max_drawdown_pct || 0) > 5 ? COLORS.danger : COLORS.text, textAlign: "right" }}>
                        {(ps.portfolio?.max_drawdown_pct || 0).toFixed(2)}%
                      </span>
                    </div>
                  </div>
                );
              })}

              {/* AI Brain */}
              {aiBrain && (
                <div style={{ background: `${COLORS.blue}08`, border: `1px solid ${COLORS.blue}25`, borderRadius: 8, padding: 12 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
                    <div style={{ width: 6, height: 6, borderRadius: "50%", background: aiBrain.active ? COLORS.accent : COLORS.danger, boxShadow: `0 0 6px ${aiBrain.active ? COLORS.accent : COLORS.danger}` }} />
                    <span style={{ fontSize: 10, color: COLORS.blue, textTransform: "uppercase", letterSpacing: "0.08em", fontFamily: mono, fontWeight: 700 }}>AI Brain</span>
                  </div>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4, fontSize: 10, fontFamily: mono }}>
                    <span style={{ color: COLORS.textDim }}>Decisions</span>
                    <span style={{ color: COLORS.text, textAlign: "right" }}>{aiBrain.decisions_today}</span>
                    <span style={{ color: COLORS.textDim }}>Overrides</span>
                    <span style={{ color: aiBrain.overrides_today > 0 ? COLORS.warn : COLORS.text, textAlign: "right" }}>{aiBrain.overrides_today}</span>
                    <span style={{ color: COLORS.textDim }}>Escalations</span>
                    <span style={{ color: aiBrain.escalations_today > 0 ? COLORS.warn : COLORS.text, textAlign: "right" }}>{aiBrain.escalations_today || 0}</span>
                    <span style={{ color: COLORS.textDim }}>Strategist</span>
                    <span style={{ color: aiBrain.has_strategist ? COLORS.accent : COLORS.textMuted, textAlign: "right" }}>{aiBrain.has_strategist ? "Grok 4" : "None"}</span>
                    <span style={{ color: COLORS.textDim }}>Cost Today</span>
                    <span style={{ color: COLORS.text, textAlign: "right" }}>${aiBrain.cost_today?.toFixed(3)}</span>
                    <span style={{ color: COLORS.textDim }}>Latency</span>
                    <span style={{ color: COLORS.text, textAlign: "right" }}>{aiBrain.avg_latency_ms}ms</span>
                    <span style={{ color: COLORS.textDim }}>Status</span>
                    <span style={{ color: aiBrain.active ? COLORS.accent : COLORS.danger, textAlign: "right" }}>{aiBrain.active ? "Active" : "Offline"}</span>
                  </div>
                </div>
              )}

              {/* Session Info */}
              <div style={{ background: `${COLORS.purple}08`, border: `1px solid ${COLORS.purple}25`, borderRadius: 8, padding: 12 }}>
                <div style={{ fontSize: 10, color: COLORS.purple, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6, fontFamily: mono, fontWeight: 700 }}>Session</div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4, fontSize: 10, fontFamily: mono }}>
                  <span style={{ color: COLORS.textDim }}>Orders</span>
                  <span style={{ color: COLORS.text, textAlign: "right" }}>Limit Post-Only</span>
                  <span style={{ color: COLORS.textDim }}>Interval</span>
                  <span style={{ color: COLORS.text, textAlign: "right" }}>{state?.interval ? `${state.interval}s` : "—"}</span>
                  <span style={{ color: COLORS.textDim }}>Pairs</span>
                  <span style={{ color: COLORS.text, textAlign: "right" }}>{pairNames.length}</span>
                  <span style={{ color: COLORS.textDim }}>Circuit Brk</span>
                  <span style={{ color: COLORS.text, textAlign: "right" }}>15% DD</span>
                  <span style={{ color: COLORS.textDim }}>Dead Man</span>
                  <span style={{ color: COLORS.accent, textAlign: "right" }}>Active</span>
                  <span style={{ color: COLORS.textDim }}>Sizing</span>
                  <span style={{ color: COLORS.text, textAlign: "right" }}>{state?.mode === "competition" ? "Half-Kelly" : "Quarter-Kelly"}</span>
                  <span style={{ color: COLORS.textDim }}>FX Session</span>
                  <span style={{ color: getForexSession().color, textAlign: "right" }}>{getForexSession().label}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ─── Companion Orb + Drawer ─── */}
      <CompanionOrb
        theme={COMPANION_THEMES[activeCompanion] || COMPANION_THEMES.apex}
        onClick={companionToggle}
        regime={state?.pairs ? (Object.values(state.pairs).map(p => p.regime).find(r => r === "VOLATILE") || "TREND") : "TREND"}
        hasUnread={getUnread(activeCompanion)}
        visible={companionVisible && !companionDrawerOpen}
        soulId={activeCompanion}
      />
      <CompanionDrawer
        open={companionDrawerOpen && companionVisible}
        onClose={companionToggle}
        active={activeCompanion}
        onSwitch={companionSwitch}
        companions={companions}
        messages={getMessages(activeCompanion) || []}
        typing={getTyping(activeCompanion)}
        onSend={companionSend}
        onProposalConfirm={companionProposalConfirm}
        onProposalReject={companionProposalReject}
        connected={connected}
        drawerWidth={companionDrawerWidth}
        costAlerts={companionCostAlerts}
      />

      {/* Footer */}
      <div style={{ padding: "10px 24px", borderTop: `1px solid ${COLORS.panelBorder}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: 8, color: COLORS.textMuted, fontFamily: mono }}>
          HYDRA v2.27.0 | kraken-cli v0.3.2 (WSL) | {DEFAULT_WS_URL}
          {jwtToken && (
            <span style={{ marginLeft: 16, cursor: "pointer", color: COLORS.warn }} onClick={onLogout}>
              [Logout]
            </span>
          )}
        </div>
        <div style={{ fontSize: 8, color: COLORS.textMuted, fontFamily: mono }}>
          Not financial advice. Real money at risk.
        </div>
      </div>
    </div>
  );
}


// ═══════════════════════════════════════════════════════════════
// Auth Surface
// ═══════════════════════════════════════════════════════════════

function AuthSurface({ onLogin }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleLogin = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError("");

    try {
      const ws = new WebSocket(DEFAULT_WS_URL);
      ws.onopen = () => {
        ws.send(JSON.stringify({ type: "login", username, password }));
      };
      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === "login_ack") {
          if (msg.success) {
            onLogin(msg.token);
          } else {
            setError(msg.error || "Login failed");
          }
          ws.close();
        }
      };
      ws.onerror = () => {
        setError("WebSocket connection failed. Ensure backend is running.");
      };
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "center",
      minHeight: "100vh", fontFamily: mono, color: COLORS.text,
      background: `radial-gradient(circle at 50% 50%, ${COLORS.panel}, ${COLORS.bg})`
    }}>
      <div style={{
        padding: "40px 32px",
        background: `${COLORS.panel}dd`,
        backdropFilter: "blur(12px)",
        border: `1px solid ${COLORS.panelBorder}`,
        borderRadius: 12,
        width: 380,
        boxShadow: `0 8px 32px rgba(0,0,0,0.5), inset 0 0 20px ${COLORS.accent}11`,
        textAlign: "center"
      }}>
        <div style={{ marginBottom: 32 }}>
          <div style={{
            display: "inline-flex", alignItems: "center", justifyContent: "center",
            width: 94, height: 94,
            marginBottom: 16
          }}>
            <img src="/favicon.png" alt="Hydra" style={{ width: "100%", height: "100%", filter: "drop-shadow(0 0 16px rgba(16, 185, 129, 0.6))" }} />
          </div>
          <h1 style={{ 
            margin: 0, fontSize: 32, fontWeight: 900, fontFamily: heading, letterSpacing: "-0.02em",
            background: `linear-gradient(135deg, ${COLORS.accent}, #0d9488)`,
            WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent",
          }}>HYDRA</h1>
          <div style={{ fontSize: 12, color: COLORS.textMuted, letterSpacing: "0.1em", textTransform: "uppercase", marginTop: 4 }}>
            Multi-Tenant Protocol
          </div>
        </div>

        <form onSubmit={handleLogin} style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <input
            type="text"
            placeholder="Username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            style={{
              padding: "12px 16px", borderRadius: 8, border: `1px solid ${COLORS.panelBorder}`,
              background: `${COLORS.bg}88`, color: COLORS.text, fontFamily: mono, fontSize: 14,
              outline: "none", transition: "border-color 0.2s"
            }}
          />
          <input
            type="password"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            style={{
              padding: "12px 16px", borderRadius: 8, border: `1px solid ${COLORS.panelBorder}`,
              background: `${COLORS.bg}88`, color: COLORS.text, fontFamily: mono, fontSize: 14,
              outline: "none", transition: "border-color 0.2s"
            }}
          />
          {error && <div style={{ color: COLORS.danger, fontSize: 12, marginTop: -4 }}>{error}</div>}
          <button
            type="submit"
            disabled={loading}
            style={{
              padding: "12px 16px", borderRadius: 8, border: "none",
              background: COLORS.accent, color: COLORS.bg,
              fontFamily: mono, fontSize: 14, fontWeight: 700,
              cursor: loading ? "not-allowed" : "pointer",
              opacity: loading ? 0.7 : 1, transition: "opacity 0.2s",
              boxShadow: `0 0 16px ${COLORS.accent}44`,
              marginTop: 8
            }}
          >
            {loading ? "Authenticating..." : "Initialize Session"}
          </button>
        </form>
      </div>
    </div>
  );
}

function SettingsSurface({ wsSend }) {
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState(null);

  const handleSave = () => {
    setSaving(true);
    setStatus(null);
    const jwt = localStorage.getItem("hydra_jwt");
    wsSend(JSON.stringify({ type: "save_keys", jwt, api_key: apiKey, api_secret: apiSecret }));
    setTimeout(() => {
      setSaving(false);
      setStatus({ type: "success", msg: "API Keys Saved Securely" });
      setApiKey(""); setApiSecret("");
    }, 1000);
  };

  const handleStart = () => {
    setStatus({ type: "info", msg: "Starting isolated engine instance..." });
    const jwt = localStorage.getItem("hydra_jwt");
    wsSend(JSON.stringify({ type: "start_agent", jwt }));
  };

  return (
    <div style={{
      padding: "24px", backgroundColor: COLORS.panel, borderRadius: "8px",
      border: `1px solid ${COLORS.panelBorder}`, maxWidth: "600px", margin: "32px auto"
    }}>
      <h2 style={{ fontSize: "24px", fontWeight: "bold", color: COLORS.text, marginBottom: "24px" }}>Engine Settings</h2>
      
      <div style={{ display: "flex", flexDirection: "column", gap: "24px" }}>
        <div style={{ backgroundColor: "#0f0f11", padding: "16px", borderRadius: "6px", border: `1px solid ${COLORS.panelBorder}` }}>
          <h3 style={{ fontSize: "18px", color: COLORS.accent, fontWeight: "600", marginBottom: "8px" }}>1. Exchange Credentials</h3>
          <p style={{ fontSize: "14px", color: COLORS.textDim, marginBottom: "16px" }}>Hydra uses symmetric encryption to store your keys. A unique background engine process will be spawned using your keys.</p>
          
          <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
            <div>
              <label style={{ display: "block", fontSize: "12px", fontWeight: "500", color: COLORS.textDim, marginBottom: "4px" }}>Kraken API Key</label>
              <input type="text" value={apiKey} onChange={e => setApiKey(e.target.value)} style={{ width: "100%", backgroundColor: COLORS.bg, border: `1px solid ${COLORS.panelBorder}`, borderRadius: "4px", padding: "8px 12px", color: COLORS.text, outline: "none" }} placeholder="..." />
            </div>
            <div>
              <label style={{ display: "block", fontSize: "12px", fontWeight: "500", color: COLORS.textDim, marginBottom: "4px" }}>Kraken API Secret</label>
              <input type="password" value={apiSecret} onChange={e => setApiSecret(e.target.value)} style={{ width: "100%", backgroundColor: COLORS.bg, border: `1px solid ${COLORS.panelBorder}`, borderRadius: "4px", padding: "8px 12px", color: COLORS.text, outline: "none" }} placeholder="..." />
            </div>
            <button onClick={handleSave} disabled={saving || !apiKey || !apiSecret} style={{
              width: "100%", padding: "8px 0", backgroundColor: `${COLORS.accent}33`, color: COLORS.accent,
              borderRadius: "4px", fontWeight: "500", border: "none", cursor: (saving || !apiKey || !apiSecret) ? "not-allowed" : "pointer", opacity: (saving || !apiKey || !apiSecret) ? 0.5 : 1
            }}>
              {saving ? "Encrypting & Saving..." : "Save API Keys"}
            </button>
          </div>
        </div>

        <div style={{ backgroundColor: "#0f0f11", padding: "16px", borderRadius: "6px", border: `1px solid ${COLORS.panelBorder}` }}>
          <h3 style={{ fontSize: "18px", color: COLORS.blue, fontWeight: "600", marginBottom: "8px" }}>2. Launch Engine</h3>
          <p style={{ fontSize: "14px", color: COLORS.textDim, marginBottom: "16px" }}>Start your isolated background execution engine. The dashboard will automatically reconnect to your engine's live feed.</p>
          <button onClick={handleStart} style={{
              width: "100%", padding: "8px 0", backgroundColor: `${COLORS.blue}33`, color: COLORS.blue,
              borderRadius: "4px", fontWeight: "500", border: "none", cursor: "pointer"
            }}>Start Hydra Agent</button>
        </div>

        {status && (
          <div style={{
            padding: "12px", borderRadius: "4px", textAlign: "center", fontSize: "14px",
            backgroundColor: status.type === 'success' ? `${COLORS.accent}1A` : `${COLORS.blue}1A`,
            color: status.type === 'success' ? COLORS.accent : COLORS.blue,
            border: `1px solid ${status.type === 'success' ? `${COLORS.accent}33` : `${COLORS.blue}33`}`
          }}>
            {status.msg}
          </div>
        )}
      </div>
    </div>
  );
}

export default function App() {
  const [jwtToken, setJwtToken] = useState(() => localStorage.getItem("hydra_jwt") || "");

  const handleLogin = (token) => {
    setJwtToken(token);
    localStorage.setItem("hydra_jwt", token);
  };

  const handleLogout = () => {
    setJwtToken("");
    localStorage.removeItem("hydra_jwt");
  };

  if (!jwtToken) {
    return <AuthSurface onLogin={handleLogin} />;
  }

  return <HydraDashboard jwtToken={jwtToken} onLogout={handleLogout} />;
}
