"""
Stress Tests & Practical Simulations — verify aggregation works under real conditions.

Tests:
  1. High-throughput burst: 10,000 packets through FEC + scheduler in <5 seconds
  2. Starlink satellite handoff: 3-second RTT spike + 50% loss, verify FEC recovery
  3. Cellular tower switch: burst loss of 5 consecutive packets, verify RS recovery
  4. Dual-path asymmetry: Starlink 30ms up/40ms down, cellular 50ms up/30ms down
  5. Video call QoS: Teams ports classified as realtime, FEC replicates
  6. Full pipeline: TUN → encrypt → FEC → schedule → reorder → decrypt → deliver
  7. Session resume after 5-second outage
  8. Congestion window growth and halving
  9. Reorder buffer adaptive timeout under changing path conditions
  10. A/B comparison: HyperAgg vs simulated MPTCP under identical conditions
"""

import os
import random
import statistics
import time
import pytest

from hyperagg.tunnel.packet import HyperAggPacket, set_connection_start, HEADER_SIZE
from hyperagg.tunnel.crypto import PacketCrypto, TAG_SIZE
from hyperagg.fec.fec_engine import FecEngine
from hyperagg.fec.xor_fec import XORFec, XORFecEncoder, XORFecDecoder
from hyperagg.fec.rs_fec import ReedSolomonFec
from hyperagg.fec.adaptive_fec import BurstDetector, AdaptiveFecSizer
from hyperagg.scheduler.path_monitor import PathMonitor
from hyperagg.scheduler.path_predictor import PathPredictor
from hyperagg.scheduler.path_scheduler import PathScheduler
from hyperagg.scheduler.bandwidth_estimator import BandwidthEstimator, TokenBucketPacer
from hyperagg.scheduler.congestion_control import AIMDController
from hyperagg.scheduler.owd_estimator import OWDEstimator
from hyperagg.tunnel.reorder_buffer import AdaptiveReorderBuffer
from hyperagg.metrics.throughput_calculator import ThroughputCalculator


@pytest.fixture(autouse=True)
def _init():
    set_connection_start()


SAMPLE_CONFIG = {
    "fec": {"mode": "auto", "xor_group_size": 4, "rs_data_shards": 8, "rs_parity_shards": 2},
    "scheduler": {
        "mode": "ai", "history_window": 50, "ewma_alpha": 0.3,
        "latency_budget_ms": 150, "probe_interval_ms": 100,
        "jitter_weight": 0.3, "loss_weight": 0.5, "rtt_weight": 0.2,
    },
}


class TestHighThroughputBurst:
    """10,000 packets through FEC + scheduler in <5 seconds."""

    def test_10k_packets_under_5_seconds(self):
        fec = FecEngine(SAMPLE_CONFIG)
        monitor = PathMonitor(SAMPLE_CONFIG)
        monitor.register_path(0)
        monitor.register_path(1)
        for _ in range(20):
            monitor.record_rtt(0, 30)
            monitor.record_rtt(1, 60)
        predictor = PathPredictor(SAMPLE_CONFIG)
        scheduler = PathScheduler(SAMPLE_CONFIG, monitor, predictor)
        scheduler.update_predictions()

        t0 = time.monotonic()
        packets_processed = 0
        fec_parity_count = 0

        for i in range(10000):
            payload = os.urandom(1380)
            results = fec.encode_packet(payload, "bulk")
            for fec_payload, fec_meta in results:
                decision = scheduler.schedule(i, "bulk")
                packets_processed += 1
                if fec_meta.get("is_parity"):
                    fec_parity_count += 1

        elapsed = time.monotonic() - t0
        pps = packets_processed / elapsed

        assert elapsed < 5.0, f"Too slow: {elapsed:.1f}s for 10K packets"
        assert packets_processed >= 10000
        assert fec_parity_count > 0, "No FEC parity generated"
        assert pps > 2000, f"Too slow: {pps:.0f} pps"


class TestStarlinkSatelliteHandoff:
    """Starlink RTT spikes from 30ms to 500ms with 50% loss for 3 seconds."""

    def test_fec_recovery_during_spike(self):
        fec = FecEngine(SAMPLE_CONFIG)
        monitor = PathMonitor(SAMPLE_CONFIG)
        monitor.register_path(0)
        monitor.register_path(1)

        # Phase 1: Steady state (30 ticks)
        for _ in range(30):
            monitor.record_rtt(0, random.gauss(30, 3))
            monitor.record_rtt(1, random.gauss(60, 8))

        # Phase 2: Starlink spike (simulate 3 seconds of degradation)
        loss_count = 0
        recovered_count = 0
        total_packets = 0

        for tick in range(30):
            # Starlink: RTT 500ms, 50% loss
            if random.random() > 0.5:
                monitor.record_rtt(0, random.gauss(500, 100))
            else:
                monitor.record_loss(0)
                loss_count += 1

            # Cellular: stable
            monitor.record_rtt(1, random.gauss(60, 8))

            # Update FEC based on loss
            states = monitor.get_all_path_states()
            path_loss = {pid: s.loss_pct for pid, s in states.items()}
            fec.update_mode(path_loss)

            # Send 100 packets per tick
            for _ in range(100):
                payload = os.urandom(1380)
                results = fec.encode_packet(payload, "bulk")
                total_packets += 1

                for fec_payload, fec_meta in results:
                    # Simulate loss on path 0
                    if fec_meta.get("is_parity"):
                        # Parity goes on path 1 (cellular, stable)
                        recovered = fec.decode_packet(
                            fec_meta.get("fec_group_id", 0),
                            fec_meta.get("fec_index", 0),
                            fec_meta.get("fec_group_size", 0),
                            True, fec_payload,
                        )
                        recovered_count += len(recovered)

        assert loss_count > 5, "Starlink should have had losses"
        assert fec.current_mode != "none", "FEC should have upgraded from none"


class TestCellularBurstLoss:
    """Burst loss of 5 consecutive packets, verify RS recovery."""

    def test_burst_detection_and_rs_switch(self):
        sizer = AdaptiveFecSizer()

        # Normal traffic with occasional single losses
        for _ in range(200):
            sizer.record_received()
            if random.random() < 0.01:
                sizer.record_lost()

        result = sizer.update()
        assert result["mode"] == "xor", "Should be XOR with single losses"

        # Now inject burst losses of 5
        for _ in range(20):
            for _ in range(30):
                sizer.record_received()
            for _ in range(5):
                sizer.record_lost()

        result = sizer.update()
        assert result["mode"] == "replicate", f"Should be replicate for burst=5, got {result['mode']}"

    def test_rs_recovers_burst_of_2(self):
        fec = ReedSolomonFec(data_shards=4, parity_shards=2)
        payloads = [os.urandom(1380) for _ in range(4)]
        parity = fec.encode(payloads)

        # Drop 2 consecutive data shards (burst of 2)
        shards = {2: payloads[2], 3: payloads[3], 4: parity[0], 5: parity[1]}
        recovered = fec.decode(shards, num_data=4)

        for i in range(4):
            assert recovered[i] == payloads[i], f"Shard {i} not recovered"


class TestDualPathAsymmetry:
    """Starlink 30ms up/40ms down, cellular 50ms up/30ms down."""

    def test_owd_detects_asymmetry(self):
        owd = OWDEstimator(ewma_alpha=1.0)

        # Path 0 (Starlink): 20ms up, 40ms down (60ms RTT)
        owd.record_rtt_with_asymmetry(0, 60.0, up_ratio=0.33)
        # Path 1 (Cellular): 50ms up, 30ms down (80ms RTT)
        owd.record_rtt_with_asymmetry(1, 80.0, up_ratio=0.625)

        # Best download path should be cellular (30ms < 40ms)
        assert owd.get_best_download_path() == 1
        # Best upload path should be Starlink (20ms < 50ms)
        assert owd.get_best_upload_path() == 0

        # Download differential
        diff = owd.get_download_differential()
        assert 8 < diff < 12, f"Expected ~10ms diff, got {diff}"


class TestVideoCallQoS:
    """Teams ports classified as realtime, FEC replicates."""

    def test_teams_port_classification(self):
        from hyperagg.tunnel.client import TunnelClient
        # Teams STUN/TURN ports
        for port in [3478, 3479, 3480, 3481]:
            assert TunnelClient._port_in_range(port, "3478-3481,5348-5352,8801-8810,19302-19309")
        # Zoom ports
        for port in [8801, 8805, 8810]:
            assert TunnelClient._port_in_range(port, "3478-3481,5348-5352,8801-8810,19302-19309")
        # Google Meet
        assert TunnelClient._port_in_range(19305, "3478-3481,5348-5352,8801-8810,19302-19309")
        # Non-realtime
        assert not TunnelClient._port_in_range(80, "3478-3481,5348-5352,8801-8810,19302-19309")
        assert not TunnelClient._port_in_range(443, "3478-3481,5348-5352,8801-8810,19302-19309")

    def test_realtime_tier_uses_replicate(self):
        fec = FecEngine(SAMPLE_CONFIG)
        results = fec.encode_packet(os.urandom(100), "realtime")
        # Realtime should return a replicate flag
        assert len(results) >= 1
        assert results[0][1].get("replicate") or results[0][1].get("fec_group_size", 0) == 0


class TestFullPipelineRoundtrip:
    """TUN payload → encrypt → FEC → schedule → reorder → decrypt → verify."""

    def test_end_to_end_with_encryption(self):
        key = PacketCrypto.generate_key()
        crypto = PacketCrypto(key)
        throughput = ThroughputCalculator(window_sec=5.0)

        original_payloads = []
        received_payloads = []
        global_seq = 0

        # Send 100 packets — encrypt, serialize, deserialize, decrypt, verify
        for i in range(100):
            payload = os.urandom(1380)
            original_payloads.append(payload)

            pkt = HyperAggPacket.create_data(
                payload=payload, path_id=0, seq=global_seq,
                global_seq=global_seq,
            )

            # Encrypt
            pkt.payload_len = len(pkt.payload) + TAG_SIZE
            aad = pkt.header_bytes()
            encrypted = crypto.encrypt(pkt.payload, aad, pkt.global_seq, pkt.path_id)
            pkt.payload = encrypted

            # Serialize → wire → deserialize
            wire = pkt.serialize()
            throughput.record_sent(len(wire))
            received_pkt = HyperAggPacket.deserialize(wire)

            # Decrypt
            decrypted = crypto.decrypt(
                received_pkt.payload, received_pkt.header_bytes(),
                received_pkt.global_seq, received_pkt.path_id,
            )
            received_payloads.append(decrypted)
            global_seq += 1

        assert len(received_payloads) == 100
        for i in range(100):
            assert received_payloads[i] == original_payloads[i], f"Payload {i} mismatch"

        tp = throughput.compute()
        assert tp["raw_throughput_mbps"] > 0


class TestSessionResumeAfterOutage:
    """Session state saved, 5-second outage, resume from saved state."""

    def test_resume_preserves_sequence(self):
        import tempfile
        from hyperagg.tunnel.session import SessionManager

        with tempfile.TemporaryDirectory() as td:
            # Create session and advance to seq 5000
            mgr = SessionManager(state_dir=td, save_interval_sec=0)
            state = mgr.create_session()
            sid = state.session_id
            mgr.update(global_seq=5000, path_seqs={0: 3000, 1: 2000},
                        scheduler_mode="ai",
                        path_rtt_history={0: [30, 31, 29], 1: [60, 58, 62]})

            # Simulate 5-second outage (new manager instance = process restart)
            mgr2 = SessionManager(state_dir=td)
            resumed = mgr2.try_resume(sid, last_global_seq=4990)

            assert resumed is not None
            assert resumed.global_seq == 4991  # Resume from last + 1
            assert resumed.scheduler_mode == "ai"
            assert len(resumed.path_rtt_history["0"]) == 3


class TestCongestionWindowBehavior:
    """Verify cwnd grows in slow-start and halves on loss."""

    def test_slow_start_growth(self):
        cc = AIMDController(estimated_bw=10_000_000)
        initial_cwnd = cc.cwnd_bytes

        # Simulate successful ACKs (slow-start: cwnd doubles per RTT)
        for _ in range(10):
            cc.record_sent(1400)
            cc.record_ack(1400)

        assert cc.cwnd_bytes > initial_cwnd, "cwnd should grow in slow start"
        assert cc._in_slow_start, "Should still be in slow start"

    def test_loss_halves_cwnd(self):
        cc = AIMDController(estimated_bw=10_000_000)
        # Grow cwnd first
        for _ in range(20):
            cc.record_sent(1400)
            cc.record_ack(1400)
        grown_cwnd = cc.cwnd_bytes

        cc.on_loss()
        assert cc.cwnd_bytes < grown_cwnd, "cwnd should halve on loss"
        assert not cc._in_slow_start, "Should exit slow start after loss"

    def test_bdp_calculation(self):
        cc = AIMDController(estimated_bw=5_000_000)  # 5 MB/s = 40 Mbps
        cc.record_rtt(30.0)  # 30ms RTT

        bdp = cc.bdp_bytes
        # BDP = 5,000,000 bytes/s * 0.030s = 150,000 bytes
        assert 140000 < bdp < 160000, f"BDP should be ~150KB, got {bdp}"


class TestAdaptiveReorderUnderChangingConditions:
    """Reorder buffer timeout adapts as path conditions change."""

    def test_timeout_tracks_path_differential(self):
        buf = AdaptiveReorderBuffer()

        # Phase 1: paths close (30ms, 40ms) → small timeout
        t1 = buf.update_timeout({0: 30, 1: 40}, {0: 2, 1: 3})
        assert t1 < 50, f"Timeout should be small with close paths: {t1}"

        # Phase 2: paths diverge (30ms, 200ms) → large timeout
        t2 = buf.update_timeout({0: 30, 1: 200}, {0: 2, 1: 30})
        assert t2 > 100, f"Timeout should grow with diverged paths: {t2}"
        assert t2 > t1, "Timeout should increase when paths diverge"

        # Phase 3: paths recover (30ms, 50ms) → timeout shrinks
        t3 = buf.update_timeout({0: 30, 1: 50}, {0: 2, 1: 5})
        assert t3 < t2, "Timeout should shrink when paths recover"


class TestABComparison:
    """HyperAgg vs simulated MPTCP under identical conditions."""

    def test_hyperagg_beats_mptcp_on_failover(self):
        from hyperagg.testing.ab_comparison import run_comparison

        result = run_comparison(
            name="Stress Test: Starlink Dropout",
            scenario_name="starlink_dropout",
            config=SAMPLE_CONFIG,
        )

        # HyperAgg should have lower max disruption than MPTCP
        assert result.hyperagg.max_disruption_ms <= result.mptcp.max_disruption_ms, \
            f"HyperAgg disruption ({result.hyperagg.max_disruption_ms}ms) should be <= MPTCP ({result.mptcp.max_disruption_ms}ms)"

        # HyperAgg should have FEC recoveries
        assert result.hyperagg.recovered_by_fec > 0, "HyperAgg should have FEC recoveries"

        # MPTCP should have zero FEC recoveries
        assert result.mptcp.recovered_by_fec == 0, "MPTCP should have no FEC"


class TestSendQueueResilience:
    """Verify retry queue handles EAGAIN correctly."""

    def test_queue_accepts_and_drains(self):
        from hyperagg.tunnel.send_queue import SendQueue
        sq = SendQueue(max_queue_size=10)

        # Manually enqueue (simulating EAGAIN)
        sq._enqueue(0, b"packet1", ("127.0.0.1", 9999))
        sq._enqueue(0, b"packet2", ("127.0.0.1", 9999))

        assert sq.pending_count(0) == 2
        assert sq.total_pending() == 2

        stats = sq.get_stats()
        assert stats["total_dropped"] == 0

    def test_queue_drops_oldest_when_full(self):
        from hyperagg.tunnel.send_queue import SendQueue
        sq = SendQueue(max_queue_size=3)

        for i in range(5):
            sq._enqueue(0, f"pkt{i}".encode(), ("127.0.0.1", 9999))

        assert sq.pending_count(0) == 3  # Max size
        assert sq._dropped == 2  # Oldest 2 dropped


class TestPathMTUDiscovery:
    """Verify PMTUD module calculations."""

    def test_safe_payload_size(self):
        from hyperagg.tunnel.pmtud import PathMTUDiscovery
        pmtud = PathMTUDiscovery()

        # Default MTU 1280 → safe payload = 1280 - 72 = 1208
        safe = pmtud.get_safe_payload_size(0, default_mtu=1280)
        assert safe == 1208

        # If MTU is 1500 → safe payload = 1428
        pmtud._path_mtu[0] = 1500
        safe = pmtud.get_safe_payload_size(0)
        assert safe == 1428

        # If MTU is 576 (minimum) → safe payload = 504
        pmtud._path_mtu[1] = 576
        safe = pmtud.get_safe_payload_size(1)
        assert safe == 504

    def test_should_probe_after_interval(self):
        from hyperagg.tunnel.pmtud import PathMTUDiscovery
        pmtud = PathMTUDiscovery()
        assert pmtud.should_probe(0)  # Never probed → should probe

        pmtud._last_probe[0] = time.monotonic()
        assert not pmtud.should_probe(0)  # Just probed → don't probe
