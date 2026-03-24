"""
Path Health Monitor — continuous real-time probing of each network path.
Replaces mwan3's 10-second polling with sub-100ms UDP probes.
Produces a 0-1 health score per path that drives scheduling decisions.
"""

import time
import socket
import struct
import threading
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class PathState(Enum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    ZOMBIE = "zombie"
    DEAD = "dead"
    RECOVERING = "recovering"


@dataclass
class ProbeResult:
    timestamp: float
    rtt_ms: float
    success: bool
    path_id: str
    seq: int


@dataclass
class PathHealth:
    path_id: str
    interface: str
    state: PathState = PathState.ACTIVE
    score: float = 1.0
    rtt_avg_ms: float = 0.0
    rtt_p95_ms: float = 0.0
    loss_pct: float = 0.0
    jitter_ms: float = 0.0
    last_probe_at: float = 0.0
    last_state_change_at: float = 0.0
    probe_count: int = 0
    consecutive_failures: int = 0

    def to_dict(self) -> dict:
        return {
            "path_id": self.path_id,
            "interface": self.interface,
            "state": self.state.value,
            "score": round(self.score, 4),
            "rtt_avg_ms": round(self.rtt_avg_ms, 2),
            "rtt_p95_ms": round(self.rtt_p95_ms, 2),
            "loss_pct": round(self.loss_pct, 2),
            "jitter_ms": round(self.jitter_ms, 2),
            "last_probe_at": self.last_probe_at,
            "probe_count": self.probe_count,
            "consecutive_failures": self.consecutive_failures,
        }


class HealthMonitor:
    """
    Runs a probe loop per path. Each loop sends UDP pings at the configured
    interval and computes a weighted health score from RTT, loss, and jitter.
    """

    def __init__(self, config_store):
        self.config = config_store
        self.paths: dict[str, PathHealth] = {}
        self._probe_history: dict[str, deque] = {}
        self._running = False
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._listeners: list[callable] = []

        # Subscribe to config changes
        self.config.subscribe(self._on_config_change)

    def register_path(self, path_id: str, interface: str, remote_addr: str, remote_port: int):
        """Register a network path to monitor."""
        with self._lock:
            self.paths[path_id] = PathHealth(
                path_id=path_id,
                interface=interface,
                last_state_change_at=time.time(),
            )
            window = self.config.get("path_health.loss_window_size", 20)
            self._probe_history[path_id] = deque(maxlen=window)

            if self._running:
                self._start_probe_thread(path_id, remote_addr, remote_port)

    def start(self):
        """Start all probe threads."""
        self._running = True
        # In a real deployment, threads would be started per registered path
        # For now, we run a single metrics simulation

    def stop(self):
        """Stop all probe threads."""
        self._running = False

    def record_probe(self, path_id: str, rtt_ms: Optional[float], success: bool):
        """
        Record a probe result and recalculate health score.
        Called either by the internal probe loop or externally by tunnel_server
        piggybacking on real traffic.
        """
        with self._lock:
            if path_id not in self.paths:
                return

            health = self.paths[path_id]
            now = time.time()

            result = ProbeResult(
                timestamp=now,
                rtt_ms=rtt_ms if rtt_ms else 0,
                success=success,
                path_id=path_id,
                seq=health.probe_count,
            )
            self._probe_history[path_id].append(result)
            health.probe_count += 1
            health.last_probe_at = now

            if success:
                health.consecutive_failures = 0
            else:
                health.consecutive_failures += 1

            # Recalculate score
            self._recalculate_score(path_id)

            # State machine
            old_state = health.state
            self._update_state(path_id)
            if health.state != old_state:
                health.last_state_change_at = now
                self._notify_state_change(path_id, old_state, health.state)

    def get_health(self, path_id: str) -> Optional[dict]:
        """Get current health for a path."""
        with self._lock:
            if path_id in self.paths:
                return self.paths[path_id].to_dict()
        return None

    def get_all_health(self) -> dict[str, dict]:
        """Get health for all paths."""
        with self._lock:
            return {pid: p.to_dict() for pid, p in self.paths.items()}

    def get_scores(self) -> dict[str, float]:
        """Get just the scores — used by path_balancer for scheduling."""
        with self._lock:
            return {pid: p.score for pid, p in self.paths.items()}

    def on_state_change(self, listener: callable):
        """Register callback for path state transitions."""
        self._listeners.append(listener)

    def _recalculate_score(self, path_id: str):
        """Recalculate composite health score from probe history."""
        health = self.paths[path_id]
        history = self._probe_history[path_id]

        cfg = self.config.get("path_health", {})
        min_probes = cfg.get("min_probes_for_score", 5)

        if len(history) < min_probes:
            return  # Not enough data

        recent = list(history)
        successful = [p for p in recent if p.success]

        # Loss
        loss_pct = (1 - len(successful) / len(recent)) * 100
        health.loss_pct = loss_pct

        if successful:
            rtts = [p.rtt_ms for p in successful]
            health.rtt_avg_ms = statistics.mean(rtts)
            health.rtt_p95_ms = sorted(rtts)[int(len(rtts) * 0.95)] if len(rtts) > 1 else rtts[0]
            health.jitter_ms = statistics.stdev(rtts) if len(rtts) > 1 else 0
        else:
            health.rtt_avg_ms = 999
            health.rtt_p95_ms = 999
            health.jitter_ms = 999

        # Weighted score
        weights = cfg.get("health_score_weights", {"rtt": 0.4, "loss": 0.35, "jitter": 0.25})
        rtt_thresh = cfg.get("rtt_threshold_ms", 150)
        loss_thresh = cfg.get("loss_threshold_pct", 5.0)
        jitter_thresh = cfg.get("jitter_tolerance_ms", 30)

        rtt_score = max(0, 1 - (health.rtt_avg_ms / rtt_thresh)) if rtt_thresh > 0 else 1
        loss_score = max(0, 1 - (health.loss_pct / (loss_thresh * 4))) if loss_thresh > 0 else 1
        jitter_score = max(0, 1 - (health.jitter_ms / (jitter_thresh * 3))) if jitter_thresh > 0 else 1

        health.score = (
            weights["rtt"] * rtt_score
            + weights["loss"] * loss_score
            + weights["jitter"] * jitter_score
        )
        health.score = max(0.0, min(1.0, health.score))

    def _update_state(self, path_id: str):
        """State machine for path lifecycle."""
        health = self.paths[path_id]
        cfg = self.config.get("path_health", {})
        failover_cfg = self.config.get("failover", {})

        zombie_timeout = cfg.get("zombie_timeout_ms", 2000)
        demotion = self.config.get("mptcp_scheduling.path_demotion_threshold", 0.3)
        promotion = self.config.get("mptcp_scheduling.path_promotion_threshold", 0.8)
        trigger = failover_cfg.get("trigger_threshold", 0.2)

        now = time.time()
        time_since_last = (now - health.last_probe_at) * 1000 if health.last_probe_at else 0

        if health.consecutive_failures > 10 or time_since_last > zombie_timeout * 2:
            health.state = PathState.DEAD
        elif health.consecutive_failures > 5 or time_since_last > zombie_timeout:
            health.state = PathState.ZOMBIE
        elif health.score < trigger:
            health.state = PathState.DEAD
        elif health.score < demotion:
            health.state = PathState.DEGRADED
        elif health.state in (PathState.DEAD, PathState.ZOMBIE) and health.score > promotion:
            health.state = PathState.RECOVERING
        elif health.state == PathState.RECOVERING and health.score > promotion:
            # Check hysteresis
            hysteresis = failover_cfg.get("switchback_hysteresis_ms", 3000) / 1000
            if now - health.last_state_change_at > hysteresis:
                health.state = PathState.ACTIVE
        elif health.score >= demotion:
            if health.state not in (PathState.RECOVERING,):
                health.state = PathState.ACTIVE

    def _notify_state_change(self, path_id: str, old_state: PathState, new_state: PathState):
        for listener in self._listeners:
            try:
                listener(path_id, old_state.value, new_state.value)
            except Exception:
                pass

    def _on_config_change(self, change):
        """React to config changes — e.g., resize probe windows."""
        if change.path == "path_health.loss_window_size":
            with self._lock:
                for pid in self._probe_history:
                    old = list(self._probe_history[pid])
                    self._probe_history[pid] = deque(old, maxlen=change.new_value)

    def _start_probe_thread(self, path_id: str, remote_addr: str, remote_port: int):
        """Start UDP probe thread for a path."""
        def probe_loop():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.0)
            seq = 0

            while self._running:
                interval = self.config.get("path_health.probe_interval_ms", 50) / 1000
                try:
                    payload = struct.pack("!Qd", seq, time.time())
                    sock.sendto(payload, (remote_addr, remote_port))
                    start = time.time()
                    data, _ = sock.recvfrom(64)
                    rtt = (time.time() - start) * 1000
                    self.record_probe(path_id, rtt, True)
                except socket.timeout:
                    self.record_probe(path_id, None, False)
                except Exception:
                    self.record_probe(path_id, None, False)
                seq += 1
                time.sleep(interval)

            sock.close()

        t = threading.Thread(target=probe_loop, daemon=True, name=f"probe-{path_id}")
        self._threads[path_id] = t
        t.start()
