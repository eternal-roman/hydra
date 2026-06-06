// dashboard/src/theme.js
//
// Hydra design tokens — colors, fonts, and the small set of derived
// helpers (regimeColor, signalColor) used across App.jsx and the
// research/ subtree. Pre-v2.20.1 these lived in App.jsx only and
// the research components were styled with a generic dark-template
// palette (#888/#333/#3aa757) that diverged from the rest of the
// dashboard. Centralising them here is the single source of truth
// so any future component imports the same palette.

export const COLORS = {
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

export const mono = "'JetBrains Mono', monospace";
export const heading = "'Space Grotesk', 'JetBrains Mono', monospace";

export const regimeColor = (r) =>
  ({
    TREND_UP: COLORS.trendUp,
    TREND_DOWN: COLORS.trendDown,
    RANGING: COLORS.ranging,
    VOLATILE: COLORS.volatile,
  }[r] || COLORS.textDim);

export const signalColor = (s) =>
  ({ BUY: COLORS.buy, SELL: COLORS.sell, HOLD: COLORS.hold }[s] || COLORS.textDim);

// Wilcoxon verdict colours (LabPane). better=accent (green),
// worse=danger (red), null=muted.
export const wilcoxonColor = (v) => {
  const k = String(v || "").toLowerCase();
  if (k === "better") return COLORS.accent;
  if (k === "worse") return COLORS.danger;
  return COLORS.textMuted;
};
