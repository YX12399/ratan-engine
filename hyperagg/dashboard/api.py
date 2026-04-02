"""
Dashboard API — FastAPI routes + WebSocket for real-time metrics.

Endpoints:
  GET  /api/health         — System health check
  GET  /api/state          — Complete system state snapshot
  GET  /api/paths          — Per-path state with predictions
  GET  /api/fec            — FEC stats and mode
  GET  /api/scheduler      — Scheduler stats and decisions
  GET  /api/events         — Recent event log
  GET  /api/packets        — Recent packet log
  GET  /api/packets/csv    — Export packet log as CSV
  POST /api/scheduler/mode — Set scheduler mode
  POST /api/fec/mode       — Set FEC mode
  POST /api/force-path     — Force traffic to one path
  POST /api/release-path   — Release path forcing
  WS   /ws                 — WebSocket for live updates (1Hz)
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


def create_dashboard_app(sdn_controller, packet_logger=None) -> FastAPI:
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
        }

    @app.get("/api/state")
    async def system_state():
        return ctrl.get_system_state()

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

    # ── WebSocket for live updates ──

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        logger.info("WebSocket client connected")
        try:
            while True:
                state = ctrl.get_system_state()
                # Add recent packets for live stream
                if pkt_log:
                    state["recent_packets"] = pkt_log.get_recent(20)
                    state["packet_stats"] = pkt_log.get_stats()
                # Add recent events
                state["events"] = ctrl.get_events(10)
                await ws.send_json(state)
                await asyncio.sleep(1.0)  # 1 Hz updates
        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected")
        except Exception as e:
            logger.debug(f"WebSocket error: {e}")

    return app
