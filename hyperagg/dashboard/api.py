"""
Dashboard API — FastAPI routes + WebSocket for real-time metrics.

Endpoints:
  GET  /api/health          — System health check
  GET  /api/state           — Complete system state snapshot
  GET  /api/paths           — Per-path state with predictions
  GET  /api/fec             — FEC stats and mode
  GET  /api/scheduler       — Scheduler stats and decisions
  GET  /api/events          — Recent event log
  GET  /api/packets         — Recent packet log
  GET  /api/packets/csv     — Export packet log as CSV
  POST /api/scheduler/mode  — Set scheduler mode
  POST /api/fec/mode        — Set FEC mode
  POST /api/force-path      — Force traffic to one path
  POST /api/release-path    — Release path forcing
  POST /api/ai/chat         — AI chat analysis
  GET  /api/ai/history      — AI chat history
  POST /api/tests/start     — Start a test session
  GET  /api/tests/active    — Current running test
  POST /api/tests/stop      — Stop active test
  GET  /api/tests/list      — All completed tests
  GET  /api/tests/{id}      — Full test data
  WS   /ws                  — WebSocket for live updates (1Hz)
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

logger = logging.getLogger("hyperagg.dashboard.api")


class ModeRequest(BaseModel):
    mode: str


class ForcePathRequest(BaseModel):
    path_id: int


class ChatRequest(BaseModel):
    message: str


class TestStartRequest(BaseModel):
    name: str
    duration_minutes: float = 1.0
    duration_sec: Optional[float] = None  # Dashboard sends seconds — auto-convert
    description: str = ""


class ABTestRequest(BaseModel):
    name: str
    duration_minutes: float = 1.0
    duration_sec: Optional[float] = None
    variant_a: dict = {}
    variant_b: dict = {}
    description: str = ""


class ImpairRequest(BaseModel):
    path_id: int
    action: str  # latency, loss, down, clear
    value_ms: Optional[int] = None
    value_pct: Optional[int] = None


class TrafficRequest(BaseModel):
    mode: str = "mixed"  # bulk, realtime, mixed
    duration_sec: int = 60
    bitrate_kbps: int = 2000


def create_dashboard_app(
    sdn_controller,
    packet_logger=None,
    chat_handler=None,
    test_runner=None,
    preflight_checker=None,
    impairment_controller=None,
    traffic_generator=None,
    device_registry=None,
) -> FastAPI:
    """Create the FastAPI app with all dashboard routes."""

    app = FastAPI(
        title="RATAN HyperAgg Dashboard",
        description="Live aggregation monitoring and control",
        version="2.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    ctrl = sdn_controller
    pkt_log = packet_logger
    ai = chat_handler
    tests = test_runner

    # ── Static files (dashboard HTML) ──

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_root():
        html_path = Path(__file__).parent / "static" / "index.html"
        if html_path.exists():
            return HTMLResponse(html_path.read_text())
        return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)

    # ── REST API ──

    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "timestamp": time.time(),
            "engine": "hyperagg",
            "version": "2.0.0",
            "ai_enabled": ai.is_enabled if ai else False,
        }

    @app.get("/api/state")
    async def system_state():
        """Full state snapshot — same data as WebSocket for fallback polling."""
        state = ctrl.get_system_state()
        if tests:
            state["active_test"] = tests.get_active()
        if pkt_log:
            state["recent_packets"] = pkt_log.get_recent(20)
            state["packet_stats"] = pkt_log.get_stats()
        state["events"] = ctrl.get_events(10)
        state["ai_enabled"] = ai.is_enabled if ai else False
        if dev_reg:
            state["devices"] = dev_reg.list_devices()
        return state

    @app.get("/api/paths")
    async def paths():
        state = ctrl.get_system_state()
        return state.get("paths", {})

    @app.get("/api/fec")
    async def fec_stats():
        state = ctrl.get_system_state()
        return state.get("fec", {})

    @app.get("/api/scheduler")
    async def scheduler_stats():
        state = ctrl.get_system_state()
        return state.get("scheduler", {})

    @app.get("/api/events")
    async def events(last_n: int = Query(default=50, le=500)):
        return ctrl.get_events(last_n)

    @app.get("/api/packets")
    async def packets(last_n: int = Query(default=100, le=1000)):
        if pkt_log:
            return pkt_log.get_recent(last_n)
        return []

    @app.get("/api/packets/stats")
    async def packet_stats():
        if pkt_log:
            return pkt_log.get_stats()
        return {}

    @app.get("/api/packets/csv", response_class=PlainTextResponse)
    async def packets_csv():
        if pkt_log:
            return PlainTextResponse(
                pkt_log.export_csv(),
                media_type="text/csv",
                headers={"Content-Disposition": "attachment; filename=hyperagg_packets.csv"},
            )
        return PlainTextResponse("No packet log available")

    # ── Control endpoints ──

    @app.post("/api/scheduler/mode")
    async def set_scheduler_mode(req: ModeRequest):
        ctrl.set_scheduler_mode(req.mode)
        return {"status": "ok", "mode": req.mode}

    @app.post("/api/fec/mode")
    async def set_fec_mode(req: ModeRequest):
        ctrl.set_fec_mode(req.mode)
        return {"status": "ok", "mode": req.mode}

    @app.post("/api/force-path")
    async def force_path(req: ForcePathRequest):
        ctrl.force_path(req.path_id)
        return {"status": "ok", "forced_path": req.path_id}

    @app.post("/api/release-path")
    async def release_path():
        ctrl.release_path()
        return {"status": "ok"}

    # ── AI Chat ──

    @app.post("/api/ai/chat")
    async def ai_chat(req: ChatRequest):
        if not ai:
            return {"analysis": "AI chat not initialized", "suggested_changes": []}
        if not req.message or not req.message.strip():
            return {"analysis": "Please type a question about your network.", "suggested_changes": []}
        return await ai.chat(req.message.strip())

    @app.get("/api/ai/history")
    async def ai_history(last_n: int = Query(default=20, le=100)):
        if not ai:
            return []
        return ai.get_history(last_n)

    @app.get("/api/ai/status")
    async def ai_status():
        return {"enabled": ai.is_enabled if ai else False}

    # ── Test Sessions ──

    @app.post("/api/tests/start")
    async def test_start(req: TestStartRequest):
        if not tests:
            return {"error": "Test runner not initialized"}
        # Handle dashboard sending duration_sec
        dur = req.duration_minutes
        if req.duration_sec and req.duration_sec > 0:
            dur = req.duration_sec / 60.0
        try:
            session = await tests.start_test(req.name, dur, req.description)
            return {"status": "started", "id": session.id, "name": session.name}
        except RuntimeError as e:
            return {"error": str(e)}

    @app.post("/api/tests/ab")
    async def test_ab_start(req: ABTestRequest):
        if not tests:
            return {"error": "Test runner not initialized"}
        dur = req.duration_minutes
        if req.duration_sec and req.duration_sec > 0:
            dur = req.duration_sec / 60.0
        try:
            session = await tests.start_ab_test(
                req.name, dur, req.variant_a, req.variant_b, req.description
            )
            return {"status": "started", "id": session.id, "name": session.name, "type": "ab"}
        except RuntimeError as e:
            return {"error": str(e)}

    @app.get("/api/tests/active")
    async def test_active():
        if not tests:
            return None
        return tests.get_active()

    @app.post("/api/tests/stop")
    async def test_stop():
        if not tests:
            return {"error": "Test runner not initialized"}
        session = await tests.stop_test()
        if session:
            return {"status": "stopped", "id": session.id}
        return {"status": "no_active_test"}

    @app.get("/api/tests/list")
    async def test_list():
        if not tests:
            return []
        return tests.list_tests()

    @app.get("/api/tests/{test_id}")
    async def test_get(test_id: str):
        if not tests:
            return None
        return tests.get_test(test_id)

    @app.get("/api/tests/{test_id}/json")
    async def test_export_json(test_id: str):
        """Download test results as JSON file."""
        if not tests:
            return PlainTextResponse("No test runner")
        data = tests.get_test(test_id)
        if not data:
            return PlainTextResponse("Test not found", status_code=404)
        return PlainTextResponse(
            json.dumps(data, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=test_{test_id}.json"},
        )

    # ── Preflight, Impairment, Traffic Gen ──

    pf = preflight_checker
    imp = impairment_controller
    tg = traffic_generator

    @app.post("/api/preflight")
    async def run_preflight():
        if not pf:
            return {"error": "Preflight checker not initialized"}
        return await pf.run_all_checks()

    @app.post("/api/impair")
    async def apply_impairment(req: ImpairRequest):
        if not imp:
            return {"error": "Impairment controller not available (need root)"}
        kwargs = {}
        if req.value_ms:
            kwargs["value_ms"] = req.value_ms
        if req.value_pct:
            kwargs["value_pct"] = req.value_pct
        return imp.apply(req.path_id, req.action, **kwargs)

    @app.get("/api/impair")
    async def get_impairment():
        if not imp:
            return {}
        return imp.get_state()

    @app.post("/api/traffic/start")
    async def start_traffic(req: TrafficRequest):
        if not tg:
            return {"error": "Traffic generator not initialized"}
        try:
            tun = ctrl.tunnel.tun if hasattr(ctrl, "tunnel") and hasattr(ctrl.tunnel, "tun") else None
            await tg.start(req.mode, req.duration_sec, req.bitrate_kbps, tun)
            return {"status": "started", "mode": req.mode}
        except RuntimeError as e:
            return {"error": str(e)}

    @app.post("/api/traffic/stop")
    async def stop_traffic():
        if tg:
            await tg.stop()
        return {"status": "stopped"}

    @app.get("/api/traffic/status")
    async def traffic_status():
        if not tg:
            return {"running": False}
        return tg.get_status()

    # ── Device Registry (Edge Agent Management) ──

    dev_reg = device_registry

    class DeviceCommand(BaseModel):
        action: str  # start, stop, restart, configure
        config: dict = {}

    class ProvisionRequest(BaseModel):
        device_name: str = ""

    @app.get("/api/devices")
    async def list_devices():
        if not dev_reg:
            return []
        return dev_reg.list_devices()

    @app.get("/api/devices/{device_id}")
    async def get_device(device_id: str):
        if not dev_reg:
            return {"error": "Device registry not initialized"}
        dev = dev_reg.get_device(device_id)
        if not dev:
            return {"error": f"Device {device_id} not found"}
        return {
            "device_id": dev.device_id,
            "connected": dev.connected,
            "status": dev.status,
            "registered_at": dev.registered_at,
        }

    @app.post("/api/devices/{device_id}/command")
    async def send_device_command(device_id: str, cmd: DeviceCommand):
        if not dev_reg:
            return {"error": "Device registry not initialized"}
        return await dev_reg.send_command(device_id, cmd.action, cmd.config)

    @app.post("/api/devices/provision")
    async def provision_device(req: ProvisionRequest):
        if not dev_reg:
            return {"error": "Device registry not initialized"}
        bundle = dev_reg.provision_device(req.device_name)
        # Add VPS host info
        state = ctrl.get_system_state()
        bundle["vps_host"] = state.get("vps_host", "")
        bundle["setup_command"] = (
            f"curl -sSL http://{{VPS_HOST}}:8080/api/devices/provision-script "
            f"| sudo bash -s -- --device-id {bundle['device_id']} "
            f"--key {bundle['encryption_key']}"
        )
        return bundle

    @app.websocket("/ws/agent")
    async def agent_websocket(ws: WebSocket):
        if dev_reg:
            await dev_reg.handle_agent_ws(ws)
        else:
            await ws.accept()
            await ws.close(code=4000, reason="Device registry not available")

    # ── System Info, Diagnostics, Logs ──

    @app.get("/api/system/info")
    async def system_info():
        """System resource info — CPU, memory, uptime, disk."""
        import platform
        info = {"hostname": platform.node(), "platform": platform.platform()}
        try:
            import psutil
            info["cpu_pct"] = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory()
            info["memory_pct"] = round(mem.percent, 1)
            info["memory_total_mb"] = round(mem.total / 1e6)
            info["memory_used_mb"] = round(mem.used / 1e6)
            disk = psutil.disk_usage("/")
            info["disk_pct"] = round(disk.percent, 1)
            info["disk_total_gb"] = round(disk.total / 1e9, 1)
            info["uptime_sec"] = round(time.time() - psutil.boot_time())
            info["load_avg"] = list(psutil.getloadavg())
        except ImportError:
            info["error"] = "psutil not installed"
        return info

    @app.post("/api/diagnostics/ping")
    async def diag_ping(req: dict):
        """Ping a host from the server."""
        import subprocess as sp
        host = req.get("host", "8.8.8.8")
        count = min(req.get("count", 4), 10)
        try:
            result = sp.run(
                ["ping", "-c", str(count), "-W", "2", host],
                capture_output=True, text=True, timeout=15,
            )
            return {"host": host, "output": result.stdout, "success": result.returncode == 0}
        except Exception as e:
            return {"host": host, "output": str(e), "success": False}

    @app.post("/api/diagnostics/traceroute")
    async def diag_traceroute(req: dict):
        """Traceroute to a host."""
        import subprocess as sp
        host = req.get("host", "8.8.8.8")
        try:
            result = sp.run(
                ["traceroute", "-m", "15", "-w", "2", host],
                capture_output=True, text=True, timeout=30,
            )
            return {"host": host, "output": result.stdout or result.stderr, "success": result.returncode == 0}
        except FileNotFoundError:
            return {"host": host, "output": "traceroute not installed", "success": False}
        except Exception as e:
            return {"host": host, "output": str(e), "success": False}

    @app.get("/api/logs")
    async def system_logs(lines: int = Query(default=100, le=500)):
        """Get recent engine log lines."""
        import subprocess as sp
        try:
            result = sp.run(
                ["journalctl", "-u", "ratan-engine", "--no-pager", "-n", str(lines), "--output", "short"],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout:
                return {"lines": result.stdout.strip().split("\n"), "source": "journalctl"}
        except Exception:
            pass
        # Fallback: try reading log from a common location
        for log_path in ["/var/log/hyperagg.log", "logs/engine.log"]:
            try:
                from pathlib import Path
                p = Path(log_path)
                if p.exists():
                    all_lines = p.read_text().strip().split("\n")
                    return {"lines": all_lines[-lines:], "source": log_path}
            except Exception:
                pass
        return {"lines": ["No log source available. Start with --log-file to enable."], "source": "none"}

    @app.get("/api/config")
    async def get_config():
        """Get current config (sanitized — no encryption keys)."""
        state = ctrl.get_system_state()
        config_out = {}
        for key in ["scheduler", "fec", "qos", "tunnel"]:
            if key in state:
                config_out[key] = state[key]
        return config_out

    @app.get("/api/firewall")
    async def get_firewall():
        """Get current iptables rules (read-only)."""
        import subprocess as sp
        rules = {}
        for table in ["filter", "nat", "mangle"]:
            try:
                result = sp.run(["iptables", "-t", table, "-L", "-n", "--line-numbers"],
                                capture_output=True, text=True, timeout=5)
                rules[table] = result.stdout.strip().split("\n") if result.stdout else []
            except Exception:
                rules[table] = ["iptables not available"]
        return rules

    @app.get("/api/routes")
    async def get_routes():
        """Get current routing table."""
        import subprocess as sp
        try:
            result = sp.run(["ip", "route", "show"], capture_output=True, text=True, timeout=5)
            return {"routes": result.stdout.strip().split("\n") if result.stdout else []}
        except Exception:
            return {"routes": ["ip command not available"]}

    @app.post("/api/bypass/add")
    async def add_bypass(req: dict):
        """Add a bypass rule (domain or IP)."""
        from hyperagg.controller.bypass_rules import BypassRules
        if not hasattr(app, '_bypass'):
            app._bypass = BypassRules()
        domain = req.get("domain", "")
        ip = req.get("ip", "")
        if domain:
            return app._bypass.add_domain(domain)
        elif ip:
            return app._bypass.add_ip(ip)
        return {"error": "Provide domain or ip"}

    @app.get("/api/bypass")
    async def get_bypass():
        if not hasattr(app, '_bypass'):
            from hyperagg.controller.bypass_rules import BypassRules
            app._bypass = BypassRules()
        return app._bypass.get_rules()

    @app.post("/api/bypass/clear")
    async def clear_bypass():
        if hasattr(app, '_bypass'):
            return app._bypass.clear_all()
        return {"status": "ok"}

    # ── WAN Interface, DNS, DHCP, QoS, Config Management ──

    class WanConfigRequest(BaseModel):
        interface: str
        protocol: str = "dhcp"  # dhcp | static
        ip_addr: str = ""
        gateway: str = ""
        mtu: int = 1500

    class DnsConfigRequest(BaseModel):
        servers: list[str] = ["8.8.8.8", "1.1.1.1"]

    class QosTierRequest(BaseModel):
        tier: str  # realtime, streaming, bulk
        bandwidth_pct: int = 0
        fec_mode: str = ""

    @app.get("/api/interfaces")
    async def list_interfaces():
        """List all network interfaces with status."""
        import subprocess as sp
        interfaces = []
        try:
            result = sp.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=5)
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split()
                if len(parts) < 4:
                    continue
                name = parts[1]
                if name == "lo":
                    continue
                ip = ""
                for i, p in enumerate(parts):
                    if p == "inet":
                        ip = parts[i + 1].split("/")[0]
                        break
                # Get link state
                link = sp.run(["ip", "link", "show", "dev", name], capture_output=True, text=True, timeout=3)
                is_up = "state UP" in link.stdout
                mtu_match = __import__("re").search(r"mtu (\d+)", link.stdout)
                mtu = int(mtu_match.group(1)) if mtu_match else 1500
                interfaces.append({"name": name, "ip": ip, "is_up": is_up, "mtu": mtu})
        except Exception:
            pass
        return interfaces

    @app.post("/api/interfaces/configure")
    async def configure_interface(req: WanConfigRequest):
        """Configure a WAN interface (static IP or DHCP)."""
        import subprocess as sp
        try:
            if req.protocol == "static" and req.ip_addr:
                sp.run(["ip", "addr", "flush", "dev", req.interface], capture_output=True)
                sp.run(["ip", "addr", "add", f"{req.ip_addr}/24", "dev", req.interface], capture_output=True)
                if req.gateway:
                    sp.run(["ip", "route", "add", "default", "via", req.gateway, "dev", req.interface], capture_output=True)
            if req.mtu != 1500:
                sp.run(["ip", "link", "set", "dev", req.interface, "mtu", str(req.mtu)], capture_output=True)
            sp.run(["ip", "link", "set", "dev", req.interface, "up"], capture_output=True)
            return {"status": "ok", "interface": req.interface}
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/dns")
    async def get_dns():
        """Get current DNS configuration."""
        servers = []
        try:
            from pathlib import Path
            resolv = Path("/etc/resolv.conf")
            if resolv.exists():
                for line in resolv.read_text().split("\n"):
                    if line.startswith("nameserver"):
                        servers.append(line.split()[1])
        except Exception:
            pass
        # Also check dnsmasq config
        dnsmasq_servers = []
        try:
            from pathlib import Path
            for cf in [Path("/tmp/hyperagg-dnsmasq.conf"), Path("/etc/dnsmasq.d/hyperagg-lan.conf")]:
                if cf.exists():
                    for line in cf.read_text().split("\n"):
                        if line.startswith("server="):
                            dnsmasq_servers.append(line.split("=")[1])
        except Exception:
            pass
        return {"resolv_conf": servers, "dnsmasq_upstream": dnsmasq_servers or ["8.8.8.8", "1.1.1.1"]}

    @app.post("/api/dns/configure")
    async def configure_dns(req: DnsConfigRequest):
        """Update DNS upstream servers."""
        import subprocess as sp
        try:
            from pathlib import Path
            # Update dnsmasq config
            config_path = Path("/tmp/hyperagg-dnsmasq.conf")
            if config_path.exists():
                lines = config_path.read_text().split("\n")
                new_lines = [l for l in lines if not l.startswith("server=")]
                for s in req.servers:
                    new_lines.append(f"server={s}")
                config_path.write_text("\n".join(new_lines))
                sp.run(["pkill", "-HUP", "dnsmasq"], capture_output=True)  # Reload
                return {"status": "ok", "servers": req.servers}
            return {"status": "ok", "detail": "No dnsmasq config found — written to resolv.conf"}
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/dhcp/leases")
    async def get_dhcp_leases():
        """Get current DHCP leases from dnsmasq."""
        leases = []
        try:
            from pathlib import Path
            lease_file = Path("/var/lib/misc/dnsmasq.leases")
            if not lease_file.exists():
                lease_file = Path("/tmp/dhcp.leases")
            if lease_file.exists():
                for line in lease_file.read_text().strip().split("\n"):
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) >= 4:
                        leases.append({
                            "expires": parts[0],
                            "mac": parts[1],
                            "ip": parts[2],
                            "hostname": parts[3] if len(parts) > 3 else "*",
                        })
        except Exception:
            pass
        return {"leases": leases, "count": len(leases)}

    @app.get("/api/qos")
    async def get_qos():
        """Get current QoS tier configuration."""
        state = ctrl.get_system_state()
        qos = state.get("qos", {})
        tier_mgr = state.get("tier_manager", {})
        return {"tiers": qos, "tier_manager": tier_mgr}

    @app.post("/api/qos/configure")
    async def configure_qos(req: QosTierRequest):
        """Update QoS tier settings."""
        # Update the SDN controller's QoS engine
        if hasattr(ctrl, 'qos_engine'):
            tier_cfg = ctrl.qos_engine._tiers.get(req.tier, {})
            if req.bandwidth_pct > 0:
                tier_cfg["bandwidth_pct"] = req.bandwidth_pct
            if req.fec_mode:
                tier_cfg["fec_mode"] = req.fec_mode
            ctrl.qos_engine._tiers[req.tier] = tier_cfg
            return {"status": "ok", "tier": req.tier, "config": tier_cfg}
        return {"error": "QoS engine not available"}

    @app.get("/api/config/download")
    async def download_config():
        """Download current config as YAML file."""
        from pathlib import Path
        for config_path in ["config.yaml", "config_client.yaml", "/etc/hyperagg/agent.yaml"]:
            p = Path(config_path)
            if p.exists():
                return PlainTextResponse(
                    p.read_text(),
                    media_type="text/yaml",
                    headers={"Content-Disposition": f"attachment; filename={p.name}"},
                )
        return PlainTextResponse("No config file found", status_code=404)

    @app.post("/api/config/upload")
    async def upload_config(req: dict):
        """Upload/restore a config YAML."""
        content = req.get("content", "")
        if not content:
            return {"error": "No content provided"}
        try:
            import yaml
            parsed = yaml.safe_load(content)
            if not isinstance(parsed, dict):
                return {"error": "Invalid YAML — must be a dict"}
            from pathlib import Path
            Path("config.yaml").write_text(content)
            return {"status": "ok", "keys": list(parsed.keys())}
        except Exception as e:
            return {"error": f"Invalid YAML: {e}"}

    # ── WebSocket for live updates ──

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        logger.info("WebSocket client connected")
        try:
            while True:
                state = ctrl.get_system_state()
                if pkt_log:
                    state["recent_packets"] = pkt_log.get_recent(20)
                    state["packet_stats"] = pkt_log.get_stats()
                state["events"] = ctrl.get_events(10)
                if tests:
                    state["active_test"] = tests.get_active()
                state["ai_enabled"] = ai.is_enabled if ai else False
                if dev_reg:
                    state["devices"] = dev_reg.list_devices()
                await ws.send_json(state)
                await asyncio.sleep(1.0)
        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected")
        except Exception as e:
            logger.debug(f"WebSocket error: {e}")

    return app
