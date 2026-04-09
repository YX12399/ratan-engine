"""
Edge Agent — lightweight daemon for RPi/Beelink edge devices.

Runs on the edge device and provides:
  1. Local HTTP API (LAN-facing :8081) for first-time setup
  2. Outbound WebSocket to VPS for remote control from dashboard
  3. Bonding process lifecycle management (start/stop tunnel client)

The agent phones home to the VPS. The VPS dashboard can then
control the device — start bonding, stop bonding, view status —
all from a browser. No SSH needed after initial setup.
"""

import asyncio
import hashlib
import json
import logging
import os
import platform
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("hyperagg.agent")

DEFAULT_CONFIG_PATH = "/etc/hyperagg/agent.yaml"
DEFAULT_LOCAL_PORT = 8081


class DeviceInfo:
    """Collects device hardware/network info."""

    @staticmethod
    def get_interfaces() -> list[dict]:
        """Discover network interfaces with IPs and gateways."""
        interfaces = []
        try:
            result = subprocess.run(
                ["ip", "-4", "-o", "addr", "show"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split()
                if len(parts) < 4:
                    continue
                name = parts[1]
                if name in ("lo",) or name.startswith(("hagg", "ratan", "docker", "veth", "br-")):
                    continue
                # Extract IP
                for i, p in enumerate(parts):
                    if p == "inet":
                        ip = parts[i + 1].split("/")[0]
                        gw = DeviceInfo._get_gateway(name)
                        interfaces.append({
                            "name": name, "ip": ip, "gateway": gw,
                            "type": DeviceInfo._guess_type(name),
                        })
                        break
        except Exception as e:
            logger.debug(f"Interface discovery failed: {e}")
        return interfaces

    @staticmethod
    def _get_gateway(iface: str) -> str:
        try:
            r = subprocess.run(
                ["ip", "route", "show", "dev", iface, "default"],
                capture_output=True, text=True, timeout=3,
            )
            for part in r.stdout.split():
                if part.count(".") == 3:
                    return part
        except Exception:
            pass
        return ""

    @staticmethod
    def _guess_type(name: str) -> str:
        """Guess interface type from name."""
        if name.startswith(("eth", "enp", "eno")):
            return "ethernet"
        if name.startswith(("wlan", "wlp")):
            return "wifi"
        if name.startswith(("usb", "wwan", "enx")):
            return "cellular"
        return "unknown"

    @staticmethod
    def get_system_info() -> dict:
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory()
            return {
                "cpu_pct": cpu,
                "memory_pct": round(mem.percent, 1),
                "memory_total_mb": round(mem.total / 1e6),
            }
        except ImportError:
            return {"cpu_pct": 0, "memory_pct": 0, "memory_total_mb": 0}


class EdgeAgent:
    """Main agent class — manages local API, VPS connection, and tunnel lifecycle."""

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        self._config_path = config_path
        self._config = self._load_config()
        self._device_id = self._config.get("device_id", f"edge-{platform.node()}")
        self._vps_host = self._config.get("vps_host", "")
        self._vps_port = self._config.get("vps_port", 8080)
        self._tunnel_port = self._config.get("tunnel_port", 9999)
        self._encryption_key = self._config.get("encryption_key", "")
        self._local_port = self._config.get("local_port", DEFAULT_LOCAL_PORT)
        self._auto_start = self._config.get("auto_start", False)

        # State
        self._running = False
        self._tunnel_task: Optional[asyncio.Task] = None
        self._tunnel_client = None  # TunnelClient reference for metrics
        self._tunnel_status = "stopped"  # stopped, starting, running, error, stopping
        self._tunnel_error = ""
        self._vps_connected = False
        self._vps_ws = None
        self._start_time = time.monotonic()

    def _load_config(self) -> dict:
        path = Path(self._config_path)
        if path.exists():
            try:
                return yaml.safe_load(path.read_text()) or {}
            except Exception as e:
                logger.warning(f"Config load failed: {e}")
        return {}

    def save_config(self, updates: dict) -> None:
        """Update and persist config."""
        self._config.update(updates)
        path = Path(self._config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.dump(self._config, default_flow_style=False))
        # Refresh internal state
        self._vps_host = self._config.get("vps_host", self._vps_host)
        self._vps_port = self._config.get("vps_port", self._vps_port)
        self._encryption_key = self._config.get("encryption_key", self._encryption_key)
        self._device_id = self._config.get("device_id", self._device_id)
        logger.info(f"Config saved to {path}")

    def get_status(self) -> dict:
        """Full device status for API/WebSocket — includes tunnel metrics."""
        status = {
            "device_id": self._device_id,
            "tunnel_status": self._tunnel_status,
            "tunnel_error": self._tunnel_error,
            "vps_connected": self._vps_connected,
            "vps_host": self._vps_host,
            "interfaces": DeviceInfo.get_interfaces(),
            "system": DeviceInfo.get_system_info(),
            "uptime_sec": round(time.monotonic() - self._start_time),
            "auto_start": self._auto_start,
            "encryption_configured": bool(self._encryption_key),
            "config_path": self._config_path,
        }
        # Include full tunnel metrics when tunnel is running
        if self._tunnel_client and self._tunnel_status == "running":
            try:
                status["tunnel_metrics"] = self._tunnel_client.get_metrics()
            except Exception:
                pass
        return status

    # ── Tunnel Lifecycle ──

    async def start_tunnel(self) -> dict:
        """Start the bonding tunnel client."""
        if self._tunnel_status == "running":
            return {"status": "already_running"}
        if not self._vps_host:
            return {"status": "error", "detail": "VPS host not configured"}

        self._tunnel_status = "starting"
        self._tunnel_error = ""
        logger.info("Starting tunnel client...")

        try:
            self._tunnel_task = asyncio.create_task(self._run_tunnel())
            # Wait a moment to see if it crashes immediately
            await asyncio.sleep(2)
            if self._tunnel_status == "error":
                return {"status": "error", "detail": self._tunnel_error}
            self._tunnel_status = "running"
            return {"status": "started"}
        except Exception as e:
            self._tunnel_status = "error"
            self._tunnel_error = str(e)
            return {"status": "error", "detail": str(e)}

    async def stop_tunnel(self) -> dict:
        """Stop the bonding tunnel client."""
        if self._tunnel_status not in ("running", "starting"):
            return {"status": "already_stopped"}

        self._tunnel_status = "stopping"
        if self._tunnel_task:
            self._tunnel_task.cancel()
            try:
                await self._tunnel_task
            except (asyncio.CancelledError, Exception):
                pass
            self._tunnel_task = None
        self._tunnel_status = "stopped"
        logger.info("Tunnel stopped")
        return {"status": "stopped"}

    async def _run_tunnel(self) -> None:
        """Run the tunnel client as an async task."""
        try:
            # Build config for the tunnel client
            tunnel_config = {
                "server": {"host": "0.0.0.0", "port": 8082},  # Don't conflict with agent
                "vps": {
                    "host": self._vps_host,
                    "tunnel_port": self._tunnel_port,
                    "encryption_key": self._encryption_key,
                    "server_ip": "10.99.0.2",
                    "client_ip": "10.99.0.1",
                },
                "interfaces": {"wan_interfaces": self._config.get("interfaces", [])},
                "tunnel": {"mtu": 1400, "keepalive_interval_ms": 1000, "sequence_window": 1024, "reorder_timeout_ms": 100},
                "fec": {"mode": "auto", "xor_group_size": 4, "rs_data_shards": 8, "rs_parity_shards": 2},
                "scheduler": {
                    "mode": "ai", "history_window": 50, "ewma_alpha": 0.3,
                    "latency_budget_ms": 150, "probe_interval_ms": 100,
                    "jitter_weight": 0.3, "loss_weight": 0.5, "rtt_weight": 0.2,
                },
                "qos": {
                    "enabled": True,
                    "tiers": {
                        "realtime": {
                            "ports": "3478-3481,5348-5352,8801-8810,19302-19309",
                            "fec_mode": "replicate",
                            "bandwidth_pct": 60,
                        },
                        "streaming": {
                            "ports": "5000-5010,8000-8100",
                            "fec_mode": "reed_solomon",
                            "bandwidth_pct": 30,
                        },
                        "bulk": {"fec_mode": "xor", "bandwidth_pct": 10},
                    },
                },
            }

            # Import and run the client
            from hyperagg.tunnel.tun_device import TunDevice
            from hyperagg.tunnel.client import TunnelClient
            from hyperagg.controller.network_manager import NetworkManager

            client = TunnelClient(tunnel_config)
            self._tunnel_client = client  # Store for metrics access
            nm = NetworkManager(tunnel_config)

            # Auto-detect interfaces if not specified
            ifaces = self._config.get("interfaces", [])
            if not ifaces:
                discovered = nm.discover_interfaces()
                # Exclude LAN interface
                lan_iface = self._config.get("lan_interface", "")
                ifaces = [i.name for i in discovered if i.name != lan_iface]

            if not ifaces:
                self._tunnel_status = "error"
                self._tunnel_error = "No WAN interfaces found"
                return

            for i, iface_name in enumerate(ifaces):
                info = nm.discover_interface(iface_name)
                if info:
                    client.add_path(i, iface_name, local_addr=info.ip_addr)
                else:
                    client.add_path(i, iface_name)

            # Try to create TUN
            tun = None
            try:
                tun = TunDevice(name="hagg0", mtu=1400)
                await tun.open(ip_addr="10.99.0.1", subnet_mask=30)
                client.tun = tun
            except Exception as e:
                logger.warning(f"TUN creation failed: {e} — running without TUN")

            self._tunnel_status = "running"
            logger.info(f"Tunnel client running on {len(ifaces)} path(s): {ifaces}")

            try:
                await client.start()
            finally:
                if tun:
                    await tun.close()
                await client.stop()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._tunnel_status = "error"
            self._tunnel_error = str(e)
            logger.error(f"Tunnel error: {e}")

    # ── VPS WebSocket Connection ──

    async def _vps_websocket_loop(self) -> None:
        """Maintain persistent WebSocket connection to VPS."""
        import aiohttp
        backoff = 1

        while self._running:
            if not self._vps_host:
                await asyncio.sleep(5)
                continue

            url = f"http://{self._vps_host}:{self._vps_port}/ws/agent"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, timeout=10) as ws:
                        self._vps_connected = True
                        self._vps_ws = ws
                        backoff = 1
                        logger.info(f"Connected to VPS at {url}")

                        # Send auth
                        await ws.send_json({
                            "type": "auth",
                            "device_id": self._device_id,
                            "key_hash": hashlib.sha256(
                                self._encryption_key.encode()
                            ).hexdigest()[:16] if self._encryption_key else "",
                        })

                        # Main loop: send heartbeats, receive commands
                        last_heartbeat = 0
                        while self._running:
                            # Send heartbeat every 5 seconds
                            now = time.monotonic()
                            if now - last_heartbeat >= 5:
                                await ws.send_json({
                                    "type": "heartbeat",
                                    **self.get_status(),
                                })
                                last_heartbeat = now

                            # Check for incoming commands (non-blocking)
                            try:
                                msg = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
                                await self._handle_vps_command(msg)
                            except asyncio.TimeoutError:
                                continue
                            except TypeError:
                                break  # Connection closed

            except Exception as e:
                logger.debug(f"VPS connection failed: {e}")
            finally:
                self._vps_connected = False
                self._vps_ws = None

            # Backoff and retry
            await asyncio.sleep(min(backoff, 30))
            backoff = min(backoff * 2, 30)

    async def _handle_vps_command(self, msg: dict) -> None:
        """Handle a command from the VPS dashboard."""
        action = msg.get("action", "")
        logger.info(f"VPS command: {action}")

        if action == "start":
            result = await self.start_tunnel()
            if self._vps_ws:
                await self._vps_ws.send_json({"type": "command_result", "action": action, **result})

        elif action == "stop":
            result = await self.stop_tunnel()
            if self._vps_ws:
                await self._vps_ws.send_json({"type": "command_result", "action": action, **result})

        elif action == "configure":
            config = msg.get("config", {})
            if config:
                self.save_config(config)
                if self._vps_ws:
                    await self._vps_ws.send_json({"type": "command_result", "action": action, "status": "ok"})

        elif action == "restart":
            await self.stop_tunnel()
            await asyncio.sleep(1)
            result = await self.start_tunnel()
            if self._vps_ws:
                await self._vps_ws.send_json({"type": "command_result", "action": action, **result})

    # ── Local HTTP API ──

    def create_local_app(self):
        """Create FastAPI app for LAN-facing setup/control UI."""
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import HTMLResponse
        from pydantic import BaseModel

        app = FastAPI(title="HyperAgg Edge Agent", version="2.0.0")
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

        agent = self

        class ConfigUpdate(BaseModel):
            vps_host: str = ""
            encryption_key: str = ""
            device_name: str = ""
            interfaces: list[str] = []
            lan_interface: str = ""
            auto_start: bool = False

        @app.get("/", response_class=HTMLResponse)
        async def local_ui():
            return self._get_local_html()

        @app.get("/api/agent/status")
        async def status():
            return agent.get_status()

        @app.post("/api/agent/configure")
        async def configure(cfg: ConfigUpdate):
            updates = {k: v for k, v in cfg.dict().items() if v or isinstance(v, bool)}
            if updates.get("device_name"):
                updates["device_id"] = updates.pop("device_name")
            agent.save_config(updates)
            return {"status": "ok", "config": agent._config}

        @app.post("/api/agent/start")
        async def start():
            return await agent.start_tunnel()

        @app.post("/api/agent/stop")
        async def stop():
            return await agent.stop_tunnel()

        @app.get("/api/agent/interfaces")
        async def interfaces():
            return DeviceInfo.get_interfaces()

        return app

    def _get_local_html(self) -> str:
        """Minimal local setup page."""
        return """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HyperAgg Edge Setup</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:system-ui;background:#f3f4f6;padding:20px;max-width:600px;margin:auto}
.card{background:#fff;border-radius:8px;padding:16px;margin-bottom:16px;border:1px solid #e5e7eb}
h1{font-size:20px;color:#2563eb;margin-bottom:16px}h3{font-size:14px;color:#6b7280;margin-bottom:8px}
input,select{width:100%;padding:8px;border:1px solid #d1d5db;border-radius:6px;margin-bottom:8px;font-size:14px}
button{padding:8px 16px;border:none;border-radius:6px;cursor:pointer;font-size:14px;margin:4px}
.btn-blue{background:#2563eb;color:#fff}.btn-green{background:#16a34a;color:#fff}.btn-red{background:#dc2626;color:#fff}
.btn-gray{background:#e5e7eb;color:#374151}
#status{font-size:13px;line-height:1.6}
.ok{color:#16a34a}.err{color:#dc2626}.warn{color:#ca8a04}
</style></head><body>
<h1>HyperAgg Edge Setup</h1>
<div class="card"><h3>Device Status</h3><div id="status">Loading...</div></div>
<div class="card"><h3>Configuration</h3>
<label>VPS Host</label><input id="vps" placeholder="89.167.91.132">
<label>Encryption Key</label><input id="key" placeholder="base64 key (from VPS dashboard)">
<label>Device Name</label><input id="name" placeholder="rpi-jlr-01">
<button class="btn-blue" onclick="saveCfg()">Save Config</button>
</div>
<div class="card"><h3>Bonding Control</h3>
<button class="btn-green" onclick="doCmd('start')">Start Bonding</button>
<button class="btn-red" onclick="doCmd('stop')">Stop Bonding</button>
<button class="btn-gray" onclick="refresh()">Refresh Status</button>
</div>
<div class="card"><h3>Interfaces</h3><div id="ifaces">Detecting...</div></div>
<script>
function refresh(){fetch('/api/agent/status').then(r=>r.json()).then(d=>{
  var h='<b>Device:</b> '+d.device_id+'<br>';
  h+='<b>Tunnel:</b> <span class="'+(d.tunnel_status==='running'?'ok':d.tunnel_status==='error'?'err':'warn')+'">'+d.tunnel_status+'</span>';
  if(d.tunnel_error)h+=' — '+d.tunnel_error;
  h+='<br><b>VPS:</b> '+(d.vps_connected?'<span class="ok">Connected</span>':'<span class="warn">Disconnected</span>')+' ('+d.vps_host+')';
  h+='<br><b>Encryption:</b> '+(d.encryption_configured?'<span class="ok">Yes</span>':'<span class="warn">No</span>');
  h+='<br><b>Uptime:</b> '+d.uptime_sec+'s';
  document.getElementById('status').innerHTML=h;
  if(d.vps_host)document.getElementById('vps').value=d.vps_host;
  if(d.device_id)document.getElementById('name').value=d.device_id;
  var ih='';d.interfaces.forEach(function(i){ih+='<div>'+i.name+': '+i.ip+(i.gateway?' via '+i.gateway:'')+' ('+i.type+')</div>'});
  document.getElementById('ifaces').innerHTML=ih||'<span class="warn">No interfaces found</span>';
})}
function saveCfg(){fetch('/api/agent/configure',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({vps_host:document.getElementById('vps').value,encryption_key:document.getElementById('key').value,
  device_name:document.getElementById('name').value})}).then(()=>refresh())}
function doCmd(c){fetch('/api/agent/'+c,{method:'POST'}).then(()=>setTimeout(refresh,2000))}
refresh();setInterval(refresh,5000);
</script></body></html>"""

    # ── Main Run Loop ──

    async def run(self) -> None:
        """Start the agent — local API + VPS connection + optional auto-start."""
        self._running = True
        logger.info(f"Edge Agent starting (device={self._device_id})")

        # Setup LAN interface if configured
        lan_iface = self._config.get("lan_interface", "")
        lan_ip = self._config.get("lan_ip", "192.168.50.1")
        if lan_iface:
            try:
                subprocess.run(["ip", "addr", "add", f"{lan_ip}/24", "dev", lan_iface],
                               capture_output=True, timeout=5)
                subprocess.run(["ip", "link", "set", "dev", lan_iface, "up"],
                               capture_output=True, timeout=5)
                logger.info(f"LAN interface {lan_iface} at {lan_ip}")
            except Exception:
                pass

        import uvicorn
        local_app = self.create_local_app()

        tasks = [
            self._run_local_server(local_app),
            self._vps_websocket_loop(),
        ]

        # Auto-start tunnel if configured
        if self._auto_start and self._vps_host:
            tasks.append(self._auto_start_tunnel())

        logger.info(f"Local setup UI: http://{lan_ip or 'localhost'}:{self._local_port}")

        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_local_server(self, app) -> None:
        """Run the local FastAPI server."""
        import uvicorn
        config = uvicorn.Config(app, host="0.0.0.0", port=self._local_port, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    async def _auto_start_tunnel(self) -> None:
        """Auto-start tunnel after a brief delay."""
        await asyncio.sleep(5)  # Let network settle
        logger.info("Auto-starting tunnel (auto_start=true)")
        await self.start_tunnel()

    async def shutdown(self) -> None:
        self._running = False
        await self.stop_tunnel()
