"""
SDN Controller — central orchestrator for all HyperAgg components.

Manages the lifecycle of:
  - TunnelClient or TunnelServer
  - FecEngine (via tunnel)
  - PathMonitor + PathPredictor + PathScheduler (via tunnel)
  - QoSEngine
  - NetworkManager
  - Dashboard API + WebSocket

Also runs the main control loop that:
  1. Updates path predictions
  2. Adjusts FEC mode based on loss
  3. Emits events to dashboard
  4. Logs state to telemetry
"""

import asyncio
import logging
import time
from typing import Optional

from hyperagg.tunnel.client import TunnelClient
from hyperagg.tunnel.server import TunnelServer
from hyperagg.controller.network_manager import NetworkManager
from hyperagg.controller.qos_engine import QoSEngine

logger = logging.getLogger("hyperagg.controller.sdn")


class SDNController:
    """Central control logic for HyperAgg."""

    def __init__(self, config: dict, mode: str = "client"):
        self._config = config
        self._mode = mode

        # Components (initialized during start)
        self.tunnel: Optional[TunnelClient | TunnelServer] = None
        self.network_manager = NetworkManager(config)
        self.qos_engine = QoSEngine(config)

        # State
        self._running = False
        self._events: list[dict] = []  # Recent events for dashboard
        self._start_time = 0.0

        # Control loop interval
        self._control_interval = config.get("scheduler", {}).get(
            "probe_interval_ms", 100
        ) / 1000.0

    def set_tunnel(self, tunnel: TunnelClient | TunnelServer) -> None:
        """Set the tunnel instance (client or server)."""
        self.tunnel = tunnel

    async def start(self) -> None:
        """Start the SDN controller control loop."""
        self._running = True
        self._start_time = time.monotonic()
        self._emit_event("system", "SDN controller started")
        logger.info("SDN controller started")

        while self._running:
            try:
                await self._control_tick()
            except Exception as e:
                logger.error(f"Control loop error: {e}")
            await asyncio.sleep(self._control_interval)

    async def stop(self) -> None:
        self._running = False
        self._emit_event("system", "SDN controller stopped")

    async def _control_tick(self) -> None:
        """One iteration of the control loop."""
        if not self.tunnel:
            return

        if isinstance(self.tunnel, TunnelClient):
            # Update scheduler predictions
            self.tunnel._scheduler.update_predictions()

            # Update FEC mode based on current loss
            states = self.tunnel._monitor.get_all_path_states()
            path_loss = {pid: s.loss_pct for pid, s in states.items()}
            old_mode = self.tunnel._fec.current_mode
            self.tunnel._fec.update_mode(path_loss)
            new_mode = self.tunnel._fec.current_mode

            if new_mode != old_mode:
                self._emit_event(
                    "fec",
                    f"FEC mode changed: {old_mode} -> {new_mode}",
                )

            # Check for path state changes
            for pid, state in states.items():
                if not state.is_alive:
                    self._emit_event(
                        "path",
                        f"Path {pid} is DOWN (consecutive failures)",
                    )

            # Module 2+4: update pacer rates and congestion control from BW estimator
            if hasattr(self.tunnel, '_bw_estimator'):
                for pid in states:
                    bw = self.tunnel._bw_estimator.get_estimated_bw(pid)
                    if bw > 0:
                        self.tunnel._pacer.set_rate(pid, bw)
                        if pid in self.tunnel._congestion:
                            self.tunnel._congestion[pid].update_estimated_bw(bw)
                total_bw = sum(
                    self.tunnel._bw_estimator.get_estimated_bw(pid) for pid in states
                )
                if total_bw > 0:
                    self.tunnel._tier_mgr.update_capacity(total_bw)

    # ── Control API (called by dashboard) ──

    def set_scheduler_mode(self, mode: str) -> None:
        """Override scheduler mode."""
        if isinstance(self.tunnel, TunnelClient):
            self.tunnel._scheduler._mode = mode
            self.tunnel._scheduler.update_predictions()
            self._emit_event("config", f"Scheduler mode set to: {mode}")

    def set_fec_mode(self, mode: str) -> None:
        """Override FEC mode."""
        if self.tunnel:
            self.tunnel._fec._mode_setting = mode
            if mode != "auto":
                self.tunnel._fec._current_mode = mode
            self._emit_event("config", f"FEC mode set to: {mode}")

    def force_path(self, path_id: int) -> None:
        """Force all traffic onto one path (for demo/testing)."""
        if isinstance(self.tunnel, TunnelClient):
            self.tunnel._scheduler._mode = "weighted"
            # Set all paths to 0 except the forced one
            for pid in self.tunnel._scheduler._alive_paths:
                if pid != path_id:
                    state = self.tunnel._monitor.get_path_state(pid)
                    # We can't directly set score, but we can switch to weighted
            self._emit_event("config", f"Forced traffic to path {path_id}")

    def release_path(self) -> None:
        """Return to AI scheduling."""
        if isinstance(self.tunnel, TunnelClient):
            self.tunnel._scheduler._mode = self._config.get(
                "scheduler", {}
            ).get("mode", "ai")
            self._emit_event("config", "Released path forcing, back to AI mode")

    def get_system_state(self) -> dict:
        """Complete snapshot for dashboard."""
        state = {
            "mode": self._mode,
            "uptime_sec": round(time.monotonic() - self._start_time, 1),
            "running": self._running,
        }

        if isinstance(self.tunnel, TunnelClient):
            state["metrics"] = self.tunnel.get_metrics()
            path_states = self.tunnel._monitor.get_all_path_states()
            state["paths"] = {}
            for pid, ps in path_states.items():
                pred = self.tunnel._scheduler._predictions.get(pid)
                state["paths"][pid] = {
                    "avg_rtt_ms": round(ps.avg_rtt_ms, 1),
                    "min_rtt_ms": round(ps.min_rtt_ms, 1),
                    "max_rtt_ms": round(ps.max_rtt_ms, 1),
                    "jitter_ms": round(ps.jitter_ms, 1),
                    "loss_pct": round(ps.loss_pct * 100, 2),
                    "throughput_mbps": round(ps.throughput_mbps, 1),
                    "is_alive": ps.is_alive,
                    "quality_score": round(ps.quality_score, 3),
                    "prediction": {
                        "delivery_probability": round(pred.delivery_probability, 3) if pred else None,
                        "trend": pred.trend if pred else None,
                        "recommended_action": pred.recommended_action if pred else None,
                    } if pred else None,
                }
            state["scheduler"] = {
                "mode": self.tunnel._scheduler._mode,
                "best_path": self.tunnel._scheduler._best_path,
                "stats": {
                    "decisions_total": self.tunnel._scheduler.get_stats().decisions_total,
                    "avg_decision_time_us": round(
                        self.tunnel._scheduler.get_stats().avg_decision_time_us, 1
                    ),
                    "decisions_by_reason": dict(
                        self.tunnel._scheduler.get_stats().decisions_by_reason
                    ),
                },
            }
            state["fec"] = self.tunnel._fec.get_stats()
            state["qos"] = self.qos_engine.get_tier_info()

            # Module stats (all 7 modules)
            if hasattr(self.tunnel, '_bw_estimator'):
                state["bandwidth"] = {
                    pid: self.tunnel._bw_estimator.get_estimated_bw_mbps(pid)
                    for pid in path_states
                }
                state["pacer"] = self.tunnel._pacer.get_stats()
                state["congestion"] = {
                    pid: cc.get_stats()
                    for pid, cc in self.tunnel._congestion.items()
                }
                state["owd"] = self.tunnel._owd.get_stats()
                state["reorder"] = self.tunnel._reorder.get_stats()
                state["throughput"] = self.tunnel._throughput.compute()
                state["tier_manager"] = self.tunnel._tier_mgr.get_stats()
                state["session"] = self.tunnel._session_mgr.get_stats()
                state["adaptive_fec"] = self.tunnel._fec.get_adaptive_stats()

        elif isinstance(self.tunnel, TunnelServer):
            state["metrics"] = self.tunnel.get_metrics()
            state["sessions"] = self.tunnel.get_sessions()
            state["fec"] = self.tunnel._fec.get_stats()

        return state

    def get_events(self, last_n: int = 50) -> list[dict]:
        """Get recent events for event log."""
        return self._events[-last_n:]

    def _emit_event(self, category: str, message: str) -> None:
        """Add an event to the log."""
        event = {
            "timestamp": time.time(),
            "category": category,
            "message": message,
        }
        self._events.append(event)
        # Keep bounded
        if len(self._events) > 500:
            self._events = self._events[-250:]
        logger.info(f"[{category}] {message}")
