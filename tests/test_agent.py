"""Tests for Edge Agent + Device Registry."""
import pytest
import time


class TestDeviceRegistry:
    def test_provision_device(self):
        from hyperagg.dashboard.agent_ws import DeviceRegistry
        reg = DeviceRegistry()
        bundle = reg.provision_device("test-rpi")
        assert bundle["device_id"] == "test-rpi"
        assert len(bundle["encryption_key"]) > 20
        assert "config_yaml" in bundle

    def test_list_devices_after_provision(self):
        from hyperagg.dashboard.agent_ws import DeviceRegistry
        reg = DeviceRegistry()
        reg.provision_device("dev-1")
        reg.provision_device("dev-2")
        devices = reg.list_devices()
        assert len(devices) == 2
        ids = [d["device_id"] for d in devices]
        assert "dev-1" in ids
        assert "dev-2" in ids

    def test_device_not_connected_by_default(self):
        from hyperagg.dashboard.agent_ws import DeviceRegistry
        reg = DeviceRegistry()
        reg.provision_device("dev-1")
        devices = reg.list_devices()
        assert devices[0]["connected"] is False

    async def test_send_command_to_disconnected(self):
        from hyperagg.dashboard.agent_ws import DeviceRegistry
        reg = DeviceRegistry()
        reg.provision_device("dev-1")
        result = await reg.send_command("dev-1", "start")
        assert result["status"] == "error"
        assert "not connected" in result["detail"]

    async def test_send_command_unknown_device(self):
        from hyperagg.dashboard.agent_ws import DeviceRegistry
        reg = DeviceRegistry()
        result = await reg.send_command("nonexistent", "start")
        assert result["status"] == "error"


class TestEdgeAgent:
    def test_agent_init_without_config(self):
        from hyperagg.agent.edge_agent import EdgeAgent
        agent = EdgeAgent(config_path="/tmp/nonexistent.yaml")
        assert agent._device_id.startswith("edge-")
        assert agent._tunnel_status == "stopped"

    def test_agent_status(self):
        from hyperagg.agent.edge_agent import EdgeAgent
        agent = EdgeAgent(config_path="/tmp/nonexistent.yaml")
        status = agent.get_status()
        assert "device_id" in status
        assert "tunnel_status" in status
        assert "interfaces" in status
        assert status["tunnel_status"] == "stopped"

    def test_agent_save_config(self):
        import tempfile, os
        from hyperagg.agent.edge_agent import EdgeAgent
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("device_id: test\n")
            f.flush()
            agent = EdgeAgent(config_path=f.name)
            agent.save_config({"vps_host": "1.2.3.4"})
            # Reload and verify
            agent2 = EdgeAgent(config_path=f.name)
            assert agent2._vps_host == "1.2.3.4"
            os.unlink(f.name)

    def test_device_info_interfaces(self):
        from hyperagg.agent.edge_agent import DeviceInfo
        import shutil
        if not shutil.which("ip"):
            pytest.skip("ip command not available")
        ifaces = DeviceInfo.get_interfaces()
        assert isinstance(ifaces, list)

    def test_local_app_creation(self):
        from hyperagg.agent.edge_agent import EdgeAgent
        agent = EdgeAgent(config_path="/tmp/nonexistent.yaml")
        app = agent.create_local_app()
        assert app is not None

    async def test_start_tunnel_without_vps(self):
        from hyperagg.agent.edge_agent import EdgeAgent
        agent = EdgeAgent(config_path="/tmp/nonexistent.yaml")
        result = await agent.start_tunnel()
        assert result["status"] == "error"
        assert "VPS host not configured" in result["detail"]


class TestDashboardDeviceEndpoints:
    @pytest.fixture
    def app_with_registry(self, sample_config):
        from hyperagg.dashboard.api import create_dashboard_app
        from hyperagg.dashboard.agent_ws import DeviceRegistry
        from hyperagg.controller.sdn_controller import SDNController
        from hyperagg.telemetry.packet_logger import PacketLogger
        sdn = SDNController(sample_config, mode="server")
        pkt_log = PacketLogger()
        reg = DeviceRegistry()
        return create_dashboard_app(sdn, pkt_log, device_registry=reg), reg

    @pytest.fixture
    def client(self, app_with_registry):
        from starlette.testclient import TestClient
        app, _ = app_with_registry
        return TestClient(app)

    @pytest.fixture
    def registry(self, app_with_registry):
        _, reg = app_with_registry
        return reg

    def test_list_devices_empty(self, client):
        resp = client.get("/api/devices")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_provision_and_list(self, client):
        resp = client.post("/api/devices/provision", json={"device_name": "test-rpi"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["device_id"] == "test-rpi"

        resp = client.get("/api/devices")
        devices = resp.json()
        assert len(devices) == 1
        assert devices[0]["device_id"] == "test-rpi"

    def test_send_command_to_offline(self, client, registry):
        registry.provision_device("dev-1")
        resp = client.post("/api/devices/dev-1/command", json={"action": "start"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"

    def test_dashboard_has_devices_tab(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "data-tab=\"devices\"" in resp.text
        assert "page-devices" in resp.text
