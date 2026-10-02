import datetime as dt
from hydra_walk_forward import build_quarterly_folds, WalkForwardSpec


def _ts(year, month, day=1):
    return int(dt.datetime(year, month, day, tzinfo=dt.timezone.utc).timestamp())


def test_builds_quarterly_folds():
    spec = WalkForwardSpec(is_lookback_quarters=4)
    folds = build_quarterly_folds(_ts(2022, 1, 1), _ts(2023, 1, 1), spec)
    # 2022 → 4 OOS quarters: Q1 (Jan-Mar), Q2 (Apr-Jun), Q3, Q4. But the FIRST
    # fold needs at least 1 quarter of IS, so Q1 2022 is skipped.
    assert len(folds) == 3
    f0 = folds[0]   # IS = Q1 2022, OOS = Q2 2022
    assert f0.is_start == _ts(2022, 1, 1)
    assert f0.is_end == _ts(2022, 4, 1)
    assert f0.oos_start == _ts(2022, 4, 1)
    assert f0.oos_end == _ts(2022, 7, 1)


def test_is_lookback_capped():
    spec = WalkForwardSpec(is_lookback_quarters=2)
    # 3 years of data; on the last fold, IS should be capped to last 2 quarters.
    folds = build_quarterly_folds(_ts(2020, 1, 1), _ts(2023, 1, 1), spec)
    last = folds[-1]
    is_quarters = (last.is_end - last.is_start) // (90 * 86400)
    assert is_quarters <= 2 + 1   # ±1 for 90-vs-91-day months


def test_walk_forward_slices_pairs_on_one_timestamp_clock(monkeypatch):
    """Per-pair INDEX slicing tested BTC hours 466-776 against SOL hours
    966-1376 when SOL listed 500 hours later, and never tested BTC's tail."""
    import hydra_backtest_metrics as m
    from hydra_backtest import BacktestConfig, Candle

    def series(start_h, n):
        return [Candle(open=1, high=1.01, low=0.99, close=1, volume=1,
                       timestamp=float((start_h + i) * 3600)) for i in range(n)]

    full = {"BTC/USD": series(0, 2000), "SOL/USD": series(500, 1500)}
    monkeypatch.setattr(m, "_materialize_candles", lambda cfg: full)
    seen = []

    class _Runner:
        def __init__(self, cfg, sources_override=None):
            seen.append({p: [c.timestamp for c in src.iter_candles(p)]
                         for p, src in sources_override.items()})

        def run(self):
            class _M:
                total_trades = 0
                total_return_pct = 0.0
                sharpe = 0.0
                sortino = 0.0
                max_drawdown_pct = 0.0

            class _R:
                metrics = _M()
                equity_curve = {}
            return _R()

    monkeypatch.setattr(m, "BacktestRunner", _Runner)
    cfg = BacktestConfig(name="wf", pairs=("BTC/USD", "SOL/USD"))
    m.walk_forward(cfg, n_windows=3)
    for slice_ts in seen:
        btc, sol = slice_ts["BTC/USD"], slice_ts["SOL/USD"]
        if btc and sol:
            assert max(btc) == max(sol)
    # Index slicing used the SHORTER series' length (1500), so BTC hours
    # beyond 1500 were never tested; on the shared clock the last slice
    # reaches BTC's tail.
    assert max(seen[-1]["BTC/USD"]) > 1900 * 3600.0
