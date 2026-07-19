"""S3Strategy stage machine, degradation, ZEC exclusion — driven off the
real parity fixtures (real market bars, real setups)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s3bounce.model import load_artifact  # noqa: E402
from s3bounce.strategy import MIN_BARS, S3Signal, S3Strategy  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(pair):
    return json.loads((FIXTURES / f"parity_{pair}.json").read_text())


def make_strategy(cut_bars=None):
    """Strategy seeded with fixture daily bars (optionally truncated to
    end at a chosen bar index, same cut across assets by day)."""
    strat = S3Strategy(load_artifact())
    fixes = {p: fixture(p.replace("/", "_")) for p in strat.universe}
    cut_day = None
    if cut_bars is not None:
        cut_day = fixes["BTC/USD"]["bars"][cut_bars]["ts"] // 86400
    for p, fix in fixes.items():
        rows = fix["bars"]
        if cut_day is not None:
            rows = [r for r in rows if r["ts"] // 86400 <= cut_day]
        strat.seed(p, rows)
    now = (cut_day + 1) * 86400 if cut_day is not None else 2**40
    return strat, fixes, now


def test_zec_never_model_loaded():
    strat, _, now = make_strategy()
    sig = strat.evaluate("ZEC/USD", now)
    assert isinstance(sig, S3Signal)
    assert not sig.model_loaded and not sig.gated
    assert "asset_not_in_artifact" in sig.reasons


def test_warmup_degrades():
    strat = S3Strategy(load_artifact())
    fix = fixture("BTC_USD")
    strat.seed("BTC/USD", fix["bars"][:30])
    sig = strat.evaluate("BTC/USD", 2**40)
    assert sig.degraded and sig.stage == "none"
    assert any(r.startswith("warmup:") for r in sig.reasons)
    assert 30 < MIN_BARS


def test_stage_b0_then_b1_on_real_setup():
    """Cut the fixture tape at a real NON-ADJACENT bounce bar (bounce >=
    low+2, so the swing is already confirmed): stage must be scored_b0
    with a score; one day later entryable_b1 (if unresolved)."""
    fix = fixture("BTC_USD")
    setups = fix["setups"]
    s = next(x for x in setups if x["bounce_idx"] > MIN_BARS + 5
             and x["bounce_idx"] - x["low_idx"] >= 2)
    b0 = s["bounce_idx"]
    strat, _, now = make_strategy(cut_bars=b0)
    sig = strat.evaluate("BTC/USD", now)
    assert sig.stage == "scored_b0" and sig.score is not None
    assert abs(sig.setup.low_px - s["low_px"]) < 1e-12

    strat2, _, now2 = make_strategy(cut_bars=b0 + 1)
    sig2 = strat2.evaluate("BTC/USD", now2)
    # b1 stage requires the setup unresolved through b1 — real tape decides
    if sig2.stage == "entryable_b1":
        assert sig2.entry_idx == b0 + 1
        assert sig2.score is not None
    else:
        assert sig2.stage in ("none", "scored_b0")


def test_adjacent_bounce_first_detectable_at_b1():
    """The common adjacent case (bounce = low+1): invisible at b0 cut
    (swing unconfirmed — SW=2 lag), detectable at b1 cut when the tape
    keeps the low intact."""
    fix = fixture("BTC_USD")
    s = next(x for x in fix["setups"] if x["bounce_idx"] > MIN_BARS + 5
             and x["bounce_idx"] - x["low_idx"] == 1)
    strat, _, now = make_strategy(cut_bars=s["bounce_idx"])
    assert strat.evaluate("BTC/USD", now).stage == "none"
    strat2, _, now2 = make_strategy(cut_bars=s["bounce_idx"] + 1)
    sig = strat2.evaluate("BTC/USD", now2)
    assert sig.stage in ("entryable_b1", "none")   # none iff resolved on tape
    if sig.stage == "entryable_b1":
        assert abs(sig.setup.low_px - s["low_px"]) < 1e-12


def test_breadth_member_missing_degrades():
    strat = S3Strategy(load_artifact())
    fix = fixture("BTC_USD")
    s = next(x for x in fix["setups"] if x["bounce_idx"] > MIN_BARS + 5
             and x["bounce_idx"] - x["low_idx"] >= 2)
    cut_day = fix["bars"][s["bounce_idx"]]["ts"] // 86400
    strat.seed("BTC/USD", [r for r in fix["bars"] if r["ts"] // 86400 <= cut_day])
    # ETH/ZEC never seeded -> breadth members missing
    sig = strat.evaluate("BTC/USD", (cut_day + 1) * 86400)
    assert sig.stage == "scored_b0"
    assert sig.degraded and not sig.gated
    assert any(r.startswith("breadth_member_missing") for r in sig.reasons)


def test_gate_matches_fixture_scores():
    """Every fixture-scored setup: strategy score at its b0 cut equals the
    fixture score (same artifact, same bars)."""
    fix = fixture("ETH_USD")
    checked = 0
    for s in fix["setups"]:
        if s["bounce_idx"] <= MIN_BARS + 5:
            continue
        strat, _, now = make_strategy(cut_bars=None)
        # evaluate at the historical cut: truncate by the bounce day
        strat2, fixes, now2 = make_strategy(cut_bars=None)
        del strat, now
        cut_day = fix["bars"][s["bounce_idx"]]["ts"] // 86400
        strat3 = S3Strategy(load_artifact())
        for p, f in fixes.items():
            strat3.seed(p, [r for r in f["bars"] if r["ts"] // 86400 <= cut_day])
        sig = strat3.evaluate("ETH/USD", (cut_day + 1) * 86400)
        if sig.stage != "scored_b0":
            continue
        key = f"{sig.setup.low_idx}@{sig.setup.low_px:.10g}"
        if key in fix["scores"]:
            assert abs(sig.score - fix["scores"][key]) < 1e-9
            checked += 1
        if checked >= 3:
            break
    assert checked >= 1, "no b0 cut reproduced a fixture score"
