"""
HYDRA ExecutionStream Health & Auto-Restart Test Suite

Validates the health diagnostics, ensure_healthy auto-restart with cooldown,
reader-thread exit reason tracking, FakeExecutionStream parity, and the
agent tick body's transition-only warning behavior.

No real `kraken ws executions` subprocess is spawned — tests stub Popen and
manipulate internal state directly. The live subprocess path is exercised by
the live_harness in `validate` and `live` modes.
"""

import sys
import os
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_streams import FakeExecutionStream  # noqa: E402
from hydra_streams import ExecutionStream


# ═══════════════════════════════════════════════════════════════
# Helpers — fake Popen object so we don't actually spawn anything
# ═══════════════════════════════════════════════════════════════

class _FakeProc:
    """Stand-in for subprocess.Popen with controllable poll() behavior."""

    def __init__(self, rc=None):
        self._rc = rc
        self.terminated = False
        self.killed = False
        self.stdout = None
        self.stderr = None
        self.pid = 99999

    def poll(self):
        return self._rc

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        if self._rc is None:
            self._rc = -9

    def wait(self, timeout=None):
        return self._rc

    def set_exit(self, rc):
        self._rc = rc


def _make_stream_with_fake_proc(rc=None, hb_age_s=0.0, reader_alive=True):
    """Build an ExecutionStream wired to a _FakeProc, with the heartbeat and
    reader-thread state preconfigured to whatever the test needs."""
    es = ExecutionStream(paper=False)
    es._proc = _FakeProc(rc=rc)
    # Heartbeat tracked on the monotonic clock — match production semantics
    # so the staleness math is identical.
    es._last_heartbeat = time.monotonic() - hb_age_s
    if reader_alive:
        # A trivially-alive thread we can interrogate via .is_alive()
        es._reader_thread = _LiveDaemon()
    else:
        es._reader_thread = _DeadThread()
    return es


class _LiveDaemon(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, target=self._run)
        self._stop = threading.Event()
        self.start()

    def _run(self):
        self._stop.wait()  # block until told to stop

    def stop(self):
        self._stop.set()


class _DeadThread:
    """Quack-alike for a thread that has already exited."""
    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


# ═══════════════════════════════════════════════════════════════
# TEST: health_status — paper short-circuit
# ═══════════════════════════════════════════════════════════════

class TestHealthStatusPaper:
    def test_paper_always_healthy(self):
        es = ExecutionStream(paper=True)
        assert es.health_status() == (True, "")
        assert es.healthy is True

    def test_paper_ensure_healthy_noop(self):
        es = ExecutionStream(paper=True)
        # Even with no proc, paper short-circuits.
        assert es.ensure_healthy() == (True, "")


# ═══════════════════════════════════════════════════════════════
# TEST: health_status — diagnostic reasons
# ═══════════════════════════════════════════════════════════════

class TestHealthStatusReasons:
    def test_subprocess_not_started(self):
        es = ExecutionStream(paper=False)
        ok, reason = es.health_status()
        assert ok is False
        assert reason == "subprocess not started"

    def test_subprocess_exited_includes_rc(self):
        es = _make_stream_with_fake_proc(rc=137)
        try:
            ok, reason = es.health_status()
            assert ok is False
            assert "subprocess exited" in reason
            assert "137" in reason
        finally:
            es._reader_thread.stop()

    def test_reader_thread_dead(self):
        es = _make_stream_with_fake_proc(rc=None, reader_alive=False)
        es._reader_exit_reason = "EOF (subprocess closed stdout)"
        ok, reason = es.health_status()
        assert ok is False
        assert reason.startswith("reader thread")
        assert "EOF" in reason

    def test_reader_thread_dead_unknown_reason(self):
        es = _make_stream_with_fake_proc(rc=None, reader_alive=False)
        es._reader_exit_reason = None
        ok, reason = es.health_status()
        assert ok is False
        assert "unknown" in reason

    def test_heartbeat_stale_includes_age(self):
        # Force a stale heartbeat by setting it 60s in the past (well over
        # the 30s threshold).
        es = _make_stream_with_fake_proc(rc=None, hb_age_s=60.0)
        try:
            ok, reason = es.health_status()
            assert ok is False
            assert "no heartbeat" in reason
            assert "60s" in reason
        finally:
            es._reader_thread.stop()

    def test_healthy_when_all_checks_pass(self):
        es = _make_stream_with_fake_proc(rc=None, hb_age_s=1.0)
        try:
            assert es.health_status() == (True, "")
            assert es.healthy is True
        finally:
            es._reader_thread.stop()


# ═══════════════════════════════════════════════════════════════
# TEST: ensure_healthy — auto-restart and cooldown
# ═══════════════════════════════════════════════════════════════

class _RestartCounter:
    """Replaces ExecutionStream.start so tests can track invocations without
    spawning a real subprocess."""

    def __init__(self, es: ExecutionStream, restart_makes_healthy=True):
        self.es = es
        self.calls = 0
        self.restart_makes_healthy = restart_makes_healthy
        self._original = es.start

    def install(self):
        def fake_start():
            self.calls += 1
            if self.restart_makes_healthy:
                self.es._proc = _FakeProc(rc=None)
                self.es._reader_thread = _LiveDaemon()
                self.es._last_heartbeat = time.monotonic()
            return True
        self.es.start = fake_start

    def restore(self):
        self.es.start = self._original


class TestEnsureHealthyRestart:
    def test_healthy_returns_without_restart(self):
        es = _make_stream_with_fake_proc(rc=None, hb_age_s=1.0)
        rc = _RestartCounter(es)
        rc.install()
        try:
            ok, reason = es.ensure_healthy()
            assert ok is True
            assert reason == ""
            assert rc.calls == 0
        finally:
            rc.restore()
            es._reader_thread.stop()

    def test_unhealthy_triggers_restart(self):
        es = _make_stream_with_fake_proc(rc=137)
        # Drain the cooldown by setting last attempt far in the past
        es._last_restart_attempt = 0.0
        rc = _RestartCounter(es)
        rc.install()
        try:
            ok, reason = es.ensure_healthy()
            assert rc.calls == 1
            assert ok is True
            assert es._restart_count == 1
        finally:
            rc.restore()
            if es._reader_thread is not None:
                try:
                    es._reader_thread.stop()
                except Exception:
                    pass

    def test_cooldown_suppresses_second_restart(self):
        es = _make_stream_with_fake_proc(rc=137)
        es._last_restart_attempt = 0.0
        rc = _RestartCounter(es, restart_makes_healthy=False)
        rc.install()
        try:
            es.ensure_healthy()
            # First call attempted a restart
            assert rc.calls == 1
            # Mark unhealthy again — second call must NOT call start() because
            # the cooldown timer was just bumped.
            es._proc = _FakeProc(rc=137)
            ok, reason = es.ensure_healthy()
            assert rc.calls == 1, "second call should be cooldown-suppressed"
            assert ok is False
        finally:
            rc.restore()
            if es._reader_thread is not None:
                try:
                    es._reader_thread.stop()
                except Exception:
                    pass

    def test_cooldown_expiry_allows_new_restart(self):
        es = _make_stream_with_fake_proc(rc=137)
        es._last_restart_attempt = 0.0
        rc = _RestartCounter(es, restart_makes_healthy=False)
        rc.install()
        try:
            es.ensure_healthy()
            assert rc.calls == 1
            # Backdate the cooldown timer past RESTART_COOLDOWN_S so the
            # next call is allowed.
            es._last_restart_attempt = time.monotonic() - es.RESTART_COOLDOWN_S - 1
            es._proc = _FakeProc(rc=137)
            es.ensure_healthy()
            assert rc.calls == 2
        finally:
            rc.restore()
            if es._reader_thread is not None:
                try:
                    es._reader_thread.stop()
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════════════
# TEST: heartbeat dispatch updates the timestamp
# ═══════════════════════════════════════════════════════════════

class TestDispatchHeartbeat:
    def test_heartbeat_channel_bumps_timestamp(self):
        es = ExecutionStream(paper=False)
        es._last_heartbeat = 0.0  # ancient
        es._on_message({"channel": "heartbeat"})
        # Production uses monotonic; the bump must be a recent monotonic value.
        assert es._last_heartbeat > time.monotonic() - 1.0

    def test_executions_channel_bumps_timestamp(self):
        es = ExecutionStream(paper=False)
        es._last_heartbeat = 0.0
        es._on_message({"channel": "executions", "type": "update", "data": []})
        assert es._last_heartbeat > time.monotonic() - 1.0

    def test_status_channel_does_not_bump(self):
        es = ExecutionStream(paper=False)
        es._last_heartbeat = 12345.0  # canary value
        es._on_message({"channel": "status", "data": []})
        assert es._last_heartbeat == 12345.0

    def test_subscribe_response_does_not_bump(self):
        es = ExecutionStream(paper=False)
        es._last_heartbeat = 12345.0
        es._on_message({"method": "subscribe", "success": True})
        assert es._last_heartbeat == 12345.0


# ═══════════════════════════════════════════════════════════════
# TEST: FakeExecutionStream parity — health_status / ensure_healthy
# ═══════════════════════════════════════════════════════════════

class TestFakeExecutionStreamParity:
    def test_default_healthy(self):
        f = FakeExecutionStream()
        assert f.healthy is True
        assert f.health_status() == (True, "")
        assert f.ensure_healthy() == (True, "")

    def test_marked_unhealthy_reports_diagnostic(self):
        f = FakeExecutionStream()
        f.set_healthy(False)
        ok, reason = f.health_status()
        assert ok is False
        assert "fake stream marked unhealthy" in reason

    def test_ensure_healthy_does_not_restart(self):
        f = FakeExecutionStream()
        f.set_healthy(False)
        # FakeExecutionStream.start is a no-op; ensure_healthy should report
        # the same state without trying to "restart".
        ok, reason = f.ensure_healthy()
        assert ok is False
        assert "fake stream marked unhealthy" in reason


# ═══════════════════════════════════════════════════════════════
# TEST: restart preserves correlation maps + resets sequence
# ═══════════════════════════════════════════════════════════════

class TestRestartStatePreservation:
    def test_restart_preserves_known_orders(self):
        """An auto-restart in the middle of a placed-but-unfilled order must
        NOT lose the correlation map — the new subprocess's snapshot replay
        will deliver the fill via the same order_id, and the agent needs
        the journal_index + engine_ref + pre_trade_snapshot to finalize."""
        es = _make_stream_with_fake_proc(rc=137)
        # Pre-register an in-flight order
        es.register(
            order_id="ORDER_ABC", userref=12345, journal_index=7,
            pair="SOL/USDC", side="BUY", placed_amount=1.0,
            engine_ref="<engine>", pre_trade_snapshot={"size": 0},
        )
        es._last_restart_attempt = 0.0
        rc = _RestartCounter(es)
        rc.install()
        try:
            es.ensure_healthy()
            assert "ORDER_ABC" in es._known_orders
            assert es._userref_to_order_id.get(12345) == "ORDER_ABC"
            entry = es._known_orders["ORDER_ABC"]
            assert entry["journal_index"] == 7
            assert entry["pair"] == "SOL/USDC"
            assert entry["pre_trade_snapshot"] == {"size": 0}
        finally:
            rc.restore()
            if es._reader_thread is not None:
                try:
                    es._reader_thread.stop()
                except Exception:
                    pass

    def test_start_resets_last_sequence(self):
        """A restart spawns a fresh WS connection that starts its own
        sequence numbering at 1. Carrying over the old _last_sequence would
        produce a spurious 'sequence gap' warning on the first executions
        message after restart."""
        import hydra_streams
        es = ExecutionStream(paper=False)
        es._last_sequence = 9999
        # Patch the subprocess module reference used inside hydra_streams so
        # start() spawns a stub instead of a real wsl.exe. Restore in finally
        # to keep sibling tests hermetic.
        original_popen = hydra_streams.subprocess.Popen
        try:
            hydra_streams.subprocess.Popen = lambda *a, **kw: _make_empty_fake_proc()
            # Suppress the [EXECSTREAM] start/exit prints so they don't
            # pollute test output — they're normal here, just noisy.
            with _PrintCapture():
                ok = es.start()
                # Give the reader thread a moment to drain its empty iterator
                # and exit cleanly so its 'reader thread exited' print also
                # falls inside the capture window.
                if es._reader_thread is not None:
                    es._reader_thread.join(timeout=1.0)
            assert ok is True
            assert es._last_sequence is None
        finally:
            hydra_streams.subprocess.Popen = original_popen
            with _PrintCapture():
                es.stop()


def _make_empty_fake_proc():
    """A _FakeProc whose stdout/stderr are immediately-exhausted iterators
    so start()'s reader/stderr threads exit cleanly without blocking."""
    proc = _FakeProc(rc=None)
    proc.stdout = _EmptyIter()
    proc.stderr = _EmptyIter()
    return proc


class _EmptyIter:
    """Behaves like an immediately-exhausted file iterator (iter() then EOF)."""
    def __iter__(self):
        return iter([])


# ═══════════════════════════════════════════════════════════════
# TEST: agent tick warning rate-limit (transition only)
# ═══════════════════════════════════════════════════════════════

class _PrintCapture:
    """Captures stdout prints emitted during a block."""
    def __init__(self):
        self.lines = []

    def __enter__(self):
        import io
        self._buf = io.StringIO()
        self._sys_stdout = sys.stdout
        sys.stdout = self._buf
        return self

    def __exit__(self, *exc):
        sys.stdout = self._sys_stdout
        self.lines = self._buf.getvalue().splitlines()


def _simulate_tick_health_check(agent, stream):
    """Reproduces the EXACT logic in HydraAgent.run()'s tick body so we can
    unit-test the warning rate-limit without spinning up a full agent loop."""
    if not stream.paper:
        healthy, reason = stream.ensure_healthy()
        if not healthy:
            if agent._exec_stream_warned_reason != reason:
                print(
                    f"  [WARN] execution stream unhealthy — {reason} "
                    f"(lifecycle finalization stalled)"
                )
                agent._exec_stream_warned_reason = reason
        elif agent._exec_stream_warned_reason is not None:
            print("  [EXECSTREAM] stream healthy again")
            agent._exec_stream_warned_reason = None


class _MiniAgent:
    """Minimum surface area to drive _simulate_tick_health_check."""
    def __init__(self):
        self._exec_stream_warned_reason = None


class TestAgentTickWarningRateLimit:
    def test_first_unhealthy_prints_warning(self):
        agent = _MiniAgent()
        stream = FakeExecutionStream()
        stream.set_healthy(False)
        with _PrintCapture() as cap:
            _simulate_tick_health_check(agent, stream)
        warning_lines = [l for l in cap.lines if "[WARN]" in l]
        assert len(warning_lines) == 1
        assert "fake stream marked unhealthy" in warning_lines[0]
        assert agent._exec_stream_warned_reason == "fake stream marked unhealthy"

    def test_repeated_unhealthy_same_reason_silent(self):
        agent = _MiniAgent()
        stream = FakeExecutionStream()
        stream.set_healthy(False)
        # First tick: prints
        with _PrintCapture():
            _simulate_tick_health_check(agent, stream)
        # Subsequent ticks with the SAME reason: silent
        with _PrintCapture() as cap:
            _simulate_tick_health_check(agent, stream)
            _simulate_tick_health_check(agent, stream)
            _simulate_tick_health_check(agent, stream)
        warning_lines = [l for l in cap.lines if "[WARN]" in l]
        assert len(warning_lines) == 0, (
            f"Repeated same-reason ticks must not re-print; got: {warning_lines}"
        )

    def test_recovery_prints_healthy_again_once(self):
        agent = _MiniAgent()
        stream = FakeExecutionStream()
        stream.set_healthy(False)
        with _PrintCapture():
            _simulate_tick_health_check(agent, stream)
        # Now recover
        stream.set_healthy(True)
        with _PrintCapture() as cap:
            _simulate_tick_health_check(agent, stream)
        recovery_lines = [l for l in cap.lines if "healthy again" in l]
        assert len(recovery_lines) == 1
        assert agent._exec_stream_warned_reason is None
        # Subsequent healthy ticks must NOT re-print
        with _PrintCapture() as cap:
            _simulate_tick_health_check(agent, stream)
            _simulate_tick_health_check(agent, stream)
        assert all("healthy again" not in l for l in cap.lines)

    def test_paper_stream_skips_check_entirely(self):
        agent = _MiniAgent()
        stream = ExecutionStream(paper=True)
        with _PrintCapture() as cap:
            _simulate_tick_health_check(agent, stream)
        assert cap.lines == []
        assert agent._exec_stream_warned_reason is None

    def test_starts_healthy_no_initial_print(self):
        """First tick on a healthy stream must produce no print at all."""
        agent = _MiniAgent()
        stream = FakeExecutionStream()  # default: healthy
        with _PrintCapture() as cap:
            _simulate_tick_health_check(agent, stream)
        assert cap.lines == []
        assert agent._exec_stream_warned_reason is None


# ═══════════════════════════════════════════════════════════════
# Test runner
# ═══════════════════════════════════════════════════════════════

class TestFillTolerance:
    """_is_fully_filled tolerance (audit-2026-05-28 finding #5).

    The previous 1% tolerance mis-classified near-full fills (e.g. 99.5% of
    placed) as FILLED, skipping reconcile_partial_fill and leaving the engine
    permanently over-committed. The dust-level FILL_TOLERANCE (1e-6) absorbs
    float noise on genuine full fills while routing any real shortfall to the
    PARTIALLY_FILLED path.
    """

    def test_exact_full_fill_is_filled(self):
        from hydra_streams import _is_fully_filled
        assert _is_fully_filled(1.0, 1.0) is True

    def test_float_noise_full_fill_is_filled(self):
        from hydra_streams import _is_fully_filled
        # ~1e-9 relative noise on a genuine full fill still counts as FILLED.
        assert _is_fully_filled(1.0 - 1e-9, 1.0) is True

    def test_near_full_fill_is_partial(self):
        from hydra_streams import _is_fully_filled
        # 99.5% filled must NOT be treated as fully filled (the bug case).
        assert _is_fully_filled(0.995, 1.0) is False
        assert _is_fully_filled(99.5, 100.0) is False

    def test_clear_partial_is_partial(self):
        from hydra_streams import _is_fully_filled
        assert _is_fully_filled(0.4, 1.0) is False

    def test_tolerance_boundary(self):
        from hydra_streams import _is_fully_filled, FILL_TOLERANCE
        assert FILL_TOLERANCE == 1e-6
        assert _is_fully_filled(1.0 - 5e-7, 1.0) is True    # inside dust band
        assert _is_fully_filled(1.0 - 2e-6, 1.0) is False   # outside dust band

    def test_zero_placed_is_not_filled(self):
        from hydra_streams import _is_fully_filled
        assert _is_fully_filled(0.0, 0.0) is False


def run_tests():
    passed = 0
    failed = 0
    errors = []

    test_classes = [
        TestHealthStatusPaper,
        TestHealthStatusReasons,
        TestEnsureHealthyRestart,
        TestDispatchHeartbeat,
        TestFakeExecutionStreamParity,
        TestRestartStatePreservation,
        TestAgentTickWarningRateLimit,
        TestFillTolerance,
    ]

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in sorted(methods):
            test_name = f"{cls.__name__}.{method_name}"
            try:
                getattr(instance, method_name)()
                passed += 1
                print(f"  PASS  {test_name}")
            except AssertionError as e:
                failed += 1
                errors.append((test_name, str(e)))
                print(f"  FAIL  {test_name}: {e}")
            except Exception as e:
                failed += 1
                errors.append((test_name, str(e)))
                print(f"  FAIL  {test_name} (error): {e}")

    print(f"\n  {'='*60}")
    print(f"  ExecutionStream Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")

    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
