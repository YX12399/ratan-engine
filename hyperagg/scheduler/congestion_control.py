"""
Per-Path AIMD Congestion Control with per-tier bandwidth reservation.

AIMD (Additive Increase Multiplicative Decrease):
  - On success (no loss for 100ms): send_rate += alpha
  - On loss: send_rate *= beta (0.5)

Per-tier guarantees:
  - Realtime: 2 Mbps minimum
  - Streaming: 5 Mbps minimum
  - Bulk: gets remainder
"""

import logging
import time
from typing import Optional

logger = logging.getLogger("hyperagg.scheduler.cc")

# Tier bandwidth guarantees in bytes/sec
TIER_GUARANTEES = {
    "realtime": 2_000_000 / 8,   # 2 Mbps = 250 KB/s
    "streaming": 5_000_000 / 8,  # 5 Mbps = 625 KB/s
    "bulk": 0,                    # Gets whatever's left
}


class AIMDController:
    """Per-path Additive Increase Multiplicative Decrease congestion control."""

    def __init__(self, estimated_bw: float = 10_000_000, alpha_ratio: float = 0.05,
                 beta: float = 0.5, min_rate_ratio: float = 0.1):
        self._estimated_bw = estimated_bw  # bytes/sec
        self._alpha = estimated_bw * alpha_ratio  # additive increase
        self._beta = beta  # multiplicative decrease
        self._min_rate = estimated_bw * min_rate_ratio
        self._send_rate = estimated_bw * 0.8  # Start at 80% of estimated
        self._last_increase = time.monotonic()
        self._increase_interval = 0.1  # 100ms
        self._loss_events = 0
        self._increase_events = 0

    @property
    def send_rate(self) -> float:
        """Current send rate in bytes/sec."""
        return self._send_rate

    @property
    def send_rate_mbps(self) -> float:
        return self._send_rate * 8 / 1_000_000

    def update_estimated_bw(self, bw_bytes_per_sec: float) -> None:
        """Update the bandwidth estimate (from BandwidthEstimator)."""
        self._estimated_bw = bw_bytes_per_sec
        self._alpha = bw_bytes_per_sec * 0.05
        self._min_rate = bw_bytes_per_sec * 0.1
        # Don't let send_rate exceed 1.2x estimated
        self._send_rate = min(self._send_rate, bw_bytes_per_sec * 1.2)

    def on_success(self) -> None:
        """Called when packets are delivered without loss over a 100ms window."""
        now = time.monotonic()
        if now - self._last_increase >= self._increase_interval:
            old = self._send_rate
            self._send_rate = min(
                self._send_rate + self._alpha,
                self._estimated_bw * 1.2,
            )
            self._last_increase = now
            self._increase_events += 1

    def on_loss(self) -> None:
        """Called when loss is detected."""
        old = self._send_rate
        self._send_rate = max(
            self._send_rate * self._beta,
            self._min_rate,
        )
        self._loss_events += 1
        if old != self._send_rate:
            logger.debug(f"AIMD decrease: {old*8/1e6:.1f} → {self._send_rate*8/1e6:.1f} Mbps")

    def get_stats(self) -> dict:
        return {
            "send_rate_mbps": round(self.send_rate_mbps, 1),
            "estimated_bw_mbps": round(self._estimated_bw * 8 / 1_000_000, 1),
            "loss_events": self._loss_events,
            "increase_events": self._increase_events,
            "utilization_pct": round(
                self._send_rate / max(self._estimated_bw, 1) * 100, 1
            ),
        }


class TierBandwidthManager:
    """Manages per-tier bandwidth reservation across all paths."""

    def __init__(self, total_capacity_bytes_per_sec: float = 10_000_000):
        self._total_capacity = total_capacity_bytes_per_sec
        self._tier_usage: dict[str, float] = {}
        self._tier_limits: dict[str, float] = {}
        self._update_limits()

    def update_capacity(self, total_bytes_per_sec: float) -> None:
        self._total_capacity = total_bytes_per_sec
        self._update_limits()

    def _update_limits(self) -> None:
        """Compute per-tier limits based on guarantees and available capacity."""
        guaranteed = sum(TIER_GUARANTEES.values())
        surplus = max(0, self._total_capacity - guaranteed)

        for tier, guarantee in TIER_GUARANTEES.items():
            # Each tier gets its guarantee + proportional surplus
            weight = guarantee / max(guaranteed, 1) if guaranteed > 0 else 1.0 / 3
            self._tier_limits[tier] = guarantee + surplus * weight

        # Bulk gets all surplus beyond other guarantees
        self._tier_limits["bulk"] = max(
            0, self._total_capacity - TIER_GUARANTEES["realtime"] - TIER_GUARANTEES["streaming"]
        )

    def can_send(self, tier: str, nbytes: int) -> bool:
        """Check if tier has budget to send."""
        used = self._tier_usage.get(tier, 0)
        limit = self._tier_limits.get(tier, self._total_capacity)
        return used + nbytes <= limit

    def record_send(self, tier: str, nbytes: int) -> None:
        self._tier_usage[tier] = self._tier_usage.get(tier, 0) + nbytes

    def reset_window(self) -> None:
        """Reset usage counters (call every 1 second)."""
        self._tier_usage = {}

    def get_stats(self) -> dict:
        return {
            "total_capacity_mbps": round(self._total_capacity * 8 / 1e6, 1),
            "tier_limits_mbps": {
                t: round(l * 8 / 1e6, 1) for t, l in self._tier_limits.items()
            },
            "tier_usage_mbps": {
                t: round(u * 8 / 1e6, 1) for t, u in self._tier_usage.items()
            },
        }
