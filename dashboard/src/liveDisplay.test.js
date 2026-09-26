import assert from "node:assert/strict";
import { test } from "node:test";
import {
  currentDrawdownPct,
  deadManCell,
  finitePct,
  reduceAuthAck,
  shouldApplyLiveState,
  TRADABLE_FALSE_TITLE,
} from "./liveDisplay.js";

test("current drawdown uses peak and equity, and does not invent a number", () => {
  assert.equal(currentDrawdownPct({ peak_equity: 100, equity: 80 }), 20);
  assert.equal(currentDrawdownPct({ peak_equity: 100, equity: 0 }), 100);
  assert.equal(currentDrawdownPct({ peak_equity: 100 }), null);
  assert.equal(currentDrawdownPct({ equity: 80 }), null);
  assert.equal(currentDrawdownPct({ peak_equity: 0, equity: 0 }), null);
  assert.equal(currentDrawdownPct(null), null);
  assert.equal(finitePct(undefined), null);
  assert.equal(finitePct(1.5), 1.5);
});

test("dead-man copy is off in paper or demo and does not claim a live ack", () => {
  assert.deepEqual(deadManCell({ paper: true }), { text: "Off (paper)", neutral: true });
  assert.deepEqual(deadManCell({ demo: true }), { text: "Off (demo)", neutral: true });
  assert.equal(deadManCell({ demo: true, paper: true }).text, "Off (demo)");
  const live = deadManCell({ paper: false, demo: false });
  assert.equal(live.neutral, false);
  assert.equal(live.text, "Refreshed every tick");
  assert.doesNotMatch(live.text, /active/i);
});

test("untradable copy says entries are blocked and exits are allowed", () => {
  assert.match(TRADABLE_FALSE_TITLE, /entries blocked/i);
  assert.match(TRADABLE_FALSE_TITLE, /exits/i);
  assert.doesNotMatch(TRADABLE_FALSE_TITLE, /orders won't be placed/i);
});

test("state frames wait for this socket's auth_ack, and failure clears live state", () => {
  assert.equal(shouldApplyLiveState(false), false);
  assert.equal(shouldApplyLiveState(undefined), false);
  assert.equal(shouldApplyLiveState(true), true);
  assert.deepEqual(reduceAuthAck({ success: true }), { authed: true, clearLive: false });
  assert.deepEqual(reduceAuthAck({ success: false }), { authed: false, clearLive: true });
  assert.deepEqual(reduceAuthAck({}), { authed: false, clearLive: true });
});
