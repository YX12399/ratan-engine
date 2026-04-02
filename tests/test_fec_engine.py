"""Tests for the unified FEC engine with auto-mode selection."""
import os
import pytest
from hyperagg.fec.fec_engine import FecEngine


@pytest.fixture
def engine():
    config = {
        "fec": {
            "mode": "auto",
            "xor_group_size": 4,
            "rs_data_shards": 8,
            "rs_parity_shards": 2,
        }
    }
    return FecEngine(config)


class TestAutoMode:
    def test_xor_at_low_loss(self, engine):
        engine.update_mode({0: 0.01, 1: 0.01})
        assert engine.current_mode == "xor"

    def test_rs_at_medium_loss(self, engine):
        engine.update_mode({0: 0.05, 1: 0.05})
        assert engine.current_mode == "reed_solomon"

    def test_rs_heavy_at_high_loss(self, engine):
        engine.update_mode({0: 0.12, 1: 0.12})
        assert engine.current_mode == "reed_solomon_heavy"

    def test_replicate_at_very_high_loss(self, engine):
        engine.update_mode({0: 0.20, 1: 0.20})
        assert engine.current_mode == "replicate"

    def test_mode_change_counter(self, engine):
        engine.update_mode({0: 0.01})
        engine.update_mode({0: 0.05})
        engine.update_mode({0: 0.20})
        assert engine._mode_changes >= 2


class TestOverhead:
    def test_xor_overhead(self, engine):
        engine._current_mode = "xor"
        assert engine.overhead_pct == 25.0  # 1/4

    def test_none_overhead(self, engine):
        engine._current_mode = "none"
        assert engine.overhead_pct == 0.0

    def test_replicate_overhead(self, engine):
        engine._current_mode = "replicate"
        assert engine.overhead_pct == 100.0


class TestFixedMode:
    def test_fixed_xor(self):
        config = {"fec": {"mode": "xor", "xor_group_size": 4}}
        engine = FecEngine(config)
        engine.update_mode({0: 0.50})  # Should NOT change
        assert engine.current_mode == "xor"

    def test_fixed_none(self):
        config = {"fec": {"mode": "none"}}
        engine = FecEngine(config)
        assert engine.current_mode == "none"


class TestEncodeXOR:
    def test_xor_encode_produces_parity(self, engine):
        engine._current_mode = "xor"
        results = []
        for _ in range(4):
            results.extend(engine.encode_packet(os.urandom(100), "bulk"))

        # 4 data + 1 parity = 5 packets
        assert len(results) == 5
        parity_packets = [r for r in results if r[1].get("is_parity")]
        assert len(parity_packets) == 1


class TestEncodeReplicate:
    def test_replicate_returns_single_with_flag(self, engine):
        engine._current_mode = "replicate"
        results = engine.encode_packet(b"test", "bulk")
        assert len(results) == 1
        assert results[0][1].get("replicate")


class TestTierOverride:
    def test_realtime_always_replicates(self, engine):
        engine._current_mode = "xor"
        results = engine.encode_packet(b"test", "realtime")
        assert results[0][1].get("replicate")


class TestStats:
    def test_stats_structure(self, engine):
        stats = engine.get_stats()
        assert "current_mode" in stats
        assert "total_recoveries" in stats
        assert "overhead_pct" in stats
