/** Pure live-dashboard decisions. No DOM — node:test imports this directly. */

/** Current drawdown from the pair book. Null when equity or peak is missing. */
export function currentDrawdownPct(portfolio) {
  if (!portfolio || typeof portfolio !== "object") return null;
  const equity = portfolio.equity;
  const peak = portfolio.peak_equity;
  if (typeof equity !== "number" || !Number.isFinite(equity)) return null;
  if (typeof peak !== "number" || !Number.isFinite(peak) || !(peak > 0)) return null;
  return ((peak - equity) / peak) * 100;
}

/** Pass through a real finite percent. Do not coerce missing to 0. */
export function finitePct(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Dead-man cell. Paper/demo do not arm cancel_after. Live only claims the
 * agent refreshes the switch every tick — not a per-order ack.
 */
export function deadManCell(state) {
  if (state?.demo) return { text: "Off (demo)", neutral: true };
  if (state?.paper) return { text: "Off (paper)", neutral: true };
  return { text: "Refreshed every tick", neutral: false };
}

export const TRADABLE_FALSE_TITLE =
  "Entries blocked: new buys are not placed. Exits (sells) are still allowed. Signals still feed cross-pair confluence.";

/** Live account frames apply only after this socket's auth_ack succeeded. */
export function shouldApplyLiveState(socketAuthed) {
  return socketAuthed === true;
}

/** auth_ack failure must drop any account state this socket already painted. */
export function reduceAuthAck(msg) {
  if (msg && msg.success) return { authed: true, clearLive: false };
  return { authed: false, clearLive: true };
}
