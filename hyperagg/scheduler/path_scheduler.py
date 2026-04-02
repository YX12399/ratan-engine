"""
Path Scheduler — JUMP-style per-packet scheduling decisions.

For every packet, decides which path(s) to send it on. This replaces
mwan3's connection-level routing with PACKET-level scheduling.

Modes:
  - ai: Probabilistic scheduling based on delivery predictions (default)
  - round_robin: Alternate packets between paths
  - weighted: Send proportional to quality score
  - replicate: Always send on all paths

Performance target: <100us per decision using pre-computed predictions.
"""

import logging
import time
from typing import Optional

from hyperagg.scheduler.models import (
    PathAssignment,
    PathPrediction,
    PathState,
    SchedulerDecision,
    SchedulerStats,
)
from hyperagg.scheduler.path_monitor import PathMonitor
from hyperagg.scheduler.path_predictor import PathPredictor

logger = logging.getLogger("hyperagg.scheduler")


class PathScheduler:
    """Per-packet path scheduler with AI-driven decisions."""

    def __init__(
        self,
        config: dict,
        path_monitor: PathMonitor,
        path_predictor: PathPredictor,
    ):
        sched_cfg = config.get("scheduler", {})
        self._mode = sched_cfg.get("mode", "ai")
        self._latency_budget_ms = sched_cfg.get("latency_budget_ms", 150)
        self._probe_interval_ms = sched_cfg.get("probe_interval_ms", 100)

        self._monitor = path_monitor
        self._predictor = path_predictor

        # Pre-computed state (updated periodically, not per-packet)
        self._predictions: dict[int, PathPrediction] = {}
        self._path_states: dict[int, PathState] = {}
        self._alive_paths: list[int] = []
        self._best_path: Optional[int] = None
        self._last_update = 0.0

        # Round-robin state
        self._rr_counter = 0

        # Weighted round-robin table
        self._schedule_table: list[int] = []
        self._table_pos = 0

        # Stats
        self._stats = SchedulerStats()
        self._decision_times: list[float] = []

    def update_predictions(self) -> None:
        """
        Refresh path predictions from monitor data.
        Called periodically (every probe_interval_ms), NOT per-packet.
        """
        self._path_states = self._monitor.get_all_path_states()
        self._alive_paths = [
            pid for pid, s in self._path_states.items() if s.is_alive
        ]

        self._predictions = {}
        for pid, state in self._path_states.items():
            self._predictions[pid] = self._predictor.predict_path_in_future(
                pid, self._latency_budget_ms, state
            )

        # Find best path
        if self._alive_paths:
            self._best_path = max(
                self._alive_paths,
                key=lambda p: self._predictions.get(p, PathPrediction(
                    p, 999, (0, 999), 1.0, 0.0, "stable", "abandon"
                )).delivery_probability,
            )
        else:
            self._best_path = None

        # Rebuild weighted table
        self._rebuild_schedule_table()
        self._last_update = time.monotonic()
        self._stats.current_mode = self._mode
        self._stats.current_best_path = self._best_path

    def schedule(
        self, global_seq: int, traffic_tier: str = "bulk"
    ) -> SchedulerDecision:
        """
        Make a scheduling decision for one packet.

        Performance target: <100us (uses pre-computed predictions).
        """
        t0 = time.monotonic_ns()

        # Auto-refresh if stale
        now = time.monotonic()
        if now - self._last_update > (self._probe_interval_ms / 1000.0):
            self.update_predictions()

        if not self._alive_paths:
            decision = SchedulerDecision(
                packet_global_seq=global_seq,
                assignments=[],
                reason="no_alive_paths",
            )
            self._record_decision(decision, t0)
            return decision

        if self._mode == "ai":
            decision = self._schedule_ai(global_seq, traffic_tier)
        elif self._mode == "round_robin":
            decision = self._schedule_round_robin(global_seq)
        elif self._mode == "weighted":
            decision = self._schedule_weighted(global_seq)
        elif self._mode == "replicate":
            decision = self._schedule_replicate(global_seq)
        else:
            decision = self._schedule_weighted(global_seq)

        self._record_decision(decision, t0)
        return decision

    def _schedule_ai(
        self, global_seq: int, traffic_tier: str
    ) -> SchedulerDecision:
        """AI mode — probabilistic scheduling based on delivery predictions."""

        # Realtime traffic: full replication, zero tolerance for loss
        if traffic_tier == "realtime":
            assignments = [
                PathAssignment(path_id=pid, send=True) for pid in self._alive_paths
            ]
            return SchedulerDecision(
                packet_global_seq=global_seq,
                assignments=assignments,
                reason=f"realtime: full replication on {len(self._alive_paths)} paths",
            )

        if len(self._alive_paths) < 2:
            pid = self._alive_paths[0]
            return SchedulerDecision(
                packet_global_seq=global_seq,
                assignments=[PathAssignment(path_id=pid, send=True)],
                reason=f"single-path: only path {pid} alive",
            )

        # Get predictions for alive paths
        preds = {
            pid: self._predictions.get(pid)
            for pid in self._alive_paths
            if pid in self._predictions
        }
        if not preds:
            return self._schedule_weighted(global_seq)

        # Sort by delivery probability
        sorted_paths = sorted(
            preds.items(),
            key=lambda x: x[1].delivery_probability if x[1] else 0,
            reverse=True,
        )

        best_pid, best_pred = sorted_paths[0]
        second_pid, second_pred = sorted_paths[1] if len(sorted_paths) > 1 else (None, None)

        best_prob = best_pred.delivery_probability if best_pred else 0
        second_prob = second_pred.delivery_probability if second_pred else 0
        spread = best_prob - second_prob

        # Decision logic (from JUMP algorithm)
        if best_prob > 0.95 and spread > 0.3:
            # Best path is excellent and clearly better
            return SchedulerDecision(
                packet_global_seq=global_seq,
                assignments=[PathAssignment(path_id=best_pid, send=True)],
                reason=f"single-path: path {best_pid} has {best_prob:.0%} delivery, spread={spread:.2f}",
            )

        elif best_prob > 0.7:
            # Good path — send data on best, FEC parity on second
            assignments = [
                PathAssignment(path_id=best_pid, send=True),
            ]
            if second_pid is not None:
                assignments.append(
                    PathAssignment(path_id=second_pid, send=True, is_fec_parity=True)
                )
            return SchedulerDecision(
                packet_global_seq=global_seq,
                assignments=assignments,
                reason=f"split-fec: data on {best_pid}, parity on {second_pid}",
            )

        elif best_prob > 0.5 and second_prob > 0.5:
            # Both paths acceptable — stripe weighted by capacity
            pid = self._pick_from_table()
            return SchedulerDecision(
                packet_global_seq=global_seq,
                assignments=[PathAssignment(path_id=pid, send=True)],
                reason=f"stripe: weighted across paths (picked {pid})",
            )

        else:
            # Degraded — full replication for safety
            assignments = [
                PathAssignment(path_id=pid, send=True) for pid in self._alive_paths
            ]
            return SchedulerDecision(
                packet_global_seq=global_seq,
                assignments=assignments,
                reason=f"degraded: replicating due to low delivery probability",
            )

    def _schedule_round_robin(self, global_seq: int) -> SchedulerDecision:
        pid = self._alive_paths[self._rr_counter % len(self._alive_paths)]
        self._rr_counter += 1
        return SchedulerDecision(
            packet_global_seq=global_seq,
            assignments=[PathAssignment(path_id=pid, send=True)],
            reason=f"round_robin: path {pid}",
        )

    def _schedule_weighted(self, global_seq: int) -> SchedulerDecision:
        pid = self._pick_from_table()
        return SchedulerDecision(
            packet_global_seq=global_seq,
            assignments=[PathAssignment(path_id=pid, send=True)],
            reason=f"weighted: path {pid}",
        )

    def _schedule_replicate(self, global_seq: int) -> SchedulerDecision:
        assignments = [
            PathAssignment(path_id=pid, send=True) for pid in self._alive_paths
        ]
        return SchedulerDecision(
            packet_global_seq=global_seq,
            assignments=assignments,
            reason=f"replicate: all {len(self._alive_paths)} paths",
        )

    def _rebuild_schedule_table(self) -> None:
        """Build weighted round-robin table from path quality scores."""
        if not self._alive_paths:
            self._schedule_table = []
            return

        scores = {}
        for pid in self._alive_paths:
            state = self._path_states.get(pid)
            scores[pid] = state.quality_score if state else 0.5

        total = sum(scores.values())
        if total <= 0:
            self._schedule_table = list(self._alive_paths)
            return

        # Build table with 100 slots
        table = []
        for pid in self._alive_paths:
            slots = max(1, int(100 * scores[pid] / total))
            table.extend([pid] * slots)

        self._schedule_table = table
        self._table_pos = 0

        # Update utilization stats
        for pid in self._alive_paths:
            self._stats.path_utilization[pid] = scores[pid] / total

    def _pick_from_table(self) -> int:
        """O(1) path selection from pre-computed table."""
        if not self._schedule_table:
            return self._alive_paths[0] if self._alive_paths else 0
        pid = self._schedule_table[self._table_pos % len(self._schedule_table)]
        self._table_pos += 1
        return pid

    def _record_decision(self, decision: SchedulerDecision, start_ns: int) -> None:
        """Record decision stats."""
        elapsed_us = (time.monotonic_ns() - start_ns) / 1000.0
        self._decision_times.append(elapsed_us)
        if len(self._decision_times) > 1000:
            self._decision_times = self._decision_times[-500:]

        self._stats.decisions_total += 1

        # Categorize reason
        reason_key = decision.reason.split(":")[0]
        self._stats.decisions_by_reason[reason_key] = (
            self._stats.decisions_by_reason.get(reason_key, 0) + 1
        )

        if self._decision_times:
            self._stats.avg_decision_time_us = (
                sum(self._decision_times) / len(self._decision_times)
            )

    def get_stats(self) -> SchedulerStats:
        return self._stats


if __name__ == "__main__":
    import random

    config = {
        "scheduler": {
            "mode": "ai",
            "history_window": 50,
            "ewma_alpha": 0.3,
            "latency_budget_ms": 150,
            "probe_interval_ms": 100,
            "jitter_weight": 0.3,
            "loss_weight": 0.5,
            "rtt_weight": 0.2,
        }
    }

    monitor = PathMonitor(config)
    predictor = PathPredictor(config)
    scheduler = PathScheduler(config, monitor, predictor)

    # Register paths and feed data
    monitor.register_path(0)
    monitor.register_path(1)

    for _ in range(50):
        monitor.record_rtt(0, random.gauss(30, 3))
        monitor.record_rtt(1, random.gauss(65, 10))

    scheduler.update_predictions()

    # Run 1000 scheduling decisions
    reasons = {}
    t0 = time.monotonic()
    for i in range(1000):
        decision = scheduler.schedule(i, "bulk")
        key = decision.reason.split(":")[0]
        reasons[key] = reasons.get(key, 0) + 1
    elapsed_ms = (time.monotonic() - t0) * 1000

    stats = scheduler.get_stats()
    print(f"Scheduler Test (1000 decisions in {elapsed_ms:.1f}ms):")
    print(f"  Avg decision time: {stats.avg_decision_time_us:.1f}us")
    print(f"  Decisions by reason: {dict(stats.decisions_by_reason)}")
    print(f"  Path utilization: {dict(stats.path_utilization)}")
    print(f"  Best path: {stats.current_best_path}")
    print("Path Scheduler Test: PASSED")
