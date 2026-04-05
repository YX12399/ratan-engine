"""Integration test: verify all 7 modules work together in the TunnelClient."""
import os
import random
import time
import pytest

from hyperagg.tunnel.client import TunnelClient
from hyperagg.tunnel.packet import set_connection_start


@pytest.fixture
def integrated_client():
    """Create a TunnelClient with all 7 modules wired in."""
    set_connection_start()
    config = {
        "vps": {"host": "127.0.0.1", "tunnel_port": 9999},
        "tunnel": {"mtu": 1400, "sequence_window": 1024, "reorder_timeout_ms": 100},
        "fec": {"mode": "auto", "xor_group_size": 4, "rs_data_shards": 8, "rs_parity_shards": 2},
        "scheduler": {
            "mode": "ai", "history_window": 50, "ewma_alpha": 0.3,
            "latency_budget_ms": 150, "probe_interval_ms": 100,
            "jitter_weight": 0.3, "loss_weight": 0.5, "rtt_weight": 0.2,
        },
        "qos": {"enabled": False},
    }
    client = TunnelClient(config)
    # Register two paths (don't create real sockets — just register monitors)
    client._monitor.register_path(0)
    client._monitor.register_path(1)
    # Create AIMD controllers for both paths (normally done in add_path)
    from hyperagg.scheduler.congestion_control import AIMDController
    client._congestion[0] = AIMDController(estimated_bw=5_000_000)
    client._congestion[1] = AIMDController(estimated_bw=2_000_000)
    client._pacer.set_rate(0, 5_000_000)
    client._pacer.set_rate(1, 2_000_000)
    return client


class TestAllModulesIntegrated:
    def test_adaptive_reorder_buffer_wired(self, integrated_client):
        """Module 1: AdaptiveReorderBuffer replaces old fixed-timeout buffer."""
        from hyperagg.tunnel.reorder_buffer import AdaptiveReorderBuffer
        assert isinstance(integrated_client._reorder, AdaptiveReorderBuffer)

        # Feed path measurements
        for _ in range(20):
            integrated_client._monitor.record_rtt(0, random.gauss(30, 3))
            integrated_client._monitor.record_rtt(1, random.gauss(80, 10))

        # Update timeout from real path metrics
        states = integrated_client._monitor.get_all_path_states()
        rtts = {pid: s.avg_rtt_ms for pid, s in states.items()}
        jitters = {pid: s.jitter_ms for pid, s in states.items()}
        t = integrated_client._reorder.update_timeout(rtts, jitters)

        # Should be ~50ms (diff) + jitter margin, not fixed 100ms
        assert t > 20  # Path differential exists
        assert t != 100  # Not the fixed default

    def test_bandwidth_estimator_wired(self, integrated_client):
        """Module 2: BandwidthEstimator tracks per-path bandwidth."""
        from hyperagg.scheduler.bandwidth_estimator import BandwidthEstimator
        assert isinstance(integrated_client._bw_estimator, BandwidthEstimator)

        # Simulate ACKs
        for _ in range(50):
            integrated_client._bw_estimator.record_ack(0, 1400)
            integrated_client._bw_estimator.record_ack(1, 1400)
            time.sleep(0.001)

        bw0 = integrated_client._bw_estimator.get_estimated_bw(0)
        bw1 = integrated_client._bw_estimator.get_estimated_bw(1)
        assert bw0 > 0
        assert bw1 > 0

    def test_pacer_wired(self, integrated_client):
        """Module 2: TokenBucketPacer controls send rate."""
        from hyperagg.scheduler.bandwidth_estimator import TokenBucketPacer
        assert isinstance(integrated_client._pacer, TokenBucketPacer)

        # Pacer should have rates set
        time.sleep(0.01)  # Let tokens accumulate
        assert integrated_client._pacer.can_send(0, 1400)

    def test_adaptive_fec_wired(self, integrated_client):
        """Module 3: AdaptiveFecSizer integrated into FecEngine."""
        from hyperagg.fec.adaptive_fec import AdaptiveFecSizer
        assert isinstance(integrated_client._fec._adaptive_sizer, AdaptiveFecSizer)

        # Record some losses to trigger burst detection
        for _ in range(100):
            integrated_client._fec.record_received()
        # Burst of 3
        for _ in range(3):
            integrated_client._fec.record_lost()
        for _ in range(50):
            integrated_client._fec.record_received()

        stats = integrated_client._fec.get_adaptive_stats()
        assert "p95_burst_length" in stats["burst_stats"]
        assert stats["burst_stats"]["window_size"] > 0

    def test_aimd_congestion_control_wired(self, integrated_client):
        """Module 4: AIMD controllers exist per path."""
        from hyperagg.scheduler.congestion_control import AIMDController
        assert 0 in integrated_client._congestion
        assert 1 in integrated_client._congestion
        assert isinstance(integrated_client._congestion[0], AIMDController)

        # Test AIMD decrease on loss
        initial_rate = integrated_client._congestion[0].send_rate
        integrated_client._congestion[0].on_loss()
        assert integrated_client._congestion[0].send_rate < initial_rate

    def test_tier_manager_wired(self, integrated_client):
        """Module 4: TierBandwidthManager manages per-tier budgets."""
        from hyperagg.scheduler.congestion_control import TierBandwidthManager
        assert isinstance(integrated_client._tier_mgr, TierBandwidthManager)

        # Realtime should always be allowed
        assert integrated_client._tier_mgr.can_send("realtime", 1000)

    def test_owd_estimator_wired(self, integrated_client):
        """Module 5: OWDEstimator tracks asymmetric delays."""
        from hyperagg.scheduler.owd_estimator import OWDEstimator
        assert isinstance(integrated_client._owd, OWDEstimator)

        # Feed asymmetric delay data
        integrated_client._owd.record_rtt_with_asymmetry(0, 60, up_ratio=0.33)
        integrated_client._owd.record_rtt_with_asymmetry(1, 80, up_ratio=0.625)

        owd_up_0 = integrated_client._owd.get_owd_up(0)
        owd_down_0 = integrated_client._owd.get_owd_down(0)
        assert owd_up_0 > 0
        assert owd_down_0 > owd_up_0  # Download slower for Starlink

        # Best download path should be path 1 (30ms down vs 40ms down)
        assert integrated_client._owd.get_best_download_path() == 1

    def test_throughput_calculator_wired(self, integrated_client):
        """Module 6: ThroughputCalculator tracks effective throughput."""
        from hyperagg.metrics.throughput_calculator import ThroughputCalculator
        assert isinstance(integrated_client._throughput, ThroughputCalculator)

        # Record some sends
        for _ in range(50):
            integrated_client._throughput.record_sent(1400)
        for _ in range(10):
            integrated_client._throughput.record_sent(1400, is_fec_parity=True)

        result = integrated_client._throughput.compute()
        assert result["raw_throughput_mbps"] > 0
        assert result["fec_overhead_ratio"] > 0  # Some FEC overhead
        assert result["efficiency_pct"] < 100  # Not 100% efficient due to FEC

    def test_session_manager_wired(self, integrated_client):
        """Module 7: SessionManager persists state for reconnect."""
        from hyperagg.tunnel.session import SessionManager
        assert isinstance(integrated_client._session_mgr, SessionManager)

        # Create a session
        state = integrated_client._session_mgr.create_session()
        assert len(state.session_id) == 32
        assert integrated_client._session_mgr.status == "active"

        # Update with sequence data
        integrated_client._session_mgr.update(
            global_seq=5000,
            path_seqs={0: 3000, 1: 2000},
            scheduler_mode="ai",
        )
        stats = integrated_client._session_mgr.get_stats()
        assert stats["global_seq"] == 5000

    def test_get_metrics_includes_all_modules(self, integrated_client):
        """All module stats appear in get_metrics()."""
        # Feed some data so metrics are non-empty
        for _ in range(20):
            integrated_client._monitor.record_rtt(0, 30)
            integrated_client._monitor.record_rtt(1, 60)
        integrated_client._throughput.record_sent(1400)
        integrated_client._session_mgr.create_session()

        metrics = integrated_client.get_metrics()

        # Original metrics
        assert "packets_sent_per_path" in metrics
        assert "fec_mode" in metrics

        # Module 1
        assert "reorder" in metrics
        assert "timeout_ms" in metrics["reorder"]

        # Module 2
        assert "bandwidth" in metrics
        assert "pacer" in metrics

        # Module 4
        assert "congestion" in metrics
        assert "tier_manager" in metrics

        # Module 5
        assert "owd" in metrics

        # Module 6
        assert "throughput" in metrics
        assert "efficiency_pct" in metrics["throughput"]

        # Module 7
        assert "session" in metrics
        assert "status" in metrics["session"]

        # Module 3
        assert "adaptive_fec" in metrics

    def test_reorder_buffer_uses_owd_when_available(self, integrated_client):
        """Reorder buffer prefers OWD download differential over RTT."""
        # Feed OWD data for both paths
        integrated_client._owd.record_rtt_with_asymmetry(0, 60, up_ratio=0.33)  # down=40
        integrated_client._owd.record_rtt_with_asymmetry(1, 80, up_ratio=0.625)  # down=30

        owd_data = integrated_client._owd.get_all_owd()
        assert len(owd_data) >= 2

        # The download differential should be ~10ms (40-30)
        diff = integrated_client._owd.get_download_differential()
        assert diff > 0
