"""
Adaptive FEC Group Sizing with burst-loss-aware interleaving.

Detects burst loss patterns and adjusts FEC parameters:
- p95_burst_length <= 1: XOR group=4
- p95_burst_length == 2: RS(8,3)
- p95_burst_length 3-4: RS(6,4)
- p95_burst_length >= 5: Full replication

Also implements cross-group interleaving to spread burst losses
across multiple FEC groups.
"""

import logging
from collections import deque
from typing import Optional

logger = logging.getLogger("hyperagg.fec.adaptive")


class BurstDetector:
    """Tracks packet loss patterns to detect burst losses."""

    def __init__(self, window_size: int = 1000):
        self._window = window_size
        self._events: deque[bool] = deque(maxlen=window_size)  # True=received, False=lost
        self._burst_lengths: deque[int] = deque(maxlen=200)
        self._current_burst = 0

    def record_received(self) -> None:
        """Record a successfully received packet."""
        self._events.append(True)
        if self._current_burst > 0:
            self._burst_lengths.append(self._current_burst)
            self._current_burst = 0

    def record_lost(self) -> None:
        """Record a lost packet."""
        self._events.append(False)
        self._current_burst += 1

    @property
    def p95_burst_length(self) -> int:
        """95th percentile of burst lengths."""
        if not self._burst_lengths:
            return 0
        sorted_bursts = sorted(self._burst_lengths)
        idx = int(len(sorted_bursts) * 0.95)
        return sorted_bursts[min(idx, len(sorted_bursts) - 1)]

    @property
    def avg_burst_length(self) -> float:
        if not self._burst_lengths:
            return 0.0
        return sum(self._burst_lengths) / len(self._burst_lengths)

    @property
    def loss_rate(self) -> float:
        if not self._events:
            return 0.0
        return sum(1 for e in self._events if not e) / len(self._events)

    @property
    def burst_count(self) -> int:
        return len(self._burst_lengths)

    def get_stats(self) -> dict:
        return {
            "p95_burst_length": self.p95_burst_length,
            "avg_burst_length": round(self.avg_burst_length, 1),
            "burst_count": self.burst_count,
            "loss_rate": round(self.loss_rate, 4),
            "window_size": len(self._events),
        }


class AdaptiveFecSizer:
    """Selects FEC parameters based on observed burst loss patterns."""

    def __init__(self):
        self._detector = BurstDetector()
        self._current_mode = "xor"
        self._current_data_shards = 4
        self._current_parity_shards = 1
        self._interleave_enabled = False
        self._interleave_depth = 1

    def record_received(self) -> None:
        self._detector.record_received()

    def record_lost(self) -> None:
        self._detector.record_lost()

    def update(self) -> dict:
        """
        Recompute FEC parameters from observed burst patterns.

        Returns:
            {"mode": str, "data_shards": int, "parity_shards": int,
             "interleave": bool, "interleave_depth": int, "changed": bool}
        """
        p95 = self._detector.p95_burst_length
        old_mode = self._current_mode

        if p95 <= 1:
            self._current_mode = "xor"
            self._current_data_shards = 4
            self._current_parity_shards = 1
            self._interleave_enabled = False
            self._interleave_depth = 1
        elif p95 == 2:
            self._current_mode = "reed_solomon"
            self._current_data_shards = 8
            self._current_parity_shards = 3
            self._interleave_enabled = True
            self._interleave_depth = 2
        elif p95 <= 4:
            self._current_mode = "reed_solomon"
            self._current_data_shards = 6
            self._current_parity_shards = 4
            self._interleave_enabled = True
            self._interleave_depth = p95
        else:
            self._current_mode = "replicate"
            self._current_data_shards = 1
            self._current_parity_shards = 0
            self._interleave_enabled = False
            self._interleave_depth = 1

        changed = self._current_mode != old_mode
        if changed:
            logger.info(
                f"Adaptive FEC: {old_mode} → {self._current_mode} "
                f"(p95_burst={p95}, data={self._current_data_shards}, "
                f"parity={self._current_parity_shards}, "
                f"interleave={self._interleave_enabled})"
            )

        return {
            "mode": self._current_mode,
            "data_shards": self._current_data_shards,
            "parity_shards": self._current_parity_shards,
            "interleave": self._interleave_enabled,
            "interleave_depth": self._interleave_depth,
            "changed": changed,
        }

    @property
    def overhead_pct(self) -> float:
        if self._current_mode == "replicate":
            return 100.0
        if self._current_data_shards == 0:
            return 0.0
        return 100.0 * self._current_parity_shards / self._current_data_shards

    def get_stats(self) -> dict:
        return {
            "mode": self._current_mode,
            "data_shards": self._current_data_shards,
            "parity_shards": self._current_parity_shards,
            "overhead_pct": round(self.overhead_pct, 1),
            "interleave_enabled": self._interleave_enabled,
            "interleave_depth": self._interleave_depth,
            "burst_stats": self._detector.get_stats(),
        }


class Interleaver:
    """
    Cross-group packet interleaver.

    Instead of sending [A0,A1,A2,A3,A_parity,B0,B1,...],
    sends [A0,B0,A1,B1,A2,B2,...] so a burst loss hits
    different groups instead of destroying one group.
    """

    def __init__(self, depth: int = 2):
        self._depth = max(1, depth)
        self._groups: list[list] = []
        self._current_group: list = []

    def set_depth(self, depth: int) -> None:
        self._depth = max(1, depth)

    def add_packet(self, group_id: int, packet_data) -> list:
        """
        Add a packet. Returns interleaved packets when enough groups accumulate.

        Returns:
            List of packets in interleaved order, or empty if still accumulating.
        """
        # Find or create group
        group = None
        for g in self._groups:
            if g and g[0][0] == group_id:
                group = g
                break
        if group is None:
            group = []
            self._groups.append(group)

        group.append((group_id, packet_data))

        # When we have enough groups, interleave and flush
        if len(self._groups) >= self._depth:
            return self._flush()

        return []

    def flush(self) -> list:
        """Force flush all accumulated packets in interleaved order."""
        if not self._groups:
            return []
        return self._flush()

    def _flush(self) -> list:
        """Interleave packets across groups and return."""
        max_len = max(len(g) for g in self._groups) if self._groups else 0
        result = []
        for i in range(max_len):
            for group in self._groups:
                if i < len(group):
                    result.append(group[i][1])
        self._groups = []
        return result
