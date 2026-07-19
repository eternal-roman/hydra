"""S3Strategy facade — multi-asset daily bars in, causal signals out.

Stage machine per asset (evaluated on COMPLETED bars only):

  none          no fresh setup at the last completed bar
  scored_b0     the last completed bar is the bounce-confirm bar (b0) of
                a setup: features computed, model scored, gate decided
  entryable_b1  the last completed bar is b1 (bounce+1) of a setup that
                is still unresolved (entry_index valid): a shadow
                proposal may be logged at this bar's close

Swing-confirmation lag (SW=2): a swing low is only confirmed two bars
after it prints, so for ADJACENT bounces (bounce = low+1, the common
case) the setup first becomes computable at b1's close — exactly the
entry-decision bar. scored_b0 therefore only occurs when
bounce_idx >= low_idx + 2; entryable_b1 is always causally detectable
because entry_index's no-undercut condition IS the swing-confirmation
condition. This matches the research harness, which scores b0 features
in hindsight but only ever ENTERS at b1 close.

Degradation (breadth off-distribution or missing/stale model) forces
`gated=False` with the reason recorded — SKIP semantics for proposals;
the signal itself stays visible. Assets absent from the model artifact
(ZEC) always report model_loaded=False; their bars still feed breadth.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Optional

from .candles import DailyBarSeries
from .features import compute_features, fresh_low_days
from .model import Artifact, gate as model_gate, score as model_score
from .setups import Setup, causal_setups, entry_index

MIN_BARS = 90          # ATR14 + swing/vol/shock lookbacks + margin


@dataclass
class S3Signal:
    asset: str
    model_loaded: bool
    stage: str                      # none | scored_b0 | entryable_b1
    score: Optional[float] = None
    gated: bool = False
    degraded: bool = False
    setup: Optional[Setup] = None
    entry_idx: Optional[int] = None
    n_bars: int = 0
    reasons: list = field(default_factory=list)


class S3Strategy:
    def __init__(self, artifact: Artifact,
                 universe: tuple[str, ...] = ("BTC/USD", "ETH/USD", "ZEC/USD")):
        self.artifact = artifact
        self.universe = tuple(universe)
        self.series: dict[str, DailyBarSeries] = {a: DailyBarSeries()
                                                  for a in self.universe}

    def seed(self, asset: str, rows: list[dict]) -> None:
        self.series[asset].seed(rows)

    def on_1h(self, asset: str, ts: float, o: float, h: float, low: float,
              c: float, v: float) -> None:
        self.series[asset].update_1h(ts, o, h, low, c, v)

    def evaluate(self, asset: str, now_ts: Optional[float] = None) -> S3Signal:
        now_ts = _time.time() if now_ts is None else now_ts
        model = self.artifact.models.get(asset)
        sig = S3Signal(asset=asset, model_loaded=model is not None,
                       stage="none")
        bars = self.series[asset].completed_bars(now_ts)
        sig.n_bars = len(bars)
        if model is None:
            sig.reasons.append("asset_not_in_artifact")
            return sig
        if len(bars) < MIN_BARS:
            sig.degraded = True
            sig.reasons.append(f"warmup:{len(bars)}/{MIN_BARS}")
            return sig

        low_days = {}
        breadth_ok = True
        for member in self.universe:
            mbars = self.series[member].completed_bars(now_ts)
            if len(mbars) < 21 or (mbars and bars and
                                   mbars[-1].day < bars[-1].day - 2):
                breadth_ok = False
                sig.reasons.append(f"breadth_member_missing:{member}")
            low_days[member] = fresh_low_days(mbars)

        setups = causal_setups(bars)
        last_idx = len(bars) - 1
        b0 = [s for s in setups if s.bounce_idx == last_idx]
        b1 = [s for s in setups if s.bounce_idx == last_idx - 1
              and entry_index(bars, s, 1) == last_idx]
        picked = (b1 or b0)
        if not picked:
            return sig
        s = picked[-1]
        compute_features(bars, [s], low_days)
        sig.setup = s
        sig.stage = "entryable_b1" if b1 else "scored_b0"
        if sig.stage == "entryable_b1":
            sig.entry_idx = last_idx
        sig.score = model_score(model, s.x)
        if self.artifact.stale(now_ts):
            sig.degraded = True
            sig.reasons.append("model_stale")
        if not breadth_ok:
            sig.degraded = True
        sig.gated = (not sig.degraded) and model_gate(model, s.x)
        return sig
