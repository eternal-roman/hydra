"""Unit tests for hydra_derivatives_stream.DerivativesStream.

Covers: regime classification, delta computation, snapshot lifecycle,
basis parsing, synthetic SOL/BTC derivation, and the spot-only
invariant (no order-placement imports exist in the module).
"""
from collections import deque
import os
import subprocess
import time

import pytest

from hydra_derivatives_stream import (
    DerivativesSnapshot,
    DerivativesStream,
    _absolute_to_relative_bps,
    _delta_pct,
    _maybe_float,
    _prune_before,
)


# ─── Helpers ─────────────────────────────────────────────────


def test_maybe_float_handles_none_and_strings():
    assert _maybe_float(None) is None
    assert _maybe_float("1.5") == 1.5
    assert _maybe_float(2) == 2.0
    assert _maybe_float("not-a-number") is None
    assert _maybe_float([1, 2]) is None


def test_absolute_to_relative_bps_normal():
    # 2.5 / 50000 * 10000 = 0.5 bps
    assert _absolute_to_relative_bps(2.5, 50000.0, "BTC/USDC", "test") == 0.5


def test_absolute_to_relative_bps_returns_none_when_either_input_none():
    assert _absolute_to_relative_bps(None, 50000.0, "BTC/USDC", "test") is None
    assert _absolute_to_relative_bps(2.5, None, "BTC/USDC", "test") is None


def test_absolute_to_relative_bps_returns_none_on_nan_or_inf():
    """Phase-1 audit catch: float('nan') passes _maybe_float and would
    propagate silently into R1/R2 (NaN comparisons always False = wrong
    'no fire'). Helper must null these before they reach the rules."""
    nan = float("nan")
    inf = float("inf")
    assert _absolute_to_relative_bps(nan, 50000.0, "BTC/USDC", "test") is None
    assert _absolute_to_relative_bps(2.5, nan, "BTC/USDC", "test") is None
    assert _absolute_to_relative_bps(inf, 50000.0, "BTC/USDC", "test") is None


def test_absolute_to_relative_bps_returns_none_when_markprice_zero():
    assert _absolute_to_relative_bps(2.5, 0.0, "BTC/USDC", "test") is None


def test_absolute_to_relative_bps_returns_none_when_clamp_exceeded(capsys):
    # 0.06 / 1.0 * 10000 = 600 > 500
    assert _absolute_to_relative_bps(0.06, 1.0, "BTC/USDC", "test") is None
    assert "exceeds sanity bound" in capsys.readouterr().err


def test_delta_pct_returns_none_when_empty_history():
    assert _delta_pct(deque(), 100.0, 50.0) is None
    assert _delta_pct(deque([(0.0, 100.0)]), 100.0, None) is None


def test_delta_pct_returns_none_when_chosen_baseline_zero():
    # target_ts=25 falls at or before the t=0 sample → baseline val=0 → None
    hist = deque([(0.0, 0.0), (50.0, 10.0)])
    assert _delta_pct(hist, 25.0, 20.0) is None


def test_delta_pct_picks_sample_at_or_before_target():
    # Samples at t=0, 60, 120; target_ts=90 → closest is t=60 val=110
    hist = deque([(0.0, 100.0), (60.0, 110.0), (120.0, 130.0)])
    assert _delta_pct(hist, 90.0, 130.0) == round(100.0 * (130 - 110) / 110, 2)


def test_prune_before_strips_old_entries():
    hist = deque([(0.0, 1.0), (50.0, 2.0), (100.0, 3.0), (200.0, 4.0)])
    _prune_before(hist, 60.0)
    assert list(hist) == [(100.0, 3.0), (200.0, 4.0)]


# ─── Regime classifier ───────────────────────────────────────


@pytest.fixture
def stream():
    return DerivativesStream(pairs=["BTC/USDC"])


@pytest.mark.parametrize(
    "oi_delta,px_delta,expected",
    [
        (1.5, 1.0, "trend_confirm_long"),      # OI↑ + Px↑
        (1.5, -1.0, "trend_confirm_short"),    # OI↑ + Px↓
        (-1.5, 1.0, "short_squeeze"),          # OI↓ + Px↑
        (-1.5, -1.0, "liquidation_cascade"),   # OI↓ + Px↓
        (0.1, 0.1, "balanced"),                # both under threshold
        (0.1, 2.0, "balanced"),                # OI under threshold
        (2.0, 0.1, "balanced"),                # Px under threshold
        (None, 1.0, "unknown"),
        (1.0, None, "unknown"),
    ],
)
def test_classify_oi_price_regime(stream, oi_delta, px_delta, expected):
    assert stream._classify_oi_price_regime(oi_delta, px_delta) == expected


# ─── Snapshot lifecycle ──────────────────────────────────────


def test_stream_instantiates_only_configured_pairs():
    s = DerivativesStream(pairs=["BTC/USDC", "SOL/USDC", "SOL/BTC", "ETH/USDC"])
    # ETH/USDC is not in SPOT_TO_DERIVATIVES and must be dropped
    assert set(s.pairs) == {"BTC/USDC", "SOL/USDC", "SOL/BTC"}


def test_latest_returns_none_for_unknown_pair():
    s = DerivativesStream(pairs=["BTC/USDC"])
    assert s.latest("ETH/USDC") is None


def test_latest_returns_initial_snapshot_with_nones(stream):
    snap = stream.latest("BTC/USDC")
    assert snap is not None
    assert snap.pair == "BTC/USDC"
    assert snap.perp_symbol == "PF_XBTUSD"
    assert snap.funding_bps_8h is None
    assert snap.open_interest is None
    assert snap.staleness_s == float("inf")


def test_populate_from_ticker_updates_snapshot(stream):
    snap = stream._snapshots["BTC/USDC"]
    # Kraken Futures PF_* fundingRate is absolute USD/contract/period.
    # Bps = (fr/markPrice)*10000. With markPrice=95000.5:
    #   fr=4.75025 → 4.75025/95000.5 = 5.0e-5 → 0.5 bps
    #   fr=3.80020 → 3.80020/95000.5 = 4.0e-5 → 0.4 bps
    tick = {
        "symbol": "PF_XBTUSD",
        "markPrice": "95000.5",
        "indexPrice": "94990.0",
        "fundingRate": "4.75025",
        "fundingRatePrediction": "3.80020",
        "openInterest": "12345.67",
    }
    now = time.time()
    stream._populate_from_ticker(snap, tick, now)
    assert snap.mark_price == 95000.5
    assert snap.funding_bps_8h == 0.5
    assert snap.funding_predicted_bps == 0.4
    assert snap.open_interest == 12345.67
    assert snap.last_updated_ts == now
    assert snap.fetch_error_streak == 0


def test_oi_delta_computes_against_history(stream):
    snap = stream._snapshots["BTC/USDC"]
    base = time.time() - 3700  # slightly over 1h ago
    # Seed history: OI went from 10000 (1h ago) to 10500 (now) → +5%
    t0 = {"symbol": "PF_XBTUSD", "markPrice": "95000", "indexPrice": "95000",
          "fundingRate": "0", "fundingRatePrediction": "0", "openInterest": "10000"}
    stream._populate_from_ticker(snap, t0, base)
    t1 = {"symbol": "PF_XBTUSD", "markPrice": "95500", "indexPrice": "95500",
          "fundingRate": "0", "fundingRatePrediction": "0", "openInterest": "10500"}
    stream._populate_from_ticker(snap, t1, time.time())
    assert snap.oi_delta_1h_pct == 5.0
    assert snap.oi_price_regime == "trend_confirm_long"


def test_synthetic_sol_btc_computes_from_usd_perps(stream):
    s = DerivativesStream(pairs=["SOL/BTC"])
    snap = s._snapshots["SOL/BTC"]
    # Each leg's funding must be normalized by its own markPrice first.
    # sol: 0.015 / 150 = 1.0e-4 → 1.0 bps
    # btc: 3.0 / 60000 = 5.0e-5 → 0.5 bps
    # diff: 1.0 - 0.5 = 0.5 bps
    sol = {"fundingRate": "0.015", "markPrice": "150.0"}
    btc = {"fundingRate": "3.0", "markPrice": "60000.0"}
    s._populate_synthetic(snap, sol, btc, time.time())
    assert snap.funding_bps_8h == 0.5
    # Ratio: 150 / 60000 = 0.0025
    assert snap.mark_price == 0.0025


# ─── Funding markPrice-relative + sanity clamp (v2.15.2) ────


def test_funding_uses_relative_rate_not_absolute(stream):
    """Kraken Futures PF_* returns fundingRate as absolute USD per contract
    per period, not as a decimal rate. Correct conversion to bps requires
    dividing by markPrice. Pre-fix bug: BTC at markPrice=50000 with
    fundingRate=-0.5 produced -5000 bps (firing R2 spuriously); correct
    output is -0.10 bps."""
    s = stream
    snap = DerivativesSnapshot(pair="BTC/USDC", perp_symbol="PF_XBTUSD")
    tick = {"fundingRate": -0.5, "markPrice": 50000.0, "indexPrice": 50000.0}
    s._populate_from_ticker(snap, tick, time.time())
    # (-0.5 / 50000) * 10000 = -0.10 bps
    assert snap.funding_bps_8h == -0.1, (
        f"expected -0.1 bps from (fr/mp)*10000, got {snap.funding_bps_8h}"
    )


def test_funding_predicted_also_relative(stream):
    s = stream
    snap = DerivativesSnapshot(pair="BTC/USDC", perp_symbol="PF_XBTUSD")
    tick = {
        "fundingRate": 0.0,
        "fundingRatePrediction": -1.0,
        "markPrice": 50000.0,
        "indexPrice": 50000.0,
    }
    s._populate_from_ticker(snap, tick, time.time())
    # (-1.0 / 50000) * 10000 = -0.2 bps
    assert snap.funding_predicted_bps == -0.2


def test_funding_returns_none_when_markprice_missing(stream):
    """Without markPrice we cannot compute relative funding. Don't guess."""
    s = stream
    snap = DerivativesSnapshot(pair="BTC/USDC", perp_symbol="PF_XBTUSD")
    tick = {"fundingRate": -0.5}  # no markPrice
    s._populate_from_ticker(snap, tick, time.time())
    assert snap.funding_bps_8h is None


def test_synthetic_funding_uses_per_leg_relative_rates(stream):
    """Synthetic SOL/BTC funding cannot subtract two absolute USD-per-contract
    rates that don't share a denominator. Each leg must be normalized to its
    own markPrice first: (sol_fr/sol_mark - btc_fr/btc_mark) * 10000."""
    s = DerivativesStream(pairs=["SOL/BTC"])
    snap = s._snapshots["SOL/BTC"]
    sol = {"fundingRate": -0.0036, "markPrice": 80.0}     # -0.45 bps relative
    btc = {"fundingRate": -1.0,    "markPrice": 50000.0}  # -0.20 bps relative
    s._populate_synthetic(snap, sol, btc, time.time())
    # (-0.0036/80 - (-1.0/50000)) * 10000 = (-0.000045 + 0.00002) * 10000 = -0.25 bps
    assert snap.funding_bps_8h == -0.25, (
        f"synthetic must normalize each leg by its markPrice first, "
        f"got {snap.funding_bps_8h}"
    )


def test_synthetic_funding_returns_none_when_either_markprice_missing(stream):
    s = DerivativesStream(pairs=["SOL/BTC"])
    snap = s._snapshots["SOL/BTC"]
    sol = {"fundingRate": -0.0036}   # no markPrice
    btc = {"fundingRate": -1.0, "markPrice": 50000.0}
    s._populate_synthetic(snap, sol, btc, time.time())
    assert snap.funding_bps_8h is None


def test_funding_clamp_catches_unexpected_magnitude(stream):
    """Defense-in-depth: even after the divide fix, if Kraken changes the
    units again, ±500 bps clamps the value to None rather than feeding R1/R2
    a poisoned signal."""
    s = stream
    snap = DerivativesSnapshot(pair="BTC/USDC", perp_symbol="PF_XBTUSD")
    # markPrice=1.0 makes (fr/mp)*10000 == fr*10000.  fr=0.06 → 600 bps.
    tick = {"fundingRate": 0.06, "markPrice": 1.0, "indexPrice": 1.0}
    s._populate_from_ticker(snap, tick, time.time())
    assert snap.funding_bps_8h is None, "out-of-band magnitude must null"


def test_funding_normal_range_passes_through(stream):
    """Regression guard: typical funding stays intact."""
    s = stream
    snap = DerivativesSnapshot(pair="BTC/USDC", perp_symbol="PF_XBTUSD")
    # markPrice=50000, fundingRate=2.5 → 0.5 bps (normal)
    tick = {"fundingRate": 2.5, "markPrice": 50000.0, "indexPrice": 50000.0}
    s._populate_from_ticker(snap, tick, time.time())
    assert snap.funding_bps_8h == 0.5


# ─── Basis parsing ───────────────────────────────────────────


def test_find_quarterly_returns_earliest(stream):
    """Fixed `now` anchor so test is stable across wall-clock drift.
    All four FF_XBTUSD_* suffixes resolve to dates after the anchor;
    the earliest non-expired is 2030-03-28."""
    import datetime
    now = datetime.datetime(
        2030, 1, 1, tzinfo=datetime.timezone.utc
    ).timestamp()
    by_symbol = {
        "PF_XBTUSD": {},
        "FF_XBTUSD_300927": {},
        "FF_XBTUSD_300328": {},   # earliest non-expired
        "FF_XBTUSD_300627": {},
        "FF_SOLUSD_300328": {},
    }
    assert stream._find_quarterly(
        by_symbol, "FF_XBTUSD", now
    ) == "FF_XBTUSD_300328"


def test_find_quarterly_returns_none_when_no_match(stream):
    assert stream._find_quarterly({"PF_XBTUSD": {}}, "FF_XBTUSD") is None
    assert stream._find_quarterly({}, None) is None


def test_find_quarterly_skips_expired_contracts(stream):
    """An expired dated contract that lingers in the ticker feed must
    be skipped; otherwise `_compute_basis` would annualize residual
    premium over a 1-day clamped tenor (garbage APR)."""
    import datetime
    now = datetime.datetime(
        2026, 6, 1, tzinfo=datetime.timezone.utc
    ).timestamp()
    by_symbol = {
        "FF_XBTUSD_260101": {},  # expired — must be skipped
        "FF_XBTUSD_260424": {},  # expired — must be skipped
        "FF_XBTUSD_260927": {},  # first non-expired
        "FF_XBTUSD_261226": {},
    }
    assert stream._find_quarterly(
        by_symbol, "FF_XBTUSD", now
    ) == "FF_XBTUSD_260927"


def test_find_quarterly_returns_none_when_all_expired(stream):
    import datetime
    now = datetime.datetime(
        2030, 1, 1, tzinfo=datetime.timezone.utc
    ).timestamp()
    by_symbol = {
        "FF_XBTUSD_260101": {},
        "FF_XBTUSD_260424": {},
    }
    assert stream._find_quarterly(by_symbol, "FF_XBTUSD", now) is None


def test_find_quarterly_skips_malformed_suffix(stream):
    """Non-6-digit or non-numeric suffixes must not crash the parser
    and must be skipped in favor of a well-formed sibling."""
    import datetime
    now = datetime.datetime(
        2030, 1, 1, tzinfo=datetime.timezone.utc
    ).timestamp()
    by_symbol = {
        "FF_XBTUSD_NEXTWK": {},  # non-numeric
        "FF_XBTUSD_12345": {},   # 5 digits
        "FF_XBTUSD_9999999": {}, # 7 digits
        "FF_XBTUSD_301301": {},  # invalid month → ValueError
        "FF_XBTUSD_300927": {},  # only valid entry
    }
    assert stream._find_quarterly(
        by_symbol, "FF_XBTUSD", now
    ) == "FF_XBTUSD_300927"


def test_compute_basis_annualizes_premium(stream):
    """30 days to expiry, 2% premium ⇒ ~24.33% APR.

    v2.14.1: the fake `now` passed to `_compute_basis` is derived from
    the same anchor as the expiry suffix, so this test is independent of
    wall-clock drift (previously the test computed expiry from real
    `datetime.now()` and then passed a real `time.time()`, which drifts
    across midnight / leap-second / clock-sync jitter)."""
    snap = stream._snapshots["BTC/USDC"]
    import datetime
    # Pick a fixed anchor we fully control — avoids any real-clock dependency.
    anchor = datetime.datetime(2026, 6, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    expiry_dt = anchor + datetime.timedelta(days=30)
    suffix = expiry_dt.strftime("%y%m%d")
    q_symbol = f"PI_XBTUSD_{suffix}"
    perp_tick = {"markPrice": "100.0"}
    q_tick = {"markPrice": "102.0"}
    stream._compute_basis(snap, perp_tick, q_tick, q_symbol, anchor.timestamp())
    # Expected: (102 - 100) / 100 = 0.02 → 0.02 * 365/30 * 100 ≈ 24.33%
    assert snap.basis_apr_pct is not None
    assert 23.0 < snap.basis_apr_pct < 26.0


# ─── Spot-only invariant (meta-test) ─────────────────────────


def test_module_contains_no_order_placement_calls():
    """Verifies the hard invariant: hydra_derivatives_stream.py must
    never place orders on Kraken Futures. We grep the source for any
    order-placement call patterns that would indicate a bug."""
    path = os.path.join(
        os.path.dirname(__file__), "..", "hydra_derivatives_stream.py"
    )
    src = open(path, encoding="utf-8").read()
    forbidden_patterns = [
        "sendOrder",
        "sendorder",
        "api_key",           # no auth credentials belong here
        "apiKey",
        "Authent",
        "editOrder",
        "cancelOrder",
        "/sendorder",
        "/sendOrder",
    ]
    for pat in forbidden_patterns:
        assert pat not in src, (
            f"SPOT-ONLY INVARIANT VIOLATED: '{pat}' appears in "
            f"hydra_derivatives_stream.py. This module must stay read-only."
        )


# ─── Fetch failure-mode logging ──────────────────────────────


def test_fetch_tickers_logs_timeout_distinctly(stream, monkeypatch, capsys):
    s = stream

    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="kraken", timeout=10)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = s._fetch_tickers()
    assert result == []
    err = capsys.readouterr().err
    assert "timeout" in err.lower(), f"expected timeout-labelled warning, got: {err!r}"


def test_fetch_tickers_logs_json_error_distinctly(stream, monkeypatch, capsys):
    s = stream

    class FakeResult:
        stdout = "not json{{"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
    result = s._fetch_tickers()
    assert result == []
    err = capsys.readouterr().err
    assert "json" in err.lower(), f"expected json-labelled warning, got: {err!r}"


def test_fetch_tickers_logs_oserror_distinctly(stream, monkeypatch, capsys):
    s = stream

    def fake_run(*a, **kw):
        raise OSError("WSL not available")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = s._fetch_tickers()
    assert result == []
    err = capsys.readouterr().err
    assert "oserror" in err.lower() or "wsl" in err.lower()


# ─── v2.18.0: snapshot() / restore() round-trip ─────────────


def test_snapshot_roundtrip_rehydrates_history(stream):
    """Round-trip: seed deques, snapshot, restore into fresh stream,
    assert `_delta_pct` returns the same value either side."""
    sym = "PF_XBTUSD"
    now = 1_000_000.0
    samples_oi = [(now - 3600, 1000.0), (now - 1800, 1100.0), (now - 60, 1200.0)]
    samples_px = [(now - 3600, 100.0), (now - 1800, 105.0), (now - 60, 110.0)]
    stream._oi_history[sym] = deque(samples_oi)
    stream._price_history[sym] = deque(samples_px)

    snap = stream.snapshot()
    assert "oi_history" in snap and "price_history" in snap
    assert snap["oi_history"][sym] == [[t, v] for t, v in samples_oi]

    fresh = DerivativesStream(pairs=["BTC/USDC"])
    fresh.restore(snap, now=now)
    # _delta_pct over the 1h window should match pre-snapshot
    before = _delta_pct(stream._oi_history[sym], now - 3600, 1200.0)
    after = _delta_pct(fresh._oi_history[sym], now - 3600, 1200.0)
    assert before == after
    assert after is not None and after > 0


def test_restore_skips_when_history_stale(stream):
    """Gate: newest persisted sample older than MAX_RESTORE_GAP_S →
    drop the history for that symbol, preserving the "don't lie"
    invariant after long downtime."""
    sym = "PF_XBTUSD"
    now = 1_000_000.0
    stale_newest = now - (stream.MAX_RESTORE_GAP_S + 60)
    snap = {
        "oi_history": {sym: [[stale_newest - 600, 1000.0], [stale_newest, 1100.0]]},
        "price_history": {},
    }
    stream.restore(snap, now=now)
    assert sym not in stream._oi_history or len(stream._oi_history[sym]) == 0


def test_restore_merges_without_duplicating_concurrent_samples(stream):
    """Merge semantics: a sample the polling thread collected at the
    same timestamp as a persisted sample must appear exactly once."""
    sym = "PF_XBTUSD"
    now = 1_000_000.0
    shared_ts = now - 120
    stream._oi_history[sym] = deque([(shared_ts, 999.0), (now - 30, 1300.0)])
    snap = {
        "oi_history": {sym: [[now - 600, 800.0], [shared_ts, 1000.0]]},
        "price_history": {},
    }
    stream.restore(snap, now=now)
    timestamps = [t for t, _ in stream._oi_history[sym]]
    assert timestamps == sorted(timestamps)
    assert timestamps.count(shared_ts) == 1
    # New (older) sample from the snapshot merged in
    assert any(t == now - 600 for t, _ in stream._oi_history[sym])
    # Newer live sample preserved
    assert any(t == now - 30 for t, _ in stream._oi_history[sym])


def test_restore_handles_missing_and_malformed_inputs(stream):
    """Fail-soft: None, empty dict, wrong field types, non-numeric
    sample values must all be silently ignored without raising."""
    stream.restore(None)
    stream.restore({})
    stream.restore({"oi_history": "not a dict", "price_history": None})
    stream.restore({"oi_history": {"PF_XBTUSD": [["bad", "values"]]}})
    # All no-ops; no exceptions, deques remain untouched
    assert stream._oi_history == {} or all(
        len(dq) == 0 for dq in stream._oi_history.values()
    )


def test_snapshot_json_roundtrip_preserves_floats(stream):
    """Exercise the real persistence path: snapshot() → json.dumps →
    json.loads → restore(). Catches any future regression where floats
    accidentally become strings via `default=str` fallbacks."""
    import json

    sym = "PF_XBTUSD"
    now = 1_000_000.0
    stream._oi_history[sym] = deque([(now - 120, 1000.5), (now - 60, 1050.25)])
    stream._price_history[sym] = deque([(now - 120, 99.9), (now - 60, 100.1)])

    raw = json.dumps(stream.snapshot())
    revived = json.loads(raw)

    fresh = DerivativesStream(pairs=["BTC/USDC"])
    fresh.restore(revived, now=now)
    assert list(fresh._oi_history[sym]) == list(stream._oi_history[sym])
    assert list(fresh._price_history[sym]) == list(stream._price_history[sym])


def test_restore_salvages_partially_malformed_sample_list(stream):
    """Per-element parsing: a single bad tuple inside an otherwise valid
    sample list must not discard the symbol's entire history."""
    sym = "PF_XBTUSD"
    now = 1_000_000.0
    snap = {
        "oi_history": {
            sym: [
                [now - 300, 1000.0],
                ["not-a-number", 1050.0],   # malformed
                [now - 60, 1100.0],
            ],
        },
        "price_history": {},
    }
    stream.restore(snap, now=now)
    timestamps = [t for t, _ in stream._oi_history[sym]]
    assert now - 300 in timestamps
    assert now - 60 in timestamps
    assert len(timestamps) == 2  # one bad entry dropped, two survive


def test_restore_respects_history_window_prune_on_load(stream):
    """Pruning: samples older than HISTORY_WINDOW_S on the merged
    result must be dropped so a pathological snapshot cannot balloon
    the deque past its normal cap."""
    sym = "PF_XBTUSD"
    now = 1_000_000.0
    # Persisted samples span just inside the window; fresh-enough to
    # pass the MAX_RESTORE_GAP_S gate on the newest sample.
    very_old = now - (stream.HISTORY_WINDOW_S + 100)
    snap = {
        "oi_history": {sym: [[very_old, 500.0], [now - 300, 1200.0]]},
        "price_history": {},
    }
    stream.restore(snap, now=now)
    timestamps = [t for t, _ in stream._oi_history[sym]]
    assert all(t >= now - stream.HISTORY_WINDOW_S for t in timestamps)
    assert very_old not in timestamps
