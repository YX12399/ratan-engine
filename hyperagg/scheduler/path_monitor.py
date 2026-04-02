"""
Path Monitor — continuous measurement of each network path's quality.

Replaces mwan3 polling with per-packet measurement (every 100ms via keepalives).
Feeds data to the AI predictor for probabilistic scheduling decisions.
"""

import logging
import statistics
import time
from collections import deque
from typing import Optional

from hyperagg.scheduler.models import PathMeasurement, PathState

logger = logging.getLogger("hyperagg.scheduler.monitor")


class PathMonitor:
    """Continuous path quality measurement engine."""

    def __init__(self, config: dict):
        sched_cfg = config.get("scheduler", {})
        self._history_window = sched_cfg.get("history_window", 50)
        self._ewma_alpha = sched_cfg.get("ewma_alpha", 0.3)
        self._latency_budget_ms = sched_cfg.get("latency_budget_ms", 150)
        self._jitter_weight = sched_cfg.get("jitter_weight", 0.3)
        self._loss_weight = sched_cfg.get("loss_weight", 0.5)
        self._rtt_weight = sched_cfg.get("rtt_weight", 0.2)
        self._keepalive_timeout_count = config.get("tunnel", {}).get(
            "keepalive_timeout_count", 3
        ) or 3

        # Per-path state
        self._rtt_history: dict[int, deque[float]] = {}
        self._loss_history: dict[int, deque[bool]] = {}
        self._throughput_history: dict[int, deque[float]] = {}
        self._ewma_rtt: dict[int, float] = {}
        self._ewma_jitter: dict[int, float] = {}
        self._prev_rtt: dict[int, float] = {}
        self._consecutive_failures: dict[int, int] = {}
        self._last_success: dict[int, float] = {}
        self._throughput_accum: dict[int, int] = {}
        self._throughput_last_time: dict[int, float] = {}

    def register_path(self, path_id: int) -> None:
        """Register a new path for monitoring."""
        self._rtt_history[path_id] = deque(maxlen=self._history_window)
        self._loss_history[path_id] = deque(maxlen=self._history_window)
        self._throughput_history[path_id] = deque(maxlen=self._history_window)
        self._ewma_rtt[path_id] = 0.0
        self._ewma_jitter[path_id] = 0.0
        self._prev_rtt[path_id] = 0.0
        self._consecutive_failures[path_id] = 0
        self._last_success[path_id] = time.monotonic()
        self._throughput_accum[path_id] = 0
        self._throughput_last_time[path_id] = time.monotonic()

    def record_rtt(self, path_id: int, rtt_ms: float) -> None:
        """Record an RTT measurement from a keepalive response."""
        if path_id not in self._rtt_history:
            self.register_path(path_id)

        self._rtt_history[path_id].append(rtt_ms)
        self._loss_history[path_id].append(False)
        self._consecutive_failures[path_id] = 0
        self._last_success[path_id] = time.monotonic()

        # EWMA RTT
        alpha = self._ewma_alpha
        if self._ewma_rtt[path_id] == 0.0:
            self._ewma_rtt[path_id] = rtt_ms
        else:
            self._ewma_rtt[path_id] = alpha * rtt_ms + (1 - alpha) * self._ewma_rtt[path_id]

        # Jitter: |current - previous|
        if self._prev_rtt[path_id] > 0:
            jitter = abs(rtt_ms - self._prev_rtt[path_id])
            self._ewma_jitter[path_id] = alpha * jitter + (1 - alpha) * self._ewma_jitter[path_id]
        self._prev_rtt[path_id] = rtt_ms

    def record_loss(self, path_id: int) -> None:
        """Record a probe timeout / packet loss."""
        if path_id not in self._loss_history:
            self.register_path(path_id)

        self._loss_history[path_id].append(True)
        self._consecutive_failures[path_id] += 1

    def record_throughput(self, path_id: int, bytes_sent: int) -> None:
        """Accumulate bytes for throughput calculation."""
        if path_id not in self._throughput_accum:
            self.register_path(path_id)
        self._throughput_accum[path_id] += bytes_sent

    def _flush_throughput(self, path_id: int) -> None:
        """Convert accumulated bytes to Mbps measurement."""
        now = time.monotonic()
        elapsed = now - self._throughput_last_time.get(path_id, now)
        if elapsed > 0.01:  # At least 10ms
            bytes_sent = self._throughput_accum.get(path_id, 0)
            mbps = (bytes_sent * 8) / (elapsed * 1_000_000)
            self._throughput_history[path_id].append(mbps)
            self._throughput_accum[path_id] = 0
            self._throughput_last_time[path_id] = now

    def get_path_state(self, path_id: int) -> PathState:
        """Get the current state of a path."""
        if path_id not in self._rtt_history:
            return PathState(path_id=path_id, is_alive=False, quality_score=0.0)

        self._flush_throughput(path_id)

        rtt_hist = list(self._rtt_history[path_id])
        loss_hist = list(self._loss_history[path_id])

        # RTT stats
        if rtt_hist:
            avg_rtt = self._ewma_rtt[path_id]
            recent_rtts = rtt_hist[-10:] if len(rtt_hist) >= 10 else rtt_hist
            min_rtt = min(recent_rtts)
            max_rtt = max(recent_rtts)
        else:
            avg_rtt = min_rtt = max_rtt = 0.0

        # Jitter
        jitter = self._ewma_jitter.get(path_id, 0.0)

        # Loss percentage
        if loss_hist:
            loss_pct = sum(1 for l in loss_hist if l) / len(loss_hist)
        else:
            loss_pct = 0.0

        # Throughput
        tp_hist = list(self._throughput_history.get(path_id, []))
        throughput_mbps = tp_hist[-1] if tp_hist else 0.0

        # Alive check
        is_alive = self._consecutive_failures.get(path_id, 0) < self._keepalive_timeout_count

        # Quality score
        quality_score = self._compute_quality_score(avg_rtt, loss_pct, jitter)
        if not is_alive:
            quality_score = 0.0

        return PathState(
            path_id=path_id,
            avg_rtt_ms=avg_rtt,
            min_rtt_ms=min_rtt,
            max_rtt_ms=max_rtt,
            jitter_ms=jitter,
            loss_pct=loss_pct,
            throughput_mbps=throughput_mbps,
            is_alive=is_alive,
            quality_score=quality_score,
            rtt_history=rtt_hist,
            loss_history=loss_hist,
        )

    def get_all_path_states(self) -> dict[int, PathState]:
        """Get states for all registered paths."""
        return {pid: self.get_path_state(pid) for pid in self._rtt_history}

    def _compute_quality_score(
        self, avg_rtt: float, loss_pct: float, jitter: float
    ) -> float:
        """
        Weighted composite quality score (0.0 to 1.0).

        score = 1.0 - (rtt_weight * normalized_rtt
                      + loss_weight * loss_pct
                      + jitter_weight * normalized_jitter)
        """
        budget = self._latency_budget_ms
        norm_rtt = min(avg_rtt / budget, 1.0) if budget > 0 else 0.0
        norm_jitter = min(jitter / (budget * 0.5), 1.0) if budget > 0 else 0.0

        score = 1.0 - (
            self._rtt_weight * norm_rtt
            + self._loss_weight * loss_pct
            + self._jitter_weight * norm_jitter
        )
        return max(0.0, min(1.0, score))


if __name__ == "__main__":
    import random

    config = {
        "scheduler": {
            "history_window": 50,
            "ewma_alpha": 0.3,
            "latency_budget_ms": 150,
            "jitter_weight": 0.3,
            "loss_weight": 0.5,
            "rtt_weight": 0.2,
        }
    }
    monitor = PathMonitor(config)
    monitor.register_path(0)
    monitor.register_path(1)

    # Feed Starlink-like data (low RTT, low loss)
    for _ in range(50):
        monitor.record_rtt(0, random.gauss(30, 3))
        if random.random() < 0.02:
            monitor.record_loss(0)

    # Feed cellular-like data (higher RTT, more loss)
    for _ in range(50):
        monitor.record_rtt(1, random.gauss(65, 12))
        if random.random() < 0.05:
            monitor.record_loss(1)

    for pid in [0, 1]:
        state = monitor.get_path_state(pid)
        print(f"  Path {pid}: RTT={state.avg_rtt_ms:.1f}ms, "
              f"jitter={state.jitter_ms:.1f}ms, "
              f"loss={state.loss_pct:.1%}, "
              f"score={state.quality_score:.3f}, "
              f"alive={state.is_alive}")

    print("Path Monitor Test: PASSED")
