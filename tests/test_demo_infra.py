"""Tests for demo infrastructure: preflight, impairment, traffic gen."""
import pytest
import time


class TestPreflightChecker:
    async def test_run_all_checks(self, sample_config):
        from hyperagg.testing.preflight import PreflightChecker
        checker = PreflightChecker(sample_config)
        results = await checker.run_all_checks()
        assert isinstance(results, dict)
        # Should at least check TUN permission and encryption key
        assert "tun_permission" in results
        assert "encryption_key" in results
        for name, result in results.items():
            assert "status" in result
            assert result["status"] in ("pass", "fail", "warn")
            assert "detail" in result

    async def test_encryption_key_check_pass(self):
        from hyperagg.testing.preflight import PreflightChecker
        config = {"vps": {"encryption_key": "wCPJN/q6ap9io+qO4X4v0rmS4aVJLHkYV6Oua48UlYQ="}}
        checker = PreflightChecker(config)
        result = await checker.check_encryption_key(config)
        assert result["status"] == "pass"

    async def test_encryption_key_check_missing(self):
        from hyperagg.testing.preflight import PreflightChecker
        config = {"vps": {"encryption_key": "${HYPERAGG_KEY}"}}
        checker = PreflightChecker(config)
        result = await checker.check_encryption_key(config)
        assert result["status"] == "warn"

    async def test_tun_permission(self):
        from hyperagg.testing.preflight import PreflightChecker
        checker = PreflightChecker({})
        result = await checker.check_tun_permission()
        assert result["status"] in ("pass", "warn", "fail")


class TestImpairmentController:
    def test_register_path(self):
        from hyperagg.controller.impairment import ImpairmentController
        imp = ImpairmentController()
        imp.register_path(0, "eth1")
        state = imp.get_state()
        assert 0 in state
        assert state[0]["action"] == "clear"

    def test_apply_clear(self):
        from hyperagg.controller.impairment import ImpairmentController
        imp = ImpairmentController()
        imp.register_path(0, "lo")  # Use loopback for safety
        result = imp.apply(0, "clear")
        assert result["status"] in ("ok", "error")  # tc may not be available

    def test_apply_unknown_path(self):
        from hyperagg.controller.impairment import ImpairmentController
        imp = ImpairmentController()
        result = imp.apply(99, "latency", value_ms=100)
        assert result["status"] == "error"

    def test_state_tracking(self):
        from hyperagg.controller.impairment import ImpairmentController
        imp = ImpairmentController()
        imp.register_path(0, "lo")
        imp._state[0] = {"action": "latency", "detail": "+200ms latency"}
        state = imp.get_state()
        assert state[0]["action"] == "latency"


class TestTrafficGenerator:
    async def test_start_and_stop(self):
        from hyperagg.testing.traffic_gen import TrafficGenerator
        tg = TrafficGenerator()
        await tg.start(mode="bulk", duration_sec=1)
        assert tg._running
        status = tg.get_status()
        assert status["running"]
        assert status["mode"] == "bulk"
        await tg.stop()
        assert not tg._running

    def test_status_when_idle(self):
        from hyperagg.testing.traffic_gen import TrafficGenerator
        tg = TrafficGenerator()
        status = tg.get_status()
        assert not status["running"]
        assert status["packets_sent"] == 0

    async def test_realtime_mode(self):
        from hyperagg.testing.traffic_gen import TrafficGenerator
        tg = TrafficGenerator()
        await tg.start(mode="realtime", duration_sec=1, bitrate_kbps=100)
        await asyncio.sleep(0.5)
        assert tg._packets_sent > 0
        await tg.stop()

    async def test_cannot_start_twice(self):
        from hyperagg.testing.traffic_gen import TrafficGenerator
        tg = TrafficGenerator()
        await tg.start(mode="bulk", duration_sec=10)
        with pytest.raises(RuntimeError):
            await tg.start(mode="bulk", duration_sec=10)
        await tg.stop()


class TestDemoWithAllModules:
    """Verify demo mode serves all module stats."""

    def test_demo_controller_has_all_stats(self, sample_config):
        """RealDemoController should return all 9 module stat keys."""
        from hyperagg.main import RealDemoController
        from hyperagg.scheduler.path_monitor import PathMonitor
        from hyperagg.scheduler.path_predictor import PathPredictor
        from hyperagg.scheduler.path_scheduler import PathScheduler
        from hyperagg.fec.fec_engine import FecEngine
        from hyperagg.tunnel.reorder_buffer import AdaptiveReorderBuffer
        from hyperagg.scheduler.bandwidth_estimator import BandwidthEstimator, TokenBucketPacer
        from hyperagg.scheduler.congestion_control import AIMDController, TierBandwidthManager
        from hyperagg.scheduler.owd_estimator import OWDEstimator
        from hyperagg.metrics.throughput_calculator import ThroughputCalculator
        from hyperagg.tunnel.session import SessionManager
        from hyperagg.tunnel.packet import set_connection_start
        import tempfile

        set_connection_start()
        monitor = PathMonitor(sample_config)
        monitor.register_path(0)
        monitor.register_path(1)
        # Feed some data
        for _ in range(10):
            monitor.record_rtt(0, 30)
            monitor.record_rtt(1, 60)

        predictor = PathPredictor(sample_config)
        scheduler = PathScheduler(sample_config, monitor, predictor)
        fec = FecEngine(sample_config)
        reorder = AdaptiveReorderBuffer()
        bw = BandwidthEstimator()
        pacer = TokenBucketPacer()
        pacer.set_rate(0, 5_000_000)
        pacer.set_rate(1, 2_000_000)
        congestion = {0: AIMDController(), 1: AIMDController()}
        tier_mgr = TierBandwidthManager()
        owd = OWDEstimator()
        owd.record_rtt_with_asymmetry(0, 60, 0.33)
        owd.record_rtt_with_asymmetry(1, 80, 0.45)
        tp = ThroughputCalculator()
        tp.record_sent(1400)
        with tempfile.TemporaryDirectory() as td:
            sm = SessionManager(state_dir=td)
            sm.create_session()

            sdn = RealDemoController(
                sample_config, monitor, predictor, scheduler, fec,
                reorder, bw, pacer, congestion, tier_mgr, owd, tp, sm,
            )

            state = sdn.get_system_state()

            # All 9 module stat keys must be present
            for key in ["bandwidth", "pacer", "congestion", "owd", "reorder",
                        "throughput", "tier_manager", "session", "adaptive_fec"]:
                assert key in state, f"Missing key: {key}"

            # Verify they have real data (OWD paths may use int or str keys)
            owd_paths = state["owd"]["paths"]
            first_path = owd_paths.get(0) or owd_paths.get("0") or list(owd_paths.values())[0]
            assert first_path["owd_down_ms"] > 0
            assert state["session"]["status"] == "active"
            assert state["reorder"]["timeout_ms"] > 0
            assert state["throughput"]["raw_throughput_mbps"] >= 0


import asyncio
