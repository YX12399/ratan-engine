"""
One-Way Delay Estimation — simplified NTP-style clock offset estimation.

Computes asymmetric path delays (upload vs download) instead of RTT/2.

Protocol:
  Client sends T1 in keepalive → Server records T2, replies with T2 and T3
  Client records T4 at receive

  offset = ((T2 - T1) - (T4 - T3)) / 2
  owd_up   = (T2 - T1) - offset
  owd_down = (T4 - T3) + offset
"""

import logging
import time
from collections import deque
from typing import Optional

logger = logging.getLogger("hyperagg.scheduler.owd")


class OWDEstimator:
    """Per-path one-way delay estimator."""

    def __init__(self, ewma_alpha: float = 0.1, history_size: int = 50):
        self._alpha = ewma_alpha
        # path_id -> estimates
        self._owd_up: dict[int, float] = {}      # ms
        self._owd_down: dict[int, float] = {}     # ms
        self._offset: dict[int, float] = {}       # ms
        self._history_up: dict[int, deque] = {}
        self._history_down: dict[int, deque] = {}
        self._history_size = history_size

    def record_timestamps(self, path_id: int, t1: float, t2: float,
                          t3: float, t4: float) -> None:
        """
        Record a full timestamp exchange.

        Args:
            t1: Client send time (monotonic seconds)
            t2: Server receive time (monotonic seconds)
            t3: Server send time (monotonic seconds)
            t4: Client receive time (monotonic seconds)

        All times in seconds (will be converted to ms internally).
        """
        if path_id not in self._history_up:
            self._history_up[path_id] = deque(maxlen=self._history_size)
            self._history_down[path_id] = deque(maxlen=self._history_size)

        # Convert to ms
        t1_ms, t2_ms, t3_ms, t4_ms = t1 * 1000, t2 * 1000, t3 * 1000, t4 * 1000

        # NTP-style offset calculation
        offset = ((t2_ms - t1_ms) - (t4_ms - t3_ms)) / 2.0
        owd_up = (t2_ms - t1_ms) - offset
        owd_down = (t4_ms - t3_ms) + offset

        # Sanity checks (OWD can't be negative)
        owd_up = max(0.1, owd_up)
        owd_down = max(0.1, owd_down)

        # EWMA smoothing (alpha=0.1 for slow clock drift tracking)
        if path_id in self._owd_up:
            self._owd_up[path_id] = self._alpha * owd_up + (1 - self._alpha) * self._owd_up[path_id]
            self._owd_down[path_id] = self._alpha * owd_down + (1 - self._alpha) * self._owd_down[path_id]
            self._offset[path_id] = self._alpha * offset + (1 - self._alpha) * self._offset[path_id]
        else:
            self._owd_up[path_id] = owd_up
            self._owd_down[path_id] = owd_down
            self._offset[path_id] = offset

        self._history_up[path_id].append(owd_up)
        self._history_down[path_id].append(owd_down)

    def record_rtt_with_asymmetry(self, path_id: int, rtt_ms: float,
                                   up_ratio: float = 0.5) -> None:
        """
        Estimate OWD from RTT when full timestamp exchange isn't available.
        Uses a configurable up/down ratio (default 0.5 = symmetric).

        For Starlink: up_ratio ≈ 0.33 (upload slower, 2:1 down:up)
        For cellular: up_ratio ≈ 0.45 (slightly asymmetric)
        """
        owd_up = rtt_ms * up_ratio
        owd_down = rtt_ms * (1 - up_ratio)

        if path_id not in self._history_up:
            self._history_up[path_id] = deque(maxlen=self._history_size)
            self._history_down[path_id] = deque(maxlen=self._history_size)

        if path_id in self._owd_up:
            self._owd_up[path_id] = self._alpha * owd_up + (1 - self._alpha) * self._owd_up[path_id]
            self._owd_down[path_id] = self._alpha * owd_down + (1 - self._alpha) * self._owd_down[path_id]
        else:
            self._owd_up[path_id] = owd_up
            self._owd_down[path_id] = owd_down

        self._history_up[path_id].append(owd_up)
        self._history_down[path_id].append(owd_down)

    def get_owd_up(self, path_id: int) -> float:
        """Get smoothed one-way delay client→server in ms."""
        return self._owd_up.get(path_id, 0.0)

    def get_owd_down(self, path_id: int) -> float:
        """Get smoothed one-way delay server→client in ms."""
        return self._owd_down.get(path_id, 0.0)

    def get_download_differential(self) -> float:
        """
        Get max(owd_down) - min(owd_down) across all paths.
        Used by the reorder buffer instead of RTT-based differential.
        """
        if len(self._owd_down) < 2:
            return 0.0
        values = [v for v in self._owd_down.values() if v > 0]
        if len(values) < 2:
            return 0.0
        return max(values) - min(values)

    def get_best_download_path(self) -> Optional[int]:
        """Get the path with lowest download OWD."""
        if not self._owd_down:
            return None
        return min(self._owd_down, key=self._owd_down.get)

    def get_best_upload_path(self) -> Optional[int]:
        """Get the path with lowest upload OWD."""
        if not self._owd_up:
            return None
        return min(self._owd_up, key=self._owd_up.get)

    def get_all_owd(self) -> dict:
        """Get all OWD estimates."""
        result = {}
        for pid in set(list(self._owd_up.keys()) + list(self._owd_down.keys())):
            result[pid] = {
                "owd_up_ms": round(self._owd_up.get(pid, 0), 1),
                "owd_down_ms": round(self._owd_down.get(pid, 0), 1),
                "asymmetry_ratio": round(
                    self._owd_up.get(pid, 1) / max(self._owd_down.get(pid, 1), 0.1), 2
                ),
            }
        return result

    def get_stats(self) -> dict:
        return {
            "paths": self.get_all_owd(),
            "download_differential_ms": round(self.get_download_differential(), 1),
            "best_upload_path": self.get_best_upload_path(),
            "best_download_path": self.get_best_download_path(),
        }
