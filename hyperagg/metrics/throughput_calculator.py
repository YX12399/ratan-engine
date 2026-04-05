"""
Effective Throughput Accounting.

Computes real throughput by subtracting FEC overhead, reorder buffer wait costs,
and duplicate waste from raw throughput.

  effective = raw * (1 - fec_overhead_ratio) - reorder_cost - duplicate_waste
  efficiency = effective / raw * 100%
"""

import logging
import time
from collections import deque

logger = logging.getLogger("hyperagg.metrics.throughput")


class ThroughputCalculator:
    """Computes raw, effective, and efficiency metrics."""

    def __init__(self, window_sec: float = 5.0):
        self._window_sec = window_sec

        # Raw bytes sent (all paths combined)
        self._sent_samples: deque = deque()  # (timestamp, bytes)
        # Parity/FEC bytes sent
        self._fec_bytes_samples: deque = deque()
        # Duplicate bytes (replication mode)
        self._dup_bytes_samples: deque = deque()
        # Reorder wait times (seconds each packet waited in buffer)
        self._reorder_waits: deque = deque()  # (timestamp, wait_sec)

        # Counters
        self._total_sent = 0
        self._total_fec = 0
        self._total_dup = 0
        self._total_reorder_wait = 0.0

    def record_sent(self, nbytes: int, is_fec_parity: bool = False,
                    is_duplicate: bool = False) -> None:
        """Record bytes sent."""
        now = time.monotonic()
        self._sent_samples.append((now, nbytes))
        self._total_sent += nbytes

        if is_fec_parity:
            self._fec_bytes_samples.append((now, nbytes))
            self._total_fec += nbytes
        if is_duplicate:
            self._dup_bytes_samples.append((now, nbytes))
            self._total_dup += nbytes

    def record_reorder_wait(self, wait_sec: float) -> None:
        """Record how long a packet waited in the reorder buffer."""
        now = time.monotonic()
        self._reorder_waits.append((now, wait_sec))
        self._total_reorder_wait += wait_sec

    def _prune(self) -> None:
        """Remove samples outside the window."""
        now = time.monotonic()
        cutoff = now - self._window_sec
        for dq in (self._sent_samples, self._fec_bytes_samples,
                    self._dup_bytes_samples, self._reorder_waits):
            while dq and dq[0][0] < cutoff:
                dq.popleft()

    def compute(self) -> dict:
        """
        Compute current throughput metrics.

        Returns:
            {
                "raw_throughput_mbps": float,
                "fec_overhead_ratio": float,
                "fec_overhead_mbps": float,
                "duplicate_waste_mbps": float,
                "reorder_cost_mbps": float,
                "effective_throughput_mbps": float,
                "efficiency_pct": float,
            }
        """
        self._prune()
        now = time.monotonic()

        # Raw throughput
        raw_bytes = sum(b for _, b in self._sent_samples)
        window = self._window_sec
        raw_bps = raw_bytes / window if window > 0 else 0
        raw_mbps = raw_bps * 8 / 1_000_000

        # FEC overhead
        fec_bytes = sum(b for _, b in self._fec_bytes_samples)
        fec_ratio = fec_bytes / max(raw_bytes, 1)
        fec_mbps = fec_bytes / window * 8 / 1_000_000 if window > 0 else 0

        # Duplicate waste
        dup_bytes = sum(b for _, b in self._dup_bytes_samples)
        dup_mbps = dup_bytes / window * 8 / 1_000_000 if window > 0 else 0

        # Reorder wait cost (throughput lost to waiting)
        if self._reorder_waits:
            avg_wait = sum(w for _, w in self._reorder_waits) / len(self._reorder_waits)
            reorder_cost_bps = avg_wait * raw_bps  # Effective throughput penalty
        else:
            avg_wait = 0
            reorder_cost_bps = 0
        reorder_cost_mbps = reorder_cost_bps * 8 / 1_000_000

        # Effective throughput
        effective_mbps = max(0, raw_mbps * (1 - fec_ratio) - reorder_cost_mbps - dup_mbps)

        # Efficiency
        efficiency = (effective_mbps / raw_mbps * 100) if raw_mbps > 0 else 0

        return {
            "raw_throughput_mbps": round(raw_mbps, 2),
            "fec_overhead_ratio": round(fec_ratio, 4),
            "fec_overhead_mbps": round(fec_mbps, 2),
            "duplicate_waste_mbps": round(dup_mbps, 2),
            "reorder_cost_mbps": round(reorder_cost_mbps, 4),
            "effective_throughput_mbps": round(effective_mbps, 2),
            "efficiency_pct": round(efficiency, 1),
        }

    def get_lifetime_stats(self) -> dict:
        """Get all-time totals."""
        return {
            "total_sent_mb": round(self._total_sent / 1_000_000, 2),
            "total_fec_mb": round(self._total_fec / 1_000_000, 2),
            "total_dup_mb": round(self._total_dup / 1_000_000, 2),
            "lifetime_fec_ratio": round(
                self._total_fec / max(self._total_sent, 1), 4
            ),
        }
