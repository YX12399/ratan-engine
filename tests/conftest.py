"""Shared test fixtures for HyperAgg tests."""
import os
import pytest
from hyperagg.tunnel.packet import set_connection_start


@pytest.fixture(autouse=True)
def _init_connection_time():
    """Ensure packet timestamps work in all tests."""
    set_connection_start()


@pytest.fixture
def sample_config():
    """Minimal config dict for tests that need one."""
    return {
        "vps": {"host": "127.0.0.1", "tunnel_port": 9999},
        "server": {"host": "0.0.0.0"},
        "tunnel": {"mtu": 1400, "sequence_window": 1024, "reorder_timeout_ms": 100},
        "fec": {"mode": "auto", "xor_group_size": 4, "rs_data_shards": 8, "rs_parity_shards": 2},
        "scheduler": {
            "mode": "ai", "history_window": 50, "ewma_alpha": 0.3,
            "latency_budget_ms": 150, "probe_interval_ms": 100,
            "jitter_weight": 0.3, "loss_weight": 0.5, "rtt_weight": 0.2,
        },
        "qos": {"enabled": False},
        "interfaces": {"wan_interfaces": []},
    }


@pytest.fixture
def encryption_key():
    """Generate a test encryption key."""
    return os.urandom(32)
