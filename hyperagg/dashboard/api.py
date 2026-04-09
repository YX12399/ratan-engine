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
        state = ctrl.get_system_state()
        if tests:
            state["active_test"] = tests.get_active()
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
        return await ai.chat(req.message)

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
