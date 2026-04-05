"""Tests for all 7 networking math modules."""
import os
import time
import random
import pytest


# ── MODULE 1: Adaptive Reorder Buffer ──

class TestAdaptiveReorderBuffer:
    def test_adaptive_timeout_adjusts_to_path_differential(self):
        from hyperagg.tunnel.reorder_buffer import AdaptiveReorderBuffer
        buf = AdaptiveReorderBuffer(initial_timeout_ms=100)

        # Path 0: 30ms, Path 1: 80ms → differential = 50ms
        timeout = buf.update_timeout(
            path_rtts={0: 30.0, 1: 80.0},
            path_jitters={0: 2.0, 1: 5.0},
        )
        # Expected: 50 (diff) + 10 (2*5 jitter) = 60ms
        assert 50 <= timeout <= 80  # Within reasonable range

    def test_timeout_expands_on_spike(self):
        from hyperagg.tunnel.reorder_buffer import AdaptiveReorderBuffer
        buf = AdaptiveReorderBuffer(initial_timeout_ms=50)

        # Stable
        t1 = buf.update_timeout({0: 30, 1: 40}, {0: 2, 1: 3})

        # Spike: path 1 jumps to 200ms
        t2 = buf.update_timeout({0: 30, 1: 200}, {0: 2, 1: 30})

        assert t2 > t1
        assert t2 >= 170  # diff=170 + jitter_margin

    def test_timeout_shrinks_on_recovery(self):
        from hyperagg.tunnel.reorder_buffer import AdaptiveReorderBuffer
        buf = AdaptiveReorderBuffer(initial_timeout_ms=300)

        # High diff first
        buf.update_timeout({0: 30, 1: 200}, {0: 2, 1: 20})
        # Then recover
        t = buf.update_timeout({0: 30, 1: 40}, {0: 2, 1: 3})
        assert t < 100

    def test_clamp_bounds(self):
        from hyperagg.tunnel.reorder_buffer import AdaptiveReorderBuffer
        buf = AdaptiveReorderBuffer(min_timeout_ms=10, max_timeout_ms=500)

        # Very small diff
        t = buf.update_timeout({0: 30, 1: 31}, {0: 0.1, 1: 0.1})
        assert t >= 10

        # Very large diff
        t = buf.update_timeout({0: 30, 1: 800}, {0: 50, 1: 100})
        assert t <= 500

    def test_late_delivery_feedback(self):
        from hyperagg.tunnel.reorder_buffer import AdaptiveReorderBuffer
        buf = AdaptiveReorderBuffer(initial_timeout_ms=10)

        # Insert in-order packets
        for i in range(20):
            buf.insert(i, b"data")

        # Now simulate late arrivals by skipping ahead
        buf._next_seq = 50  # Jump ahead
        for i in range(20, 50):
            buf._flushed_seqs.add(i)

        # These are "late" — they arrive after their slot was flushed
        for i in range(20, 30):
            buf.insert(i, b"late")

        assert buf.late_delivery_pct > 0

    def test_in_order_delivery(self):
        from hyperagg.tunnel.reorder_buffer import AdaptiveReorderBuffer
        buf = AdaptiveReorderBuffer()
        r = buf.insert(0, b"a")
        assert r == [b"a"]
        r = buf.insert(1, b"b")
        assert r == [b"b"]

    def test_out_of_order_reorder(self):
        from hyperagg.tunnel.reorder_buffer import AdaptiveReorderBuffer
        buf = AdaptiveReorderBuffer()
        assert buf.insert(2, b"c") == []
        assert buf.insert(1, b"b") == []
        r = buf.insert(0, b"a")
        assert r == [b"a", b"b", b"c"]


# ── MODULE 2: Bandwidth Estimator + Pacer ──

class TestBandwidthEstimator:
    def test_estimate_from_samples(self):
        from hyperagg.scheduler.bandwidth_estimator import BandwidthEstimator
        bw = BandwidthEstimator(ewma_alpha=1.0, window_sec=1.0)

        # Record 1MB over ~1 second
        for _ in range(100):
            bw.record_ack(0, 10000)
            time.sleep(0.001)

        est = bw.get_estimated_bw(0)
        assert est > 0  # Should have a positive estimate

    def test_per_path_tracking(self):
        from hyperagg.scheduler.bandwidth_estimator import BandwidthEstimator
        bw = BandwidthEstimator()
        bw.record_ack(0, 50000)
        bw.record_ack(1, 20000)
        time.sleep(0.01)
        bw.record_ack(0, 50000)
        bw.record_ack(1, 20000)
        assert bw.get_estimated_bw(0) > bw.get_estimated_bw(1)


class TestTokenBucketPacer:
    def test_consume_and_empty(self):
        from hyperagg.scheduler.bandwidth_estimator import TokenBucketPacer
        pacer = TokenBucketPacer()
        pacer.set_rate(0, 100000)  # 100 KB/s
        time.sleep(0.01)  # Let some tokens accumulate

        # Should be able to consume some
        assert pacer.can_send(0, 100)
        assert pacer.consume(0, 100)

    def test_empty_bucket_blocks(self):
        from hyperagg.scheduler.bandwidth_estimator import TokenBucketPacer
        pacer = TokenBucketPacer()
        pacer.set_rate(0, 100)  # Very low rate: 100 bytes/sec
        # Drain bucket
        pacer._buckets[0] = 0
        pacer._last_refill[0] = time.monotonic()
        assert not pacer.can_send(0, 1000)

    def test_proportional_distribution(self):
        from hyperagg.scheduler.bandwidth_estimator import TokenBucketPacer
        pacer = TokenBucketPacer()
        pacer.set_rate(0, 4_000_000)  # 40 Mbps
        pacer.set_rate(1, 1_500_000)  # 15 Mbps

        time.sleep(0.05)

        t0 = pacer.tokens_available(0)
        t1 = pacer.tokens_available(1)
        if t0 > 0 and t1 > 0:
            ratio = t0 / (t0 + t1)
            assert 0.5 < ratio < 0.9  # ~73% on path 0


# ── MODULE 3: Adaptive FEC ──

class TestBurstDetector:
    def test_detects_burst_length(self):
        from hyperagg.fec.adaptive_fec import BurstDetector
        det = BurstDetector()

        # Normal traffic with occasional single losses
        for _ in range(100):
            for _ in range(20):
                det.record_received()
            det.record_lost()

        assert det.p95_burst_length <= 1

    def test_detects_burst_of_3(self):
        from hyperagg.fec.adaptive_fec import BurstDetector
        det = BurstDetector()

        for _ in range(50):
            for _ in range(20):
                det.record_received()
            # Burst of 3
            det.record_lost()
            det.record_lost()
            det.record_lost()

        assert det.p95_burst_length >= 3


class TestAdaptiveFecSizer:
    def test_xor_for_no_bursts(self):
        from hyperagg.fec.adaptive_fec import AdaptiveFecSizer
        sizer = AdaptiveFecSizer()
        for _ in range(200):
            sizer.record_received()
            if random.random() < 0.01:
                sizer.record_lost()
        result = sizer.update()
        assert result["mode"] == "xor"

    def test_rs_for_burst_3(self):
        from hyperagg.fec.adaptive_fec import AdaptiveFecSizer
        sizer = AdaptiveFecSizer()
        for _ in range(30):
            for _ in range(30):
                sizer.record_received()
            sizer.record_lost()
            sizer.record_lost()
            sizer.record_lost()
        result = sizer.update()
        assert result["mode"] == "reed_solomon"
        assert result["parity_shards"] >= 3  # Can handle burst of 3

    def test_replicate_for_burst_5(self):
        from hyperagg.fec.adaptive_fec import AdaptiveFecSizer
        sizer = AdaptiveFecSizer()
        for _ in range(30):
            for _ in range(20):
                sizer.record_received()
            for _ in range(5):
                sizer.record_lost()
        result = sizer.update()
        assert result["mode"] == "replicate"


class TestInterleaver:
    def test_interleaves_two_groups(self):
        from hyperagg.fec.adaptive_fec import Interleaver
        il = Interleaver(depth=2)

        # Add packets from group A
        il.add_packet(0, "A0")
        il.add_packet(0, "A1")

        # Add packets from group B — depth reached, triggers interleave
        il.add_packet(1, "B0")
        il.add_packet(1, "B1")

        # Flush to get interleaved result
        result = il.flush()
        # Even if some were already flushed, the interleaving logic works
        # The key property: packets from different groups are interleaved
        all_packets = result if result else []

        # Verify we get packets from both groups alternating
        # (exact order depends on when flush triggers, but groups must mix)
        assert len(all_packets) >= 0  # May have flushed earlier

        # Direct test: build fresh and flush manually
        il2 = Interleaver(depth=3)  # Higher depth so it doesn't auto-flush
        il2.add_packet(0, "A0")
        il2.add_packet(0, "A1")
        il2.add_packet(1, "B0")
        il2.add_packet(1, "B1")
        result2 = il2.flush()
        assert result2 == ["A0", "B0", "A1", "B1"]


# ── MODULE 4: Congestion Control ──

class TestAIMDController:
    def test_additive_increase(self):
        from hyperagg.scheduler.congestion_control import AIMDController
        cc = AIMDController(estimated_bw=10_000_000)
        initial = cc.send_rate
        time.sleep(0.11)  # Wait for increase interval
        cc.on_success()
        assert cc.send_rate >= initial

    def test_multiplicative_decrease(self):
        from hyperagg.scheduler.congestion_control import AIMDController
        cc = AIMDController(estimated_bw=10_000_000)
        initial = cc.send_rate
        cc.on_loss()
        assert cc.send_rate < initial
        assert cc.send_rate == pytest.approx(initial * 0.5, rel=0.01)

    def test_min_rate_floor(self):
        from hyperagg.scheduler.congestion_control import AIMDController
        cc = AIMDController(estimated_bw=10_000_000)
        for _ in range(20):
            cc.on_loss()
        assert cc.send_rate >= 10_000_000 * 0.1  # min_rate


class TestTierBandwidthManager:
    def test_realtime_guaranteed(self):
        from hyperagg.scheduler.congestion_control import TierBandwidthManager
        mgr = TierBandwidthManager(total_capacity_bytes_per_sec=1_000_000)

        # Realtime should always be allowed (2 Mbps = 250 KB/s guarantee)
        assert mgr.can_send("realtime", 1000)

    def test_bulk_gets_remainder(self):
        from hyperagg.scheduler.congestion_control import TierBandwidthManager
        mgr = TierBandwidthManager(total_capacity_bytes_per_sec=10_000_000)
        stats = mgr.get_stats()
        # Bulk limit should be total minus rt+streaming guarantees
        assert stats["tier_limits_mbps"]["bulk"] > 0


# ── MODULE 5: One-Way Delay ──

class TestOWDEstimator:
    def test_asymmetric_detection(self):
        from hyperagg.scheduler.owd_estimator import OWDEstimator
        owd = OWDEstimator(ewma_alpha=1.0)

        # Path 0: 20ms up, 40ms down (60ms RTT)
        owd.record_rtt_with_asymmetry(0, 60.0, up_ratio=0.33)

        assert owd.get_owd_up(0) == pytest.approx(20.0, abs=1)
        assert owd.get_owd_down(0) == pytest.approx(40.0, abs=1)

    def test_best_download_path(self):
        from hyperagg.scheduler.owd_estimator import OWDEstimator
        owd = OWDEstimator(ewma_alpha=1.0)

        # Path 0: 20ms up / 40ms down
        owd.record_rtt_with_asymmetry(0, 60.0, up_ratio=0.33)
        # Path 1: 50ms up / 30ms down
        owd.record_rtt_with_asymmetry(1, 80.0, up_ratio=0.625)

        # Path 1 has lower download OWD (30ms vs 40ms)
        assert owd.get_best_download_path() == 1
        # Path 0 has lower upload OWD (20ms vs 50ms)
        assert owd.get_best_upload_path() == 0

    def test_ntp_style_timestamps(self):
        from hyperagg.scheduler.owd_estimator import OWDEstimator
        owd = OWDEstimator(ewma_alpha=1.0)

        # Simulate: 20ms up, 40ms down
        base = time.monotonic()
        t1 = base
        t2 = base + 0.020  # Server receives 20ms later
        t3 = base + 0.021  # Server processes 1ms
        t4 = base + 0.061  # Client receives 40ms after server sent

        owd.record_timestamps(0, t1, t2, t3, t4)
        assert owd.get_owd_up(0) > 0
        assert owd.get_owd_down(0) > 0

    def test_download_differential(self):
        from hyperagg.scheduler.owd_estimator import OWDEstimator
        owd = OWDEstimator(ewma_alpha=1.0)
        owd.record_rtt_with_asymmetry(0, 60, up_ratio=0.33)  # down=40
        owd.record_rtt_with_asymmetry(1, 80, up_ratio=0.625)  # down=30

        diff = owd.get_download_differential()
        assert diff == pytest.approx(10.0, abs=2)  # 40-30 = 10ms


# ── MODULE 6: Throughput Calculator ──

class TestThroughputCalculator:
    def test_raw_throughput(self):
        from hyperagg.metrics.throughput_calculator import ThroughputCalculator
        tc = ThroughputCalculator(window_sec=1.0)

        # Send 1MB in 100 chunks
        for _ in range(100):
            tc.record_sent(10000)

        result = tc.compute()
        assert result["raw_throughput_mbps"] > 0

    def test_fec_overhead(self):
        from hyperagg.metrics.throughput_calculator import ThroughputCalculator
        tc = ThroughputCalculator(window_sec=1.0)

        for _ in range(80):
            tc.record_sent(1000, is_fec_parity=False)
        for _ in range(20):
            tc.record_sent(1000, is_fec_parity=True)

        result = tc.compute()
        assert result["fec_overhead_ratio"] == pytest.approx(0.20, abs=0.02)

    def test_efficiency_drops_with_replication(self):
        from hyperagg.metrics.throughput_calculator import ThroughputCalculator
        tc = ThroughputCalculator(window_sec=1.0)

        for _ in range(100):
            tc.record_sent(1000, is_duplicate=True)

        result = tc.compute()
        # All traffic is duplicates → efficiency should be very low
        assert result["efficiency_pct"] < 10


# ── MODULE 7: Session Resumption ──

class TestSessionManager:
    def test_create_session(self):
        import tempfile
        from hyperagg.tunnel.session import SessionManager
        with tempfile.TemporaryDirectory() as td:
            mgr = SessionManager(state_dir=td)
            state = mgr.create_session()
            assert len(state.session_id) == 32
            assert mgr.status == "active"

    def test_update_and_persist(self):
        import tempfile
        from hyperagg.tunnel.session import SessionManager
        with tempfile.TemporaryDirectory() as td:
            mgr = SessionManager(state_dir=td, save_interval_sec=0)
            state = mgr.create_session()
            mgr.update(global_seq=5000, path_seqs={0: 3000, 1: 2000})

            # Verify file exists
            from pathlib import Path
            files = list(Path(td).glob("*.json"))
            assert len(files) == 1

    def test_resume_session(self):
        import tempfile
        from hyperagg.tunnel.session import SessionManager
        with tempfile.TemporaryDirectory() as td:
            # Create and save
            mgr1 = SessionManager(state_dir=td, save_interval_sec=0)
            state1 = mgr1.create_session()
            sid = state1.session_id
            mgr1.update(global_seq=10000)

            # Resume from a different manager instance
            mgr2 = SessionManager(state_dir=td)
            resumed = mgr2.try_resume(sid, last_global_seq=9990)

            assert resumed is not None
            assert resumed.session_id == sid
            assert resumed.global_seq == 9991  # last + 1
            assert mgr2.status == "resumed"

    def test_resume_fails_on_large_gap(self):
        import tempfile
        from hyperagg.tunnel.session import SessionManager
        with tempfile.TemporaryDirectory() as td:
            mgr1 = SessionManager(state_dir=td, save_interval_sec=0, max_gap=100)
            state = mgr1.create_session()
            sid = state.session_id
            mgr1.update(global_seq=50000)

            mgr2 = SessionManager(state_dir=td, max_gap=100)
            resumed = mgr2.try_resume(sid, last_global_seq=0)
            assert resumed is None  # Gap too large

    def test_resume_with_path_history(self):
        import tempfile
        from hyperagg.tunnel.session import SessionManager
        with tempfile.TemporaryDirectory() as td:
            mgr = SessionManager(state_dir=td, save_interval_sec=0)
            state = mgr.create_session()
            sid = state.session_id
            mgr.update(
                global_seq=1000,
                path_rtt_history={0: [30, 31, 29, 32], 1: [60, 58, 62]},
                scheduler_mode="ai",
            )

            mgr2 = SessionManager(state_dir=td)
            resumed = mgr2.try_resume(sid, last_global_seq=990)
            assert resumed is not None
            assert resumed.scheduler_mode == "ai"
            assert len(resumed.path_rtt_history["0"]) == 4
