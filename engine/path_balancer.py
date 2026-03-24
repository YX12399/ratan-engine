"""
Path Balancer — the core aggregation engine.
Decides how to distribute traffic across MPTCP subflows based on health scores,
configured weights, and scheduler mode.

This is the heart of RATAN. It takes health data from HealthMonitor and produces
scheduling directives that tunnel_server and tunnel_client consume.
"""

import time
import threading
from enum import Enum
from dataclasses import dataclass
from typing import Optional


class SchedulerMode(Enum):
    WEIGHTED = "weighted"       # Static weights adjusted by health
    MIN_RTT = "min_rtt"         # Always prefer lowest RTT path
    REDUNDANT = "redundant"     # Send on all paths (max reliability, wastes bandwidth)
    ADAPTIVE = "adaptive"       # Dynamically adjust weights based on throughput feedback


@dataclass
class SchedulingDirective:
    """What the balancer tells the tunnel layer to do."""
    path_id: str
    weight: float               # 0-1, share of traffic
    active: bool                # Whether to use this path at all
    reinjection: bool           # Whether to reinject lost packets on this path
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return {
            "path_id": self.path_id,
            "weight": round(self.weight, 4),
            "active": self.active,
            "reinjection": self.reinjection,
            "timestamp": self.timestamp,
        }


@dataclass
class AggregationMetrics:
    """Real-time aggregation performance metrics."""
    combined_throughput_mbps: float = 0.0
    effective_rtt_ms: float = 0.0
    active_paths: int = 0
    total_paths: int = 0
    reinjection_rate_pct: float = 0.0
    failover_count: int = 0
    last_failover_at: float = 0.0
    uptime_pct: float = 100.0

    def to_dict(self) -> dict:
        return {
            "combined_throughput_mbps": round(self.combined_throughput_mbps, 2),
            "effective_rtt_ms": round(self.effective_rtt_ms, 2),
            "active_paths": self.active_paths,
            "total_paths": self.total_paths,
            "reinjection_rate_pct": round(self.reinjection_rate_pct, 2),
            "failover_count": self.failover_count,
            "last_failover_at": self.last_failover_at,
            "uptime_pct": round(self.uptime_pct, 2),
        }


class PathBalancer:
    """
    Consumes health scores, applies scheduling policy, emits directives.
    Runs on both VPS (for server-side scheduling) and edge (for client hints).
    """

    def __init__(self, config_store, health_monitor):
        self.config = config_store
        self.health = health_monitor
        self._directives: dict[str, SchedulingDirective] = {}
        self._metrics = AggregationMetrics()
        self._lock = threading.Lock()
        self._running = False
        self._rebalance_thread: Optional[threading.Thread] = None

        # Track throughput per path for adaptive mode
        self._throughput_samples: dict[str, list[float]] = {}
        self._bytes_sent: dict[str, int] = {}
        self._last_sample_time: dict[str, float] = {}

        # Listen for state changes
        self.health.on_state_change(self._on_path_state_change)
        self.config.subscribe(self._on_config_change)

    def start(self):
        """Start the rebalancing loop."""
        self._running = True
        self._rebalance_thread = threading.Thread(
            target=self._rebalance_loop, daemon=True, name="path-balancer"
        )
        self._rebalance_thread.start()

    def stop(self):
        self._running = False

    def get_directives(self) -> dict[str, dict]:
        """Get current scheduling directives for all paths."""
        with self._lock:
            return {pid: d.to_dict() for pid, d in self._directives.items()}

    def get_directive(self, path_id: str) -> Optional[dict]:
        with self._lock:
            if path_id in self._directives:
                return self._directives[path_id].to_dict()
        return None

    def get_metrics(self) -> dict:
        with self._lock:
            return self._metrics.to_dict()

    def record_bytes(self, path_id: str, nbytes: int):
        """Record bytes sent on a path — for throughput calculation."""
        with self._lock:
            self._bytes_sent.setdefault(path_id, 0)
            self._bytes_sent[path_id] += nbytes

    def rebalance(self):
        """Force an immediate rebalance."""
        self._do_rebalance()

    def _rebalance_loop(self):
        """Periodic rebalance based on health scores."""
        while self._running:
            self._do_rebalance()
            # Rebalance at 2x probe interval for responsiveness
            interval = self.config.get("path_health.probe_interval_ms", 50) / 1000 * 2
            time.sleep(max(interval, 0.05))

    def _do_rebalance(self):
        """Core scheduling logic."""
        with self._lock:
            scores = self.health.get_scores()
            if not scores:
                return

            sched_cfg = self.config.get("mptcp_scheduling", {})
            failover_cfg = self.config.get("failover", {})
            mode = SchedulerMode(sched_cfg.get("scheduler_mode", "weighted"))
            configured_weights = sched_cfg.get("subflow_weights", {})
            demotion = sched_cfg.get("path_demotion_threshold", 0.3)
            trigger = failover_cfg.get("trigger_threshold", 0.2)
            reinjection_thresh = sched_cfg.get("reinjection_threshold_ms", 200)

            all_health = self.health.get_all_health()

            if mode == SchedulerMode.WEIGHTED:
                self._schedule_weighted(scores, configured_weights, demotion, trigger, reinjection_thresh, all_health)
            elif mode == SchedulerMode.MIN_RTT:
                self._schedule_min_rtt(scores, demotion, trigger, reinjection_thresh, all_health)
            elif mode == SchedulerMode.REDUNDANT:
                self._schedule_redundant(scores, trigger, all_health)
            elif mode == SchedulerMode.ADAPTIVE:
                self._schedule_adaptive(scores, configured_weights, demotion, trigger, reinjection_thresh, all_health)

            # Update aggregation metrics
            self._update_metrics(all_health)

    def _schedule_weighted(self, scores, configured_weights, demotion, trigger, reinject_thresh, all_health):
        """Weighted scheduling — configured weights modulated by health score."""
        active_paths = {}
        for pid, score in scores.items():
            if score > trigger:
                base_weight = configured_weights.get(pid, 1.0 / len(scores))
                # Modulate weight by health: healthy paths get full weight, degraded get less
                effective_weight = base_weight * score
                active_paths[pid] = effective_weight

        # Normalize weights to sum to 1
        total = sum(active_paths.values()) or 1
        now = time.time()

        for pid, score in scores.items():
            health_data = all_health.get(pid, {})
            rtt = health_data.get("rtt_avg_ms", 0)

            if pid in active_paths:
                self._directives[pid] = SchedulingDirective(
                    path_id=pid,
                    weight=active_paths[pid] / total,
                    active=True,
                    reinjection=rtt > reinject_thresh,
                    timestamp=now,
                )
            else:
                self._directives[pid] = SchedulingDirective(
                    path_id=pid,
                    weight=0.0,
                    active=False,
                    reinjection=False,
                    timestamp=now,
                )

    def _schedule_min_rtt(self, scores, demotion, trigger, reinject_thresh, all_health):
        """Min-RTT scheduling — all traffic to lowest RTT path, others as backup."""
        now = time.time()
        candidates = []
        for pid, score in scores.items():
            if score > trigger:
                health_data = all_health.get(pid, {})
                rtt = health_data.get("rtt_avg_ms", 999)
                candidates.append((pid, rtt, score))

        candidates.sort(key=lambda x: x[1])  # Sort by RTT ascending

        for i, (pid, rtt, score) in enumerate(candidates):
            if i == 0:
                self._directives[pid] = SchedulingDirective(
                    path_id=pid, weight=1.0, active=True,
                    reinjection=False, timestamp=now,
                )
            else:
                # Backup paths get minimal weight for keep-alive
                self._directives[pid] = SchedulingDirective(
                    path_id=pid, weight=0.0, active=False,
                    reinjection=False, timestamp=now,
                )

        # Dead paths
        for pid in scores:
            if pid not in {c[0] for c in candidates}:
                self._directives[pid] = SchedulingDirective(
                    path_id=pid, weight=0.0, active=False,
                    reinjection=False, timestamp=now,
                )

    def _schedule_redundant(self, scores, trigger, all_health):
        """Redundant scheduling — send on all active paths."""
        now = time.time()
        active = [pid for pid, s in scores.items() if s > trigger]
        for pid in scores:
            self._directives[pid] = SchedulingDirective(
                path_id=pid,
                weight=1.0 if pid in active else 0.0,
                active=pid in active,
                reinjection=True,
                timestamp=now,
            )

    def _schedule_adaptive(self, scores, configured_weights, demotion, trigger, reinject_thresh, all_health):
        """
        Adaptive scheduling — starts from configured weights, then adjusts
        based on measured throughput. Paths delivering more throughput get more weight.
        """
        now = time.time()
        # Calculate throughput per path
        throughputs = {}
        for pid in scores:
            bytes_sent = self._bytes_sent.get(pid, 0)
            last_time = self._last_sample_time.get(pid, now - 1)
            elapsed = now - last_time
            if elapsed > 0:
                throughputs[pid] = (bytes_sent * 8) / (elapsed * 1_000_000)  # Mbps
            else:
                throughputs[pid] = 0
            self._bytes_sent[pid] = 0
            self._last_sample_time[pid] = now

        # Blend configured weights with throughput-adjusted weights
        active_paths = {}
        for pid, score in scores.items():
            if score > trigger:
                base = configured_weights.get(pid, 1.0 / len(scores))
                tp_factor = throughputs.get(pid, 0) / max(sum(throughputs.values()), 0.001)
                # 70% configured + 30% throughput-derived
                effective = 0.7 * base * score + 0.3 * tp_factor
                active_paths[pid] = max(effective, 0.01)

        total = sum(active_paths.values()) or 1
        for pid, score in scores.items():
            health_data = all_health.get(pid, {})
            rtt = health_data.get("rtt_avg_ms", 0)
            if pid in active_paths:
                self._directives[pid] = SchedulingDirective(
                    path_id=pid,
                    weight=active_paths[pid] / total,
                    active=True,
                    reinjection=rtt > reinject_thresh,
                    timestamp=now,
                )
            else:
                self._directives[pid] = SchedulingDirective(
                    path_id=pid, weight=0.0, active=False,
                    reinjection=False, timestamp=now,
                )

    def _update_metrics(self, all_health):
        """Update aggregation metrics."""
        active = [h for h in all_health.values() if h.get("state") == "active"]
        self._metrics.active_paths = len(active)
        self._metrics.total_paths = len(all_health)

        if active:
            rtts = [h.get("rtt_avg_ms", 0) for h in active]
            weights = [self._directives.get(h["path_id"], SchedulingDirective("", 0, False, False)).weight for h in active]
            # Effective RTT is weighted average
            total_w = sum(weights) or 1
            self._metrics.effective_rtt_ms = sum(r * w for r, w in zip(rtts, weights)) / total_w

    def _on_path_state_change(self, path_id, old_state, new_state):
        """React to path state changes — trigger immediate rebalance."""
        if new_state in ("dead", "zombie"):
            self._metrics.failover_count += 1
            self._metrics.last_failover_at = time.time()
        self._do_rebalance()

    def _on_config_change(self, change):
        """React to config changes — rebalance immediately."""
        if change.path.startswith("mptcp_scheduling") or change.path.startswith("failover"):
            self._do_rebalance()
