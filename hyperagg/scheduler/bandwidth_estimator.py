"""
Bandwidth Estimation + Token Bucket Pacing.

Bandwidth estimation: track bytes acked per path over sliding 1-second windows.
Token bucket pacer: each path gets a bucket that refills at estimated_bw bytes/sec.
"""

import logging
import time
from collections import deque
from typing import Optional

logger = logging.getLogger("hyperagg.scheduler.bw")


class BandwidthEstimator:
    """Per-path bandwidth estimation using sliding window + EWMA."""

    def __init__(self, ewma_alpha: float = 0.2, window_sec: float = 1.0):
        self._alpha = ewma_alpha
        self._window_sec = window_sec
        # path_id -> deque of (timestamp, bytes)
        self._samples: dict[int, deque] = {}
        self._ewma_bw: dict[int, float] = {}  # bytes/sec

    def record_ack(self, path_id: int, nbytes: int) -> None:
        """Record acknowledged bytes on a path."""
        now = time.monotonic()
        if path_id not in self._samples:
            self._samples[path_id] = deque()
            self._ewma_bw[path_id] = 0.0
        self._samples[path_id].append((now, nbytes))
        self._prune(path_id, now)

    def get_estimated_bw(self, path_id: int) -> float:
        """Get estimated bandwidth in bytes/sec for a path."""
        now = time.monotonic()
        self._prune(path_id, now)
        samples = self._samples.get(path_id)
        if not samples or len(samples) < 2:
            return self._ewma_bw.get(path_id, 0.0)

        total_bytes = sum(b for _, b in samples)
        span = now - samples[0][0]
        if span < 0.01:
            return self._ewma_bw.get(path_id, 0.0)

        raw_bw = total_bytes / span
        prev = self._ewma_bw.get(path_id, raw_bw)
        smoothed = self._alpha * raw_bw + (1 - self._alpha) * prev
        self._ewma_bw[path_id] = smoothed
        return smoothed

    def get_estimated_bw_mbps(self, path_id: int) -> float:
        return self.get_estimated_bw(path_id) * 8 / 1_000_000

    def _prune(self, path_id: int, now: float) -> None:
        samples = self._samples.get(path_id)
        if not samples:
            return
        cutoff = now - self._window_sec
        while samples and samples[0][0] < cutoff:
            samples.popleft()


class TokenBucketPacer:
    """Per-path token bucket for pacing packet sends."""

    def __init__(self, max_wait_ms: float = 10.0):
        self._max_wait_ms = max_wait_ms
        self._buckets: dict[int, float] = {}        # path_id -> tokens (bytes)
        self._rates: dict[int, float] = {}           # path_id -> bytes/sec
        self._last_refill: dict[int, float] = {}     # path_id -> timestamp
        self._max_burst: dict[int, float] = {}       # path_id -> max bucket size

    def set_rate(self, path_id: int, rate_bytes_per_sec: float) -> None:
        """Set the refill rate for a path's bucket."""
        self._rates[path_id] = rate_bytes_per_sec
        self._max_burst[path_id] = rate_bytes_per_sec * 0.05  # 50ms burst
        if path_id not in self._buckets:
            self._buckets[path_id] = self._max_burst[path_id]
            self._last_refill[path_id] = time.monotonic()

    def can_send(self, path_id: int, nbytes: int) -> bool:
        """Check if the bucket has enough tokens to send nbytes."""
        self._refill(path_id)
        return self._buckets.get(path_id, 0) >= nbytes

    def consume(self, path_id: int, nbytes: int) -> bool:
        """
        Try to consume tokens. Returns True if successful.
        If not enough tokens, returns False (caller should try another path).
        """
        self._refill(path_id)
        bucket = self._buckets.get(path_id, 0)
        if bucket >= nbytes:
            self._buckets[path_id] = bucket - nbytes
            return True
        return False

    def tokens_available(self, path_id: int) -> float:
        """Get current token count for a path."""
        self._refill(path_id)
        return self._buckets.get(path_id, 0)

    def time_until_available(self, path_id: int, nbytes: int) -> float:
        """Estimate milliseconds until nbytes tokens are available."""
        self._refill(path_id)
        deficit = nbytes - self._buckets.get(path_id, 0)
        if deficit <= 0:
            return 0.0
        rate = self._rates.get(path_id, 1)
        return (deficit / rate) * 1000 if rate > 0 else self._max_wait_ms

    def _refill(self, path_id: int) -> None:
        now = time.monotonic()
        last = self._last_refill.get(path_id, now)
        elapsed = now - last
        if elapsed <= 0:
            return
        rate = self._rates.get(path_id, 0)
        added = rate * elapsed
        max_b = self._max_burst.get(path_id, rate * 0.05)
        self._buckets[path_id] = min(
            self._buckets.get(path_id, 0) + added, max_b
        )
        self._last_refill[path_id] = now

    def get_stats(self) -> dict:
        return {
            pid: {
                "rate_mbps": round(self._rates.get(pid, 0) * 8 / 1_000_000, 1),
                "tokens_bytes": round(self._buckets.get(pid, 0)),
                "max_burst_bytes": round(self._max_burst.get(pid, 0)),
            }
            for pid in self._rates
        }
