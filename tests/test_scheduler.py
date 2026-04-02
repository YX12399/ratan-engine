"""Tests for AI path scheduler."""
import random
import time
import pytest
from hyperagg.scheduler.path_monitor import PathMonitor
from hyperagg.scheduler.path_predictor import PathPredictor
from hyperagg.scheduler.path_scheduler import PathScheduler
from hyperagg.scheduler.models import PathState


@pytest.fixture
def config():
    return {
        "scheduler": {
            "mode": "ai",
            "history_window": 50,
            "ewma_alpha": 0.3,
            "latency_budget_ms": 150,
            "probe_interval_ms": 100,
            "jitter_weight": 0.3,
            "loss_weight": 0.5,
            "rtt_weight": 0.2,
        }
    }


@pytest.fixture
def monitor(config):
    m = PathMonitor(config)
    m.register_path(0)
    m.register_path(1)
    return m


@pytest.fixture
def scheduler(config, monitor):
    predictor = PathPredictor(config)
    return PathScheduler(config, monitor, predictor)


class TestPathMonitor:
    def test_quality_score_range(self, monitor):
        for _ in range(20):
            monitor.record_rtt(0, random.gauss(30, 3))
        state = monitor.get_path_state(0)
        assert 0.0 <= state.quality_score <= 1.0

    def test_loss_recording(self, monitor):
        for _ in range(10):
            monitor.record_rtt(0, 30)
        for _ in range(5):
            monitor.record_loss(0)
        state = monitor.get_path_state(0)
        assert state.loss_pct > 0

    def test_path_dies_on_consecutive_failures(self, monitor):
        for _ in range(5):
            monitor.record_loss(0)
        state = monitor.get_path_state(0)
        assert not state.is_alive

    def test_good_path_stays_alive(self, monitor):
        for _ in range(20):
            monitor.record_rtt(0, 30)
        state = monitor.get_path_state(0)
        assert state.is_alive


class TestPathPredictor:
    def test_delivery_probability_range(self, config):
        predictor = PathPredictor(config)
        state = PathState(
            path_id=0, avg_rtt_ms=30, jitter_ms=5, loss_pct=0.01,
            is_alive=True, rtt_history=[30 + random.gauss(0, 3) for _ in range(20)],
        )
        prob = predictor.predict_delivery_probability(0, 150, state)
        assert 0.0 <= prob <= 1.0

    def test_dead_path_zero_probability(self, config):
        predictor = PathPredictor(config)
        state = PathState(path_id=0, is_alive=False)
        prob = predictor.predict_delivery_probability(0, 150, state)
        assert prob == 0.0

    def test_trend_detection(self, config):
        predictor = PathPredictor(config)
        # Degrading path
        degrading_hist = [30 + i * 5 for i in range(30)]
        state = PathState(path_id=0, is_alive=True, rtt_history=degrading_hist)
        pred = predictor.predict_path_in_future(0, 200, state)
        assert pred.trend == "degrading"

    def test_recommended_action_thresholds(self, config):
        predictor = PathPredictor(config)
        # Excellent path
        state = PathState(
            path_id=0, avg_rtt_ms=20, jitter_ms=1, loss_pct=0.0,
            is_alive=True, rtt_history=[20 + random.gauss(0, 1) for _ in range(20)],
        )
        pred = predictor.predict_path_in_future(0, 200, state)
        assert pred.recommended_action in ("prefer", "use")


class TestPathScheduler:
    def test_decision_with_two_paths(self, scheduler, monitor):
        for _ in range(30):
            monitor.record_rtt(0, random.gauss(30, 3))
            monitor.record_rtt(1, random.gauss(65, 10))
        scheduler.update_predictions()

        decision = scheduler.schedule(0, "bulk")
        assert len(decision.assignments) > 0
        assert decision.reason != ""

    def test_realtime_replicates(self, scheduler, monitor):
        for _ in range(30):
            monitor.record_rtt(0, 30)
            monitor.record_rtt(1, 60)
        scheduler.update_predictions()

        decision = scheduler.schedule(0, "realtime")
        # Should replicate on all alive paths
        sending = [a for a in decision.assignments if a.send]
        assert len(sending) >= 2
        assert "realtime" in decision.reason

    def test_no_alive_paths(self, scheduler, monitor):
        for _ in range(10):
            monitor.record_loss(0)
            monitor.record_loss(1)
        scheduler.update_predictions()

        decision = scheduler.schedule(0, "bulk")
        assert len(decision.assignments) == 0

    def test_weighted_distribution(self, config, monitor):
        """Verify weighted mode distributes proportionally."""
        config["scheduler"]["mode"] = "weighted"
        predictor = PathPredictor(config)
        sched = PathScheduler(config, monitor, predictor)

        # Path 0 much better than path 1
        for _ in range(30):
            monitor.record_rtt(0, 20)
            monitor.record_rtt(1, 100)
        sched.update_predictions()

        counts = {0: 0, 1: 0}
        for i in range(1000):
            decision = sched.schedule(i, "bulk")
            for a in decision.assignments:
                if a.send:
                    counts[a.path_id] = counts.get(a.path_id, 0) + 1

        # Path 0 should get more traffic
        assert counts[0] > counts[1]

    def test_round_robin(self, config, monitor):
        config["scheduler"]["mode"] = "round_robin"
        predictor = PathPredictor(config)
        sched = PathScheduler(config, monitor, predictor)

        for _ in range(30):
            monitor.record_rtt(0, 30)
            monitor.record_rtt(1, 30)
        sched.update_predictions()

        paths = []
        for i in range(10):
            decision = sched.schedule(i, "bulk")
            for a in decision.assignments:
                if a.send:
                    paths.append(a.path_id)

        # Should alternate
        assert 0 in paths and 1 in paths

    def test_stats_tracking(self, scheduler, monitor):
        for _ in range(30):
            monitor.record_rtt(0, 30)
            monitor.record_rtt(1, 60)
        scheduler.update_predictions()

        for i in range(100):
            scheduler.schedule(i, "bulk")

        stats = scheduler.get_stats()
        assert stats.decisions_total == 100
        assert stats.avg_decision_time_us > 0

    def test_decision_speed(self, scheduler, monitor):
        """Decisions must complete in <100us."""
        for _ in range(30):
            monitor.record_rtt(0, 30)
            monitor.record_rtt(1, 60)
        scheduler.update_predictions()

        t0 = time.monotonic()
        for i in range(10000):
            scheduler.schedule(i, "bulk")
        elapsed = time.monotonic() - t0
        avg_us = (elapsed / 10000) * 1_000_000
        assert avg_us < 100, f"Too slow: {avg_us:.1f}us per decision"
