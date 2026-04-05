"""
Adaptive Reorder Buffer — adjusts timeout based on real path differentials.

Instead of a fixed 100ms timeout, computes:
  adaptive_timeout = path_differential + jitter_margin
where:
  path_differential = max(ewma_rtt) - min(ewma_rtt) across all paths
  jitter_margin = 2 * max(ewma_jitter) across all paths

Clamped to [10ms, 500ms]. Tracks late deliveries as feedback signal.
"""

import logging
import time
from collections import deque
from typing import Optional

logger = logging.getLogger("hyperagg.tunnel.reorder")


class AdaptiveReorderBuffer:
    """Reorder buffer with adaptive timeout based on path RTT differentials."""

    def __init__(self, window_size: int = 1024, min_timeout_ms: float = 10.0,
                 max_timeout_ms: float = 500.0, initial_timeout_ms: float = 100.0):
        self._window = window_size
        self._min_timeout = min_timeout_ms
        self._max_timeout = max_timeout_ms
        self._timeout_ms = initial_timeout_ms

        self._buffer: dict[int, bytes] = {}
        self._next_seq = 0
        self._timestamps: dict[int, float] = {}
        self._seen: set[int] = set()

        # Late delivery tracking
        self._recent_deliveries: deque[bool] = deque(maxlen=100)  # True = on-time
        self._late_count = 0
        self._total_delivered = 0

        # Flushed sequences (for detecting late arrivals)
        self._flushed_seqs: set[int] = set()
        self._flushed_max = 0

    @property
    def timeout_ms(self) -> float:
        return self._timeout_ms

    @property
    def depth(self) -> int:
        return len(self._buffer)

    @property
    def late_delivery_pct(self) -> float:
        if not self._recent_deliveries:
            return 0.0
        late = sum(1 for x in self._recent_deliveries if not x)
        return late / len(self._recent_deliveries)

    def update_timeout(self, path_rtts: dict[int, float],
                       path_jitters: dict[int, float]) -> float:
        """
        Recompute adaptive timeout from current path metrics.

        Args:
            path_rtts: {path_id: ewma_rtt_ms} for alive paths
            path_jitters: {path_id: ewma_jitter_ms} for alive paths

        Returns:
            New timeout in milliseconds.
        """
        if not path_rtts or len(path_rtts) < 2:
            return self._timeout_ms

        rtt_values = [r for r in path_rtts.values() if r > 0]
        jitter_values = [j for j in path_jitters.values() if j > 0]

        if not rtt_values:
            return self._timeout_ms

        path_differential = max(rtt_values) - min(rtt_values)
        jitter_margin = 2.0 * max(jitter_values) if jitter_values else 0.0

        new_timeout = path_differential + jitter_margin

        # Late delivery feedback: if >5% late in last 100, increase by 20%
        if self.late_delivery_pct > 0.05:
            new_timeout *= 1.2
            logger.debug(f"Reorder: late deliveries {self.late_delivery_pct:.1%}, "
                         f"increasing timeout")

        # Clamp
        new_timeout = max(self._min_timeout, min(self._max_timeout, new_timeout))

        if abs(new_timeout - self._timeout_ms) > 2.0:
            logger.info(f"Reorder timeout: {self._timeout_ms:.0f}ms → {new_timeout:.0f}ms "
                        f"(diff={path_differential:.0f}, jitter_margin={jitter_margin:.0f})")

        self._timeout_ms = new_timeout
        return new_timeout

    def is_duplicate(self, global_seq: int) -> bool:
        if global_seq < self._next_seq:
            return True
        return global_seq in self._seen

    def is_late(self, global_seq: int) -> bool:
        """Check if this packet arrived after its slot was already flushed."""
        return global_seq < self._next_seq

    def insert(self, global_seq: int, payload: bytes) -> list[bytes]:
        """Insert a packet and return any deliverable in order."""
        if self.is_duplicate(global_seq):
            # Track late arrivals
            if global_seq in self._flushed_seqs:
                self._late_count += 1
                self._recent_deliveries.append(False)
            return []

        self._seen.add(global_seq)
        if len(self._seen) > self._window * 2:
            cutoff = self._next_seq
            self._seen = {s for s in self._seen if s >= cutoff}

        self._buffer[global_seq] = payload
        self._timestamps[global_seq] = time.monotonic()

        delivered = self._deliver_in_order()
        for _ in delivered:
            self._recent_deliveries.append(True)
            self._total_delivered += 1
        return delivered

    def check_timeouts(self) -> list[bytes]:
        """Deliver buffered packets that have waited past the adaptive timeout."""
        now = time.monotonic()
        cutoff = now - (self._timeout_ms / 1000.0)
        delivered = []

        while self._next_seq not in self._buffer:
            if not self._buffer:
                break
            min_seq = min(self._buffer.keys())
            ts = self._timestamps.get(min_seq, now)
            if ts > cutoff:
                break
            # Timeout: skip missing seq, record as flushed
            self._flushed_seqs.add(self._next_seq)
            if len(self._flushed_seqs) > self._window:
                min_flushed = min(self._flushed_seqs)
                self._flushed_seqs.discard(min_flushed)
            self._next_seq += 1

        delivered.extend(self._deliver_in_order())
        for _ in delivered:
            self._recent_deliveries.append(True)
            self._total_delivered += 1
        return delivered

    def _deliver_in_order(self) -> list[bytes]:
        delivered = []
        while self._next_seq in self._buffer:
            delivered.append(self._buffer.pop(self._next_seq))
            self._timestamps.pop(self._next_seq, None)
            self._next_seq += 1
        return delivered

    def get_stats(self) -> dict:
        return {
            "timeout_ms": round(self._timeout_ms, 1),
            "depth": self.depth,
            "late_delivery_pct": round(self.late_delivery_pct * 100, 1),
            "total_delivered": self._total_delivered,
            "late_count": self._late_count,
        }
