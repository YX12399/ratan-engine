"""Tests for SDN controller, network manager, QoS engine, and dashboard API."""
import asyncio
import json
import pytest

from hyperagg.controller.sdn_controller import SDNController
from hyperagg.controller.network_manager import NetworkManager, InterfaceInfo
from hyperagg.controller.qos_engine import QoSEngine
from hyperagg.telemetry.packet_logger import PacketLogger


class TestQoSEngine:
    def test_classify_bulk_by_default(self, sample_config):
        qos = QoSEngine(sample_config)
        assert qos.classify(b"\x00" * 20) == "bulk"

    def test_classify_realtime_udp(self):
        import struct
        config = {
            "qos": {
                "enabled": True,
                "tiers": {"realtime": {"ports": "3478-3481", "fec_mode": "replicate"}},
            }
        }
        qos = QoSEngine(config)
        # Build fake UDP packet to port 3479
        ip = bytearray(20)
        ip[0] = 0x45  # IPv4, IHL=5
        ip[9] = 17    # UDP
        udp = struct.pack("!HH", 12345, 3479)
        pkt = bytes(ip) + udp + b"\x00" * 4
        assert qos.classify(pkt) == "realtime"

    def test_get_fec_mode(self):
        config = {
            "qos": {
                "enabled": True,
                "tiers": {"streaming": {"ports": "5000-5010", "fec_mode": "reed_solomon"}},
            }
        }
        qos = QoSEngine(config)
        assert qos.get_fec_mode("streaming") == "reed_solomon"
        assert qos.get_fec_mode("unknown") == "xor"  # default

    def test_get_tier_info(self):
        config = {
            "qos": {
                "enabled": True,
                "tiers": {
                    "realtime": {"ports": "3478-3481", "fec_mode": "replicate", "bandwidth_pct": 60},
                },
            }
        }
        qos = QoSEngine(config)
        info = qos.get_tier_info()
        assert "realtime" in info
        assert info["realtime"]["fec_mode"] == "replicate"


class TestNetworkManager:
    def test_discover_interfaces_runs(self, sample_config):
        import shutil
        if not shutil.which("ip"):
            pytest.skip("'ip' command not available in this environment")
        nm = NetworkManager(sample_config)
        ifaces = nm.discover_interfaces()
        assert isinstance(ifaces, list)

    def test_get_all_interfaces_empty_initially(self, sample_config):
        nm = NetworkManager(sample_config)
        assert nm.get_all_interfaces() == {}


class TestSDNController:
    def test_init(self, sample_config):
        sdn = SDNController(sample_config, mode="client")
        assert sdn._mode == "client"
        assert sdn._running is False

    def test_get_system_state_without_tunnel(self, sample_config):
        sdn = SDNController(sample_config, mode="client")
        state = sdn.get_system_state()
        assert state["mode"] == "client"
        assert "running" in state

    def test_events(self, sample_config):
        sdn = SDNController(sample_config)
        sdn._emit_event("test", "hello")
        events = sdn.get_events()
        assert len(events) == 1
        assert events[0]["category"] == "test"
        assert events[0]["message"] == "hello"

    def test_set_fec_mode(self, sample_config):
        from hyperagg.tunnel.client import TunnelClient
        sdn = SDNController(sample_config, mode="client")
        client = TunnelClient(sample_config)
        sdn.set_tunnel(client)
        sdn.set_fec_mode("reed_solomon")
        assert client._fec._mode_setting == "reed_solomon"


class TestPacketLogger:
    def test_log_and_retrieve(self):
        pl = PacketLogger()
        pl.log(1, 0, "data", 1400)
        pl.log(2, 1, "fec_parity", 1400)
        recent = pl.get_recent(10)
        assert len(recent) == 2
        assert recent[0]["global_seq"] == 1
        assert recent[1]["packet_type"] == "fec_parity"

    def test_stats(self):
        pl = PacketLogger()
        for i in range(10):
            pl.log(i, 0, "data", 1400)
        pl.log(10, 0, "recovered", 1400)
        stats = pl.get_stats()
        assert stats["data_packets"] == 10
        assert stats["recovered_packets"] == 1
        assert stats["total_logged"] == 11

    def test_csv_export(self):
        pl = PacketLogger()
        pl.log(1, 0, "data", 100)
        csv = pl.export_csv()
        assert "global_seq" in csv  # header
        assert "data" in csv        # row

    def test_ring_buffer(self):
        pl = PacketLogger(max_entries=5)
        for i in range(10):
            pl.log(i, 0, "data", 100)
        assert pl._total_logged == 10
        assert len(pl._log) == 5  # ring buffer kept only 5


class TestChatHandler:
    def test_mock_response_without_api_key(self, sample_config):
        from hyperagg.ai.chat_handler import ChatHandler
        sdn = SDNController(sample_config, mode="client")
        handler = ChatHandler(sdn, api_key="")  # No API key
        assert not handler.is_enabled

    async def test_mock_chat(self, sample_config):
        from hyperagg.ai.chat_handler import ChatHandler
        sdn = SDNController(sample_config, mode="client")
        handler = ChatHandler(sdn, api_key="")
        result = await handler.chat("How are my paths?")
        assert "analysis" in result
        assert "suggested_changes" in result
        # All suggested_changes should have endpoint+body format
        for change in result["suggested_changes"]:
            assert "endpoint" in change
            assert "body" in change
            assert "description" in change

    async def test_mock_generates_suggestions_for_bad_path(self, sample_config):
        """When a path has low score, mock should suggest forcing to the good path."""
        from hyperagg.ai.chat_handler import ChatHandler
        sdn = SDNController(sample_config, mode="client")
        # Simulate a degraded state
        sdn._state = sdn.get_system_state()
        sdn._state["paths"] = {
            0: {"avg_rtt_ms": 30, "loss_pct": 1, "quality_score": 0.95, "is_alive": True,
                "prediction": {"trend": "stable", "recommended_action": "prefer"}},
            1: {"avg_rtt_ms": 300, "loss_pct": 15, "quality_score": 0.2, "is_alive": True,
                "prediction": {"trend": "degrading", "recommended_action": "avoid"}},
        }
        sdn._state["metrics"] = {"fec_mode": "xor", "effective_loss_pct": 5,
                                   "aggregate_throughput_mbps": 10, "scheduler_mode": "ai",
                                   "packets_sent_per_path": {}}
        sdn._state["fec"] = {"current_mode": "xor", "total_recoveries": 5}
        sdn._state["scheduler"] = {"mode": "ai"}
        sdn.get_system_state = lambda: sdn._state
        handler = ChatHandler(sdn, api_key="")
        result = await handler.chat("What's wrong?")
        assert len(result["suggested_changes"]) > 0
        endpoints = [c["endpoint"] for c in result["suggested_changes"]]
        assert any("/api/" in e for e in endpoints)

    def test_normalize_action_format(self):
        from hyperagg.ai.chat_handler import ChatHandler
        changes = [{"action": "set_scheduler_mode", "value": "weighted", "reason": "test"}]
        normalized = ChatHandler._normalize_changes(changes)
        assert len(normalized) == 1
        assert normalized[0]["endpoint"] == "/api/scheduler/mode"
        assert normalized[0]["body"] == {"mode": "weighted"}

    def test_history(self, sample_config):
        from hyperagg.ai.chat_handler import ChatHandler
        sdn = SDNController(sample_config, mode="client")
        handler = ChatHandler(sdn, api_key="")
        assert handler.get_history() == []


class TestTestRunner:
    async def test_start_and_stop(self, sample_config):
        from hyperagg.testing.test_runner import TestRunner
        sdn = SDNController(sample_config, mode="client")
        runner = TestRunner(sdn)
        session = await runner.start_test("test1", duration_minutes=0.01)
        assert session.status == "running"
        assert runner.get_active() is not None
        result = await runner.stop_test()
        assert result.status == "stopped"
        assert runner.get_active() is None

    async def test_ab_test(self, sample_config):
        from hyperagg.testing.test_runner import TestRunner
        sdn = SDNController(sample_config, mode="client")
        runner = TestRunner(sdn)
        session = await runner.start_ab_test(
            "ab1", duration_minutes=0.02,
            variant_a={"scheduler_mode": "ai"},
            variant_b={"scheduler_mode": "weighted"},
        )
        assert session.test_type == "ab"
        assert session.status == "running"
        result = await runner.stop_test()
        assert result.test_type == "ab"

    def test_list_tests_empty(self, sample_config):
        from hyperagg.testing.test_runner import TestRunner
        sdn = SDNController(sample_config, mode="client")
        runner = TestRunner(sdn)
        tests = runner.list_tests()
        assert isinstance(tests, list)


class TestDashboardAPI:
    @pytest.fixture
    def app(self, sample_config):
        from hyperagg.dashboard.api import create_dashboard_app
        from hyperagg.ai.chat_handler import ChatHandler
        from hyperagg.testing.test_runner import TestRunner
        sdn = SDNController(sample_config, mode="client")
        sdn._emit_event("test", "startup")
        pkt_log = PacketLogger()
        pkt_log.log(1, 0, "data", 1400)
        ai = ChatHandler(sdn, api_key="")
        tests = TestRunner(sdn)
        return create_dashboard_app(sdn, pkt_log, ai, tests)

    @pytest.fixture
    def client(self, app):
        from starlette.testclient import TestClient
        return TestClient(app)

    def test_health(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["engine"] == "hyperagg"

    def test_state(self, client):
        resp = client.get("/api/state")
        assert resp.status_code == 200
        data = resp.json()
        assert "mode" in data

    def test_events(self, client):
        resp = client.get("/api/events")
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) >= 1

    def test_packets(self, client):
        resp = client.get("/api/packets")
        assert resp.status_code == 200
        pkts = resp.json()
        assert len(pkts) >= 1

    def test_packets_csv(self, client):
        resp = client.get("/api/packets/csv")
        assert resp.status_code == 200
        assert "global_seq" in resp.text

    def test_set_scheduler_mode(self, client):
        resp = client.post("/api/scheduler/mode", json={"mode": "weighted"})
        assert resp.status_code == 200

    def test_set_fec_mode(self, client):
        resp = client.post("/api/fec/mode", json={"mode": "xor"})
        assert resp.status_code == 200

    def test_dashboard_html_served(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "HyperAgg" in resp.text

    def test_ai_chat(self, client):
        resp = client.post("/api/ai/chat", json={"message": "How are my paths?"})
        assert resp.status_code == 200
        data = resp.json()
        assert "analysis" in data
        assert "suggested_changes" in data

    def test_ai_status(self, client):
        resp = client.get("/api/ai/status")
        assert resp.status_code == 200
        assert "enabled" in resp.json()

    def test_ai_history(self, client):
        resp = client.get("/api/ai/history")
        assert resp.status_code == 200

    def test_tests_list(self, client):
        resp = client.get("/api/tests/list")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_tests_active_none(self, client):
        resp = client.get("/api/tests/active")
        assert resp.status_code == 200

    def test_tests_start_and_stop(self, client):
        resp = client.post("/api/tests/start", json={"name": "test1", "duration_minutes": 0.01})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "started"

        resp = client.post("/api/tests/stop")
        assert resp.status_code == 200
