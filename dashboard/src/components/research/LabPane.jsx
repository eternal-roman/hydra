// dashboard/src/components/research/LabPane.jsx
//
// Mode B pane of the Research tab — hypothesis lab. Walk-forward param diff.
//
// T30A: schema-driven slider rows for the 8 tunable engine params from
// PARAM_BOUNDS in hydra_tuner. Each param renders as Baseline | Candidate
// sliders with the live current value alongside.
//
// T30B: synchronous ack returns {success, job_id, n_folds, pair}; the daemon
// streams research_lab_progress per-fold and research_lab_result on completion.
//
// v2.20.1: restyled — was a generic dark-template aesthetic with hardcoded
// hex codes (#888/#3aa757/#1a1a1a). Now uses the shared theme tokens
// (COLORS, mono) and the Hydra button/letterSpacing/uppercase patterns.
// Layout was a cramped 7-column table; now stacked param cards so each
// row breathes. Sliders are full-width inside their card.

import React, { useState, useEffect } from "react";
import { COLORS, mono, heading, wilcoxonColor } from "../../theme";

const PAIRS = ["BTC/USD", "ETH/USD", "ZEC/USD", "SOL/USD", "SOL/BTC"];

const LABELS = {
  volatile_atr_mult: "Volatile ATR multiplier",
  volatile_bb_mult: "Volatile Bollinger multiplier",
  trend_ema_ratio: "Trend EMA ratio",
  momentum_rsi_lower: "Momentum RSI lower",
  momentum_rsi_upper: "Momentum RSI upper",
  mean_reversion_rsi_buy: "Mean-rev RSI buy",
  mean_reversion_rsi_sell: "Mean-rev RSI sell",
  min_confidence_threshold: "Min confidence threshold",
};

// ─── Building blocks ────────────────────────────────────────────────

const Card = ({ children, style }) => (
  <div
    style={{
      background: COLORS.panel,
      border: `1px solid ${COLORS.panelBorder}`,
      borderRadius: 6,
      padding: 16,
      ...style,
    }}
  >
    {children}
  </div>
);

const Label = ({ children, style }) => (
  <div
    style={{
      fontSize: 10,
      color: COLORS.textMuted,
      fontFamily: mono,
      letterSpacing: "0.1em",
      textTransform: "uppercase",
      marginBottom: 6,
      ...style,
    }}
  >
    {children}
  </div>
);

const Mono = ({ children, color, style }) => (
  <span
    style={{
      fontFamily: mono,
      color: color || COLORS.text,
      fontSize: 12,
      ...style,
    }}
  >
    {children}
  </span>
);

// Hydra-style range input — uses CSS custom thumb/track via inline <style>
// keyed off a unique class so we don't pollute global CSS.
const SLIDER_CSS = `
.hydra-slider {
  -webkit-appearance: none;
  appearance: none;
  width: 100%;
  height: 4px;
  background: ${COLORS.panelBorder};
  border-radius: 2px;
  outline: none;
  cursor: pointer;
  margin: 0;
}
.hydra-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: ${COLORS.accent};
  border: 2px solid ${COLORS.bg};
  cursor: pointer;
  box-shadow: 0 0 0 1px ${COLORS.accent};
  transition: transform 120ms;
}
.hydra-slider::-webkit-slider-thumb:hover { transform: scale(1.15); }
.hydra-slider::-moz-range-thumb {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: ${COLORS.accent};
  border: 2px solid ${COLORS.bg};
  cursor: pointer;
}
.hydra-slider-candidate::-webkit-slider-thumb {
  background: ${COLORS.blue};
  box-shadow: 0 0 0 1px ${COLORS.blue};
}
.hydra-slider-candidate::-moz-range-thumb { background: ${COLORS.blue}; }
`;

// Each param renders as a card with: label + range, then two stacked
// rows (Baseline | Candidate) each containing a colored swatch label,
// a full-width slider, and a numeric readout. Much more breathable
// than the old dense 7-column table.
function ParamRow({ name, def, baselineVal, candidateVal, onBaseline, onCandidate }) {
  const fmt = (v) => (v == null ? "—" : Number(v).toFixed(3));
  const drift =
    baselineVal != null && candidateVal != null
      ? candidateVal - baselineVal
      : null;
  const driftPct =
    drift != null && baselineVal !== 0
      ? (drift / Math.abs(baselineVal)) * 100
      : null;

  return (
    <Card style={{ marginBottom: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between",
                    alignItems: "baseline", marginBottom: 12, gap: 12 }}>
        <div>
          <div style={{ color: COLORS.text, fontSize: 14, fontWeight: 600,
                        fontFamily: heading, marginBottom: 2 }}>
            {LABELS[name] || name}
          </div>
          <Mono color={COLORS.textMuted} style={{ fontSize: 10 }}>
            range [{def.min}, {def.max}] · step {def.step}
          </Mono>
        </div>
        <div style={{ textAlign: "right" }}>
          <Label style={{ marginBottom: 2 }}>Live</Label>
          <Mono color={COLORS.textDim}>{fmt(def.current)}</Mono>
        </div>
      </div>

      <SliderRow
        accent={COLORS.accent}
        label="Baseline"
        value={baselineVal ?? def.current}
        min={def.min}
        max={def.max}
        step={def.step}
        onChange={onBaseline}
      />
      <SliderRow
        accent={COLORS.blue}
        label="Candidate"
        value={candidateVal ?? def.current}
        min={def.min}
        max={def.max}
        step={def.step}
        onChange={onCandidate}
        candidate
      />

      {drift != null && Math.abs(drift) > (def.step ?? 0) / 2 && (
        <div style={{ marginTop: 10, paddingTop: 10,
                      borderTop: `1px solid ${COLORS.panelBorder}`,
                      display: "flex", justifyContent: "flex-end", gap: 6,
                      alignItems: "baseline" }}>
          <Label style={{ marginBottom: 0 }}>Δ</Label>
          <Mono color={drift > 0 ? COLORS.accent : COLORS.danger}>
            {drift > 0 ? "+" : ""}
            {fmt(drift)}
            {driftPct != null && (
              <span style={{ color: COLORS.textMuted, marginLeft: 6, fontSize: 10 }}>
                ({drift > 0 ? "+" : ""}{driftPct.toFixed(1)}%)
              </span>
            )}
          </Mono>
        </div>
      )}
    </Card>
  );
}

function SliderRow({ accent, label, value, min, max, step, onChange, candidate }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}>
      <div style={{ width: 88, display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ width: 8, height: 8, borderRadius: "50%", background: accent,
                       display: "inline-block", flexShrink: 0 }} />
        <Mono color={COLORS.textDim} style={{ fontSize: 10, letterSpacing: "0.06em",
                                              textTransform: "uppercase" }}>
          {label}
        </Mono>
      </div>
      <input
        type="range"
        className={`hydra-slider${candidate ? " hydra-slider-candidate" : ""}`}
        min={min}
        max={max}
        step={step}
        value={value ?? min}
        onChange={(e) => onChange(parseFloat(e.target.value))}
      />
      <Mono color={accent} style={{ width: 60, textAlign: "right", fontWeight: 600 }}>
        {value == null ? "—" : Number(value).toFixed(3)}
      </Mono>
    </div>
  );
}

// ─── Main component ────────────────────────────────────────────────

export default function LabPane({
  sendMessage,
  labResult,
  paramsSchema,
  labProgress,
  clearLabRunState,
}) {
  const [pair, setPair] = useState("BTC/USD");
  const [baselineValues, setBaselineValues] = useState({});
  const [candidateValues, setCandidateValues] = useState({});
  const [running, setRunning] = useState(false);

  useEffect(() => {
    sendMessage({ type: "research_params_current", pair });
    setBaselineValues({});
    setCandidateValues({});
    if (typeof clearLabRunState === "function") clearLabRunState();
    setRunning(false);
  }, [pair, sendMessage, clearLabRunState]);

  const schema =
    paramsSchema && paramsSchema.success && paramsSchema.pair === pair
      ? paramsSchema.data
      : null;

  useEffect(() => {
    if (!schema) return;
    const init = {};
    for (const [k, def] of Object.entries(schema)) {
      init[k] = def.current ?? def.default ?? def.min;
    }
    setBaselineValues((b) => (Object.keys(b).length === 0 ? init : b));
    setCandidateValues((c) => (Object.keys(c).length === 0 ? init : c));
  }, [schema]);

  const progressMsgs = labProgress || [];
  const startMsg = progressMsgs.find((m) => m.phase === "started");
  const doneMsg = progressMsgs.find((m) => m.phase === "done");
  const errorMsg = progressMsgs.find((m) => m.phase === "error");
  const foldMetricsMsgs = progressMsgs.filter((m) => "fold_idx" in m);
  const foldsCompleted = new Set(foldMetricsMsgs.map((m) => `${m.fold_idx}|${m.side}`)).size;
  const totalSteps = (startMsg?.n_folds || labResult?.n_folds || 1) * 2;
  const ackJobId = labResult?.success && labResult.job_id ? labResult.job_id : null;

  useEffect(() => {
    if (doneMsg || errorMsg) setRunning(false);
  }, [doneMsg, errorMsg]);

  const run = () => {
    setRunning(true);
    sendMessage({
      type: "research_lab_run",
      pair,
      baseline_params: baselineValues,
      candidate_params: candidateValues,
      spec: { fold_kind: "quarterly", is_lookback_quarters: 8 },
    });
  };

  const disabled = running || !schema;

  return (
    <div style={{ padding: 24, color: COLORS.text }}>
      <style>{SLIDER_CSS}</style>

      {/* Header */}
      <div style={{ marginBottom: 20 }}>
        <h3 style={{ margin: 0, fontFamily: heading, fontSize: 18, fontWeight: 700,
                     color: COLORS.text, letterSpacing: "-0.01em" }}>
          Hypothesis Lab
        </h3>
        <p style={{ color: COLORS.textDim, fontSize: 12, marginTop: 6,
                    marginBottom: 0, lineHeight: 1.5, maxWidth: 720 }}>
          Mode B — paired walk-forward of candidate parameters vs baseline on
          real history. Sliders show live current values; drag to set
          candidate, then run.
        </p>
      </div>

      {/* Lab-run failure banner */}
      {labResult && labResult.success === false && (
        <Card
          style={{
            marginBottom: 20,
            borderColor: `${COLORS.danger}55`,
            background: `${COLORS.danger}14`,
            color: COLORS.danger,
          }}
        >
          <Label style={{ color: COLORS.danger, marginBottom: 4 }}>Error</Label>
          <Mono color={COLORS.text}>{labResult.error}</Mono>
        </Card>
      )}

      {/* Pair selector */}
      <div style={{ marginBottom: 20, display: "flex", alignItems: "center", gap: 12 }}>
        <Label style={{ marginBottom: 0 }}>Pair</Label>
        <select
          value={pair}
          onChange={(e) => setPair(e.target.value)}
          style={{
            padding: "8px 12px",
            background: COLORS.bg,
            color: COLORS.text,
            border: `1px solid ${COLORS.panelBorder}`,
            borderRadius: 4,
            fontFamily: mono,
            fontSize: 12,
            outline: "none",
            cursor: "pointer",
          }}
        >
          {PAIRS.map((p) => (
            <option key={p}>{p}</option>
          ))}
        </select>
      </div>

      {/* Param cards */}
      {!schema ? (
        <Card>
          <Mono color={COLORS.textDim}>Loading params for {pair}…</Mono>
        </Card>
      ) : (
        <div>
          {Object.entries(schema).map(([name, def]) => (
            <ParamRow
              key={name}
              name={name}
              def={def}
              baselineVal={baselineValues[name]}
              candidateVal={candidateValues[name]}
              onBaseline={(v) =>
                setBaselineValues((prev) => ({ ...prev, [name]: v }))
              }
              onCandidate={(v) =>
                setCandidateValues((prev) => ({ ...prev, [name]: v }))
              }
            />
          ))}
        </div>
      )}

      {/* Run button — Hydra-style primary */}
      <button
        onClick={run}
        disabled={disabled}
        style={{
          marginTop: 12,
          padding: "10px 18px",
          background: disabled ? COLORS.panel : `${COLORS.accent}33`,
          color: disabled ? COLORS.textMuted : COLORS.accent,
          border: `1px solid ${disabled ? COLORS.panelBorder : `${COLORS.accent}55`}`,
          borderRadius: 4,
          cursor: disabled ? "not-allowed" : "pointer",
          fontFamily: mono,
          fontSize: 11,
          fontWeight: 700,
          letterSpacing: "0.1em",
          textTransform: "uppercase",
          transition: "background 160ms, border-color 160ms",
        }}
      >
        {running ? "Running…" : "Run walk-forward"}
      </button>

      {/* In-flight progress */}
      {running && !doneMsg && !errorMsg && (
        <Card style={{ marginTop: 16 }}>
          <Label>In flight</Label>
          <Mono color={COLORS.text}>
            {foldsCompleted}/{totalSteps} fold-runs completed
          </Mono>
          {ackJobId && (
            <div style={{ marginTop: 6 }}>
              <Mono color={COLORS.textMuted} style={{ fontSize: 10 }}>
                job {ackJobId}
              </Mono>
            </div>
          )}
        </Card>
      )}

      {/* Daemon error */}
      {errorMsg && (
        <Card
          style={{
            marginTop: 16,
            borderColor: `${COLORS.danger}55`,
            background: `${COLORS.danger}14`,
          }}
        >
          <Label style={{ color: COLORS.danger }}>Walk-forward error</Label>
          <Mono color={COLORS.text}>{errorMsg.error}</Mono>
        </Card>
      )}

      {/* Verdict */}
      {doneMsg && (
        <Card style={{ marginTop: 16 }}>
          <div style={{ marginBottom: 12 }}>
            <h4 style={{ margin: 0, fontFamily: heading, fontSize: 14, fontWeight: 700,
                         color: COLORS.text }}>
              Verdict
            </h4>
            <Mono color={COLORS.textMuted} style={{ fontSize: 10 }}>
              paired Wilcoxon · α=0.05
            </Mono>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {Object.entries(doneMsg.wilcoxon || {}).map(([metric, v]) => {
              const color = wilcoxonColor(v.verdict);
              return (
                <div key={metric}
                     style={{ display: "flex", gap: 12, alignItems: "baseline",
                              padding: "6px 0",
                              borderBottom: `1px solid ${COLORS.panelBorder}` }}>
                  <span style={{
                    minWidth: 76, padding: "2px 8px", borderRadius: 3,
                    background: `${color}22`, color,
                    fontFamily: mono, fontSize: 10, fontWeight: 700,
                    letterSpacing: "0.08em", textTransform: "uppercase",
                    textAlign: "center",
                  }}>
                    {(v.verdict || "?").toUpperCase()}
                  </span>
                  <Mono color={COLORS.text} style={{ fontWeight: 600, minWidth: 140 }}>
                    {metric}
                  </Mono>
                  <Mono color={COLORS.textDim} style={{ fontSize: 11 }}>
                    {v.candidate_wins}/{v.n} wins · p={Number(v.p_value).toFixed(4)} ·
                    {" "}median Δ={Number(v.median_delta).toFixed(3)}
                  </Mono>
                </div>
              );
            })}
          </div>
          <div style={{ marginTop: 10 }}>
            <Mono color={COLORS.textMuted} style={{ fontSize: 10 }}>
              {doneMsg.n_folds_completed} folds completed, {doneMsg.skipped_folds} skipped (insufficient trades)
            </Mono>
          </div>
        </Card>
      )}
    </div>
  );
}
